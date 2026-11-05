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

The token is read from `~/.config/mile_marker/smashrun_token` and only sent in a request header. Every run
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

### Web service (Mile Marker's Smooth tab)

`server.py` serves Mile Marker's Smooth tab: paste a link or pick your latest run, get
before/after distance, splits and plots, then optionally replace the run on Smashrun or restore
the original. Replace and restore need a passphrase; previews never change Smashrun. It has no
published port;
Mile Marker reaches it over Docker's `mile_marker_default` network, so start Mile Marker first.

```
mkdir -p cache backups     # copy mapstore.sqlite into cache/ and your existing backups into backups/
PUID=$(id -u) PGID=$(id -g) docker compose up -d --build
docker compose logs run-smooth    # should say "token found"
```

The folder `~/.config/mile_marker` is mounted read-only (set `SMOOTH_SECRETS_DIR` to use another).
It holds `smashrun_token` and, to allow replacing runs, `smooth_passphrase`:

```
read -rs -p "Passphrase: " p && printf '%s' "$p" > ~/.config/mile_marker/smooth_passphrase && chmod 600 ~/.config/mile_marker/smooth_passphrase
```

Both files are read on each request, so changes need no restart. Personal Smashrun tokens expire
about every 60 days. When one does, the Smooth tab says so and shows an Update Smashrun token box
(also folded at the bottom of the tab): get a new token at https://api.smashrun.com/register
(log in, type `client`), paste it and the passphrase. The service checks the token with Smashrun
before saving it, and never returns or logs it. Only the token file is mounted writable; the
folder around it stays read-only. Without the passphrase file, replace and restore are off. Five wrong
passphrases lock them for 10 minutes.

Replace refuses a preview with warnings unless you tick Replace anyway, re-checks the run just
before deleting it, and can be applied once per preview. Restore works on runs this tool wrote:
it finds `backups/<old id>_original.json` from the run's `smoothrun-<old id>-...` external ID,
and saves the smoothed version as `backups/<id>_smoothed_<time>.json` first.

### Run copy (for Mile Marker's stats)

`smoothrun/mirror.py` keeps a local copy of your Smashrun runs in `mirror/`: each run's full
activity JSON in `mirror/activities/<id>.json`, plus `mirror/index.json` with one summary row per
run. A sync only reads from Smashrun. It lists runs since `SYNC_SINCE` (default `2026-01-01`, set
in `docker-compose.yml`), fetches new runs and runs whose `dateUpdatedUTC` changed (a run this
tool replaced can keep its ID), and marks runs no longer on Smashrun as removed without deleting
their files. It fetches at most 100 runs per sync, to stay under Smashrun's 250 requests an hour;
the rest come on the next sync.

```
mkdir -p mirror                                         # before the first docker compose up
curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:3000/api/runs/sync
python -m smoothrun.mirror sync --since 2026-01-01      # or from the command line
python -m smoothrun.mirror rebuild                      # recompute index.json from the files
```

The service serves `GET /runs`, `GET /runs/<id>`, `GET /runs/sync` and `POST /runs/sync`; Mile
Marker forwards them as `/api/runs/...`.

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
