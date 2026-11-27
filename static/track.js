/* FWX usage tracking, shared by every page (see /about, "What this site records").
 *
 * FWXTrack.init(page, ctx)  page: "main" | "beta" | "range" | "about"; ctx() -> {aoi, base, layers}
 * FWXTrack.send(ev, extra)  one beacon: view, base, layers, tab, ...
 * FWXTrack.act(name)        a feature used; sent once per visit per feature
 * FWXTrack.feedback()       opens the "was this useful?" box
 *
 * A visit is one open tab: a random id kept in sessionStorage (gone when the tab closes; no cookies).
 * Time counts only while the page is visible, sent every 10 min and whenever the page is hidden or closed.
 */
(() => {
  let page = "main", ctx = () => ({}), visibleSince = null, unsent = 0, total = 0, asked = false;
  const acted = new Set();
  let sid = "";
  try { sid = sessionStorage.getItem("fwx-sid") || ""; } catch (_) {}
  if (!/^[a-z0-9]{12}$/.test(sid)) {
    sid = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => "abcdefghijklmnopqrstuvwxyz0123456789"[b % 36]).join("");
    try { sessionStorage.setItem("fwx-sid", sid); } catch (_) {}
  }

  function send(ev, extra = {}) {
    try {
      const c = ctx() || {};
      navigator.sendBeacon("/api/usage", JSON.stringify({ page, ev, sid, aoi: c.aoi || undefined, base: c.base || undefined,
        layers: c.layers || undefined, ...extra }));
    } catch (_) {}
  }

  // ---- time on screen ----------------------------------------------------------------------------------
  function tick() {
    if (visibleSince != null) { const s = (Date.now() - visibleSince) / 1000; unsent += s; total += s; visibleSince = Date.now(); }
  }
  function flush() {
    tick();
    const s = Math.round(unsent);
    if (s >= 1) { send("time", { secs: s }); unsent -= s; }
  }
  function onVis() {
    if (document.visibilityState === "visible") { if (visibleSince == null) visibleSince = Date.now(); }
    else { flush(); visibleSince = null; }
  }

  // ---- feedback ----------------------------------------------------------------------------------------
  const ASK_AFTER_S = 90, ASK_EVERY_DAYS = 14;
  function css() {
    if (document.getElementById("fwx-fb-css")) return;
    const s = document.createElement("style");
    s.id = "fwx-fb-css";
    s.textContent = `#fwx-fb{position:fixed;left:12px;bottom:12px;z-index:5000;background:#1d2329;color:#fff;border-radius:10px;
      padding:10px 12px;font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.3);max-width:320px}
      #fwx-fb button{font:inherit;border:0;border-radius:6px;padding:4px 10px;margin:0 4px 0 0;cursor:pointer;background:#3a444d;color:#fff}
      #fwx-fb button.v{font-size:18px;padding:2px 10px}#fwx-fb button.go{background:#eb6834}
      #fwx-fb .x{position:absolute;right:6px;top:4px;background:none;padding:0 4px;color:#aab3bb}
      #fwx-fb textarea{width:100%;margin:8px 0 6px;border-radius:6px;border:0;padding:6px;font:inherit;min-height:60px;resize:vertical}
      #fwx-fb .row{display:flex;align-items:center;gap:4px;margin-top:6px}`;
    document.head.appendChild(s);
  }
  function feedback(auto = false) {
    if (document.getElementById("fwx-fb")) return;
    css();
    try { localStorage.setItem("fwx-fb-asked", String(Date.now())); } catch (_) {}
    const box = document.createElement("div");
    box.id = "fwx-fb";
    box.setAttribute("role", "dialog");
    box.innerHTML = `<button class="x" aria-label="Close">×</button>
      <div>${auto ? "Was FWX useful today?" : "How is FWX working for you?"}</div>
      <div class="row"><button class="v" data-v="up" aria-label="Useful">👍</button><button class="v" data-v="down" aria-label="Not useful">👎</button></div>`;
    document.body.appendChild(box);
    const close = () => box.remove();
    box.querySelector(".x").onclick = () => { if (auto) send("act", { act: "feedback_dismissed" }); close(); };
    box.querySelectorAll("[data-v]").forEach((b) => (b.onclick = () => {
      const vote = b.dataset.v;
      box.innerHTML = `<button class="x" aria-label="Close">×</button>
        <div>${vote === "up" ? "Thanks! What do you use it for?" : "Sorry about that. What was missing or wrong?"}</div>
        <textarea maxlength="500" placeholder="Optional"></textarea>
        <div class="row"><button class="go">Send</button><button class="skip">Skip</button></div>`;
      const ta = box.querySelector("textarea");
      ta.focus();
      const done = (text) => { send("fb", { vote, text: text || undefined }); box.innerHTML = "<div>Thank you, every response is read.</div>"; setTimeout(close, 2500); };
      box.querySelector(".go").onclick = () => done(ta.value.trim().slice(0, 500));
      box.querySelector(".skip").onclick = () => done("");
      box.querySelector(".x").onclick = () => done("");
    }));
  }
  function maybeAsk() {
    if (asked || page === "range" || page === "about") return;
    tick();
    if (total < ASK_AFTER_S) return;
    asked = true;
    let last = 0;
    try { last = +localStorage.getItem("fwx-fb-asked") || 0; } catch (_) {}
    if (Date.now() - last > ASK_EVERY_DAYS * 864e5) feedback(true);
  }

  window.FWXTrack = {
    init(p, c) {
      page = p; ctx = c || ctx;
      document.addEventListener("visibilitychange", onVis);
      window.addEventListener("pagehide", flush);
      onVis();
      setInterval(flush, 10 * 60e3);
      setInterval(maybeAsk, 15e3);
    },
    send,
    act(name) { if (!acted.has(name)) { acted.add(name); send("act", { act: name }); } },
    feedback: () => feedback(false),
  };
})();
