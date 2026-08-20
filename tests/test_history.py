"""
Tests for the historical layer, plus a synthetic-archive generator.

The real archives are only reachable with network access, so these tests build
a raw frame that reproduces the quirks the real files actually contain -- mixed
date formats, a stray unmapped column, blank codes, negative delays, times that
will not parse -- and assert the cleaning survives them.

    python tests/test_history.py                # run the tests
    python tests/test_history.py --write-demo   # also write a demo history.json
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd  # noqa: E402

from build_history import normalise, summarise  # noqa: E402
from common import SITE_DATA, write_json  # noqa: E402

SUBWAY_CAUSES = {
    "SUDP": "Disorderly Patron", "MUPAA": "Passenger Assistance Alarm Activated",
    "SUUT": "Unauthorized at Track Level", "PUSTC": "Signals Track Circuit Problem",
    "MUIS": "Injured or Ill Customer (In Station) - Transported",
    "EUDO": "Door Problems - Faulty Equipment", "MUATC": "ATC Project",
    "TUS": "Track Switch Failure", "PUOPO": "Operations - Operator",
    "SUAP": "Assault / Patron Involved", "MUNCA": "No Crew Available",
    "EUCD": "Compressor Defective", "MUO": "Miscellaneous Other",
}
SURFACE_CAUSES = {
    "Mechanical": "Mechanical", "Operations": "Operations - Operator",
    "Collision - TTC": "Collision - TTC", "Diversion": "Diversion",
    "Emergency Services": "Emergency Services", "General Delay": "General Delay",
    "Security": "Security", "Held By": "Held By", "Investigation": "Investigation",
    "Road Blocked": "Road Blocked - NON-TTC Collision", "Utilized Off Route": "Utilized Off Route",
    "Cleaning": "Cleaning - Unsanitary", "Late Leaving Garage": "Late Leaving Garage",
}
SUBWAY_LINES = ["YU", "BD", "SHP", "SRT"]
BUS_ROUTES = ["29", "32", "35", "36", "39", "52", "53", "85", "96", "102", "165", "196"]
STREETCAR_ROUTES = ["501", "504", "505", "506", "510", "512"]


def synth_raw(mode: str, n: int, seed: int = 7) -> pd.DataFrame:
    """
    Build a raw frame shaped like the real archive files, quirks included.

    Deliberate quirks reproduced here:
      * two different date string formats in the same column
      * an unmapped extra column that normalise() must drop and log
      * blank / NaN delay codes
      * negative delay values (real data-entry errors in the archives)
      * unparseable time strings
    """
    rng = random.Random(seed)
    causes = list(SUBWAY_CAUSES) if mode == "subway" else list(SURFACE_CAUSES)
    lines = SUBWAY_LINES if mode == "subway" else (BUS_ROUTES if mode == "bus" else STREETCAR_ROUTES)
    # Real incident data is heavily skewed: a few causes and a few routes
    # account for most of the lost time. Uniform sampling would produce a flat,
    # obviously-fake chart, so both are drawn from a decaying weight.
    cause_w = [1 / (r + 1.2) ** 1.4 for r in range(len(causes))]
    line_w = [1 / (r + 1.5) ** 1.1 for r in range(len(lines))]

    rows = []
    for i in range(n):
        year = rng.choices(range(2014, 2027), weights=[0.8, 0.85, 0.9, 0.95, 1.0, 1.05,
                                                       0.6, 0.7, 0.95, 1.05, 1.1, 1.15, 0.7])[0]
        # Do not invent records for months that have not happened yet.
        max_month = 8 if year == 2026 else 12
        month, day = rng.randint(1, max_month), rng.randint(1, 28)
        hour = rng.choices(range(24), weights=[1, 1, 1, 1, 2, 5, 9, 14, 16, 11, 8, 8,
                                               9, 9, 10, 13, 17, 19, 15, 10, 7, 5, 3, 2])[0]
        # Two date formats, alternating, exactly like the real archives.
        date = (f"{year}-{month:02d}-{day:02d}" if i % 2
                else f"{day:02d}/{month:02d}/{year}")
        # Rush-hour incidents run longer.
        base = 9 if hour in (7, 8, 16, 17, 18) else 5
        delay = max(0, round(rng.lognormvariate(0.9, 0.9) * base / 5))
        rows.append({
            "Date" if mode == "subway" else "Report Date": date,
            "Time": f"{hour:02d}:{rng.randint(0,59):02d}" if i % 37 else "n/a",
            "Day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][rng.randint(0, 6)],
            "Station" if mode == "subway" else "Location": f"Stop {rng.randint(1, 200)}",
            "Code" if mode == "subway" else "Incident": rng.choices(causes, weights=cause_w)[0] if i % 23 else "",
            "Min Delay": -3 if i % 500 == 0 else delay,
            "Min Gap": delay + rng.randint(0, 6),
            "Bound" if mode == "subway" else "Direction": rng.choice(["N", "S", "E", "W"]),
            "Line" if mode == "subway" else "Route": rng.choices(lines, weights=line_w)[0],
            "Vehicle": rng.randint(1000, 9999),
            "Sheet Ref": f"ignore-me-{i}",     # unmapped column: must be dropped
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
def test_normalise_maps_both_schemas():
    sub = normalise(synth_raw("subway", 500), "subway", "subway-test")
    bus = normalise(synth_raw("bus", 500), "bus", "bus-test")
    for df in (sub, bus):
        assert {"date", "time", "code", "delay_min", "line", "mode", "hour"} <= set(df.columns)
        assert "Sheet Ref" not in df.columns          # unmapped column dropped
        assert df["date"].notna().all()               # both date formats parsed
    assert sub["mode"].unique().tolist() == ["subway"]
    print("ok  normalise handles subway and surface schemas")


def test_negative_delays_nulled_not_dropped():
    raw = synth_raw("bus", 2000)
    n_negative = int((raw["Min Delay"] < 0).sum())
    assert n_negative > 0, "fixture should contain negative delays"
    df = normalise(raw, "bus", "t")
    assert len(df) == len(raw)                        # rows kept
    assert (df["delay_min"].dropna() >= 0).all()      # values nulled
    print(f"ok  {n_negative} negative delays nulled, rows retained as incidents")


def test_unparseable_times_excluded_not_zeroed():
    df = normalise(synth_raw("subway", 1000), "subway", "t")
    assert df["hour"].isna().sum() > 0                # the "n/a" times
    assert df["hour"].dropna().between(0, 23).all()
    # The critical assertion: bad times must NOT silently become hour 0.
    hour_counts = df["hour"].value_counts()
    assert hour_counts.get(0, 0) < hour_counts.max() * 0.5
    print("ok  unparseable times become NaN, not a fake midnight spike")


def test_summarise_shape():
    frames = [normalise(synth_raw(m, 3000, seed=i), m, m)
              for i, m in enumerate(["subway", "bus", "streetcar"])]
    summary = summarise(pd.concat(frames, ignore_index=True), SUBWAY_CAUSES)

    assert set(summary) == {"meta", "by_year", "by_month", "top_causes",
                            "by_hour", "by_dow", "worst_lines"}
    assert summary["meta"]["rows"] == 9000
    assert sorted(summary["meta"]["modes"]) == ["bus", "streetcar", "subway"]
    assert len(summary["by_dow"]) == 7
    assert len(summary["top_causes"]) <= 12
    assert all(0 <= r["hour"] <= 23 for r in summary["by_hour"])
    # Codes must have been translated to readable descriptions.
    assert any(" " in str(r["cause"]) for r in summary["top_causes"])
    print(f"ok  summarise produced {len(summary['by_month'])} months, "
          f"{summary['meta']['total_delay_hours']:,} delay-hours")


def test_json_is_serialisable():
    import json
    frames = [normalise(synth_raw(m, 800, seed=i), m, m) for i, m in enumerate(["subway", "bus"])]
    summary = summarise(pd.concat(frames, ignore_index=True), SUBWAY_CAUSES)
    json.dumps(summary)                               # numpy types would raise here
    print("ok  summary is JSON-serialisable (no numpy scalars leak through)")


# ---------------------------------------------------------------------------
def write_demo(scale: int = 45000) -> None:
    """Write a demo history.json so the dashboard renders before a real run."""
    frames = []
    for i, (mode, n) in enumerate([("subway", scale), ("bus", int(scale * 2.2)),
                                   ("streetcar", int(scale * 0.5))]):
        frames.append(normalise(synth_raw(mode, n, seed=i * 11), mode, mode))
    full = pd.concat(frames, ignore_index=True)
    summary = summarise(full, SUBWAY_CAUSES)
    summary["meta"]["synthetic"] = True
    write_json(SITE_DATA / "history.json", summary)
    print(f"\nwrote demo history.json: {summary['meta']['rows']:,} synthetic rows")
    print("*** SYNTHETIC. Run `python src/build_history.py` for the real archive. ***")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-demo", action="store_true")
    args = ap.parse_args()

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\nall tests passed" if not failures else f"\n{failures} test(s) failed")

    if args.write_demo and not failures:
        write_demo()
    raise SystemExit(1 if failures else 0)
