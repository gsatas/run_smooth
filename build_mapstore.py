#!/usr/bin/env python3
"""Build a compact local map (cache/mapstore.sqlite) from an OpenStreetMap .osm.pbf extract,
so the smoother doesn't depend on the public Overpass servers.

Keeps only ways a runner could use (anything tagged highway=*, plus leisure=track) inside the
listed regions, with a whitelist of tags. One pass over the file; the big .pbf can be deleted
afterwards (re-download from download.geofabrik.de if a new region is ever needed).

    .venv/bin/python build_mapstore.py cache/us-northeast-261103.osm.pbf
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import numpy as np
import osmium

# (name, south, west, north, east)
REGIONS = [
    ("New Jersey", 38.92, -75.56, 41.36, -73.89),
    ("NYC area", 40.48, -74.27, 41.10, -73.65),
    ("Connecticut", 40.95, -73.73, 42.05, -71.78),
    ("Rhode Island", 41.14, -71.91, 42.02, -71.12),
    ("Boston area", 42.15, -71.35, 42.55, -70.85),
]
KEEP_TAGS = {"highway", "name", "foot", "access", "area", "footway", "path", "tunnel", "bridge",
             "leisure", "sidewalk", "layer", "surface"}
SKIP_HIGHWAY = {"proposed", "construction", "abandoned", "razed", "raceway", "bus_guideway",
                "platform", "elevator"}


def in_regions(minlat, minlon, maxlat, maxlon):
    for _, s, w, n, e in REGIONS:
        if maxlat >= s and minlat <= n and maxlon >= w and minlon <= e:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbf")
    ap.add_argument("-o", "--out", default="cache/mapstore.sqlite")
    args = ap.parse_args()

    tmp = args.out + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE ways (id INTEGER PRIMARY KEY, tags TEXT, refs BLOB, lat BLOB, lon BLOB);
        CREATE VIRTUAL TABLE ways_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
    """)

    fp = (osmium.FileProcessor(args.pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
          .with_filter(osmium.filter.KeyFilter("highway", "leisure")))
    t0, seen, kept, batch = time.time(), 0, 0, []
    for w in fp:
        seen += 1
        tags = {t.k: t.v for t in w.tags if t.k in KEEP_TAGS}
        hw = tags.get("highway")
        if hw is None and tags.get("leisure") != "track":
            continue
        if hw in SKIP_HIGHWAY:
            continue
        try:
            locs = [(n.ref, n.location.lat, n.location.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            continue
        if len(locs) < 2:
            continue
        arr = np.array(locs, dtype=np.float64)
        lat, lon = arr[:, 1], arr[:, 2]
        if not in_regions(lat.min(), lon.min(), lat.max(), lon.max()):
            continue
        batch.append((w.id, json.dumps(tags, separators=(",", ":")),
                      np.array([r for r, _, _ in locs], dtype=np.int64).tobytes(),
                      np.round(lat * 1e7).astype(np.int32).tobytes(),
                      np.round(lon * 1e7).astype(np.int32).tobytes(),
                      lat.min(), lat.max(), lon.min(), lon.max()))
        kept += 1
        if len(batch) >= 20000:
            _flush(db, batch)
            print("  %d candidate ways read, %d kept (%.0fs)" % (seen, kept, time.time() - t0),
                  flush=True)
    _flush(db, batch)
    db.execute("INSERT INTO meta VALUES ('regions', ?)", (json.dumps(REGIONS),))
    db.execute("INSERT INTO meta VALUES ('source', ?)", (os.path.basename(args.pbf),))
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, args.out)
    print("done: %d ways kept -> %s (%.0f MB, %.0fs)" % (kept, args.out,
          os.path.getsize(args.out) / 1e6, time.time() - t0))


def _flush(db, batch):
    db.executemany("INSERT INTO ways VALUES (?,?,?,?,?)", [b[:5] for b in batch])
    db.executemany("INSERT INTO ways_idx VALUES (?,?,?,?,?)",
                   [(b[0], b[5], b[6], b[7], b[8]) for b in batch])
    db.commit()
    batch.clear()


if __name__ == "__main__":
    sys.exit(main())
