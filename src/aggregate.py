"""
Pure aggregation logic.

Nothing in this module touches the network, the filesystem, or protobuf. It
takes plain Python dicts in and returns plain Python dicts out, which is what
makes it testable offline (see tests/test_aggregate.py) and what lets the whole
pipeline be exercised with fixtures before it ever runs against the live feed.

The split is deliberate:
    collect_realtime.py  -> I/O and protobuf decoding (thin, hard to unit test)
    aggregate.py         -> all the arithmetic and judgement (thick, fully tested)
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Sequence

from common import (
    MAX_PLAUSIBLE_DELAY_S,
    ON_TIME_EARLY_S,
    ON_TIME_LATE_S,
)

# Column order for the snapshot CSV. Declared once so the header written on a
# brand-new monthly partition can never drift from the rows appended to it.
SNAPSHOT_COLUMNS = [
    "ts_utc",
    "route_id",
    "route_name",
    "mode",
    "n_vehicles",
    "n_trips",
    "median_delay_s",
    "p90_delay_s",
    "pct_on_time",
    "pct_late",
    "pct_early",
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
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(vals[int(pos)])
    frac = pos - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def pct(numerator: int, denominator: int) -> float | None:
    """Percentage, or None when there is nothing to divide by."""
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def _round(x, nd=1):
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
def clean_delays(delays: Iterable[float]) -> list[float]:
    """
    Drop delay readings that cannot be real.

    GTFS-RT feeds routinely emit garbage when a vehicle is still broadcasting
    against a trip it finished hours ago: you see delays of 30,000+ seconds that
    would otherwise drag a route's mean into nonsense. Filtering these is the
    single most important cleaning step in the whole pipeline, and it is why the
    dashboard reports medians rather than means as well.
    """
    out = []
    for d in delays:
        if d is None:
            continue
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if math.isnan(d) or abs(d) > MAX_PLAUSIBLE_DELAY_S:
            continue
        out.append(d)
    return out


def classify(delay_s: float) -> str:
    """Bucket a single delay reading into early / on_time / late."""
    if delay_s < ON_TIME_EARLY_S:
        return "early"
    if delay_s > ON_TIME_LATE_S:
        return "late"
    return "on_time"


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------
def build_snapshot(
    ts_utc: str,
    vehicles: Iterable[dict],
    trip_updates: Iterable[dict],
    alerts: Iterable[dict],
    routes: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Collapse one moment of the live feed into one row per route.

    Inputs are already-decoded plain dicts:
        vehicles      {"route_id": str, "vehicle_id": str}
        trip_updates  {"route_id": str, "delay_s": float}
        alerts        {"route_ids": [str, ...]}
        routes        {route_id: {"route_name": str, "mode": str}}

    Returns a list of dicts using SNAPSHOT_COLUMNS. One row per route that
    appears anywhere in the feed, plus a synthetic "ALL" row carrying the
    system-wide figure so the dashboard never has to re-derive it.
    """
    routes = routes or {}

    vehicles_by_route: dict[str, set] = defaultdict(set)
    for v in vehicles:
        rid = str(v.get("route_id") or "").strip()
        if rid:
            vehicles_by_route[rid].add(v.get("vehicle_id") or id(v))

    delays_by_route: dict[str, list[float]] = defaultdict(list)
    for t in trip_updates:
        rid = str(t.get("route_id") or "").strip()
        if rid:
            delays_by_route[rid].append(t.get("delay_s"))

    alerts_by_route: dict[str, int] = defaultdict(int)
    for a in alerts:
        for rid in a.get("route_ids") or []:
            rid = str(rid).strip()
            if rid:
                alerts_by_route[rid] += 1

    all_routes = set(vehicles_by_route) | set(delays_by_route) | set(alerts_by_route)

    rows: list[dict] = []
    system_delays: list[float] = []
    system_vehicles = 0

    for rid in sorted(all_routes, key=_route_sort_key):
        cleaned = clean_delays(delays_by_route.get(rid, []))
        buckets = defaultdict(int)
        for d in cleaned:
            buckets[classify(d)] += 1
        n = len(cleaned)

        meta = routes.get(rid, {})
        n_veh = len(vehicles_by_route.get(rid, ()))
        system_delays.extend(cleaned)
        system_vehicles += n_veh

        rows.append(
            {
                "ts_utc": ts_utc,
                "route_id": rid,
                "route_name": meta.get("route_name") or rid,
                "mode": meta.get("mode") or "unknown",
                "n_vehicles": n_veh,
                "n_trips": n,
                "median_delay_s": _round(median(cleaned)),
                "p90_delay_s": _round(percentile(cleaned, 90)),
                "pct_on_time": pct(buckets["on_time"], n),
                "pct_late": pct(buckets["late"], n),
                "pct_early": pct(buckets["early"], n),
                "n_alerts": alerts_by_route.get(rid, 0),
            }
        )

    sys_buckets = defaultdict(int)
    for d in system_delays:
        sys_buckets[classify(d)] += 1
    n_sys = len(system_delays)

    rows.append(
        {
            "ts_utc": ts_utc,
            "route_id": "ALL",
            "route_name": "System-wide",
            "mode": "all",
            "n_vehicles": system_vehicles,
            "n_trips": n_sys,
            "median_delay_s": _round(median(system_delays)),
            "p90_delay_s": _round(percentile(system_delays, 90)),
            "pct_on_time": pct(sys_buckets["on_time"], n_sys),
            "pct_late": pct(sys_buckets["late"], n_sys),
            "pct_early": pct(sys_buckets["early"], n_sys),
            "n_alerts": sum(alerts_by_route.values()),
        }
    )
    return rows


def _route_sort_key(route_id: str):
    """Sort 501 before 96 (numeric where possible), keeping output stable."""
    rid = str(route_id)
    return (0, int(rid), "") if rid.isdigit() else (1, 0, rid)


# ---------------------------------------------------------------------------
# Live view for the dashboard
# ---------------------------------------------------------------------------
def build_live_payload(rows: list[dict], worst_n: int = 8) -> dict:
    """
    Shape one snapshot into exactly what the dashboard needs to render "now".

    Doing this server-side (well, Actions-side) rather than in the browser keeps
    the page fast and means the site works with a single small fetch instead of
    parsing a growing CSV in JavaScript.
    """
    by_id = {r["route_id"]: r for r in rows}
    system = by_id.get("ALL", {})
    routes = [r for r in rows if r["route_id"] != "ALL"]

    # Only rank routes with enough observations for the number to mean anything.
    # Without this floor, a route with two reporting vehicles and one stuck bus
    # shows "0% on time" and tops the worst list every time.
    rankable = [r for r in routes if (r.get("n_trips") or 0) >= 5 and r.get("pct_on_time") is not None]
    worst = sorted(rankable, key=lambda r: (r["pct_on_time"], -(r["n_trips"] or 0)))[:worst_n]
    best = sorted(rankable, key=lambda r: (-r["pct_on_time"], -(r["n_trips"] or 0)))[:worst_n]

    return {
        "generated_utc": system.get("ts_utc"),
        "system": {
            "vehicles": system.get("n_vehicles", 0),
            "trips": system.get("n_trips", 0),
            "median_delay_s": system.get("median_delay_s"),
            "p90_delay_s": system.get("p90_delay_s"),
            "pct_on_time": system.get("pct_on_time"),
            "pct_late": system.get("pct_late"),
            "pct_early": system.get("pct_early"),
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

    1008 = 7 days at one snapshot per 10 minutes. The dashboard only ever plots
    a week, so shipping more than that to the browser is wasted bytes; the full
    history stays in the CSV partitions for anyone who clones the repo.
    """
    rows = [r for r in system_rows if r.get("route_id") == "ALL"]
    rows.sort(key=lambda r: r.get("ts_utc") or "")
    trimmed = rows[-keep:]
    return [
        {
            "ts_utc": r["ts_utc"],
            "pct_on_time": _num(r.get("pct_on_time")),
            "median_delay_s": _num(r.get("median_delay_s")),
            "vehicles": int(_num(r.get("n_vehicles")) or 0),
        }
        for r in trimmed
    ]


def _num(x):
    """CSV round-trips everything to strings; coerce back, tolerating blanks."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def hourly_profile(system_rows: list[dict], tz_offset_hours: int = -4) -> list[dict]:
    """
    Average on-time performance by hour of the local day.

    Answers the question the live view cannot: not "how is the system right
    now" but "when is the system reliably bad". The offset is applied crudely
    rather than with a tz database because the aggregate is over weeks and an
    hour of DST drift does not move a 24-bucket average meaningfully.
    """
    buckets: dict[int, list[float]] = defaultdict(list)
    for r in system_rows:
        if r.get("route_id") != "ALL":
            continue
        val = _num(r.get("pct_on_time"))
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
            "pct_on_time": _round(sum(buckets[h]) / len(buckets[h])) if buckets.get(h) else None,
            "n": len(buckets.get(h, [])),
        }
        for h in range(24)
    ]
