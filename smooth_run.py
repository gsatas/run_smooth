#!/usr/bin/env python3
"""Smooth a run's GPS track for realistic distance, splits and pace.

Local file -> corrected TCX:
    .venv/bin/python smooth_run.py run.tcx --map --cadence

Smashrun run -> preview (nothing is changed on Smashrun without --apply):
    .venv/bin/python smooth_run.py --smashrun https://smashrun.com/<you>/run/47363102
    .venv/bin/python smooth_run.py --smashrun 47363102 --apply

Replace a Smashrun run with a smoothed local original (e.g. after a manual fix):
    .venv/bin/python smooth_run.py original.tcx --replace 47378221 [--apply]

--map snaps the run onto OpenStreetMap streets/paths (cached in cache/); --cadence uses cadence
along the route. Smashrun modes use both by default. Every Smashrun activity is saved untouched
to backups/ before anything else happens.
"""
import argparse
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import numpy as np

from smoothrun import pipeline, report
from smoothrun.tcx import read_tcx, write_tcx


def show(name, start, t, rec_dist, out, units):
    dur = t[-1] - t[0]
    print("%s  (%s, %d points, %d:%02d elapsed)" % (name, start.strftime("%Y-%m-%d %H:%M"),
                                                     len(t), dur // 60, dur % 60))
    rows = [("recorded", rec_dist[-1]), ("smoothed (no map)", out.smoothed.dist[-1])]
    if out.match is not None:
        lab = "on route" + (" +cadence" if out.used_cadence else "")
        rows.append((lab, out.distance))
    for lab, d in rows:
        print("  %-18s %7.0f m  %5.2f mi" % (lab, d, d / report.MILE))
    if out.used_cadence:
        print("  cadence used on %d%% of the run" % (100 * np.mean(out.along.cad_used)))
    if out.match is not None and out.match.splices:
        print("  off-map stretches using the GPS shape: %d" % len(out.match.splices))
    cols = [("recorded", rec_dist), ("smoothed", out.dist)]
    for uname, m in (("mi", report.MILE), ("km", 1000.0)):
        if units in (uname, "both"):
            print()
            report.print_splits(t, cols, m, uname)


def plots(base, t, cadence, rec_speed, out):
    os.makedirs("output", exist_ok=True)
    png = os.path.join("output", base + ".png")
    report.plot(png, SimpleNamespace(t=t, speed=rec_speed, cadence=cadence), out.smoothed, None,
                title=base)
    print("\n  wrote %s" % png)
    if out.match is not None:
        mpng = os.path.join("output", base + "_map.png")
        report.plot_map(mpng, out.net, out.smoothed, out.match, title=base)
        print("  wrote %s" % mpng)


def confirm(msg, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("not confirmed (non-interactive; pass --yes to confirm)")
        return False
    return input("%s Type 'yes' to continue: " % msg).strip().lower() == "yes"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="local TCX file")
    ap.add_argument("--smashrun", metavar="LINK_OR_ID", help="fetch and smooth a Smashrun run")
    ap.add_argument("--replace", metavar="LINK_OR_ID",
                    help="replace this Smashrun run with the smoothed local INPUT file")
    ap.add_argument("--apply", action="store_true", help="actually write to Smashrun")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation with --apply")
    ap.add_argument("--force", action="store_true", help="process runs that look already fixed")
    ap.add_argument("--map", action="store_true", help="match to the OSM street/path network")
    ap.add_argument("--cadence", action="store_true", help="use cadence along the route")
    ap.add_argument("--no-map", action="store_true", help="(Smashrun modes) skip map matching")
    ap.add_argument("--no-cadence", action="store_true", help="(Smashrun modes) ignore cadence")
    ap.add_argument("-o", "--output", help="corrected TCX path (local mode)")
    ap.add_argument("--units", choices=["mi", "km", "both"], default="both")
    args = ap.parse_args()

    if args.smashrun:
        return smashrun_mode(args)
    if not args.input:
        ap.error("give a TCX file, or --smashrun LINK")
    if args.replace:
        return replace_mode(args)

    tr = read_tcx(args.input)
    base = os.path.splitext(os.path.basename(args.input))[0] + "_smoothed"
    out = pipeline.run(tr.t, tr.lat, tr.lon, tr.cadence, use_map=args.map, use_cadence=args.cadence)
    rec = np.maximum.accumulate(np.nan_to_num(tr.dist, nan=0.0))
    show(base, tr.start, tr.t, rec, out, args.units)
    path = args.output or os.path.join("output", base + ".tcx")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_tcx(tr, out.lat, out.lon, out.dist, out.speed, path)
    print("\n  wrote %s" % path, end="")
    plots(base, tr.t, tr.cadence, tr.speed, out)


def smashrun_mode(args):
    from smoothrun import smashrun as S
    client = S.Client()
    aid = S.activity_id(args.smashrun)
    original = client.get(aid)
    bpath = S.backup(original)
    print("backed up original to %s" % bpath)
    warn = S.warnings_for(original)
    if warn and not args.force:
        print("Skipping %d - it looks already processed:\n  - %s\n(use --force to process anyway)"
              % (aid, "\n  - ".join(warn)))
        return 1
    run = S.to_run(original)
    out = pipeline.run(run.t, run.lat, run.lon, run.cadence, use_map=not args.no_map,
                       use_cadence=not args.no_cadence)
    rec = np.nan_to_num(run.dist_km * 1000.0)
    show("smashrun_%d" % aid, run.start, run.t, rec, out, args.units)
    rec_v = np.r_[0, np.diff(rec) / np.maximum(np.diff(run.t), 1e-3)]
    plots("smashrun_%d" % aid, run.t, run.cadence, rec_v, out)
    new = S.smoothed_activity(original, out.lat, out.lon, out.dist)
    return write_back(args, client, aid, new, bpath, original["distance"], out.distance)


def replace_mode(args):
    from smoothrun import smashrun as S
    client = S.Client()
    aid = S.activity_id(args.replace)
    target = client.get(aid)
    bpath = S.backup(target)
    print("backed up current Smashrun version of %d to %s" % (aid, bpath))
    tr = read_tcx(args.input)
    from datetime import datetime
    tgt_start = datetime.fromisoformat(target["startDateTimeLocal"])
    gap = abs((tr.start - tgt_start).total_seconds())
    if gap > 600 and not args.force:
        print("Refusing: %s starts %s but Smashrun run %d starts %s (%.0f min apart)."
              % (args.input, tr.start, aid, tgt_start, gap / 60))
        return 1
    out = pipeline.run(tr.t, tr.lat, tr.lon, tr.cadence, use_map=not args.no_map,
                       use_cadence=not args.no_cadence)
    base = "replace_%d" % aid
    rec = np.maximum.accumulate(np.nan_to_num(tr.dist, nan=0.0))
    show(base, tr.start, tr.t, rec, out, args.units)
    plots(base, tr.t, tr.cadence, tr.speed, out)
    start_local = tr.start.astimezone(tgt_start.tzinfo).isoformat()
    new = S.activity_from_track(target, start_local, tr.t, out.lat, out.lon, out.dist,
                                cadence=tr.cadence, elevation=getattr(tr, "alt", None))
    return write_back(args, client, aid, new, bpath, target["distance"], out.distance)


def write_back(args, client, aid, new, bpath, old_km, new_m):
    from smoothrun import smashrun as S
    print("\nSmashrun run %d: %.2f km -> %.2f km" % (aid, old_km, new_m / 1000))
    if not args.apply:
        print("Preview only - nothing changed on Smashrun. Re-run with --apply to replace it.")
        return 0
    if not confirm("This deletes run %d on Smashrun and uploads the smoothed version." % aid,
                   args.yes):
        return 1
    new_id = S.replace(client, aid, new, bpath)
    print("done: new activity id %s" % new_id)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
