"""Fetch the walkable OpenStreetMap network around a run and turn it into a graph.

Only the bounding box is sent to the Overpass API; results are cached under cache/.
"""
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from .smoother import to_local

# Public Overpass servers, tried in turn (the main one is often at capacity).
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "smoothrun-gps-smoother/0.1"

# Ways a runner can't (or won't) be on.
EXCLUDE_HIGHWAY = {"motorway", "motorway_link", "construction", "proposed", "raceway",
                   "bus_guideway", "platform", "elevator", "corridor", "abandoned", "razed"}


def _query(bbox):
    s, w, n, e = bbox
    return ('[out:json][timeout:120];'
            '(way["highway"](%.5f,%.5f,%.5f,%.5f);way["leisure"="track"](%.5f,%.5f,%.5f,%.5f););'
            '(._;>;);out body qt;' % (s, w, n, e, s, w, n, e))


def fetch(bbox, cache_dir="cache"):
    """bbox = (south, west, north, east) in degrees. Returns raw Overpass JSON (cached)."""
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha1(("%.4f,%.4f,%.4f,%.4f" % tuple(bbox)).encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, "osm_%s.json" % key)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    # curl rather than urllib: macOS system Python's LibreSSL fails the TLS handshake.
    # The public server sometimes rate-limits or times out; retry, and never cache a bad reply.
    last = None
    for attempt in range(6):
        server = OVERPASS_SERVERS[attempt % len(OVERPASS_SERVERS)]
        if attempt >= len(OVERPASS_SERVERS):
            time.sleep(15 * 2 ** (attempt - len(OVERPASS_SERVERS)))  # exponential backoff
        out = subprocess.run(["curl", "-sS", "-m", "180", "-A", USER_AGENT, "--data-urlencode",
                              "data=" + _query(bbox), server], capture_output=True)
        try:
            data = json.loads(out.stdout.decode())
        except ValueError:
            last = "%s: %s" % (server, (out.stdout.decode(errors="replace")
                                        or out.stderr.decode(errors="replace"))[:150])
            continue
        if data.get("remark") and "error" in data["remark"].lower() or not data.get("elements"):
            last = "%s: %s" % (server, data.get("remark", "empty response"))
            continue
        break
    else:
        raise RuntimeError("Overpass API failed after retries: %s" % last)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def runnable(tags):
    hw = tags.get("highway")
    if hw is None and tags.get("leisure") == "track":
        return True   # running tracks (often access=private at schools; races finish on them)
    if hw is None or hw in EXCLUDE_HIGHWAY:
        return False
    if tags.get("area") == "yes":
        return False  # plaza outlines aren't lines anyone runs along
    # Separately-mapped sidewalks and crosswalks run parallel to the street centreline; keeping
    # them makes the matcher zig-zag between them. Match to the "runner's line" instead.
    if tags.get("footway") in ("sidewalk", "crossing", "traffic_island") or \
            tags.get("path") in ("sidewalk", "crossing"):
        return False
    foot = tags.get("foot")
    access = tags.get("access")
    if foot in ("no", "private"):
        # Races can use closed roads, so a plain road with foot=no stays unless it's a car tunnel/trunk.
        if tags.get("tunnel") == "yes" or tags.get("bridge") == "yes" or hw in ("trunk", "trunk_link"):
            return False
    if access in ("no", "private") and foot not in ("yes", "designated", "permissive"):
        return False
    return True


@dataclass
class Network:
    xy: np.ndarray        # (N, 2) node coordinates in the run's local frame (m)
    ea: np.ndarray        # edge endpoint node indices
    eb: np.ndarray
    elen: np.ndarray      # edge length (m)
    eway: np.ndarray      # index into way_tags
    way_tags: list
    adj: sp.csr_matrix    # symmetric adjacency weighted by length
    tree: cKDTree         # over points sampled along edges
    sample_edge: np.ndarray

    def candidates(self, p, radius, k_max=8, merge_m=3.0):
        """Nearest positions on the network to point p: list of (edge, frac, qx, qy, dist)."""
        idx = self.tree.query_ball_point(p, radius + 5.0)
        if not idx:
            return []
        edges = np.unique(self.sample_edge[idx])
        a, b = self.xy[self.ea[edges]], self.xy[self.eb[edges]]
        ab = b - a
        L2 = np.maximum((ab ** 2).sum(1), 1e-9)
        tau = np.clip(((p - a) * ab).sum(1) / L2, 0, 1)
        q = a + tau[:, None] * ab
        d = np.hypot(*(q - p).T)
        order = np.argsort(d)
        out, seen = [], set()
        for j in order:
            if d[j] > radius:
                break
            # One candidate per street (nearest point on it), so a handful of nearby streets
            # can't crowd out the one you were actually on.
            w = self.eway[edges[j]]
            key = (self.way_tags[w].get("name") or ("way", w)) if w >= 0 else ("edge", edges[j])
            if key in seen:
                continue
            if any(np.hypot(q[j, 0] - c[2], q[j, 1] - c[3]) < merge_m for c in out):
                continue
            seen.add(key)
            out.append((int(edges[j]), float(tau[j]), float(q[j, 0]), float(q[j, 1]), float(d[j])))
            if len(out) >= k_max:
                break
        return out


def build(data, lat0, lon0, sample_step=4.0, reconnect_m=25.0):
    nodes = {}
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
    node_index, coords = {}, []
    ea, eb, eway, way_tags = [], [], [], []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        if not runnable(tags):
            continue
        wi = len(way_tags)
        way_tags.append(tags)
        refs = [r for r in el.get("nodes", []) if r in nodes]
        for r in refs:
            if r not in node_index:
                node_index[r] = len(coords)
                coords.append(nodes[r])
        for r1, r2 in zip(refs, refs[1:]):
            if r1 != r2:
                ea.append(node_index[r1])
                eb.append(node_index[r2])
                eway.append(wi)
    ll = np.array(coords)
    x, y = to_local(ll[:, 0], ll[:, 1], lat0, lon0)
    xy = np.c_[x, y]
    ea, eb = np.array(ea), np.array(eb)
    eway = np.array(eway)
    ea, eb, eway = _reconnect(xy, ea, eb, eway, reconnect_m)
    ea, eb, eway = _join_islands(xy, ea, eb, eway, way_tags, 40.0)
    # Keep only the largest connected piece so nothing can snap onto an isolated fragment.
    nn = len(xy)
    g = sp.coo_matrix((np.ones(len(ea)), (ea, eb)), shape=(nn, nn))
    _, comp = connected_components(g, directed=False)
    main = np.argmax(np.bincount(comp[ea], minlength=comp.max() + 1))
    keep = comp[ea] == main
    ea, eb, eway = ea[keep], eb[keep], eway[keep]
    elen = np.hypot(*(xy[eb] - xy[ea]).T)
    n = len(xy)
    adj = sp.coo_matrix((np.r_[elen, elen] + 1e-6, (np.r_[ea, eb], np.r_[eb, ea])), shape=(n, n)).tocsr()

    # Points every few metres along each edge for the spatial index.
    pts, pe = [], []
    for e in range(len(ea)):
        k = max(1, int(np.ceil(elen[e] / sample_step)))
        f = np.linspace(0, 1, k + 1)
        pts.append(xy[ea[e]] + f[:, None] * (xy[eb[e]] - xy[ea[e]]))
        pe.append(np.full(k + 1, e))
    pts, pe = np.vstack(pts), np.concatenate(pe)
    return Network(xy, ea, eb, elen, eway, way_tags, adj, cKDTree(pts), pe)


def _join_islands(xy, ea, eb, eway, way_tags, max_m):
    """Join closed loops that touch nothing (running tracks) to the nearest node of the main
    network within max_m, so they aren't discarded as isolated fragments."""
    nn = len(xy)
    g = sp.coo_matrix((np.ones(len(ea)), (ea, eb)), shape=(nn, nn))
    _, comp = connected_components(g, directed=False)
    main = np.argmax(np.bincount(comp[ea], minlength=comp.max() + 1))
    main_nodes = np.unique(np.r_[ea, eb][comp[np.r_[ea, eb]] == main])
    if len(main_nodes) == 0:
        return ea, eb, eway
    tree = cKDTree(xy[main_nodes])
    is_track = np.array([w >= 0 and way_tags[w].get("leisure") == "track" for w in eway])
    new_a, new_b = [], []
    for c in np.unique(comp[ea[is_track]]):
        if c == main:
            continue
        nodes = np.unique(np.r_[ea, eb][comp[np.r_[ea, eb]] == c])
        d, j = tree.query(xy[nodes])
        k = int(np.argmin(d))
        if d[k] <= max_m:
            new_a.append(nodes[k])
            new_b.append(main_nodes[j[k]])
    if new_a:
        ea, eb = np.r_[ea, new_a], np.r_[eb, new_b]
        eway = np.r_[eway, np.full(len(new_a), -1)]
    return ea, eb, eway


def _reconnect(xy, ea, eb, eway, max_m):
    """Join dead ends to the nearest node of another way within max_m. Dropping sidewalks
    leaves e.g. park paths that only touched a sidewalk dangling next to the street."""
    n = len(xy)
    deg = np.bincount(np.r_[ea, eb], minlength=n)
    node_ways = [set() for _ in range(n)]
    for a, b, w in zip(ea, eb, eway):
        node_ways[a].add(w)
        node_ways[b].add(w)
    used = deg > 0
    used_idx = np.where(used)[0]
    tree = cKDTree(xy[used_idx])
    new_a, new_b = [], []
    for d in np.where(deg == 1)[0]:
        for j in sorted(tree.query_ball_point(xy[d], max_m),
                        key=lambda j: np.hypot(*(xy[used_idx[j]] - xy[d]))):
            o = used_idx[j]
            if o != d and not (node_ways[o] & node_ways[d]):
                new_a.append(d)
                new_b.append(o)
                break
    if new_a:
        ea = np.r_[ea, new_a]
        eb = np.r_[eb, new_b]
        eway = np.r_[eway, np.full(len(new_a), -1)]  # -1 = synthetic connector
    return ea, eb, eway


TILE_DEG = 0.02   # ~2.2 km x 1.7 km at NYC's latitude


def _tiles_for(lat, lon, margin_deg):
    """Fixed grid tiles covering the track (+margin). Runs in the same neighbourhood share
    tiles, so after the first few runs most need no map server at all."""
    ok = np.isfinite(lat) & np.isfinite(lon)
    i0 = int(np.floor((np.min(lat[ok]) - margin_deg) / TILE_DEG))
    i1 = int(np.floor((np.max(lat[ok]) + margin_deg) / TILE_DEG))
    j0 = int(np.floor((np.min(lon[ok]) - margin_deg) / TILE_DEG))
    j1 = int(np.floor((np.max(lon[ok]) + margin_deg) / TILE_DEG))
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            yield (i * TILE_DEG, j * TILE_DEG, (i + 1) * TILE_DEG, (j + 1) * TILE_DEG)


MAPSTORE = "mapstore.sqlite"


def _from_mapstore(lat, lon, margin_deg, cache_dir):
    """Elements (Overpass-style) from the local map store, or None if the run isn't covered."""
    import sqlite3
    path = os.path.join(cache_dir, MAPSTORE)
    if not os.path.exists(path):
        return None
    ok = np.isfinite(lat) & np.isfinite(lon)
    s_, w_ = np.min(lat[ok]) - margin_deg, np.min(lon[ok]) - margin_deg
    n_, e_ = np.max(lat[ok]) + margin_deg, np.max(lon[ok]) + margin_deg
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        regions = json.loads(db.execute("SELECT value FROM meta WHERE key='regions'").fetchone()[0])
        if not any(s_ >= r[1] and w_ >= r[2] and n_ <= r[3] and e_ <= r[4] for r in regions):
            return None
        rows = db.execute(
            "SELECT w.id, w.tags, w.refs, w.lat, w.lon FROM ways_idx i JOIN ways w ON w.id = i.id "
            "WHERE i.maxlat >= ? AND i.minlat <= ? AND i.maxlon >= ? AND i.minlon <= ?",
            (s_, n_, w_, e_)).fetchall()
    finally:
        db.close()
    nodes, elements = {}, []
    for wid, tags, refs, la, lo in rows:
        refs = np.frombuffer(refs, dtype=np.int64)
        la = np.frombuffer(la, dtype=np.int32) / 1e7
        lo = np.frombuffer(lo, dtype=np.int32) / 1e7
        for r, a, b in zip(refs.tolist(), la.tolist(), lo.tolist()):
            nodes[r] = (a, b)
        elements.append({"type": "way", "id": wid, "nodes": refs.tolist(), "tags": json.loads(tags)})
    elements += [{"type": "node", "id": r, "lat": a, "lon": b} for r, (a, b) in nodes.items()]
    return {"elements": elements}


def network_for_track(lat, lon, lat0, lon0, margin_deg=0.003, cache_dir="cache"):
    local = _from_mapstore(lat, lon, margin_deg, cache_dir)
    if local is not None:
        return build(local, lat0, lon0)
    ok = np.isfinite(lat) & np.isfinite(lon)
    # Older per-run cache entries (pre-tiles) still work if present.
    bbox = (np.min(lat[ok]) - margin_deg, np.min(lon[ok]) - margin_deg,
            np.max(lat[ok]) + margin_deg, np.max(lon[ok]) + margin_deg)
    bbox = (np.floor(bbox[0] * 500) / 500, np.floor(bbox[1] * 500) / 500,
            np.ceil(bbox[2] * 500) / 500, np.ceil(bbox[3] * 500) / 500)
    key = hashlib.sha1(("%.4f,%.4f,%.4f,%.4f" % tuple(bbox)).encode()).hexdigest()[:16]
    if os.path.exists(os.path.join(cache_dir, "osm_%s.json" % key)):
        return build(fetch(bbox, cache_dir), lat0, lon0)

    seen, elements = set(), []
    for tile in _tiles_for(lat, lon, margin_deg):
        for el in fetch(tile, os.path.join(cache_dir, "tiles"))["elements"]:
            k = (el["type"], el["id"])
            if k not in seen:
                seen.add(k)
                elements.append(el)
    return build({"elements": elements}, lat0, lon0)
