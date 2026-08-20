"""
Build the historical layer from City of Toronto Open Data.

The TTC publishes a delay record for every service disruption on the subway,
bus and streetcar networks going back to 2014 -- roughly a million rows across
the three modes, spread over dozens of Excel and CSV files whose column names
change from year to year. This script downloads them, forces them into one
schema, joins the subway cause codes to their descriptions, and writes the
aggregates the dashboard needs.

Run monthly (the archives update monthly), not every 10 minutes:
    python src/build_history.py
    python src/build_history.py --modes subway          # just one mode
    python src/build_history.py --since 2019            # skip older files

Why this exists alongside the realtime collector: the live feed shows what is
happening now but has no memory, and the subway has no realtime vehicle feed at
all. The archive has ten years of memory but is a month stale. Together they
answer both "how is it right now" and "is it getting worse".
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from common import (  # noqa: E402
    CKAN,
    DELAY_PACKAGES,
    PROCESSED_DIR,
    SITE_DATA,
    log,
    write_json,
)

USER_AGENT = "ttc-pulse/1.0 (open-data portfolio project; contact via GitHub issues)"

# ---------------------------------------------------------------------------
# Column normalisation
# ---------------------------------------------------------------------------
# The same field is spelled differently across years and modes. Rather than
# special-casing each file, every header is lowercased and stripped, then looked
# up here. Anything unmapped is dropped -- loudly, so a new spelling surfaces in
# the log instead of silently becoming a column of NaNs.
COLUMN_MAP = {
    "date": "date", "report date": "date",
    "time": "time",
    "day": "day",
    "station": "location", "location": "location",
    "code": "code", "incident": "code", "delay code": "code",
    "min delay": "delay_min", "delay": "delay_min", "min_delay": "delay_min",
    "min gap": "gap_min", "gap": "gap_min", "min_gap": "gap_min",
    "bound": "direction", "direction": "direction",
    "line": "line", "route": "line",
    "vehicle": "vehicle",
}

# Codes with these meanings are administrative, not service events.
NON_INCIDENT_CODES = {"", "nan", "none"}


def http_get(url: str, timeout: int = 240) -> bytes:
    import requests

    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.content


def ckan_resources(package: str) -> list[dict]:
    import requests

    r = requests.get(f"{CKAN}/package_show", params={"id": package},
                     timeout=60, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()["result"]["resources"]


def normalise(df: pd.DataFrame, mode: str, source: str) -> pd.DataFrame:
    """Force one raw file into the canonical schema."""
    renamed, dropped = {}, []
    for col in df.columns:
        key = re.sub(r"\s+", " ", str(col)).strip().lower()
        if key in COLUMN_MAP:
            renamed[col] = COLUMN_MAP[key]
        else:
            dropped.append(str(col))
    if dropped:
        log(f"    note: ignoring unmapped columns in {source}: {dropped}")

    df = df.rename(columns=renamed)
    df = df[[c for c in df.columns if c in set(COLUMN_MAP.values())]].copy()

    for needed in ("date", "time", "code", "delay_min", "line", "location", "day"):
        if needed not in df.columns:
            df[needed] = pd.NA

    # Dates arrive as real datetimes in some files and dd/mm/yyyy strings in
    # others; format="mixed" lets pandas resolve per value instead of guessing
    # one format for the column and mangling the rest.
    df["date"] = pd.to_datetime(df["date"], errors="coerce", format="mixed")

    # Time is sometimes "07:35", sometimes a full timestamp, sometimes an Excel
    # fraction of a day. Parse the first two; the third becomes NaT and is
    # excluded from the hourly profile rather than silently landing at midnight.
    parsed_time = pd.to_datetime(df["time"].astype(str), errors="coerce", format="mixed")
    df["hour"] = parsed_time.dt.hour

    df["delay_min"] = pd.to_numeric(df["delay_min"], errors="coerce")
    df["mode"] = mode
    df["code"] = df["code"].astype(str).str.strip()
    df["line"] = df["line"].astype(str).str.strip().str.upper()

    before = len(df)
    df = df[df["date"].notna()]
    # A "delay" of zero is a logged event with no service impact; negatives are
    # data entry errors. Both are excluded from delay statistics but the row is
    # kept, because it still counts as an incident.
    df.loc[df["delay_min"] < 0, "delay_min"] = pd.NA
    if before - len(df):
        log(f"    dropped {before - len(df):,} rows with unparseable dates")
    return df


def load_mode(mode: str, package: str, since_year: int | None) -> pd.DataFrame:
    """Download and normalise every archive file for one mode."""
    log(f"  {mode}: listing resources ...")
    frames = []
    for res in ckan_resources(package):
        fmt = (res.get("format") or "").upper()
        name = res.get("name") or ""
        if fmt not in {"XLSX", "CSV"}:
            continue
        # Skip the readme and the code-description lookups; those are handled
        # separately by load_code_descriptions().
        if re.search(r"readme|code.?descriptions|delay.?codes", name, re.I):
            continue
        if since_year:
            years = [int(y) for y in re.findall(r"(20\d{2})", name)]
            if years and max(years) < since_year:
                continue
        try:
            blob = http_get(res["url"])
            buf = io.BytesIO(blob)
            raw = pd.read_excel(buf) if fmt == "XLSX" else pd.read_csv(buf, encoding_errors="replace")
            log(f"    {name}: {len(raw):,} rows")
            frames.append(normalise(raw, mode, name))
        except Exception as exc:  # noqa: BLE001
            log(f"    WARN skipped {name}: {exc}")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_code_descriptions() -> dict[str, str]:
    """Subway delay codes are opaque ('SUDP'); this maps them to plain English."""
    lookup: dict[str, str] = {}
    try:
        for res in ckan_resources(DELAY_PACKAGES["subway"]):
            name = (res.get("name") or "").lower()
            if "code" not in name or (res.get("format") or "").upper() not in {"XLSX", "CSV"}:
                continue
            blob = http_get(res["url"])
            buf = io.BytesIO(blob)
            df = pd.read_excel(buf) if name.endswith("xlsx") or (res.get("format") == "XLSX") \
                else pd.read_csv(buf, encoding_errors="replace")
            cols = [str(c).strip().lower() for c in df.columns]
            df.columns = cols
            code_col = next((c for c in cols if "code" in c), None)
            desc_col = next((c for c in cols if "desc" in c), None)
            if code_col and desc_col:
                for c, d in zip(df[code_col], df[desc_col]):
                    if pd.notna(c) and pd.notna(d):
                        lookup[str(c).strip()] = str(d).strip()
        log(f"  loaded {len(lookup)} delay code descriptions")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARN could not load code descriptions: {exc}")
    return lookup


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def summarise(df: pd.DataFrame, codes: dict[str, str]) -> dict:
    """Turn ~1M incident rows into the handful of numbers the dashboard shows."""
    df = df.copy()
    df["year"] = df["date"].dt.year
    df["dow"] = df["date"].dt.dayofweek           # 0 = Monday
    df["month"] = df["date"].dt.to_period("M").astype(str)
    df["cause"] = df["code"].map(lambda c: codes.get(c, c) if c not in NON_INCIDENT_CODES else "Unspecified")

    delayed = df[df["delay_min"] > 0]

    by_year = (
        df.groupby(["year", "mode"], dropna=True)
          .agg(incidents=("code", "size"), delay_minutes=("delay_min", "sum"))
          .reset_index()
    )
    by_year = by_year[by_year["year"].between(2014, 2100)]

    by_month = (
        delayed.groupby("month")
               .agg(incidents=("code", "size"),
                    delay_minutes=("delay_min", "sum"),
                    median_delay=("delay_min", "median"))
               .reset_index().sort_values("month")
    )

    top_causes = (
        delayed.groupby("cause")
               .agg(incidents=("cause", "size"), delay_minutes=("delay_min", "sum"))
               .reset_index()
               .sort_values("delay_minutes", ascending=False)
               .head(12)
    )

    by_hour = (
        delayed[delayed["hour"].notna()]
        .groupby(["hour", "mode"])
        .agg(incidents=("cause", "size"), median_delay=("delay_min", "median"))
        .reset_index()
    )
    by_hour["hour"] = by_hour["hour"].astype(int)

    by_dow = (
        delayed.groupby("dow")
               .agg(incidents=("cause", "size"), delay_minutes=("delay_min", "sum"))
               .reset_index()
    )

    worst_lines = (
        delayed[delayed["line"].notna() & (delayed["line"] != "NAN") & (delayed["line"] != "")]
        .groupby(["line", "mode"])
        .agg(incidents=("cause", "size"), delay_minutes=("delay_min", "sum"),
             median_delay=("delay_min", "median"))
        .reset_index()
        .sort_values("delay_minutes", ascending=False)
        .head(15)
    )

    span = (df["date"].min(), df["date"].max())
    return {
        "meta": {
            "rows": int(len(df)),
            "rows_with_delay": int(len(delayed)),
            "first_date": None if pd.isna(span[0]) else span[0].strftime("%Y-%m-%d"),
            "last_date": None if pd.isna(span[1]) else span[1].strftime("%Y-%m-%d"),
            "modes": sorted(df["mode"].dropna().unique().tolist()),
            "total_delay_hours": round(float(delayed["delay_min"].sum()) / 60, 1),
        },
        "by_year": _records(by_year),
        "by_month": _records(by_month),
        "top_causes": _records(top_causes),
        "by_hour": _records(by_hour),
        "by_dow": _records(by_dow),
        "worst_lines": _records(worst_lines),
    }


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records, rounding floats and nulling NaN."""
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if pd.isna(v):
                clean[k] = None
            elif isinstance(v, float):
                clean[k] = round(v, 2)
            elif hasattr(v, "item"):
                clean[k] = v.item()
            else:
                clean[k] = v
        out.append(clean)
    return out


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Build the TTC historical delay layer.")
    ap.add_argument("--modes", nargs="*", default=list(DELAY_PACKAGES),
                    choices=list(DELAY_PACKAGES), help="which networks to load")
    ap.add_argument("--since", type=int, default=None, help="skip archive files older than this year")
    args = ap.parse_args()

    log(f"building historical layer for: {', '.join(args.modes)}")
    codes = load_code_descriptions()

    frames = []
    for mode in args.modes:
        df = load_mode(mode, DELAY_PACKAGES[mode], args.since)
        if len(df):
            log(f"  {mode}: {len(df):,} rows normalised")
            frames.append(df)

    if not frames:
        log("ERROR no data loaded")
        return 1

    full = pd.concat(frames, ignore_index=True)
    log(f"combined: {len(full):,} incident rows")

    parquet_path = PROCESSED_DIR / "delays.parquet"
    try:
        full.to_parquet(parquet_path, index=False)
        log(f"wrote {parquet_path.name} ({parquet_path.stat().st_size/1e6:.1f} MB)")
    except Exception as exc:  # noqa: BLE001
        log(f"WARN parquet write failed ({exc}); writing CSV instead")
        full.to_csv(PROCESSED_DIR / "delays.csv.gz", index=False, compression="gzip")

    summary = summarise(full, codes)
    write_json(SITE_DATA / "history.json", summary)
    log(f"wrote history.json: {summary['meta']['rows']:,} rows, "
        f"{summary['meta']['total_delay_hours']:,} delay-hours, "
        f"{summary['meta']['first_date']} to {summary['meta']['last_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
