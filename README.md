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

- `--map` snaps the run onto the OpenStreetMap street/path network. It needs internet the first
  time for each area; the map is cached in ~2 km tiles under `cache/tiles/`. Strongly recommended,
  especially for older watch files with points every ~8 s.
- `--cadence` uses cadence to keep pace steady through GPS drift. Cadence outside your normal
  running band (wonky readings, walking) is ignored automatically.
- Without flags, only the GPS smoother runs (no map, no cadence).

### Smashrun

The token is read from `~/.config/run-smoother/token` and only sent in a request header. Every run
is saved untouched to `backups/<id>_original.json` before anything else happens.

```
# preview only: fetch, smooth, show before/after, splits and plots
.venv/bin/python smooth_run.py --smashrun https://smashrun.com/<you>/run/<id>

# replace the run on Smashrun (deletes it, uploads the smoothed version; restores on failure)
.venv/bin/python smooth_run.py --smashrun <id> --apply

# replace a run with a smoothed local original, e.g. after a manual fix
.venv/bin/python smooth_run.py original.tcx --replace <id> --apply
```

Runs that look already processed are skipped unless you add `--force`. That covers file uploads,
a headline distance that doesn't match the track, and runs this tool wrote (their external ID
starts with `smoothrun-`). Your notes are left untouched.

Smashrun has no in-place update for GPS tracks, so a replace deletes the run and re-uploads it.
Smashrun keeps the same activity ID and your notes. Pauses are settled first: Smashrun zeroes
distance across pauses, so false auto-pauses (you kept moving) are dropped and real stops are kept.
Smashrun modes use the map and cadence by default (`--no-map` and `--no-cadence` turn them off).

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
