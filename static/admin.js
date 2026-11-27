/* FWX admin: data feed health, problem notifications to this device, and the usage summary.
 * The key comes from #t=<key> once, then lives in localStorage; it goes to the server in a header, never in a URL.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const when = (iso) => (iso ? new Date(iso).toLocaleString("en-CA", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "");
  const ago = (iso) => {
    if (!iso) return "";
    const m = (Date.now() - Date.parse(iso)) / 60e3;
    return m < 90 ? `${Math.round(m)} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
  };

  let token = "";
  const h = location.hash.match(/t=([^&]+)/);
  try {
    if (h) { token = decodeURIComponent(h[1]); localStorage.setItem("fwx-admin-token", token); history.replaceState(null, "", "/admin"); }
    token = token || localStorage.getItem("fwx-admin-token") || "";
    $("excl").value = localStorage.getItem("fwx-admin-excl") || "";
  } catch (_) {}

  async function api(path, body) {
    const r = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { "X-Admin-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined, cache: "no-store",
    });
    if (r.status === 404 && path.startsWith("/api/admin")) throw new Error("locked");
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
    return r.json();
  }

  function lock() {
    $("locked").hidden = false; $("health-s").hidden = $("usage-s").hidden = true;
    try { localStorage.removeItem("fwx-admin-token"); } catch (_) {}
  }
  $("tok-go").onclick = () => { token = $("tok").value.trim(); try { localStorage.setItem("fwx-admin-token", token); } catch (_) {} start(); };

  // ---- health --------------------------------------------------------------------------------------
  function renderHealth(j) {
    const rows = Object.entries(j.checks || {});
    $("h-tab").innerHTML = `<tr><th>Feed</th><th>State</th><th>Detail</th><th>Since</th><th>Checked</th></tr>`
      + (rows.length ? rows.map(([k, s]) => `<tr><td>${esc(k)}</td><td class="${s.ok ? "ok" : "bad"}">${s.ok ? "OK" : "Failing"}${!s.ok && s.alerted ? " · notified" : ""}</td>
        <td>${esc(s.msg)}</td><td class="muted">${esc(when(s.since))}</td><td class="muted">${esc(ago(s.checked))}</td></tr>`).join("")
        : `<tr><td colspan="5" class="muted">No checks have run yet (the first runs 2 min after a restart).</td></tr>`);
    if (j.admin_devices != null) $("p-count").textContent = `${j.admin_devices} device${j.admin_devices === 1 ? "" : "s"} getting notices`;
    $("h-when").textContent = "every 10 min";
  }
  $("h-run").onclick = async () => { $("h-run").disabled = true; try { renderHealth(await api("/api/admin/health/run", {})); loadHealth(); } finally { $("h-run").disabled = false; } };
  async function loadHealth() { renderHealth(await api("/api/admin/health")); }

  // ---- problem notifications to this device ------------------------------------------------------------
  const pushable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const b64 = (s) => Uint8Array.from(atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const say = (t) => ($("msg").textContent = t);
  async function subscription(create) {
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub && create) {
      if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications are blocked for this site in the browser settings.");
      const { key } = await (await fetch("/api/push/key")).json();
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
    }
    return sub;
  }
  $("p-add").onclick = async () => {
    if (!pushable) return say("This browser can't receive notifications. On iPhone, add FWX to the home screen first and open it from there.");
    try {
      const sub = await subscription(true);
      const label = /iPhone|iPad/.test(navigator.userAgent) ? "iPhone/iPad" : /Android/.test(navigator.userAgent) ? "Android" : navigator.platform || "computer";
      const j = await api("/api/admin/push/subscribe", { subscription: sub.toJSON(), label });
      say(`Added. ${j.admin_devices} device(s) get FWX problem notices.`);
      loadHealth();
    } catch (e) { say(e.message); }
  };
  $("p-test").onclick = async () => { try { const j = await api("/api/admin/push/test", {}); say(`Test sent to ${j.sent} device(s).`); } catch (e) { say(e.message); } };
  $("p-rm").onclick = async () => {
    try {
      const sub = pushable && await subscription(false);
      if (!sub) return say("This device isn't set up for notifications.");
      const j = await api("/api/admin/push/remove", { endpoint: sub.endpoint });
      say(`Removed. ${j.admin_devices} device(s) left.`); loadHealth();
    } catch (e) { say(e.message); }
  };

  // ---- usage -------------------------------------------------------------------------------------------
  const bar = (v, max) => `<div class="bar" style="width:${max ? Math.max(1, (100 * v) / max) : 0}%"></div>`;
  const netName = (ip) => (/^205\.19[2-5]\./.test(ip) ? "Gov. of Canada network" : "");
  async function loadUsage() {
    const excl = $("excl").value.trim();
    try { localStorage.setItem("fwx-admin-excl", excl); } catch (_) {}
    const U = await api(`/api/admin/usage?days=${$("days").value}&exclude=${encodeURIComponent(excl)}`);
    $("u-tot").textContent = `${U.totals.views} page views from ${U.totals.ips} IP addresses since ${U.from}`;
    const dur = (s) => (s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
    const PN = { main: "Map", beta: "Beta", range: "Alert panel", about: "About" };
    const V = Object.entries(U.visits || {});
    $("u-visit").innerHTML = V.length ? `<tr><th>Page</th><th>Visits</th><th>Median</th><th>3 in 4 under</th><th>Glance &lt;30 s · check &lt;3 min · working</th><th>Total on screen</th></tr>`
      + V.map(([pg, v]) => {
        const t = v.glance + v.check + v.work || 1;
        return `<tr><td>${esc(PN[pg] || pg)}</td><td class="n">${v.visits}</td><td class="n">${dur(v.median_s)}</td><td class="n">${dur(v.p75_s)}</td>
          <td><div class="split" title="${v.glance} glance · ${v.check} check · ${v.work} working"><i class="g" style="width:${100 * v.glance / t}%"></i><i class="c" style="width:${100 * v.check / t}%"></i><i class="w" style="width:${100 * v.work / t}%"></i></div>
          <small class="muted">${v.glance} · ${v.check} · ${v.work}</small></td><td class="n">${v.hours} h</td></tr>`;
      }).join("") + `<tr><td colspan="6" class="muted">The alert panel is meant to stay on screen, so its total hours matter more than its visit lengths.</td></tr>`
      : `<tr><td class="muted">No timed visits yet.</td></tr>`;
    $("u-act").innerHTML = (U.actions || []).length ? `<tr><th>Page</th><th>Feature</th><th></th><th></th></tr>` + U.actions.map((a) =>
      `<tr><td class="muted">${esc(PN[a.page] || a.page)}</td><td>${esc(a.action.replace(/_/g, " "))}</td><td class="n">${Math.round(a.share * 100)}%</td><td class="barcell">${bar(a.share, 1)}</td></tr>`).join("")
      : `<tr><td class="muted">No feature use logged yet.</td></tr>`;
    $("u-ret").innerHTML = Object.entries(U.returning || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v}</td></tr>`).join("");
    const fv = U.feedback_votes || {};
    $("u-fbv").textContent = `👍 ${fv.up || 0} · 👎 ${fv.down || 0}`;
    $("u-fb").innerHTML = (U.feedback || []).length ? U.feedback.map((f) => `<tr><td>${f.vote === "up" ? "👍" : "👎"}</td><td class="fbt">${esc(f.text || "")}</td>
        <td class="muted">${esc(PN[f.page] || f.page)}${f.aoi ? ` · ${esc(f.aoi)}` : ""}</td><td class="muted">${esc(f.ip)}</td><td class="muted">${esc(when(f.t))}</td></tr>`).join("")
      : `<tr><td class="muted">No feedback yet. The map and beta ask once every two weeks after 90 s on screen, and there's a "Send feedback" button in Layers.</td></tr>`;
    const dmax = Math.max(1, ...U.by_day.map((d) => d.ips));
    $("u-day").innerHTML = U.by_day.length ? U.by_day.slice().reverse().map((d) => `<tr><td>${esc(d.day)}</td><td class="n">${d.views}</td><td class="n">${d.ips}</td><td class="barcell">${bar(d.ips, dmax)}</td></tr>`).join("")
      : `<tr><td class="muted">Nothing logged yet.</td></tr>`;
    const bmax = Math.max(1, ...U.by_base.map((b) => b.views));
    $("u-base").innerHTML = U.by_base.length ? U.by_base.map((b) => `<tr><td>${esc(b.name)}</td><td class="n">${b.views}</td><td class="n">${b.ips}</td><td class="n">${b.pdf || ""}</td><td class="barcell">${bar(b.views, bmax)}</td></tr>`).join("")
      : `<tr><td class="muted">No bases opened yet.</td></tr>`;
    $("u-lnote").textContent = `share of ${U.main_ipdays} map visits (one per IP per day) that had it on`;
    $("u-layer").innerHTML = U.layers.length ? U.layers.map((l) => `<tr><td>${esc(l.layer)}</td><td class="n">${Math.round(l.share * 100)}%</td><td class="barcell">${bar(l.share, 1)}</td></tr>`).join("")
      : `<tr><td class="muted">No layer use logged yet.</td></tr>`;
    $("u-page").innerHTML = Object.entries(U.by_page).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v}</td></tr>`).join("") || `<tr><td class="muted">—</td></tr>`;
    $("u-tab").innerHTML = Object.entries(U.tabs).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v}</td></tr>`).join("") || `<tr><td class="muted">—</td></tr>`;
    $("u-ip").innerHTML = `<tr><th>IP address</th><th>Views</th><th>Bases</th><th>Pages</th><th>Device</th><th>First seen</th><th>Last seen</th></tr>`
      + (U.by_ip.map((u) => `<tr><td><a href="https://ipinfo.io/${encodeURIComponent(u.ip)}" target="_blank" rel="noopener">${esc(u.ip)}</a>
          <div class="muted">${esc(netName(u.ip))}</div></td><td class="n">${u.views}</td>
          <td class="chips">${Object.entries(u.bases).map(([k, v]) => `<span>${esc(k)} ${v}</span>`).join("")}</td>
          <td class="chips">${Object.entries(u.pages).map(([k, v]) => `<span>${esc(k)} ${v}</span>`).join("")}</td>
          <td>${esc(u.ua)}</td><td class="muted">${esc(when(u.first))}</td><td class="muted">${esc(when(u.last))}</td></tr>`).join("")
        || `<tr><td colspan="7" class="muted">Nothing logged yet.</td></tr>`);
  }
  $("u-go").onclick = () => loadUsage().catch((e) => alert(e.message));
  $("days").onchange = $("u-go").onclick;

  // ---- weekly report ------------------------------------------------------------------------------------
  async function loadReportMeta() {
    const j = await api("/api/admin/report");
    const l = j.last || {};
    $("r-last").textContent = (j.repo ? `to ${j.repo}` : "FW_REPORT_REPO not set: saved on the server only")
      + (l.day ? ` · last sent ${l.day}: ${l.status}` : " · none sent yet");
    return j;
  }
  $("r-show").onclick = async () => {
    const pre = $("r-text");
    if (!pre.hidden) { pre.hidden = true; return; }
    try { pre.textContent = (await loadReportMeta()).text; pre.hidden = false; } catch (e) { alert(e.message); }
  };
  $("r-pub").onclick = async () => {
    $("r-pub").disabled = true;
    try { const j = await api("/api/admin/report/publish", {}); alert(j.status); loadReportMeta(); } catch (e) { alert(e.message); }
    finally { $("r-pub").disabled = false; }
  };

  async function start() {
    if (!token) return lock();
    try {
      await loadHealth();
      $("locked").hidden = true; $("health-s").hidden = $("usage-s").hidden = $("report-s").hidden = false;
      loadReportMeta().catch(() => {});
      await loadUsage();
    } catch (e) {
      if (e.message === "locked") lock(); else alert(e.message);
    }
  }
  start();
  setInterval(() => { if (!$("health-s").hidden) loadHealth().catch(() => {}); }, 120e3);
})();
