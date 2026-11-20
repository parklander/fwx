/* FWX alert panel for range control and the fire hall (wall display).
 *
 * Reads the ECCC alerts FWX already polls (/api/alerts, every 5 min on the server), so every panel and phone
 * sees the same alerts and the same acknowledgements. A new, unacknowledged alert pulses the screen and sounds
 * a tone every 20 s until someone acknowledges it; acknowledgements are logged on the server.
 *
 * URL options: ?aoi=<area id>   ?test=1 (adds a fake warning, acknowledged only in this browser)
 *              ?autostart=1 (skip the start button; for a kiosk browser that allows sound without a click)
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const ALERT_EVERY = 60e3, WX_EVERY = 15 * 60e3, TONE_EVERY = 20e3;
  const RANK = { warning: 3, watch: 2, statement: 1 };
  const DANGER = { Low: "#2f6fd6", Moderate: "#3c9a3f", High: "#e8c400", "Very High": "#f08a00", Extreme: "#d32f2f" };
  const DARK_TEXT = new Set(["High", "Very High"]);
  // display bands only (km/h): highlight strong gusts and flag a wind shift in the next 12 h
  const GUST_HI = 50, GUST_VHI = 70, SHIFT_DEG = 60, SHIFT_HOURS = 3, SHIFT_MIN_WS = 15;

  const qs = new URLSearchParams(location.search);
  const TEST = qs.get("test") === "1";
  let areaId = qs.get("aoi");
  let muted = false, started = false, lastAlerts = null, lastOk = 0, pollMin = 5, ackTarget = null;
  const testAcks = {};
  try { areaId = areaId || localStorage.getItem("fwx-range-aoi"); muted = localStorage.getItem("fwx-range-muted") === "1"; } catch (_) {}

  // ---- clock ---------------------------------------------------------------------------------------
  const hm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const when = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const same = d.toDateString() === new Date().toDateString();
    return (same ? "" : d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + " ") + hm(d);
  };
  const tick = () => { $("clock").textContent = hm(new Date()); };
  setInterval(tick, 1000); tick();

  // ---- sound ---------------------------------------------------------------------------------------
  let ctx = null;
  function tone() {
    if (muted || !ctx) return;
    const t0 = ctx.currentTime;
    [[880, 0], [660, 0.35], [880, 0.7], [660, 1.05]].forEach(([f, dt]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "square"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + dt);
      g.gain.exponentialRampToValueAtTime(0.25, t0 + dt + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.3);
      o.connect(g).connect(ctx.destination); o.start(t0 + dt); o.stop(t0 + dt + 0.32);
    });
  }
  const setMute = () => { $("mute").textContent = muted ? "🔕 Sound off" : "🔔 Sound on"; };
  $("mute").onclick = () => { muted = !muted; setMute(); try { localStorage.setItem("fwx-range-muted", muted ? "1" : "0"); } catch (_) {} };
  setMute();

  // keep the screen on
  let lock = null;
  async function wake() { try { if ("wakeLock" in navigator && document.visibilityState === "visible") lock = await navigator.wakeLock.request("screen"); } catch (_) {} }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") wake(); });

  const start = () => {
    try { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); ctx.resume(); } catch (_) {}
    started = true; $("start").hidden = true; wake();
    render();
  };
  $("startBtn").onclick = start;
  // ?autostart=1 for a kiosk browser started with --autoplay-policy=no-user-gesture-required
  if (qs.get("autostart") === "1") start(); else $("start").hidden = false;

  // ---- areas ---------------------------------------------------------------------------------------
  async function loadAreas() {
    const r = await fetch("/api/areas"); const j = await r.json();
    const areas = j.areas || j;
    areaId = areas.some((a) => a.id === areaId) ? areaId : (j.default || areas[0].id);
    $("area").innerHTML = areas.map((a) => `<option value="${esc(a.id)}"${a.id === areaId ? " selected" : ""}>${esc(a.name)}</option>`).join("");
    $("area").onchange = () => {
      areaId = $("area").value;
      try { localStorage.setItem("fwx-range-aoi", areaId); } catch (_) {}
      const u = new URL(location.href); u.searchParams.set("aoi", areaId); history.replaceState(null, "", u);
      render(); loadWx(); loadLog(); loadFires();
    };
  }

  // ---- alerts --------------------------------------------------------------------------------------
  async function loadAlerts() {
    try {
      const r = await fetch("/api/alerts", { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      pollMin = j.poll_min || 5; lastAlerts = j; lastOk = Date.now();
    } catch (_) { /* keep the last result; render() shows the panel as out of contact */ }
    render();
  }

  function current() {
    const a = lastAlerts && lastAlerts.areas.find((x) => x.id === areaId);
    if (!a) return null;
    const list = a.alerts.slice();
    if (TEST) list.unshift({ name: "TEST — Wind Warning", level: "warning", test: true, issued: new Date().toISOString(),
      text: "Test alert from ?test=1. Strong winds with gusts up to 100 km/h, abrupt wind shift with a cold front. Acknowledge to clear it (this browser only).",
      ack: testAcks.t || null });
    list.sort((x, y) => (RANK[y.level] || 0) - (RANK[x.level] || 0));
    return { area: a, list };
  }

  function render() {
    const cur = current();
    const st = $("status"), word = $("statusWord"), sub = $("statusSub");
    // contact with the FWX server, and the server's last successful check of ECCC
    const checked = cur && cur.area.checked ? new Date(cur.area.checked) : null;
    const age = checked ? (Date.now() - checked) / 60e3 : Infinity;
    const lost = !lastOk || Date.now() - lastOk > 3 * ALERT_EVERY;
    const stale = lost || age > 3 * pollMin || (cur && cur.area.error);
    const feed = $("feed");
    if (lost) { feed.className = "feed bad"; feed.textContent = "No contact with FWX — check weather.gc.ca directly"; }
    else if (stale) { feed.className = "feed bad"; feed.textContent = `ECCC not answering — last good check ${checked ? when(checked) : "none"}`; }
    else { feed.className = "feed"; feed.textContent = `ECCC checked ${hm(checked)}${cur.area.region ? " · " + cur.area.region : ""}`; }

    const list = cur ? cur.list : [];
    const top = list[0];
    const unacked = list.filter((x) => !x.ack);
    st.className = "status " + (top ? top.level : stale ? "stale" : "clear");
    if (top) {
      word.textContent = top.level === "warning" ? "WARNING IN EFFECT" : top.level === "watch" ? "WATCH IN EFFECT" : "STATEMENT IN EFFECT";
      sub.textContent = list.map((x) => x.name).join(" · ");
    } else if (stale) {
      word.textContent = "ALERTS UNKNOWN"; sub.textContent = "This panel can't confirm the alert status. Check weather.gc.ca.";
    } else {
      word.textContent = "NO ALERTS"; sub.textContent = "No ECCC warnings, watches or statements for this area.";
    }
    document.body.classList.toggle("unacked", started && unacked.length > 0);
    document.title = (unacked.length ? `(${unacked.length}) ` : "") + "FWX · Alert panel";

    $("alerts").innerHTML = list.map((x, i) => `
      <article class="alert ${esc(x.level)}">
        <h3>${esc(x.name)}</h3>
        <div class="meta">Issued ${esc(when(x.issued))}${x.expires ? ` · expires ${esc(when(x.expires))}` : ""}${x.first_seen ? ` · on panel since ${esc(when(x.first_seen))}` : ""}
          ${x.link || cur.area.page ? ` · <a href="${esc(x.link || cur.area.page)}" target="_blank" rel="noopener">ECCC page</a>` : ""}</div>
        <div class="act">${x.ack
          ? `<div class="acked">✔ Acknowledged by ${esc(x.ack.by)}<small>${esc(when(x.ack.at))}${x.ack.issued && x.issued && x.issued !== x.ack.issued ? " · ECCC has updated it since" : ""}</small></div>`
          : `<button class="btn ackbtn" data-i="${i}">Acknowledge</button>`}</div>
        <div class="text" id="t${i}">${esc(x.text || "")}</div>
        ${(x.text || "").length > 300 ? `<button class="more" data-t="t${i}">Show full text</button>` : ""}
      </article>`).join("");
    $("alerts").querySelectorAll(".ackbtn").forEach((b) => b.onclick = () => openAck(list[+b.dataset.i]));
    $("alerts").querySelectorAll(".more").forEach((b) => b.onclick = () => { const t = $(b.dataset.t); t.classList.toggle("open"); b.textContent = t.classList.contains("open") ? "Show less" : "Show full text"; });
  }

  // tone while anything is unacknowledged
  setInterval(() => { if (started && document.body.classList.contains("unacked")) tone(); }, TONE_EVERY);
  let prevUnacked = 0;
  const watchNew = () => {
    const n = document.querySelectorAll(".ackbtn").length;
    if (started && n > prevUnacked) tone();  // sound straight away for a new one
    prevUnacked = n;
  };
  new MutationObserver(watchNew).observe($("alerts"), { childList: true });

  // ---- acknowledging -------------------------------------------------------------------------------
  function openAck(x) {
    ackTarget = x;
    $("ackTitle").textContent = "Acknowledge: " + x.name;
    $("ackErr").textContent = "";
    try { $("ackBy").value = localStorage.getItem("fwx-range-by") || ""; } catch (_) {}
    $("ackDlg").hidden = false; $("ackBy").focus(); $("ackBy").select();
  }
  $("ackCancel").onclick = () => { $("ackDlg").hidden = true; };
  $("ackForm").onsubmit = async (e) => {
    e.preventDefault();
    const by = $("ackBy").value.trim();
    if (!by || !ackTarget) return;
    try { localStorage.setItem("fwx-range-by", by); } catch (_) {}
    if (ackTarget.test) {
      testAcks.t = { by, at: new Date().toISOString() };
      $("ackDlg").hidden = true; render(); return;
    }
    try {
      const r = await fetch("/api/alerts/ack", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ area: areaId, name: ackTarget.name, issued: ackTarget.issued || null, by }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.status);
      $("ackDlg").hidden = true;
      await loadAlerts(); loadLog();
    } catch (err) { $("ackErr").textContent = "Not saved: " + err.message; }
  };

  async function loadLog() {
    try {
      const r = await fetch(`/api/alerts/acklog?area=${encodeURIComponent(areaId)}&limit=8`, { cache: "no-store" });
      const j = await r.json();
      $("acklog").querySelector("tbody").innerHTML = j.acks.length
        ? j.acks.map((a) => `<tr><td>${esc(when(a.at))}</td><td>${esc(a.name)}</td><td>${esc(a.by)}</td></tr>`).join("")
        : `<tr><td colspan="3">None yet for this area.</td></tr>`;
    } catch (_) {}
  }

  // ---- wind and danger -----------------------------------------------------------------------------
  const DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const compass = (d) => DIRS[Math.round(((d % 360) + 360) % 360 / 45) % 8];
  const angDiff = (a, b) => { const d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; };

  async function loadWx() {
    try {
      const r = await fetch(`/api/area/${encodeURIComponent(areaId)}`, { cache: "no-store" });
      const j = await r.json();
      drawWind(j.weather); drawDanger(j.fwi_days || [], j.today);
    } catch (_) { $("windStrip").textContent = "Wind forecast unavailable."; }
  }

  function drawWind(w) {
    if (!w) { $("windStrip").textContent = "Wind forecast unavailable."; $("shift").hidden = true; return; }
    $("windSrc").textContent = w.model;
    const h = w.hourly;
    let i0 = h.time.indexOf(w.now); if (i0 < 0) i0 = 0;
    const idx = []; for (let i = i0; i <= i0 + 12 && i < h.time.length; i++) idx.push(i);
    // first hour in the next 12 where the direction has turned SHIFT_DEG+ within SHIFT_HOURS, with real wind
    let shiftAt = null;
    for (const i of idx) {
      const j = i - SHIFT_HOURS;
      if (j < 0 || h.wd[i] == null || h.wd[j] == null) continue;
      if (angDiff(h.wd[i], h.wd[j]) >= SHIFT_DEG && (h.ws[i] || 0) >= SHIFT_MIN_WS) { shiftAt = i; break; }
    }
    const gmax = Math.max(...idx.map((i) => h.gust[i] || 0));
    const hh = (t) => t.slice(11, 16);
    const box = $("shift");
    if (shiftAt != null) {
      const j = shiftAt - SHIFT_HOURS;
      box.hidden = false;
      box.textContent = `Wind shift forecast about ${hh(h.time[shiftAt])}: ${compass(h.wd[j])} → ${compass(h.wd[shiftAt])}, ${Math.round(h.ws[shiftAt])} km/h gusting ${Math.round(h.gust[shiftAt] || 0)}. Max gust next 12 h: ${Math.round(gmax)} km/h.`;
    } else box.hidden = true;
    $("windStrip").innerHTML = idx.map((i) => {
      const g = h.gust[i] || 0, cls = g >= GUST_VHI ? "vhi" : g >= GUST_HI ? "hi" : "";
      // arrow points the way the wind blows to (wd is where it comes from)
      return `<div class="hr${i === i0 ? " now" : ""}${i === shiftAt ? " shiftmark" : ""}">
        <div class="t">${i === i0 ? "Now" : hh(h.time[i])}</div>
        <div class="arr" style="transform:rotate(${(h.wd[i] || 0) + 180}deg)">↑</div>
        <div class="d">${h.wd[i] == null ? "" : compass(h.wd[i])}</div>
        <div class="ws">${h.ws[i] == null ? "–" : Math.round(h.ws[i])}</div>
        <div class="g ${cls}">G${Math.round(g)}</div></div>`;
    }).join("");
  }

  function drawDanger(rows, today) {
    const names = ["Today", "Tomorrow", "Next day"];
    $("danger").innerHTML = rows.length ? rows.slice(0, 3).map((r, k) => {
      const lab = r.fdr_label || "–", col = DANGER[lab] || "#4a535b";
      return `<div class="day"><div class="lbl">${names[k] || r.date}</div>
        <div class="chip${DARK_TEXT.has(lab) ? " dark" : ""}" style="background:${col}">${esc(lab)}
        <small>FWI ${r.fwi == null ? "–" : Math.round(r.fwi * 10) / 10}${r.fdr_max_label && r.fdr_max_label !== lab ? ` · area max ${esc(r.fdr_max_label)}` : ""}</small></div></div>`;
    }).join("") : "No CWFIS data for this area.";
  }

  // ---- fire detections near the area (FIRMS hotspots, CWFIS reported fires) -------------------------
  const ageH = (t) => (Date.now() - new Date(t)) / 36e5;
  const agoTxt = (t) => { const h = ageH(t); return h < 1.5 ? `${Math.round(h * 60)} min ago` : `${Math.round(h)} h ago`; };
  const AGE_COL = (h) => h < 6 ? "#ff2d2d" : h < 24 ? "#ff9800" : "#ffd54f";

  async function loadFires() {
    let j;
    try { j = await (await fetch(`/api/fires/area/${encodeURIComponent(areaId)}?hours=24`, { cache: "no-store" })).json(); }
    catch (_) { $("fires").textContent = "Fire detections unavailable."; return; }
    const st = j.status || {};
    const recentInside = j.hotspots.some((h) => h.km === 0 && ageH(h.time) < 6);
    $("firesCard").classList.toggle("hot", recentInside);
    const rows = j.hotspots.slice(0, 6).map((h) => `<tr><td class="age"><span class="dot" style="background:${AGE_COL(ageH(h.time))}"></span>${esc(agoTxt(h.time))}</td>
        <td>${h.km === 0 ? "<b>inside area</b>" : `${h.km} km out`}</td><td>${esc(h.sat)}${h.conf ? ` · ${esc(h.conf)}` : ""}${h.frp != null ? ` · ${h.frp} MW` : ""}</td></tr>`).join("");
    const rep = j.reported.slice(0, 4).map((f) => `<tr><td class="age">▲ ${f.km === 0 ? "inside" : `${f.km} km`}</td>
        <td>${esc(f.name)}${f.agency ? ` (${esc(f.agency)})` : ""}</td><td>${esc(f.stage || "")}${f.ha != null ? ` · ${f.ha} ha` : ""}</td></tr>`).join("");
    const notes = [];
    if (!st.firms?.enabled) notes.push("Satellite detections are off: the FIRMS key isn't set on the server.");
    else if (st.firms.error) notes.push("FIRMS isn't answering; showing the last detections received.");
    else if (st.firms.checked) notes.push(`FIRMS checked ${hm(new Date(st.firms.checked))}.`);
    if (st.reported?.error) notes.push("CWFIS reported fires unavailable.");
    notes.push(`Hotspots within ${j.km} km; reported fires within ${Math.max(j.km, 50)} km. A hotspot can be a range fire, a burn or a flare.`);
    $("fires").innerHTML = `
      <div class="fcount">
        <div><b class="${j.inside ? "hot" : ""}">${j.inside}</b><span>inside area</span></div>
        <div><b>${j.nearby}</b><span>within ${j.km} km</span></div>
        <div><b>${j.reported.length}</b><span>reported fires</span></div>
      </div>
      <div class="flist">${rows || rep ? `<table>${rows}${rep}</table>` : "No detections in the last 24 h."}
        ${j.latest ? `<div class="muted">Latest detection ${esc(when(j.latest))}</div>` : ""}</div>
      <div class="fnote">${esc(notes.join(" "))}</div>`;
  }

  // ---- go ------------------------------------------------------------------------------------------
  (async () => {
    try { await loadAreas(); } catch (_) {}
    loadAlerts(); loadWx(); loadLog(); loadFires();
    setInterval(loadAlerts, ALERT_EVERY);
    setInterval(loadFires, 5 * 60e3);
    setInterval(() => { loadWx(); loadLog(); }, WX_EVERY);
    setInterval(loadLog, ALERT_EVERY);
  })();
})();
