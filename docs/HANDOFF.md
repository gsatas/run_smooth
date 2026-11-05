# Handoff: state as of 2026-11-04

This is where the project stands and what comes next. Read the README first for usage and how
the pipeline works.

## Goal

The user runs (mostly in Manhattan, recording on a phone) and syncs runs to Smashrun. The phone's
GPS inflates distance by roughly 3–9%, which makes pace look worse and hides real improvement.
This tool computes realistic distance, splits and pace and writes the corrected run back to
Smashrun. The user cares about distance and splits most, then a clean map.

## Done

- **The pipeline** (`smoothrun/pipeline.py`): 2D robust smoother → OSM map matching → 1D
  along-route solve with cadence. On the measured half marathon it gives 21,216 m against
  21,097.5 m certified (+0.6%, partly clock start/stop). The same 5K course run twice gives
  5,181 / 5,155 m.
- **Local map** (`cache/mapstore.sqlite`, 725 MB, not in git): 3.07 M runnable ways for NJ, the
  NYC area, CT, RI and the Boston area. It was built with `build_mapstore.py` from Geofabrik's
  `us-northeast` extract, which has since been deleted. Runs outside those regions fall back to
  the public Overpass servers, which are often overloaded (they were unusable on 2026-11-04).
- **Smashrun from the CLI** (`smoothrun/smashrun.py`, `smooth_run.py --smashrun / --replace`):
  fetch, back up, smooth, preview, and write back only with `--apply`.
- **Already replaced on Smashrun**, from the user's original TCX recordings:
  - 47383521: Harlem run, Nov 3 2026
  - 47383542: Abbot Dash race, Oct 31 2026
  - 47383548: Abbot Dash race, Nov 2 2024

  These replaced the user's manual fixes. Their previous versions are in `backups/`. The user
  still needs to re-flag both races as races on the website (see the API facts below).

## Smashrun API facts we learned (several are not in their docs)

- **Auth:** a personal token in `~/.config/mile_marker/smashrun_token`, sent only as
  `Authorization: Bearer` via curl's `-H @-` so it never appears in `ps` or logs. Never print it.
  Personal tokens expire about every 60 days. A 401 means the user needs a new token from the
  Smashrun API explorer. The limit is 250 requests/hour. `GET /v1/auth/{token}` returns no useful
  scope info for personal tokens.
- **Reading:**
  - `GET /v1/my/activities/search?count=N` lists runs.
  - `GET /v1/my/activities/{id}` returns `recordingKeys` / `recordingValues`: clock (s), latitude,
    longitude, elevation, distance (**km**), cadence (**strides/min**, ~73), and sometimes
    heartRate. It also has `pauseIndexes` and a headline `distance` in km.
- **Placeholder points:** `(-1, -1)` lat/lon means no GPS fix. `to_run` masks these; without
  that the bounding box spans the planet.
- **Uploading:** `POST /v1/my/activities/` with JSON works and returns
  `{"activityIds": ["<id>"], "validations": {...}}`. Check `validations`.
- **No in-place update:** `PUT` returns **HTTP 405**. `PATCH` returns 200 but ignores `isRace`
  (and the docs say it only covers notes, duration, distance and start time). So a replace is:
  back up → `DELETE` → `POST` → re-POST the backup if the POST fails (`smashrun.replace`).
- **After a replace:**
  - If the deleted run was written by our client, the new upload **reuses the same activity
    ID**. Runs from other sources get a new ID.
  - Smashrun **keeps the existing notes** in place of the ones we send. We now never modify
    notes.
  - `isRace` is always stored as 0, so the user must re-flag races on the website.
- **Pauses:** Smashrun **zeroes distance across every pause index**. Its headline distance stays
  as sent, so the track and headline can disagree. `smashrun.settle_pauses` keeps a pause only
  where the smoother says the runner was stopped (<0.5 m/s across the gap) and drops false
  auto-pauses (the user's phone auto-pause is unreliable).
- **Distance must never decrease:** Smashrun rejects a distance series that ever decreases, even
  after rounding. We enforce `np.maximum.accumulate`.
- **Snapping:** Smashrun appears to snap runs within ~1% of 5K to exactly 5.000 km, recomputing
  both the distance series and duration. The user is fine with this.
- **Marking our runs:** runs we write have `externalId` = `smoothrun-<old id>-<UTC stamp>`.
  `warnings_for()` uses that to refuse re-processing without `--force`. It also flags file
  uploads (`source` = gpxfile/tcxfile/fitfile) and a headline distance that doesn't match the
  track, which is how manual fixes show up.

## Safety rules (agreed with the user)

- Always back up the full original JSON to `backups/<id>_original.json` before any write, and
  never overwrite an existing backup. The user also keeps the original GPS files on their phone.
- Show a preview before writing. Writes need an explicit `--apply`, or a confirm button in the
  web app.
- Do write tests on a throwaway activity first, one step at a time, and ask before any
  Smashrun write that isn't already approved.

## In progress: the Smooth tab in Mile Marker

Decided on 2026-11-04: instead of a separate web app, the UI is a **Smooth tab in Mile Marker**
(`../mile_marker`), kept separate from run logging for now. The user copies the smoothed distance
into the log by hand.

- **Step 1, done:** `server.py`, a stdlib HTTP service in its own container (`Dockerfile`,
  `docker-compose.yml`) on Mile Marker's Docker network. It runs preview jobs one at a time on a
  background thread and keeps the last 20 in memory. Mile Marker forwards an allowlist of routes
  under `/api/smooth/` to it.
- **Step 2, done:** the Smooth tab: paste a link, or tap Latest run (newest run with a GPS track)
  → before/after distance, splits, map and pace plots. Deployed and checked on real runs.
- **Token path:** now `~/.config/mile_marker/smashrun_token` (moved by the user on 2026-11-04).
- **Step 3, built:** Replace and Restore in the Smooth tab, each with a second-tap confirm. Writes
  need the **passphrase** in `~/.config/mile_marker/smooth_passphrase` (agreed with the user),
  because Mile Marker has no login and anyone on the tailnet can open it. Safety checks are listed
  in the README. Races get a reminder to re-flag them. Tested against an in-memory fake Smashrun
  (replace, restore, stale run, failed uploads, lockout).
- **Step 4, built:** logging from the Smooth tab into the Mile Marker plan. It logs whatever
  Smashrun has now (smoothed after a replace, original otherwise): distance in the plan's unit,
  moving time, and the ID link `https://smashrun.com/<userName>/run/<id>` (username from
  `GET /v1/my/userinfo`). Targets: the workout scheduled on the run's date, a workout already
  linked to that run ID (update after smoothing), or a new unplanned run.
- **Token updates:** an expired token (401) shows a clear message in the Smooth tab with an
  Update Smashrun token box: link to https://api.smashrun.com/register (log in, type `client`),
  token and passphrase fields. `POST /token` checks the token against `/my/userinfo` before
  writing it (mode 600); only the token file is mounted read-write.
- **Test activity:** **47383782** (uploaded 2026-11-04) is a copy of run 47363102 dated
  2026-11-04 07:00, with notes saying it's a test. Deleted on 2026-11-04 after testing; a copy is
  in `backups/47383782_deleted_*.json`. For the next test, upload a fresh copy the same way and
  get the user's go-ahead first. After a DELETE, `GET /my/activities/<id>` kept returning the run
  for a while, but it was gone from the recent list right away.
- Date-style links (`/run/2026/11/3`) are now rejected with a clear message; before, they parsed
  as activity ID 2026. The user will paste ID links.

## In progress: a Smashrun-style stats view (run copy)

Agreed 2026-11-04: Smashrun stays the sync inbox; we keep a local copy of every run and build
stats and plots on it, kept separate from the plan and Smooth features.

- **Phase 1, done (branch `run-mirror` in both repos):** `smoothrun/mirror.py` and the `/runs`
  endpoints (see README, "Run copy"). Sync is manual only (a Sync now button; no timer).
  `SYNC_SINCE=2026-01-01`: the user's comeback runs (4 at first sync). The older backlog is to
  be copied later. Summaries are rebuilt from the saved files when `SUMMARY_VERSION` changes.
  Tests against a fake client: `python test_mirror.py`.
- **Phase 2, done (same branches):** the Stats tab, all in Mile Marker's `public/stats.js`
  (index.html only has the nav link, view, route hook and CSS). Sync now; this week / month /
  year / last 30 days with a comparison only when the earlier stretch is inside the copy; weekly
  distance (last 12 weeks, from SYNC_SINCE); pace per run (faster higher, races as rings, line =
  pace of the last 5 runs together); sortable run list linking to Smashrun (username saved at
  sync). Phones hide the climb and temperature columns. mi/km is per device (localStorage).
- **Phase 3, done:** one-run page at `#stats/<id>`, drawn in the browser from the saved activity.
  Moving time leaves out each pause (`pauseIndexes` mark the gap before that point), which
  matches Smashrun's duration. Splits interpolate moving time at each mile/km. Pace, cadence and
  HR are averaged over 200 m; cadence is stored in strides/min and shown as steps/min (×2).
  Route is an SVG line (no map tiles, so the CSP stays closed). One cursor across all charts and
  the route.
- **Achievements, first cut done:** `achievements/catalog.json` is the source of truth (244
  entries, tidied from `~/RunningAchievements.xlsx` with the user; `first_cut` marks the 205
  computed now, `later` says what the rest need). `smoothrun/achievements.py` replays runs since
  `ACH_SINCE` (= SYNC_SINCE, 2026-01-01) after every sync, writes `mirror/achievements.json` and
  serves `GET /achievements`; per-run facts are cached in `mirror/facts.json`. Tests:
  `python test_achievements.py`. User decisions:
  - Everything counts from 2026 runs only, except distance PRs, which should come from the old
    runs. PR trophies are left out until the 324 pre-2026 runs are copied.
  - Accept GPS-inflated best efforts; the user fixes suspicious runs by smoothing them.
  - Heat Wave dropped; It's Too Damn Hot is a one-off (≥95°F); Dog Days = 7 runs >85°F in one
    calendar year (any month). Race Day is the first race; Serial Racer starts at 5.
  - Weather uses Smashrun's single reading per run, often missing for API uploads (3 of the
    first 4 runs have none). Wind is assumed to be km/h. Sunrise/sunset, moon phases and
    solstices are computed locally. Best efforts and splits use moving time.
  Later: earlier PRs, NYC map shapes, birthday/time-zone settings, detailed weather, race names.
- **Later:** calendar heatmap, backlog sync (also needed for PRs).

## Original plan: the web app on the home server

The user is moving this work to their home server. It runs Docker services and is reachable only
over Tailscale. The agreed design:

- A small web app in Docker, **reachable only on the Tailscale network** (never public), because
  it holds a token that can delete runs.
- Paste a Smashrun link → fetch + back up → run the pipeline → **preview page** with before/after
  distance, splits, the map plot and the pace plot → a "Replace on Smashrun" button → result.
- A "restore original" action that uses the backup.
- Later: list recent runs with before/after distance and batch-correct them after review. The
  user said history isn't a priority ("fresh start" after a few years off).
- Show a clear message when the token has expired (401).
- **Volumes:** `cache/` (holds the 725 MB mapstore plus the Overpass tile cache), `backups/`,
  and `output/` for plots. Mount the token file read-only as a file or Docker secret.
- **Image needs:** Python with numpy, scipy, matplotlib and osmium (`requirements.txt`), plus
  **curl**, because all HTTP goes through curl via subprocess (originally to work around macOS
  LibreSSL; harmless on Linux). It would be fine to switch to `requests` on the server.
- **Pipeline speed:** about 1 s per run with the local map. Overpass fallback can take minutes or
  fail, so the web app should run jobs in the background and report errors.

## Not in git: copy to the server

- `cache/mapstore.sqlite` (725 MB). Rebuild with `build_mapstore.py <extract.osm.pbf>` (one pass,
  ~5 min, needs ~2 GB RAM) after downloading `us-northeast-latest.osm.pbf` (~1.8 GB) from
  download.geofabrik.de. The regions are listed in `build_mapstore.py`.
- `backups/`: originals of every Smashrun run we've touched. Keep these.
- `example_files/`: the user's TCX test runs, used for checks. `half_part_2.TCX` is the measured
  half; `40716350` and `47363104` are the Abbot Dash 5K, same course.
- The token file `~/.config/mile_marker/smashrun_token`.
- `scratch/` and `output/` are disposable.

## Open items

- One simulated run-walk case is off by +2.8% (`validate_synthetic.py --run-walk`). The real
  half with run-walk was fine.
- Cadence while walking is ignored. A separate walking-stride band could use it.
- Running tracks are included when OSM maps them (`leisure=track`). The half's finish track
  (Pierce Memorial Stadium, East Providence) isn't mapped; start/finish off the map falls back to
  the GPS shape.
- A small kink in the matched route near a Central Park entrance on run 47363102 (a few metres).
