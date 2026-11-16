"""RDPS 10 m wind for the next 48 hours: GRIB2 from the MSC Datamart, regridded for the map's wind overlay.

A background thread looks for the newest RDPS run every few minutes. Once a run's files reach the last hour
we want, it downloads the 10 m wind speed and direction GRIB2 for each hour from about now to now + 48 h,
regrids them onto a Web Mercator raster over Canada and keeps them as small gzipped byte frames:

    data/wind/<run>/<var>/<hhh>.u8.gz     (run = YYYYMMDDHH UTC, hhh = forecast hour)

The browser draws particles or arrows from these frames (static/windlayer.js). Only the newest complete run is
kept; the previous one is served until the next has fully arrived.

RDPS has no 2 m wind; 10 m is the standard height for fire weather (FWI System, FBP) and the CWFIS wind.
Speed and direction come as their own fields (earth-relative, direction the wind blows from), so the rotated
grid's U/V components never need rotating.
"""
import gzip
import json
import logging
import math
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

log = logging.getLogger("wind")

DATAMART = "https://dd.weather.gc.ca"
RUN_HOURS = (0, 6, 12, 18)
MAX_FH = 84                                   # RDPS forecast length
AHEAD_H = int(os.environ.get("FW_WIND_HOURS", "48"))  # how far past "now" to cover
BEHIND_H = 2                                  # also keep a couple of hours before now
POLL_MIN = 10
WORKERS = 4
TOKENS = {"wspd": "WindSpeed_AGL-10m", "wdir": "WindDir_AGL-10m"}

# Display raster: Web Mercator over southern and central Canada (all the areas, with room to pan)
BOUNDS = {"west": -142.0, "east": -50.0, "south": 40.0, "north": 72.0}
WIDTH = 1000  # ~0.09° per pixel, about the RDPS cell size
# Frames are uint8: 0 = no data, else round((v - offset) * scale) + 1
VARIABLES = {
    "wspd": {"label": "Wind speed (10 m)", "units": "km/h", "offset": 0.0, "scale": 1.0},
    "wdir": {"label": "Wind direction (10 m)", "units": "°", "offset": 0.0, "scale": 0.5},
}

_session = requests.Session()


def _merc_y(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def raster_shape():
    b = BOUNDS
    dx = math.radians(b["east"] - b["west"]) / WIDTH
    return int(round((_merc_y(b["north"]) - _merc_y(b["south"])) / dx)), WIDTH


def pixel_latlon():
    """Flat (lat, lon) of the display-pixel centres, row by row from the north."""
    b = BOUNDS
    h, w = raster_shape()
    lons = b["west"] + (np.arange(w) + 0.5) * (b["east"] - b["west"]) / w
    ys = _merc_y(b["north"]) - (np.arange(h) + 0.5) * (_merc_y(b["north"]) - _merc_y(b["south"])) / h
    lats = np.degrees(2 * np.arctan(np.exp(ys)) - np.pi / 2)
    glon, glat = np.meshgrid(lons, lats)
    return glat.ravel(), glon.ravel()


# ---- the rotated lat-lon grid -> display raster mapping --------------------------------------------------

def _to_rotated(lat, lon, lat_sp, lon_sp):
    """Geographic -> rotated coordinates for a GRIB rotated_ll grid whose south pole is (lat_sp, lon_sp)."""
    la, lo = np.radians(lat), np.radians(lon - lon_sp)
    x, y, z = np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)
    t = np.radians(90 + lat_sp)
    xr = np.cos(t) * x + np.sin(t) * z
    zr = -np.sin(t) * x + np.cos(t) * z
    return np.degrees(np.arcsin(np.clip(zr, -1, 1))), np.degrees(np.arctan2(y, xr))


_map_lock = threading.Lock()
_maps = {}


def grid_map(h):
    """For each display pixel, the index of the nearest RDPS cell in the GRIB message (-1 outside the domain).

    Worked out from the grid definition, then checked against ecCodes' own cell coordinates. Cached per grid.
    """
    import eccodes
    g = lambda k: eccodes.codes_get(h, k)
    if g("gridType") != "rotated_ll":
        raise ValueError(f"unexpected grid type {g('gridType')}")
    key = tuple(g(k) for k in ("Ni", "Nj", "latitudeOfSouthernPoleInDegrees", "longitudeOfSouthernPoleInDegrees",
                               "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                               "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees",
                               "iScansNegatively", "jScansPositively", "angleOfRotationInDegrees"))
    with _map_lock:
        if key in _maps:
            return _maps[key]
        ni, nj, lat_sp, lon_sp, lat1, lon1, di, dj, ineg, jpos, rot = key
        if rot:
            raise ValueError("rotated grids with an angle of rotation aren't handled")
        if ineg:
            di = -di
        if not jpos:
            dj = -dj
        plat, plon = pixel_latlon()
        rlat, rlon = _to_rotated(plat, plon, lat_sp, lon_sp)
        i = np.rint(((rlon - lon1) * np.sign(di)) % 360 / abs(di))  # columns can wrap past 180°
        j = np.rint((rlat - lat1) / dj)
        inside = (i >= 0) & (i < ni) & (j >= 0) & (j < nj)
        idx = np.where(inside, j * ni + i, -1).astype(np.int64)
        # check against ecCodes: the cell picked for each pixel must be within ~a cell of the pixel
        lat = eccodes.codes_get_array(h, "latitudes")
        lon = eccodes.codes_get_array(h, "longitudes")
        k = np.flatnonzero(idx >= 0)[::97]
        if not k.size:
            raise ValueError("the display area is outside the RDPS grid")
        dlat = np.abs(lat[idx[k]] - plat[k])
        dlon = np.abs(((lon[idx[k]] - plon[k]) + 180) % 360 - 180) * np.cos(np.radians(plat[k]))
        worst = float(np.max(np.hypot(dlat, dlon)))
        if worst > 1.5 * abs(dj):
            raise ValueError(f"grid mapping check failed: {worst:.3f}° off")
        log.info("RDPS grid %dx%d mapped onto a %dx%d display raster (worst %.3f°)", ni, nj, *raster_shape()[::-1], worst)
        _maps[key] = idx
        return idx


def grib_to_frame(content, var):
    """GRIB2 bytes -> gzipped uint8 display frame."""
    import eccodes
    h = eccodes.codes_new_from_message(content)
    try:
        idx = grid_map(h)
        vals = eccodes.codes_get_values(h).astype(np.float64)
        if eccodes.codes_get(h, "bitmapPresent"):
            vals[vals == eccodes.codes_get(h, "missingValue")] = np.nan
    finally:
        eccodes.codes_release(h)
    v = np.where(idx >= 0, vals[np.maximum(idx, 0)], np.nan)
    if var == "wspd":
        v = v * 3.6  # m/s -> km/h
    else:
        v = v % 360
    spec = VARIABLES[var]
    q = np.rint((v - spec["offset"]) * spec["scale"])
    if var == "wdir":
        q %= 180  # 360° -> 0°
    q = np.clip(np.nan_to_num(q, nan=0), 0, 254) + 1
    q[np.isnan(v)] = 0
    return gzip.compress(q.astype(np.uint8).tobytes(), 6)


# ---- runs on the Datamart --------------------------------------------------------------------------------

def run_time(run):
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def url(run, var, fh):
    d, hh = run[:8], run[8:]
    return (f"{DATAMART}/{d}/WXO-DD/model_rdps/10km/{hh}/{fh:03d}/"
            f"{d}T{hh}Z_MSC_RDPS_{TOKENS[var]}_RLatLon0.09_PT{fh:03d}H.grib2")


def hours_for(run, now=None):
    """Forecast hours of `run` from a little before now to now + AHEAD_H (as far as the run goes)."""
    now = now or datetime.now(timezone.utc)
    since = (now - run_time(run)).total_seconds() / 3600
    first = max(0, math.floor(since) - BEHIND_H)
    last = min(MAX_FH, math.ceil(since) + AHEAD_H)
    return list(range(first, last + 1))


def recent_runs(now=None, n=5):
    now = now or datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    return [(t - timedelta(hours=6 * k)).strftime("%Y%m%d%H") for k in range(n)]


def _exists(u):
    try:
        return _session.head(u, timeout=20).status_code == 200
    except requests.RequestException:
        return False


class Wind:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "wind"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.status = "starting"
        self.run, self.hours = None, []
        for d in sorted(self.dir.iterdir(), reverse=True):  # newest complete run already on disk
            m = d / "manifest.json"
            if d.is_dir() and m.exists():
                try:
                    doc = json.loads(m.read_text())
                    self.run, self.hours = doc["run"], doc["hours"]
                    self.status = "ok"
                    break
                except (ValueError, KeyError):
                    pass
        self._prune()

    def path(self, run, var, fh):
        return self.dir / run / var / f"{fh:03d}.u8.gz"

    def meta(self):
        h, w = raster_shape()
        with self._lock:
            run, hours, status = self.run, list(self.hours), self.status
        out = {"run": run, "status": status, "bounds": BOUNDS, "width": w, "height": h, "variables": VARIABLES,
               "source": "ECCC RDPS 10 km, 10 m wind", "hours": []}
        if run:
            t0 = run_time(run)
            out["hours"] = [{"fh": fh, "valid": (t0 + timedelta(hours=fh)).strftime("%Y-%m-%dT%H:%MZ")} for fh in hours]
        return out

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.update()
            except Exception:
                log.exception("wind update failed")
                with self._lock:
                    self.status = "error" if not self.run else self.status
            stop.wait(POLL_MIN * 60)

    def update(self):
        """Fetch the newest run whose files reach the hours we want, if it's newer than the one we have."""
        now = datetime.now(timezone.utc)
        for run in recent_runs(now):
            if self.run and run <= self.run:
                return  # nothing newer is complete yet
            hours = hours_for(run, now)
            if _exists(url(run, "wspd", hours[-1])) and _exists(url(run, "wdir", hours[-1])):
                break
        else:
            log.info("no recent RDPS run is complete on the Datamart yet")
            return
        log.info("downloading RDPS %sZ 10 m wind, hours %d–%d", run, hours[0], hours[-1])
        part = self.dir / f"{run}.part"
        shutil.rmtree(part, ignore_errors=True)
        for v in TOKENS:
            (part / v).mkdir(parents=True)

        def one(job):
            var, fh = job
            for attempt in range(3):
                try:
                    r = _session.get(url(run, var, fh), timeout=120)
                    if r.status_code == 404:
                        return False
                    r.raise_for_status()
                    (part / var / f"{fh:03d}.u8.gz").write_bytes(grib_to_frame(r.content, var))
                    return True
                except requests.RequestException as e:
                    log.warning("%s %s %03d: %s", run, var, fh, e)
                    time.sleep(5 * (attempt + 1))
            return False

        t = time.time()
        jobs = [(v, fh) for fh in hours for v in TOKENS]
        with ThreadPoolExecutor(WORKERS, thread_name_prefix="wind") as pool:
            ok = dict(zip(jobs, pool.map(one, jobs)))
        got = [fh for fh in hours if all(ok[(v, fh)] for v in TOKENS)]
        if len(got) < len(hours) * 0.9:
            log.warning("RDPS %s: only %d of %d hours arrived; keeping the previous run", run, len(got), len(hours))
            shutil.rmtree(part, ignore_errors=True)
            return
        (part / "manifest.json").write_text(json.dumps({"run": run, "hours": got}))
        final = self.dir / run
        shutil.rmtree(final, ignore_errors=True)
        part.rename(final)
        with self._lock:
            self.run, self.hours, self.status = run, got, "ok"
        log.info("RDPS %sZ wind ready: %d hours in %.0f s", run, len(got), time.time() - t)
        self._prune()

    def _prune(self):
        for d in self.dir.iterdir():
            if d.name != self.run:
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
