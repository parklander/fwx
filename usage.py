"""Usage log: which pages, bases and layers people use, for feedback on what's useful.

The pages send a small beacon (POST /api/usage) when they load and when layers change; the server adds the
time and the client IP and appends one JSON line to data/usage.jsonl. The about page says this is collected.
summary() rolls the log up for the admin page: by base, by IP, by layer and by day.
"""
import json
import logging
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("fwx.usage")

PAGES = {"main", "beta", "range", "about"}
EVENTS = {"view", "layers", "tab", "pdf", "base", "time", "act", "fb"}
LAYER_KEYS = {"layer", "wind", "precip", "basemap", "fires", "repfires", "smoke", "tab"}
OFF = {"", "off", "none", None}
KEEP_DAYS = 120
MAX_BYTES = 40e6
PER_IP_HOUR = 300  # beacons per IP per hour before the rest are dropped
FB_PER_IP_DAY = 5   # feedback messages per IP per day
_word = re.compile(r"^[\w .:/-]{0,60}$")
_sid = re.compile(r"^[a-z0-9]{12}$")
_act = re.compile(r"^[a-z_]{1,32}$")


def _clean(v):
    return v if isinstance(v, str) and _word.match(v) else None


class Usage:
    def __init__(self, data_dir):
        self.file = Path(data_dir) / "usage.jsonl"
        self.lock = threading.Lock()
        self.rate = {}  # ip -> (hour, count)
        self.fb_rate = {}  # ip -> (day, count)
        try:
            if self.file.stat().st_size > MAX_BYTES:
                self._trim()
        except FileNotFoundError:
            pass

    def _trim(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
        with self.lock:
            keep = [ln for ln in self.file.read_text(encoding="utf-8").splitlines() if _t(ln) >= cutoff]
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            tmp.replace(self.file)
        log.info("usage log trimmed to %d lines", len(keep))

    def record(self, ip, page, event="view", aoi=None, base=None, layers=None, ua=None,
               sid=None, secs=None, act=None, vote=None, text=None):
        """True if logged. Feedback ("fb") needs a vote; "time" needs secs; "act" needs an action name."""
        if page not in PAGES or event not in EVENTS:
            return False
        if event == "time" and not (isinstance(secs, (int, float)) and 0 < secs <= 86400):
            return False
        if event == "act" and not (isinstance(act, str) and _act.match(act)):
            return False
        if event == "fb":
            if vote not in ("up", "down"):
                return False
            day = time.strftime("%Y%m%d")
            with self.lock:
                d, n = self.fb_rate.get(ip, (day, 0))
                n = n + 1 if d == day else 1
                self.fb_rate[ip] = (day, n)
            if n > FB_PER_IP_DAY:
                return False
        hour = int(time.time() // 3600)
        with self.lock:
            h, n = self.rate.get(ip, (hour, 0))
            n = n + 1 if h == hour else 1
            self.rate[ip] = (hour, n)
            if len(self.rate) > 5000:
                self.rate = {k: v for k, v in self.rate.items() if v[0] == hour}
        if n > PER_IP_HOUR:
            return False
        rec = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ip": ip, "page": page, "ev": event}
        if _clean(aoi):
            rec["aoi"] = aoi
        if _clean(base):
            rec["base"] = base
        if isinstance(layers, dict):
            ly = {k: v for k, v in layers.items() if k in LAYER_KEYS and _clean(v) is not None}
            if ly:
                rec["layers"] = ly
        if isinstance(ua, str):
            rec["ua"] = _ua(ua)
        if isinstance(sid, str) and _sid.match(sid):
            rec["sid"] = sid
        if event == "time":
            rec["secs"] = int(round(secs))
        if event == "act":
            rec["act"] = act
        if event == "fb":
            rec["vote"] = vote
            if isinstance(text, str) and text.strip():
                rec["text"] = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.strip())[:500]
        line = json.dumps(rec, separators=(",", ":"))
        with self.lock:
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True

    def summary(self, days=14, exclude=()):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            with self.lock:
                lines = self.file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        by_day = defaultdict(lambda: {"views": 0, "ips": set()})
        by_base = defaultdict(lambda: {"views": 0, "ips": set(), "pdf": 0, "last": ""})
        by_ip = defaultdict(lambda: {"views": 0, "bases": Counter(), "pages": Counter(), "first": "", "last": "", "ua": ""})
        by_page = Counter()
        tabs = Counter()
        # layers: count each (ip, day) once per layer setting it used, so a busy user doesn't swamp the rest
        layer_use = defaultdict(set)
        main_ipdays = set()
        sessions = defaultdict(lambda: {"page": None, "secs": 0, "acts": set(), "aoi": None, "ip": None})
        ip_days = defaultdict(set)
        feedback = []
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("t", "") < cutoff or r.get("ip") in exclude:
                continue
            ip, day, ev = r["ip"], r["t"][:10], r.get("ev")
            aoi = r.get("aoi")
            if r.get("sid"):
                s = sessions[r["sid"]]
                s["page"] = s["page"] or r["page"]
                s["ip"] = ip
                s["aoi"] = aoi or s["aoi"]
                if ev == "time":
                    s["secs"] += r.get("secs", 0)
                elif ev == "act":
                    s["acts"].add(r.get("act"))
            if ev == "view":
                ip_days[ip].add(day)
            if ev == "fb":
                feedback.append({k: r.get(k) for k in ("t", "ip", "page", "aoi", "vote", "text")})
            u = by_ip[ip]
            u["first"] = u["first"] or r["t"]
            u["last"] = r["t"]
            u["ua"] = r.get("ua") or u["ua"]
            if ev == "view":
                by_day[day]["views"] += 1
                by_day[day]["ips"].add(ip)
                by_page[r["page"]] += 1
                u["views"] += 1
                u["pages"][r["page"]] += 1
            if aoi and ev in ("view", "base", "pdf"):
                b = by_base[aoi]
                b["ips"].add(ip)
                b["last"] = r["t"]
                if ev == "pdf":
                    b["pdf"] += 1
                else:
                    b["views"] += 1
                u["bases"][aoi] += 1
            if ev == "tab" and r.get("layers", {}).get("tab"):
                tabs[r["layers"]["tab"]] += 1
            if r["page"] == "main" and ev in ("view", "layers"):
                main_ipdays.add((ip, day))
                for k, v in (r.get("layers") or {}).items():
                    if k != "tab" and v not in OFF and not (k == "basemap"):
                        layer_use[f"{k}: {v}"].add((ip, day))
                    if k == "basemap":
                        layer_use[f"basemap: {v}"].add((ip, day))
        n_main = len(main_ipdays) or 1
        # visit length per page (the alert panel is a wall display: its hours on screen are reported on their own)
        visits = {}
        for pg in ("main", "beta", "range", "about"):
            secs = sorted(s["secs"] for s in sessions.values() if s["page"] == pg and s["secs"] > 0)
            if not secs:
                continue
            q = lambda f: secs[min(len(secs) - 1, int(f * len(secs)))]
            visits[pg] = {"visits": len(secs), "median_s": round(statistics.median(secs)), "p75_s": q(0.75), "hours": round(sum(secs) / 3600, 1),
                          "glance": sum(x < 30 for x in secs), "check": sum(30 <= x < 180 for x in secs),
                          "work": sum(x >= 180 for x in secs)}
        acts = Counter()
        n_sess = Counter()
        for s in sessions.values():
            if s["page"]:
                n_sess[s["page"]] += 1
            for a in s["acts"]:
                acts[(s["page"], a)] += 1
        actions = sorted(({"page": pg, "action": a, "visits": n, "share": round(n / (n_sess[pg] or 1), 3)}
                          for (pg, a), n in acts.items()), key=lambda x: (x["page"], -x["visits"]))
        days_back = Counter(min(len(d), 4) for d in ip_days.values())
        return {
            "visits": visits, "sessions": dict(n_sess), "actions": actions,
            "returning": {"1 day": days_back[1], "2 days": days_back[2], "3 days": days_back[3], "4+ days": days_back[4]},
            "feedback": feedback[::-1][:200],
            "feedback_votes": dict(Counter(f["vote"] for f in feedback)),
            "days": days, "from": cutoff[:10],
            "totals": {"views": sum(d["views"] for d in by_day.values()),
                       "ips": len({ip for d in by_day.values() for ip in d["ips"]})},
            "by_day": [{"day": k, "views": v["views"], "ips": len(v["ips"])} for k, v in sorted(by_day.items())],
            "by_page": dict(by_page.most_common()),
            "by_base": sorted(({"aoi": k, "views": v["views"], "ips": len(v["ips"]), "pdf": v["pdf"], "last": v["last"]}
                               for k, v in by_base.items()), key=lambda x: -x["views"]),
            "by_ip": sorted(({"ip": k, "views": v["views"], "bases": dict(v["bases"].most_common(6)),
                              "pages": dict(v["pages"]), "first": v["first"], "last": v["last"], "ua": v["ua"]}
                             for k, v in by_ip.items()), key=lambda x: -x["views"]),
            "layers": sorted(({"layer": k, "ipdays": len(v), "share": round(len(v) / n_main, 3)}
                              for k, v in layer_use.items()), key=lambda x: -x["ipdays"]),
            "main_ipdays": len(main_ipdays),
            "tabs": dict(tabs.most_common()),
        }


def _t(line):
    try:
        return json.loads(line).get("t", "")
    except ValueError:
        return ""


def _ua(ua):
    """A short device/browser label, not the whole user-agent string."""
    dev = "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else \
        "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "other"
    br = "Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua else \
        "Safari" if "Safari/" in ua else "other"
    bot = re.search(r"bot|crawl|spider|preview|curl|python", ua, re.I)
    return "bot" if bot else f"{dev} {br}"
