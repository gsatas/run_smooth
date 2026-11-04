# smooth_run_algo

GPS smoother for the user's runs: realistic distance, splits and pace, written back to Smashrun.

- Read `docs/HANDOFF.md` first: current state, Smashrun API behaviour we discovered, safety rules
  and the web app plan. Usage and the pipeline design are in `README.md`.
- Never print or log the Smashrun token (`~/.config/run-smoother/token`).
- Never write to Smashrun without a backup in `backups/` and the user's go-ahead. Smashrun has
  no PUT, so a replace deletes the run and re-uploads it.
- Check changes with `python validate_synthetic.py --trials 20` and against the real runs in
  `example_files/`. The half marathon should come out around 21.2 km; the two Abbot Dash 5Ks
  around 5.16–5.18 km.
- GPS tracks, backups and the token are personal data; keep them out of git (see `.gitignore`).
