# bpo-npi-observation-planner

Turn a list of airports and a **required NPI quality level** into the concrete
list of price-collection observations the BPO team should gather this week —
the same hand-off previously produced by hand on the
[npi-coverage-v5 dashboard](https://personal.uberinternal.com/anton.tereshchuk/npi-coverage-v5/dashboard.html)
and published to
[this Google Sheet](https://docs.google.com/spreadsheets/d/19UAkSYujxgFDz39IBMexUFXKs_nxU5yAmGFKONDqivo/edit).

You give it targets; it figures out how many routes/observations each airport
needs, removes the ones already collected, packs the rest into BPO shifts under
the 30-per-hour rule, geocodes the addresses, and publishes the sheet.

It ships **both halves of the pipeline**: the *route-generation* stage that
builds the coverage curves from the demand snapshot (`scripts/routegen/`), and
the *consumer* that turns curves + quality targets into the observation list. By
default it consumes a **vendored curve snapshot** (fast, nothing to recompute);
route generation is a config toggle (§9) for refreshing the curves or adding an
airport. The asset imports nothing outside its own folder.

> **One command:** `python run.py`. First run builds `./.venv` automatically.
> Supported out of the box: the **193 EMEA airports** in the vendored snapshot.

---

## 1. What it produces

Five tabs (written to `outputs/<timestamp>/` and pushed to the Google Sheet —
the same five tabs as the historical hand-off):

### `PU_extraction` / `DO_extraction` — the observation list the BPO collects
One row per observation to collect. **PU** = pickup at the airport (departure
side); **DO** = drop-off at the airport (arrival side).

| column | meaning |
|---|---|
| `city_id` | Uber city id of the airport |
| `dayofweek`, `hourofday` | local day (1=Mon) + mid-hour of the occasion |
| `dayofweek_utc`, `hourofday_utc`, `bpo_shift_slot` | the UTC shift the BPO works this row in (e.g. `Mon 10:00Z`) |
| `pickup_address`, `dropoff_address` | human-readable endpoints (airport side + geocoded city side) |
| `pickup_lat/lng`, `dropoff_lat/lng` | coordinates |
| `pu_airport_code`, `do_airport_code` | which end is the airport |
| `time_bucket` | the occasion (e.g. `wkd_day`) |
| `route_rank` | importance rank of the route (1 = most important) |

### `bpo_shift_schedule` — the weekly staffing plan
One row per staffed hour-of-week cell: `day, hour_utc, slot, n_obs, breakdown`.
`n_obs` never exceeds **30** (the office capacity); `breakdown` lists which
`airport/trip/occasion` the observations in that hour belong to.

### `quality_summary` — planned vs realized NPI quality per cut
The tab named `quality_summary` in the sheet carries the **realized** (post-trim)
quality: `airport_code, trip_type, n_dropped, planned_/realized_NPI_quality,
planned_/realized_time_NPI_quality, planned_/realized_sample_coverage,
planned_/realized_g_coverage, tier_changed, status`. A local
`quality_summary.csv` (the *planned* 18-column table with `current_*`,
`to_be_*`, `n_obs_to_be_added`, …) is also written for transparency.

### `dropped_routes` — what the 30/hr cap forced out
`airport, trip, route_rank, time_bucket, reason`. When weekly demand exceeds
the office's 168×30 capacity, the least-important routes are trimmed here.

Also written locally: `resolved_cuts.csv` (the solver's target→routes decision
per cut, with a `status`) and `validation_report.md`.

---

## 2. How it works

```
inputs/targets.csv                    inputs/config.json
  airport, trip_type,
  npi_quality, time_npi_quality
        │
        ▼
 (0) REGENERATE (optional, config) ── rebuild routes.json + LUTs from the        ┐
        │   vendored demand CSV. OFF by default → consume the vendored snapshot.  │
        ▼                                                                        │
 (1) SOLVE  ── invert the coverage curve ─────────────────────────────┐         │
        │   for each (airport,trip): search scenarios (light/mid/max)   │         │
        │   and pick the plan reaching BOTH quality targets with the     │        │
        │   FEWEST observations. The user never picks a scenario.       │         │
        ▼                                                               │         │
 (2) FRESH OBSERVATIONS ── QueryRunner ─── existing BPO-collected obs   │         │
        │   over the last N weeks (cached in state/).                    │        │
        ▼                                                               │         │
 (3) BUILD ROWS ── explode the first N routes × their occasions,        │         │
        │   SKIP any (hex, occasion) already covered by ≥1 existing obs. │        │
        ▼                                                               │         │
 (4) SHIFTS ── pack rows into hour-of-week cells, ≤30 each, at their    │         │
        │   true local time; trim least-important routes if over cap.   │         │
        ▼                                                               │         │
 (5) ENRICH ── addresses (geocode cache) + UTC shift columns.          │         │
        ▼                                                               │         │
 (6) REALIZED QUALITY ── recompute tiers after the trim.               │         │
        ▼                                                               │         │
 (7) PUBLISH ── 5 tabs → Google Sheet (chunked, read-back verified)    │         │
        ▼         + local CSVs.                                         │         │
 (8) VALIDATE ── validation_report.md ◄────────────────────────────────┴─────────┘
```

**The two quality metrics.** Each airport/trip is scored on two tiers
(`Good > Moderate > Poor > Unacceptable`):
- **NPI quality** — from overall *sample coverage* (share of airport sessions
  whose (occasion, distance) cell has an observation) and *geo coverage* (same
  at the hex level).
- **time-NPI quality** — the tier of the **mean per-occasion score** across the
  10 occasions (half-up rounded). This rewards covering *all* times of day/week,
  not just the busy ones. (It is NOT the worst occasion's tier.)

**The solver** is the automation of dragging the dashboard's route slider until
the quality chip turns the colour you want, then reading off the plan. It does
this across all three scenarios and keeps the one with the **fewest total
observations** that still meets both targets — so the user only states *what
quality they want*, never *how* to get it. If no scenario can reach a target
(e.g. every scenario caps time-NPI below the ask), it returns the best
achievable plan and flags the cut `time_target_unreachable` /
`npi_target_unreachable` in `resolved_cuts.csv` and the validation report — never
silently.

**scenario (θ)** — an *internal* knob, chosen for you — controls how many
occasions each route covers: `max`=1.0 (all occasions), `mid`=0.75, `light`=0.5.
Higher θ ⇒ more observations per route ⇒ better time-NPI quality. The solver
trades this off against route count to minimise total observations (`resolved_cuts.csv`
records which scenario it picked per cut).

**Existing-observation exclusion.** A `(hex, occasion)` already covered by ≥
`min_obs` (default 1) existing observations for the airport's canonical
competitor is dropped from the request — the BPO isn't asked to re-collect it.

**BPO shift rule.** One office, one week = 168 hour-of-week cells, ≤30
observations per cell, each collected at its true local time (weekday occasions
on weekdays, weekend on weekends). When demand exceeds capacity, the
matroid-greedy trim drops the highest-`route_rank` (least important) routes
first, so quality loss is minimised.

---

## 3. Inputs

`inputs/targets.csv` — the sheet you fill in. You specify **only the airport, the
direction, and the two quality tiers you want** — not how to achieve them:

```csv
airport_code,trip_type,npi_quality,time_npi_quality
CDG,PU,Good,Moderate
CDG,DO,Good,Moderate
LHR,PU,Good,Moderate
```

- `trip_type` ∈ {PU, DO}; `npi_quality` / `time_npi_quality` ∈
  {Good, Moderate, Poor, Unacceptable}.
- **You do not choose a `scenario`.** The solver searches light/mid/max and
  returns the plan that meets both quality tiers with the **fewest observations**
  (validated: this reproduces all 76 of Anton's historical `output_scenario`
  picks). You *may* pin a `scenario` column to override the auto-choice — e.g. to
  reproduce a historical cut — but it is optional.

`inputs/config.json` — run parameters:

| key | default | meaning |
|---|---|---|
| `lookback_weeks` | 4 | window for the fresh existing-observations pull |
| `hex_size` | 7 | H3 resolution (must match the curves) |
| `min_obs` | 1 | existing obs per (hex, occasion) that count as "already covered" |
| `geocode` | true | fill missing city addresses via Nominatim (network) |
| `gsheet_id` | "" | target Google Sheet; empty ⇒ publish skipped |
| `routes_dir` / `demand_lut_dir` | `config/…_2026-05-12` | vendored curve snapshot in use |
| `regenerate_routes` | false | Step 0: rebuild the curves from the demand CSV before solving (see §9) |
| `regenerate_scope` | `targets` | `targets` = only airports in `targets.csv`; `all` = every airport in the demand CSV |
| `regenerate_full_exclusion` | false | with regenerate on: `false` reproduces the shipped curves (routes skip the airport-terminal exclusion for ~178 airports); `true` applies it to every airport (corrected — changes ~178 curves) |
| `demand_csv` / `demand_obs_csv` | `config/demand_2026-05-12/…` | vendored demand + observations snapshot (generation inputs) |
| `demand_snapshot_date` | `2026-05-12` | dates the regenerated `routes_<date>/` + `demand_lut_<date>/` output |

Alternatively, skip the solver entirely and give explicit route counts with
`--cuts <csv>` (columns `airport,trip_type,scenario,n_routes`).

---

## 4. Run it

```bash
./setup_env.sh                       # once — builds ./.venv (uv; ~30s)

python run.py                        # solve inputs/targets.csv, publish to the sheet
python run.py --no-publish           # local CSVs only, no sheet
python run.py --no-geocode           # skip network geocoding (cache only)
python run.py --end-date 2026-07-13  # obs window ends (exclusive) on this date
python run.py --gsheet-id <SHEET_ID> # publish to a specific sheet

# power users / reproduction:
python run.py --cuts inputs/my_cuts.csv --no-publish            # explicit route counts
python run.py --obs-csv <frozen_obs.csv> --no-publish           # frozen observations
```

Set `gsheet_id` in `inputs/config.json` (or pass `--gsheet-id`) to publish.

---

## 5. Outputs

```
outputs/<timestamp>/
├── PU_extraction.csv            # observation list, pickup-at-airport (16 cols)
├── DO_extraction.csv            # observation list, dropoff-at-airport
├── bpo_shift_schedule.csv       # weekly staffing, ≤30 obs / hour-of-week
├── quality_summary.csv          # PLANNED quality per cut (current vs to-be)
├── realized_quality_summary.csv # planned vs realized (post-trim) — the sheet's quality tab
├── dropped_routes.csv           # routes trimmed by the 30/hr cap
├── resolved_cuts.csv            # solver decision per cut (+ unreachable flags)
├── validation_report.md         # assumption → expected → actual → pass/fail
└── _work/                       # pre-enrichment intermediates
state/obs_<end>.csv              # cached fresh observations pull
```

---

## 6. Worked example — CDG / DO / mid, target (Good, Moderate)

1. **Solve.** Search CDG `DO` across light/mid/max for the cheapest plan reaching
   (Good, Moderate). `mid` at **N = 48 routes** (≈ 244 observations) wins — light
   would need far more observations and max slightly more. → cut `CDG, DO, mid, 48`.
2. **Existing obs.** Pull the last 4 weeks; CDG's canonical competitor is Bolt.
   Any (hex, occasion) already collected is skipped.
3. **Build.** Explode the top 48 routes × their `mid` occasions minus the
   skipped ones → the surviving DO rows.
4. **Shift-pack.** Each row is placed in a UTC hour matching its local occasion,
   ≤30 per hour. CDG (UTC+2 in summer) has room, so nothing is trimmed.
5. **Result.** `realized_quality_summary` shows CDG/DO planned = realized =
   (Good, Moderate); `resolved_cuts` status = `ok`.

---

## 7. Validation

`validation_report.md` is written every run (quality ≥ target, row
reconciliation, kept+dropped == requested, shift cap ≤ 30, completeness).

**Parity with the already-published artifacts** — run:

```bash
python scripts/verify_published_parity.py
```

- **Part A** re-runs the asset on the exact SNPE cuts + frozen observations and
  asserts `PU_extraction`, `DO_extraction`, `bpo_shift_schedule`,
  `dropped_routes` are **row-for-row identical** to the published hand-off sheet
  (`tests/fixtures/snpe_pilot_published/`), and `realized_quality_summary` tiers
  identical (coverage within 0.002, documented demand-snapshot drift).
- **Part B** asserts both solver paths reproduce **all 76** route counts in
  Anton's `output_scenario` sheet from the quality targets alone —
  `solve_one` (per pinned scenario) and `solve_best` (the user-facing
  auto-scenario, fewest-observations path).

Current status: **all parity checks pass** (PU 2937 / DO 522 / shifts 120 /
dropped 427 identical; solver 76/76).

Add `--live` to also pull the live sheet via `scripts/lib/fetch_live_sheet.py`.

**Parity of the route-generation stage** — run:

```bash
python scripts/verify_routegen_parity.py            # all 193 airports
python scripts/verify_routegen_parity.py --sample 20  # quick subset
```

Regenerates `routes.json` + demand LUTs from the vendored demand CSV and asserts
they are **field-for-field identical** to the shipped snapshot
(`config/routes_2026-05-12/` + `config/demand_lut_2026-05-12/`) — proof that the
in-asset generator faithfully rebuilds what the asset ships. Current status:
**all 193 airports reproduce the snapshot exactly.**

---

## 8. Provenance

This asset vendors snapshots of the npi-coverage-v5 curves and the
coverage-curve-extraction pipeline; it imports nothing outside its own folder at
run time. See [`config/PROVENANCE.md`](config/PROVENANCE.md) for the file-by-file
source map and how to refresh the vendored curves. Baseline `current_*` comes
from the dashboard `current.csv` (demand-free) and may differ by <1pp from a
same-day demand recompute; it never affects the observation list.

---

## 9. Generating / refreshing the curves (the "front half")

The coverage curves (`routes.json`) are produced by a geo-optimization over
airport session demand. The asset ships that stage in `scripts/routegen/` plus a
vendored **demand snapshot** (`config/demand_2026-05-12/emea_demand_full.csv`,
~206 MB), so it can rebuild the curves **fully offline** — no warehouse needed.

**When you'd use it:**
- refresh the curves after demand has shifted, or
- add an airport that isn't in the vendored 193.

**How (no terminal edits needed — a config toggle):** set in `inputs/config.json`
```json
"regenerate_routes": true,
"regenerate_scope": "targets"   // just the airports in targets.csv (fast); or "all"
```
then run as usual. Step 0 rebuilds `routes.json` + demand LUTs (and a
snapshot-local `current.csv`) into `config/routes_<demand_snapshot_date>/` +
`config/demand_lut_<demand_snapshot_date>/`, and the rest of the run consumes
them. `--regenerate-routes` / `--no-regenerate` override the toggle for one run.

Generation reads only the vendored demand CSV — it does **not** hit the
warehouse. It's deterministic: `scripts/verify_routegen_parity.py` confirms it
reproduces the shipped snapshot exactly.

### Bug-compatible reproduction vs. the corrected refresh

The shipped `routes.json` snapshot has a **latent gap**: the original dashboard
route-builder only carried airport coordinates for **15 hub airports**, so it
silently skipped the airport-terminal hex exclusion (the parent hex + its 6
neighbours) for the other ~178 airports. The demand LUTs, built from the
complete `config/airport_coords.json`, excluded the terminal ring for all 193.

To honour "do not re-generate the routes," the generator **reproduces the
snapshot as-is by default**: routes use the 15-entry
`config/airport_coords_routegen.json` (matching the published curves), LUTs use
the complete table (matching the published LUTs). That is why
`verify_routegen_parity.py` reproduces all 193 field-for-field.

To rebuild the curves **with** the airport-terminal exclusion applied to every
airport — the corrected behaviour — set in `inputs/config.json`:
```json
"regenerate_routes": true,
"regenerate_full_exclusion": true
```
This changes ~178 airports' curves (routes sitting on the terminal hex are
dropped), so it's opt-in only; leave it `false` to keep byte-for-byte parity with
the shipped snapshot.

**Refreshing the demand snapshot itself** (needs warehouse access) is a separate,
rarer step — re-pull `scripts/queries/airport_session_demand_emea.sql`, replace
`config/demand_<new_date>/…`, bump `demand_snapshot_date`, regenerate. Details in
[`config/PROVENANCE.md`](config/PROVENANCE.md) → "Regenerating the vendored data".
