"""CWFIS daily fire weather grids (NRCan GeoServer): which layers and dates exist, and values for an area.

The grids are noon local standard time. Past days are interpolated station observations; from today on they
are CWFIS's forecast (CWFIS doesn't label the boundary, so the report infers it from the date).
"""
import io
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import requests

log = logging.getLogger("cwfis")

WMS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wms"
WCS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/wcs"

# Map layers offered in the page (WMS names)
MAP_LAYERS = ["fwi", "isi", "ffmc", "bui", "dmc", "dc", "fdr", "hfi"]
# Grids read for the report (WCS coverage public__<name>)
WEATHER = ("temp", "rh", "ws", "wdir", "precip")
INDICES = ("ffmc", "dmc", "dc", "isi", "bui", "fwi")
REPORT_VARS = WEATHER + INDICES + ("fdr",)
AREA_MAX_VARS = ("fwi", "fdr")          # also reported as the worst cell in the area's box
FDR_LABELS = ["Low", "Moderate", "High", "Very High", "Extreme"]
FALLBACK_M = 5000                        # if the point's cell is no data (water), nearest valid cell within this

_session = requests.Session()
_pool = ThreadPoolExecutor(12, thread_name_prefix="cwfis")
_cache, _lock = {}, threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
        if len(_cache) > 20000:  # keep memory bounded on a small server
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:5000]:
                del _cache[k]
    return val


# ---- what exists ---------------------------------------------------------------------------------------

def layers():
    """[{"id", "title", "start", "end"}] for the map layers, from WMS GetCapabilities (cached an hour)."""
    def fetch():
        r = _session.get(WMS, params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}, timeout=60)
        r.raise_for_status()
        out = []
        for name in MAP_LAYERS:
            m = re.search(rf"<Name>{name}</Name>\s*<Title>([^<]*)</Title>.*?<Dimension name=\"time\"[^>]*>([^/<]+)/([^/<]+)/P1D",
                          r.text, re.S)
            if m:
                out.append({"id": name, "title": m[1].split(" / ")[0].strip(), "start": m[2][:10], "end": m[3][:10]})
        return out
    return _cached("layers", 3600, fetch)


def last_date():
    """Last date of the dated daily series (the end of the forecast), from WCS DescribeCoverage."""
    def fetch():
        r = _session.get(WCS, timeout=60, params={"service": "WCS", "version": "2.0.1",
                                                  "request": "DescribeCoverage", "coverageId": "public__fwi"})
        r.raise_for_status()
        m = re.search(r"<gml:endPosition>(\d{4}-\d\d-\d\d)", r.text)
        return date.fromisoformat(m.group(1)) if m else None
    return _cached("last_date", 3600, fetch)


# ---- values ----------------------------------------------------------------------------------------------

def _to_lcc(lon, lat):
    from pyproj import Transformer
    return Transformer.from_crs(4326, 3978, always_xy=True).transform(lon, lat)


def fetch_grid(var, d, x0, y0, x1, y1):
    """The CWFIS grid for one variable and day over a box in EPSG:3978 metres -> (array, x0, sx, y0, sy) or None."""
    import tifffile
    logging.getLogger("tifffile").setLevel(logging.ERROR)
    r = _session.get(WCS, timeout=60, params=[
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", f"public__{var}"), ("format", "image/geotiff"),
        ("subset", f"E({x0},{x1})"), ("subset", f"N({y0},{y1})"),
        ("subset", f'time("{d.isoformat()}T00:00:00.000Z")')])
    if r.status_code != 200 or r.content[:2] not in (b"II", b"MM"):
        return None
    with tifffile.TiffFile(io.BytesIO(r.content)) as t:
        page = t.pages[0]
        a = page.asarray().astype(float)
        tags = page.tags
        if "ModelTransformationTag" in tags:
            m = tags["ModelTransformationTag"].value
            gx0, sx, gy0, sy = m[3], m[0], m[7], m[5]
        else:
            scale, tie = tags["ModelPixelScaleTag"].value, tags["ModelTiepointTag"].value
            gx0, sx, gy0, sy = tie[3], scale[0], tie[4], -scale[1]
    a[~(np.isfinite(a) & (a > -1e5))] = np.nan
    return a, gx0, sx, gy0, sy


def summarize(grid, x, y, box=None):
    """(point value, km to the cell used, max over the box or None) from fetch_grid's result.

    The point takes the cell containing it, or the nearest valid cell within FALLBACK_M.
    GeoServer may return a larger extent than asked for, so the box max uses cell centres inside the box.
    """
    a, gx0, sx, gy0, sy = grid
    cx = gx0 + (np.arange(a.shape[1]) + 0.5) * sx
    cy = gy0 + (np.arange(a.shape[0]) + 0.5) * sy
    valid = np.isfinite(a)
    row = min(max(int((y - gy0) // sy), 0), a.shape[0] - 1)
    col = min(max(int((x - gx0) // sx), 0), a.shape[1] - 1)
    if valid[row, col]:
        v, km = float(a[row, col]), 0.0
    else:
        dist = np.where(valid, np.hypot(cx[None, :] - x, cy[:, None] - y), np.inf)
        k = np.unravel_index(np.argmin(dist), a.shape)
        if not np.isfinite(dist[k]) or dist[k] > FALLBACK_M:
            v, km = None, None
        else:
            v, km = float(a[k]), float(dist[k]) / 1000
    mx = None
    if box is not None:
        bx0, by0, bx1, by1 = box
        inside = valid & ((cx >= bx0) & (cx <= bx1))[None, :] & ((cy >= by0) & (cy <= by1))[:, None]
        if inside.any():
            mx = float(np.max(a[inside]))
    return v, km, mx


def area_daily(area, first, last):
    """Daily rows for an area: [{"date", var: value, ..., "fwi_max", "fdr_max", "cell_km"}].

    Values are at the area's reference point; *_max are the worst cell in its bounding box (if it has one).
    """
    x, y = _to_lcc(area["lon"], area["lat"])
    box = None
    if area.get("bbox"):
        w, s, e, n = area["bbox"]
        corners = [_to_lcc(lo, la) for lo, la in ((w, s), (w, n), (e, s), (e, n))]
        box = (min(c[0] for c in corners), min(c[1] for c in corners),
               max(c[0] for c in corners), max(c[1] for c in corners))
    pad = FALLBACK_M + 2000
    ext = (min(x, box[0] if box else x) - pad, min(y, box[1] if box else y) - pad,
           max(x, box[2] if box else x) + pad, max(y, box[3] if box else y) + pad)
    ext = tuple(round(v) for v in ext)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    jobs = [(v, d) for d in days for v in REPORT_VARS]

    def one(job):
        var, d = job
        def get():
            g = fetch_grid(var, d, *ext)
            return summarize(g, x, y, box) if g is not None else (None, None, None)
        return _cached(f"{area['id']}:{ext}:{var}:{d}", 3 * 3600, get)

    rows = {d: {"date": d.isoformat()} for d in days}
    for (var, d), (v, km, mx) in zip(jobs, _pool.map(one, jobs)):
        row = rows[d]
        if var == "wdir" and v is not None:
            v %= 360  # CWFIS stores -180..180; direction the wind blows from
        row[var] = None if v is None else round(v, 1)
        if var == "fwi":
            row["cell_km"] = km
        if var in AREA_MAX_VARS and box is not None:
            row[f"{var}_max"] = None if mx is None else round(mx, 1)
    out = []
    for d in days:
        row = rows[d]
        if row.get("fwi") is None and row.get("temp") is None:
            continue
        for k in ("fdr", "fdr_max"):
            val = row.get(k)
            row[k + "_label"] = FDR_LABELS[int(val)] if val is not None and 0 <= val <= 4 else None
        out.append(row)
    return out
