/* ==========================================================================
   WebCrawler Pro — console behaviour
   --------------------------------------------------------------------------
   Progressive enhancement only. Every page is rendered by the server and
   works with this file blocked; what it adds is motion (reveal, counters,
   tabs), keyboard reach (Cmd-K palette), and live polling for pages that
   show something still running.

   No framework, no bundler, no network calls to anywhere but our own API.
   ========================================================================== */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* --------------------------------------------------------------- utils -- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function on(el, ev, fn) { if (el) el.addEventListener(ev, fn); }

  /* --------------------------------------------------------------- toast -- */
  var toasts = null;
  function toast(msg, kind) {
    if (!toasts) {
      toasts = document.createElement("div");
      toasts.className = "toasts";
      document.body.appendChild(toasts);
    }
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    toasts.appendChild(el);
    setTimeout(function () {
      el.classList.add("out");
      setTimeout(function () { el.remove(); }, 260);
    }, 4200);
  }
  window.wcpToast = toast;

  /* --------------------------------------------------------------- theme -- */
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("wcp.theme", t); } catch (e) {}
  }
  $$("[data-theme-toggle]").forEach(function (btn) {
    on(btn, "click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      setTheme(cur === "dark" ? "light" : "dark");
    });
  });

  /* ------------------------------------------------------------- sidebar -- */
  $$("[data-sidebar-toggle]").forEach(function (btn) {
    on(btn, "click", function () {
      var app = $(".app");
      if (!app) return;
      app.classList.toggle("collapsed");
      try {
        localStorage.setItem("wcp.sidebar", app.classList.contains("collapsed") ? "1" : "0");
      } catch (e) {}
    });
  });

  /* --------------------------------------------------------------- tabs -- */
  /* Delegated so tabs that arrive later (rendered by a poll, say) work too. */
  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".tab");
    if (!tab) return;
    var group = tab.closest("[data-tabs]");
    if (!group) return;
    e.preventDefault();

    var name = tab.getAttribute("data-tab");
    $$(".tab", group).forEach(function (t) {
      t.setAttribute("aria-selected", t === tab ? "true" : "false");
    });
    $$(".tabpanel", group).forEach(function (p) {
      var match = p.getAttribute("data-panel") === name;
      p.hidden = !match;
      p.style.animation = "none";
      void p.offsetWidth;               /* restart the entry animation */
      p.style.animation = "";
    });
    try {
      if (history.replaceState) {
        var url = new URL(location.href);
        url.searchParams.set("tab", name);
        history.replaceState(null, "", url);
      }
    } catch (err) {}
  });

  /* A tab can be deep-linked with ?tab=name */
  function openTabFromQuery() {
    var want = new URLSearchParams(location.search).get("tab");
    if (!want) return;
    var tab = document.querySelector('.tab[data-tab="' + CSS.escape(want) + '"]');
    if (tab) tab.click();
  }
  openTabFromQuery();

  /* ------------------------------------------------------------- modals -- */
  window.wcpModal = {
    open: function (id) {
      var m = document.getElementById(id);
      if (!m) return;
      m.hidden = false;
      var f = m.querySelector("[data-autofocus]");
      if (f) setTimeout(function () { f.focus(); }, 40);
    },
    close: function (el) {
      var m = el.closest(".modal-back");
      if (m) m.hidden = true;
    }
  };
  document.addEventListener("click", function (e) {
    var open = e.target.closest("[data-modal-open]");
    if (open) { e.preventDefault(); wcpModal.open(open.getAttribute("data-modal-open")); return; }
    var close = e.target.closest("[data-modal-close]");
    if (close) { e.preventDefault(); wcpModal.close(close); return; }
    if (e.target.classList && e.target.classList.contains("modal-back")) {
      e.target.hidden = true;
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      $$(".modal-back:not([hidden])").forEach(function (m) { m.hidden = true; });
    }
  });

  /* -------------------------------------------------------------- reveal -- */
  function initReveal() {
    var items = $$("[data-reveal]");
    if (!items.length) return;
    if (reduced || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        en.target.classList.add("in");
        io.unobserve(en.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach(function (el, i) {
      /* stagger within a section, but never more than ~350 ms of waiting */
      if (!el.style.getPropertyValue("--d")) {
        el.style.setProperty("--d", Math.min(i % 8, 7) * 55 + "ms");
      }
      io.observe(el);
    });
  }
  initReveal();

  /* ------------------------------------------------------------ counters -- */
  function initCounters() {
    var nums = $$("[data-count]");
    if (!nums.length) return;
    function run(el) {
      var target = parseFloat(el.getAttribute("data-count"));
      var suffix = el.getAttribute("data-suffix") || "";
      var decimals = parseInt(el.getAttribute("data-decimals") || "0", 10);
      if (reduced) { el.textContent = target.toFixed(decimals) + suffix; return; }
      var dur = 1250, t0 = performance.now();
      function step(now) {
        var p = Math.min(1, (now - t0) / dur);
        var eased = 1 - Math.pow(1 - p, 3);        /* easeOutCubic */
        el.textContent = (target * eased).toFixed(decimals) + suffix;
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    }
    if (!("IntersectionObserver" in window)) { nums.forEach(run); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        run(en.target);
        io.unobserve(en.target);
      });
    }, { threshold: 0.4 });
    nums.forEach(function (el) { io.observe(el); });
  }
  initCounters();

  /* ------------------------------------------------------------- sorting -- */
  function numericish(txt) {
    var n = parseFloat(String(txt).replace(/[^0-9.\-]/g, ""));
    return isNaN(n) ? null : n;
  }
  document.addEventListener("click", function (e) {
    var th = e.target.closest("th[data-sort]");
    if (!th) return;
    var table = th.closest("table");
    if (!table) return;
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var dir = th.getAttribute("data-dir") === "asc" ? "desc" : "asc";
    th.setAttribute("data-dir", dir);
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var av = a.cells[idx] ? a.cells[idx].textContent.trim() : "";
      var bv = b.cells[idx] ? b.cells[idx].textContent.trim() : "";
      var an = numericish(av), bn = numericish(bv);
      var cmp = (an !== null && bn !== null) ? an - bn : av.localeCompare(bv);
      return dir === "asc" ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  });

  /* ------------------------------------------------------------ filtering -- */
  $$("[data-filter]").forEach(function (input) {
    on(input, "input", function () {
      var term = input.value.trim().toLowerCase();
      $$(input.getAttribute("data-filter")).forEach(function (row) {
        row.hidden = term ? row.textContent.toLowerCase().indexOf(term) < 0 : false;
      });
    });
  });

  /* -------------------------------------------------------------- copying -- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    e.preventDefault();
    var text = btn.getAttribute("data-copy");
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1400);
      toast("Copied to clipboard", "ok");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { toast("Copy failed", "err"); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); }
      catch (err) { toast("Copy failed", "err"); }
      ta.remove();
    }
  });

  /* --------------------------------------------------------------- polling -- */
  /* Anything carrying data-poll="/path" re-reads that path on an interval,
     which is how a running crawl or index build stays live without
     websockets or a second piece of infrastructure. */
  function pollAll() {
    $$("[data-poll]").forEach(function (el) {
      if (el.dataset.busy === "1") return;
      el.dataset.busy = "1";
      fetch(el.getAttribute("data-poll"), {
        headers: { "Accept": "application/json" }, credentials: "same-origin"
      })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
        .then(function (data) {
          var render = window[el.getAttribute("data-poll-render")];
          if (typeof render === "function") { render(el, data); }
          else if (el.hasAttribute("data-poll-reload")) { location.reload(); }
        })
        .catch(function () { /* transient failures must not spam toasts */ })
        .then(function () { el.dataset.busy = "0"; });
    });
  }
  var pollers = $$("[data-poll]");
  if (pollers.length) setInterval(pollAll, 4000);

  /* ------------------------------------------------------ command palette -- */
  var PALETTE = [
    { t: "Dashboard", u: "/app", i: "▦", k: "home overview status" },
    { t: "Query the index", u: "/app/search", i: "🔍", k: "search query bm25 find" },
    { t: "Crawl jobs", u: "/app/crawl", i: "🕷", k: "crawl spider start job url" },
    { t: "Topic discovery", u: "/app/discovery", i: "✦", k: "discover topic seed domains" },
    { t: "Own search index", u: "/app/index", i: "🗂", k: "index corpus build sources levels" },
    { t: "Data exchange", u: "/app/marketplace", i: "🛒", k: "dataset catalogue bounty licence" },
    { t: "Agent marketplace", u: "/app/agents", i: "⚙", k: "agent catalog listing manifest publisher" },
    { t: "API keys", u: "/app/keys", i: "🔑", k: "api key token secret auth" },
    { t: "Usage & billing", u: "/app/billing", i: "💳", k: "billing plan invoice usage quota" },
    { t: "Settings & webhooks", u: "/app/settings", i: "⚙", k: "settings webhook config" },
    { t: "API reference", u: "/app/docs", i: "📘", k: "docs api reference endpoint" }
  ];

  function buildPalette() {
    if (document.getElementById("palette")) return;
    var back = document.createElement("div");
    back.className = "modal-back";
    back.id = "palette";
    back.hidden = true;
    back.innerHTML =
      '<div class="modal">' +
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
    on(q, "input", render);
    on(q, "keydown", function (e) {
      if (e.key === "Enter") {
        var first = list.querySelector("a");
        if (first) location.href = first.getAttribute("href");
      }
    });
    on(back, "click", function (e) { if (e.target === back) back.hidden = true; });
    render();
    return back;
  }

  function togglePalette() {
    var back = document.getElementById("palette") || buildPalette();
    if (!back) return;
    back.hidden = !back.hidden;
    if (!back.hidden) {
      var q = back.querySelector("#pal-q");
      q.value = "";
      q.focus();
    }
  }
  $$("[data-palette]").forEach(function (btn) { on(btn, "click", togglePalette); });

  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      togglePalette();
    }
  });

  /* ------------------------------------------------------------- misc ux -- */
  /* Confirm before anything destructive. */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  /* Buttons that should look busy rather than be clicked twice. */
  document.addEventListener("submit", function (e) {
    var btn = e.target.querySelector("[data-busy]");
    if (!btn) return;
    btn.disabled = true;
    var label = btn.textContent;
    btn.textContent = btn.getAttribute("data-busy") || "Working…";
    setTimeout(function () { btn.disabled = false; btn.textContent = label; }, 6000);
  });
})();
