# Walkthrough — how this works and how to talk about it

This document exists so you can explain every part of this project without
hedging. Read it once end to end, then run the three commands in "Make it
yours" so the code has your fingerprints on it.

---

## 1. The one-paragraph version

> I built a dashboard that tracks TTC reliability. A GitHub Actions job hits the
> TTC's realtime feed every ten minutes, computes on-time performance per route,
> and commits the result back to the repo — so the git history is the time-series
> database, with no server to pay for. Separately I pull the City of Toronto's
> delay archives going back to 2014, about a million records across three
> networks with a schema that changes almost every year, normalise them into one
> table, and aggregate them. The site is a single self-contained HTML file served
> from GitHub Pages.

If you only memorise one thing, memorise that. It covers ingestion, scheduling,
storage, cleaning, aggregation, and deployment in six sentences.

---

## 2. The architecture, and why each piece is the way it is

### Storage: commit the data to git

Most portfolio projects that need a database either don't have one (so they
analyse a static CSV and stop) or spin up a free-tier Postgres that expires.
This one appends to a CSV in the repo and lets GitHub Actions commit it.

**Why it's good:** free forever, durable, versioned, and every historical value
is auditable — you can `git log` a number and see the run that produced it.

**What it costs:** it's append-only and not queryable without loading the file.
That's fine at this scale (~4,300 snapshots/month × ~200 routes ≈ 860k rows/year,
a few hundred MB). If it needed to serve concurrent queries, this would be the
wrong choice and you'd move to Postgres or DuckDB over object storage.

**Say that trade-off out loud in an interview.** Knowing when your design stops
working is the thing that separates a junior who copied a tutorial from one who
made a decision.

### The split between `aggregate.py` and `collect_realtime.py`

`collect_realtime.py` does I/O: HTTP, protobuf decoding, file writes. It is thin
and hard to unit-test because testing it means mocking a network.

`aggregate.py` does arithmetic: cleaning, percentiles, bucketing, ranking. It has
zero third-party imports, touches nothing external, and takes plain dicts in and
plain dicts out.

That's why `tests/test_aggregate.py` can cover the logic completely without a
network connection, and why the CI workflow passes on a machine with no internet
access. **This is the single most defensible design decision in the project** —
if an interviewer asks "how would you test this?", the answer is "I already did,
and here's why the boundary is where it is."

### Scheduling

Three workflows, three cadences, because the data has three cadences:

| Workflow | Cadence | Why |
|---|---|---|
| `collect.yml` | every 10 min | the realtime feed changes constantly |
| `history.yml` | monthly, 8th | the City republishes archives monthly |
| `tests.yml` | every push | catch a broken aggregation before it writes bad rows |

The route-name lookup sits inside `collect.yml` but only fires in the first run
after midnight UTC on Mondays, because the static GTFS bundle changes every six
weeks or so and re-downloading it 4,000 times a month would be rude to the
City's servers and slow for no benefit.

---

## 3. The cleaning decisions (this is the part interviewers dig into)

Real data analysis is 80% deciding what to throw away. Every one of these is a
judgement call you should be able to justify.

**Implausible delays are dropped.** `MAX_PLAUSIBLE_DELAY_S = 7200`. GTFS-RT
feeds emit delays of 30,000–90,000 seconds when a vehicle is still broadcasting
against a trip it finished hours ago. Those aren't delays, they're stale trip
assignments. Two hours is the cutoff because a genuine surface-transit delay
doesn't exceed it.
*Follow-up you should expect:* "How do you know you're not throwing away real
extreme delays?" Honest answer: you can't be certain, which is why the headline
figures are medians — the cleaning and the statistic are two independent
defences against the same problem.

**Negative delays are nulled, but the row is kept.** In the historical archive,
a negative "Min Delay" is a data-entry error. But the incident still happened,
so the row still counts toward incident totals; only the delay *value* is
discarded. Dropping the row entirely would undercount incidents.

**Unparseable times become NaN, not midnight.** Some archive rows have a time
like `"n/a"`. `pd.to_datetime(..., errors="coerce")` gives NaT, and those rows
are excluded from the hourly profile. If you let them default to 0, you get a
giant fake spike at midnight — `test_unparseable_times_excluded_not_zeroed`
exists specifically to catch that regression.

**Mixed date formats.** The archives contain both `2019-04-13` and `13/04/2019`,
sometimes in the same column. `format="mixed"` resolves per value. Guessing one
format for the column silently mangles the other half.

**A minimum-observation floor on rankings.** Routes need ≥5 reporting vehicles
to appear in "least reliable". Without it, a route with two vehicles and one
stuck bus shows 0% on time and tops the list forever.
`test_live_payload_applies_observation_floor` locks this in.

**Empty feeds produce no row at all.** If everything is unreachable, the script
exits 1 without writing. A gap in the chart is honest; a row of zeros reads as a
reliability collapse that never happened.

---

## 4. Reading the dashboard code

`docs/index.html` is one file: tokens, layout, a small chart layer, and the
render logic. No framework, no CDN — which is exactly why "push it and it works"
is true.

The chart layer is four functions. Each one measures its host element, builds a
linear scale, and emits SVG:

- `lineChart()` — trends. Crosshair and tooltip; series labelled at their end
  points, with collision-nudging so overlapping labels push apart
- `barsH()` — horizontal ranked bars, rounded data-end anchored to the baseline
- `columns()` — the 24-hour profiles, 2px gap between bars
- `renderTable()` — every chart has a "Show data table" toggle

Colour is assigned by role, not by taste. Series colours live in CSS custom
properties, light and dark are two separately-chosen sets rather than an
inversion, and the three-series palette was checked for colour-vision separation.
Every chart with more than one series has both a legend *and* direct labels, so
identity never rests on colour alone — that's the accessibility requirement, and
the data tables are the fallback for anyone who can't use the visual at all.

---

## 5. Questions you should expect, and honest answers

**"Walk me through what happens when the collector runs."**
Actions checks out the repo, installs deps, runs `collect_realtime.py`. That
fetches three protobuf feeds, decodes them to dicts, passes them to
`build_snapshot()` which cleans the delays and produces one row per route plus a
system-wide row, appends to this month's CSV, and rewrites two JSON files the
dashboard reads. Then the workflow commits and pushes, rebasing first in case a
previous run is still landing.

**"Why medians instead of averages?"**
Delay distributions have a long right tail and the feed contains artifacts. One
stale vehicle reporting a 40,000-second delay moves a mean enormously and a
median not at all.

**"What's the weakest part of this?"**
Be honest — pick one and own it. Good candidates: the on-time threshold is a
convention I adopted rather than derived; the subway has no realtime feed so the
live layer covers surface only; the hourly profile applies a fixed UTC offset
rather than a proper timezone database, which is fine over weeks of aggregation
but wrong on the two DST changeover days a year.

**"How would you scale this to a real agency?"**
The ingestion shape holds. Storage moves from git to object storage plus DuckDB
or Postgres; the collector becomes a container on a schedule; the dashboard
queries an API instead of static JSON. The cleaning logic in `aggregate.py`
transfers unchanged, which is the point of keeping it dependency-free.

**"What did you find?"**
You need a real answer here, from your own data, after `make history` runs. Look
at the causes chart and the per-network hourly chart and write down two
sentences about what surprised you. **Do this before you send the link to
anyone** — a dashboard whose author can't say what it shows is worse than no
dashboard.

---

## 6. Make it yours

Do these three. They take an evening and they change this from something you
were handed into something you built on.

**1. Add a question of your own.** The data supports plenty that isn't built yet:
Are streetcars actually less reliable than buses, controlling for time of day?
Did reliability change after a specific service cut? Which routes have degraded
most since 2014? Add the aggregation to `build_history.py`, add a chart, write
one sentence of what you found.

**2. Write the finding into the page.** Right now the dashboard shows numbers and
lets the reader draw conclusions. Add a short "What this shows" paragraph at the
top with two things you actually found. That paragraph is what a hiring manager
reads, and it's the difference between "made a dashboard" and "did an analysis."

**3. Break it on purpose, then fix it.** Change `MAX_PLAUSIBLE_DELAY_S` to
1,000,000 and re-run the tests. Watch which test fails and why. Now you know
that code is load-bearing rather than decorative, and you'll never be caught out
explaining it.

---

## 7. Putting it on your resume

One project entry, three bullets, with the live link. Something like:

> **TTC Pulse — live transit reliability dashboard** · *Python, pandas, SQL-style
> aggregation, GitHub Actions, JavaScript/SVG* · `<your-live-url>`
> - Built an automated pipeline that samples the TTC's realtime feed every 10
>   minutes and has collected N snapshots to date, using scheduled jobs that
>   commit to version control as the storage layer — no server, no cost.
> - Normalised ~1M delay records published across a decade of inconsistent
>   schemas into a single analysis table, handling mixed date formats,
>   unparseable timestamps and out-of-range values.
> - Published a live dashboard with a tested aggregation layer and accessible
>   charts (data tables, dark mode, colour-vision-safe palette).

Fill in N once you have real numbers, and replace the third bullet with your own
finding from step 2 above — a specific result beats a description of the build
every time.
