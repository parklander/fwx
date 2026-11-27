"""Watchdog for FWX's data feeds: pushes to the admin's devices when a feed breaks or recovers.

Every CHECK_MIN minutes each check returns (ok, message). A feed must fail FAILS_TO_ALERT checks in a row
before a notification goes out (so one slow response doesn't page anyone), and a recovery is announced once.
If the whole server is down nothing here runs: an outside uptime check (DigitalOcean Uptime) covers that.
"""
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("fwx.health")

CHECK_MIN = 10
FAILS_TO_ALERT = 2


def _age_h(iso):
    if not iso:
        return None
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds() / 3600


class Health:
    def __init__(self, data_dir, notify):
        self.file = Path(data_dir) / "health_state.json"
        self.notify = notify  # notify(title, body)
        self.checks = {}      # name -> fn() -> (ok, message)
        self.lock = threading.Lock()
        try:
            self.state = json.loads(self.file.read_text())
        except Exception:
            self.state = {}   # name -> {"ok", "msg", "fails", "since", "alerted"}

    def add(self, name, fn):
        self.checks[name] = fn

    def run_once(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        for name, fn in self.checks.items():
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, f"check failed: {str(e)[:160]}"
            with self.lock:
                s = self.state.setdefault(name, {"ok": True, "msg": "", "fails": 0, "since": now, "alerted": False})
                if ok:
                    if s["alerted"]:
                        out.append((f"FWX recovered: {name}", msg or "Working again."))
                    if not s["ok"]:
                        s["since"] = now
                    s.update(ok=True, msg=msg, fails=0, alerted=False)
                else:
                    if s["ok"]:
                        s["since"] = now
                    s.update(ok=False, msg=msg, fails=s["fails"] + 1)
                    if s["fails"] >= FAILS_TO_ALERT and not s["alerted"]:
                        s["alerted"] = True
                        out.append((f"FWX problem: {name}", msg))
                s["checked"] = now
        with self.lock:
            try:
                self.file.write_text(json.dumps(self.state, indent=1))
            except OSError:
                pass
        for title, body in out:
            log.warning("%s: %s", title, body)
            try:
                self.notify(title, body)
            except Exception:
                log.exception("admin push failed")

    def report(self):
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    def loop(self, stop):
        stop.wait(120)  # let the feeds load after a restart before judging them
        while not stop.is_set():
            self.run_once()
            stop.wait(CHECK_MIN * 60)


# ---- the checks, built from the app's own objects ----------------------------------------------------------

def alerts_check(wx_alerts):
    def fn():
        with wx_alerts.lock:
            areas = list(wx_alerts.by_area.values())
        if not areas:
            return False, "No ECCC alert poll has finished since the restart."
        newest = max(a.get("checked") or "" for a in areas)
        age = _age_h(newest)
        if age is None or age > 0.5:
            return False, f"ECCC alerts last checked {age or 0:.1f} h ago; the poll loop may have stopped."
        bad = [a["name"] for a in areas if a.get("error")]
        if len(bad) == len(areas):
            return False, f"ECCC alerts failing for every area: {areas[0].get('error', '')[:120]}"
        if bad:
            return True, f"ECCC alerts failing for {len(bad)} of {len(areas)} areas ({', '.join(bad[:3])})."
        return True, f"{len(areas)} areas checked."
    return fn


def cwfis_check(cwfis, today_fn):
    def fn():
        last = cwfis.last_date()
        if last is None:
            return False, "CWFIS DescribeCoverage gave no end date."
        if last < today_fn():
            return False, f"CWFIS forecast ends {last}, before today; CWFIS hasn't published today's grids."
        return True, f"CWFIS forecast to {last}."
    return fn


def weather_check(wxpoint, area_fn):
    def fn():
        a = area_fn()
        if not a:
            return True, "No area to test."
        wxpoint.open_meteo(a["lat"], a["lon"])  # cached 30 min, so this costs at most one call per half hour
        return True, f"Open-Meteo answering ({a['name']})."
    return fn


def rdps_check(rdps_maps, run_time):
    def fn():
        m = rdps_maps.meta()
        if not m.get("run"):
            return False, f"No RDPS wind run loaded (status {m.get('status')})."
        age = (datetime.now(timezone.utc) - run_time(m["run"])).total_seconds() / 3600
        if age > 18:
            return False, f"RDPS wind run {m['run']} is {age:.0f} h old; runs come every 6 h."
        return True, f"RDPS run {m['run']}."
    return fn


def fires_check(fire_data):
    def fn():
        with fire_data.lock:
            st = json.loads(json.dumps(fire_data.status))
        msgs, ok = [], True
        f = st.get("firms") or {}
        if f.get("enabled"):
            age = _age_h(f.get("checked"))
            if f.get("error") or age is None or age > 1.5:
                ok = False
                msgs.append(f"FIRMS: {f.get('error') or 'not checked for %.1f h' % (age or 0)}")
        r = st.get("reported") or {}
        if r.get("error"):
            ok = False
            msgs.append(f"CWFIS reported fires: {r['error'][:100]}")
        return ok, "; ".join(msgs) or "FIRMS and reported fires OK."
    return fn


def smoke_check(src, seasonal=False):
    """EER: the run should be fresh. BlueSky (seasonal): only a failing fetch counts, not an old run."""
    def fn():
        m = src.meta()
        if m.get("status") == "error" or (m.get("error") and not m.get("times")):
            return False, f"{src.label}: {str(m.get('error'))[:140]}"
        if not seasonal and m.get("stale"):
            return False, f"{src.label}: latest run is more than 18 h old."
        return True, f"{src.label} OK."
    return fn


def obs_check(obs_data):
    def fn():
        with obs_data.lock:
            st = json.loads(json.dumps(obs_data.status))
        msgs, ok = [], True
        for key, label, max_h in (("eccc", "ECCC stations", 0.75), ("mb", "Manitoba stations", 2.5)):
            s = st[key]
            age = _age_h(s.get("ok"))
            if s.get("checked") is None:
                continue  # not polled yet since the restart
            if age is None or age > max_h:
                ok = False
                msgs.append(f"{label}: {s.get('error') or 'no good update'}" + (f" ({age:.1f} h since the last good one)" if age else ""))
            else:
                msgs.append(f"{label}: {s.get('n')}")
        return ok, "; ".join(msgs) or "Not polled yet."
    return fn
