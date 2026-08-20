"""
Shared paths, constants and small helpers for TTC Pulse.

Everything that more than one script needs lives here so there is exactly one
place to change a URL, a threshold, or a directory name.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# ROOT is the repository root: this file is <root>/src/common.py
ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
REALTIME_DIR = DATA / "realtime"       # monthly CSV partitions of collected snapshots
PROCESSED_DIR = DATA / "processed"     # cleaned historical archives (parquet)
SITE_DATA = ROOT / "docs" / "data"     # JSON the dashboard fetches (docs/ is what GitHub Pages serves)

for _d in (REALTIME_DIR, PROCESSED_DIR, SITE_DATA):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
# TTC GTFS-Realtime. Covers the SURFACE network (bus + streetcar). The subway
# does not publish a realtime vehicle feed, which is why subway analysis in this
# project comes from the historical delay archive instead. No API key required.
GTFSRT_VEHICLES = "https://bustime.ttc.ca/gtfsrt/vehicles"
GTFSRT_TRIPS = "https://bustime.ttc.ca/gtfsrt/trips"
GTFSRT_ALERTS = "https://bustime.ttc.ca/gtfsrt/alerts"

# City of Toronto Open Data (CKAN). Used for the static GTFS bundle (route
# names) and for the monthly delay archives that go back to 2014.
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
GTFS_STATIC_PACKAGE = "ttc-routes-and-schedules"
DELAY_PACKAGES = {
    "subway": "ttc-subway-delay-data",
    "bus": "ttc-bus-delay-data",
    "streetcar": "ttc-streetcar-delay-data",
}

# ---------------------------------------------------------------------------
# Analysis constants
# ---------------------------------------------------------------------------
# --- Headway regularity ---------------------------------------------------
# The TTC feed publishes predicted arrival times for trips marked
# `schedule_relationship: NEW`, i.e. trips with no counterpart in the published
# timetable. There is therefore no scheduled time to subtract, and schedule
# adherence ("on-time performance") cannot be computed from it. See the module
# docstring in aggregate.py for the evidence. What the feed does support is
# headway regularity, which is the metric that matters on frequent service
# anyway: riders who turn up without checking a timetable care about even
# spacing, not punctuality against a schedule they never read.

# How far forward to trust predictions. Beyond an hour the agency's estimate is
# mostly its own model extrapolating, not a measurement of what is on the road.
PREDICTION_WINDOW_S = 3600

# Plausible band for a headway between consecutive vehicles at one stop.
# Under 30s is almost always the same vehicle reported twice rather than two
# genuine arrivals; over an hour means the next vehicle is too far out to say
# anything about current service.
HEADWAY_MIN_S = 30
HEADWAY_MAX_S = 3600

# A stop needs at least this many gaps before its regularity is reported.
# Below it, one early vehicle swings the percentage to a meaningless extreme.
MIN_GAPS_PER_STOP = 3

# A gap counts as regular when it falls within these multiples of the local
# average headway at that stop. Below the floor is bunching (vehicles arriving
# together); above the ceiling is a gap in service. The comparison is relative
# because a 4-minute gap is normal on King and a sign of bunching on a route
# that runs every 20 minutes.
REGULAR_LOW = 0.5
REGULAR_HIGH = 1.5

# Toronto is UTC-5 (EST) / UTC-4 (EDT). Timestamps are stored in UTC and
# converted for display; see to_toronto().
TORONTO_TZ = "America/Toronto"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    """Current UTC time, timezone-aware. Never use naive datetimes."""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialize a datetime to a stable ISO-8601 string with a Z suffix."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_toronto(dt: datetime):
    """Convert an aware datetime to Toronto local time (handles DST)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(TORONTO_TZ))
    except Exception:
        return dt


def month_partition(dt: datetime) -> Path:
    """
    Snapshots are appended to one CSV per calendar month.

    Partitioning matters here: a single ever-growing CSV would be rewritten on
    every commit, so the git history would balloon. One file per month keeps
    each commit's diff to a handful of appended lines.
    """
    return REALTIME_DIR / f"snapshots-{dt:%Y-%m}.csv"


def write_json(path: Path, payload) -> None:
    """
    Write JSON deterministically.

    sort_keys and a fixed separator matter for a git-committed pipeline: without
    them, dict ordering noise produces diffs even when nothing changed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def read_json(path: Path, default=None):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def log(msg: str) -> None:
    """Timestamped stdout logging, which becomes the GitHub Actions run log."""
    print(f"[{iso(utcnow())}] {msg}", flush=True)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}
