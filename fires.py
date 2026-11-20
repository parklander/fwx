"""Fire detections near each area: NASA FIRMS satellite hotspots and the fires agencies report to CWFIS.

FIRMS (https://firms.modaps.eosdis.nasa.gov/api/area/) is read every 15 min for all of Canada, one request per
satellite. Its NRT sources also carry the real-time and ultra real-time detections over Canada, so a hotspot
can show up within minutes of the pass; those are replaced by the NRT version within about 6 h. Needs a free
MAP_KEY in the FIRMS_MAP_KEY environment variable; without one the FIRMS part is simply off.

CWFIS reported fires (the fires provincial and territorial agencies are managing) come from the CWFIF
GeoServer WFS every hour, with the older CWFIS GeoServer as a fallback. No key needed.

A hotspot is a satellite pixel with a strong heat signal: a wildfire, a training-range fire, a prescribed burn,
a flare stack. It is not a confirmed fire.
"""
import csv
import io
import json
import logging
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("fires")

FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{box}/{days}"
FIRMS_SOURCES = {  # source -> short label
    "VIIRS_NOAA20_NRT": "VIIRS NOAA-20",
    "VIIRS_NOAA21_NRT": "VIIRS NOAA-21",
    "VIIRS_SNPP_NRT": "VIIRS S-NPP",
    "MODIS_NRT": "MODIS",
}
CANADA = (-141.1, 41.6, -52.5, 83.2)  # w, s, e, n
FIRMS_DAYS = 2        # today and yesterday (UTC); each day is one transaction of the key's 5000 per 10 min
FIRMS_EVERY_MIN = 15
KEEP_HOURS = 48

REPORTED_WFS = [  # (WFS endpoint, layer) tried in order
    ("https://geoserver.cwfif.nrcan.gc.ca/geoserver/wfs", "public:cwfif_national_activefires"),
    ("https://cwfis.cfs.nrcan.gc.ca/geoserver/wfs", "public:activefires_current"),
]
REPORTED_EVERY_MIN = 60
NEAR_KM = 25  # "near" an area: inside its box, or within this distance of it

_session = requests.Session()
_session.headers["User-Agent"] = "fwx (fire weather for CAF ranges)"


# ---- geometry ----------------------------------------------------------------------------------------

def km_between(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1.0, a)))


def km_to_area(area, lat, lon):
    """0 inside the area's box, else the distance to the box's nearest edge (or to its point if it has no box)."""
    box = area.get("bbox")
    if box:
        w, s, e, n = box
        if w <= lon <= e and s <= lat <= n:
            return 0.0
        return km_between(lat, lon, min(max(lat, s), n), min(max(lon, w), e))
    return km_between(lat, lon, area["lat"], area["lon"])


# ---- FIRMS --------------------------------------------------------------------------------------------

def _conf(row):
    """'high' / 'nominal' / 'low' from VIIRS letters or MODIS 0-100."""
    c = (row.get("confidence") or "").strip().lower()
    if c in ("h", "high"):
        return "high"
    if c in ("n", "nominal"):
        return "nominal"
    if c in ("l", "low"):
        return "low"
    try:
        v = float(c)
    except ValueError:
        return None
    return "high" if v >= 80 else "nominal" if v >= 30 else "low"


def parse_firms(text, label):
    """Detections from a FIRMS area CSV: [{lat, lon, time (UTC ISO), sat, conf, frp, day}]."""
    t = text.lstrip()
    if not t or not t.startswith("latitude"):
        # FIRMS answers errors (bad key, limit reached) as plain text with status 200
        raise ValueError(t[:200] or "empty response")
    out = []
    for r in csv.DictReader(io.StringIO(t)):
        try:
            hhmm = (r.get("acq_time") or "0").zfill(4)
            when = datetime.strptime(f"{r['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
            frp = r.get("frp")
            out.append({"lat": round(float(r["latitude"]), 4), "lon": round(float(r["longitude"]), 4),
                        "time": when.isoformat(timespec="minutes"), "sat": label, "conf": _conf(r),
                        "frp": round(float(frp), 1) if frp not in (None, "") else None,
                        "day": (r.get("daynight") or "").upper() != "N"})
        except (KeyError, ValueError):
            continue
    return out


# ---- CWFIS reported fires -------------------------------------------------------------------------------

def _pick(props, *names):
    low = {k.lower(): v for k, v in props.items()}
    for n in names:
        v = low.get(n)
        if v not in (None, ""):
            return v
    return None


STAGE = {"OC": "Out of control", "BH": "Being held", "UC": "Under control", "OUT": "Out",
         "MOD": "Modified response", "MON": "Monitored", "FULL": "Full response"}


def parse_reported(geojson):
    out = []
    for f in geojson.get("features", []):
        g, p = f.get("geometry") or {}, f.get("properties") or {}
        lat, lon = _pick(p, "lat", "latitude"), _pick(p, "lon", "lng", "longitude")
        if (lat is None or lon is None) and g.get("type") == "Point":
            x, y = g["coordinates"][:2]
            lon, lat = (y, x) if abs(x) <= 90 and abs(y) > 90 else (x, y)  # tolerate lat/lon axis order
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        stage = _pick(p, "stage_of_control", "stage", "soc", "status")
        size = _pick(p, "hectares", "size_ha", "fire_size", "area_ha", "size")
        try:
            size = round(float(size), 1)
        except (TypeError, ValueError):
            size = None
        out.append({"lat": round(lat, 4), "lon": round(lon, 4),
                    "name": str(_pick(p, "firename", "fire_name", "name", "fire_id", "id") or "Reported fire"),
                    "agency": (str(_pick(p, "agency", "agency_code", "jurisdiction") or "")).upper() or None,
                    "stage": STAGE.get(str(stage).upper(), stage) if stage else None,
                    "ha": size, "start": _pick(p, "startdate", "start_date", "discovery_date", "date")})
    return out


# ---- the poller ------------------------------------------------------------------------------------------

class Fires:
    def __init__(self, aoi_file, data_dir, on_new=None):
        self.aoi_file, self.data_dir, self.on_new = Path(aoi_file), Path(data_dir), on_new
        self.key = os.environ.get("FIRMS_MAP_KEY", "").strip()
        self.lock = threading.Lock()
        self.hotspots, self.reported = [], []
        self.status = {"firms": {"enabled": bool(self.key), "checked": None, "error": None, "sources": {}},
                       "reported": {"checked": None, "error": None, "source": None}}
        self.seen_inside = set()  # (area, lat, lon, time) already announced
        self._first = True

    # FIRMS
    def poll_firms(self):
        if not self.key:
            return
        box = ",".join(str(v) for v in CANADA)
        got, errors = [], {}
        for src, label in FIRMS_SOURCES.items():
            try:
                r = _session.get(FIRMS_URL.format(key=self.key, source=src, box=box, days=FIRMS_DAYS), timeout=60)
                r.raise_for_status()
                rows = parse_firms(r.text, label)
                got += rows
                errors[src] = None
                log.info("FIRMS %s: %d detections", src, len(rows))
            except Exception as e:
                log.warning("FIRMS %s failed: %s", src, e)
                errors[src] = str(e)[:200]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)).isoformat(timespec="minutes")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            if any(v is None for v in errors.values()):  # keep the last good set if every source failed
                ok = {FIRMS_SOURCES[s] for s, v in errors.items() if v is None}
                keep = [h for h in self.hotspots if h["sat"] not in ok]  # failed sources: hold their last points
                self.hotspots = sorted([h for h in got + keep if h["time"] >= cutoff], key=lambda h: h["time"])
                self.status["firms"]["checked"] = now
            self.status["firms"]["sources"] = errors
            self.status["firms"]["error"] = None if any(v is None for v in errors.values()) else "FIRMS isn't answering"
        self._announce()

    # CWFIS
    def poll_reported(self):
        last = None
        for url, layer in REPORTED_WFS:
            try:
                r = _session.get(url, timeout=60, params={"service": "WFS", "version": "1.1.0", "request": "GetFeature",
                                                          "typeName": layer, "outputFormat": "application/json",
                                                          "srsName": "EPSG:4326"})
                r.raise_for_status()
                rows = parse_reported(r.json())
                with self.lock:
                    self.reported = rows
                    self.status["reported"] = {"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                               "error": None, "source": layer}
                log.info("CWFIS reported fires (%s): %d", layer, len(rows))
                return
            except Exception as e:
                last = e
                log.warning("CWFIS reported fires from %s failed: %s", url, e)
        with self.lock:
            self.status["reported"]["error"] = f"CWFIS isn't answering ({str(last)[:120]})"

    # new hotspots inside an area's box -> on_new(area, summary), once per detection
    def _announce(self):
        areas = self._areas()
        new = {}
        with self.lock:
            for h in self.hotspots:
                for a in areas:
                    if a.get("bbox") and km_to_area(a, h["lat"], h["lon"]) == 0:
                        k = (a["id"], h["lat"], h["lon"], h["time"])
                        if k not in self.seen_inside:
                            self.seen_inside.add(k)
                            new.setdefault(a["id"], (a, []))[1].append(h)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)).isoformat(timespec="minutes")
            self.seen_inside = {k for k in self.seen_inside if k[3] >= cutoff}
        if self._first:  # don't announce what was already there when the server started
            self._first = False
            return
        if self.on_new:
            for a, hs in new.values():
                try:
                    self.on_new(a, hs)
                except Exception:
                    log.exception("fire notification for %s failed", a["id"])

    def _areas(self):
        return json.loads(self.aoi_file.read_text(encoding="utf-8"))["areas"]

    def loop(self, stop):
        n = 0
        while not stop.is_set():
            try:
                self.poll_firms()
            except Exception:
                log.exception("FIRMS poll failed")
            if n % max(1, REPORTED_EVERY_MIN // FIRMS_EVERY_MIN) == 0:
                try:
                    self.poll_reported()
                except Exception:
                    log.exception("CWFIS reported fires poll failed")
            n += 1
            stop.wait(FIRMS_EVERY_MIN * 60)

    # ---- views --------------------------------------------------------------------------------------
    def geojson(self, bbox=None, hours=24, limit=20000):
        """Hotspots (newest last) and reported fires as GeoJSON for the map."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="minutes")
        w, s, e, n = bbox or CANADA
        inb = lambda p: w <= p["lon"] <= e and s <= p["lat"] <= n
        with self.lock:
            hs = [h for h in self.hotspots if h["time"] >= cutoff and inb(h)][-limit:]
            rep = [f for f in self.reported if inb(f)]
            st = json.loads(json.dumps(self.status))
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [h["lon"], h["lat"]]},
                  "properties": {"kind": "hotspot", **{k: v for k, v in h.items() if k not in ("lat", "lon")}}} for h in hs]
        feats += [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
                   "properties": {"kind": "reported", **{k: v for k, v in f.items() if k not in ("lat", "lon")}}} for f in rep]
        return {"type": "FeatureCollection", "features": feats, "status": st, "hours": hours}

    def near(self, area, hours=24, km=NEAR_KM):
        """Hotspots and reported fires inside the area or within km of it, nearest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="minutes")
        with self.lock:
            hs, rep = list(self.hotspots), list(self.reported)
            st = json.loads(json.dumps(self.status))
        out_h = []
        for h in hs:
            if h["time"] < cutoff:
                continue
            d = km_to_area(area, h["lat"], h["lon"])
            if d <= km:
                out_h.append(h | {"km": round(d, 1)})
        out_r = []
        for f in rep:
            d = km_to_area(area, f["lat"], f["lon"])
            if d <= max(km, 50):  # reported fires are fewer and bigger: look a bit further out
                out_r.append(f | {"km": round(d, 1)})
        out_h.sort(key=lambda h: (h["km"], h["time"]))
        out_r.sort(key=lambda f: f["km"])
        inside = [h for h in out_h if h["km"] == 0]
        return {"area": area["id"], "hours": hours, "km": km,
                "inside": len(inside), "nearby": len(out_h) - len(inside),
                "latest": max((h["time"] for h in out_h), default=None),
                "hotspots": out_h[:200], "reported": out_r[:50], "status": st}
