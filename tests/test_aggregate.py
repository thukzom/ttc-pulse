"""
Unit tests for the aggregation layer.

Run with:  python -m pytest tests/ -v      (or plain `python tests/test_aggregate.py`)

These cover the parts of the pipeline where a silent bug would produce a
plausible-looking but wrong dashboard: percentile maths, the delay-cleaning
filter, the minimum-observation floor on route rankings, and the CSV round trip.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aggregate import (  # noqa: E402
    build_live_payload,
    build_snapshot,
    build_trend,
    classify,
    clean_delays,
    hourly_profile,
    median,
    percentile,
    pct,
)
from make_fixtures import synthetic_feed  # noqa: E402

TS = "2026-08-20T21:00:00Z"


# ---------------------------------------------------------------------------
def test_percentile_basics():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 50) == 2.5          # interpolated
    assert percentile([10], 90) == 10                    # single value
    assert percentile([], 50) is None                    # empty is None, not 0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9.1
    print("ok  percentile")


def test_median_and_pct():
    assert median([5, 1, 3]) == 3                        # unsorted input
    assert pct(1, 4) == 25.0
    assert pct(0, 0) is None                             # no division by zero
    print("ok  median / pct")


# ---------------------------------------------------------------------------
def test_clean_delays_drops_feed_artifacts():
    raw = [30, -120, 48000, None, float("nan"), "junk", -33000, 600]
    assert clean_delays(raw) == [30.0, -120.0, 600.0]
    print("ok  clean_delays removes artifacts, Nones, NaNs and junk")


def test_classify_boundaries():
    assert classify(-61) == "early"
    assert classify(-60) == "on_time"                    # boundary is inclusive
    assert classify(0) == "on_time"
    assert classify(300) == "on_time"
    assert classify(301) == "late"
    print("ok  classify boundaries")


# ---------------------------------------------------------------------------
def test_build_snapshot_shape_and_system_row():
    vehicles = [{"route_id": "501", "vehicle_id": "a"}, {"route_id": "501", "vehicle_id": "b"},
                {"route_id": "29", "vehicle_id": "c"}]
    trips = [{"route_id": "501", "delay_s": 60}, {"route_id": "501", "delay_s": 600},
             {"route_id": "29", "delay_s": 0}]
    alerts = [{"route_ids": ["501"]}]
    routes = {"501": {"route_name": "501 Queen", "mode": "streetcar"}}

    rows = build_snapshot(TS, vehicles, trips, alerts, routes)
    by_id = {r["route_id"]: r for r in rows}

    assert set(by_id) == {"501", "29", "ALL"}
    assert by_id["501"]["route_name"] == "501 Queen"
    assert by_id["29"]["route_name"] == "29"             # falls back to the id
    assert by_id["501"]["n_vehicles"] == 2
    assert by_id["501"]["pct_on_time"] == 50.0           # 60s ok, 600s late
    assert by_id["501"]["n_alerts"] == 1

    # The ALL row must be derived from every reading, not averaged from the
    # per-route averages -- that distinction is where naive pipelines go wrong.
    assert by_id["ALL"]["n_trips"] == 3
    assert by_id["ALL"]["n_vehicles"] == 3
    assert by_id["ALL"]["pct_on_time"] == round(100 * 2 / 3, 1)
    print("ok  build_snapshot per-route rows + system row")


def test_duplicate_vehicles_counted_once():
    vehicles = [{"route_id": "501", "vehicle_id": "a"}] * 3
    rows = build_snapshot(TS, vehicles, [], [], {})
    assert {r["route_id"]: r for r in rows}["501"]["n_vehicles"] == 1
    print("ok  duplicate vehicle ids deduplicated")


def test_empty_feed_does_not_crash():
    rows = build_snapshot(TS, [], [], [], {})
    assert len(rows) == 1 and rows[0]["route_id"] == "ALL"
    assert rows[0]["pct_on_time"] is None                # None, never a fake 0
    print("ok  empty feed yields a single null system row")


# ---------------------------------------------------------------------------
def test_live_payload_applies_observation_floor():
    """A route with 2 observations must not be able to top the 'worst' list."""
    trips = ([{"route_id": "999", "delay_s": 9999}] * 2 +        # tiny sample, terrible
             [{"route_id": "501", "delay_s": 400}] * 10)         # real sample, bad
    rows = build_snapshot(TS, [], trips, [], {})
    payload = build_live_payload(rows)
    worst_ids = [r["route_id"] for r in payload["worst_routes"]]
    assert "999" not in worst_ids
    assert "501" in worst_ids
    print("ok  routes under 5 observations excluded from rankings")


def test_live_payload_system_block():
    vehicles, trips, alerts, routes = synthetic_feed(hour_local=17)
    rows = build_snapshot(TS, vehicles, trips, alerts, routes)
    payload = build_live_payload(rows)
    sys_block = payload["system"]
    assert sys_block["vehicles"] > 100
    assert 0 <= sys_block["pct_on_time"] <= 100
    assert payload["worst_routes"][0]["pct_on_time"] <= payload["best_routes"][0]["pct_on_time"]
    print(f"ok  live payload from fixtures ({sys_block['vehicles']} vehicles, "
          f"{sys_block['pct_on_time']}% on time)")


# ---------------------------------------------------------------------------
def test_trend_handles_csv_string_round_trip():
    """After a CSV round trip every value is a string; the trend must cope."""
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-2{i}T12:00:00Z",
             "pct_on_time": str(60 + i), "median_delay_s": "120", "n_vehicles": "1500"}
            for i in range(5)]
    trend = build_trend(rows)
    assert len(trend) == 5
    assert trend[0]["pct_on_time"] == 60.0               # coerced back to float
    assert isinstance(trend[0]["vehicles"], int)
    print("ok  trend coerces CSV strings back to numbers")


def test_trend_is_sorted_and_capped():
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-20T{h:02d}:00:00Z",
             "pct_on_time": "70", "median_delay_s": "60", "n_vehicles": "10"}
            for h in range(23, -1, -1)]                  # deliberately reversed
    trend = build_trend(rows, keep=5)
    assert len(trend) == 5
    assert trend[0]["ts_utc"] < trend[-1]["ts_utc"]      # sorted ascending
    print("ok  trend sorted ascending and capped")


def test_hourly_profile_has_24_buckets():
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-20T{h:02d}:00:00Z", "pct_on_time": "80"}
            for h in range(24)]
    prof = hourly_profile(rows, tz_offset_hours=-4)
    assert len(prof) == 24
    assert [p["hour"] for p in prof] == list(range(24))
    assert sum(p["n"] for p in prof) == 24
    print("ok  hourly profile covers all 24 local hours")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\nall tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
