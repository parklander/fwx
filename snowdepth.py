"""Snow depth from ECCC GeoMet (WMS), for the map's Snow layers.

- CaLDAS-NSRPS (2.5 km): the land-surface analysis of snow on the ground, every 3 hours, four runs a day.
- HRDPS (2.5 km): forecast snow depth, hourly, about 48 hours ahead, from the model's own land surface
  (not from this CaLDAS analysis: in Nov 2026 CaLDAS still showed 4 cm at Shilo days after the snow had melted,
  while HRDPS and the stations showed none).

GeoMet only answers times inside each layer's window, so the page asks here for the current window and
model run. Product pages: https://eccc-msc.github.io/open-data/msc-data/nwp_caldas-nsrps/readme_caldas-nsrps_en/
and https://eccc-msc.github.io/open-data/msc-data/nwp_hrdps/readme_hrdps_en/ . Data Source: Environment and
Climate Change Canada (ECCC Data Servers End-use Licence).
"""
import io
import json
import logging
import re
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("snowdepth")

WMS = "https://geo.weather.gc.ca/geomet"
LAYERS = {
    "caldas": {"layer": "CaLDAS-NSRPS_2.5km_SnowDepth", "title": "Snow on ground (CaLDAS analysis)", "style": "SNOWDEPTH"},
    "hrdps": {"layer": "HRDPS.CONTINENTAL_SD", "title": "Snow depth forecast (HRDPS)", "style": "SNOWDEPTH"},
}
STALE_H = 18  # a run older than this means the layer has stopped updating (runs are every 6 h)
_session = requests.Session()
_lock = threading.Lock()
_cache = {"at": 0.0, "layers": {}}


def _dim(text, name):
    """(start, end, step) of a WMS Dimension 'start/end/PTnH' (or a single value), or None."""
    m = re.search(rf'<Dimension name="{name}"[^>]*>([^<]+)</Dimension>', text)
    if not m:
        return None
    parts = m[1].strip().split(",")[-1].split("/")
    return (parts[0], parts[1] if len(parts) > 1 else parts[0], parts[2] if len(parts) > 2 else None)


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def layers(max_age=300):
    """{key: {"layer", "title", "style", "start", "end", "step", "run", "stale"}} (cached 5 min)."""
    with _lock:
        if time.time() - _cache["at"] < max_age and _cache["layers"]:
            return _cache["layers"]

    def one(item):
        key, spec = item
        try:
            r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities",
                                          "layer": spec["layer"]}, timeout=30)
            r.raise_for_status()
            t, ref = _dim(r.text, "time"), _dim(r.text, "reference_time")
            if not t:
                return key, None
            run = ref[1] if ref else None  # the newest model run
            stale = bool(run) and datetime.now(timezone.utc) - _t(run) > timedelta(hours=STALE_H)
            return key, {**spec, "start": t[0], "end": t[1], "step": t[2], "run": run, "stale": stale}
        except Exception as e:
            log.warning("%s unavailable: %s", spec["layer"], e)
            return key, None

    with ThreadPoolExecutor(len(LAYERS)) as ex:
        out = {k: v for k, v in ex.map(one, LAYERS.items()) if v}
    with _lock:
        if out:
            _cache.update(at=time.time(), layers=out)
        return _cache["layers"]


# ---- snow on the ground inside an area, from the CaLDAS analysis (WCS) --------------------------------------
# The latest analysis cut to the area's box as a float GeoTIFF of snow depth in metres, one value per 2.5 km
# cell. "Covered" counts cells with at least COVER_CM. A daily sample per area is kept in SNOW_HISTORY so the
# page can say when the snow went and whether it's melting.
COVER_CM = 1.0
AREA_TTL = 3600
SNOW_HISTORY_DAYS = 150
_area_cache = {}
_hist_lock = threading.Lock()
HISTORY_FILE = None  # set by the app: data/snow_history.json


def _box(area, pad=0.02):
    b = area.get("bbox")
    if b:
        return b[0], b[1], b[2], b[3]
    return area["lon"] - 0.1, area["lat"] - 0.07, area["lon"] + 0.1, area["lat"] + 0.07


def grid(key, w, s, e, n, when=None):
    """Snow depth in cm over a lon/lat box from LAYERS[key] (numpy array; NaN where there's no value).
    when: an ISO time inside the layer's window (HRDPS forecast hour); None = the server's default."""
    import numpy as np
    from PIL import Image
    params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
              ("coverageId", LAYERS[key]["layer"]), ("subsettingcrs", "EPSG:4326"), ("format", "image/tiff"),
              ("subset", f"lat({s:.4f},{n:.4f})"), ("subset", f"long({w:.4f},{e:.4f})")]
    if when:
        params.append(("time", when))
    r = _session.get(WMS, timeout=60, params=params)
    r.raise_for_status()
    if not r.content.startswith((b"II*", b"MM\x00*")):
        raise ValueError("GeoMet didn't return a GeoTIFF: " + r.text[:120])
    a = np.array(Image.open(io.BytesIO(r.content)), dtype=np.float32) * 100.0  # m -> cm
    a[(~np.isfinite(a)) | (a < 0) | (a > 2000)] = np.nan
    return a


def _stats(a, when):
    import numpy as np
    v = a[np.isfinite(a)]
    if not v.size:
        return None
    return {"mean_cm": round(float(v.mean()), 1), "max_cm": round(float(v.max()), 1),
            "covered_pct": round(100.0 * float((v >= COVER_CM).mean())), "cells": int(v.size), "time": when}


def _hrdps_hour(L):
    """The HRDPS hour nearest now, inside the layer's window."""
    if not L:
        return None
    want = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t = min(max(want, _t(L["start"])), _t(L["end"]))
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


DISAGREE_PCT, DISAGREE_CM = 50, 3.0


def area_snow(area, stations=()):
    """Snow on the ground inside an area (cached 1 h), from three sources:
    the CaLDAS analysis, the HRDPS forecast for the hour nearest now, and snow depth measured at nearby stations
    (stations: [{name, km, snow, t}]). The best estimate is the grid closest to the stations' median; with no
    station measuring snow depth nearby, HRDPS (CaLDAS has been seen to hold snow days after it melted).
    Returns the best estimate's {mean_cm, max_cm, covered_pct, cells} plus source, sources, stations,
    station_median_cm, disagree, basis, snow_free_since and trend_cm_3d."""
    key = area["id"]
    hit = _area_cache.get(key)
    if hit and time.time() - hit[0] < AREA_TTL:
        return hit[1]
    box, L = _box(area), layers()
    sources, errors = {}, {}
    for k, when in (("hrdps", _hrdps_hour(L.get("hrdps"))), ("caldas", (L.get("caldas") or {}).get("end"))):
        try:
            st = _stats(grid(k, *box, when=when if k == "hrdps" else None), when)
            if st:
                sources[k] = st
        except Exception as e:
            errors[k] = str(e)[:120]
    if not sources:
        raise ValueError("no snow depth from CaLDAS or HRDPS: " + "; ".join(errors.values()))
    st = [x for x in stations if x.get("snow") is not None]
    med = None
    if st:
        vals = sorted(x["snow"] for x in st)
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    if med is not None:
        best = min(sources, key=lambda k: abs(sources[k]["mean_cm"] - med))
        basis = f"closest to the snow depth measured at {len(st)} station{'s' if len(st) > 1 else ''} within 60 km"
    else:
        best = "hrdps" if "hrdps" in sources else "caldas"
        basis = "no station measures snow depth nearby; HRDPS preferred" if best == "hrdps" else "HRDPS unavailable"
    c, h = sources.get("caldas"), sources.get("hrdps")
    disagree = bool(c and h and (abs(c["covered_pct"] - h["covered_pct"]) >= DISAGREE_PCT
                                 or abs(c["mean_cm"] - h["mean_cm"]) >= DISAGREE_CM))
    out = {**sources[best], "source": best, "sources": sources, "errors": errors or None, "stations": st,
           "station_median_cm": med, "disagree": disagree, "basis": basis, "cover_cm": COVER_CM,
           "analysis": sources[best]["time"]}
    out.update(_history(key, out))
    _area_cache[key] = (time.time(), out)
    return out


def _history(key, cur):
    """Record today's sample for the area; return snow_free_since (date) and trend_cm_3d from the samples."""
    if not HISTORY_FILE:
        return {}
    today = datetime.now(timezone.utc).date().isoformat()
    with _hist_lock:
        try:
            h = json.loads(Path(HISTORY_FILE).read_text())
        except Exception:
            h = {}
        days = h.setdefault(key, {})
        days[today] = [cur["mean_cm"], cur["covered_pct"]]
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=SNOW_HISTORY_DAYS)).isoformat()
        h[key] = {d: x for d, x in sorted(days.items()) if d >= cutoff}
        try:
            Path(HISTORY_FILE).write_text(json.dumps(h, separators=(",", ":")))
        except OSError:
            pass
        series = sorted(h[key].items())
    out = {"snow_free_since": None, "trend_cm_3d": None, "history_days": len(series)}
    # snow-free since: the first day of the current run of days with under a quarter covered
    if cur["covered_pct"] < 25:
        since = today
        for d, (m, c) in reversed(series):
            if c >= 25:
                break
            since = d
        out["snow_free_since"] = since if len(series) > 1 else None
    back = [x for d, x in series if d <= (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()]
    if back:
        out["trend_cm_3d"] = round(cur["mean_cm"] - back[-1][0], 1)
    return out
