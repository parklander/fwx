"""Smoke forecasts for the map: ECCC FireWork (RAQDPS-FW) and BlueSky Canada (firesmoke.ca).

FireWork comes ready-made as a time-enabled GeoMet WMS layer, like the snow layers: the page only needs the
layer name and its current time window. The exact surface PM2.5 layer name isn't documented on the product
page, so the first of CANDIDATES that GeoMet lists is used.
https://eccc-msc.github.io/open-data/msc-data/nwp_raqdps-fw/readme_raqdps-fw_en/
Data Source: Environment and Climate Change Canada.

BlueSky Canada publishes its current run as a netCDF file (https://firesmoke.ca/data):
    https://firesmoke.ca/forecasts/current/dispersion.nc
A thread checks it every 30 min. When it changes, the file is downloaded, each hour of surface PM2.5 is
drawn as a transparent PNG on a Web Mercator grid (Leaflet places it with imageOverlay), and the netCDF is
deleted again to keep the disk small:
    data/bluesky/<run>/<i>.png  +  manifest.json
The file follows the Models-3 IOAPI layout (PM25[TSTEP, LAY, ROW, COL], TFLAG, XORIG/YORIG/XCELL/YCELL,
rows south to north). A plain lat/lon netCDF is read too. BlueSky forecasts run April to September; out of
season there may be no current file. (c) BlueSky Canada.
"""
import json
import logging
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

import snowdepth  # its GeoMet capabilities helpers

log = logging.getLogger("smoke")
_session = requests.Session()
_session.headers["User-Agent"] = "fwx (fire weather for CAF ranges)"

# ---- FireWork (GeoMet WMS) ---------------------------------------------------------------------------
WMS = snowdepth.WMS
CANDIDATES = [  # surface PM2.5 first; the column smoke total as a last resort
    ("RAQDPS-FW.SFC_PM2.5", "Surface PM2.5"),
    ("RAQDPS-FW.SFC_PM2.5-DIFF", "Surface PM2.5 from wildfires"),
    ("RAQDPS.SFC_PM2.5-WildfireSmokePlume", "Surface PM2.5 from wildfire smoke"),
    ("RAQDPS-FW.EATM_PM2.5-DIFF", "Wildfire PM2.5, whole air column"),
]
FW_STALE_H = 30  # runs are 00 and 12 UTC
_fw_lock = threading.Lock()
_fw = {"at": 0.0, "spec": None}


def firework(max_age=600):
    """{"layer", "title", "start", "end", "step", "run", "stale"} for the first FireWork layer GeoMet lists."""
    with _fw_lock:
        if time.time() - _fw["at"] < max_age and _fw["spec"]:
            return _fw["spec"]
    spec = None
    for name, title in CANDIDATES:
        try:
            r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities",
                                          "layer": name}, timeout=30)
            r.raise_for_status()
            if f"<Name>{name}</Name>" not in r.text:  # GeoMet answers an unknown layer with some other layer
                continue
            t, ref = snowdepth._dim(r.text, "time"), snowdepth._dim(r.text, "reference_time")
            if not t:
                continue
            run = ref[1] if ref else None
            stale = bool(run) and datetime.now(timezone.utc) - snowdepth._t(run) > timedelta(hours=FW_STALE_H)
            spec = {"layer": name, "title": title, "start": t[0], "end": t[1], "step": t[2], "run": run, "stale": stale}
            break
        except Exception as e:
            log.warning("FireWork layer %s: %s", name, e)
    with _fw_lock:
        if spec:
            _fw.update(at=time.time(), spec=spec)
        return _fw["spec"]


# ---- BlueSky Canada (netCDF -> PNG frames) ---------------------------------------------------------------
BS_URL = "https://firesmoke.ca/forecasts/current/dispersion.nc"
BS_EVERY_MIN = 30
BS_MAX_BYTES = 3e9
BS_MAX_W = 1600          # widest frame, px
BS_STALE_H = 36
# PM2.5 colour bins, µg/m³ (lower edge, colour, opacity); below the first bin is clear
PM_BINS = [(1, "#d6e4f0", 0.30), (5, "#a9c9e2", 0.45), (10, "#86b3d6", 0.55), (20, "#c7b27a", 0.65),
           (40, "#d9953f", 0.72), (60, "#d8622c", 0.78), (100, "#b52d2a", 0.84), (200, "#7a1a52", 0.9),
           (500, "#3e0a3e", 0.95)]


def _merc(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _lut():
    lut = np.zeros((len(PM_BINS) + 1, 4), dtype=np.uint8)
    for i, (_, col, a) in enumerate(PM_BINS):
        lut[i + 1] = [int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16), round(a * 255)]
    return lut


def _ioapi_time(yyyyddd, hhmmss):
    d = datetime.strptime(f"{int(yyyyddd):07d}", "%Y%j").replace(tzinfo=timezone.utc)
    h = int(hhmmss)
    return d + timedelta(hours=h // 10000, minutes=(h // 100) % 100, seconds=h % 100)


def read_grid(nc):
    """(var, times[], lat_south_to_north[], lon[]) from an IOAPI or plain lat/lon netCDF."""
    names = {k.lower(): k for k in nc.variables}
    var = nc.variables[names.get("pm25") or names.get("pm2.5") or names.get("pm2_5")]
    if hasattr(nc, "XORIG"):  # IOAPI
        x0, y0, dx, dy = float(nc.XORIG), float(nc.YORIG), float(nc.XCELL), float(nc.YCELL)
        ncol, nrow = int(nc.NCOLS), int(nc.NROWS)
        lon = x0 + dx * (np.arange(ncol) + 0.5)
        lat = y0 + dy * (np.arange(nrow) + 0.5)
        if "TFLAG" in nc.variables:
            tf = nc.variables["TFLAG"][:, 0, :]
            times = [_ioapi_time(a, b) for a, b in tf]
        else:
            t0 = _ioapi_time(nc.SDATE, nc.STIME)
            step = int(nc.TSTEP)
            dt = timedelta(hours=step // 10000, minutes=(step // 100) % 100)
            times = [t0 + dt * i for i in range(var.shape[0])]
        return var, times, lat, lon
    lat = np.asarray(nc.variables[names.get("lat") or names["latitude"]][:], dtype=float)
    lon = np.asarray(nc.variables[names.get("lon") or names["longitude"]][:], dtype=float)
    tv = nc.variables[names["time"]]
    import netCDF4
    times = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
             for t in netCDF4.num2date(tv[:], tv.units, only_use_cftime_datetimes=False)]
    return var, times, lat, lon


def render_frames(path, out_dir):
    """Write one PNG per time step; return the manifest (times, bounds, size)."""
    import netCDF4
    from PIL import Image
    out_dir.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path) as nc:
        var, times, lat, lon = read_grid(nc)
        flip = lat[0] > lat[-1]  # make rows south -> north
        if flip:
            lat = lat[::-1]
        dlon, dlat = lon[1] - lon[0], lat[1] - lat[0]
        west, east = float(lon[0] - dlon / 2), float(lon[-1] + dlon / 2)
        south, north = float(lat[0] - dlat / 2), float(min(lat[-1] + dlat / 2, 84.0))
        stride = max(1, math.ceil(len(lon) / BS_MAX_W))
        cols = np.arange(0, len(lon), stride)
        w = len(cols)
        h = max(1, round(w * (_merc(north) - _merc(south)) / math.radians(east - west)))
        # each output row (north at the top, evenly spaced in Mercator) -> nearest source row
        ys = np.linspace(_merc(north), _merc(south), h, endpoint=False) - (_merc(north) - _merc(south)) / h / 2
        row_lat = np.degrees(2 * np.arctan(np.exp(ys)) - np.pi / 2)
        rows = np.clip(np.round((row_lat - lat[0]) / dlat).astype(int), 0, len(lat) - 1)
        edges = np.array([b[0] for b in PM_BINS], dtype=float)
        lut = _lut()
        for i in range(len(times)):
            a = var[i]
            a = np.asarray(a[0] if a.ndim == 3 else a, dtype=np.float32)  # first (surface) layer
            if flip:
                a = a[::-1]
            a = np.nan_to_num(np.ma.filled(a, 0), nan=0.0)[rows][:, cols]
            Image.fromarray(lut[np.searchsorted(edges, a, side="right")], "RGBA").save(out_dir / f"{i}.png", optimize=True)
    return {"times": [t.strftime("%Y-%m-%dT%H:%MZ") for t in times],
            "bounds": {"west": west, "east": east, "south": south, "north": north}, "width": w, "height": h}


class BlueSky:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "bluesky"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.doc, self.status, self.error, self.checked = None, "starting", None, None
        for d in sorted(self.dir.iterdir(), reverse=True):
            m = d / "manifest.json"
            if d.is_dir() and m.exists():
                try:
                    self.doc, self.status = json.loads(m.read_text()), "ok"
                    break
                except ValueError:
                    pass

    def meta(self):
        with self._lock:
            doc = dict(self.doc) if self.doc else None
            out = {"status": self.status, "error": self.error, "checked": self.checked, "source": BS_URL}
        if doc:
            last = datetime.fromisoformat(doc["times"][-1].replace("Z", "+00:00"))
            first = datetime.fromisoformat(doc["times"][0].replace("Z", "+00:00"))
            doc["stale"] = datetime.now(timezone.utc) - first > timedelta(hours=BS_STALE_H)
            doc["ended"] = last < datetime.now(timezone.utc)
            out.update(doc)
        return out

    def path(self, run, i):
        return self.dir / run / f"{i}.png"

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.update()
            except Exception as e:
                log.exception("BlueSky update failed")
                with self._lock:
                    self.status, self.error = ("error" if not self.doc else self.status), str(e)[:200]
            stop.wait(BS_EVERY_MIN * 60)

    def update(self):
        self.checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = self.dir / "download.nc"
        got = 0
        # one streamed GET: its headers say whether the run changed, and only then is the body read
        with _session.get(BS_URL, timeout=300, stream=True) as g:
            if g.status_code == 404:
                with self._lock:
                    self.status = "none" if not self.doc else self.status
                    self.error = "No current BlueSky forecast (they run April to September)"
                return
            g.raise_for_status()
            stamp = g.headers.get("Last-Modified") or g.headers.get("ETag") or ""
            with self._lock:
                if self.doc and stamp and self.doc.get("stamp") == stamp:
                    self.error = None
                    return
            with tmp.open("wb") as f:
                for chunk in g.iter_content(1 << 20):
                    got += len(chunk)
                    if got > BS_MAX_BYTES:
                        raise ValueError("dispersion.nc is larger than expected; not downloaded")
                    f.write(chunk)
        try:
            run = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            out = self.dir / run
            doc = render_frames(tmp, out)
            doc.update(run=run, stamp=stamp, mb=round(got / 1e6, 1))
            (out / "manifest.json").write_text(json.dumps(doc))
            log.info("BlueSky: %d hours from %s, %.0f MB", len(doc["times"]), doc["times"][0], got / 1e6)
        finally:
            tmp.unlink(missing_ok=True)
        with self._lock:
            self.doc, self.status, self.error = doc, "ok", None
        for d in self.dir.iterdir():  # keep only the newest run
            if d.is_dir() and d.name != run:
                shutil.rmtree(d, ignore_errors=True)
