"""Read and write TCX files, keeping everything we don't touch intact."""
from dataclasses import dataclass
from datetime import datetime
from typing import List
import xml.etree.ElementTree as ET

import numpy as np

TCD = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
AX = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
NS = {"t": TCD, "x": AX}

# Keep the conventional prefixes so the output looks like a normal Garmin TCX.
ET.register_namespace("", TCD)
ET.register_namespace("tpx", AX)  # ("ns<N>" prefixes are reserved by ElementTree)
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")


def _q(tag: str) -> str:
    return "{%s}%s" % (TCD, tag)


@dataclass
class Track:
    tree: ET.ElementTree
    points: List[ET.Element]   # Trackpoint elements, document order
    lap_of_point: np.ndarray   # lap index for each point
    laps: List[ET.Element]
    t: np.ndarray              # seconds since first point
    lat: np.ndarray            # NaN where missing
    lon: np.ndarray
    cadence: np.ndarray        # strides/min (TCX RunCadence), NaN where missing
    speed: np.ndarray          # recorded speed m/s, NaN where missing
    dist: np.ndarray           # recorded cumulative distance m, NaN where missing
    start: datetime


def _float(el, path):
    e = el.find(path, NS)
    return float(e.text) if e is not None and e.text else np.nan


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_tcx(path: str) -> Track:
    tree = ET.parse(path)
    root = tree.getroot()
    laps = root.findall(".//t:Lap", NS)
    points, lap_idx = [], []
    for li, lap in enumerate(laps):
        for tp in lap.findall(".//t:Trackpoint", NS):
            points.append(tp)
            lap_idx.append(li)
    if not points:
        raise ValueError("no trackpoints in %s" % path)

    times = [_parse_time(tp.find("t:Time", NS).text) for tp in points]
    t0 = times[0]
    t = np.array([(x - t0).total_seconds() for x in times])
    lat = np.array([_float(tp, "t:Position/t:LatitudeDegrees") for tp in points])
    lon = np.array([_float(tp, "t:Position/t:LongitudeDegrees") for tp in points])
    cad = np.array([_float(tp, "t:Extensions/x:TPX/x:RunCadence") for tp in points])
    spd = np.array([_float(tp, "t:Extensions/x:TPX/x:Speed") for tp in points])
    dist = np.array([_float(tp, "t:DistanceMeters") for tp in points])
    return Track(tree, points, np.array(lap_idx), laps, t, lat, lon, cad, spd, dist, t0)


def _set_child(parent, tag, text, after=()):
    """Set parent/tag text, creating it after the last existing sibling in `after`."""
    el = parent.find(_q(tag))
    if el is None:
        el = ET.Element(_q(tag))
        pos = 0
        for i, child in enumerate(list(parent)):
            if child.tag in [_q(a) for a in after]:
                pos = i + 1
        parent.insert(pos, el)
    el.text = text
    return el


def write_tcx(track: Track, lat, lon, dist, speed, path: str) -> None:
    for i, tp in enumerate(track.points):
        pos = tp.find(_q("Position"))
        if pos is None:
            pos = ET.Element(_q("Position"))
            tp.insert(1, pos)  # schema order: Time, Position, ...
        _set_child(pos, "LatitudeDegrees", "%.7f" % lat[i])
        _set_child(pos, "LongitudeDegrees", "%.7f" % lon[i], after=("LatitudeDegrees",))
        _set_child(tp, "DistanceMeters", "%.2f" % dist[i],
                   after=("Time", "Position", "AltitudeMeters"))
        tpx = tp.find("t:Extensions/x:TPX", NS)
        if tpx is not None:
            sp = tpx.find("{%s}Speed" % AX)
            if sp is None:
                sp = ET.Element("{%s}Speed" % AX)
                tpx.insert(0, sp)
            sp.text = "%.3f" % speed[i]

    prev_end = 0.0
    for li, lap in enumerate(track.laps):
        idx = np.where(track.lap_of_point == li)[0]
        if len(idx) == 0:
            continue
        end = dist[idx[-1]]
        _set_child(lap, "DistanceMeters", "%.2f" % (end - prev_end))
        _set_child(lap, "MaximumSpeed", "%.3f" % float(np.max(speed[idx])))
        prev_end = end

    ET.indent(track.tree, space="  ")
    track.tree.write(path, encoding="UTF-8", xml_declaration=True)
