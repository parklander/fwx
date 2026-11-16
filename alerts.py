"""ECCC public weather alerts for each area, polled every few minutes.

An area with an "alert_feed" in aois.json (e.g. "mbrm67") is read from its ECCC battleboard feed,
https://weather.gc.ca/rss/battleboard/<code>_e.xml, the same region as the public warnings page.
Other areas use the MSC GeoMet weather-alerts API at the area's reference point.

New alerts are handed to `on_new` (push notifications). The alerts already known are kept in
data/alerts_state.json, so a restart doesn't re-announce alerts that were already in effect.
"""
import html
import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("alerts")

POLL_MIN = 5
END_POLLS = 2  # an alert counts as ended only after this many checks in a row without it (ECCC re-issues flicker)
FEED_URL = "https://weather.gc.ca/rss/battleboard/{code}_e.xml"
PAGE_URL = "https://weather.gc.ca/warnings/report_e.html?{code}"
GEOMET = "https://api.weather.gc.ca/collections/weather-alerts/items"
ATOM = "{http://www.w3.org/2005/Atom}"
_session = requests.Session()


def level(name):
    """Coarse severity from the alert name: warning > watch > statement/advisory."""
    n = name.lower()
    if "warning" in n or n.startswith("red"):
        return "warning"
    if "watch" in n or n.startswith("orange"):
        return "watch"
    return "statement"


def location_page(lat, lon):
    return f"https://weather.gc.ca/en/location/index.html?coords={lat:.3f},{lon:.3f}"


def _strip(s):
    return re.sub(r"[ \t\xa0]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_feed(xml_text):
    """(region, active alerts) from a battleboard Atom feed; skips "No alerts in effect" and ended entries."""
    root = ET.fromstring(xml_text)
    region = (root.findtext(f"{ATOM}title") or "").split(" - Weather Alert")[0].strip()
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        t = title.lower()
        if t.startswith("no ") or " ended" in t or t.startswith("ended"):
            continue
        name = re.sub(r"\s+in effect$", "", title.split(",")[0].strip(), flags=re.I)
        link = e.find(f"{ATOM}link")
        name = name.title() if name.isupper() else name
        out.append({"name": name, "level": level(name), "issued": e.findtext(f"{ATOM}updated"), "expires": None,
                    "text": _strip(e.findtext(f"{ATOM}summary")), "link": link.get("href") if link is not None else None})
    return region, out


def geomet_alerts(lat, lon):
    """Active alerts whose area covers the point (MSC GeoMet). Raises if the request fails."""
    r = _session.get(GEOMET, timeout=30, params={"bbox": f"{lon - 0.01},{lat - 0.01},{lon + 0.01},{lat + 0.01}",
                                                  "limit": 100, "f": "json"})
    r.raise_for_status()
    now = datetime.now(timezone.utc).isoformat()
    out, seen = [], set()
    region = None
    for f in r.json().get("features", []):
        p = f["properties"]
        if (p.get("expiration_datetime") or "9") < now:
            continue
        name = p.get("alert_name_en") or ""
        if (p.get("alert_code"), name) in seen:
            continue
        seen.add((p.get("alert_code"), name))
        region = region or p.get("feature_name_en")
        out.append({"name": name, "level": level(name), "issued": p.get("publication_datetime"),
                    "expires": p.get("expiration_datetime"), "text": (p.get("alert_text_en") or "").strip(),
                    "link": location_page(lat, lon)})
    return region, out


class Alerts:
    def __init__(self, aoi_file, state_file, on_new=None):
        self.aoi_file, self.state_file, self.on_new = Path(aoi_file), Path(state_file), on_new
        self.lock = threading.Lock()
        self.by_area = {}
        self.missing = {}
        try:
            self.known = set(json.loads(self.state_file.read_text()))
        except Exception:
            self.known = None  # first run: take whatever is in effect as the baseline, announce nothing

    def _fetch(self, a):
        code = a.get("alert_feed")
        if code:
            r = _session.get(FEED_URL.format(code=code), timeout=20)
            r.raise_for_status()
            region, items = parse_feed(r.text)
            return {"source": "ECCC", "feed": code, "region": region, "page": PAGE_URL.format(code=code), "alerts": items}
        region, items = geomet_alerts(a["lat"], a["lon"])
        return {"source": "MSC GeoMet", "feed": None, "region": region,
                "page": location_page(a["lat"], a["lon"]), "alerts": items}

    def poll(self):
        areas = json.loads(self.aoi_file.read_text(encoding="utf-8"))["areas"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for a in areas:
            with self.lock:
                prev = self.by_area.get(a["id"])
            try:
                cur = self._fetch(a) | {"error": None}
            except Exception as e:  # keep the last good result, flag the error
                log.warning("alerts for %s unavailable: %s", a["id"], e)
                cur = (prev or {"source": None, "feed": a.get("alert_feed"), "region": None,
                                "page": location_page(a["lat"], a["lon"]), "alerts": []}) | {"error": str(e)}
            cur |= {"id": a["id"], "name": a["name"], "prov": a.get("prov"), "checked": now}
            # ECCC re-issues ongoing alerts; for a moment the old one is gone and the new one not yet listed
            seen = {x["name"] for x in cur["alerts"]}
            for x in (prev or {}).get("alerts", []):
                k = (a["id"], x["name"])
                if x["name"] in seen:
                    continue
                self.missing[k] = self.missing.get(k, 0) + 1
                if self.missing[k] < END_POLLS:
                    cur["alerts"].append(x)
                else:
                    self.missing.pop(k, None)
            for n in seen:
                self.missing.pop((a["id"], n), None)
            with self.lock:
                self.by_area[a["id"]] = cur
        self._announce()

    def _announce(self):
        with self.lock:
            active = {f"{v['id']}|{x['name']}": (v, x) for v in self.by_area.values() for x in v["alerts"]}
        if self.known is not None and self.on_new:
            for k, (area, alert) in active.items():
                if k not in self.known:
                    log.warning("NEW ALERT %s: %s", area["name"], alert["name"])
                    try:
                        self.on_new(area, alert)
                    except Exception:
                        log.exception("notifying %s failed", k)
        self.known = set(active)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(sorted(self.known)))
        except Exception as e:
            log.warning("could not save alert state: %s", e)

    def view(self):
        with self.lock:
            return list(self.by_area.values())

    def for_area(self, area_id):
        with self.lock:
            return self.by_area.get(area_id)

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("alert poll failed")
            stop.wait(POLL_MIN * 60)
