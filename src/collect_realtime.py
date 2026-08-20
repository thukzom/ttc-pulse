"""
Collect one snapshot of the TTC live feed and append it to the archive.

Runs every 10 minutes in GitHub Actions. Each run:
  1. fetches the three GTFS-Realtime feeds
  2. decodes protobuf -> plain dicts
  3. hands those to aggregate.build_snapshot() for all the arithmetic
  4. appends one row per route to data/realtime/snapshots-YYYY-MM.csv
  5. rewrites docs/data/live.json and docs/data/trend.json for the dashboard

The workflow then commits whatever changed. That "commit the data back to the
repo" pattern is the whole storage layer: no database, no server, no hosting
bill, and every historical value is auditable in git history.

Offline usage (no network needed):
    python src/collect_realtime.py --fixture
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggregate import (  # noqa: E402
    SNAPSHOT_COLUMNS,
    build_live_payload,
    build_snapshot,
    build_trend,
    hourly_profile,
)
from common import (  # noqa: E402
    CKAN,
    GTFSRT_ALERTS,
    GTFSRT_TRIPS,
    GTFSRT_VEHICLES,
    GTFS_STATIC_PACKAGE,
    REALTIME_DIR,
    SITE_DATA,
    DATA,
    iso,
    log,
    month_partition,
    read_json,
    utcnow,
    write_json,
)

ROUTES_CACHE = DATA / "routes.json"
USER_AGENT = "ttc-pulse/1.0 (open-data portfolio project; contact via GitHub issues)"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: int = 45) -> bytes:
    import requests

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content


def fetch_feed(url: str):
    """
    Fetch and decode one GTFS-Realtime feed.

    Returns None rather than raising: a single feed being briefly unavailable
    should degrade the snapshot, not fail the run. A failed run would leave a
    gap in the time series; a partial one at least records vehicle counts.
    """
    from google.transit import gtfs_realtime_pb2

    try:
        raw = http_get(url)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        log(f"WARN could not fetch {url}: {exc}")
        return None

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(raw)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN could not parse {url}: {exc}")
        return None
    return feed


# ---------------------------------------------------------------------------
# Protobuf -> plain dicts
# ---------------------------------------------------------------------------
def decode_vehicles(feed) -> list[dict]:
    out = []
    if feed is None:
        return out
    for ent in feed.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        out.append(
            {
                "route_id": v.trip.route_id or "",
                "vehicle_id": v.vehicle.id or ent.id,
                "lat": round(v.position.latitude, 5) if v.HasField("position") else None,
                "lon": round(v.position.longitude, 5) if v.HasField("position") else None,
            }
        )
    return out


def decode_predictions(feed) -> list[dict]:
    """
    Pull every predicted stop arrival out of the TripUpdate feed.

    The TTC feed gives absolute epoch times and no `delay` field, and marks its
    trips `schedule_relationship: NEW`, so there is nothing to compare against a
    timetable. What we extract instead is (route, stop, trip, predicted time) -
    the raw material for headway regularity, computed in aggregate.py.

    Departure time is used when a stop carries no arrival, which is how the
    first stop of a trip is normally expressed.
    """
    out = []
    if feed is None:
        return out
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        route_id = tu.trip.route_id or ""
        if not route_id:
            continue
        # trip_id is synthetic and negative on this feed, but it is still a
        # stable key *within* a snapshot, which is all the dedup needs.
        trip_key = tu.trip.trip_id or ent.id
        for stu in tu.stop_time_update:
            stop_id = stu.stop_id or ""
            if not stop_id:
                continue
            t = None
            if stu.HasField("arrival") and stu.arrival.HasField("time"):
                t = stu.arrival.time
            elif stu.HasField("departure") and stu.departure.HasField("time"):
                t = stu.departure.time
            if not t:
                continue
            out.append({"route_id": route_id, "stop_id": stop_id,
                        "trip_key": trip_key, "time": float(t)})
    return out


def decode_alerts(feed) -> list[dict]:
    out = []
    if feed is None:
        return out
    for ent in feed.entity:
        if not ent.HasField("alert"):
            continue
        rids = [ie.route_id for ie in ent.alert.informed_entity if ie.route_id]
        header = ""
        if ent.alert.header_text.translation:
            header = ent.alert.header_text.translation[0].text
        out.append({"route_ids": rids, "header": header})
    return out


# ---------------------------------------------------------------------------
# Route names from the static GTFS bundle
# ---------------------------------------------------------------------------
def load_routes(refresh: bool = False) -> dict[str, dict]:
    """
    Map route_id -> {route_name, mode}, cached on disk.

    The realtime feed identifies routes only by id. Names come from the static
    GTFS zip, which the TTC republishes roughly every six weeks, so it is
    fetched at most once a week rather than on every 10-minute run.
    """
    cached = read_json(ROUTES_CACHE)
    if cached and not refresh:
        return cached

    try:
        import requests

        meta = requests.get(
            f"{CKAN}/package_show", params={"id": GTFS_STATIC_PACKAGE},
            timeout=60, headers={"User-Agent": USER_AGENT},
        ).json()
        zip_url = next(
            r["url"] for r in meta["result"]["resources"]
            if (r.get("format") or "").upper() == "ZIP"
        )
        blob = http_get(zip_url, timeout=180)
        routes: dict[str, dict] = {}
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            name = next(n for n in zf.namelist() if n.endswith("routes.txt"))
            text = zf.read(name).decode("utf-8-sig")
            for row in csv.DictReader(io.StringIO(text)):
                rid = (row.get("route_id") or "").strip()
                if not rid:
                    continue
                short = (row.get("route_short_name") or "").strip()
                long_ = (row.get("route_long_name") or "").strip()
                routes[rid] = {
                    "route_name": f"{short} {long_}".strip() or rid,
                    "mode": GTFS_ROUTE_TYPES.get((row.get("route_type") or "").strip(), "unknown"),
                }
        if routes:
            write_json(ROUTES_CACHE, routes)
            log(f"route lookup refreshed: {len(routes)} routes")
            return routes
    except Exception as exc:  # noqa: BLE001
        log(f"WARN route lookup failed, continuing with ids only: {exc}")

    return cached or {}


# GTFS route_type codes we expect from the TTC surface feed.
GTFS_ROUTE_TYPES = {"0": "streetcar", "1": "subway", "2": "rail", "3": "bus", "11": "trolleybus"}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def append_snapshot(rows: list[dict]) -> Path:
    """Append rows to this month's partition, writing a header if it is new."""
    now = utcnow()
    path = month_partition(now)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
    log(f"appended {len(rows)} rows -> {path.name}")
    return path


def load_recent_system_rows(months: int = 2) -> list[dict]:
    """Read back the system-wide rows from the most recent monthly partitions."""
    files = sorted(REALTIME_DIR.glob("snapshots-*.csv"))[-months:]
    rows: list[dict] = []
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            rows.extend(r for r in csv.DictReader(fh) if r.get("route_id") == "ALL")
    return rows


# ---------------------------------------------------------------------------
# Fixture mode
# ---------------------------------------------------------------------------
def fixture_inputs():
    """Deterministic synthetic feed data, so the pipeline runs with no network."""
    from make_fixtures import synthetic_feed

    return synthetic_feed()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Collect one TTC realtime snapshot.")
    ap.add_argument("--fixture", action="store_true", help="use synthetic data, no network")
    ap.add_argument("--refresh-routes", action="store_true", help="re-download the static GTFS route list")
    args = ap.parse_args()

    ts = iso(utcnow())

    if args.fixture:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
        vehicles, predictions, alerts, routes = fixture_inputs()
        log(f"FIXTURE MODE: {len(vehicles)} vehicles, {len(predictions)} stop predictions")
    else:
        routes = load_routes(refresh=args.refresh_routes)
        vehicles = decode_vehicles(fetch_feed(GTFSRT_VEHICLES))
        predictions = decode_predictions(fetch_feed(GTFSRT_TRIPS))
        alerts = decode_alerts(fetch_feed(GTFSRT_ALERTS))
        log(f"fetched {len(vehicles)} vehicles, {len(predictions)} stop predictions, {len(alerts)} alerts")

        # Guard against committing an empty snapshot when every feed is down.
        # A missing row is honest; a row of zeros is a lie that would show up as
        # a reliability cliff on the trend chart.
        if not vehicles and not predictions:
            log("ERROR all feeds empty - skipping this snapshot rather than recording zeros")
            return 1

    rows = build_snapshot(ts, vehicles, predictions, alerts, routes,
                          now_epoch=utcnow().timestamp())
    append_snapshot(rows)

    write_json(SITE_DATA / "live.json", build_live_payload(rows))

    system_rows = load_recent_system_rows()
    write_json(
        SITE_DATA / "trend.json",
        {"series": build_trend(system_rows), "hourly": hourly_profile(system_rows)},
    )

    live = build_live_payload(rows)["system"]
    log(f"snapshot ok: {live['vehicles']} vehicles, {live['gaps']} headways measured "
        f"across {live['stops_measured']} stops, {live['pct_regular']}% regular")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
