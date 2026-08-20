"""
Unit tests for the aggregation layer.

Run with:  python tests/test_aggregate.py

These cover the places where a silent bug would produce a plausible-looking but
wrong dashboard: the headway maths, the plausibility filters, the minimum-sample
floors, and the CSV round trip.
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
    classify_gap,
    gaps_from_times,
    hourly_profile,
    mean,
    median,
    pct,
    percentile,
    stdev,
    stop_headway_stats,
)
from common import utcnow  # noqa: E402
from make_fixtures import synthetic_feed  # noqa: E402

TS = "2026-08-20T21:00:00Z"
NOW = 1787000000.0


# ---------------------------------------------------------------------------
def test_percentile_basics():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 50) == 2.5          # interpolated
    assert percentile([10], 90) == 10
    assert percentile([], 50) is None                    # empty is None, not 0
    print("ok  percentile")


def test_mean_median_stdev_pct():
    assert median([5, 1, 3]) == 3                        # unsorted input
    assert mean([2, 4]) == 3
    assert stdev([5]) is None                            # needs two values
    assert round(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 3) == 2.0
    assert pct(1, 4) == 25.0
    assert pct(0, 0) is None                             # no division by zero
    print("ok  mean / median / stdev / pct")


# ---------------------------------------------------------------------------
def test_gaps_from_times_filters_and_sorts():
    # Unsorted, with a duplicate, a sub-30s gap, and a gap over an hour.
    times = [1000, 400, 1000, 405, 1600, 9999]
    gaps = gaps_from_times(times)
    # 400->405 is 5s (dropped), 405->1000 is 595s (kept), 1000->1600 is 600s
    # (kept), 1600->9999 is 8399s (dropped for exceeding the hour ceiling).
    assert gaps == [595.0, 600.0]
    print("ok  gaps_from_times sorts, dedupes, and filters implausible gaps")


def test_classify_gap_is_relative_to_local_headway():
    # 4 minutes is regular where the average is 5, bunched where it is 20.
    assert classify_gap(240, 300) == "regular"
    assert classify_gap(240, 1200) == "bunched"
    assert classify_gap(2000, 1200) == "gapped"
    # Boundaries: exactly 0.5x and 1.5x are regular.
    assert classify_gap(150, 300) == "regular"
    assert classify_gap(450, 300) == "regular"
    assert classify_gap(149, 300) == "bunched"
    assert classify_gap(451, 300) == "gapped"
    print("ok  classify_gap is relative to each stop's own rhythm")


def test_stop_needs_enough_gaps():
    # Three arrivals = two gaps, below the floor of three.
    assert stop_headway_stats([0, 300, 600]) is None
    # Five arrivals = four gaps, enough.
    stats = stop_headway_stats([0, 300, 600, 900, 1200])
    assert stats is not None and stats["regular"] == 4 and stats["cv"] == 0.0
    print("ok  stops below the minimum gap count are excluded")


def test_perfectly_even_service_scores_100():
    stats = stop_headway_stats([0, 300, 600, 900, 1200, 1500])
    assert stats["cv"] == 0.0                            # no variation at all
    assert stats["bunched"] == 0 and stats["gapped"] == 0
    print("ok  perfectly even spacing gives cv 0 and no bunching")


def test_bunched_service_is_detected():
    # Three vehicles arrive together, then a long gap: classic bunching.
    stats = stop_headway_stats([0, 40, 80, 120, 1500])
    assert stats["bunched"] >= 3
    assert stats["gapped"] >= 1
    assert stats["cv"] > 1.0
    print(f"ok  bunching detected (cv {stats['cv']:.2f})")


# ---------------------------------------------------------------------------
def _preds(route, stop, times, prefix="t"):
    return [{"route_id": route, "stop_id": stop, "trip_key": f"{prefix}{i}", "time": NOW + t}
            for i, t in enumerate(times)]


def test_build_snapshot_shape_and_system_row():
    vehicles = [{"route_id": "501", "vehicle_id": "a"}, {"route_id": "501", "vehicle_id": "b"},
                {"route_id": "29", "vehicle_id": "c"}]
    preds = (_preds("501", "s1", [60, 360, 660, 960, 1260]) +
             _preds("29", "s2", [60, 100, 140, 180, 1500], prefix="u"))
    alerts = [{"route_ids": ["501"]}]
    routes = {"501": {"route_name": "501 Queen", "mode": "streetcar"}}

    rows = build_snapshot(TS, vehicles, preds, alerts, routes, now_epoch=NOW)
    by_id = {r["route_id"]: r for r in rows}

    assert set(by_id) == {"501", "29", "ALL"}
    assert by_id["501"]["route_name"] == "501 Queen"
    assert by_id["29"]["route_name"] == "29"             # falls back to the id
    assert by_id["501"]["n_vehicles"] == 2
    assert by_id["501"]["pct_regular"] == 100.0          # evenly spaced
    assert by_id["29"]["pct_bunched"] > 0                # deliberately bunched
    assert by_id["501"]["n_alerts"] == 1

    # The ALL row must be derived from every gap, not averaged from per-route
    # averages -- that distinction is where naive pipelines go wrong.
    assert by_id["ALL"]["n_gaps"] == by_id["501"]["n_gaps"] + by_id["29"]["n_gaps"]
    assert by_id["ALL"]["n_vehicles"] == 3
    print("ok  build_snapshot per-route rows + system row")


def test_predictions_outside_the_window_are_ignored():
    # Five arrivals two hours out: beyond the one-hour prediction window.
    preds = _preds("99", "s1", [7200, 7500, 7800, 8100, 8400])
    rows = build_snapshot(TS, [], preds, [], {}, now_epoch=NOW)
    by_id = {r["route_id"]: r for r in rows}
    assert by_id["ALL"]["n_gaps"] == 0
    assert by_id["ALL"]["pct_regular"] is None           # None, never a fake 0
    print("ok  far-future predictions excluded from the window")


def test_same_trip_listed_twice_cannot_fake_a_headway():
    # One trip reported twice at the same stop must not become a 0-second gap.
    preds = [{"route_id": "5", "stop_id": "s", "trip_key": "SAME", "time": NOW + 100},
             {"route_id": "5", "stop_id": "s", "trip_key": "SAME", "time": NOW + 105}]
    preds += _preds("5", "s", [400, 700, 1000, 1300], prefix="other")
    rows = build_snapshot(TS, [], preds, [], {}, now_epoch=NOW)
    by_id = {r["route_id"]: r for r in rows}
    assert by_id["5"]["pct_regular"] == 100.0            # the duplicate collapsed
    print("ok  duplicate trip/stop entries deduplicated")


def test_duplicate_vehicles_counted_once():
    rows = build_snapshot(TS, [{"route_id": "501", "vehicle_id": "a"}] * 3, [], [], {}, now_epoch=NOW)
    assert {r["route_id"]: r for r in rows}["501"]["n_vehicles"] == 1
    print("ok  duplicate vehicle ids deduplicated")


def test_empty_feed_does_not_crash():
    rows = build_snapshot(TS, [], [], [], {}, now_epoch=NOW)
    assert len(rows) == 1 and rows[0]["route_id"] == "ALL"
    assert rows[0]["pct_regular"] is None
    print("ok  empty feed yields a single null system row")


# ---------------------------------------------------------------------------
def test_live_payload_applies_sample_floor():
    """A route with a handful of gaps must not be able to top the worst list."""
    tiny = _preds("999", "s", [60, 100, 140, 180], prefix="x")        # ~3 gaps, awful
    big = []
    for st in range(6):
        big += _preds("501", f"s{st}", [60, 360, 660, 960, 1260], prefix=f"b{st}")
    rows = build_snapshot(TS, [], tiny + big, [], {}, now_epoch=NOW)
    payload = build_live_payload(rows)
    assert "999" not in [r["route_id"] for r in payload["worst_routes"]]
    print("ok  routes below the gap floor excluded from rankings")


def test_live_payload_from_fixtures():
    now = utcnow().timestamp()
    v, p, a, r = synthetic_feed(hour_local=17, now_epoch=now)
    rows = build_snapshot(TS, v, p, a, r, now_epoch=now)
    s = build_live_payload(rows)["system"]
    assert s["vehicles"] > 100
    assert s["gaps"] > 100
    assert 0 <= s["pct_regular"] <= 100
    assert s["median_headway_s"] > 0
    print(f"ok  live payload from fixtures ({s['vehicles']} vehicles, "
          f"{s['gaps']} gaps, {s['pct_regular']}% regular)")


def test_peak_is_less_regular_than_overnight():
    """The metric has to actually discriminate, or it is measuring nothing."""
    now = utcnow().timestamp()
    scores = {}
    for hr in (3, 17):
        v, p, a, r = synthetic_feed(hour_local=hr, now_epoch=now)
        rows = build_snapshot(TS, v, p, a, r, now_epoch=now)
        scores[hr] = build_live_payload(rows)["system"]["pct_regular"]
    assert scores[3] > scores[17] + 10
    print(f"ok  metric discriminates (3am {scores[3]}% vs 5pm {scores[17]}% regular)")


# ---------------------------------------------------------------------------
def test_trend_handles_csv_string_round_trip():
    """After a CSV round trip every value is a string; the trend must cope."""
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-2{i}T12:00:00Z",
             "pct_regular": str(60 + i), "pct_bunched": "10",
             "median_headway_s": "420", "n_vehicles": "1500"} for i in range(5)]
    trend = build_trend(rows)
    assert len(trend) == 5
    assert trend[0]["pct_regular"] == 60.0               # coerced back to float
    assert isinstance(trend[0]["vehicles"], int)
    print("ok  trend coerces CSV strings back to numbers")


def test_trend_is_sorted_and_capped():
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-20T{h:02d}:00:00Z",
             "pct_regular": "70", "median_headway_s": "420", "n_vehicles": "10"}
            for h in range(23, -1, -1)]                  # deliberately reversed
    trend = build_trend(rows, keep=5)
    assert len(trend) == 5
    assert trend[0]["ts_utc"] < trend[-1]["ts_utc"]      # sorted ascending
    print("ok  trend sorted ascending and capped")


def test_hourly_profile_has_24_buckets():
    rows = [{"route_id": "ALL", "ts_utc": f"2026-08-20T{h:02d}:00:00Z", "pct_regular": "80"}
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
