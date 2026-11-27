/* FWX beta: base status first (see the interface review, Nov 2026).
 *
 * One statement for the base (Normal / Heightened / High, from /api/status, draft rules), the items behind it,
 * and one 48 h timeline on a single time axis: the CWFIS daily danger class, the calculated hourly FWI, wind coloured
 * as on the map, gusts, RH, precipitation, smoke and alert periods. A small map shows the area and hotspots.
 * The main site holds the full map, burn windows and the PDF; links pass the base along (?aoi=).
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const MAIN = location.hostname.startsWith("fwx-beta.") ? "https://fwx.rtams.ca" : "";  // same server under /beta
  const DANGER = { Low: "#2f6fd6", Moderate: "#3c9a3f", High: "#e8c400", "Very High": "#f08a00", Extreme: "#d32f2f" };
  // the map's arrow colours (windlayer.js RAMP), km/h
  const RAMP = [[5, "#2bc6ee"], [10, "#1e8cf0"], [15, "#2436d8"], [20, "#a4ea3a"], [25, "#1fc21f"], [30, "#3a9a3a"],
    [35, "#f2ee00"], [40, "#f5c400"], [45, "#f59800"], [50, "#f02a1a"], [55, "#cc0000"], [60, "#960000"],
    [65, "#c45cf2"], [70, "#a800ee"], [80, "#6e1fa8"], [Infinity, "#111111"]];
  const windCol = (s) => RAMP.find(([t]) => s < t)[1];
  // the EER plume levels, µg/m³
  // crossover tiers, as on the old crossover charts: 1 crossover, 2 30/30, 3 30/30/30
  const XO = { 1: ["#fff3a8", "Crossover"], 2: ["#ffcf8a", "30/30"], 3: ["#f4a3a3", "30/30/30"] };
  const SMOKE = [[5, null], [10, "#cfe0ee"], [25, "#a9c9e2"], [35, "#d2c38a"], [50, "#d9a54a"], [75, "#d9822f"],
    [100, "#d0582a"], [200, "#b52d2a"], [Infinity, "#5e1550"]];
  const smokeCol = (v) => (v == null ? null : SMOKE.find(([t]) => v < t)[1]);
  const ALERT_COL = { warning: "#b3261e", watch: "#e07b00", statement: "#7b1fa2" };
  // light tint behind temperatures: blue below 0 °C, clear around 10 °C, warming to red above 25 °C
  // one-hue ramp for the hourly FWI number: no class colours, so it doesn't read as a danger rating
  const fwiTint = (v) => `rgba(235,104,52,${Math.min(0.6, 0.06 + v / 50).toFixed(2)})`;
  const tempTint = (t) => t < 0 ? `rgba(70,130,220,${Math.min(0.35, 0.1 + -t * 0.012)})`
    : t < 10 ? "transparent" : `rgba(230,${Math.max(60, 170 - (t - 10) * 6)},40,${Math.min(0.4, 0.06 + (t - 10) * 0.018)})`;
  const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const compass = (d) => (d == null ? "" : COMPASS[Math.round((d % 360) / 45) % 8]);

  let areas = [], areaId = null, S = null, sel = 0, map = null, boxLayer = null, hotLayer = null;
  try { areaId = new URLSearchParams(location.search).get("aoi") || localStorage.getItem("fwx-beta-base"); } catch (_) {}

  // ---- time formatting in the base's own time zone ----------------------------------------------------
  const tz = () => S?.timeline?.timezone || undefined;
  const fmt = (iso, o) => new Date(iso).toLocaleString("en-CA", { timeZone: tz(), hour12: false, ...o });
  const hm = (iso) => fmt(iso, { hour: "2-digit", minute: "2-digit" });
  const dayHm = (iso) => fmt(iso, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const when = (iso) => {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toDateString() === new Date().toDateString() ? `today ${hm(iso)}` : dayHm(iso);
  };

  // ---- base list (grouped by province, west to east) -----------------------------------------------------
  const PROV = ["BC", "YT", "AB", "NT", "SK", "MB", "NU", "ON", "QC", "NB", "PE", "NS", "NL"];
  const sortKey = (n) => n.replace(/^(CFB|CFS|CFD|BFC|CF Camp|Camp|Det|NDHQ|\d+ Wing)\s+/i, "").toLowerCase();
  async function loadAreas() {
    const j = await (await fetch("/api/areas")).json();
    areas = j.areas || j;
    const groups = {};
    for (const a of areas) (groups[a.prov || "Other"] ||= []).push(a);
    const order = Object.keys(groups).sort((x, y) => ((PROV.indexOf(x) + 1) || 99) - ((PROV.indexOf(y) + 1) || 99));
    $("base").innerHTML = `<option value="" disabled>Choose your base…</option>` + order.map((p) =>
      `<optgroup label="${esc(p)}">${groups[p].sort((x, y) => sortKey(x.name).localeCompare(sortKey(y.name)))
        .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</optgroup>`).join("");
    if (!areas.some((a) => a.id === areaId)) areaId = null;
    $("base").value = areaId || "";
    $("base").onchange = () => {
      areaId = $("base").value;
      try { localStorage.setItem("fwx-beta-base", areaId); } catch (_) {}
      const u = new URL(location.href); u.searchParams.set("aoi", areaId); history.replaceState(null, "", u);
      sel = 0; load();
    };
  }

  // ---- the status ------------------------------------------------------------------------------------
  let beaconed = false;
  const TRK = window.FWXTrack || { init() {}, send() {}, act() {} };
  TRK.init("beta", () => ({ aoi: areaId }));
  const beacon = (ev) => TRK.send(ev);
  ["lnk-map", "lnk-burn", "lnk-pdf", "lnk-panel"].forEach((id) => { const a = document.getElementById(id); if (a) a.addEventListener("click", () => TRK.act(id.replace("lnk-", "link_"))); });
  async function load() {
    beacon(beaconed ? "base" : "view"); beaconed = true;
    if (!areaId) {
      $("status").className = "status lvl-x"; $("st-word").textContent = "Choose your base";
      $("st-head").textContent = "Pick it in the list above; this browser remembers it."; return;
    }
    const q = encodeURIComponent(areaId);
    $("lnk-map").href = `${MAIN}/?aoi=${q}`;
    $("lnk-burn").href = `${MAIN}/?aoi=${q}&tab=rx`;
    $("lnk-pdf").href = `${MAIN}/api/forecast.pdf?aoi=${q}`;
    $("lnk-panel").href = `${MAIN}/range?aoi=${q}`;
    try {
      const pv = new URLSearchParams(location.search).get("season");
      const r = await fetch(`/api/status/${q}${pv ? `?season=${encodeURIComponent(pv)}` : ""}`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      S = await r.json();
    } catch (_) {
      $("updated").textContent = "Couldn't refresh; showing the last status";
      if (!S) { $("st-word").textContent = "Unavailable"; $("st-head").textContent = "FWX couldn't build the status. Try again in a minute."; }
      return;
    }
    render();
  }

  function render() {
    $("status").className = `status lvl-${S.level}`;
    $("st-word").textContent = S.label;
    $("st-head").textContent = S.headline;
    $("st-next").innerHTML = S.next ? `Next: <b>${esc(S.next.title)}</b>${esc(when(S.next.at))}` : "";
    const se = S.season;
    if (se) {
      const sn = se.snow;
      const NAME = { hrdps: "HRDPS", caldas: "CaLDAS" };
      const src = (k) => { const x = sn.sources?.[k]; return x ? `${NAME[k]} ${x.mean_cm} cm, ${x.covered_pct}% covered${k === sn.source ? " (used)" : ""}` : `${NAME[k]} unavailable`; };
      const snowLine = !sn ? "Snow cover unavailable right now"
        : `Snow: ${src(sn.source)} · ${src(sn.source === "hrdps" ? "caldas" : "hrdps")}`
          + (sn.stations?.length ? ` · measured at ${sn.stations.slice(0, 3).map((x) => `${esc(x.name)} ${x.snow} cm`).join(", ")}` : "")
          + (sn.disagree ? ` · <b>the models disagree</b>; using the one ${esc(sn.basis.replace(/^closest/, "closer"))}` : "")
          + (sn.trend_cm_3d != null ? ` · ${sn.trend_cm_3d > 0 ? "+" : ""}${sn.trend_cm_3d} cm in 3 days` : "");
      $("st-season").className = `st-season s-${se.mode}`;
      $("st-season").innerHTML = `<span class="chip">${esc(se.icon)} ${esc(se.label)}</span> ${esc(se.text)}`
        + (se.preview ? ` <b class="pv">Preview of this mode; the rest of the page is live.</b>` : `<div class="sn">${snowLine}</div>`);
    }
    const gaps = Object.entries(S.sources_ok || {}).filter(([, ok]) => !ok).map(([k]) => k.toUpperCase());
    $("updated").textContent = `Updated ${S.fetched ? hm(S.fetched) : ""}` + (gaps.length ? ` · missing: ${gaps.join(", ")}` : "");
    document.title = `${S.label} · ${S.area.name} · FWX beta`;
    renderAttention();
    renderTimeline();
    renderMap();
  }

  function renderAttention() {
    const items = S.items.filter((x) => x.level > 0 || x.kind === "fire");
    $("attention").innerHTML = items.length ? items.map((x) => `<li class="l${x.level}"><span class="dot"></span>
        <span class="t">${esc(x.title)}</span>
        <span class="d">${esc(x.detail)}${x.at && !isNaN(new Date(x.at)) && x.at.length > 10 ? ` · ${esc(x.kind === "alert" ? "issued " : "")}${esc(when(x.at))}` : ""}${x.until ? ` · until ${esc(when(x.until))}` : ""}</span></li>`).join("")
      : `<li class="empty">Nothing needs attention for this base in the next 48 h.</li>`;
  }

  // ---- the timeline ----------------------------------------------------------------------------------
  function renderTimeline() {
    const T = S.timeline;
    if (!T) { $("timeline").innerHTML = "<p>The hourly forecast isn't available right now.</p>"; return; }
    const n = T.time.length;
    $("timeline").style.setProperty("--n", n);
    $("tl-tz").textContent = `times ${new Intl.DateTimeFormat("en-CA", { timeZone: tz(), timeZoneName: "short" }).formatToParts(new Date()).find((p) => p.type === "timeZoneName").value}`;
    const hr = (i) => +T.local[i].slice(11, 13);
    const dayStart = (i) => i > 0 && hr(i) === 0;
    const cls = (i, extra = "") => `c${i === 0 ? " now" : ""}${dayStart(i) ? " day" : ""}${i === sel ? " sel" : ""}${T.shifts.includes(i) ? " shift" : ""} ${extra}`;
    const cell = (i, html, extra = "", style = "") => `<div class="${cls(i, extra)}" data-i="${i}" style="${style}">${html}</div>`;
    const row = (label, sub, f) => `<div class="lab">${label}${sub ? `<small>${sub}</small>` : ""}</div>` + T.time.map((_, i) => f(i)).join("");
    const pmax = Math.max(1, ...T.precip.map((v) => v || 0));

    // alert periods: one coloured run per alert across the hours it covers
    const alerts = S.items.filter((x) => x.kind === "alert");
    const alertAt = (i) => {
      const t = new Date(T.time[i]);
      return alerts.find((a) => (!a.at || new Date(a.at) <= t) && (!a.until || new Date(a.until) >= t));
    };

    let html = `<div class="lab"></div>` + T.time.map((_, i) => cell(i,
      `${i === 0 ? "Now" : String(hr(i)).padStart(2, "0")}<span class="dn">${i === 0 || dayStart(i) ? fmt(T.time[i], { weekday: "short" }) : ""}</span>`, "hd")).join("");
    html += row("Fire danger", "CWFIS daily", (i) => {
      const d = T.danger[i];
      return cell(i, "", "", d ? `background:${DANGER[d]}` : "");
    });
    html += row("FWI", "hourly, calc.", (i) => {
      const v = T.fwi[i];
      return cell(i, v == null ? "" : Math.round(v), "", v == null ? "" : `background:${fwiTint(v)}`);
    });
    html += row("Temperature", "°C", (i) => {
      const v = T.temp[i];
      return cell(i, v == null ? "" : Math.round(v), "", v == null ? "" : `background:${tempTint(v)}`);
    });
    html += row("Humidity", "%", (i) => {
      const v = T.rh[i];
      return cell(i, v == null ? "" : Math.round(v), v != null && v <= S.rules.rh_low ? "rhlow" : "");
    });
    if (T.xover) html += row("Crossover", "RH&lt;T · 30/30 · 30/30/30", (i) => {
      const x = T.xover[i];
      return cell(i, x >= 2 ? (x === 3 ? "30³" : "30²") : "", x ? "xo" : "", x ? `background:${XO[x][0]}` : "");
    });
    html += row("Wind", "km/h", (i) => {
      const s = T.ws[i], d = T.wd[i];
      return cell(i, s == null ? "" : `<span><span class="arr" style="color:${windCol(s)};transform:rotate(${(d || 0) + 180}deg)">↑</span><br>${Math.round(s)}</span>`);
    });
    html += row("Gusts", "km/h", (i) => {
      const g = T.gust[i];
      return cell(i, g == null ? "" : Math.round(g), g >= S.rules.gust_high ? "g2" : g >= S.rules.gust_heightened ? "g1" : "");
    });
    html += row("Precip", "mm", (i) => {
      const v = T.precip[i] || 0;
      return cell(i, v > 0 ? `<div class="pbar" style="height:${Math.max(3, (v / pmax) * 20)}px" title="${v} mm"></div>` : "");
    });
    html += row("Smoke", "PM2.5", (i) => {
      const c = smokeCol(T.smoke[i]);
      return cell(i, "", "", c ? `background:${c}` : "");
    });
    // alerts: one labelled bar per alert across the hours it covers (grid-column span), gaps as empty cells
    html += `<div class="lab">Alerts<small>ECCC</small></div>`;
    for (let i = 0; i < n;) {
      const a = alertAt(i);
      let k = 1;
      while (i + k < n && alertAt(i + k) === a) k++;
      html += a
        ? `<div class="c alert${dayStart(i) ? " day" : ""}" data-i="${i}" style="grid-column:span ${k};background:${ALERT_COL[a.alert_level] || "#7b1fa2"}" title="${esc(a.title)}">${esc(a.title)}</div>`
        : Array.from({ length: k }, (_, j) => cell(i + j, "")).join("");
      i += k;
    }
    $("timeline").innerHTML = html;
    $("timeline").querySelectorAll(".c").forEach((el) => el.onclick = () => { TRK.act("timeline_hour"); sel = +el.dataset.i; renderTimeline(); });
    readout();
  }

  function readout() {
    const T = S.timeline, i = sel;
    const parts = [`<b>${esc(i === 0 ? "Now" : dayHm(T.time[i]))}</b>`];
    if (T.danger[i]) parts.push(`fire danger ${esc(T.danger[i])} (CWFIS daily)`);
    if (T.fwi[i] != null) parts.push(`FWI ${T.fwi[i].toFixed(0)} (hourly, calc.)`);
    if (T.ws[i] != null) parts.push(`wind ${compass(T.wd[i])} ${Math.round(T.ws[i])} km/h, gusts ${Math.round(T.gust[i] ?? T.ws[i])}`);
    if (T.rh[i] != null) parts.push(`RH ${Math.round(T.rh[i])}%`);
    if (T.temp[i] != null) parts.push(`${Math.round(T.temp[i])}°C`);
    parts.push(T.precip[i] ? `precip ${T.precip[i]} mm` : "no precip");
    if (T.smoke[i] != null) parts.push(`smoke ${Math.round(T.smoke[i])} µg/m³`);
    if (T.shifts.includes(i)) parts.push("<b>wind shift</b>");
    if (T.xover?.[i]) parts.push(`<b>${XO[T.xover[i]][1]}</b>`);
    $("readout").innerHTML = parts.join(" · ");
  }

  // ---- the small map -----------------------------------------------------------------------------------
  async function renderMap() {
    const a = S.area;
    if (!map) {
      map = L.map("map", { zoomControl: true, attributionControl: true });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 15, attribution: "© OpenStreetMap" }).addTo(map);
    }
    if (boxLayer) map.removeLayer(boxLayer);
    const b = a.bbox ? [[a.bbox[1], a.bbox[0]], [a.bbox[3], a.bbox[2]]] : null;
    boxLayer = b ? L.rectangle(b, { color: "#eb6834", weight: 2, fill: false }) : L.circleMarker([a.lat, a.lon], { radius: 6, color: "#eb6834" });
    boxLayer.addTo(map);
    if (b) map.fitBounds(L.latLngBounds(b).pad(0.6)); else map.setView([a.lat, a.lon], 9);
    // hotspots around the base, last 24 h
    const vb = map.getBounds().pad(0.5);
    try {
      const j = await (await fetch(`/api/fires?hours=24&reported=false&bbox=${[vb.getWest(), vb.getSouth(), vb.getEast(), vb.getNorth()].map((v) => v.toFixed(3)).join(",")}`)).json();
      if (hotLayer) map.removeLayer(hotLayer);
      hotLayer = L.layerGroup(j.features.map((f) => {
        const [lon, lat] = f.geometry.coordinates, h = (Date.now() - new Date(f.properties.time)) / 36e5;
        return L.circleMarker([lat, lon], { radius: 5, weight: 1, color: "#3a0000", fillColor: h < 6 ? "#ff2d2d" : "#ff9800", fillOpacity: 0.95 })
          .bindPopup(`Satellite hotspot · ${esc(when(f.properties.time))} · ${esc(f.properties.sat)}`);
      })).addTo(map);
    } catch (_) {}
  }

  // ---- go --------------------------------------------------------------------------------------------
  (async () => {
    try { await loadAreas(); } catch (_) {}
    load();
    setInterval(load, 10 * 60e3);
  })();
})();
