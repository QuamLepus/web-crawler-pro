/* ==========================================================================
   WebCrawler Pro — console behaviour
   --------------------------------------------------------------------------
   Deliberately vanilla and dependency-free: no framework, no CDN, no build.
   The console has to load on an air-gapped box the same way the crawler runs
   on one. ~300 lines of progressive enhancement on top of server-rendered
   HTML — the pages work with JavaScript disabled.
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------------------------------------------- theming -- */
  var THEME_KEY = "wcp.theme";
  var root = document.documentElement;

  function applyTheme(t) {
    root.setAttribute("data-theme", t);
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) btn.textContent = t === "dark" ? "☀" : "☾";
  }
  var stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) { /* private mode */ }
  applyTheme(stored || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-toggle]");
    if (!b) return;
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (err) { /* ignore */ }
  });

  /* ------------------------------------------------------------- sidebar -- */
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-sidebar-toggle]")) {
      var app = document.querySelector(".app");
      if (window.innerWidth <= 900) {
        document.querySelector(".sidebar").classList.toggle("open");
      } else {
        app.classList.toggle("is-collapsed");
        try {
          localStorage.setItem("wcp.sidebar",
            app.classList.contains("is-collapsed") ? "1" : "0");
        } catch (err) { /* ignore */ }
      }
    }
  });
  try {
    if (localStorage.getItem("wcp.sidebar") === "1") {
      document.querySelector(".app").classList.add("is-collapsed");
    }
  } catch (e) { /* ignore */ }

  /* --------------------------------------------------------------- toasts -- */
  var host = document.createElement("div");
  host.className = "toasts";
  document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(host); });

  function toast(msg, kind) {
    if (!host.parentNode) document.body.appendChild(host);
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    var icon = kind === "err" ? "✕" : kind === "warn" ? "!" : kind === "ok" ? "✓" : "i";
    el.innerHTML = '<span style="font-weight:700">' + icon + "</span><span></span>";
    el.lastChild.textContent = msg;
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .25s, transform .25s";
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      setTimeout(function () { el.remove(); }, 260);
    }, 4200);
  }
  window.wcpToast = toast;

  /* ---------------------------------------------------------------- JSON -- */
  /** Fetch JSON from our own API. Returns parsed body, throws on HTTP error. */
  async function api(path, opts) {
    opts = opts || {};
    var init = {
      method: opts.method || "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    var res = await fetch(path, init);
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (!res.ok) {
      var msg = (data && data.detail) || (data && data.message) ||
        (res.status === 401 ? "Session expired — sign in again" : "Request failed (" + res.status + ")");
      var err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }
  window.wcpApi = api;

  /* -------------------------------------------------- declarative actions -- */
  /* Any element with data-post="/path" is wired automatically: it POSTs (or
     whatever data-method says), shows a spinner, and toasts the result.
     data-confirm asks first, data-reload refreshes the page after.          */
  document.addEventListener("click", async function (e) {
    var el = e.target.closest("[data-post]");
    if (!el || el.disabled) return;
    e.preventDefault();
    var url = el.getAttribute("data-post");
    var method = (el.getAttribute("data-method") || "POST").toUpperCase();
    var confirmText = el.getAttribute("data-confirm");
    if (confirmText && !window.confirm(confirmText)) return;

    var label = el.innerHTML;
    el.classList.add("is-busy");
    el.innerHTML = '<span class="spin"></span>';
    try {
      var data = await api(url, { method: method, body: el.dataset.body ? JSON.parse(el.dataset.body) : {} });
      if (el.hasAttribute("data-reload")) {
        toast(el.getAttribute("data-ok") || "Done", "ok");
        setTimeout(function () { window.location.reload(); }, 550);
        return;
      }
      if (el.hasAttribute("data-redirect")) {
        window.location = el.getAttribute("data-redirect");
        return;
      }
      toast(el.getAttribute("data-ok") || "Done", "ok");
      if (el.hasAttribute("data-target") && data) {
        var t = document.querySelector(el.getAttribute("data-target"));
        if (t) t.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
      }
      el.innerHTML = label;
      el.classList.remove("is-busy");
    } catch (err) {
      el.innerHTML = label;
      el.classList.remove("is-busy");
      toast(err.message, "err");
    }
  });

  /* ---------------------------------------------------------------- copy --- */
  document.addEventListener("click", async function (e) {
    var el = e.target.closest("[data-copy]");
    if (!el) return;
    var text = el.getAttribute("data-copy");
    try {
      await navigator.clipboard.writeText(text);
      var old = el.textContent;
      el.textContent = "Copied";
      setTimeout(function () { el.textContent = old; }, 1200);
    } catch (err) {
      toast("Could not copy — select and press " + (navigator.platform.indexOf("Mac") >= 0 ? "⌘C" : "Ctrl+C"), "warn");
    }
  });

  /* -------------------------------------------------------------- modals --- */
  window.wcpModal = function (id) {
    var m = document.getElementById(id);
    if (m) m.hidden = false;
  };
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-modal-open]")) {
      var mid = e.target.closest("[data-modal-open]").getAttribute("data-modal-open");
      window.wcpModal(mid);
    }
    if (e.target.closest("[data-modal-close]") || e.target.classList.contains("modal-back")) {
      var back = e.target.closest(".modal-back");
      if (back) back.hidden = true;
    }
    if (e.key === "Escape") { /* handled below */ }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal-back:not([hidden])").forEach(function (m) { m.hidden = true; });
      var pal = document.getElementById("palette");
      if (pal && !pal.hidden) pal.hidden = true;
    }
  });

  /* ------------------------------------------------------------- polling --- */
  /* Anything with data-poll="/path" refreshes itself on an interval, which is
     how running crawl jobs and index builds stay live without websockets.   */
  function pollAll() {
    document.querySelectorAll("[data-poll]").forEach(async function (el) {
      if (el.dataset.busy === "1") return;
      el.dataset.busy = "1";
      try {
        var data = await api(el.getAttribute("data-poll"));
        var render = window[el.getAttribute("data-poll-render")];
        if (typeof render === "function") render(el, data);
        else el.textContent = JSON.stringify(data);
      } catch (err) { /* silent: a transient failure should not spam toasts */ }
      el.dataset.busy = "0";
    });
  }
  setInterval(pollAll, 4000);

  /* ------------------------------------------------------ command palette -- */
  var PALETTE = [
    { t: "Dashboard", u: "/app", i: "▦", k: "go dashboard home overview" },
    { t: "Search the index", u: "/app/search", i: "🔍", k: "search query find google" },
    { t: "New crawl job", u: "/app/crawl", i: "🕷", k: "crawl spider start job url" },
    { t: "Topic discovery", u: "/app/discovery", i: "✦", k: "discover topic seed domains" },
    { t: "Own search index", u: "/app/index", i: "🗂", k: "index bm25 build status levels spell" },
    { t: "Data marketplace", u: "/app/marketplace", i: "🛒", k: "market dataset buy sell bounty" },
    { t: "API keys", u: "/app/keys", i: "🔑", k: "api key token secret auth" },
    { t: "Billing & plan", u: "/app/billing", i: "💳", k: "billing plan invoice usage stripe" },
    { t: "Settings & webhooks", u: "/app/settings", i: "⚙", k: "settings webhook config" },
    { t: "API reference", u: "/app/docs", i: "📘", k: "docs api reference endpoint" },
  ];

  function buildPalette() {
    if (document.getElementById("palette")) return;
    var back = document.createElement("div");
    back.className = "modal-back";
    back.id = "palette";
    back.hidden = true;
    back.innerHTML =
      '<div class="modal" style="max-width:560px">' +
      '<div class="head"><input class="input" id="pal-q" placeholder="Jump to…" autocomplete="off"></div>' +
      '<div id="pal-list" style="max-height:340px;overflow:auto;padding:8px"></div></div>';
    document.body.appendChild(back);

    var q = back.querySelector("#pal-q");
    var list = back.querySelector("#pal-list");

    function render() {
      var term = q.value.trim().toLowerCase();
      var hits = PALETTE.filter(function (p) {
        return !term || p.t.toLowerCase().indexOf(term) >= 0 || p.k.indexOf(term) >= 0;
      });
      list.innerHTML = hits.map(function (p, i) {
        return '<a href="' + p.u + '" class="row" style="padding:9px 12px;border-radius:8px;' +
          (i === 0 ? "background:var(--bg-sunken);" : "") + '">' +
          '<span style="width:22px;text-align:center">' + p.i + "</span>" +
          '<span style="flex:1;font-size:13.5px">' + p.t + "</span>" +
          '<span class="kbd">↵</span></a>';
      }).join("") || '<div class="empty" style="padding:28px"><p>No match</p></div>';
    }
    q.addEventListener("input", render);
    q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        var first = list.querySelector("a");
        if (first) window.location = first.getAttribute("href");
      }
    });
    back.addEventListener("click", function (e) {
      if (e.target === back) back.hidden = true;
    });
    render();
  }

  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      buildPalette();
      var back = document.getElementById("palette");
      back.hidden = false;
      var q = back.querySelector("#pal-q");
      q.value = "";
      q.focus();
      q.dispatchEvent(new Event("input"));
    }
  });

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-palette]")) {
      e.preventDefault();
      buildPalette();
      var b = document.getElementById("palette");
      b.hidden = false;
      b.querySelector("#pal-q").focus();
    }
  });

  /* ----------------------------------------------------------- table sort -- */
  document.addEventListener("click", function (e) {
    var th = e.target.closest("th[data-sort]");
    if (!th) return;
    var table = th.closest("table");
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var dir = th.dataset.dir === "asc" ? "desc" : "asc";
    th.dataset.dir = dir;
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    rows.sort(function (a, b) {
      var x = a.cells[idx].innerText.trim();
      var y = b.cells[idx].innerText.trim();
      var nx = parseFloat(x.replace(/[^0-9.\-]/g, ""));
      var ny = parseFloat(y.replace(/[^0-9.\-]/g, ""));
      var cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y);
      return dir === "asc" ? cmp : -cmp;
    });
    rows.forEach(function (r) { table.tBodies[0].appendChild(r); });
  });

  /* -------------------------------------------------------------- filters -- */
  document.addEventListener("input", function (e) {
    var inp = e.target.closest("[data-filter]");
    if (!inp) return;
    var term = inp.value.trim().toLowerCase();
    document.querySelectorAll(inp.getAttribute("data-filter")).forEach(function (row) {
      row.style.display = row.innerText.toLowerCase().indexOf(term) >= 0 ? "" : "none";
    });
  });

  /* --------------------------------------------------------- auto dismiss -- */
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-autofocus]").forEach(function (el) { el.focus(); });
  });
})();
