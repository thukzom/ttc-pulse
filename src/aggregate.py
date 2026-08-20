"""
Pure aggregation logic.

Nothing in this module touches the network, the filesystem, or protobuf. It
takes plain Python dicts in and returns plain Python dicts out, which is what
makes it testable offline (see tests/test_aggregate.py) and what lets the whole
pipeline be exercised with fixtures before it ever runs against the live feed.

    collect_realtime.py  -> I/O and protobuf decoding (thin, hard to unit test)
    aggregate.py         -> all the arithmetic and judgement (thick, fully tested)


WHY HEADWAY REGULARITY AND NOT ON-TIME PERFORMANCE
--------------------------------------------------
The obvious metric for transit reliability is schedule adherence: how late is
this vehicle against its timetable. That is not computable from the TTC's feed,
and it is worth understanding why before reading the code.

Inspecting the live feed shows every TripUpdate carries
`schedule_relationship: NEW` with a synthetic negative trip_id, and every
StopTimeUpdate carries an absolute `arrival.time` with no `delay` field:

    trip { trip_id: "-687326008"  schedule_relationship: NEW  route_id: "39" }
    stop_time_update { stop_sequence: 2  arrival { time: 1787113654 } ... }

`NEW` means the trip does not correspond to any trip in the published timetable,
so there is no scheduled arrival to subtract from the predicted one. The feed is
a *prediction* feed, not a schedule-deviation feed.

What the feed does support is the metric that actually matters on frequent
service, where riders turn up without consulting a timetable: are vehicles
evenly spaced? A rider on a route with a 10-minute average wait does not care
that the bus is "6 minutes late" against a schedule they never read - they care
that three buses arrived together and then nothing came for 25 minutes. That is
bunching, and it is the dominant failure mode of high-frequency transit.

So: for each route and stop we take the predicted arrivals, sort them, take the
gaps between consecutive arrivals, and measure how uniform those gaps are.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Sequence

from common import (
    HEADWAY_MAX_S,
    HEADWAY_MIN_S,
    MIN_GAPS_PER_STOP,
    PREDICTION_WINDOW_S,
    REGULAR_HIGH,
    REGULAR_LOW,
)

# Column order for the snapshot CSV. Declared once so the header written on a
# brand-new monthly partition can never drift from the rows appended to it.
SNAPSHOT_COLUMNS = [
    "ts_utc",
    "route_id",
    "route_name",
    "mode",
    "n_vehicles",
    "n_stops_measured",
    "n_gaps",
    "median_headway_s",
    "headway_cv",
    "pct_regular",
    "pct_bunched",
    "pct_gapped",
    "n_alerts",
]


# ---------------------------------------------------------------------------
# Small statistical helpers
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], q: float) -> float | None:
    """
    Linear-interpolated percentile, q in [0, 100].

    Written by hand rather than pulled from numpy so this module has zero
    third-party dependencies and can run in any Python 3.10+ environment.
    """
    vals = sorted(v for v in values if v is not None and not math.isnan(v))
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * (q / 100.0)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(vals[int(pos)])
    frac = pos - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def mean(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(values: Sequence[float]) -> float | None:
    """Population standard deviation; None for fewer than two values."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def pct(numerator: int, denominator: int) -> float | None:
    """Percentage, or None when there is nothing to divide by."""
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def _round(x, nd=1):
    return None if x is None else round(x, nd)


def _num(x):
    """CSV round-trips everything to strings; coerce back, tolerating blanks."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Headways
# ---------------------------------------------------------------------------
def gaps_from_times(times: Iterable[float]) -> list[float]:
    """
    Turn a set of predicted arrival times at one stop into headways.

    Sorts, differences consecutive values, and drops gaps outside a plausible
    band. Sub-30-second gaps are almost always the same vehicle reported twice
    rather than two genuinely simultaneous arrivals; gaps beyond an hour mean
    the next prediction is so far out it says nothing about current service.
    """
    ts = sorted(set(float(t) for t in times if t is not None))
    out = []
    for a, b in zip(ts, ts[1:]):
        gap = b - a
        if HEADWAY_MIN_S <= gap <= HEADWAY_MAX_S:
            out.append(gap)
    return out


def classify_gap(gap: float, reference: float) -> str:
    """
    Label one headway against the local average headway at that stop.

    The comparison is deliberately relative rather than absolute: a 4-minute gap
    is perfectly regular on King Street and a sign of bunching on a route that
    runs every 20 minutes. Each stop is judged against its own rhythm.
    """
    if reference <= 0:
        return "regular"
    ratio = gap / reference
    if ratio < REGULAR_LOW:
        return "bunched"
    if ratio > REGULAR_HIGH:
        return "gapped"
    return "regular"


def stop_headway_stats(times: Iterable[float]) -> dict | None:
    """
    Headway statistics for a single (route, stop) pair.

    Returns None when there are too few predicted arrivals for the numbers to
    mean anything. Requiring several gaps rather than one is what stops a stop
    with two predictions from generating a confident-looking 0% regularity.
    """
    gaps = gaps_from_times(times)
    if len(gaps) < MIN_GAPS_PER_STOP:
        return None
    ref = mean(gaps)
    buckets = defaultdict(int)
    for g in gaps:
        buckets[classify_gap(g, ref)] += 1
    sd = stdev(gaps)
    return {
        "gaps": gaps,
        "mean": ref,
        # Coefficient of variation: the standard measure of headway regularity.
        # 0 is perfectly even spacing; above ~0.5 means riders experience the
        # service as unpredictable regardless of how frequent it is on paper.
        "cv": (sd / ref) if (sd is not None and ref) else None,
        "regular": buckets["regular"],
        "bunched": buckets["bunched"],
        "gapped": buckets["gapped"],
    }


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------
def build_snapshot(
    ts_utc: str,
    vehicles: Iterable[dict],
    predictions: Iterable[dict],
    alerts: Iterable[dict],
    routes: dict[str, dict] | None = None,
    now_epoch: float | None = None,
) -> list[dict]:
    """
    Collapse one moment of the live feed into one row per route.

    Inputs are already-decoded plain dicts:
        vehicles     {"route_id": str, "vehicle_id": str}
        predictions  {"route_id": str, "stop_id": str, "trip_key": str, "time": float}
        alerts       {"route_ids": [str, ...]}
        routes       {route_id: {"route_name": str, "mode": str}}

    Returns a list of dicts using SNAPSHOT_COLUMNS: one row per route seen
    anywhere in the feed, plus a synthetic "ALL" row carrying the system-wide
    figure so the dashboard never has to re-derive it.
    """
    routes = routes or {}

    vehicles_by_route: dict[str, set] = defaultdict(set)
    for v in vehicles:
        rid = str(v.get("route_id") or "").strip()
        if rid:
            vehicles_by_route[rid].add(v.get("vehicle_id") or id(v))

    # Group predicted arrivals by route and stop. Deduplicated on
    # (trip, stop) so a trip listed twice in one feed cannot manufacture a
    # zero-length headway.
    by_route_stop: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for p in predictions:
        rid = str(p.get("route_id") or "").strip()
        sid = str(p.get("stop_id") or "").strip()
        t = p.get("time")
        if not rid or not sid or t is None:
            continue
        t = float(t)
        # Only predictions inside the forward window. Far-future predictions are
        # the agency's own guesswork compounding, not a measurement of service.
        if now_epoch is not None and not (now_epoch - 60 <= t <= now_epoch + PREDICTION_WINDOW_S):
            continue
        key = str(p.get("trip_key") or t)
        prev = by_route_stop[(rid, sid)].get(key)
        if prev is None or t < prev:
            by_route_stop[(rid, sid)][key] = t

    stops_by_route: dict[str, list[dict]] = defaultdict(list)
    for (rid, _sid), trip_times in by_route_stop.items():
        stats = stop_headway_stats(trip_times.values())
        if stats:
            stops_by_route[rid].append(stats)

    alerts_by_route: dict[str, int] = defaultdict(int)
    for a in alerts:
        for rid in a.get("route_ids") or []:
            rid = str(rid).strip()
            if rid:
                alerts_by_route[rid] += 1

    all_routes = set(vehicles_by_route) | {r for r, _ in by_route_stop} | set(alerts_by_route)

    rows: list[dict] = []
    sys_gaps: list[float] = []
    sys_cvs: list[float] = []
    sys_buckets: dict[str, int] = defaultdict(int)
    sys_vehicles = 0
    sys_stops = 0

    for rid in sorted(all_routes, key=_route_sort_key):
        meta = routes.get(rid, {})
        n_veh = len(vehicles_by_route.get(rid, ()))
        stops = stops_by_route.get(rid, [])

        gaps = [g for s in stops for g in s["gaps"]]
        cvs = [s["cv"] for s in stops if s["cv"] is not None]
        reg = sum(s["regular"] for s in stops)
        bun = sum(s["bunched"] for s in stops)
        gap_ = sum(s["gapped"] for s in stops)
        total = reg + bun + gap_

        sys_vehicles += n_veh
        sys_stops += len(stops)
        sys_gaps.extend(gaps)
        sys_cvs.extend(cvs)
        sys_buckets["regular"] += reg
        sys_buckets["bunched"] += bun
        sys_buckets["gapped"] += gap_

        rows.append({
            "ts_utc": ts_utc,
            "route_id": rid,
            "route_name": meta.get("route_name") or rid,
            "mode": meta.get("mode") or "unknown",
            "n_vehicles": n_veh,
            "n_stops_measured": len(stops),
            "n_gaps": total,
            "median_headway_s": _round(median(gaps), 0),
            # Route-level CV is the median of its stops' CVs rather than a CV
            # over all gaps pooled together: pooling stops with genuinely
            # different headways would report their difference as irregularity.
            "headway_cv": _round(median(cvs), 3),
            "pct_regular": pct(reg, total),
            "pct_bunched": pct(bun, total),
            "pct_gapped": pct(gap_, total),
            "n_alerts": alerts_by_route.get(rid, 0),
        })

    sys_total = sum(sys_buckets.values())
    rows.append({
        "ts_utc": ts_utc,
        "route_id": "ALL",
        "route_name": "System-wide",
        "mode": "all",
        "n_vehicles": sys_vehicles,
        "n_stops_measured": sys_stops,
        "n_gaps": sys_total,
        "median_headway_s": _round(median(sys_gaps), 0),
        "headway_cv": _round(median(sys_cvs), 3),
        "pct_regular": pct(sys_buckets["regular"], sys_total),
        "pct_bunched": pct(sys_buckets["bunched"], sys_total),
        "pct_gapped": pct(sys_buckets["gapped"], sys_total),
        "n_alerts": sum(alerts_by_route.values()),
    })
    return rows


def _route_sort_key(route_id: str):
    """Sort 96 before 501 (numeric where possible), keeping output stable."""
    rid = str(route_id)
    return (0, int(rid), "") if rid.isdigit() else (1, 0, rid)


# ---------------------------------------------------------------------------
# Live view for the dashboard
# ---------------------------------------------------------------------------
def build_live_payload(rows: list[dict], worst_n: int = 8, min_gaps: int = 12) -> dict:
    """
    Shape one snapshot into exactly what the dashboard needs to render "now".

    Doing this here rather than in the browser keeps the page to a single small
    fetch instead of parsing a growing CSV in JavaScript.
    """
    by_id = {r["route_id"]: r for r in rows}
    system = by_id.get("ALL", {})
    routes = [r for r in rows if r["route_id"] != "ALL"]

    # Only rank routes with enough measured gaps for the number to mean
    # anything. Without this floor, a route with one qualifying stop and three
    # gaps shows 0% regular and tops the worst list on pure noise.
    rankable = [r for r in routes
                if (r.get("n_gaps") or 0) >= min_gaps and r.get("pct_regular") is not None]
    worst = sorted(rankable, key=lambda r: (r["pct_regular"], -(r["n_gaps"] or 0)))[:worst_n]
    best = sorted(rankable, key=lambda r: (-r["pct_regular"], -(r["n_gaps"] or 0)))[:worst_n]

    return {
        "generated_utc": system.get("ts_utc"),
        "system": {
            "vehicles": system.get("n_vehicles", 0),
            "stops_measured": system.get("n_stops_measured", 0),
            "gaps": system.get("n_gaps", 0),
            "median_headway_s": system.get("median_headway_s"),
            "headway_cv": system.get("headway_cv"),
            "pct_regular": system.get("pct_regular"),
            "pct_bunched": system.get("pct_bunched"),
            "pct_gapped": system.get("pct_gapped"),
            "alerts": system.get("n_alerts", 0),
            "routes_reporting": len(routes),
            "routes_rankable": len(rankable),
        },
        "worst_routes": worst,
        "best_routes": best,
    }


# ---------------------------------------------------------------------------
# Rolling trend across many snapshots
# ---------------------------------------------------------------------------
def build_trend(system_rows: list[dict], keep: int = 1008) -> list[dict]:
    """
    Trim the system-wide series to the most recent `keep` snapshots.

    1008 = 7 days at one snapshot per 10 minutes. The dashboard only plots a
    week, so shipping more than that to the browser is wasted bytes; the full
    history stays in the CSV partitions for anyone who clones the repo.
    """
    rows = [r for r in system_rows if r.get("route_id") == "ALL"]
    rows.sort(key=lambda r: r.get("ts_utc") or "")
    return [
        {
            "ts_utc": r["ts_utc"],
            "pct_regular": _num(r.get("pct_regular")),
            "pct_bunched": _num(r.get("pct_bunched")),
            "median_headway_s": _num(r.get("median_headway_s")),
            "vehicles": int(_num(r.get("n_vehicles")) or 0),
        }
        for r in rows[-keep:]
    ]


def hourly_profile(system_rows: list[dict], tz_offset_hours: int = -4) -> list[dict]:
    """
    Average headway regularity by hour of the local day.

    Answers the question the live view cannot: not "how is the system right
    now" but "when is the system reliably irregular". The offset is applied
    crudely rather than with a timezone database because the aggregate is over
    weeks and an hour of DST drift does not move a 24-bucket average.
    """
    buckets: dict[int, list[float]] = defaultdict(list)
    for r in system_rows:
        if r.get("route_id") != "ALL":
            continue
        val = _num(r.get("pct_regular"))
        ts = r.get("ts_utc") or ""
        if val is None or len(ts) < 13:
            continue
        try:
            utc_hour = int(ts[11:13])
        except ValueError:
            continue
        buckets[(utc_hour + tz_offset_hours) % 24].append(val)

    return [
        {
            "hour": h,
            "pct_regular": _round(mean(buckets[h])) if buckets.get(h) else None,
            "n": len(buckets.get(h, [])),
        }
        for h in range(24)
    ]
