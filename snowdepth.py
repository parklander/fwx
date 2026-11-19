"""Snow depth from ECCC GeoMet (WMS), for the map's Snow layers.

- CaLDAS-NSRPS (2.5 km): the land-surface analysis of snow on the ground, every 3 hours, four runs a day.
- HRDPS (2.5 km): forecast snow depth, hourly, about 48 hours ahead; it starts from the CaLDAS analysis
  and adds new snowfall and melt.

GeoMet only answers times inside each layer's window, so the page asks here for the current window and
model run. Product pages: https://eccc-msc.github.io/open-data/msc-data/nwp_caldas-nsrps/readme_caldas-nsrps_en/
and https://eccc-msc.github.io/open-data/msc-data/nwp_hrdps/readme_hrdps_en/ . Data Source: Environment and
Climate Change Canada (ECCC Data Servers End-use Licence).
"""
import logging
import re
import threading
import time
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
