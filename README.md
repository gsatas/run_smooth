# smooth_run_algo

Cleans up GPS tracks from runs (TCX from Smashrun) to get realistic distance, splits and pace,
and writes a corrected TCX to re-upload.

## Usage

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once

.venv/bin/python smooth_run.py run.tcx --map --cadence
```

Writes `output/<name>_smoothed.tcx` plus `<name>_smoothed.png` (pace/diagnostics) and
`<name>_smoothed_map.png` (matched route). Prints distance and km/mile splits next to what was
recorded. To re-upload to Smashrun, delete the original activity first (it dedupes by start time).

- `--map` snaps the run onto the OpenStreetMap street/path network (needs internet the first time
  for each area; cached in `cache/`). Strongly recommended, especially for older watch files with
  points every ~8 s.
- `--cadence` uses cadence to keep pace steady through GPS drift. Cadence outside your normal
  running band (wonky readings, walking) is ignored automatically.
- Without flags, only the GPS smoother runs (no map, no cadence).

## How it works

1. **Smooth** (`smoothrun/smoother.py`): robust least-squares fit of the whole track. It uses a
   smooth-motion prior, a pace ceiling, and a "stopped" state for lights. GPS outliers are
   down-weighted.
2. **Map match** (`smoothrun/osm.py`, `smoothrun/mapmatch.py`): HMM matching onto street
   centrelines and paths. Sidewalks, crosswalks, plaza outlines and car-only tunnels/bridges are
   dropped. It is tuned for urban-canyon GPS that sits 30–80 m off for minutes. Back-and-forth
   jitter is removed, but real out-and-back turnarounds are kept.
3. **Along the route** (`smoothrun/along.py`): solves distance-along-route for every second, with
   cadence (speed = stride × cadence; stride calibrated from GPS). Where cadence clearly shows you
   ran a line the map doesn't have (cut corner, unmapped path), that stretch uses the GPS track's
   own shape.

`validate_synthetic.py` simulates Manhattan-style runs with known truth (realistic GPS errors,
drift, stops, auto-pause gaps, flaky cadence, optional `--run-walk`). Use it to check changes:
`.venv/bin/python validate_synthetic.py --trials 20`.

## Validation so far

- Measured half marathon: 21,206 m vs 21,097.5 m certified (+0.5%; the watch said +0.4%).
- Same 5K course run twice (2024, 2026): 5,181 m and 5,155 m.
- Simulation: distance within about ±0.2%, mile splits typically within 1–2 s.

## If results look weird

- Look at `<name>_smoothed_map.png` first. Most problems are a wrong matched route (detour onto a
  parallel street, a missing connection, or a path missing from OSM).
- Straight lines through blocks mean the matcher found no network connection there.
- Suspicious pace dips or spikes: compare runs with and without `--cadence`.
- Known open item: in one simulated run-walk case the distance was +2.8%. The real half was fine.
