"use strict";

// ---------- state & helpers ----------

const S = {
  user: null, csrf: null, tab: "overview", charts: [],
  settings: { reference_model: "claude-sonnet-5", stale_after_s: 1800 },   // replaced by /api/session
  prefs: loadPrefs(),
  colorSlots: loadSlots(),   // dimension -> {key: slot}; colour follows the entity, kept across page loads
  tkSkew: 0,                 // server clock minus this browser's, from /api/me/tickets: discounts end on the server's clock
  discountsEnded: new Set(), // discount ends already re-rendered for, so a server still listing one can't loop renders
};
const $ = (sel, root = document) => root.querySelector(sel);
const tzOffset = () => -new Date().getTimezoneOffset() * 60;
// The page's own text in the chosen language (i18n.js), and the switch between Persian and English.
applyStatic();
document.title = t("app.title");
$("#lang-toggle").onclick = $("#lang-signin").onclick = () => setLang(LANG === "fa" ? "en" : "fa");

function loadPrefs() {
  const d = { range: "7d", granularity: "day", split: "user", metric: "weighted", period: "24h", userPeriod: "7d", bucket: "5h", modelMetric: "cost_usd", orderStatus: "open" };
  try { return { ...d, ...JSON.parse(localStorage.getItem("cp-prefs") || "{}") }; } catch { return d; }
}
function loadSlots() { try { return JSON.parse(localStorage.getItem("cp-colors") || "{}"); } catch { return {}; } }
function savePrefs() { try { localStorage.setItem("cp-prefs", JSON.stringify(S.prefs)); } catch { /* private mode */ } }

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const SLOTS = 8;
const OTHER = "Other";
function colorFor(dim, key) {
  if (key === OTHER) return css("--other");
  if (key === "unattributed") return css("--unattributed");
  const m = (S.colorSlots[dim] ||= {});
  if (!(key in m)) {
    m[key] = Object.keys(m).length;
    try { localStorage.setItem("cp-colors", JSON.stringify(S.colorSlots)); } catch { /* private mode */ }
  }
  const slot = m[key];
  return slot < SLOTS ? css(`--s${slot + 1}`) : css("--other");
}
// Keep at most 7 named series; fold the tail into "Other" (never a 9th hue).
function topKeys(totals, max = 7) {
  const keys = Object.entries(totals).sort((a, b) => b[1] - a[1]).map(([k]) => k);
  return keys.length <= max + 1 ? keys : keys.slice(0, max);
}

// i18n-formatters:start
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bdi = (v) => `<bdi>${esc(v)}</bdi>`;   // a name, id or address inside translated text keeps its own direction
const iso = (v) => `\u2068${v}\u2069`;   // the same for plain text (textContent, confirm()): first-strong isolate
// Persian keyboards type Persian digits (U+06F0..9; Arabic ones U+0660..9, and U+066B as the decimal point);
// number fields read them as 0-9.
const asciiDigits = (s) => s.replace(/[\u06F0-\u06F9]/g, (d) => d.charCodeAt(0) - 0x6F0).replace(/[\u0660-\u0669]/g, (d) => d.charCodeAt(0) - 0x660).replace(/\u066B/g, ".");
const num = (v) => Number(asciiDigits(String(v ?? "").trim()));
const nf = new Intl.NumberFormat(LOC, { maximumFractionDigits: 1, notation: "compact" });   // 24.9M, never "24.9m" (minutes?)
const nfFull = new Intl.NumberFormat(LOC);
const nfFix = (d) => new Intl.NumberFormat(LOC, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false });
const nf0 = nfFix(0), nf2 = nfFix(2);
function fmtNum(v) { return v == null ? "—" : nf.format(v); }
function fmtUsd(v) { return v == null ? "—" : v === 0 ? `$${nf0.format(0)}` : v < 0.01 ? `<$${nf2.format(0.01)}` : "$" + (v >= 100 ? nf0 : nf2).format(v); }
const fmtPrice = (v) => `$${Number.isInteger(v) ? nf0.format(v) : nf2.format(v)}`;   // a USD price: whole dollars bare, otherwise cents
const fmtShare = (v) => `${new Intl.NumberFormat(LOC, { maximumFractionDigits: 2 }).format(+v)}%`;   // 6.1000000000000005 reads 6.1%
function fmtPct(v, d = 0) { return v == null ? "—" : `${nfFix(d).format(v)}%`; }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  const u = (n, unit) => t(`dur.${unit}`, { n: nf0.format(n) });
  if (s < 60) return u(s, "s");
  // Two largest units, e.g. "2h 30m"; the second is dropped when it is zero.
  const [big, bigU, small, smallU] = s < 3600 ? [Math.floor(s / 60), "m", s % 60, "s"]
    : s < 86400 ? [Math.floor(s / 3600), "h", Math.floor(s % 3600 / 60), "m"]
    : [Math.floor(s / 86400), "d", Math.floor(s % 86400 / 3600), "h"];
  return small ? t("dur.join", { a: u(big, bigU), b: u(small, smallU) }) : u(big, bigU);
}
function fmtAgo(t_) { return t_ ? t("app.ago", { d: fmtDur(Date.now() / 1000 - t_) }) : t("app.never"); }
function fmtTime(t_) { return t_ ? new Date(t_ * 1000).toLocaleString(LOC, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
const fmtDate = (t_) => (t_ ? new Date(t_ * 1000).toLocaleString(LOC, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const money = (amount, currency) => { try { return new Intl.NumberFormat(LOC, { style: "currency", currency }).format(amount); } catch { return `${nf2.format(amount)} ${currency}`; } };
// i18n-formatters:end
const toLocal = (t) => { const d = new Date(t * 1000); d.setSeconds(0, 0); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };   // for <input type=datetime-local>
const fromLocal = (v) => (v ? Math.floor(new Date(v).getTime() / 1000) : null);
const METRICS = Object.fromEntries(["weighted", "raw", "cost_usd", "requests"].map((k) => [k, t(`metric.${k}`)]));
function fmtMetric(metric, v) { return metric === "cost_usd" ? fmtUsd(v) : fmtNum(v); }

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body !== undefined) headers["content-type"] = "application/json";
  if (opts.method && opts.method !== "GET" && S.csrf) headers["x-csrf-token"] = S.csrf;
  const r = await fetch(path, { credentials: "same-origin", ...opts, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("signed out"); }
  if (!r.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${r.status}`), { status: r.status, data });
  return data;
}

// ---------- tooltips ----------

// Trusted markup, one or two sentences each. Keys are referenced by data-tip; dynamic tips use data-tip-html.
const TIPS = Object.fromEntries(["login_key", "requests", "weighted", "raw", "cost", "active_users", "burn_rate", "quota", "bucket:5h", "bucket:7d", "share", "unattributed", "stale", "exhaustion", "served", "share_col", "limits_col", "gclaude_version", "key_prefix", "act_limits", "act_rename", "act_upgrade", "act_rotate", "act_routes_key", "act_routes_key_remove", "act_disable", "act_enable", "act_revoke", "act_delete", "role", "scope", "kind", "unit", "value", "split:provider", "metric:weighted", "metric:raw", "metric:cost_usd", "metric:requests", "reported", "provider_sub", "provider_own", "cache_ratio", "recent_requests", "session", "sess_user", "sess_started", "sess_duration", "sess_requests", "sess_weighted", "sess_cost", "sess_models", "act_ungate", "act_ungate_paused", "act_ticket_cancel", "act_ticket_bonus", "capacity_sold", "capacity_util", "sold_out_vs_queued"].map((k) => [k, t(`tip.${k}`)]));
const KIND_TIPS = Object.fromEntries(["requests_minute", "tokens_minute", "tokens_5h", "requests_daily", "tokens_daily", "tokens_weekly", "requests_monthly", "tokens_monthly", "cost_daily", "cost_monthly", "cost_total", "share_5h", "share_7d", "allowed_models"].map((k) => [k, t(`kind.${k}`)]));
const KIND_NOTE = { window: t("kindnote.window"), share: t("kindnote.share") };
const UNIT_TIPS = Object.fromEntries(["count", "weighted", "raw", "usd", "pct", "list"].map((k) => [k, t(`unit.${k}`)]));
// One plain sentence per kind, shown beside each item of the Kind dropdown.
const KIND_SHORT = Object.fromEntries(["requests_minute", "requests_daily", "requests_monthly", "tokens_minute", "tokens_5h", "tokens_daily", "tokens_weekly", "tokens_monthly", "cost_daily", "cost_monthly", "cost_total", "share_5h", "share_7d", "allowed_models"].map((k) => [k, t(`kshort.${k}`)]));
const LIMIT_PLACEHOLDER = { count: t("lim.eg", { v: "200" }), weighted: t("lim.eg", { v: "5000000" }), raw: t("lim.eg", { v: "20000000" }), usd: t("lim.eg", { v: "50" }), pct: t("lim.eg", { v: "25" }), list: "claude-sonnet-*,muse-spark" };
KIND_NOTE.total = t("kindnote.total");
const kindNote = (k) => (k.startsWith("share_") ? KIND_NOTE.share : k === "allowed_models" ? "" : k === "cost_total" ? KIND_NOTE.total : KIND_NOTE.window);
Object.entries(KIND_TIPS).forEach(([k, v]) => {
  TIPS[`kind:${k}`] = `<span class="th">${t(`kname.${k}`)}</span><p>${v}</p>${kindNote(k) ? `<p class="tm">${kindNote(k)}</p>` : ""}`;
});
const ERROR_TIPS = Object.fromEntries(["gateway_limit", "gateway_auth", "gateway_key_scope", "gateway_bad_request", "upstream_quota", "upstream_throttle", "upstream_request_scoped", "gateway_needs_login", "gateway_refresh_unavailable", "gateway_upstream_unreachable", "gateway_route_unconfigured", "overloaded_error", "api_error"].map((k) => [k, t(`etip.${k}`)]));
Object.entries(ERROR_TIPS).forEach(([k, v]) => (TIPS[`err:${k}`] = v));
const ERROR_LABELS = Object.fromEntries(["gateway_limit", "gateway_auth", "gateway_key_scope", "gateway_bad_request", "upstream_quota", "upstream_throttle", "upstream_request_scoped", "gateway_needs_login", "gateway_upstream_unreachable", "gateway_route_unconfigured", "overloaded_error", "gateway_refresh_unavailable", "api_error", "usage_limit", "gateway_unavailable"].map((k) => [k, t(`elabel.${k}`)]));

// A non-admin is never told about the subscription behind the gateway; the server sends them nothing about
// it (no account quota, no share limits, no credential state). These replace every tip that would mention it.
const USER_TIPS = Object.fromEntries(["weighted", "cost", "burn_rate", "split:provider", "metric:weighted", "metric:cost_usd", "provider_sub", "cache_ratio", "sess_weighted", "sess_cost", "kind:tokens_5h", "kind:cost_total", "kind:5h_limit", "kind:weekly_limit", "err:usage_limit", "err:gateway_unavailable", "err:upstream_request_scoped", "err:overloaded_error"].map((k) => [k, t(`utip.${k}`, { note: KIND_NOTE.window })]));
const ADMIN_TIPS = { ...TIPS };
const USER_LIMIT_KINDS = ["5h_limit", "weekly_limit", "today_limit"];   // share limits, as a non-admin's own allowance

const errorKind = (k, rejectedBy) => `${tipT(esc(ERROR_LABELS[k] || k), `err:${k}`)}${rejectedBy && rejectedBy !== "auth" && rejectedBy !== "request" ? ` <span class="muted">(${esc(rejectedBy)})</span>` : ""}`;

// "claude-sonnet-5" -> "Sonnet 5", "claude-haiku-4-5-20251001" -> "Haiku 4.5"; anything else as is.
function modelName(id) {
  const m = /^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?$/.exec(id || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2].replace("-", ".")}` : id;
}
const refModel = () => modelName(S.settings.reference_model);
// Requests without a model, such as Claude Code listing models (GET /v1/models): counted, but no tokens.
const NO_MODEL = "no model";
// What a chart or table shows for a series key: the two internal ones read in the page's language.
const seriesLabel = (k) => (k === OTHER ? t("chart.other") : k === NO_MODEL ? t("chart.no_model") : k);
// Tip text quotes config through placeholders: {ref} the weighting reference model, {stale} the staleness cutoff.
const fillTip = (html) => html.replace(/\{ref\}/g, esc(refModel())).replace(/\{stale\}/g, fmtDur(S.settings.stale_after_s));

// Info dot, dotted term, or an attribute for any element. Unknown keys render nothing extra.
const tipI = (key, label = t("app.what_is_this")) => (TIPS[key]
  ? `<span class="tip-i" tabindex="0" role="button" aria-label="${esc(label)}" data-tip="${esc(key)}">?</span>` : "");
const tipT = (html, key) => (TIPS[key] ? `<span class="tip-t" tabindex="0" data-tip="${esc(key)}">${html}</span>` : html);
const tipAttr = (html) => (html ? ` data-tip-html="${esc(html)}"` : "");
const tipH = (html, tip) => (tip ? `<span class="tip-t" tabindex="0"${tipAttr(tip)}>${html}</span>` : html);

// One shared #tip. It is a manual popover so it enters the top layer above the modal dialog.
const TIP = { for: null, pinned: false, timer: 0 };
function showTip(el, pinned = false) {
  const html = el.dataset.tipHtml || TIPS[el.dataset.tip];
  if (!html) return;
  clearTimeout(TIP.timer);
  if (TIP.for !== el) hideTip();
  const tip = $("#tip");
  tip.innerHTML = fillTip(html);
  if (tip.showPopover && !tip.matches(":popover-open")) tip.showPopover();
  tip.classList.add("open");
  TIP.for = el; TIP.pinned = pinned;
  el.classList.add("tip-on");
  el.setAttribute("aria-describedby", "tip");
  const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  const gutter = 16, gap = 8, vw = document.documentElement.clientWidth, vh = window.innerHeight;
  const cx = r.left + r.width / 2;
  const left = Math.min(Math.max(gutter, cx - w / 2), vw - gutter - w);
  // data-tip-side="right" (dropdown items): beside the element, on the side the text reads away from (the right,
  // or the left in Persian), else on the other side when that one has no room.
  const rtl = document.documentElement.dir === "rtl";
  const fitsR = r.right + gap + w <= vw - gutter, fitsL = r.left - gap - w >= gutter;
  const side = el.dataset.tipSide !== "right" ? null
    : rtl ? (fitsL ? "left" : fitsR ? "right" : null) : (fitsR ? "right" : fitsL ? "left" : null);
  if (side) {
    const top = Math.min(Math.max(gutter, r.top + r.height / 2 - h / 2), vh - gutter - h);
    tip.dataset.side = side;
    tip.style.left = `${side === "right" ? r.right + gap : r.left - gap - w}px`;
    tip.style.top = `${top}px`;
    tip.style.setProperty("--ax", side === "right" ? "0px" : `${w}px`);
    tip.style.setProperty("--ay", `${Math.min(Math.max(12, r.top + r.height / 2 - top), h - 12)}px`);
    tip.style.setProperty("--dy", "0px");
    return;
  }
  const above = r.top - gap - h >= gutter || (r.bottom + gap + h > vh - gutter && r.top > vh - r.bottom);
  tip.dataset.side = above ? "top" : "bottom";
  tip.style.left = `${left}px`;
  tip.style.top = `${above ? r.top - gap - h : r.bottom + gap}px`;
  tip.style.setProperty("--ax", `${Math.min(Math.max(12, cx - left), w - 12)}px`);
  tip.style.setProperty("--dy", above ? "3px" : "-3px");
}
function hideTip() {
  clearTimeout(TIP.timer);
  const tip = $("#tip");
  if (!tip.classList.contains("open")) return;
  tip.classList.remove("open");
  if (tip.hidePopover && tip.matches(":popover-open")) tip.hidePopover();
  if (TIP.for) { TIP.for.classList.remove("tip-on"); TIP.for.removeAttribute("aria-describedby"); }
  TIP.for = null; TIP.pinned = false;
}
const tipTarget = (e) => (e.target instanceof Element ? e.target.closest("[data-tip],[data-tip-html]") : null);
document.addEventListener("pointerover", (e) => {
  if (e.pointerType === "touch") return;
  const el = tipTarget(e);
  if (!el) return;
  clearTimeout(TIP.timer);
  if (el !== TIP.for) TIP.timer = setTimeout(() => showTip(el), TIP.for ? 0 : 150);
});
document.addEventListener("pointerout", (e) => {
  if (e.pointerType === "touch") return;
  const el = tipTarget(e);
  if (!el || el.contains(e.relatedTarget)) return;
  clearTimeout(TIP.timer);
  if (el === TIP.for && !TIP.pinned) TIP.timer = setTimeout(hideTip, 80);
});
document.addEventListener("focusin", (e) => { const el = tipTarget(e); if (el && e.target === el) showTip(el); });
document.addEventListener("focusout", (e) => { if (e.target === TIP.for && !TIP.pinned) hideTip(); });
// Taps and clicks pin a tip open on info dots and terms; controls (seg buttons, row actions) keep their own click.
document.addEventListener("click", (e) => {
  const el = tipTarget(e);
  if (el && !el.matches("button, a, input, select, [role=option]")) {
    e.preventDefault();   // an info dot inside a <label> must not open the labelled control
    if (el === TIP.for && TIP.pinned) hideTip(); else showTip(el, true);
  } else hideTip();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
// Scrolling a dropdown list keeps its item's tip, moved along with the item.
window.addEventListener("scroll", (e) => (e.target instanceof Element && e.target.matches(".tsel-list") && e.target.contains(TIP.for)
  ? showTip(TIP.for) : hideTip()), true);
window.addEventListener("resize", hideTip);

// ---------- charts ----------

function disposeCharts() { S.charts.forEach((c) => c.dispose()); S.charts = []; }
function chart(el) {
  const c = echarts.init(el, null, { renderer: "svg" });
  S.charts.push(c);
  return c;
}
window.addEventListener("resize", () => S.charts.forEach((c) => c.resize()));

const narrow = () => document.documentElement.clientWidth < 600;
function baseOption() {
  const ink2 = css("--ink-2"), muted = css("--muted"), grid = css("--grid"), axis = css("--axis"), surface = css("--surface"), ink = css("--ink");
  return {
    animationDuration: 300,
    textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: ink2 },
    grid: { left: 16, right: 28, top: 36, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis", backgroundColor: surface, borderColor: css("--border") || axis, textStyle: { color: ink, fontSize: 12 },
      axisPointer: { type: "line", lineStyle: { color: axis } }, confine: true,
    },
    legend: { type: "scroll", top: 0, left: 0, right: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: ink2, fontSize: 12 } },
    xAxis: { axisLine: { lineStyle: { color: axis } }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: 11, hideOverlap: true }, splitLine: { show: false } },
    yAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: 11, hideOverlap: true }, splitLine: { lineStyle: { color: grid } } },
  };
}
function timeLabel(gran) {
  return (v) => {
    const d = new Date(Number(v));   // category axes hand the formatter strings
    if (gran === "hour") return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
}
// Time-axis labels in the page's language and calendar (ECharts would print English month names).
const timeAxis = (v) => new Date(v).toLocaleString(LOC, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
// Axis labels for bucket times (seconds): hourly buckets show the clock time, with the date
// only on the first label and where the day changes, so the date is not repeated on every tick.
function axisLabels(gran, times) {
  if (gran !== "hour") return times.map((t) => timeLabel(gran)(t * 1000));
  return times.map((t, i) => {
    const d = new Date(t * 1000);
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    const newDay = i === 0 || d.toDateString() !== new Date(times[i - 1] * 1000).toDateString();
    return newDay ? `${timeLabel("day")(t * 1000)} ${time}` : time;
  });
}

// Stacked bars over time from API points [{t, key, <metric>}].
function stackedTime(el, points, metric, dim, gran, opts = {}) {
  const none = dim === "model" ? NO_MODEL : "—";
  points = points.map((p) => (p.key == null ? { ...p, key: none } : p));
  const totals = {};
  points.forEach((p) => { totals[p.key] = (totals[p.key] || 0) + (p[metric] || 0); });
  if (Object.values(totals).some((v) => v > 0)) points = points.filter((p) => totals[p.key] > 0);
  const keep = new Set(topKeys(totals));
  const fold = (k) => (keep.has(k) ? k : OTHER);
  const seen = [...new Set(points.map((p) => p.t))].sort((a, b) => a - b);
  // Fill empty buckets so gaps in time stay visible (daily and weekly buckets can shift an hour at DST).
  const step = { hour: 3600, day: 86400, week: 604800 }[gran];
  const times = [];
  if (seen.length && step) {
    for (let t = seen[0], i = 0; t <= seen[seen.length - 1] + 1; t += step) {
      while (i < seen.length && seen[i] < t + step / 2) times.push(seen[i++]);
      if (!times.length || times[times.length - 1] < t - step / 2) times.push(t);
    }
  } else times.push(...seen);
  const series = {};
  points.forEach((p) => {
    const k = fold(p.key);
    (series[k] ||= new Map()).set(p.t, (series[k].get(p.t) || 0) + (p[metric] || 0));
  });
  const keys = Object.keys(series).sort((a, b) => (a === OTHER) - (b === OTHER) || totals[b] - totals[a]);
  const c = chart(el);
  const o = baseOption();
  c.setOption({
    ...o,
    tooltip: { ...o.tooltip, valueFormatter: (v) => fmtMetric(metric, v) },
    legend: { ...o.legend, show: keys.length > 1, data: keys },
    xAxis: { ...o.xAxis, type: "category", data: axisLabels(gran, times) },
    yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => fmtMetric(metric, v) } },
    dataZoom: times.length > 30 ? [{ type: "inside" }] : [],
    series: keys.map((k) => ({
      name: k, type: "bar", stack: "total", barMaxWidth: 28, emphasis: { focus: "series" },
      itemStyle: { color: colorFor(dim, k), borderColor: css("--surface"), borderWidth: 1, borderRadius: 0 },
      data: times.map((t) => series[k].get(t) || 0),
    })),
    ...(opts.extra || {}),
  });
  return { keys, times, series };
}

function tableView(title, head, rows) {
  return `<details class="tableview"><summary>${esc(title)}</summary><div class="table-wrap"><table class="data">
    <thead><tr>${head.map((h, i) => `<th class="${i ? "r" : ""}">${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((v, i) => `<td class="${i ? "r" : ""}">${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
}

// options: [value, label, tip key?]
function seg(name, options, value) {
  return `<span class="seg" data-pref="${name}">${options.map(([v, label, tip]) =>
    `<button type="button" data-v="${v}" aria-pressed="${v === value}"${TIPS[tip] ? ` data-tip="${esc(tip)}"` : ""}>${esc(label)}</button>`).join("")}</span>`;
}
function wireSegs(root, rerender) {
  root.querySelectorAll(".seg[data-pref]").forEach((s) => s.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    S.prefs[s.dataset.pref] = b.dataset.v; savePrefs(); rerender();
  }));
}

function meter(pct) {
  const cls = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
  return `<div class="meter ${cls}"><span style="width:${Math.min(100, Math.max(0, pct || 0)).toFixed(1)}%"></span></div>`;
}
function limitLabel(l) {
  const scope = l.scope && l.scope !== "*" ? ` [${l.scope}]` : "";
  return `${t(`kname.${l.kind}`)}${scope}`;
}
function limitValue(l) {
  if (l.kind === "allowed_models") return l.value;
  const reset = l.reset_in ? t("lim.resets_in", { d: fmtDur(l.reset_in) }) : "";
  if (USER_LIMIT_KINDS.includes(l.kind)) {
    if (l.skipped || l.current == null) return t("lim.not_measured");
    return `${t("lim.used_pct", { v: nf0.format(l.current) })}${l.no_live_data ? t("lim.est_no_live") : ""}${reset}`;
  }
  if (l.skipped) return t("lim.skipped", { why: l.skipped });
  const f = l.unit === "usd" ? fmtUsd : l.unit === "pct" ? (v) => t("lim.pts", { v: nfFix(1).format(v) }) : fmtNum;
  return `${l.no_live_data ? t("lim.est_nolive_prefix") : l.estimated ? t("lim.est_prefix") : ""}${f(l.current || 0)} / ${f(l.limit)}${reset}`;
}
function limitValueTip(l) {
  if (l.kind === "allowed_models") return "";
  if (USER_LIMIT_KINDS.includes(l.kind)) return l.skipped ? t("lim.tip_user_skipped") : "";
  if (l.no_live_data) return t("lim.tip_no_live");
  if (l.skipped) return t("lim.tip_skipped");
  if (l.estimated) return t("lim.tip_est");
  if (!l.reset_in) return "";
  return t("lim.tip_resets");
}
function limitsBlock(ls) {
  if (!ls.length) return `<span class="muted">${t("lim.none")}</span>`;
  return ls.map((l) => `<div class="limit-row"><span>${tipT(esc(limitLabel(l)), `kind:${l.kind}`)}</span><span class="muted num">${tipH(esc(limitValue(l)), limitValueTip(l))}</span>
    <div style="grid-column:1/-1">${l.kind === "allowed_models" || l.skipped ? "" : meter(l.pct)}</div></div>`).join("");
}

// ---------- views ----------

const VIEWS = {
  overview: { render: renderOverview },
  usage: { render: renderUsage },
  users: { render: renderUsers, admin: true },
  tickets: { render: renderTickets, admin: true, feature: "tickets" },
  orders: { render: renderOrders, admin: true, feature: "tickets" },
  pricing: { render: renderPricing, admin: true, feature: "tickets" },
  authorize: { render: renderAuthorize, hidden: true },   // #authorize/<code>, opened by gclaude or claude-gateway on
  quota: { render: renderQuota, admin: true },
  models: { render: renderModels },
  activity: { render: renderActivity },
  sessions: { render: renderSessions },
  errors: { render: renderErrors },
  audit: { render: renderAudit, admin: true },
  user: { render: renderUser, admin: true, hidden: true, parent: "users" },   // #user/<id>, opened from Users & limits
};
const isAdmin = () => S.user && S.user.role === "admin";

function renderTabs() {
  const tabs = Object.entries(VIEWS).filter(([, v]) => !v.hidden && (!v.admin || isAdmin()) && (!v.feature || S.tickets?.enabled));
  const current = VIEWS[S.tab]?.parent || S.tab;
  const badge = (k) => (k === "orders" && S.tickets?.orders_new > 0 ? ` <span class="badge tab-count" aria-label="${esc(t("tab.orders_new", { n: S.tickets.orders_new }))}">${nf0.format(S.tickets.orders_new)}</span>` : "");
  $("#tabs").innerHTML = tabs.map(([k]) => `<button role="tab" data-tab="${k}" aria-selected="${k === current}">${esc(t(`tab.${k}`))}${badge(k)}</button>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  S.tab = b.dataset.tab; location.hash = S.tab; render();
});

async function render() {
  renderTabs();
  hideTip();
  disposeCharts();
  const view = VIEWS[S.tab] && (!VIEWS[S.tab].admin || isAdmin()) && (!VIEWS[S.tab].feature || S.tickets?.enabled) ? VIEWS[S.tab] : VIEWS.overview;
  const main = $("#main");
  main.innerHTML = `<p class="muted">${t("app.loading")}</p>`;
  try { await view.render(main); } catch (e) {
    if (e.message !== "signed out") main.innerHTML = `<div class="banner critical"><span class="icon">!</span><span>${esc(e.message)}</span></div>`;
  }
  if (S.toPrices) { S.toPrices = false; scrollToPrices(); }
}
// The user's own price list; when it didn't load (the tickets call failed), the public one.
function scrollToPrices() {
  const el = document.getElementById("prices");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  else location.href = "/?home#pricing";
}

function credentialPill(c) {
  const pill = $("#cred-pill");
  pill.querySelector(".dot").style.background = c.healthy ? css("--good") : css("--critical");
  pill.querySelector("span:last-child").textContent = c.healthy ? t("cred.linked") : t("cred.needed");
  pill.dataset.tipHtml = (c.healthy ? t("cred.linked_tip") : t("cred.needed_tip")) + (c.detail ? `<p class="tm">${esc(c.detail)}</p>` : "");
}

async function renderOverview(main) {
  const buyer = !isAdmin() && S.tickets?.enabled;
  const [ov, me, keys, tk, mo] = await Promise.all([api("/api/overview"), isAdmin() ? null : api("/api/me/status"), api("/api/keys"), buyer ? api("/api/me/tickets") : null,
    buyer ? api("/api/me/orders").catch((e) => { if (e.message === "signed out") throw e; return null; }) : null]);
  const order = mo && mo.order;
  if (ov.credential) credentialPill(ov.credential);
  if (tk && typeof tk.now === "number") S.tkSkew = tk.now - Date.now() / 1000;
  const tot = ov.totals[S.prefs.period] || ov.totals["24h"];
  const banners = [];
  if (ov.credential && !ov.credential.healthy) banners.push(`<div class="banner critical"><span class="icon">!</span><span>${t("ov.cred_fail")} ${isAdmin() ? `${t("ov.cred_fail_admin")}${ov.credential.detail ? ` <span class="muted">(${esc(ov.credential.detail)})</span>` : ""}` : t("ov.cred_fail_user")}</span></div>`);
  const stale = (ov.quota || []).filter((q) => q.utilization_pct != null && q.stale);
  if (stale.length) banners.push(`<div class="banner warning"><span class="icon">⚠</span><span>${t("ov.stale", { buckets: stale.map((q) => esc(t(`bucket.${q.bucket}`))).join(t("app.list_sep")) })}</span></div>`);
  const unpriced = tot.unpriced_models || [];
  const ex = ov.exhaustion;
  main.innerHTML = `
    <section class="view">
      <h2>${isAdmin() ? t("ov.title_admin") : t("ov.title_user", { name: bdi(S.user.name) })}</h2>
      <p class="lede">${isAdmin() ? t("ov.lede_admin") : t("ov.lede_user")}</p>
      ${banners.join("")}
      <div class="controls">${seg("period", [["24h", t("period.24h")], ["7d", t("period.7d")], ["30d", t("period.30d")]], S.prefs.period)}</div>
      <div class="tiles">
        <div class="card tile"><div class="label">${t("metric.requests")}${tipI("requests")}</div><div class="value">${fmtNum(tot.requests)}</div><div class="foot">${tot.requests >= 1000 ? t("ov.n_forwarded", { n: esc(nfFull.format(tot.requests)) }) : t("ov.forwarded")}</div></div>
        <div class="card tile"><div class="label">${t("metric.weighted")}${tipI("weighted")}</div><div class="value">${fmtNum(tot.weighted)}</div><div class="foot">${t("ov.in_ref", { ref: esc(refModel()) })}</div></div>
        <div class="card tile"><div class="label">${t("metric.raw")}${tipI("raw")}</div><div class="value">${fmtNum(tot.raw)}</div><div class="foot">${t("ov.cache_reads", { n: fmtNum(tot.cache_read) })}</div></div>
        <div class="card tile"><div class="label">${t("ov.cost")}${tipI("cost")}</div><div class="value">${fmtUsd(tot.cost_usd)}</div><div class="foot">${unpriced.length ? t("ov.unpriced", { models: bdi(unpriced.join(", ")) }) : isAdmin() ? t("ov.not_billed") : t("ov.list_prices")}</div></div>
        ${isAdmin() ? `<div class="card tile"><div class="label">${t("ov.active_users")}${tipI("active_users")}</div><div class="value">${nfFull.format(ov.active_users_24h)}</div><div class="foot">${t("ov.last_24h")}</div></div>` : ""}
        <div class="card tile"><div class="label">${t("ov.burn_rate")}${tipI("burn_rate")}</div><div class="value">${fmtNum(ov.burn_rate_weighted_per_min)}</div><div class="foot">${t("ov.burn_foot")}</div></div>
      </div>
      <div class="grid${isAdmin() ? " cols-2" : ""}">
        ${isAdmin() ? `<div class="card"><h3>${t("ov.quota_title")}${tipI("quota")}</h3>
          <p class="sub">${t("ov.quota_sub", { share: tipT(t("ov.est_share"), "share"), unattributed: tipT(t("ov.not_attributed"), "unattributed") })}</p>
          <div id="quota-bars"></div>
          ${ex && ex.pct_per_hour > 0 ? `<p class="sub" style="margin-top:12px">${t("ov.rising", { v: nfFix(1).format(ex.pct_per_hour) })}${ex.eta_s ? t("ov.fills_in", { d: fmtDur(ex.eta_s) }) + (ex.before_reset ? t("ov.before_reset") : t("ov.after_reset")) : ""}.${tipI("exhaustion")}</p>` : ""}
        </div>` : ""}
        ${isAdmin() ? usersCard() : userLimitsCard(me, tk)}
      </div>
      ${machinesCard(keys.keys, S.install)}
      ${tk ? myOrderCard(order) + priceListCard(tk, order) : ""}
    </section>`;
  wireSegs(main, render);
  wireMachines(main);
  if (tk) wireOrdering(main, tk, order);
  if (isAdmin()) $("#quota-bars").innerHTML = ov.quota.map(quotaBar).join("") || `<p class="muted">${t("ov.no_figures")}</p>`;
  if (isAdmin()) {
    const s = await api(`/api/series?range=7d&granularity=day&split=user&tz_offset=${tzOffset()}`);
    stackedTime($("#ov-users"), s.points, "weighted", "user", "day");
  }
  if (tk) {
    const p2 = new Intl.NumberFormat(LOC, { minimumIntegerDigits: 2 });
    const tick = () => {
      let ended = false;
      main.querySelectorAll(".countdown").forEach((el) => {
        const s = Math.max(0, Math.floor(+el.dataset.ends - Date.now() / 1000 - S.tkSkew));
        if (!s && !S.discountsEnded.has(el.dataset.ends)) { S.discountsEnded.add(el.dataset.ends); ended = true; }
        el.textContent = s ? t("price.offer_ends", { d: nf0.format(Math.floor(s / 86400)), h: p2.format(Math.floor((s % 86400) / 3600)), m: p2.format(Math.floor((s % 3600) / 60)), s: p2.format(s % 60) }) : t("price.offer_ended");
      });
      if (ended) { clearInterval(S.countdown); render(); }   // once per end: the regular price returns
    };
    tick();
    clearInterval(S.countdown); S.countdown = setInterval(() => (document.contains(main.querySelector(".countdown")) ? tick() : clearInterval(S.countdown)), 1000);
  }
}

function usersCard() {
  return `<div class="card"><h3>${t("ov.by_user_title")}</h3><p class="sub">${t("ov.by_user_sub")}</p><div class="chart short" id="ov-users"></div></div>`;
}
function userLimitsCard(me, tk) {
  if (me.paused) return `<div class="card"><h3>${t("ov.your_limits")}</h3><div class="ticket">${t("ov.paused")}</div></div>`;
  const c = tk && tk.current;
  const ticket = !tk || !tk.gated ? "" : c ? `<div class="ticket">
      <div>${t("ov.ticket_line", { label: bdi(c.label), share: fmtShare(c.share_pct) })}</div>
      <div class="muted">${t("ov.ends", { date: fmtDate(c.effective_end) })}${c.bonus_days ? ` <span class="badge">${plural("ov.bonus_days", c.bonus_days, { n: nf0.format(c.bonus_days) })}</span>` : ""} · ${t("ov.today_ends", { d: fmtDur(c.day_end - Date.now() / 1000) })}</div>
      ${c.bonuses.map((b) => `<div class="badge bonus">${t("ov.bonus_share", { share: fmtShare(b.share_pct), date: fmtDate(b.ends_at) })}${b.note ? ` · ${bdi(b.note)}` : ""}</div>`).join("")}
      ${(c.day_bonuses || []).filter((b) => b.note).map((b) => `<div class="badge bonus">${plural("ov.bonus_extra_days", b.extra_days, { n: nf0.format(b.extra_days) })} · ${bdi(b.note)}</div>`).join("")}
      ${c.bonus_share ? `<p class="sub">${t("ov.bonus_measured", { share: fmtShare(c.share_pct + c.bonus_share) })}</p>` : ""}
      ${tk.queued ? `<div class="muted">${t("ov.next", { label: bdi(tk.queued.label), date: fmtDate(tk.queued.starts_at) })}</div>` : ""}</div>`
    : tk.queued ? `<div class="ticket">${t("ov.next_starts", { label: bdi(tk.queued.label), date: fmtDate(tk.queued.starts_at) })}</div>`
    : `<div class="ticket">${t("ov.ticket_ended")} ${esc(tk.how_to_buy || t("ov.ask_admin"))}</div>`;
  return `<div class="card"><h3>${c ? t("ov.your_ticket") : t("ov.your_limits")}</h3>
    <p class="sub">${c ? t("ov.ticket_sub") : t("ov.limits_sub", { served: tipT(t("ov.served"), "served") })}</p>
    ${ticket}${limitsBlock(me.limits)}</div>`;
}
// "You picked Lite for a week." for a tier and length the price list still offers; anything else says nothing.
function pickedLine(prices, pick) {
  const tier = pick && typeof pick === "object" ? prices.tiers.find((x) => x.tier === pick.tier) : null;
  const len = tier && ["day", "week", "month"].includes(pick.length) && t(`price.a_${pick.length}`);
  return len ? t("price.picked", { tier: esc(tier.label), len }) : "";
}
function priceListCard(tk, order) {
  const p = tk.prices;
  const pick = takePick();
  const picked = pickedLine(p, pick);
  const open = !!order && (order.status === "new" || order.status === "contacted");   // one open order at a time
  const now = Date.now() / 1000 + S.tkSkew;
  // A discount that ended since the server answered shows the regular price; the countdown re-renders at its end.
  const live = (l) => l.discount_ends_at && l.discount_ends_at > now;
  const L = [["day", t("price.1_day")], ["week", t("price.1_week")], ["month", t("price.1_month")]];
  const hint = (tier) => Object.entries({ sonnet: "Sonnet", opus: "Opus" }).map(([f, n]) => { const h = tier.hours[f]; return h && (h.per_5h != null || h.per_day != null)
    ? `<div class="muted">${t("price.at_least", { model: n, what: [h.per_5h != null ? t("price.h_per_5h", { n: nfFull.format(h.per_5h) }) : null, h.per_day != null ? t("price.h_per_day", { n: nfFull.format(h.per_day) }) : null].filter(Boolean).join(t("app.list_sep")) })}</div>` : ""; }).join("");
  // The home page's pick is the highlighted Order button; a sold-out price has none.
  const orderBtn = (tier, k) => `<div><button class="btn small${pick && pick.tier === tier.tier && pick.length === k ? " primary" : ""} order-btn" data-order="${esc(tier.tier)}:${k}"${open ? ` disabled title="${esc(t("price.open_order"))}"` : ""}>${t("price.order")}</button></div>`;
  const cell = (tier, k) => { const l = tier.lengths[k]; return `<td class="r">${live(l) ? `<s class="muted">${esc(money(l.list_amount, p.currency))}</s> ` : ""}<b>${esc(money(live(l) || !l.discount_ends_at ? l.amount : l.list_amount, p.currency))}</b>${l.sold_out ? ` <span class="badge">${t("price.sold_out")}</span>` : ""}
    ${live(l) ? `<div class="muted countdown" data-ends="${l.discount_ends_at}"></div>` : ""}${l.sold_out ? "" : orderBtn(tier, k)}</td>`; };
  return `<div class="card" id="prices"><h3>${t("tab.tickets")}</h3><p class="sub">${t("price.sub")}${p.rate_set_at ? t("price.rate_at", { date: fmtDate(p.rate_set_at) }) : ""}</p>
    <div class="table-wrap"><table class="data"><thead><tr><th>${t("price.tier")}</th>${L.map(([, n]) => `<th class="r">${n}</th>`).join("")}</tr></thead><tbody>
    ${p.tiers.map((tier) => `<tr><td><b>${bdi(tier.label)}</b> <span class="muted">${fmtShare(tier.share_pct)}</span><div class="muted">≈ ${esc(tier.compare)}</div>${hint(tier)}</td>${L.map(([k]) => cell(tier, k)).join("")}</tr>`).join("")}
    </tbody></table></div><p class="sub">${picked}${esc(tk.how_to_buy || (picked ? t("ov.ask_admin") : ""))}</p></div>`;
}

// ---------- order requests: the buyer's side (design 2026-10-04, section 8) ----------

// The open order with Withdraw, or the latest closed one with Dismiss until it is dismissed. The admin note never reaches here.
function myOrderCard(o) {
  if (!o) return "";
  const what = `${esc(o.label)}, ${{ day: "1 day", week: "1 week", month: "1 month" }[o.length] || esc(o.length)}`;
  if (o.status === "new" || o.status === "contacted") {
    return `<div class="card order-note" id="my-order"><div><b>Order received: ${what}. The admin will contact you.</b>
      <div class="muted">Quoted ${esc(money(o.quoted_amount, o.currency))} · ${o.status === "contacted" ? "the admin has been in touch" : "waiting for the admin"}</div></div>
      <button class="btn small" data-my-order="withdraw" data-id="${o.id}">Withdraw</button></div>`;
  }
  const text = o.status === "done" ? "Your order is done." : o.status === "declined" ? "Your order was declined." : "";
  return text ? `<div class="card order-note" id="my-order"><div>${text} <span class="muted">${what}</span></div>
    <button class="btn small" data-my-order="dismiss" data-id="${o.id}">Dismiss</button></div>` : "";
}
// What a buyer can order in: the price list's currency and USD, which always has a rate.
function orderCurrencies(p) { return [...new Set([p.currency, "USD"])]; }
function orderFormHtml(p, t, len, email) {
  const l = t.lengths[len];
  const price = (c) => (c === "USD" ? money(l.usd, "USD") : money(l.amount, c));
  return `<h3>Order ${esc(t.label)}, ${{ day: "1 day", week: "1 week", month: "1 month" }[len] || esc(len)}</h3>
    <p class="order-price"><b id="order-price">${esc(price(p.currency))}</b> <span class="muted">at today's rate</span></p>
    <form id="f-order" class="form-grid order-form">
      <label>Currency<select name="currency">${orderCurrencies(p).map((c) => `<option value="${esc(c)}"${c === p.currency ? " selected" : ""} data-price="${esc(price(c))}">${esc(c)}</option>`).join("")}</select></label>
      ${email ? "" : `<label class="wide">Your email<input type="email" name="email" required maxlength="254" autocomplete="email" placeholder="you@example.com"><span class="muted">The admin replies here. It is kept with this order only.</span></label>`}
      <label class="wide">Message (optional)<textarea name="message" maxlength="1000" rows="3" placeholder="Anything the admin should know"></textarea></label>
      <p class="hint">This is a request, not a payment. The admin will contact you with payment details.</p>
      <button class="btn primary" type="submit">Send order</button>
    </form><div class="error" id="order-err"></div><p><button class="btn" data-close>Cancel</button></p>`;
}
function orderDialog(p, t, len) {
  const d = openDialog(orderFormHtml(p, t, len, S.user.email));
  const f = $("#f-order", d);
  f.currency.onchange = () => { $("#order-price", d).textContent = f.currency.selectedOptions[0].dataset.price; };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const go = f.querySelector('button[type="submit"]');
    go.disabled = true;
    try {
      await api("/api/me/orders", { method: "POST", body: { tier: t.tier, length: len, currency: f.currency.value, message: f.message.value, ...(f.email ? { email: f.email.value } : {}) } });
      d.close(); render();
    } catch (err) { go.disabled = false; $("#order-err", d).textContent = err.message; }
  };
}
function wireOrdering(root, tk, order) {
  root.querySelectorAll("[data-order]").forEach((b) => (b.onclick = () => {
    const [tier, len] = b.dataset.order.split(":");
    const t = tk.prices.tiers.find((x) => x.tier === tier);
    if (t) orderDialog(tk.prices, t, len);
  }));
  root.querySelectorAll("[data-my-order]").forEach((b) => (b.onclick = async () => {
    const act = b.dataset.myOrder;
    if (act === "withdraw" && !confirmInline("Withdraw your order? The admin will no longer act on it.")) return;
    try { await api(`/api/me/orders/${+b.dataset.id}/${act}`, { method: "POST", body: {} }); render(); } catch (e) { alertInline(e.message); }
  }));
}

function quotaBar(q) {
  const name = q.bucket === "5h" || q.bucket === "7d" ? t(`quota.bucket_${q.bucket}`) : esc(q.bucket);
  if (q.utilization_pct == null) return `<div style="margin-bottom:14px"><b>${name}</b> <span class="muted">${t("quota.no_data")}</span></div>`;
  const shares = Object.entries(q.shares).sort((a, b) => b[1] - a[1]);
  const segs = shares.map(([k, v]) => [k, v]).concat([["unattributed", q.unattributed || 0]]).filter(([, v]) => v > 0.05);
  const resets = q.resets_at ? t("quota.resets_in", { d: fmtDur(q.resets_at - Date.now() / 1000) }) : "";
  const pts = (v) => nfFix(1).format(v);
  return `<div style="margin-bottom:16px">
    <div style="display:flex;justify-content:space-between;align-items:baseline"><span><b>${tipT(name, `bucket:${q.bucket}`)}</b>
      <span style="font-size:22px;font-weight:650;margin-inline-start:8px">${fmtPct(q.utilization_pct)}</span></span>
      <span class="muted">${esc(resets)}${q.stale ? ` · ${tipT(t("quota.stale"), "stale")}` : ""}</span></div>
    <div class="stack" role="img" aria-label="${esc(t("quota.aria", { bucket: name, pct: fmtPct(q.utilization_pct) }))}">
      ${segs.map(([k, v]) => `<span${tipAttr(k === "unattributed" ? t("quota.seg_unattributed", { v: pts(v) })
        : t("quota.seg_user", { name: bdi(k), v: pts(v) }))} style="width:${v}%;background:${colorFor("user", k)}"></span>`).join("")}
    </div>
    <div class="legend">${segs.map(([k, v]) => `<span><i style="background:${colorFor("user", k)}"></i>${k === "unattributed" ? tipT(t("ov.not_attributed"), "unattributed") : bdi(k)} ${pts(v)}</span>`).join("")}</div>
  </div>`;
}

async function renderUsage(main) {
  const p = S.prefs;
  const splits = isAdmin() ? [["user", t("usage.by_user")], ["model", t("usage.by_model")], ["provider", t("usage.by_provider"), "split:provider"]] : [["model", t("usage.by_model")], ["provider", t("usage.by_provider"), "split:provider"]];
  if (!isAdmin() && p.split === "user") p.split = "model";
  const d = await api(`/api/series?range=${p.range}&granularity=${p.granularity}&split=${p.split}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>${t("tab.usage")}</h2>
    <p class="lede">${t(isAdmin() ? "usage.lede_admin" : "usage.lede_user", { weighted: tipT(t("metric.weighted"), "weighted"), raw: tipT(t("metric.raw"), "raw"), ref: esc(refModel()) })}</p>
    <div class="controls">
      ${seg("range", [["1d", t("range.1d")], ["7d", t("range.7d")], ["30d", t("range.30d")], ["90d", t("range.90d")]], p.range)}
      ${seg("granularity", [["hour", t("gran.hour")], ["day", t("gran.day")], ["week", t("gran.week")]], p.granularity)}
      ${seg("split", splits, p.split)}
      ${seg("metric", Object.entries(METRICS).map(([k, v]) => [k, v, `metric:${k}`]), p.metric)}
    </div>
    <div class="card"><h3>${t(`usage.per_${p.granularity}`, { metric: esc(METRICS[p.metric]) })}</h3><p class="sub" id="usage-hint">${t("usage.zoom")}</p>
      <div class="chart tall" id="usage-chart"></div><div id="usage-table"></div></div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#usage-chart").outerHTML = `<p class="muted">${t("usage.none")}</p>`; return; }
  const { keys, times, series } = stackedTime($("#usage-chart"), d.points, p.metric, p.split, p.granularity);
  if (keys.length > 1) $("#usage-hint").textContent = t("usage.zoom_legend");
  $("#usage-table").innerHTML = tableView(t("app.show_table"), [t("usage.period"), ...keys.map(seriesLabel)], times.map((tm) =>
    [timeLabel(p.granularity)(tm * 1000), ...keys.map((k) => fmtMetric(p.metric, series[k].get(tm) || 0))]));
}

async function renderUsers(main) {
  const [{ users }, lim] = await Promise.all([api("/api/users"), api("/api/limits")]);
  S.kinds = lim.kinds;
  main.innerHTML = `<section class="view"><h2>${t("tab.users")}</h2>
    <p class="lede">${t("users.lede", { share: tipT(t("users.est_share"), "share") })}</p>
    <div class="controls"><button class="btn primary" id="add-user">${t("users.add")}</button></div>
    <div class="card table-wrap"><table class="data"><thead><tr>
      <th>${t("app.user")}${tipI("key_prefix", t("users.about_prefix"))}</th><th class="r">${t("range.1d")}</th><th class="r">${t("range.7d")}</th><th class="r">${t("range.30d")}</th><th class="r">${t("users.share_col")}${tipI("share_col")}</th><th>${t("users.limits")}${tipI("limits_col")}</th><th>${t("users.last_request")}</th><th></th></tr></thead>
      <tbody>${users.map(userRow).join("")}</tbody></table></div>
    <p class="muted" style="font-size:12px;margin-top:8px">${t("users.foot")}</p>
  </section>`;
  $("#add-user").onclick = addUserDialog;
  wireUserActions(main, users);
  // A click anywhere on a row, except its buttons, links and tips, opens that user's page.
  main.querySelectorAll("tr[data-user]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (!e.target.closest("button, a, [data-tip], [data-tip-html]")) location.hash = `user/${tr.dataset.user}`;
  }));
}
function wireUserActions(root, users) {
  root.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => userAction(b.dataset.act, +b.dataset.id, users.find((u) => u.id === +b.dataset.id))));
}

function userRow(u) {
  const state = u.revoked ? `<span class="badge">${t("users.revoked")}</span>` : !u.enabled ? `<span class="badge">${t("users.disabled")}</span>` : "";
  const cell = (k) => `<td class="r"><div>${fmtNum(u.usage[k].weighted)}</div><div class="muted">${fmtUsd(u.usage[k].cost_usd)}</div></td>`;
  const share = (b) => (u.share[b] == null ? "—" : nfFix(1).format(u.share[b]));
  return `<tr class="clickable" data-user="${u.id}">
    <td><span class="dot" style="background:${colorFor("user", u.name)};margin-inline-end:6px"></span><a class="user-link" href="#user/${u.id}"><b>${bdi(u.name)}</b></a> ${u.role === "admin" ? `<span class="badge">${t("users.admin")}</span>` : ""} ${state}${u.ticket && u.ticket.paused && u.ticket.gated ? `<span class="badge">${t("users.tickets_paused")}</span>` : u.ticket && u.ticket.live ? `<span class="badge">${u.ticket.current ? t("users.ticket") : t("users.ticket_queued")}</span>` : u.ticket && u.ticket.gated ? `<span class="badge">${t("users.ticket_ended")}</span>` : ""}
      <div class="muted" style="font-size:12px"><bdi>${esc(u.prefix)}…</bdi>${u.routes_prefix ? ` · OpenCode <bdi>${esc(u.routes_prefix)}…</bdi>` : ""}</div></td>
    ${cell("24h")}${cell("7d")}${cell("30d")}
    <td class="r">${share("5h")} / ${share("7d")}</td>
    <td style="min-width:240px">${limitsBlock(u.limits)}</td>
    <td class="muted nowrap">${fmtAgo(u.last_seen)}</td>
    <td>${userActions(u)}</td></tr>`;
}
function userActions(u) {
  const opencode = `<button class="btn small" data-act="routes_key" data-id="${u.id}" data-tip="act_routes_key">${u.routes_prefix ? t("users.new_opencode") : t("users.opencode")}</button>` +
    (u.routes_prefix ? `<button class="btn small" data-act="routes_key_remove" data-id="${u.id}" data-tip="act_routes_key_remove">${t("users.remove_opencode")}</button>` : "");
  const upgrade = u.limits.some((l) => l.kind === "cost_total") && !u.revoked
    ? `<button class="btn small" data-act="upgrade" data-id="${u.id}" data-tip="act_upgrade">${t("users.upgrade")}</button>` : "";
  const paused = u.ticket && u.ticket.paused;
  const ungate = u.ticket && u.ticket.gated && (!u.ticket.live || paused) && !u.revoked
    ? `<button class="btn small" data-act="ungate" data-id="${u.id}" data-tip="${paused ? "act_ungate_paused" : "act_ungate"}">${t("users.ungate")}</button>` : "";
  return `<div class="row-actions">${upgrade}${ungate}${u.revoked ? `<button class="btn small danger" data-act="delete" data-id="${u.id}" data-tip="act_delete">${t("users.delete")}</button>` : u.id === S.user.id ? `<button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">${t("users.limits")}</button>${opencode}` : `
      <button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">${t("users.limits")}</button>
      <button class="btn small" data-act="rename" data-id="${u.id}" data-tip="act_rename">${t("users.rename")}</button>
      <button class="btn small" data-act="rotate" data-id="${u.id}" data-tip="act_rotate">${t("users.rotate")}</button>
      ${opencode}
      <button class="btn small" data-act="${u.enabled ? "disable" : "enable"}" data-id="${u.id}" data-tip="act_${u.enabled ? "disable" : "enable"}">${u.enabled ? t("users.disable") : t("users.enable")}</button>
      <button class="btn small danger" data-act="revoke" data-id="${u.id}" data-tip="act_revoke">${t("users.revoke")}</button>`}</div>`;
}

// ---------- paid tickets (design 2026-10-03) ----------

const stateBadge = (s) => `<span class="badge state-${s}">${esc(s)}</span>`;

async function renderTickets(main) {
  const [cap, { tickets, deleted_users }, { users }] = await Promise.all([api("/api/admin/capacity"), api(`/api/admin/tickets${S.ticketUser == null ? "" : typeof S.ticketUser === "number" ? `?user_id=${S.ticketUser}` : `?deleted=${encodeURIComponent(S.ticketUser.slice(2))}`}`), api("/api/users")]);
  const pct = (v, max) => meter((100 * v) / max);
  const util = (b) => (cap.utilization[b].utilization_pct == null ? "—" : `${cap.utilization[b].utilization_pct.toFixed(0)}%${cap.utilization[b].stale ? " (stale)" : ""}`);
  main.innerHTML = `<section class="view"><h2>Tickets</h2>
    <p class="lede">Each ticket reserves a share of the subscription for its days. The gateway never sells the same capacity twice.</p>
    <div class="grid cols-2">
      <div class="card"><h3>Capacity${tipI("capacity_sold")}</h3>
        <div class="limit-row"><span>Sold now</span><span class="num">${cap.sold_now_pct.toFixed(1)}% of ${cap.max_sold_pct}%</span></div>${pct(cap.sold_now_pct, cap.max_sold_pct)}
        <div class="limit-row" style="margin-top:8px"><span>Peak, next 30 days</span><span class="num">${cap.peak_30d_pct.toFixed(1)}% of ${cap.max_sold_pct}%</span></div>${pct(cap.peak_30d_pct, cap.max_sold_pct)}
        <p class="sub" style="margin-top:12px">Sold is what tickets may use. Headroom for everyone else: ${(100 - cap.max_sold_pct).toFixed(0)}%.</p></div>
      <div class="card"><h3>Account utilization (reported by Anthropic)${tipI("capacity_util")}</h3>
        <div class="limit-row"><span>5-hour bucket</span><span class="num">${util("5h")}</span></div>
        <div class="limit-row"><span>Weekly bucket</span><span class="num">${util("7d")}</span></div>
        <p class="sub" style="margin-top:12px">Utilization is what everyone has used, ticket holders and headroom users alike.</p></div>
    </div>
    <div class="controls"><button class="btn primary" id="grant">Grant a ticket</button>
      <select id="ticket-user"><option value="">All users</option>${users.filter((u) => u.ticket && u.ticket.has_tickets).map((u) => `<option value="${u.id}" ${S.ticketUser === u.id ? "selected" : ""}>${esc(u.name)}</option>`).join("")}${(deleted_users || []).map((n) => `<option value="d:${esc(n)}" ${S.ticketUser === `d:${n}` ? "selected" : ""}>${esc(n)} (deleted)</option>`).join("")}</select></div>
    <div class="card table-wrap"><table class="data"><thead><tr><th>User</th><th>Tier</th><th>Period</th><th class="r">Paid</th><th>State</th><th>Bonuses</th><th>Note</th><th></th></tr></thead>
      <tbody>${tickets.map(ticketRow).join("") || `<tr><td colspan="8" class="muted">No tickets yet.</td></tr>`}</tbody></table></div>
  </section>`;
  $("#grant").onclick = () => grantDialog(users);
  $("#ticket-user").onchange = (e) => { const v = e.target.value; S.ticketUser = !v ? null : v.startsWith("d:") ? v : +v; render(); };
  main.querySelectorAll("[data-tact]").forEach((b) => b.addEventListener("click", () => ticketAction(b.dataset.tact, tickets.find((t) => t.id === +b.dataset.id), tickets)));
}
function ticketRow(t) {
  const live = t.state === "active" || t.state === "queued";
  const bonus = t.bonuses.filter((b) => !b.cancelled_at).map((b) => `${b.share_pct ? `+${fmtShare(b.share_pct)}` : ""}${b.share_pct && b.extra_days ? " " : ""}${b.extra_days ? `+${b.extra_days} d` : ""}`).join(", ");
  return `<tr><td><b>${esc(t.user_name)}</b>${t.user_id == null ? ` <span class="badge">deleted</span>` : ""}</td>
    <td>${esc(t.tier)} <span class="muted">${fmtShare(t.share_pct)}</span></td>
    <td class="nowrap">${fmtDate(t.starts_at)} → ${fmtDate(t.effective_end)}${t.effective_end !== t.ends_at ? ` <span class="muted">(+${Math.round((t.effective_end - t.ends_at) / 86400)} d bonus)</span>` : ""}</td>
    <td class="r">${esc(money(t.amount, t.currency))}${t.discount_id ? `<div class="muted"><s>${fmtPrice(t.list_usd)}</s> ${fmtPrice(t.usd)}</div>` : `<div class="muted">${fmtPrice(t.usd)}</div>`}</td>
    <td>${stateBadge(t.state)}</td><td class="muted">${esc(bonus) || "—"}</td><td class="muted">${esc(t.note || "")}</td>
    <td><div class="row-actions">${live || t.state === "ended" ? `<button class="btn small" data-tact="bonus" data-id="${t.id}" data-tip="act_ticket_bonus">Bonus</button>` : ""}
      ${live ? `<button class="btn small danger" data-tact="cancel" data-id="${t.id}" data-tip="act_ticket_cancel">Cancel</button>` : ""}</div></td></tr>`;
}
async function ticketAction(act, t, list) {
  if (act === "bonus") return bonusDialog(t, list);
  if (!confirmInline(`Cancel ${t.user_name}'s ${t.tier} ticket? The slice is freed now and its bonuses end. Refunds happen outside the app.`)) return;
  try {
    const r = await api(`/api/admin/tickets/${t.id}/cancel`, { method: "POST", body: {} });
    render();   // the ticket is cancelled either way, and any queued tickets moved
    if (r.dates_kept) infoInline("Cancelled", `Their queued tickets kept their dates because one of them would not fit earlier: ${r.reason}`);
  } catch (e) { alertInline(e.message); }
}
// `pre`, from an order: {order_id, user, tier, length, currency}. The user is fixed to the order's account, and the
// grant carries order_id, so the server closes the order with it or refuses both (spec section 7).
function grantDialog(users, pre = null) {
  const T = S.tickets;
  const pickable = pre ? users.filter((u) => u.id === pre.user) : users.filter((u) => !u.revoked && u.enabled);
  const d = openDialog(`<h3>Grant a ticket${pre ? ` for order #${esc(pre.order_id)}` : ""}</h3>
    <form id="f-grant" class="form-grid">
      <label>User<select name="user" required${pre ? " disabled" : ""}>${pickable.map((u) => `<option value="${u.id}">${esc(u.name)}</option>`).join("")}</select></label>
      <label>Tier<select name="tier">${Object.entries(T.tiers).map(([k, t]) => `<option value="${esc(k)}">${esc(t.label)} · ${fmtShare(t.share_pct)}</option>`).join("")}</select></label>
      <label>Length<select name="length">${Object.entries(T.lengths).map(([k, n]) => `<option value="${esc(k)}">${esc(k)} (${n} ${n === 1 ? "day" : "days"})</option>`).join("")}</select></label>
      <label>Currency<select name="currency">${T.currencies.map((c) => `<option ${c !== "USD" ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
      <label>Note (for you)<input type="text" name="note" maxlength="200" placeholder="e.g. transfer ref 1234"></label>
      <div id="grant-preview" class="hint" aria-live="polite">…</div>
      <div id="grant-limits"></div>
      <label id="grant-stale" class="hidden"><input type="checkbox" name="confirm_stale_rate"> The rate is stale; grant at it anyway</label>
      <button class="btn primary" type="submit" id="grant-go">Grant</button>
    </form><div class="error" id="grant-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  const f = $("#f-grant", d);
  if (pre) {
    ["tier", "length", "currency"].forEach((k) => { if ([...f[k].options].some((o) => o.value === pre[k])) f[k].value = pre[k]; });
    f.note.value = `Order #${pre.order_id}`;
  }
  let preview = null;
  const refresh = async () => {
    $("#grant-err", d).textContent = "";
    try {
      preview = await api("/api/admin/tickets/preview", { method: "POST", body: { user: +f.user.value, tier: f.tier.value, length: f.length.value, currency: f.currency.value } });
    } catch (e) { preview = null; $("#grant-preview", d).innerHTML = `<span class="muted">${esc(e.message)}</span>`; $("#grant-limits", d).innerHTML = ""; return; }
    const p = preview;
    const price = p.discount_id ? `<s>${fmtPrice(p.list_usd)}</s> <b>${fmtPrice(p.usd)}</b> (discount)` : `<b>${fmtPrice(p.usd)}</b>`;
    const when = p.queued ? `queued: starts ${fmtDate(p.starts_at)}, after the user's current ticket` : `starts now`;
    const fit = p.available ? `<span class="good">The period is available.</span>` : `<span class="critical">${esc(p.reason)}</span>`;
    const soldOut = p.sold_out_now && p.available ? `<br><span class="muted">${tipT("The home page shows this tier as sold out right now", "sold_out_vs_queued")}; this grant starts later and fits.</span>` : "";
    $("#grant-preview", d).innerHTML = `${price} → <b>${esc(money(p.amount, p.currency))}</b> at ${p.rate} ${p.rate_set_at ? `(rate of ${fmtDate(p.rate_set_at)}${p.stale_rate ? ", <b>stale</b>" : ""})` : ""}<br>
      ${p.days} day${p.days === 1 ? "" : "s"}, ${when}: ${fmtDate(p.starts_at)} → ${fmtDate(p.ends_at)}<br>${fit}${soldOut}
      ${p.credit ? `<br><span class="muted">Their sign-up credit is removed with the ticket.</span>` : ""}`;
    $("#grant-stale", d).classList.toggle("hidden", !p.stale_rate);
    $("#grant-limits", d).innerHTML = p.limit_rows.length ? `<p class="sub">Remove these hand-set limits with the grant, so they don't throttle a paying user:</p>` +
      p.limit_rows.map((r, i) => `<label class="check"><input type="checkbox" name="rm" value="${i}" checked> ${esc(limitLabel(r))} = ${esc(r.value)} ${esc(r.unit)}</label>`).join("") : "";
    $("#grant-go", d).disabled = !p.available;
  };
  ["user", "tier", "length", "currency"].forEach((k) => (f[k].onchange = refresh));
  refresh();
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!preview) return;
    const rm = [...f.querySelectorAll('input[name="rm"]:checked')].map((c) => ({ kind: preview.limit_rows[+c.value].kind, scope: preview.limit_rows[+c.value].scope }));
    try {
      // The price and rate shown: if either changed since, the grant is refused (409) and the preview refreshed.
      await api("/api/admin/tickets", { method: "POST", body: { user: +f.user.value, tier: f.tier.value, length: f.length.value, currency: f.currency.value,
        note: f.note.value, remove_limits: rm, confirm_stale_rate: f.confirm_stale_rate.checked, usd: preview.usd, rate: preview.rate,
        ...(pre ? { order_id: pre.order_id } : {}) } });
      d.close(); render();
    } catch (err) {
      if (err.status === 409) await refresh();
      $("#grant-err", d).textContent = err.message;
    }
  };
}
function bonusDialog(t, list) {
  // The user's queued tickets after this one: extra days that reach the first of them move it and the rest forward (spec section 8).
  const queued = t.user_id == null ? [] : list.filter((x) => x.user_id === t.user_id && x.id !== t.id && x.state === "queued" && x.starts_at >= t.effective_end)
    .sort((a, b) => a.starts_at - b.starts_at);
  const moves = (days) => (queued.length && queued[0].starts_at < t.effective_end + days * 86400 ? queued.length : 0);
  const d = openDialog(`<h3>Bonus on ${esc(t.user_name)}'s ${esc(t.tier)} ticket</h3>
    <p class="sub">Extra share applies between the two times (clamped to the ticket; an empty Until means its end, extra days included, which an ended ticket needs). Extra days extend the ticket at its own share and move this user's queued tickets forward as far as needed.</p>
    <form id="f-bonus" class="form-grid">
      <label>Extra share, points<input type="text" inputmode="decimal" dir="ltr" name="share_pct" min="0" step="0.1" value="0"></label>
      <label>From<input type="datetime-local" name="starts_at" value="${toLocal(Math.max(t.starts_at, Date.now() / 1000))}"></label>
      <label>Until<input type="datetime-local" name="ends_at" value="${t.effective_end > Date.now() / 1000 ? toLocal(t.effective_end) : ""}" placeholder="the ticket's end"></label>
      <label>Extra days<input type="text" inputmode="decimal" dir="ltr" name="extra_days" min="0" step="1" value="0"></label>
      <label>Note (shown to the user)<input type="text" name="note" maxlength="200" placeholder="e.g. Sorry for Tuesday's outage"></label>
      <div id="bonus-moves" class="hint" aria-live="polite"></div>
      <button class="btn primary" type="submit">Add bonus</button>
    </form><div class="error" id="bonus-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  const f = $("#f-bonus", d);
  const movesText = () => { const n = moves(num(f.extra_days.value) || 0); return n ? `These extra days move ${n} queued ticket${n === 1 ? "" : "s"} of ${t.user_name} forward.` : ""; };
  f.extra_days.oninput = () => { $("#bonus-moves", d).textContent = movesText(); };
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (movesText() && !confirmInline(`${movesText()} Add the bonus?`)) return;
    try {
      const r = await api(`/api/admin/tickets/${t.id}/bonus`, { method: "POST", body: { share_pct: num(f.share_pct.value), extra_days: num(f.extra_days.value),
        starts_at: fromLocal(f.starts_at.value), ends_at: fromLocal(f.ends_at.value), note: f.note.value } });
      d.close();
      render();
      if (r.moved) infoInline("Bonus added", `${r.moved} queued ticket${r.moved === 1 ? "" : "s"} moved forward to start after the extended ticket.`);
    } catch (err) { $("#bonus-err", d).textContent = err.message; }
  };
}

// ---------- order requests: the admin's Orders tab (design 2026-10-04, section 7) ----------

const ORDER_STATUSES = [["open", "Open orders"], ["new", "New"], ["contacted", "Contacted"], ["done", "Done"], ["declined", "Declined"], ["withdrawn", "Withdrawn"], ["all", "All orders"]];
const isOpenOrder = (o) => o.status === "new" || o.status === "contacted";

async function renderOrders(main) {
  const status = ORDER_STATUSES.some(([k]) => k === S.prefs.orderStatus) ? S.prefs.orderStatus : "open";
  const [{ orders, new: fresh }, { users }] = await Promise.all([api(`/api/admin/orders?status=${status}`), api("/api/users")]);
  if (S.tickets && S.tickets.orders_new !== fresh) { S.tickets.orders_new = fresh; renderTabs(); }
  main.innerHTML = `<section class="view"><h2>Orders</h2>
    <p class="lede">Requests from buyers. An order holds no capacity and takes no payment: contact the buyer, then grant the ticket from the order.</p>
    <div class="controls"><label for="order-status">Show</label><select id="order-status">${ORDER_STATUSES.map(([k, n]) => `<option value="${k}"${k === status ? " selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="card table-wrap"><table class="data orders"><thead><tr><th>Age</th><th>Buyer</th><th>Ticket</th><th class="r">Quoted</th><th>Message</th><th>Status</th><th>Email</th><th>Note</th><th></th></tr></thead>
      <tbody>${orders.map(orderRow).join("") || `<tr><td colspan="9" class="muted">No ${status === "all" ? "" : `${esc(status)} `}orders.</td></tr>`}</tbody></table></div>
  </section>`;
  $("#order-status").onchange = (e) => { S.prefs.orderStatus = e.target.value; savePrefs(); render(); };
  main.querySelectorAll("[data-oact]").forEach((b) => b.addEventListener("click", () => orderAction(b.dataset.oact, orders.find((o) => o.id === +b.dataset.id), users)));
}
// Every field a buyer typed (name, email, message) and the admin note go through esc().
function orderRow(o) {
  const account = o.user_id != null ? `<div class="muted">account <a class="user-link" href="#user/${o.user_id}">${esc(o.user_name || `#${o.user_id}`)}</a></div>`
    : `<div class="muted">visitor, no account yet</div>`;
  return `<tr><td class="nowrap"><span title="${esc(fmtDate(o.created_at))}">${esc(fmtAgo(o.created_at))}</span></td>
    <td><b>${esc(o.name)}</b><div><a href="mailto:${encodeURIComponent(o.email)}">${esc(o.email)}</a></div>${account}</td>
    <td class="nowrap">${esc(o.label)} · ${esc(o.length)}</td>
    <td class="r nowrap">${esc(money(o.quoted_amount, o.currency))}</td>
    <td class="order-msg">${o.message ? esc(o.message) : `<span class="muted">—</span>`}</td>
    <td>${stateBadge(o.status)}${o.ticket_id ? `<div class="muted">ticket #${esc(o.ticket_id)}</div>` : ""}</td>
    <td>${mailState(o)}</td>
    <td class="muted order-msg">${esc(o.admin_note) || "—"}</td>
    <td>${orderActions(o)}</td></tr>`;
}
function mailState(o) {
  if (o.admin_mail === "off" && o.buyer_mail === "off") return `<span class="muted">email off</span>`;
  const cls = { failed: " state-cancelled", pending: " state-queued", sent: " state-active" };
  return [["admin", o.admin_mail], ["buyer", o.buyer_mail]].map(([who, st]) => `<span class="badge mail${cls[st] || ""}">${who} email ${esc(st)}</span>`).join(" ");
}
// Exactly the status table: Contacted from new; Decline and Grant while open; Note always.
function orderActions(o) {
  const b = (act, label, cls = "") => `<button class="btn small${cls}" data-oact="${act}" data-id="${o.id}">${label}</button>`;
  const open = o.status === "new" || o.status === "contacted";
  return `<div class="row-actions">${o.status === "new" ? b("contacted", "Contacted") : ""}${open ? b("decline", "Decline", " danger") : ""}${b("note", "Note")}${open ? b("grant", "Grant ticket", " primary") : ""}</div>`;
}
async function orderAction(act, o, users) {
  if (!o) return;
  const post = (body) => api(`/api/admin/orders/${o.id}`, { method: "POST", body });
  if (act === "contacted") { try { await post({ action: "contacted" }); render(); } catch (e) { alertInline(e.message); } return; }
  if (act === "decline" || act === "note") return orderNoteDialog(o, act, post);
  if (act !== "grant") return;
  if (o.user_id == null) return linkDialog(o, users);
  grantDialog(users, { order_id: o.id, user: o.user_id, tier: o.tier, length: o.length, currency: o.currency });
}
function orderNoteDialog(o, act, post) {
  const decline = act === "decline";
  const d = openDialog(`<h3>${decline ? "Decline" : "Note on"} ${esc(o.name)}'s order</h3>
    <p class="sub">${decline ? "The note is for you; the buyer only sees that the order was declined." : "For admins only. The buyer never sees it."}</p>
    <form id="f-onote" class="form-grid"><label class="wide">Note${decline ? " (required)" : ""}<input type="text" name="note" maxlength="200"${decline ? " required" : ""} value="${esc(decline ? "" : o.admin_note)}"></label>
    <button class="btn ${decline ? "danger" : "primary"}" type="submit">${decline ? "Decline" : "Save"}</button></form>
    <div class="error" id="onote-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  $("#f-onote input", d).focus();
  $("#f-onote", d).onsubmit = async (e) => {
    e.preventDefault();
    try { await post({ action: decline ? "decline" : "note", note: e.target.note.value }); d.close(); render(); }
    catch (err) { $("#onote-err", d).textContent = err.message; }
  };
}
// A visitor order has no account: link one first. The suggested match is preselected but only the admin's click links it.
function linkDialog(o, users) {
  const live = users.filter((u) => !u.revoked);
  const sug = o.suggested_user;
  const d = openDialog(`<h3>Link ${esc(o.name)}'s order to an account</h3>
    <p class="sub">The ticket goes to an account. ${sug ? `<b>${esc(sug.name)}</b> matches ${esc(o.email)}; check it is the same person.` : `No account matches ${esc(o.email)}.`}</p>
    <form id="f-link" class="form-grid"><label class="wide">Existing account<select name="user">${sug ? "" : `<option value="">Choose…</option>`}${live.map((u) => `<option value="${u.id}"${sug && sug.id === u.id ? " selected" : ""}>${esc(u.name)}${u.email && u.email !== u.name ? ` (${esc(u.email)})` : ""}</option>`).join("")}</select></label>
      <button class="btn primary" type="submit">Link and grant</button></form>
    <p class="sub">Or <button class="btn small" id="link-create">Create user ${esc(o.email)}</button> <span class="muted">Only the owner of that address can later sign in to it.</span></p>
    <div class="error" id="link-err"></div><p><button class="btn" data-close>Cancel</button></p>`);
  const f = $("#f-link", d);
  const link = async (body) => {
    $("#link-err", d).textContent = "";
    try {
      const r = await api(`/api/admin/orders/${o.id}`, { method: "POST", body: { action: "link", ...body } });
      const { users: fresh } = await api("/api/users");   // a created account is new
      grantDialog(fresh, { order_id: o.id, user: r.order.user_id, tier: o.tier, length: o.length, currency: o.currency });
    } catch (err) {
      const existing = err.status === 409 && err.data && err.data.existing_user_id;
      if (existing && [...f.user.options].some((x) => +x.value === existing)) {
        f.user.value = String(existing);
        $("#link-err", d).textContent = `${err.message} It is selected above: link it instead if it is the same person.`;
      } else $("#link-err", d).textContent = err.message;
    }
  };
  f.onsubmit = (e) => { e.preventDefault(); if (f.user.value) link({ user_id: +f.user.value }); else $("#link-err", d).textContent = "Choose an account, or create one."; };
  $("#link-create", d).onclick = () => link({ create: true });
}

async function renderPricing(main) {
  const [{ rates }, p] = await Promise.all([api("/api/admin/rates"), api("/api/admin/prices")]);
  const price = (tier, length) => p.prices.find((x) => x.tier === tier && x.length === length)?.usd ?? "";
  const now = Date.now() / 1000;
  const dstate = (x) => (x.cancelled_at ? "cancelled" : x.ends_at <= now ? "ended" : x.starts_at > now ? "upcoming" : "active");
  main.innerHTML = `<section class="view"><h2>Pricing</h2>
    <p class="lede">Prices are in USD; buyers see them converted at today's rate. Shares live in the config file, since changing one changes capacity.</p>
    <div class="grid cols-2">
      <div class="card"><h3>Exchange rates</h3><p class="sub">Local units per 1 USD. A rate older than 36 hours is marked stale and the grant form asks you to confirm it.</p>
        ${rates.map((r) => `<form class="limit-row rate-row" data-cur="${esc(r.currency)}"><span><b>${esc(r.currency)}</b> <span class="muted">rounds to ${r.round_to}</span>${r.stale ? ` <span class="badge">stale</span>` : ""}
          <div class="muted" style="font-size:12px">${r.rate == null ? "no rate yet: unusable until set" : `${r.rate} · set ${fmtDate(r.set_at)} by ${esc(r.set_by || "?")}`}</div></span>
          <span><input type="text" inputmode="decimal" dir="ltr" name="rate" step="any" min="0" placeholder="today's rate" required style="width:110px"> <button class="btn small" type="submit">Save</button></span></form>`).join("") || `<p class="muted">No currencies besides USD in the config.</p>`}
      </div>
      <div class="card"><h3>Regular prices, USD</h3><p class="sub">Each change is logged. Existing tickets keep what they were sold at.</p>
        <table class="data"><thead><tr><th>Tier</th>${Object.keys(p.lengths).map((l) => `<th class="r">${esc(l)}</th>`).join("")}</tr></thead><tbody>
        ${Object.entries(p.tiers).map(([k, t]) => `<tr><td><b>${esc(t.label)}</b> <span class="muted">${fmtShare(t.share_pct)}</span></td>${Object.keys(p.lengths).map((l) =>
          `<td class="r"><form class="price-form" data-tier="${esc(k)}" data-length="${esc(l)}"><input type="text" inputmode="decimal" dir="ltr" name="usd" step="0.01" min="0.01" value="${price(k, l)}" required style="width:80px"> <button class="btn small" type="submit">Save</button></form></td>`).join("")}</tr>`).join("")}
        </tbody></table></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Discounts</h3><p class="sub">A lower USD price for one tier and length over a period. It must be below the regular price; while active it replaces the price everywhere and the home page shows a countdown.</p>
      <form id="f-disc" class="form-grid">
        <label>Tier<select name="tier">${Object.entries(p.tiers).map(([k, t]) => `<option value="${esc(k)}">${esc(t.label)}</option>`).join("")}</select></label>
        <label>Length<select name="length">${Object.keys(p.lengths).map((l) => `<option>${esc(l)}</option>`).join("")}</select></label>
        <label>Price, USD<input type="text" inputmode="decimal" dir="ltr" name="usd" step="0.01" min="0.01" required></label>
        <label>From<input type="datetime-local" name="starts_at" value="${toLocal(now)}" required></label>
        <label>Until<input type="datetime-local" name="ends_at" value="${toLocal(now + 7 * 86400)}" required></label>
        <button class="btn primary" type="submit">Create discount</button></form>
      <div class="error" id="disc-err"></div>
      <table class="data" style="margin-top:12px"><thead><tr><th>Tier</th><th>Length</th><th class="r">USD</th><th>Period</th><th>State</th><th></th></tr></thead><tbody>
      ${p.discounts.map((x) => `<tr><td>${esc(p.tiers[x.tier]?.label || x.tier)}</td><td>${esc(x.length)}</td><td class="r">${fmtPrice(x.usd)}</td><td class="nowrap">${fmtDate(x.starts_at)} → ${fmtDate(x.ends_at)}</td>
        <td>${stateBadge(dstate(x))}</td><td>${dstate(x) === "active" || dstate(x) === "upcoming" ? `<button class="btn small danger" data-dcancel="${x.id}">Cancel</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No discounts.</td></tr>`}
      </tbody></table></div>
  </section>`;
  const post = async (path, body, errEl) => { try { await api(path, { method: "POST", body }); render(); } catch (e) { errEl ? (errEl.textContent = e.message) : alertInline(e.message); } };
  main.querySelectorAll(".rate-row").forEach((f) => (f.onsubmit = (e) => { e.preventDefault(); post("/api/admin/rates", { currency: f.dataset.cur, rate: num(f.rate.value) }); }));
  main.querySelectorAll(".price-form").forEach((f) => (f.onsubmit = (e) => { e.preventDefault(); post("/api/admin/prices", { tier: f.dataset.tier, length: f.dataset.length, usd: num(f.usd.value) }); }));
  $("#f-disc").onsubmit = (e) => { e.preventDefault(); const f = e.target; post("/api/admin/discounts", { tier: f.tier.value, length: f.length.value, usd: num(f.usd.value), starts_at: fromLocal(f.starts_at.value), ends_at: fromLocal(f.ends_at.value) }, $("#disc-err")); };
  main.querySelectorAll("[data-dcancel]").forEach((b) => (b.onclick = () => post(`/api/admin/discounts/${b.dataset.dcancel}/cancel`, {})));
}

function openDialog(html) {
  const d = $("#dialog");
  d.innerHTML = html;
  d.showModal();
  d.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => { d.close(); render(); }));
  return d;
}
function keyDialog(title, key, how = t("key.how_default")) {
  openDialog(`<h3>${title}</h3><p>${t("key.copy_now")} ${how}</p>
    <code class="key" id="new-key">${esc(key)}</code><p><button class="btn" id="copy-key">${t("key.copy")}</button> <button class="btn primary" data-close>${t("app.done")}</button></p>`);
  $("#copy-key").onclick = async () => { try { await navigator.clipboard.writeText(key); $("#copy-key").textContent = t("key.copied"); } catch { /* clipboard blocked */ } };
}
function showWho() { $("#who").textContent = `${iso(S.user.name)} · ${t(`role.${S.user.role}`)}`; }
// Without `u`: the signed-in user renames themselves; with it, an admin renames that user.
function nameDialog(u) {
  const self = !u, name = self ? S.user.name : u.name;
  const admin = self ? isAdmin() : u.role === "admin";
  const d = openDialog(`<h3>${self ? t("name.your") : t("name.rename", { name: bdi(name) })}</h3><p>${self ? t("name.shown_self") : t("name.shown_other")}${admin ? ` ${self ? t("name.admin_self") : t("name.admin_other")}` : ""}</p>
    <form id="f-name" class="form-grid"><label>${t("name.name")}<input type="text" name="name" required maxlength="64" value="${esc(name)}"></label>
    <button class="btn primary" type="submit">${t("app.save")}</button></form><div class="error" id="name-err"></div>
    <p><button class="btn" data-close>${t("app.cancel")}</button></p>`);
  $("#f-name input", d).select();
  $("#f-name", d).onsubmit = async (e) => {
    e.preventDefault();
    try {
      const body = { name: new FormData(e.target).get("name") };
      if (self) { S.user.name = (await api("/api/me/name", { method: "POST", body })).name; showWho(); }
      else await api(`/api/admin/users/${u.id}/rename`, { method: "POST", body });
      d.close(); render();
    } catch (err) { $("#name-err").textContent = err.message; }
  };
}
function addUserDialog() {
  const d = openDialog(`<h3>${t("users.add")}</h3><form id="f-add" class="form-grid">
    <label>${t("name.name")}<input type="text" name="name" required maxlength="64"></label>
    <label><span>${t("users.role")}${tipI("role")}</span><select name="role"><option value="user">${t("role.user")}</option><option value="admin">${t("role.admin")}</option></select></label>
    <button class="btn primary" type="submit">${t("users.create")}</button></form><div class="error" id="add-err"></div>
    <p><button class="btn" data-close>${t("app.cancel")}</button></p>`);
  $("#f-add", d).onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { const r = await api("/api/admin/users", { method: "POST", body: { name: f.get("name"), role: f.get("role") } }); keyDialog(t("key.for", { name: bdi(r.name) }), r.key); }
    catch (err) { $("#add-err").textContent = err.message; }
  };
}
async function userAction(act, id, u) {
  if (act === "limits") return limitsDialog(u);
  if (act === "upgrade") return upgradeDialog(u);
  if (act === "rename") return nameDialog(u);
  if (act === "rotate" && !confirmInline(t("users.confirm_rotate", { name: iso(u.name) }))) return;
  if (act === "revoke" && !confirmInline(t("users.confirm_revoke", { name: iso(u.name) }))) return;
  if (act === "delete") return deleteDialog(u);
  if (act === "ungate") {
    if (u.ticket && u.ticket.paused && u.ticket.live && !confirmInline(t("users.confirm_ungate", { name: iso(u.name) }))) return;
    try {
      await api(`/api/admin/users/${id}/ungate`, { method: "POST", body: {} });
      const { users } = await api("/api/users");
      return limitsDialog(users.find((x) => x.id === id));   // the spec: Ungate opens the Limits dialog so hand limits get set
    } catch (e) { return alertInline(e.message); }
  }
  try {
    const r = await api(`/api/admin/users/${id}/${act}`, { method: "POST", body: {} });
    if (act === "rotate") return keyDialog(t("key.new_for", { name: bdi(u.name) }), r.key);
    if (act === "routes_key") return keyDialog(t("key.opencode_for", { name: bdi(u.name) }), r.key, t("key.how_opencode"));
    render();
  } catch (e) { alertInline(e.message); }
}
function deleteDialog(u) {
  const d = openDialog(`<h3>${t("users.delete_title", { name: bdi(u.name) })}</h3>
    <p>${t("users.delete_body")}</p>
    <form id="f-del" class="form-grid"><label>${t("users.delete_type", { name: `<b>${bdi(u.name)}</b>` })}<input type="text" name="confirm" autocomplete="off" required></label>
    <button class="btn danger" type="submit">${t("users.delete")}</button></form><div class="error" id="del-err"></div><p><button class="btn" data-close>${t("app.cancel")}</button></p>`);
  $("#f-del", d).onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/api/admin/users/${u.id}/delete`, { method: "POST", body: { confirm: new FormData(e.target).get("confirm") } }); d.close(); render(); }
    catch (err) { $("#del-err", d).textContent = err.message; }
  };
}
function upgradeDialog(u) {
  const d = openDialog(`<h3>${t("users.upgrade_title", { name: bdi(u.name) })}</h3><p>${t("users.upgrade_body")}</p>
    <form id="f-up" class="form-grid"><label>${t("users.dollars_day")}<input type="text" inputmode="decimal" dir="ltr" name="daily" min="1" step="any" value="100" required></label>
    <button class="btn primary" type="submit">${t("users.upgrade")}</button></form><div class="error" id="up-err"></div><p><button class="btn" data-close>${t("app.cancel")}</button></p>`);
  $("#f-up", d).onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/api/admin/users/${u.id}/upgrade`, { method: "POST", body: { cost_daily: num(new FormData(e.target).get("daily")) } }); d.close(); render(); }
    catch (err) { $("#up-err").textContent = err.message; }
  };
}

// ---------- computers: keys authorized in the browser by claude-gateway on ----------

function machinesCard(keys, install, owner) {
  const rows = keys.map((k) => `<tr><td><b>${esc(k.label)}</b></td><td class="muted"><code>${esc(k.key_prefix)}…</code></td>
    <td class="nowrap">${fmtTime(k.created_at)}</td><td class="muted nowrap">${k.last_used_at ? fmtAgo(k.last_used_at) : "not yet"}</td>
    <td class="muted nowrap">${k.client_version ? esc(k.client_version) : "—"}</td>
    <td><button class="btn small danger" data-key-remove="${k.id}" data-label="${esc(k.label)}">Remove</button></td></tr>`).join("");
  return `<div class="card table-wrap" style="margin-top:16px"><h3>${owner ? "Computers" : "Your computers"}</h3>
    ${install ? `<p class="sub">To set up a computer, run this in its terminal. It opens this page to authorize it, then sets up <code>gclaude</code>.</p>
      ${[["macOS / Linux", install.unix], ["Windows (PowerShell)", install.windows]].filter(([, cmd]) => cmd).map(([os, cmd]) =>
        `<p class="muted install-os">${os}</p>
      <div class="copy-row"><code class="key">${esc(cmd)}</code><button class="btn small" data-copy="${esc(cmd)}">Copy</button></div>`).join("")}` : ""}
    ${keys.length ? `<table class="data"><thead><tr><th>Computer</th><th>Key</th><th>Authorized</th><th>Last used</th><th>gclaude${tipI("gclaude_version", "About the gclaude version")}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">${owner ? "No computers authorized in the browser." : "None authorized in the browser yet."}</p>`}</div>`;
}
function wireMachines(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; } catch { /* clipboard blocked */ }
  }));
  root.querySelectorAll("[data-key-remove]").forEach((b) => (b.onclick = async () => {
    if (!confirmInline(`Remove ${b.dataset.label}? Its key stops working at once; the next gclaude there asks to sign in again.`)) return;
    try { await api(`/api/keys/${b.dataset.keyRemove}/remove`, { method: "POST", body: {} }); render(); } catch (e) { alertInline(e.message); }
  }));
}

async function renderAuthorize(main) {
  const code = S.authCode;
  forgetAuthorize();
  let req;
  try { req = await api(`/api/device/${encodeURIComponent(code)}`); }
  catch (e) {
    if (e.message === "signed out") throw e;
    main.innerHTML = `<section class="view"><div class="card authorize"><h2>Nothing to authorize</h2><p>${esc(e.message)}</p><p><a href="#overview">Go to the dashboard</a></p></div></section>`;
    return;
  }
  main.innerHTML = `<section class="view"><div class="card authorize">
    <h2>Connect a computer</h2>
    <p>A computer that calls itself <b>${esc(req.label)}</b> asks to use the gateway as <b>${esc(S.user.name)}</b>, through <code>gclaude</code>.
      It asked ${fmtAgo(req.created_at)} from ${esc(req.ip || "an unknown address")}${req.ip && req.ip !== req.your_ip ? ` <b>(not this browser's address, ${esc(req.your_ip)})</b>` : ""}.</p>
    <p>Check that your terminal shows this code:</p><div class="user-code">${esc(req.user_code)}</div>
    <p class="muted">Only authorize if you just ran <code>gclaude</code> or the install command yourself: if someone sent you this link, cancel. The computer gets a key of its own, which you can remove later under Your computers.</p>
    <p><button class="btn primary" id="az-yes">Authorize</button> <button class="btn" id="az-no">Cancel</button></p></div></section>`;
  const decide = async (decision, title, text) => {
    main.querySelectorAll(".authorize button").forEach((b) => (b.disabled = true));
    try { await api(`/api/device/${encodeURIComponent(req.user_code)}/${decision}`, { method: "POST", body: {} }); }
    catch (e) { main.querySelectorAll(".authorize button").forEach((b) => (b.disabled = false)); return alertInline(e.message); }
    $(".authorize", main).innerHTML = `<h2>${title}</h2><p>${text}</p><p><a href="#overview">Go to the dashboard</a></p>`;
  };
  $("#az-yes").onclick = () => decide("approve", "Authorized", "Go back to your terminal: it finishes setting up by itself.");
  $("#az-no").onclick = () => decide("deny", "Cancelled", "The computer was not connected. You can close this page.");
}
// The code survives signing in, including a Google or GitHub round trip that loses the address's #part.
// Kept for the 10 minutes a code lives, so an abandoned one doesn't take over a later sign-in.
function rememberAuthorize(code) { try { sessionStorage.setItem("cp-authorize", JSON.stringify({ code, at: Date.now() })); } catch { /* private mode */ } }
function forgetAuthorize() { try { sessionStorage.removeItem("cp-authorize"); } catch { /* private mode */ } }
function rememberedAuthorize() {
  try { const v = JSON.parse(sessionStorage.getItem("cp-authorize")); return v && Date.now() - v.at < 600000 ? v.code : null; } catch { return null; }
}

// ---------- sign-in with Clerk (Google, GitHub, email code), when the gateway has it ----------

let clerkReady = null;
function loadScript(src, attrs = {}) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src; el.crossOrigin = "anonymous"; el.async = true;
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    el.onload = resolve; el.onerror = () => reject(new Error(`Could not load ${src}`));
    document.head.appendChild(el);
  });
}
function setupClerk() {
  clerkReady ??= (async () => {
    const cfg = await api("/api/auth-config");
    signinSteps(cfg);
    if (!cfg.clerk) return null;
    const npm = `https://${cfg.clerk.frontend_api}/npm`;
    await loadScript(`${npm}/@clerk/ui@1/dist/ui.browser.js`);
    await loadScript(`${npm}/@clerk/clerk-js@6/dist/clerk.browser.js`, { "data-clerk-publishable-key": cfg.clerk.publishable_key });
    await window.Clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
    window.Clerk.addListener(({ session }) => { if (session && !S.user && !$("#login").classList.contains("hidden")) clerkExchange(); });
    return window.Clerk;
  })().catch((e) => { console.warn(e); return null; });
  return clerkReady;
}
async function clerkExchange() {
  if (S.exchanging) return;
  S.exchanging = true;
  try {
    const ok = await login("/api/login/clerk", { token: await window.Clerk.session.getToken() });
    if (!ok) { const msg = $("#login-error").textContent; await window.Clerk.signOut(); $("#login-error").textContent = msg; mountClerk(); }   // refused: let them try another account
  } finally { S.exchanging = false; }
}
// The how-to-join steps, when a sign-up can go all the way to a connected computer. Both install commands sit in tabs;
// the one open first suits the visitor's system, and anything not detected as Windows gets the unix one.
function signinSteps(cfg) {
  $("#signup-hint").classList.toggle("hidden", !cfg.signup);
  $("#signin-steps").classList.toggle("hidden", !(cfg.clerk && cfg.install));
  const cmds = { unix: cfg.install, windows: cfg.install_windows }, tabs = $("#signin-os");
  tabs.classList.toggle("hidden", !cmds.windows);
  const pick = (os) => {
    tabs.querySelectorAll("[data-os]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.os === os)));
    $("#signin-install").textContent = cmds[os] || "";
    $("#signin-prompt").textContent = os === "windows" ? "PS>" : "$";
  };
  tabs.querySelectorAll("[data-os]").forEach((b) => (b.onclick = () => pick(b.dataset.os)));
  let win = false;
  try { win = /Win/.test(navigator.userAgentData?.platform || navigator.platform || ""); } catch { /* unknown: unix */ }
  pick(win && cmds.windows ? "windows" : "unix");
}
// Clerk's colours from the page's own tokens, read when it mounts: the theme can be the system's or one the viewer picked.
function clerkAppearance() {
  const css = getComputedStyle(document.documentElement), v = (n) => css.getPropertyValue(n).trim();
  return {
    variables: { colorPrimary: v("--ink"), colorPrimaryForeground: v("--surface"), colorForeground: v("--ink"), colorMutedForeground: v("--ink-2"),
                 colorBackground: v("--surface"), colorInput: v("--surface"), colorInputForeground: v("--ink"), colorNeutral: v("--ink"),
                 colorDanger: v("--critical-text"), colorRing: v("--s1"), borderRadius: "8px", fontFamily: "inherit", fontSize: "14px" },
    // Inside our own panel: full width, no second card around it.
    elements: { rootBox: { width: "100%" }, cardBox: { width: "100%", boxShadow: "none", border: "none", borderRadius: 0, background: "transparent" },
                card: { boxShadow: "none", border: "none", padding: 0, background: "transparent", gap: "24px" },
                footer: { background: "transparent" } },
  };
}
// Mounted afresh each time the sign-in shows, so its colours follow a theme picked since.
function mountClerk() {
  if (S.clerkMounted) window.Clerk.unmountSignIn($("#clerk-signin"));
  S.clerkMounted = true;
  window.Clerk.mountSignIn($("#clerk-signin"), {
    withSignUp: true, routing: "virtual", forceRedirectUrl: location.href, signUpForceRedirectUrl: location.href,
    appearance: clerkAppearance(),
  });
}
const ADMIN_PAGE = location.pathname === "/admin";
async function showClerk() {
  // The other forms wait until the page knows whether Clerk is on, so they don't flash before its sign-in.
  const clerk = !ADMIN_PAGE && await setupClerk();
  if (!clerk) { $("#classic").classList.remove("hidden"); return; }
  if (S.user) return;
  $("#clerk-area").classList.remove("hidden");
  if (!S.otherWays) $("#classic").classList.add("hidden");
  if (clerk.session) clerkExchange();   // signed in to Clerk already, e.g. back from Google: just trade it in
  else mountClerk();
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (S.clerkMounted && !$("#login").classList.contains("hidden")) mountClerk(); });
$("#signin-copy").onclick = async (e) => {
  const b = e.currentTarget;
  try { await navigator.clipboard.writeText($("#signin-install").textContent); } catch { return; /* clipboard blocked */ }
  b.classList.add("copied"); b.setAttribute("aria-label", "Copied"); b.title = "Copied";
  clearTimeout(b.timer);
  b.timer = setTimeout(() => { b.classList.remove("copied"); b.setAttribute("aria-label", "Copy command"); b.title = "Copy command"; }, 1600);
};
$("#other-ways").onclick = (e) => {
  e.preventDefault();
  S.otherWays = !S.otherWays;
  $("#classic").classList.toggle("hidden", !S.otherWays);
  e.target.setAttribute("aria-expanded", S.otherWays);
  e.target.textContent = S.otherWays ? "Hide other ways to sign in" : "Other ways to sign in";
};

// Native confirm/alert block the page; use the dialog instead for anything but the irreversible revoke.
function confirmInline(msg) { return window.confirm(msg); }
function alertInline(msg) { openDialog(`<h3>${t("app.could_not")}</h3><p>${esc(msg)}</p><button class="btn" data-close>${t("app.ok")}</button>`); }
function infoInline(title, msg) { openDialog(`<h3>${esc(title)}</h3><p>${esc(msg)}</p><button class="btn" data-close>${t("app.ok")}</button>`); }

// A <select> whose options each carry a tip. A native option popup can't show tips, so this draws its own
// listbox; the <select> stays in the form, hidden, and remains the source of truth (changes fire "change" on it).
function tipSelect(sel, tipFor) {
  const btn = document.createElement("button"), list = document.createElement("ul");
  btn.type = "button"; btn.className = "tsel";
  list.className = "tsel-list"; list.id = `tsel-${sel.name}`; list.popover = "manual"; list.setAttribute("role", "listbox");
  Object.entries({ role: "combobox", "aria-haspopup": "listbox", "aria-expanded": "false", "aria-controls": list.id })
    .forEach(([k, v]) => btn.setAttribute(k, v));
  list.innerHTML = [...sel.options].map((o, i) =>
    `<li role="option" id="${list.id}-${i}" data-tip-side="right"${tipAttr(tipFor(o.value))}>${esc(o.text)}</li>`).join("");
  sel.hidden = true;
  sel.before(btn);
  // Outside the <label>: a click inside a label would re-click the button it labels.
  (sel.closest("dialog") || document.body).append(list);
  const items = [...list.children];
  let active = -1, pointer = "mouse";
  const isOpen = () => list.matches(":popover-open");
  const label = () => {
    btn.textContent = sel.selectedOptions[0]?.text || "";
    items.forEach((li, i) => li.setAttribute("aria-selected", String(i === sel.selectedIndex)));
  };
  const setActive = (i, tip = true) => {
    active = i;
    items.forEach((li, j) => li.classList.toggle("active", j === i));
    if (i < 0) { btn.removeAttribute("aria-activedescendant"); return; }
    btn.setAttribute("aria-activedescendant", items[i].id);
    items[i].scrollIntoView({ block: "nearest" });
    if (tip) showTip(items[i]);
  };
  const outside = (e) => {
    if (!list.isConnected) document.removeEventListener("pointerdown", outside, true);
    else if (!btn.contains(e.target) && !list.contains(e.target)) close(false);
  };
  const open = () => {
    if (isOpen()) return;
    list.showPopover();
    btn.setAttribute("aria-expanded", "true");
    const r = btn.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = window.innerHeight, gap = 4, gutter = 8;
    const below = vh - r.bottom - gap - gutter, above = r.top - gap - gutter;
    const down = below >= Math.min(list.scrollHeight, 240) || below >= above;
    Object.assign(list.style, { minWidth: `${r.width}px`, maxHeight: `${down ? below : above}px`,
      top: down ? `${r.bottom + gap}px` : "auto", bottom: down ? "auto" : `${vh - r.top + gap}px` });
    list.style.left = `${Math.max(gutter, Math.min(r.left, vw - gutter - list.offsetWidth))}px`;
    setActive(sel.selectedIndex, pointer !== "touch");
    document.addEventListener("pointerdown", outside, true);
  };
  function close(focus) {
    if (!isOpen()) return;
    if (items.includes(TIP.for)) hideTip();
    list.hidePopover();
    btn.setAttribute("aria-expanded", "false");
    setActive(-1);
    document.removeEventListener("pointerdown", outside, true);
    if (focus) btn.focus();
  }
  const pick = (i) => {
    if (i >= 0 && i !== sel.selectedIndex) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change")); }
    label(); close(true);
  };
  btn.onpointerdown = (e) => { pointer = e.pointerType; };
  btn.onclick = () => (isOpen() ? close(false) : open());
  btn.onkeydown = (e) => {
    const last = items.length - 1, step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (step || e.key === "Home" || e.key === "End") {
      e.preventDefault();
      pointer = "keyboard";
      if (!isOpen()) open();
      else setActive(step ? Math.min(last, Math.max(0, active + step)) : e.key === "Home" ? 0 : last);
    } else if ((e.key === "Enter" || e.key === " ") && isOpen()) { e.preventDefault(); pick(active); }
    else if (e.key === "Escape" && isOpen()) { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "Tab") close(false);
  };
  list.addEventListener("pointerdown", (e) => e.preventDefault());   // keep focus on the button
  list.addEventListener("pointermove", (e) => {
    const i = items.indexOf(e.target.closest("[role=option]"));
    if (i >= 0 && i !== active) setActive(i, e.pointerType !== "touch");
  });
  list.addEventListener("click", (e) => pick(items.indexOf(e.target.closest("[role=option]"))));
  window.addEventListener("resize", () => close(false), { once: true });
  sel.addEventListener("change", label);
  label();
}

function limitsDialog(u) {
  const kinds = S.kinds || {};
  const d = openDialog(`<h3>${t("lim.title", { name: bdi(u.name) })}</h3>
    <div id="lim-list">${u.limits.length ? u.limits.map((l) => `<div class="limit-row"><span>${tipT(esc(limitLabel(l)), `kind:${l.kind}`)} = <b><bdi>${esc(l.value)}</bdi></b> <span class="muted">${esc(t(`unitname.${l.unit}`))}</span></span>
      <button class="btn small danger" data-del="${esc(l.kind)}" data-scope="${esc(l.scope)}">${t("lim.remove")}</button></div>`).join("") : `<p class="muted">${t("lim.none_yet")}</p>`}</div>
    <h3 style="margin-top:16px">${t("lim.set")}</h3>
    <form id="f-lim" class="form-grid">
      <label><span>${t("lim.kind")}${tipI("kind", t("lim.about_kind"))}</span><select name="kind">${Object.keys(kinds).map((k) => `<option value="${esc(k)}">${esc(t(`kname.${k}`))}</option>`).join("")}</select></label>
      <label><span>${t("lim.value")}${tipI("value")}</span><input type="text" name="value" dir="ltr" required placeholder="${esc(t("lim.eg", { v: "500000" }))}"></label>
      <label><span>${t("lim.unit")}${tipI("unit", t("lim.about_unit"))}</span><select name="unit"></select></label>
      <label><span>${t("lim.models_glob")}${tipI("scope")}</span><input type="text" name="scope" dir="ltr" value="*"></label>
      <button class="btn primary" type="submit">${t("app.save")}</button>
    </form>
    <div class="hint" id="lim-hint" aria-live="polite"></div>
    <div class="error" id="lim-err"></div><p><button class="btn" data-close>${t("app.close")}</button></p>`);
  const f = $("#f-lim", d);
  tipSelect(f.kind, (k) => `<span class="th">${esc(t(`kname.${k}`))}</span><p>${KIND_SHORT[k] || KIND_TIPS[k] || ""}</p>`);
  // The Kind and Unit dots and the hint line describe whatever is selected; each Kind item also has its own short tip.
  const syncHint = () => {
    const k = f.kind.value, unit = f.unit.value;
    d.querySelector('[data-tip="kind"]').dataset.tipHtml = TIPS[`kind:${k}`] || `<b>${esc(k)}</b>`;
    d.querySelector('[data-tip="unit"]').dataset.tipHtml = `<span class="th">${esc(t(`unitname.${unit}`))}</span><p>${UNIT_TIPS[unit] || ""}</p>`;
    f.value.placeholder = LIMIT_PLACEHOLDER[unit] || "";
    $("#lim-hint", d).innerHTML = fillTip(`<b>${esc(t(`kname.${k}`))}</b>: ${KIND_TIPS[k] || ""} ${kindNote(k)}`
      + (UNIT_TIPS[unit] && unit !== "list" ? `<br><b>${esc(t(`unitname.${unit}`))}</b>: ${UNIT_TIPS[unit]}${f.unit.disabled ? t("lim.only_unit") : ""}` : ""));
  };
  const syncUnits = () => {
    const k = f.kind.value;
    f.unit.innerHTML = (kinds[k] || []).map((x) => `<option value="${esc(x)}">${esc(t(`unitname.${x}`))}</option>`).join("");
    f.unit.disabled = f.unit.options.length < 2;   // requests_* only take count, share_* only pct: nothing to pick
    f.scope.disabled = k === "allowed_models";
    syncHint();
  };
  f.kind.onchange = syncUnits; f.unit.onchange = syncHint; syncUnits();
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/admin/limits", { method: "POST", body: { user: u.id, kind: f.kind.value, value: asciiDigits(f.value.value), unit: f.unit.value, scope: f.scope.value || "*" } });
      d.close(); render();
    } catch (err) { $("#lim-err").textContent = err.message; }
  };
  d.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    try { await api("/api/admin/limits/delete", { method: "POST", body: { user: u.id, kind: b.dataset.del, scope: b.dataset.scope } }); d.close(); render(); }
    catch (err) { $("#lim-err").textContent = err.message; }
  }));
}

async function renderUser(main) {
  const id = S.userId, p = S.prefs;
  const period = ["24h", "7d", "30d"].includes(p.userPeriod) ? p.userPeriod : "7d";
  const range = { "24h": "1d", "7d": "7d", "30d": "30d" }[period], gran = range === "1d" ? "hour" : "day";
  const q = `user_id=${id}&tz_offset=${tzOffset()}`;
  const [{ users }, lim] = await Promise.all([api("/api/users"), api("/api/limits")]);
  S.kinds = lim.kinds;
  const u = users.find((x) => x.id === id);
  if (!u) {
    main.innerHTML = `<section class="view"><p><a href="#users">${t("user.back")}</a></p><p class="muted">${t("user.gone")}</p></section>`;
    return;
  }
  const [ov, series, models, heat, sess, errs, reqs, keys] = await Promise.all([
    api(`/api/overview?user_id=${id}`), api(`/api/series?range=${range}&granularity=${gran}&split=model&${q}`),
    api(`/api/models?range=${range}&${q}`), api(`/api/heatmap?range=${range}&${q}`), api(`/api/sessions?range=${range}&${q}`),
    api(`/api/errors?range=${range}&${q}`), api(`/api/requests?user_id=${id}&limit=100`), api(`/api/keys?user_id=${id}`)]);
  const tot = ov.totals[period];
  const state = u.revoked ? `<span class="badge">${t("users.revoked")}</span>` : !u.enabled ? `<span class="badge">${t("users.disabled")}</span>` : "";
  const share = (b) => `<div class="card tile"><div class="label">${t("user.est_share", { bucket: t(`quota.bucket_${b}`) })}${tipI("share")}</div>
    <div class="value">${u.share[b] == null ? "—" : nfFix(1).format(u.share[b])}</div><div class="foot">${u.share[b] == null ? t("user.no_report") : t("user.points_of", { bucket: t(`quota.bucket_${b}`) })}</div></div>`;
  const span = t(`user.span_${period}`);
  const none = `<p class="muted">${t("models.none")}</p>`;
  main.innerHTML = `
    <section class="view">
      <p class="crumb"><a href="#users">${t("user.back")}</a></p>
      <h2><span class="dot" style="background:${colorFor("user", u.name)};margin-inline-end:8px"></span>${bdi(u.name)} ${u.role === "admin" ? `<span class="badge">${t("users.admin")}</span>` : ""} ${state}</h2>
      <p class="lede">${t("user.lede", { key: `<code>${esc(u.prefix)}…</code>`, added: fmtTime(u.created_at), last: fmtAgo(u.last_seen) })}</p>
      <div class="controls">${seg("userPeriod", [["24h", t("period.24h")], ["7d", t("period.7d")], ["30d", t("period.30d")]], period)}<span class="spacer"></span>${userActions(u)}</div>
      <div class="tiles">
        <div class="card tile"><div class="label">${t("metric.requests")}${tipI("requests")}</div><div class="value">${fmtNum(tot.requests)}</div><div class="foot">${tot.requests >= 1000 ? t("ov.n_forwarded", { n: esc(nfFull.format(tot.requests)) }) : t("ov.forwarded")}</div></div>
        <div class="card tile"><div class="label">${t("metric.weighted")}${tipI("weighted")}</div><div class="value">${fmtNum(tot.weighted)}</div><div class="foot">${t("ov.in_ref", { ref: esc(refModel()) })}</div></div>
        <div class="card tile"><div class="label">${t("metric.raw")}${tipI("raw")}</div><div class="value">${fmtNum(tot.raw)}</div><div class="foot">${t("ov.cache_reads", { n: fmtNum(tot.cache_read) })}</div></div>
        <div class="card tile"><div class="label">${t("ov.cost")}${tipI("cost")}</div><div class="value">${fmtUsd(tot.cost_usd)}</div><div class="foot">${t("ov.not_billed")}</div></div>
        ${share("5h")}${share("7d")}
      </div>
      <div class="grid cols-2">
        <div class="card"><h3>${t("users.limits")}${tipI("limits_col")}</h3><p class="sub">${t("ov.limits_sub", { served: tipT(t("ov.served"), "served") })}</p>${limitsBlock(u.limits)}</div>
        <div class="card table-wrap"><h3>${t("sess.models")}</h3><p class="sub">${t("user.span_largest", { span })}</p>${models.models.length ? `<table class="data"><thead><tr><th>${t("errs.model")}</th><th class="r">${t("metric.requests")}</th><th class="r">${t("models.m_weighted")}</th><th class="r">${t("models.m_cost")}</th><th class="r">${t("user.cache_hits")}${tipI("cache_ratio")}</th></tr></thead><tbody>
          ${models.models.map((m) => `<tr><td>${bdi(m.model || seriesLabel(NO_MODEL))}</td><td class="r">${fmtNum(m.requests)}</td><td class="r">${fmtNum(m.weighted)}</td><td class="r">${fmtUsd(m.cost_usd)}</td><td class="r">${m.cache_hit_ratio == null ? "—" : fmtPct(m.cache_hit_ratio * 100)}</td></tr>`).join("")}
          </tbody></table>` : none}</div>
      </div>
      <div class="card" style="margin-top:16px"><h3>${t(`user.weighted_per_${gran}`)}${tipI("weighted")}</h3><p class="sub">${t("user.span_dot", { span })}</p><div class="chart" id="u-usage"></div></div>
      <div class="grid cols-2" style="margin-top:16px">
        <div class="card"><h3>${t("tab.activity")}</h3><p class="sub">${t("user.activity_sub", { span })}</p><div class="heat-wrap"><div class="chart" id="u-heat"></div></div></div>
        <div class="card table-wrap"><h3>${t("user.recent_errors")}</h3><p class="sub">${t("user.span_dot", { span })}</p>${errs.recent.length ? `<table class="data"><thead><tr><th>${t("errs.when")}</th><th>${t("errs.kind")}</th><th>${t("errs.model")}</th><th class="r">${t("errs.status")}</th></tr></thead><tbody>
          ${errs.recent.slice(0, 10).map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td><td>${errorKind(r.k, r.rejected_by)}</td><td class="muted">${bdi(r.model ?? "—")}</td><td class="r">${esc(r.status ?? "")}</td></tr>`).join("")}
          </tbody></table>` : `<p class="muted">${t("errs.none")}</p>`}</div>
      </div>
      <div class="card table-wrap" style="margin-top:16px"><h3>${t("tab.sessions")}</h3><p class="sub">${t("user.span_recent", { span })}</p>${sess.sessions.length ? sessionsTable(sess.sessions, false) : `<p class="muted">${t("user.no_sessions")}</p>`}</div>
      <div class="card table-wrap" style="margin-top:16px"><h3>${t("user.recent_requests")}${tipI("recent_requests")}</h3><p class="sub">${plural("user.last_n", reqs.requests.length, { n: nfFull.format(reqs.requests.length) })}</p>${reqs.requests.length ? `<table class="data"><thead><tr><th>${t("errs.when")}</th><th>${t("errs.model")}</th><th>${t("sess.session")}</th><th class="r">${t("user.input")}</th><th class="r">${t("user.output")}</th><th class="r">${t("user.cache_read")}</th><th class="r">${t("user.cache_write")}</th><th class="r">${t("models.m_weighted")}</th><th class="r">${t("models.m_cost")}</th><th class="r">${t("user.took")}</th><th>${t("user.result")}</th></tr></thead><tbody>
        ${reqs.requests.map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td><td>${bdi(r.model ?? "—")}</td><td>${sessionCell(r.title, r.session_id)}</td>
          <td class="r">${fmtNum(r.input)}</td><td class="r">${fmtNum(r.output)}</td><td class="r">${fmtNum(r.cache_read)}</td><td class="r">${fmtNum(r.cache_write)}</td>
          <td class="r">${fmtNum(r.weighted)}</td><td class="r">${fmtUsd(r.cost_usd)}</td><td class="r">${fmtDur(r.duration_s)}</td>
          <td>${r.kind ? `${errorKind(r.kind, r.rejected_by)}${r.status ? ` <span class="muted">${esc(r.status)}</span>` : ""}` : `<span class="muted">${esc(r.status ?? "—")}</span>`}</td></tr>`).join("")}
        </tbody></table>` : `<p class="muted">${t("user.no_requests")}</p>`}</div>
      ${machinesCard(keys.keys, null, u.name)}
    </section>`;
  wireSegs(main, render);
  wireUserActions(main, users);
  wireMachines(main);
  if (series.points.length) stackedTime($("#u-usage"), series.points, "weighted", "model", gran);
  else $("#u-usage").outerHTML = none;
  if (heat.cells.length) heatmap($("#u-heat"), heat.cells);
  else $("#u-heat").outerHTML = none;
}

async function renderQuota(main) {
  const p = S.prefs;
  const d = await api(`/api/quota/timeline?bucket=${encodeURIComponent(p.bucket)}&range=${p.range === "1d" ? "1d" : p.range}`);
  const bname = (b) => (b === "5h" || b === "7d" ? t(`quota.bucket_${b}`) : b);
  main.innerHTML = `<section class="view"><h2>${t("tab.quota")}</h2>
    <p class="lede">${t("quota.lede", { estimated: tipT(t("quota.estimated"), "share") })}</p>
    <div class="controls">${seg("bucket", d.buckets.map((b) => [b, bname(b), `bucket:${b}`]), p.bucket)} ${seg("range", [["1d", t("range.1d")], ["7d", t("range.7d")], ["30d", t("range.30d")]], p.range)}</div>
    <div class="grid cols-2">
      <div class="card"><h3>${t("quota.reported")}${tipI("reported")}</h3><p class="sub">${t("quota.reported_sub")}</p><div class="chart" id="q-line"></div></div>
      <div class="card"><h3>${t("quota.share_title")}${tipI("share")}</h3><p class="sub">${t("quota.share_sub", { unattributed: tipT(t("ov.not_attributed"), "unattributed") })}</p><div class="chart" id="q-share"></div><div id="q-table"></div></div>
    </div></section>`;
  wireSegs(main, render);
  const o = baseOption();
  const pctAxis = (v) => fmtPct(v);
  if (!d.snapshots.length) { $("#q-line").outerHTML = `<p class="muted">${t("quota.no_reports")}</p>`; }
  else {
    chart($("#q-line")).setOption({
      ...o, legend: { show: false },
      tooltip: { ...o.tooltip, valueFormatter: (v) => fmtPct(v, 1) },
      xAxis: { ...o.xAxis, type: "time", axisLabel: { ...o.xAxis.axisLabel, formatter: timeAxis } }, yAxis: { ...o.yAxis, type: "value", max: 100, axisLabel: { ...o.yAxis.axisLabel, formatter: pctAxis } },
      dataZoom: [{ type: "inside" }],
      series: [{ name: t("quota.utilization", { bucket: bname(p.bucket) }), type: "line", step: "end", showSymbol: false, lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1") },
                 data: d.snapshots.map((s) => [s.observed_at * 1000, s.utilization_pct]) }],
    });
  }
  const h = d.window_history;
  if (!h.length) { $("#q-share").outerHTML = `<p class="muted">${t("quota.no_window")}</p>`; return; }
  const totals = {};
  h.forEach((pt) => Object.entries(pt.shares).forEach(([k, v]) => (totals[k] = Math.max(totals[k] || 0, v))));
  const users = topKeys(totals);
  let peak = 0;
  const rows = h.map((pt) => {
    peak = Math.max(peak, pt.utilization_pct);
    const named = users.map((k) => pt.shares[k] || 0);
    const other = Object.entries(pt.shares).filter(([k]) => !users.includes(k)).reduce((a, [, v]) => a + v, 0);
    const un = Math.max(0, peak - named.reduce((a, b) => a + b, 0) - other);   // a report that dips within the window is noise
    return { t: pt.t * 1000, named, other, un };
  });
  const hasOther = rows.some((r) => r.other > 0);
  const series = users.map((k, i) => ({ name: k, data: rows.map((r) => [r.t, r.named[i]]), color: colorFor("user", k) }));
  if (hasOther) series.push({ name: seriesLabel(OTHER), data: rows.map((r) => [r.t, r.other]), color: colorFor("user", OTHER) });
  series.push({ name: t("ov.not_attributed"), data: rows.map((r) => [r.t, r.un]), color: colorFor("user", "unattributed") });
  chart($("#q-share")).setOption({
    ...o, tooltip: { ...o.tooltip, valueFormatter: (v) => t("lim.pts", { v: nfFix(1).format(v) }) },
    xAxis: { ...o.xAxis, type: "time", axisLabel: { ...o.xAxis.axisLabel, formatter: timeAxis } }, yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: pctAxis } },
    series: series.map((s) => ({ name: s.name, type: "line", stack: "share", step: "end", showSymbol: false, areaStyle: { color: s.color, opacity: 0.85 },
                                 lineStyle: { width: 0 }, itemStyle: { color: s.color }, data: s.data })),
  });
  const last = h[h.length - 1];
  $("#q-table").innerHTML = tableView(t("quota.table_title"), [t("app.user"), t("quota.table_share")],
    Object.entries(last.shares).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, nf2.format(v)])
      .concat([[t("ov.not_attributed"), nf2.format(Math.max(0, last.utilization_pct - Object.values(last.shares).reduce((a, b) => a + b, 0)))]]));
}

async function renderModels(main) {
  const p = S.prefs;
  const d = await api(`/api/models?range=${p.range === "1d" ? "7d" : p.range}&tz_offset=${tzOffset()}`);
  const mm = p.modelMetric;
  const provLabel = (k) => (!isAdmin() ? `${t("models.api_equiv", { p: k === "anthropic" ? "Claude" : bdi(k) })}${tipI("provider_sub")}`
    : k === "anthropic" ? `${t("models.claude_sub")}${tipI("provider_sub")}` : `${t("models.own_key", { p: bdi(k) })}${tipI("provider_own")}`);
  main.innerHTML = `<section class="view"><h2>${t("tab.models")}</h2>
    <p class="lede">${isAdmin() ? t("models.lede_admin") : t("models.lede_user")}</p>
    <div class="controls">${seg("range", [["7d", t("range.7d")], ["30d", t("range.30d")], ["90d", t("range.90d")]], p.range === "1d" ? "7d" : p.range)} ${seg("modelMetric", [["cost_usd", t("models.m_cost"), "metric:cost_usd"], ["weighted", t("models.m_weighted"), "metric:weighted"], ["raw", t("metric.raw"), "metric:raw"], ["requests", t("metric.requests"), "metric:requests"]], mm)}</div>
    <div class="tiles">${Object.entries(d.providers).map(([k, v]) => `<div class="card tile"><div class="label">${provLabel(k)}</div><div class="value">${fmtUsd(v.cost_usd)}</div><div class="foot">${t("models.tile_foot", { r: fmtNum(v.requests), n: fmtNum(v.raw) })}</div></div>`).join("")}</div>
    <div class="grid cols-2">
      <div class="card"><h3>${t("models.by_model", { metric: esc(METRICS[mm]) })}</h3><p class="sub">${t("models.largest")}</p><div class="chart" id="m-bars"></div></div>
      <div class="card"><h3>${t("models.cache_title")}${tipI("cache_ratio")}</h3><p class="sub">${t("models.daily")} <code>${esc(d.formula)}</code></p><div class="chart" id="m-cache"></div></div>
    </div></section>`;
  wireSegs(main, render);
  const o = baseOption();
  const models = d.models.slice(0, 12).reverse();
  if (models.length) {
    chart($("#m-bars")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, trigger: "item", valueFormatter: (v) => fmtMetric(mm, v) },
      grid: { ...o.grid, top: 8 },
      xAxis: { ...o.yAxis, type: "value", splitNumber: narrow() ? 2 : 5, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => fmtMetric(mm, v) } },
      yAxis: { ...o.xAxis, type: "category", data: models.map((m) => m.model || seriesLabel(NO_MODEL)) },
      series: [{ type: "bar", barMaxWidth: 18, itemStyle: { color: css("--s1"), borderRadius: [0, 4, 4, 0] }, data: models.map((m) => m[mm]) }],
    });
  } else $("#m-bars").outerHTML = `<p class="muted">${t("models.none")}</p>`;
  const pts = d.cache_ratio.filter((p) => p.ratio != null);
  if (pts.length) {
    chart($("#m-cache")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, valueFormatter: (v) => fmtPct(v * 100, 1) },
      xAxis: { ...o.xAxis, type: "time", minInterval: 86400000, axisLabel: { ...o.xAxis.axisLabel, formatter: (v) => timeLabel("day")(v) } },
      yAxis: { ...o.yAxis, type: "value", min: 0, max: 1, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => fmtPct(Math.round(v * 100)) } },
      series: [{ name: t("models.cache_series"), type: "line", showSymbol: pts.length < 40, symbolSize: 8, lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1") },
                 data: pts.map((p) => [p.t * 1000, p.ratio]) }],
    });
  } else $("#m-cache").outerHTML = `<p class="muted">${t("models.no_tokens")}</p>`;
}

async function renderActivity(main) {
  const p = S.prefs;
  const d = await api(`/api/heatmap?range=${p.range === "1d" ? "7d" : p.range}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>${t("tab.activity")}</h2><p class="lede">${t("act.lede")}</p>
    <div class="controls">${seg("range", [["7d", t("range.7d")], ["30d", t("range.30d")], ["90d", t("range.90d")]], p.range === "1d" ? "7d" : p.range)}</div>
    <div class="card heat-wrap"><div class="chart" id="heat"></div></div></section>`;
  wireSegs(main, render);
  heatmap($("#heat"), d.cells);
}
function heatmap(el, cells) {
  // Sunday first, as the API counts them; 2023-01-01 was a Sunday.
  const wd = new Intl.DateTimeFormat(LOC, { weekday: "short", timeZone: "UTC" });
  const days = [...Array(7).keys()].map((i) => wd.format(Date.UTC(2023, 0, 1 + i)));
  const p2 = new Intl.NumberFormat(LOC, { minimumIntegerDigits: 2 });
  const max = Math.max(1, ...cells.map((c) => c[2]));
  const o = baseOption();
  chart(el).setOption({
    ...o, legend: { show: false }, grid: { ...o.grid, top: 8, bottom: 48 },
    tooltip: { ...o.tooltip, trigger: "item", formatter: (x) => `${days[x.value[1]]} ${p2.format(x.value[0])}:${p2.format(0)} — ${plural("act.n_requests", x.value[2], { n: `<b>${nfFull.format(x.value[2])}</b>` })}` },
    xAxis: { ...o.xAxis, type: "category", data: [...Array(24).keys()].map((h) => p2.format(h)), splitArea: { show: false } },
    yAxis: { ...o.yAxis, type: "category", data: days, inverse: true, splitLine: { show: false } },
    visualMap: { min: 1, max: Math.max(2, max), calculable: false, orient: "horizontal", left: "center", bottom: 0, itemWidth: 12, itemHeight: 140, text: [plural("act.n_requests", max, { n: nfFull.format(max) }), nf0.format(1)],
                 textStyle: { color: css("--muted"), fontSize: 11 },
                 inRange: { color: [css("--seq-1"), css("--seq-2"), css("--seq-3"), css("--seq-4"), css("--seq-5")] } },
    series: [{ type: "heatmap", data: cells, itemStyle: { borderColor: css("--surface"), borderWidth: 2, borderRadius: 3 } }],
  });
}

async function renderSessions(main) {
  const d = await api(`/api/sessions?range=${S.prefs.range === "1d" ? "1d" : "7d"}`);
  main.innerHTML = `<section class="view"><h2>${t("tab.sessions")}</h2><p class="lede">${t(S.prefs.range === "1d" ? "sess.lede_1d" : "sess.lede_7d")}</p>
    <div class="card table-wrap">${d.sessions.length ? sessionsTable(d.sessions, isAdmin()) : `<p class="muted">${t("sess.none")}</p>`}</div></section>`;
}
const sessionCell = (title, id) => (title ? `<div class="sess-title" title="${esc(title)}">${bdi(title)}</div><div class="sess-id">${esc(id.slice(0, 8))}</div>`
  : id ? `<span class="muted">${esc(id.slice(0, 8))}</span>` : `<span class="muted">—</span>`);
function sessionsTable(sessions, showUser) {
  return `<table class="data"><thead><tr><th>${t("sess.session")}${tipI("session")}</th>${showUser ? `<th>${t("app.user")}${tipI("sess_user")}</th>` : ""}<th>${t("sess.started")}${tipI("sess_started")}</th><th class="r">${t("sess.duration")}${tipI("sess_duration")}</th><th class="r">${t("metric.requests")}${tipI("sess_requests")}</th><th class="r">${t("models.m_weighted")}${tipI("sess_weighted")}</th><th class="r">${t("models.m_cost")}${tipI("sess_cost")}</th><th>${t("sess.models")}${tipI("sess_models")}</th></tr></thead><tbody>
      ${sessions.map((s) => `<tr><td>${sessionCell(s.title, s.session_id)}</td>${showUser ? `<td>${bdi(s.user)}</td>` : ""}<td class="nowrap">${fmtTime(s.first)}</td><td class="r">${fmtDur(s.duration_s)}</td>
        <td class="r">${nfFull.format(s.requests)}</td><td class="r">${fmtNum(s.weighted)}</td><td class="r">${fmtUsd(s.cost_usd)}</td><td class="muted">${bdi(s.models.join(", "))}</td></tr>`).join("")}
    </tbody></table>`;
}

async function renderErrors(main) {
  const p = S.prefs;
  const range = p.range === "90d" ? "30d" : p.range;
  const d = await api(`/api/errors?range=${range}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>${t("tab.errors")}</h2><p class="lede">${!isAdmin() ? t("errs.lede_user") : t("errs.lede_admin", { quota: tipT(t("errs.quota"), "err:upstream_quota"), throttle: tipT(t("errs.throttle"), "err:upstream_throttle"), one: tipT(t("errs.one"), "err:upstream_request_scoped") })}</p>
    <div class="controls">${seg("range", [["1d", t("range.1d")], ["7d", t("range.7d")], ["30d", t("range.30d")]], range)}</div>
    <div class="card"><div class="chart" id="err-chart"></div></div>
    <div class="card table-wrap" style="margin-top:16px"><h3>${t("errs.recent")}</h3>${d.recent.length ? `<table class="data"><thead><tr><th>${t("errs.when")}</th>${isAdmin() ? `<th>${t("app.user")}</th>` : ""}<th>${t("errs.kind")}</th><th>${t("errs.model")}</th><th class="r">${t("errs.status")}</th></tr></thead><tbody>
      ${d.recent.map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td>${isAdmin() ? `<td>${bdi(r.user ?? "—")}</td>` : ""}<td>${errorKind(r.k, r.rejected_by)}</td><td class="muted">${bdi(r.model ?? "—")}</td><td class="r">${esc(r.status ?? "")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">${t("errs.none")}</p>`}</div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#err-chart").outerHTML = `<p class="muted">${t("errs.nothing")}</p>`; return; }
  const pts = d.points.map((x) => ({ t: x.t, key: ERROR_LABELS[x.k] || x.k, n: x.n }));
  stackedTime($("#err-chart"), pts, "n", "error", d.bucket_s === 3600 ? "hour" : "day");
}

function auditDetail(json) {
  if (!json) return "";
  try { return Object.entries(JSON.parse(json)).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · "); } catch { return json; }
}
async function renderAudit(main) {
  const d = await api("/api/audit");
  main.innerHTML = `<section class="view"><h2>${t("tab.audit")}</h2><p class="lede">${t("audit.lede")}</p>
    <div class="card table-wrap"><table class="data"><thead><tr><th>${t("errs.when")}</th><th>${t("audit.actor")}</th><th>${t("audit.action")}</th><th>${t("audit.target")}</th><th>${t("audit.detail")}</th></tr></thead><tbody>
      ${d.entries.map((e) => `<tr><td class="nowrap">${fmtTime(e.at)}</td><td>${bdi(e.actor ?? "—")}</td><td><code>${esc(e.action)}</code></td><td>${bdi(e.target ?? "")}</td><td class="muted" dir="ltr">${esc(auditDetail(e.detail_json))}</td></tr>`).join("")}
    </tbody></table></div></section>`;
}

$("#dialog").addEventListener("close", hideTip);

// ---------- session ----------

// The tier and length a visitor picked on the home page ("Get it"), kept through sign-in in this tab; the price
// list names it once the user is in. Taken out of the address so a reload or a shared link doesn't repeat it.
const PICK_KEY = "cp-picked";
function rememberPick() {
  const q = new URLSearchParams(location.search);
  if (!q.has("tier")) return;
  try { sessionStorage.setItem(PICK_KEY, JSON.stringify({ tier: q.get("tier"), length: q.get("length") })); } catch { /* no storage: the pick is lost */ }
  q.delete("tier"); q.delete("length");
  const rest = q.toString();   // not q.size: older Safari and Chrome lack it
  history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
}
rememberPick();
function takePick() {
  if (S.pick === undefined) {
    try { S.pick = JSON.parse(sessionStorage.getItem(PICK_KEY) || "null"); sessionStorage.removeItem(PICK_KEY); } catch { S.pick = null; }
  }
  return S.pick;
}
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  disposeCharts();
  const m = /^#authorize\/([A-Za-z-]+)$/.exec(location.hash);
  if (m) rememberAuthorize(m[1]);
  const code = rememberedAuthorize();
  $("#authorize-hint").classList.toggle("hidden", !code);
  $("#signin-about").classList.toggle("hidden", !!code);
  $("#login").classList.toggle("for-authorize", !!code);
  $("#authorize-code").textContent = code || "";
  showClerk();
}
async function boot() {
  try {
    const s = await api("/api/session");
    S.user = s.user; S.csrf = s.csrf; S.install = s.install && { unix: s.install, windows: s.install_windows };
    S.tickets = s.tickets || { enabled: false };
    if (s.settings) S.settings = { ...S.settings, ...s.settings };
    Object.assign(TIPS, isAdmin() ? ADMIN_TIPS : USER_TIPS);
    $("#cred-pill").hidden = !s.credential;
    if (s.credential) credentialPill(s.credential);
  } catch { showLogin(); return; }
  const pending = rememberedAuthorize();
  if (pending && !location.hash.startsWith("#authorize/")) history.replaceState(null, "", `#authorize/${pending}`);
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  showWho();
  if (isAdmin()) takePick();   // an admin previewing the home page has no price list to name it in
  route();
  render();
}
// "#models" opens a tab, "#user/3" a user's page. Returns whether the address named a page.
function route() {
  const h = location.hash.slice(1), m = /^user\/(\d+)$/.exec(h), a = /^authorize\/([A-Za-z-]+)$/.exec(h);
  if (m) { S.tab = "user"; S.userId = +m[1]; return true; }
  if (a) { S.tab = "authorize"; S.authCode = a[1]; return true; }
  // An old /pricing link lands here as #pricing: a user's prices are on the overview; an admin's tab has the name.
  if (h === "pricing" && !isAdmin()) { S.tab = "overview"; S.toPrices = true; history.replaceState(null, "", "#overview"); return true; }
  if (VIEWS[h] && !VIEWS[h].hidden) { S.tab = h; return true; }
  return false;
}
async function login(path, body) {
  $("#login-error").textContent = "";
  try { const r = await api(path, { method: "POST", body }); S.csrf = r.csrf; await boot(); return true; }
  catch (e) { $("#login-error").textContent = e.message; return false; }
}
$("#form-admin").onsubmit = (e) => { e.preventDefault(); const f = new FormData(e.target); login("/api/login", { username: f.get("username"), password: f.get("password") }); };
$("#form-key").onsubmit = (e) => { e.preventDefault(); login("/api/login/key", { key: new FormData(e.target).get("key") }); };
// The admin's password form lives only at /admin, which nothing links to; everyone else signs in with Clerk or a key.
if (ADMIN_PAGE) {
  $("#form-admin").classList.remove("hidden");
  $("#form-key").classList.add("hidden");
  $("#login-hint").textContent = "Sign in as the admin.";
}
$("#logout").onclick = async () => {
  await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
  S.user = null;
  if (window.Clerk?.session) await window.Clerk.signOut().catch(() => {});   // or the sign-in page would trade it in again
  showLogin();
};
$("#who").onclick = () => nameDialog();
// Signed in, "/" sends a user straight back here, so the button opens their own price list on the overview; an admin,
// who has none, previews the public page (the link's own href).
$("#pricing-btn").onclick = async (e) => {
  if (isAdmin()) return;
  e.preventDefault();
  S.tab = "overview"; S.toPrices = true;
  history.replaceState(null, "", "#overview");
  render();
};
$("#theme-toggle").onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("cp-theme", document.documentElement.dataset.theme); } catch { /* ignore */ }
  render();
};
try { const t = localStorage.getItem("cp-theme"); if (t) document.documentElement.dataset.theme = t; } catch { /* ignore */ }
window.addEventListener("hashchange", () => { if (S.user && route()) render(); });
setInterval(() => { if (S.user && !document.hidden && !$("#dialog").open && ["overview", "users", "user", "orders"].includes(S.tab)) render(); }, 60000);
// The Orders tab's count of new orders follows along on every other tab too.
setInterval(async () => {
  if (!S.user || document.hidden || !isAdmin() || !S.tickets?.enabled) return;
  try { const s = await api("/api/session"); if (s.tickets && s.tickets.orders_new !== S.tickets.orders_new) { S.tickets.orders_new = s.tickets.orders_new; renderTabs(); } }
  catch { /* the next tick tries again */ }
}, 60000);

boot();
