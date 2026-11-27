"""Current weather observations at stations: ECCC (all of Canada) and Manitoba's fire weather network.

ECCC: the SWOB real-time collection of the MSC GeoMet OGC API, which carries MSC's own stations, NAV CANADA
airports and some partner networks. Stations report every minute to every hour; every ECCC_EVERY_MIN
minutes FWX asks for the readings of the last ECCC_WINDOW_MIN minutes (only the fields it uses) and keeps the
newest one per station. The windows overlap, so hourly stations are caught too.
    https://api.weather.gc.ca/collections/swob-realtime

Manitoba: the Manitoba Wildfire Service weather network (its own stations, Manitoba Agriculture's and some MSC
ones), hourly for the last 48 h, plus its station list. FWX keeps the latest reading and the last 24 h per
station. MSC-owned stations in that list are skipped when ECCC already has a station within DUP_KM.
    https://www.gov.mb.ca/conservation_fire/Wx-Display/weatherview/

Each station comes out as {id, name, src, owner, lat, lon, t (UTC ISO), temp, rh, ws, wd, gust, rain1h}.
Wind in km/h, temperature °C, RH %, rain mm. Readings older than KEEP_H are dropped.
"""
import csv
import io
import json
import logging
import math
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("fwx.obs")

ECCC_URL = "https://api.weather.gc.ca/collections/swob-realtime/items"
ECCC_EVERY_MIN = 10
ECCC_WINDOW_MIN = 16
ECCC_PAGE = 2500
ECCC_MAX_PAGES = 10
MB_CSV = "https://www.gov.mb.ca/conservation_fire/Wx-Display/weatherview/data/wx_last48.csv"
MB_STNS = "https://www.gov.mb.ca/conservation_fire/Wx-Display/weatherview/stns_geojson.js"
MB_EVERY_MIN = 15
KEEP_H = 3
DUP_KM = 3.0
STALE_MIN = 75  # older than this the map greys the station out

# SWOB field names differ by station type; the first one present and not null wins
WS = ("avg_wnd_spd_10m_pst10mts", "avg_wnd_spd_10m_pst2mts", "avg_wnd_spd_10m_pst1mt")
WD = ("avg_wnd_dir_10m_pst10mts", "avg_wnd_dir_10m_pst2mts", "avg_wnd_dir_10m_pst1mt", "wnd_dir_10m_pst10mts_max_spd")
GUST = ("max_wnd_gst_spd_10m_pst10mts", "max_wnd_spd_10m_pst10mts", "max_wnd_spd_10m_pst1hr")
FIELDS = ["stn_nam-value", "msc_id-value", "date_tm-value", "data_pvdr-value", "air_temp", "rel_hum",
          "pcpn_amt_pst1hr", *WS, *WD, *GUST]

_session = requests.Session()
_session.headers["User-Agent"] = "FWX fire weather (fwx.rtams.ca)"


def km_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, a)))


def _first(p, keys):
    for k in keys:
        v = p.get(k)
        if isinstance(v, (int, float)):
            return v
    return None


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _r(v, n=1):
    return None if v is None else round(v, n)


# ---- ECCC SWOB ----------------------------------------------------------------------------------------------

def eccc_fetch(minutes=ECCC_WINDOW_MIN):
    """Newest reading per station over the last `minutes`, as station dicts."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    latest, offset = {}, 0
    for _ in range(ECCC_MAX_PAGES):
        r = _session.get(ECCC_URL, timeout=90, params={
            "f": "json", "datetime": f"{since}/..", "limit": ECCC_PAGE, "offset": offset, "properties": ",".join(FIELDS)})
        r.raise_for_status()
        j = r.json()
        feats = j.get("features") or []
        for f in feats:
            p, g = f.get("properties") or {}, f.get("geometry") or {}
            sid = p.get("msc_id-value") or p.get("stn_nam-value")
            t = p.get("date_tm-value")
            c = g.get("coordinates") or []
            if not sid or not t or len(c) < 2:
                continue
            if sid in latest and latest[sid]["t"] >= t:
                continue
            latest[sid] = {
                "id": f"eccc:{sid}", "name": p.get("stn_nam-value") or sid, "src": "ECCC",
                "owner": p.get("data_pvdr-value") or "MSC", "lat": round(c[1], 4), "lon": round(c[0], 4),
                "t": t.replace(".000Z", "Z"), "temp": _r(_num(p.get("air_temp"))), "rh": _r(_num(p.get("rel_hum")), 0),
                "ws": _r(_first(p, WS)), "wd": _r(_first(p, WD), 0), "gust": _r(_first(p, GUST)),
                "rain1h": _r(_num(p.get("pcpn_amt_pst1hr"))),
            }
        offset += len(feats)
        matched = j.get("numberMatched")
        if len(feats) < ECCC_PAGE or (matched is not None and offset >= matched):
            break
    return latest


# ---- Manitoba fire weather network ----------------------------------------------------------------------

def mb_stations():
    """{Stn_ID: {name, owner, region, zone, lat, lon}} from the 'var wxstns = {...};' script."""
    r = _session.get(MB_STNS, timeout=60)
    r.raise_for_status()
    txt = r.text
    doc = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    out = {}
    for f in doc.get("features", []):
        p, c = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
        if p.get("Stn_ID") and len(c) >= 2:
            out[p["Stn_ID"]] = {"name": p.get("Stn_Name") or p["Stn_ID"], "owner": p.get("OWNER") or "",
                                "region": p.get("REGION"), "zone": p.get("I_A_ZONE"), "lat": c[1], "lon": c[0]}
    return out


def mb_readings():
    """{Stn_ID: [rows oldest first]} from the last-48 h CSV; each row {t (UTC ISO), temp, rh, ws, wd, gust, rain1h}."""
    r = _session.get(MB_CSV, timeout=60)
    r.raise_for_status()
    if "html" in (r.headers.get("content-type") or "") or not r.text.startswith("station"):
        raise ValueError("Manitoba returned a page instead of the CSV")
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            t = datetime.fromisoformat(row["datetime"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.setdefault(row.get("station", "").strip(), []).append({
            "t": t.strftime("%Y-%m-%dT%H:%MZ"), "temp": _num(row.get("tmp")), "rh": _num(row.get("rh")),
            "ws": _num(row.get("ws")), "wd": _num(row.get("wd")), "gust": _num(row.get("wsmax")),
            "rain1h": _num(row.get("rn_1"))})
    for rows in out.values():
        rows.sort(key=lambda x: x["t"])
    return out


# ---- the store ------------------------------------------------------------------------------------------

class Obs:
    def __init__(self):
        self.lock = threading.Lock()
        self.eccc, self.mb, self.mb_hist, self.mb_meta = {}, {}, {}, {}
        self.status = {"eccc": {"checked": None, "ok": None, "error": None, "n": 0},
                       "mb": {"checked": None, "ok": None, "error": None, "n": 0}}

    def _stamp(self, key, err=None, n=None):
        s = self.status[key]
        s["checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        s["error"] = err
        if err is None:
            s["ok"] = s["checked"]
            s["n"] = n
        else:
            log.warning("observations %s: %s", key, err)

    def update_eccc(self):
        try:
            got = eccc_fetch()
        except Exception as e:
            with self.lock:
                self._stamp("eccc", str(e)[:200])
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=KEEP_H)).strftime("%Y-%m-%dT%H:%M")
        with self.lock:
            for k, v in got.items():
                if k not in self.eccc or self.eccc[k]["t"] < v["t"]:
                    self.eccc[k] = v
            self.eccc = {k: v for k, v in self.eccc.items() if v["t"] >= cutoff}
            self._stamp("eccc", n=len(self.eccc))
        log.info("ECCC observations: %d new readings, %d stations", len(got), len(self.eccc))

    def update_mb(self):
        try:
            if not self.mb_meta or datetime.now().hour == 3:  # the station list rarely changes
                self.mb_meta = mb_stations() or self.mb_meta
            readings = mb_readings()
        except Exception as e:
            with self.lock:
                self._stamp("mb", str(e)[:200])
            return
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=KEEP_H)).strftime("%Y-%m-%dT%H:%M")
        day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        with self.lock:
            eccc_pts = [(v["lat"], v["lon"]) for v in self.eccc.values()]
        latest, hist = {}, {}
        for sid, rows in readings.items():
            m = self.mb_meta.get(sid)
            if not m or not rows:
                continue
            if m["owner"].upper() in ("MSC", "NAV CANADA") and any(km_between(m["lat"], m["lon"], a, b) <= DUP_KM for a, b in eccc_pts):
                continue  # ECCC already has this station, minute by minute
            last = rows[-1]
            if last["t"] < cutoff:
                continue
            latest[sid] = {"id": f"mb:{sid}", "name": m["name"], "src": "MB", "owner": m["owner"],
                           "zone": m.get("zone"), "lat": round(m["lat"], 4), "lon": round(m["lon"], 4),
                           **{k: (_r(v, 0) if k in ("rh", "wd") else _r(v)) if k != "t" else v for k, v in last.items()}}
            hist[sid] = [r for r in rows if r["t"] >= day_ago]
        with self.lock:
            self.mb, self.mb_hist = latest, hist
            self._stamp("mb", n=len(latest))
        log.info("Manitoba observations: %d stations", len(latest))

    def loop(self, stop):
        last_mb = 0
        while not stop.is_set():
            self.update_eccc()
            if datetime.now().timestamp() - last_mb >= MB_EVERY_MIN * 60 - 30:
                self.update_mb()
                last_mb = datetime.now().timestamp()
            stop.wait(ECCC_EVERY_MIN * 60)

    # ---- views ------------------------------------------------------------------------------------------
    def stations(self, bbox=None):
        with self.lock:
            st = list(self.eccc.values()) + list(self.mb.values())
            status = json.loads(json.dumps(self.status))
        if bbox:
            w, s, e, n = bbox
            st = [x for x in st if w <= x["lon"] <= e and s <= x["lat"] <= n]
        return {"stations": st, "status": status, "stale_min": STALE_MIN}

    def history(self, sid):
        """Last 24 h for a Manitoba station (ECCC readings aren't kept)."""
        if not sid.startswith("mb:"):
            return []
        with self.lock:
            return list(self.mb_hist.get(sid[3:], []))

    def near(self, lat, lon, km=60, n=4, max_age_min=STALE_MIN):
        """Nearest stations with a recent reading, closest first, each with its distance and bearing from the point."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).strftime("%Y-%m-%dT%H:%M")
        with self.lock:
            st = list(self.eccc.values()) + list(self.mb.values())
        out = []
        for x in st:
            if x["t"] < cutoff:
                continue
            d = km_between(lat, lon, x["lat"], x["lon"])
            if d <= km:
                b = math.degrees(math.atan2(math.radians(x["lon"] - lon) * math.cos(math.radians(lat)),
                                            math.radians(x["lat"] - lat))) % 360
                out.append(x | {"km": round(d, 1), "bearing": round(b)})
        out.sort(key=lambda x: x["km"])
        return out[:n]
