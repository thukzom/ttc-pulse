"""
Synthetic feed data, so the whole pipeline can be run and tested with no network.

Two uses:
  1. unit tests import synthetic_feed() for deterministic assertions
  2. `python tests/make_fixtures.py --seed-demo 7` backfills a week of fake
     snapshots so the dashboard can be previewed locally before any real data
     has been collected

*** The seeded demo data is FAKE. Run `make reset` (or delete data/realtime/*)
*** before your first real collection so synthetic rows never mix with real ones.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aggregate import (  # noqa: E402
    SNAPSHOT_COLUMNS,
    build_live_payload,
    build_snapshot,
    build_trend,
    hourly_profile,
)
from common import (  # noqa: E402
    REALTIME_DIR,
    SITE_DATA,
    iso,
    utcnow,
    write_json,
)

# A realistic slice of the TTC surface network: a few streetcar lines, the
# high-frequency bus spines, and a tail of ordinary routes.
STREETCARS = [("501", "501 Queen"), ("504", "504 King"), ("505", "505 Dundas"),
              ("506", "506 Carlton"), ("510", "510 Spadina"), ("512", "512 St Clair")]
BUS_SPINES = [("29", "29 Dufferin"), ("32", "32 Eglinton West"), ("35", "35 Jane"),
              ("36", "36 Finch West"), ("39", "39 Finch East"), ("52", "52 Lawrence West"),
              ("53", "53 Steeles East"), ("85", "85 Sheppard East"), ("96", "96 Wilson"),
              ("102", "102 Markham Rd")]
TAIL = [(str(n), f"{n} Route {n}") for n in range(20, 28)]

ROUTES: dict[str, dict] = {}
for rid, name in STREETCARS:
    ROUTES[rid] = {"route_name": name, "mode": "streetcar"}
for rid, name in BUS_SPINES + TAIL:
    ROUTES[rid] = {"route_name": name, "mode": "bus"}


def _delay_scale(hour_local: int) -> float:
    """
    Delays peak in the PM rush and are mildest overnight.

    Modelled as two gaussians (AM ~08:00, PM ~17:30) over a quiet baseline, which
    is roughly the shape real transit delay data takes.
    """
    am = math.exp(-((hour_local - 8.0) ** 2) / 3.0)
    pm = math.exp(-((hour_local - 17.5) ** 2) / 4.0)
    return 0.45 + 1.5 * pm + 0.9 * am


def synthetic_feed(hour_local: int = 17, seed: int = 42):
    """
    Build one moment of plausible feed data.

    Returns (vehicles, trip_updates, alerts, routes) shaped exactly like the
    decoded protobuf output, so tests exercise the real aggregation path.
    """
    rng = random.Random(seed + hour_local)
    scale = _delay_scale(hour_local)

    vehicles, trips = [], []
    for rid, meta in ROUTES.items():
        # Streetcars and spine buses run more vehicles; the tail runs few.
        base = 14 if meta["mode"] == "streetcar" else (11 if rid in dict(BUS_SPINES) else 4)
        n_veh = max(1, int(rng.gauss(base, 2)))
        for i in range(n_veh):
            vehicles.append({"route_id": rid, "vehicle_id": f"{rid}-{i}",
                             "lat": 43.65 + rng.random() * 0.1, "lon": -79.4 + rng.random() * 0.1})

        # Streetcars in mixed traffic bunch worse than buses on suburban roads.
        penalty = 1.5 if meta["mode"] == "streetcar" else 1.0
        for _ in range(n_veh):
            delay = rng.gauss(70 * scale * penalty, 150 * scale)
            trips.append({"route_id": rid, "delay_s": round(delay)})

    # Feed artifacts: stale trip assignments producing absurd delays. The
    # cleaning step in aggregate.clean_delays() exists precisely for these, so
    # the fixtures include them on purpose.
    for _ in range(6):
        trips.append({"route_id": rng.choice(list(ROUTES)), "delay_s": rng.choice([48000, -33000, 99999])})

    alerts = [{"route_ids": [rid], "header": "Detour in effect"}
              for rid in rng.sample(list(ROUTES), 3)]

    return vehicles, trips, alerts, ROUTES


def seed_demo_history(days: int = 7, every_minutes: int = 10) -> None:
    """Backfill fake snapshots so the dashboard has something to draw locally."""
    now = utcnow()
    steps = int(days * 24 * 60 / every_minutes)
    rows_by_month: dict[str, list[dict]] = {}
    all_system_rows: list[dict] = []

    print(f"seeding {steps} synthetic snapshots over {days} days ...")
    for i in range(steps, 0, -1):
        ts_dt = now - timedelta(minutes=every_minutes * i)
        local_hour = (ts_dt.hour - 4) % 24  # crude EDT
        vehicles, trips, alerts, routes = synthetic_feed(local_hour, seed=i)
        rows = build_snapshot(iso(ts_dt), vehicles, trips, alerts, routes)
        rows_by_month.setdefault(f"{ts_dt:%Y-%m}", []).extend(rows)
        all_system_rows.extend(r for r in rows if r["route_id"] == "ALL")
        last_rows = rows

    for month, rows in rows_by_month.items():
        path = REALTIME_DIR / f"snapshots-{month}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"  wrote {len(rows):,} rows -> {path.name}")

    write_json(SITE_DATA / "live.json", build_live_payload(last_rows))
    write_json(SITE_DATA / "trend.json", {
        "series": build_trend(all_system_rows),
        "hourly": hourly_profile(all_system_rows),
    })
    print("wrote docs/data/live.json and docs/data/trend.json")
    print("\n*** THIS DATA IS SYNTHETIC. Run `make reset` before collecting real data. ***")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-demo", type=int, metavar="DAYS", help="backfill N days of fake snapshots")
    args = ap.parse_args()
    if args.seed_demo:
        seed_demo_history(args.seed_demo)
    else:
        v, t, a, r = synthetic_feed()
        print(f"{len(v)} vehicles, {len(t)} trip updates, {len(a)} alerts, {len(r)} routes")
