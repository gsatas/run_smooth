"""Hourly weather for each run, from Open-Meteo's historical forecasts (no key needed).

Smashrun stores one weather reading per run and often none for uploaded runs, and never thunder,
fog or UV. This looks up the hours a run overlapped at its start point and keeps a summary per
run in mirror/weather.json, so each run is fetched once. Only the date and the start point,
rounded to about 1 km, leave the server.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from smoothrun import mirror

API = os.environ.get("WEATHER_API", "https://historical-forecast-api.open-meteo.com/v1/forecast")
WEATHER_VERSION = 1
FIELDS = ("temperature_2m", "relative_humidity_2m", "precipitation", "rain", "snowfall", "weather_code",
          "wind_speed_10m", "uv_index", "visibility")
RAIN_CODES = set(range(51, 68)) | {80, 81, 82}
SNOW_CODES = set(range(71, 78)) | {85, 86}
THUNDER_CODES = {95, 96, 99}
FOG_CODES = {45, 48}


def _get(lat, lon, day0, day1):
    q = urllib.parse.urlencode({
        "latitude": "%.2f" % lat, "longitude": "%.2f" % lon, "start_date": day0, "end_date": day1,
        "hourly": ",".join(FIELDS), "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "GMT"})
    req = urllib.request.Request("%s?%s" % (API, q), headers={"User-Agent": "run_smooth (personal use)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["hourly"]


def summarize(hourly, start_utc, end_utc):
    """The hours from the one the run started in to the one it ended in."""
    lo = start_utc.replace(minute=0, second=0, microsecond=0)
    idx = [i for i, t in enumerate(hourly["time"])
           if lo <= datetime.fromisoformat(t).replace(tzinfo=timezone.utc) <= end_utc]
    if not idx:
        return None
    col = lambda k: [hourly[k][i] for i in idx if hourly.get(k) and hourly[k][i] is not None]
    mean = lambda v: sum(v) / len(v) if v else None
    codes = set(col("weather_code"))
    vis = col("visibility")
    return {
        "v": WEATHER_VERSION, "temp_f": mean(col("temperature_2m")), "humidity": mean(col("relative_humidity_2m")),
        "wind_mph": mean(col("wind_speed_10m")), "precip_mm": sum(col("precipitation")),
        "rain": bool(codes & RAIN_CODES or codes & THUNDER_CODES or sum(col("rain")) >= 0.3),
        "snow": bool(codes & SNOW_CODES or sum(col("snowfall")) >= 0.1),
        "thunder": bool(codes & THUNDER_CODES), "fog": bool(codes & FOG_CODES or (vis and min(vis) <= 500)),
        "uv_max": max(col("uv_index"), default=None), "codes": sorted(codes),
    }


def for_runs(runs, root=None, fetch=_get):
    """{run id: summary} for runs with a start point, fetching the ones not saved yet."""
    path = os.path.join(root or mirror.MIRROR_DIR, "weather.json")
    try:
        with open(path) as fh:
            cache = json.load(fh)
    except (FileNotFoundError, ValueError):
        cache = {}
    if cache.get("version") != WEATHER_VERSION:
        cache = {"version": WEATHER_VERSION, "runs": {}}
    out, changed, failed = {}, False, 0
    now = datetime.now(timezone.utc)
    for r in runs:
        key = str(r["id"])
        if key in cache["runs"]:
            out[r["id"]] = cache["runs"][key]
            continue
        if not r.get("start_ll") or failed >= 3:
            continue
        start = datetime.fromisoformat(r["utc"])
        end = start + timedelta(seconds=r.get("elapsed_s") or 0)
        if now - end < timedelta(hours=2):
            continue   # let the hour finish first
        try:
            hourly = fetch(r["start_ll"][0], r["start_ll"][1], start.date().isoformat(), end.date().isoformat())
            got = summarize(hourly, start, end)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print("  weather: couldn't get run %s: %s" % (key, e), flush=True)
            failed += 1   # likely offline: stop for now and try again after the next sync
            continue
        cache["runs"][key] = got
        out[r["id"]] = got
        changed = True
    if changed:
        mirror._write_atomic(path, cache)
    return out


def saved(root=None):
    """{run id (str): summary} for every run looked up so far."""
    try:
        with open(os.path.join(root or mirror.MIRROR_DIR, "weather.json")) as fh:
            cache = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return cache.get("runs", {}) if cache.get("version") == WEATHER_VERSION else {}
