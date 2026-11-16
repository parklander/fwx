/* CWFIS fire weather forecast: area, date and layer pickers over a map of the CWFIS daily grids */
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const status = (t) => { $("status").textContent = t || ""; };
  const iso = (d) => d.toISOString().slice(0, 10);
  const today = iso(new Date(Date.now() - new Date().getTimezoneOffset() * 60000));
  const prefs = (() => { try { return JSON.parse(localStorage.getItem("cwfis-prefs") || "{}"); } catch (_) { return {}; } })();
  const save = () => { try { localStorage.setItem("cwfis-prefs", JSON.stringify({ aoi: $("aoi").value, layer: $("layer").value })); } catch (_) {} };

  const map = L.map("map", { zoomControl: true }).setView([55, -97], 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "© OpenStreetMap contributors · Fire weather © NRCan CWFIS",
  }).addTo(map);

  // ---- areas -----------------------------------------------------------------------------------
  const { default: defAoi, areas } = await (await fetch("/api/areas")).json();
  const byId = Object.fromEntries(areas.map((a) => [a.id, a]));
  $("aoi").innerHTML = areas.map((a) => `<option value="${a.id}">${a.name}${a.prov ? ` (${a.prov})` : ""}</option>`).join("");
  const linked = new URLSearchParams(location.search).get("aoi");  // e.g. from a notification
  $("aoi").value = byId[linked] ? linked : byId[prefs.aoi] ? prefs.aoi : defAoi || areas[0].id;
  // every area: its box (or a dot where it has none) with a label; click one to select it
  const areaLayer = L.layerGroup().addTo(map);
  const shapes = {};
  const styleFor = (on) => on ? { color: "#e8590c", weight: 3, fill: true, fillColor: "#e8590c", fillOpacity: 0.12 }
                              : { color: "#111", weight: 1.5, fill: true, fillColor: "#111", fillOpacity: 0.06 };
  for (const a of areas) {
    const g = L.featureGroup().addTo(areaLayer);
    if (a.bbox) {
      const [w, s, e, n] = a.bbox;
      L.rectangle([[s, w], [n, e]], styleFor(false)).addTo(g);
    }
    // the dot keeps small areas visible at country scale
    L.circleMarker([a.lat, a.lon], { radius: 4, color: "#111", weight: 1.5, fillColor: "#fff", fillOpacity: 1 }).addTo(g)
      .bindTooltip(a.name, { permanent: true, interactive: true, direction: "right", offset: [6, 0], className: "aoi-label" });
    g.on("click", () => { $("aoi").value = a.id; $("aoi").onchange(); });
    // a label hidden to avoid overlap shows while the pointer is on its dot or box
    g.on("mouseover", () => g.eachLayer((x) => {
      const el = x.getTooltip?.()?.getElement();
      if (el) { el.style.visibility = "visible"; el.style.zIndex = 1000; }
    }));
    g.on("mouseout", () => { g.eachLayer((x) => { const el = x.getTooltip?.()?.getElement(); if (el) el.style.zIndex = ""; }); declutter(); });
    shapes[a.id] = g;
  }
  // southern Canada, coast to coast, where the areas are
  const CANADA = L.latLngBounds([[42, -139], [61, -52.6]]);
  function showCountry() { map.fitBounds(CANADA); }

  // Hide labels that would overlap one already shown (the selected area's goes first); zooming in brings them back
  function declutter() {
    const sel = $("aoi").value;
    const order = [sel, ...areas.map((a) => a.id).filter((id) => id !== sel)];
    const shown = [];
    for (const id of order) {
      let el = null;
      shapes[id].eachLayer((x) => { el = x.getTooltip?.()?.getElement() || el; });
      if (!el) continue;
      el.style.visibility = "visible";
      el.style.marginLeft = "";
      const hits = (r) => shown.some((o) => r.left < o.right + 2 && r.right > o.left - 2 && r.top < o.bottom + 1 && r.bottom > o.top - 1);
      let r = el.getBoundingClientRect();
      if (hits(r)) {
        el.style.marginLeft = `${-(r.width + 14)}px`;  // try the left side of the dot
        r = el.getBoundingClientRect();
      }
      if (hits(r)) el.style.visibility = "hidden"; else shown.push(r);
    }
  }
  map.on("zoomend moveend", declutter);
  function showArea(fly) {
    const id = $("aoi").value, a = byId[id];
    for (const [k, g] of Object.entries(shapes)) {
      const on = k === id;
      g.eachLayer((x) => {
        if (x instanceof L.Rectangle) x.setStyle(styleFor(on));
        else x.getTooltip()?.getElement()?.classList.toggle("on", on);
      });
      if (on) g.bringToFront();
    }
    declutter();
    if (fly) {
      // keep the area clear of the forecast panel at the bottom
      const pad = window.fwxSheet?.padding() || { left: 0, bottom: 0 };
      const box = a.bbox ? L.latLngBounds([[a.bbox[1], a.bbox[0]], [a.bbox[3], a.bbox[2]]])
                         : L.latLng(a.lat, a.lon).toBounds(20000);
      map.fitBounds(box.pad(a.bbox ? 1.5 : 0), { maxZoom: 10, paddingTopLeft: [pad.left + 10, 10], paddingBottomRight: [10, pad.bottom + 10] });
    }
  }
  // tell the forecast panel which area was picked
  const openPanel = () => document.dispatchEvent(new CustomEvent("area-selected", { detail: { id: $("aoi").value, name: byId[$("aoi").value].name } }));

  // ---- layers and dates ------------------------------------------------------------------------
  let info;
  try {
    const r = await fetch("/api/layers");
    if (!r.ok) throw new Error((await r.json()).detail);
    info = await r.json();
  } catch (e) {
    status(`CWFIS layers unavailable: ${e.message || e}`);
    info = { wms: "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wms", layers: [] };
  }
  const layerById = Object.fromEntries(info.layers.map((l) => [l.id, l]));
  $("layer").innerHTML = info.layers.map((l) => `<option value="${l.id}">${l.title}</option>`).join("");
  $("layer").value = layerById[prefs.layer] ? prefs.layer : info.layers.some((l) => l.id === "fwi") ? "fwi" : info.layers[0]?.id;

  function setDateRange() {
    const l = layerById[$("layer").value];
    if (!l) return;
    $("date").min = l.start; $("date").max = l.end;
    if (!$("date").value) $("date").value = today <= l.end ? today : l.end;
    if ($("date").value > l.end) $("date").value = l.end;
    if ($("date").value < l.start) $("date").value = l.start;
  }

  let wms = null;
  function showLayer() {
    const l = layerById[$("layer").value];
    if (!l) return;
    setDateRange();
    const time = $("date").value;
    if (wms) map.removeLayer(wms);
    wms = L.tileLayer.wms(info.wms, {
      layers: l.id, format: "image/png", transparent: true, version: "1.3.0", time, opacity: 0.7,
      attribution: "NRCan CWFIS",
    }).addTo(map);
    wms.on("loading", () => status("Loading map…"));
    wms.on("load", () => status(""));
    $("legend-title").textContent = l.title;
    $("legend-img").src = `${info.wms}?service=WMS&version=1.3.0&request=GetLegendGraphic&format=image/png&layer=${l.id}`;
    $("legend-date").textContent = `${time}${time >= today ? " (forecast)" : " (observed)"}`;
    $("legend").hidden = false;
    areaLayer.eachLayer((g) => g.bringToFront());
    shapes[$("aoi").value]?.bringToFront();
  }

  function updatePdfLink() {
    const d = $("date").value || today;
    const start = iso(new Date(Date.parse(d) - 86400e3));
    $("pdf").href = `/api/forecast.pdf?aoi=${encodeURIComponent($("aoi").value)}&start=${start}`;
  }

  $("aoi").onchange = () => { save(); openPanel(); showArea(true); updatePdfLink(); };
  $("country").onclick = showCountry;
  $("layer").onchange = () => { save(); showLayer(); };
  $("date").onchange = () => { showLayer(); updatePdfLink(); };
  $("pdf").onclick = () => status("Building the PDF — CWFIS takes 10–30 s for a full forecast…");
  window.addEventListener("focus", () => { if ($("status").textContent.startsWith("Building")) status(""); });

  // ---- ECCC alerts ----------------------------------------------------------------------------------
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const RANK = { warning: 3, watch: 2, statement: 1 };
  const worst = (list) => list.reduce((w, x) => (RANK[x.level] > RANK[w] ? x.level : w), "statement");
  const when = (t) => new Date(t).toLocaleString("en-CA", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  let ALERTS = null, alertsOpen = false;

  function markAlertedAreas() {
    const by = Object.fromEntries((ALERTS?.areas || []).map((a) => [a.id, a.alerts]));
    for (const a of areas) shapes[a.id].eachLayer((x) => {
      const tt = x.getTooltip?.();
      if (!tt) return;
      const list = by[a.id] || [];
      tt.setContent(list.length ? `⚠ ${esc(a.name)}` : esc(a.name));
      const el = tt.getElement();
      if (el) {
        el.classList.toggle("alert", list.length > 0);
        el.classList.remove("lvl-warning", "lvl-watch", "lvl-statement");
        if (list.length) el.classList.add(`lvl-${worst(list)}`);
      }
    });
    declutter();
  }

  function renderAlerts() {
    if (!ALERTS) return;
    const active = ALERTS.areas.filter((a) => a.alerts.length);
    const chip = $("alert-chip");
    chip.className = "chip" + (active.length ? ` on lvl-${worst(active.flatMap((a) => a.alerts))}` : "");
    chip.innerHTML = active.length ? `⚠<span class="long"> Alerts:</span> ${active.length}<span class="long"> area${active.length > 1 ? "s" : ""}</span>`
                                   : `<span class="long">No alerts</span><span class="short">✓ 0</span>`;
    markAlertedAreas();
    const panel = $("alerts");
    if (!alertsOpen) { panel.hidden = true; return; }
    const sel = ALERTS.areas.find((a) => a.id === $("aoi").value);
    const mine = sel?.alerts || [];
    const item = (x) => `<div class="wx-alert lvl-${x.level}"><div class="n">${esc(x.name)}</div>
      <div class="sub">${x.issued ? `Issued ${when(x.issued)}` : ""}${x.expires ? ` · expires ${when(x.expires)}` : ""}</div>
      ${x.text ? `<div class="t">${esc(x.text)}</div>` : ""}
      ${x.link || sel?.page ? `<div class="sub"><a href="${esc(x.link || sel.page)}" target="_blank" rel="noopener">Full text on weather.gc.ca ↗</a></div>` : ""}</div>`;
    const others = active.filter((a) => a !== sel);
    panel.innerHTML = `<h2><span>ECCC alerts · ${esc(byId[$("aoi").value]?.name)}</span><button class="x" id="al-close" aria-label="Close">×</button></h2>
      ${mine.length ? mine.map(item).join("") : `<div class="sub">No alerts in effect${sel?.region ? ` for ${esc(sel.region)}` : ""}.
        ${sel?.page ? `<a href="${esc(sel.page)}" target="_blank" rel="noopener">Check weather.gc.ca ↗</a>` : ""}</div>`}
      ${sel?.error ? `<div class="sub">Last check failed; showing the previous result.</div>` : ""}
      ${others.length ? `<div class="others"><div class="sub">Other areas</div>${others.map((a) =>
        `<button data-id="${esc(a.id)}" class="lvl-${worst(a.alerts)}"><b>⚠</b> ${esc(a.name)}: ${esc(a.alerts.map((x) => x.name).join(", "))}</button>`).join("")}</div>` : ""}
      <div class="sub" style="margin-top:8px">Checked every ${ALERTS.poll_min} min${sel?.checked ? `, last at ${new Date(sel.checked).toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false })}` : ""}.</div>`;
    panel.hidden = false;
    $("al-close").onclick = () => { alertsOpen = false; panel.hidden = true; };
    panel.querySelectorAll(".others button").forEach((b) => (b.onclick = () => { $("aoi").value = b.dataset.id; $("aoi").onchange(); }));
  }
  async function loadAlerts() {
    try { ALERTS = await (await fetch("/api/alerts")).json(); } catch (_) { return; }
    renderAlerts();
  }
  $("alert-chip").onclick = () => { alertsOpen = !alertsOpen; $("notify").hidden = true; closeSettings(); renderAlerts(); };
  const aoiChange = $("aoi").onchange;
  $("aoi").onchange = () => { aoiChange(); renderAlerts(); };
  setInterval(loadAlerts, 2 * 60e3);

  // ---- push notifications (work with the page closed) ------------------------------------------------
  const pushable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const b64 = (s) => Uint8Array.from(atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    return d;
  };
  let swReg = null, mySub = { subscribed: false, areas: [] };
  async function pushState() {
    if (!pushable) return;
    swReg = swReg || await navigator.serviceWorker.register("/sw.js");
    const sub = await swReg.pushManager.getSubscription();
    mySub = sub ? await post("/api/push/status", { endpoint: sub.endpoint }).catch(() => ({ subscribed: false, areas: [] }))
                : { subscribed: false, areas: [] };
    $("bell").innerHTML = mySub.subscribed ? `🔔<span class="long"> Notifications on</span>` : `🔔<span class="long"> Notify me of alerts</span>`;
    $("bell").classList.toggle("on", mySub.subscribed);
  }
  function renderNotify(msg) {
    const p = $("notify");
    const ios = /iPhone|iPad/.test(navigator.userAgent) && !navigator.standalone;
    if (!pushable) {
      p.innerHTML = `<h2><span>Notifications</span><button class="x" id="nt-close">×</button></h2>
        <div class="sub">${ios ? "On iPhone or iPad, tap Share → <b>Add to Home Screen</b>, then open the site from the Home Screen icon and turn notifications on there."
          : "This browser can't receive web notifications."}</div>`;
    } else {
      const all = mySub.areas === "all";
      const chosen = new Set(all ? [] : mySub.areas || []);
      if (!mySub.subscribed) chosen.add($("aoi").value);
      p.innerHTML = `<h2><span>Notify me of new ECCC alerts</span><button class="x" id="nt-close">×</button></h2>
        <div class="sub">A notification on this device whenever ECCC issues a new warning, watch or statement for these areas, even with this page closed.</div>
        <label style="display:block;margin-top:8px"><input type="checkbox" id="nt-all"${all ? " checked" : ""}> <b>All areas</b></label>
        <div class="areas"${all ? ' style="opacity:.4;pointer-events:none"' : ""}>${areas.map((a) =>
          `<label><input type="checkbox" value="${esc(a.id)}"${chosen.has(a.id) ? " checked" : ""}> ${esc(a.name)}</label>`).join("")}</div>
        <div class="row"><button class="primary" id="nt-save">${mySub.subscribed ? "Save" : "Turn on"}</button>
          ${mySub.subscribed ? `<button id="nt-test">Send a test</button><button id="nt-off">Turn off</button>` : ""}</div>
        ${msg ? `<div class="sub" style="margin-top:8px">${esc(msg)}</div>` : ""}`;
      $("nt-all").onchange = (e) => { const box = p.querySelector(".areas"); box.style.opacity = e.target.checked ? ".4" : ""; box.style.pointerEvents = e.target.checked ? "none" : ""; };
      $("nt-save").onclick = () => turnOn().catch((e) => renderNotify(`Couldn't turn on notifications: ${e.message}`));
      if ($("nt-test")) $("nt-test").onclick = async () => {
        const sub = await swReg.pushManager.getSubscription();
        post("/api/push/test", { endpoint: sub.endpoint }).then(() => renderNotify("Test sent; it should appear in a few seconds."), (e) => renderNotify(e.message));
      };
      if ($("nt-off")) $("nt-off").onclick = turnOff;
    }
    $("nt-close").onclick = () => { p.hidden = true; };
    p.hidden = false;
  }
  async function turnOn() {
    const p = $("notify");
    const chosen = $("nt-all").checked ? "all" : [...p.querySelectorAll(".areas input:checked")].map((i) => i.value);
    if (chosen !== "all" && !chosen.length) return renderNotify("Pick at least one area.");
    if (await Notification.requestPermission() !== "granted") return renderNotify("Notifications are blocked for this site; allow them in the browser's site settings.");
    let sub = await swReg.pushManager.getSubscription();
    if (!sub) {
      const { key } = await (await fetch("/api/push/key")).json();
      sub = await swReg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
    }
    mySub = await post("/api/push/subscribe", { subscription: sub.toJSON(), areas: chosen });
    await pushState();
    renderNotify(chosen === "all" ? "On for all areas." : `On for ${chosen.length} area${chosen.length > 1 ? "s" : ""}.`);
  }
  async function turnOff() {
    const sub = await swReg.pushManager.getSubscription();
    if (sub) { await post("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
    await pushState();
    renderNotify("Notifications are off on this device.");
  }
  $("bell").onclick = async () => {
    if (!$("notify").hidden) { $("notify").hidden = true; return; }
    alertsOpen = false; $("alerts").hidden = true; closeSettings();
    await pushState().catch(() => {});
    renderNotify();
  };
  pushState().catch(() => {});

  // ---- phones: ⚙ opens Date, Layer and All of Canada; the legend collapses on tap ------------------
  const phone = window.matchMedia("(max-width: 700px)");
  function closeSettings() { document.body.classList.remove("settings-open"); $("gear").classList.remove("on"); $("gear").setAttribute("aria-expanded", "false"); }
  $("gear").onclick = () => {
    const open = !document.body.classList.contains("settings-open");
    if (open) {
      document.documentElement.style.setProperty("--hdr", `${document.querySelector("header").getBoundingClientRect().bottom + 6}px`);
      alertsOpen = false; $("alerts").hidden = true; $("notify").hidden = true;
    }
    document.body.classList.toggle("settings-open", open);
    $("gear").classList.toggle("on", open);
    $("gear").setAttribute("aria-expanded", String(open));
  };
  map.on("click", closeSettings);
  $("country").addEventListener("click", () => { if (phone.matches) closeSettings(); });
  $("legend").onclick = () => $("legend").classList.toggle("collapsed");
  if (phone.matches) $("legend").classList.add("collapsed");
  // the header's height changes with the window; keep the map filling the rest
  window.addEventListener("resize", () => map.invalidateSize());

  showCountry();
  showArea(false);
  showLayer();
  updatePdfLink();
  loadAlerts();
  if (linked) { openPanel(); showArea(true); }
})();
