/* AI Daily front-end. Vanilla JS, no build step.
   Reads data/latest.json and renders picks, crypto strip, tip and week history. */
(function () {
  "use strict";

  var CATS = [
    ["all", "All"], ["ai", "AI"], ["code", "Code"], ["mac", "Mac Local"],
    ["crypto", "Crypto"], ["reselling", "Reselling"], ["guides", "Guides"]
  ];
  var CAT_LABEL = { ai: "AI", code: "Code", mac: "Mac Local", crypto: "Crypto", reselling: "Reselling", guides: "Guides" };
  var LLMS = ["Claude", "GPT", "Gemini", "Grok", "Llama", "DeepSeek", "Mistral", "Qwen", "Other"];

  var state = { cat: "all", llms: {} };
  LLMS.forEach(function (l) { state.llms[l] = true; });

  function loadState() {
    try {
      var raw = localStorage.getItem("aidaily.filters");
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (saved && typeof saved.cat === "string") state.cat = saved.cat;
      if (saved && saved.llms) LLMS.forEach(function (l) { if (l in saved.llms) state.llms[l] = !!saved.llms[l]; });
    } catch (e) { /* private mode or blocked storage: keep defaults */ }
  }
  function saveState() {
    try { localStorage.setItem("aidaily.filters", JSON.stringify(state)); } catch (e) { /* ignore */ }
  }

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function timeAgo(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return "now";
    if (m < 60) return m + "m ago";
    var h = Math.round(m / 60);
    if (h < 36) return h + "h ago";
    return Math.round(h / 24) + "d ago";
  }

  function hhmm(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function longDate(dateStr) {
    var d = new Date(dateStr + "T12:00:00");
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  }

  function fmtPrice(v) {
    if (v >= 1000) return "$" + Math.round(v).toLocaleString();
    return "$" + v.toFixed(2);
  }

  function renderCrypto(crypto) {
    var box = document.getElementById("crypto");
    box.textContent = "";
    if (!crypto || !crypto.prices || !crypto.prices.length) { box.hidden = true; return; }
    box.hidden = false;
    crypto.prices.forEach(function (p) {
      var ch = Number(p.change_24h || 0);
      var cls = ch >= 0 ? "up" : "down";
      box.appendChild(el("div", { "class": "coin" }, [
        el("b", { text: p.symbol }),
        el("span", { "class": cls, text: (ch >= 0 ? "+" : "") + ch.toFixed(1) + "%" }),
        el("span", { "class": "price", text: fmtPrice(Number(p.usd)) })
      ]));
    });
  }

  function renderChips() {
    var nav = document.getElementById("chips");
    nav.textContent = "";
    CATS.forEach(function (c) {
      var b = el("button", { "class": "chip" + (state.cat === c[0] ? " on" : ""), text: c[1], type: "button" });
      b.addEventListener("click", function () { state.cat = c[0]; saveState(); render(); });
      nav.appendChild(b);
    });
    var lnav = document.getElementById("llmchips");
    lnav.textContent = "";
    lnav.hidden = state.cat !== "ai";
    if (state.cat !== "ai") return;
    LLMS.forEach(function (l) {
      var b = el("button", { "class": "chip" + (state.llms[l] ? " on" : ""), text: l, type: "button", "aria-pressed": String(!!state.llms[l]) });
      b.addEventListener("click", function () { state.llms[l] = !state.llms[l]; saveState(); render(); });
      lnav.appendChild(b);
    });
  }

  function visible(it) {
    if (state.cat !== "all" && it.category !== state.cat) return false;
    if (state.cat === "ai" && it.category === "ai") {
      var llm = it.llm || "Other";
      if (!state.llms[llm]) return false;
    }
    return true;
  }

  function card(it) {
    var meta = [el("span", { "class": "tag", text: CAT_LABEL[it.category] || it.category })];
    if (it.category === "ai" && it.llm) meta.push(el("span", { "class": "tag llm", text: it.llm }));
    meta.push(el("span", { text: it.source + (it.published ? " · " + timeAgo(it.published) : "") }));
    var kids = [el("div", { "class": "meta" }, meta)];
    if (it.thumbnail) {
      var a = el("a", { href: it.url, target: "_blank", rel: "noopener" });
      a.appendChild(el("img", { "class": "thumb", src: it.thumbnail, alt: "", loading: "lazy" }));
      kids.push(a);
    }
    kids.push(el("h3", null, [el("a", { href: it.url, target: "_blank", rel: "noopener", text: it.title })]));
    var why = [];
    if (it.why) why.push(el("span", { "class": "why", text: it.why }));
    if (it.hn_url) why.push(el("a", { "class": "why", href: it.hn_url, target: "_blank", rel: "noopener", text: "HN" }));
    if (why.length) kids.push(el("div", { "class": "meta" }, why));
    if (it.summary) kids.push(el("p", { "class": "summary", text: it.summary }));
    return el("article", { "class": "card" }, kids);
  }

  function renderPicks(data) {
    var main = document.getElementById("picks");
    main.textContent = "";
    var list = (data.picks || []).filter(visible);
    if (!list.length) {
      main.appendChild(el("p", { "class": "empty", text: "Quiet day, nothing big." }));
      return;
    }
    list.forEach(function (it) { main.appendChild(card(it)); });
  }

  function renderTip(tip) {
    var box = document.getElementById("tip");
    box.textContent = "";
    if (!tip || !tip.text) { box.hidden = true; return; }
    box.hidden = false;
    box.appendChild(el("div", { "class": "label", text: "Tip of the day" + (tip.tag ? " · " + tip.tag : "") }));
    box.appendChild(el("p", { text: tip.text }));
  }

  function renderWeek(data) {
    var sec = document.getElementById("week");
    var box = document.getElementById("weekdays");
    box.textContent = "";
    var days = (data.week || []).filter(function (d) { return d.date !== data.date; });
    if (!days.length) { sec.hidden = true; return; }
    sec.hidden = false;
    days.forEach(function (d) {
      var items = d.picks || [];
      var ul = el("ul", null, items.map(function (it) {
        return el("li", null, [
          el("span", { "class": "tag", text: CAT_LABEL[it.category] || it.category }),
          el("a", { href: it.url, target: "_blank", rel: "noopener", text: it.title }),
          el("span", { "class": "src", text: it.source || "" })
        ]);
      }));
      if (!items.length) ul.appendChild(el("li", { "class": "muted", text: "Quiet day." }));
      box.appendChild(el("details", null, [
        el("summary", null, [el("span", { text: longDate(d.date) }), el("span", { "class": "count", text: items.length + (items.length === 1 ? " item" : " items") })]),
        ul
      ]));
    });
  }

  var DATA = null;
  function render() {
    if (!DATA) return;
    renderChips();
    renderPicks(DATA);
  }

  function boot(data) {
    DATA = data;
    document.getElementById("date").textContent = longDate(data.date);
    var upd = document.getElementById("updated");
    upd.textContent = data.updated ? "updated " + hhmm(data.updated) : "";
    document.title = "AI Daily · " + data.date;
    renderCrypto(data.crypto);
    renderTip(data.tip);
    renderWeek(data);
    var status = document.getElementById("status");
    if (data.status === "stale") status.textContent = "Showing yesterday's data (today's fetch failed).";
    else if (data.sources_failed && data.sources_failed.length) status.textContent = data.sources_failed.length + " source(s) skipped";
    render();
  }

  loadState();
  fetch("data/latest.json?t=" + Math.floor(Date.now() / 300000), { cache: "no-cache" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(boot)
    .catch(function (e) {
      var main = document.getElementById("picks");
      main.textContent = "";
      main.appendChild(el("p", { "class": "empty", text: "Could not load data (" + e.message + ")." }));
    });
})();
