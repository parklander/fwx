"""Web Push notifications: browsers subscribe to alerts for some areas (or all); new ECCC alerts are pushed to them.

Works with the page closed. The server's VAPID key pair is made on first start and kept in data/vapid.pem;
subscriptions are kept in data/subscriptions.json. Phones: Android Chrome works from the browser; on iPhone,
add the site to the Home Screen first (iOS only allows web push for installed sites).
"""
import base64
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("push")


class Push:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key_file = self.dir / "vapid.pem"
        self.subs_file = self.dir / "subscriptions.json"
        self.admin_file = self.dir / "push_admins.json"  # devices that get FWX's own health notices
        self.lock = threading.Lock()
        self.contact = os.environ.get("FW_CONTACT", "https://fwx.rtams.ca")
        from py_vapid import Vapid
        if not self.key_file.exists():
            v = Vapid()
            v.generate_keys()
            v.save_key(str(self.key_file))
            os.chmod(self.key_file, 0o600)
            log.info("made a new VAPID key pair")
        self.vapid = Vapid.from_file(str(self.key_file))
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        raw = self.vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        try:
            self.subs = json.loads(self.subs_file.read_text())
        except Exception:
            self.subs = {}

    def _admins(self):
        try:
            return json.loads(self.admin_file.read_text())
        except Exception:
            return {}

    def add_admin(self, subscription, label=""):
        ep = subscription.get("endpoint")
        if not ep or not str(ep).startswith("https://") or not subscription.get("keys"):
            raise ValueError("not a push subscription")
        with self.lock:
            a = self._admins()
            a[ep] = {"sub": subscription, "label": label[:60],
                     "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.admin_file.write_text(json.dumps(a, indent=1))
            os.chmod(self.admin_file, 0o600)
        return len(a)

    def remove_admin(self, endpoint):
        with self.lock:
            a = self._admins()
            a.pop(endpoint, None)
            self.admin_file.write_text(json.dumps(a, indent=1))
        return len(a)

    def admin_count(self):
        with self.lock:
            return len(self._admins())

    def notify_admin(self, title, body, url="/admin"):
        """FWX's own problems (a feed down, a recovery) to the admin devices only."""
        from pywebpush import WebPushException, webpush
        with self.lock:
            targets = self._admins()
        sent = 0
        for ep, s in targets.items():
            try:
                webpush(s["sub"], json.dumps({"title": title, "body": body[:240], "url": url, "tag": "fwx-admin|" + title}),
                        vapid_private_key=self.vapid, vapid_claims={"sub": self.contact}, ttl=24 * 3600)
                sent += 1
            except WebPushException as e:
                if getattr(e.response, "status_code", None) in (404, 410):
                    self.remove_admin(ep)
                else:
                    log.warning("admin push failed: %s", e)
        log.info("admin notice %r to %d device(s)", title, sent)
        return sent

    def _save(self):
        tmp = self.subs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.subs, indent=1))
        tmp.replace(self.subs_file)

    def subscribe(self, subscription, areas):
        """areas: list of area ids, or "all"."""
        ep = subscription.get("endpoint")
        if not ep or not str(ep).startswith("https://") or not subscription.get("keys"):
            raise ValueError("not a push subscription")
        if areas != "all" and not (isinstance(areas, list) and all(isinstance(a, str) for a in areas)):
            raise ValueError("areas must be a list of area ids or \"all\"")
        with self.lock:
            if areas != "all" and not areas:
                self.subs.pop(ep, None)
            else:
                self.subs[ep] = {"sub": subscription, "areas": areas,
                                 "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save()
        return self.status(ep)

    def unsubscribe(self, endpoint):
        with self.lock:
            self.subs.pop(endpoint, None)
            self._save()

    def status(self, endpoint):
        with self.lock:
            s = self.subs.get(endpoint)
        return {"subscribed": bool(s), "areas": s["areas"] if s else []}

    def _send(self, ep, payload):
        from pywebpush import WebPushException, webpush
        with self.lock:
            s = self.subs.get(ep)
        if not s:
            return False
        try:
            webpush(s["sub"], json.dumps(payload), vapid_private_key=self.vapid,
                    vapid_claims={"sub": self.contact}, ttl=12 * 3600)
            return True
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):  # the browser dropped the subscription
                log.info("removing expired subscription")
                self.unsubscribe(ep)
            else:
                log.warning("push failed (%s): %s", code, e)
            return False

    def send_test(self, endpoint):
        return self._send(endpoint, {"title": "Fire weather alerts are on",
                                     "body": "You'll be notified here when ECCC issues a new alert for your areas.",
                                     "url": "/", "tag": "fwx-test"})

    def notify_alert(self, area, alert):
        payload = {"title": f"{area['name']}: {alert['name']}",
                   "body": (alert.get("text") or "")[:240],
                   "url": f"/?aoi={area['id']}", "tag": f"fwx|{area['id']}|{alert['name']}"}
        with self.lock:
            targets = [ep for ep, s in self.subs.items() if s["areas"] == "all" or area["id"] in s["areas"]]
        sent = sum(self._send(ep, payload) for ep in targets)
        log.info("pushed %s / %s to %d of %d subscribers", area["id"], alert["name"], sent, len(targets))
