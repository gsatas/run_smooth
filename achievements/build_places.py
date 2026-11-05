#!/usr/bin/env python3
"""Build achievements/places.json, the outlines the place achievements use (smoothrun/places.py).

Inputs, downloaded once:
  * NYC borough boundaries, clipped to the shoreline (NYC Open Data, dataset gthc-hcne):
      curl -o boroughs.geojson 'https://data.cityofnewyork.us/api/geospatial/gthc-hcne?method=export&format=GeoJSON'
  * Westchester and Nassau counties (Census TIGERweb), the land beside the Bronx and Queens, so
    their borders don't look like shoreline:
      curl -G -o counties.geojson 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query' \\
        --data-urlencode "where=GEOID IN ('36119','36059')" --data-urlencode outFields=NAME \\
        --data-urlencode outSR=4326 --data-urlencode f=geojson

    python achievements/build_places.py boroughs.geojson counties.geojson

Rings are simplified to within 5 m and stored as [lon, lat] lists.
"""
import json
import math
import sys

TOL_M = 5.0
KX = 111320.0 * math.cos(math.radians(40.7))
KY = 110950.0


def simplify(ring):
    """Douglas-Peucker in local metres, keeping the first and last points."""
    pts = [(x * KX, y * KY) for x, y in ring]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, at = -1.0, None
        for i in range(a + 1, b):
            px, py = pts[i]
            if L2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > best:
                best, at = d, i
        if at is not None and best > TOL_M:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [[round(x, 6), round(y, 6)] for (x, y), k in zip(ring, keep) if k]


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in polys:
        for ring in poly:
            r = simplify(ring)
            if len(r) >= 4:
                out.append(r)
    return out


def main(boroughs_path, counties_path, out="achievements/places.json"):
    with open(boroughs_path) as fh:
        boroughs = {f["properties"]["boroname"]: rings(f["geometry"]) for f in json.load(fh)["features"]}
    with open(counties_path) as fh:
        neighbors = {f["properties"]["NAME"]: rings(f["geometry"]) for f in json.load(fh)["features"]}
    doc = {"source": "NYC Open Data gthc-hcne (boroughs, shoreline-clipped); Census TIGERweb (neighbors)",
           "boroughs": boroughs, "neighbors": neighbors}
    with open(out, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    n = sum(len(r) for v in list(boroughs.values()) + list(neighbors.values()) for r in v)
    print("wrote %s: %d points" % (out, n))


if __name__ == "__main__":
    main(*sys.argv[1:3])
