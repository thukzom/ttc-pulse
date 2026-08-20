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
# On-time window. A trip counts as on time if it is running between 1 minute
# early and 5 minutes late. This mirrors the convention most North American
# transit agencies publish against, so the number is comparable to TTC's own
# service standards rather than being an arbitrary cut of our own.
ON_TIME_EARLY_S = -60
ON_TIME_LATE_S = 300

# A GTFS-RT delay value beyond this is treated as a feed artifact and dropped.
# Real surface-transit delays do not legitimately exceed ~2 hours; values in the
# tens of thousands of seconds appear when a vehicle's trip assignment is stale.
MAX_PLAUSIBLE_DELAY_S = 7200

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
