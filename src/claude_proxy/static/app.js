"use strict";

// ---------- state & helpers ----------

const S = {
  user: null, csrf: null, tab: "overview", charts: [],
  settings: { reference_model: "claude-sonnet-5", stale_after_s: 1800 },   // replaced by /api/session
  prefs: loadPrefs(),
  colorSlots: loadSlots(),   // dimension -> {key: slot}; colour follows the entity, kept across page loads
};
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tzOffset = () => -new Date().getTimezoneOffset() * 60;

function loadPrefs() {
  const d = { range: "7d", granularity: "day", split: "user", metric: "weighted", period: "24h", userPeriod: "7d", bucket: "5h", modelMetric: "cost_usd" };
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

const nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, notation: "compact" });   // 24.9M, never "24.9m" (minutes?)
const nfFull = new Intl.NumberFormat();
function fmtNum(v) { return v == null ? "—" : nf.format(v); }
function fmtUsd(v) { return v == null ? "—" : v === 0 ? "$0" : v < 0.01 ? "<$0.01" : "$" + (v >= 100 ? v.toFixed(0) : v.toFixed(2)); }
function fmtPct(v, d = 0) { return v == null ? "—" : `${v.toFixed(d)}%`; }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  // Two largest units, e.g. "2h 30m"; the second is dropped when it is zero.
  const [big, bigU, small, smallU] = s < 3600 ? [Math.floor(s / 60), "m", s % 60, "s"]
    : s < 86400 ? [Math.floor(s / 3600), "h", Math.floor(s % 3600 / 60), "m"]
    : [Math.floor(s / 86400), "d", Math.floor(s % 86400 / 3600), "h"];
  return small ? `${big}${bigU} ${small}${smallU}` : `${big}${bigU}`;
}
function fmtAgo(t) { return t ? `${fmtDur(Date.now() / 1000 - t)} ago` : "never"; }
function fmtTime(t) { return t ? new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
const METRICS = { weighted: "Weighted tokens", raw: "Raw tokens", cost_usd: "Est. cost (USD)", requests: "Requests" };
function fmtMetric(metric, v) { return metric === "cost_usd" ? fmtUsd(v) : fmtNum(v); }

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body !== undefined) headers["content-type"] = "application/json";
  if (opts.method && opts.method !== "GET" && S.csrf) headers["x-csrf-token"] = S.csrf;
  const r = await fetch(path, { credentials: "same-origin", ...opts, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("signed out"); }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

// ---------- tooltips ----------

// Trusted markup, one or two sentences each. Keys are referenced by data-tip; dynamic tips use data-tip-html.
const TIPS = {
  login_key: `Your <b>gateway key</b> (<code>sk-proxy-…</code>) from the admin, the same one Claude Code uses as <code>ANTHROPIC_AUTH_TOKEN</code>. It shows only your own usage.`,
  requests: `Requests the gateway forwarded to a provider. Requests it refused (bad key, limit reached) are not counted.`,
  weighted: `<span class="th">Weighted tokens</span><p>Every token priced at API list rates and expressed in <b>{ref} input tokens</b>: an Opus output token counts for many, a cache read for a tenth of an input token of the same model.</p><p class="tm">The closest match to what a request costs against the subscription quota.</p>`,
  raw: `<span class="th">Raw tokens</span><p>Input, output, cache-write and cache-read tokens, each counted as 1. Cache reads are cheap but plentiful, so they usually dominate.</p>`,
  cost: `<span class="th">API-equivalent cost</span><p>What these requests would cost at Anthropic's API list prices. Claude usage is covered by the subscription, so this is a yardstick, not a bill.</p><p class="tm">Models missing from the price table count as $0 and are listed as unpriced.</p>`,
  active_users: `Users with at least one forwarded request in the last 24 hours.`,
  burn_rate: `Weighted tokens per minute over the last 15 minutes, Claude models only. How hard the account is being pushed right now.`,
  quota: `<span class="th">Account quota</span><p>Anthropic reports how full each of the subscription's usage buckets is. At 100%, Claude requests fail with 429 until the bucket resets.</p>`,
  "bucket:5h": `<span class="th">5-hour bucket</span><p>Short-term usage allowance. Anthropic resets it about 5 hours after its window opened.</p>`,
  "bucket:7d": `<span class="th">7-day bucket</span><p>Weekly usage allowance. Anthropic resets it 7 days after its window opened.</p>`,
  share: `<span class="th">Estimated share</span><p>Anthropic reports only the account total. Each rise between two reports is split across the users whose requests finished in between, by weighted tokens.</p><p class="tm">Reports are whole percents, so small shares are rough.</p>`,
  unattributed: `<span class="th">Not attributed</span><p>Usage the gateway can't pin on a user: the account used outside the gateway (claude.ai, another login), or usage from before this window's first report.</p>`,
  stale: `No report from Anthropic in the last {stale}. Reports arrive with each Claude response; the gateway polls when it's quiet. Share limits are skipped meanwhile.`,
  exhaustion: `How fast the 5-hour bucket rose over the last 30 minutes, projected to 100%. If that lands <b>before the reset</b>, Claude requests will start failing unless usage slows.`,
  served: `Limits are checked before each request against usage already recorded, so the request that crosses the line goes through and the next one is refused.`,
  share_col: `Each user's estimated share of the account's 5-hour and 7-day buckets, in percentage points.`,
  limits_col: `Per-user caps, checked before every request. The bar turns amber at 80% and red at 100%.`,
  key_prefix: `The grey line under each name is the start of the user's gateway key, to tell keys apart. The full key is shown only once, when created or rotated.`,
  act_limits: `View or change this user's limits.`,
  act_rotate: `Issue a new key and stop the old one immediately. Usage history is kept.`,
  act_routes_key: `Issue a key for OpenCode that works only for third-party models (such as Muse), never Claude. Issuing again replaces it.`,
  act_routes_key_remove: `Delete this user's OpenCode key. Their Claude Code key keeps working.`,
  act_disable: `Block the key until re-enabled. Nothing is deleted.`,
  act_enable: `Let the key work again.`,
  act_revoke: `Stop the key for good. Usage stays in the totals; a revoked user can only be deleted.`,
  act_delete: `Remove the user and all their recorded usage. Account totals and charts change.`,
  role: `<b>admin</b>: sees all users, manages keys and limits. <b>user</b>: sees only their own usage.`,
  scope: `Count and limit only requests for matching models. <code>*</code> matches anything, so <code>claude-opus-*</code> covers every Opus version. Leave <code>*</code> for all models.`,
  kind: `What the limit counts and over which window.`,
  unit: `What the value is measured in.`,
  value: `The cap, in the chosen unit. For <code>allowed_models</code>, the list of model globs.`,
  "split:provider": `<b>anthropic</b> is the shared Claude subscription; other providers (such as Muse on Meta) use their own API key.`,
  "metric:weighted": `Tokens priced by type and model, in {ref} input tokens. Best proxy for quota cost.`,
  "metric:raw": `Every token counts 1, cache reads included.`,
  "metric:cost_usd": `Estimated cost at Anthropic API list prices. Not billed on the subscription.`,
  "metric:requests": `Forwarded requests, regardless of size.`,
  reported: `Anthropic's own figure for the whole account, read from response headers or the usage endpoint. The line steps because it only changes when a new report arrives.`,
  provider_sub: `Claude models run on the shared subscription. The figure is what they would cost on the API, not a bill.`,
  provider_own: `This provider is billed to its own API key, at roughly this cost.`,
  cache_ratio: `<span class="th">Cache hit ratio</span><p>Share of prompt tokens read from the prompt cache instead of processed fresh. Cache reads cost a tenth of normal input, so higher means cheaper against the quota.</p>`,
  recent_requests: `Every request this user sent, refused ones included. Tokens, weighted tokens and cost are counted only for requests that reached a provider.`,
  session: `<span class="th">Session</span><p>Claude Code sends a session id with each request; one row per id. Duration runs from the first to the last request.</p><p class="tm">The title is the one Claude Code generates for the session. Sessions without one show only their id.</p>`,
  sess_user: `Who ran the session.`,
  sess_started: `Time of the session's first request.`,
  sess_duration: `Time from the first request to the last.`,
  sess_requests: `Requests sent to a provider. Refused ones are not counted.`,
  sess_weighted: `Tokens adjusted by type and model, in {ref} input tokens. Closest to quota cost.`,
  sess_cost: `What it would cost at API prices. Not billed on the subscription.`,
  sess_models: `Models the session used.`,
};
const KIND_TIPS = {
  requests_minute: "Requests in the last 60 seconds.",
  tokens_minute: "Tokens in the last 60 seconds.",
  tokens_5h: "Tokens in the last 5 hours. Separate from the account's 5-hour bucket.",
  requests_daily: "Requests in the last 24 hours.",
  tokens_daily: "Tokens in the last 24 hours.",
  tokens_weekly: "Tokens in the last 7 days.",
  requests_monthly: "Requests in the last 30 days.",
  tokens_monthly: "Tokens in the last 30 days.",
  cost_daily: "Estimated API-equivalent cost in the last 24 hours, in USD.",
  cost_monthly: "Estimated API-equivalent cost in the last 30 days, in USD.",
  share_5h: "The user's <b>estimated share</b> of the account's 5-hour bucket, in percentage points: <code>20</code> stops them at about a fifth of it. Claude models only.",
  share_7d: "The user's <b>estimated share</b> of the account's 7-day bucket, in percentage points: <code>20</code> stops them at about a fifth of it. Claude models only.",
  allowed_models: "Only these models may be requested; anything else is refused. Comma-separated globs, e.g. <code>claude-sonnet-*,muse-spark</code>.",
};
const KIND_NOTE = {
  window: "The window opens with the first request and lasts its length; then the count goes back to zero and the next request opens a new one. Only forwarded requests count; token-counting calls are free.",
  share: "Skipped while there is no fresh report from Anthropic.",
};
const UNIT_TIPS = {
  count: "Number of requests.",
  weighted: "Tokens priced by type and model, in {ref} input-token equivalents. The best proxy for quota cost.",
  raw: "Every token counts 1, cache reads included, so long cached sessions add up fast.",
  usd: "US dollars at API list prices.",
  pct: "Percentage points of the account bucket, 0 to 100.",
  list: "Comma-separated model globs.",
};
// One plain sentence per kind, shown beside each item of the Kind dropdown.
const KIND_SHORT = {
  requests_minute: "How many requests they can send in any 1 minute.",
  requests_daily: "How many requests they can send in any 24 hours.",
  requests_monthly: "How many requests they can send in any 30 days.",
  tokens_minute: "How many tokens (pieces of text) they can use in any 1 minute.",
  tokens_5h: "How many tokens they can use in any 5 hours.",
  tokens_daily: "How many tokens they can use in any 24 hours.",
  tokens_weekly: "How many tokens they can use in any 7 days.",
  tokens_monthly: "How many tokens they can use in any 30 days.",
  cost_daily: "How many dollars they can spend in any 24 hours, at API prices.",
  cost_monthly: "How many dollars they can spend in any 30 days, at API prices.",
  share_5h: "What percent of the shared subscription's 5-hour quota they can use.",
  share_7d: "What percent of the shared subscription's weekly quota they can use.",
  allowed_models: "Which models they may use. Any other model is refused.",
};
const LIMIT_PLACEHOLDER = { count: "e.g. 200", weighted: "e.g. 5000000", raw: "e.g. 20000000", usd: "e.g. 50", pct: "e.g. 25", list: "claude-sonnet-*,muse-spark" };
const kindNote = (k) => (k.startsWith("share_") ? KIND_NOTE.share : k === "allowed_models" ? "" : KIND_NOTE.window);
Object.entries(KIND_TIPS).forEach(([k, v]) => {
  TIPS[`kind:${k}`] = `<span class="th">${k.replace(/_/g, " ")}</span><p>${v}</p>${kindNote(k) ? `<p class="tm">${kindNote(k)}</p>` : ""}`;
});
const ERROR_TIPS = {
  gateway_limit: "A gateway limit refused the request (named in brackets). It never reached the provider and isn't counted as usage.",
  gateway_auth: "Missing, wrong, disabled or revoked gateway key.",
  gateway_key_scope: "An OpenCode key asked for a Claude model or a path it can't use. It never reached a provider.",
  gateway_bad_request: "The request body wasn't JSON with a <code>model</code>, so the gateway refused it without forwarding.",
  upstream_quota: "Anthropic refused it because an account bucket (5-hour or weekly) is full. Clears when that bucket resets.",
  upstream_throttle: "Anthropic's short-term rate limit (per-minute requests or tokens). Usually clears within a minute.",
  upstream_request_scoped: "A 429 without rate-limit headers: Anthropic refused this one request, not the account.",
  gateway_needs_login: "The gateway's Claude login expired or was revoked. The admin runs <code>claude-proxy login</code>.",
  gateway_refresh_unavailable: "Anthropic's sign-in service failed while the gateway renewed its token. It retries by itself.",
  gateway_upstream_unreachable: "Network error: the gateway couldn't reach the provider.",
  gateway_route_unconfigured: "The model is routed to another provider whose API key isn't set on the gateway.",
  overloaded_error: "Anthropic is temporarily overloaded. Not a quota problem; retry shortly.",
  api_error: "The provider returned a server error (5xx).",
};
Object.entries(ERROR_TIPS).forEach(([k, v]) => (TIPS[`err:${k}`] = v));
const ERROR_LABELS = { gateway_limit: "Gateway limit", gateway_auth: "Bad gateway key", gateway_key_scope: "OpenCode key: not allowed", gateway_bad_request: "Unreadable request", upstream_quota: "Account quota exhausted (429)",
                       upstream_throttle: "Per-minute throttle (429)", upstream_request_scoped: "Request refused (429)",
                       gateway_needs_login: "Subscription login needed", gateway_upstream_unreachable: "Upstream unreachable",
                       gateway_route_unconfigured: "Route key missing", overloaded_error: "Upstream overloaded (529)",
                       gateway_refresh_unavailable: "Token refresh temporarily failing",
                       api_error: "Upstream server error" };
ERROR_LABELS.usage_limit = "Usage limit reached (429)";
ERROR_LABELS.gateway_unavailable = "Gateway unavailable";

// A non-admin is never told about the subscription behind the gateway; the server sends them nothing about
// it (no account quota, no share limits, no credential state). These replace every tip that would mention it.
const USER_TIPS = {
  weighted: `<span class="th">Weighted tokens</span><p>Every token priced at API list rates and expressed in <b>{ref} input tokens</b>: an Opus output token counts for many, a cache read for a tenth of an input token of the same model.</p>`,
  cost: `<span class="th">API-equivalent cost</span><p>What these requests would cost at API list prices. A yardstick, not a bill.</p><p class="tm">Models missing from the price table count as $0 and are listed as unpriced.</p>`,
  burn_rate: `Weighted tokens per minute over the last 15 minutes, Claude models only.`,
  "split:provider": `Which service answered: <b>anthropic</b> for Claude models, others (such as Muse on Meta) for theirs.`,
  "metric:weighted": `Tokens priced by type and model, in {ref} input tokens.`,
  "metric:cost_usd": `Estimated cost at API list prices. Not a bill.`,
  provider_sub: `What these requests would cost at API list prices. Not a bill.`,
  cache_ratio: `<span class="th">Cache hit ratio</span><p>Share of prompt tokens read from the prompt cache instead of processed fresh. Cache reads cost a tenth of normal input, so higher means cheaper.</p>`,
  sess_weighted: `Tokens adjusted by type and model, in {ref} input tokens.`,
  sess_cost: `What it would cost at API prices. Not a bill.`,
  "kind:tokens_5h": `<span class="th">tokens 5h</span><p>Tokens in the last 5 hours.</p><p class="tm">${KIND_NOTE.window}</p>`,
  "kind:5h_limit": `<span class="th">5-hour limit</span><p>How much of your 5-hour allowance you have used. At 100%, Claude requests are refused until it resets.</p>`,
  "kind:weekly_limit": `<span class="th">Weekly limit</span><p>How much of your weekly allowance you have used. At 100%, Claude requests are refused until it resets.</p>`,
  "err:usage_limit": "A usage limit was reached. Requests work again once it resets.",
  "err:gateway_unavailable": "The gateway couldn't serve Claude requests at that moment. Try again later, or ask the admin.",
  "err:upstream_request_scoped": "The provider refused this one request (429).",
  "err:overloaded_error": "The provider is temporarily overloaded. Retry shortly.",
};
const ADMIN_TIPS = { ...TIPS };
const USER_LIMIT_KINDS = ["5h_limit", "weekly_limit"];   // share limits, as a non-admin's own allowance

const errorKind = (k, rejectedBy) => `${tipT(esc(ERROR_LABELS[k] || k), `err:${k}`)}${rejectedBy && rejectedBy !== "auth" && rejectedBy !== "request" ? ` <span class="muted">(${esc(rejectedBy)})</span>` : ""}`;

// "claude-sonnet-5" -> "Sonnet 5", "claude-haiku-4-5-20251001" -> "Haiku 4.5"; anything else as is.
function modelName(id) {
  const m = /^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?$/.exec(id || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2].replace("-", ".")}` : id;
}
const refModel = () => modelName(S.settings.reference_model);
// Requests without a model, such as Claude Code listing models (GET /v1/models): counted, but no tokens.
const NO_MODEL = "no model";
// Tip text quotes config through placeholders: {ref} the weighting reference model, {stale} the staleness cutoff.
const fillTip = (html) => html.replace(/\{ref\}/g, esc(refModel())).replace(/\{stale\}/g, fmtDur(S.settings.stale_after_s));

// Info dot, dotted term, or an attribute for any element. Unknown keys render nothing extra.
const tipI = (key, label = "What is this?") => (TIPS[key]
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
  // data-tip-side="right" (dropdown items): beside the element, or on its left when the right has no room.
  const side = el.dataset.tipSide !== "right" ? null
    : r.right + gap + w <= vw - gutter ? "right" : r.left - gap - w >= gutter ? "left" : null;
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
    if (gran === "hour") return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" });
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
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
    xAxis: { ...o.xAxis, type: "category", data: times.map((t) => timeLabel(gran)(t * 1000)) },
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
  return `${l.kind.replace(/_/g, " ")}${scope}`;
}
function limitValue(l) {
  if (l.kind === "allowed_models") return l.value;
  if (USER_LIMIT_KINDS.includes(l.kind)) {
    if (l.skipped || l.current == null) return "not measured right now";
    return `${l.current.toFixed(0)}% used${l.reset_in ? ` · resets in ${fmtDur(l.reset_in)}` : ""}`;
  }
  if (l.skipped) return `skipped · ${l.skipped}`;
  const f = l.unit === "usd" ? fmtUsd : l.unit === "pct" ? (v) => `${v.toFixed(1)} pts` : fmtNum;
  const reset = l.reset_in ? ` · resets in ${fmtDur(l.reset_in)}` : "";
  return `${l.estimated ? "est. " : ""}${f(l.current || 0)} / ${f(l.limit)}${reset}`;
}
function limitValueTip(l) {
  if (l.kind === "allowed_models") return "";
  if (USER_LIMIT_KINDS.includes(l.kind)) return l.skipped ? "Not enforced for now. Your other limits still apply." : "";
  if (l.skipped) return "Not enforced right now: without a fresh report from Anthropic the share can't be estimated. Token and request limits still apply.";
  if (l.estimated) return "<b>est.</b> means estimated, not measured. <b>Resets</b> is when Anthropic resets the account bucket.";
  if (!l.reset_in) return "";
  return "<b>Resets</b> is when the window that opened with the first request ends and the count goes back to zero.";
}
function limitsBlock(ls) {
  if (!ls.length) return `<span class="muted">No limits</span>`;
  return ls.map((l) => `<div class="limit-row"><span>${tipT(esc(limitLabel(l)), `kind:${l.kind}`)}</span><span class="muted num">${tipH(esc(limitValue(l)), limitValueTip(l))}</span>
    <div style="grid-column:1/-1">${l.kind === "allowed_models" || l.skipped ? "" : meter(l.pct)}</div></div>`).join("");
}

// ---------- views ----------

const VIEWS = {
  overview: { label: "Overview", render: renderOverview },
  usage: { label: "Usage over time", render: renderUsage },
  users: { label: "Users & limits", render: renderUsers, admin: true },
  quota: { label: "Account quota", render: renderQuota, admin: true },
  models: { label: "Models & cache", render: renderModels },
  activity: { label: "Activity", render: renderActivity },
  sessions: { label: "Sessions", render: renderSessions },
  errors: { label: "Errors", render: renderErrors },
  audit: { label: "Audit log", render: renderAudit, admin: true },
  user: { label: "User", render: renderUser, admin: true, hidden: true, parent: "users" },   // #user/<id>, opened from Users & limits
};
const isAdmin = () => S.user && S.user.role === "admin";

function renderTabs() {
  const tabs = Object.entries(VIEWS).filter(([, v]) => !v.hidden && (!v.admin || isAdmin()));
  const current = VIEWS[S.tab]?.parent || S.tab;
  $("#tabs").innerHTML = tabs.map(([k, v]) => `<button role="tab" data-tab="${k}" aria-selected="${k === current}">${esc(v.label)}</button>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  S.tab = b.dataset.tab; location.hash = S.tab; render();
});

async function render() {
  renderTabs();
  hideTip();
  disposeCharts();
  const view = VIEWS[S.tab] && (!VIEWS[S.tab].admin || isAdmin()) ? VIEWS[S.tab] : VIEWS.overview;
  const main = $("#main");
  main.innerHTML = `<p class="muted">Loading…</p>`;
  try { await view.render(main); } catch (e) {
    if (e.message !== "signed out") main.innerHTML = `<div class="banner critical"><span class="icon">!</span><span>${esc(e.message)}</span></div>`;
  }
}

function credentialPill(c) {
  const pill = $("#cred-pill");
  pill.querySelector(".dot").style.background = c.healthy ? css("--good") : css("--critical");
  pill.querySelector("span:last-child").textContent = c.healthy ? "Subscription linked" : "Subscription login needed";
  pill.dataset.tipHtml = (c.healthy
    ? `<span class="th">Subscription linked</span><p>The gateway holds a working Claude subscription login; everyone's Claude requests go out on it.</p>`
    : `<span class="th">Subscription login needed</span><p>The gateway's Claude login is missing or expired, so Claude requests fail. The admin runs <code>claude-proxy login</code> on the gateway host.</p>`)
    + (c.detail ? `<p class="tm">${esc(c.detail)}</p>` : "");
}

async function renderOverview(main) {
  const [ov, me] = await Promise.all([api("/api/overview"), isAdmin() ? null : api("/api/me/status")]);
  if (ov.credential) credentialPill(ov.credential);
  const t = ov.totals[S.prefs.period] || ov.totals["24h"];
  const banners = [];
  if (ov.credential && !ov.credential.healthy) banners.push(`<div class="banner critical"><span class="icon">!</span><span><b>Claude requests will fail:</b> the gateway has no working Claude subscription login. ${isAdmin() ? `Run <code>claude-proxy login</code> on the gateway host.${ov.credential.detail ? ` <span class="muted">(${esc(ov.credential.detail)})</span>` : ""}` : "Ask the admin to re-link it."}</span></div>`);
  const stale = (ov.quota || []).filter((q) => q.utilization_pct != null && q.stale);
  if (stale.length) banners.push(`<div class="banner warning"><span class="icon">⚠</span><span>No fresh account figures from Anthropic for ${stale.map((q) => q.bucket).join(", ")}. Share limits are skipped until one arrives; token limits still apply.</span></div>`);
  const unpriced = t.unpriced_models || [];
  const ex = ov.exhaustion;
  main.innerHTML = `
    <section class="view">
      <h2>${isAdmin() ? "Account overview" : `Your usage, ${esc(S.user.name)}`}</h2>
      <p class="lede">${isAdmin() ? "Everything that went through the gateway, all users." : "Only your own requests."}</p>
      ${banners.join("")}
      <div class="controls">${seg("period", [["24h", "Last 24 h"], ["7d", "7 days"], ["30d", "30 days"]], S.prefs.period)}</div>
      <div class="tiles">
        <div class="card tile"><div class="label">Requests${tipI("requests")}</div><div class="value">${fmtNum(t.requests)}</div><div class="foot">${t.requests >= 1000 ? `${esc(nfFull.format(t.requests))} forwarded` : "forwarded to a provider"}</div></div>
        <div class="card tile"><div class="label">Weighted tokens${tipI("weighted")}</div><div class="value">${fmtNum(t.weighted)}</div><div class="foot">in ${esc(refModel())} input tokens</div></div>
        <div class="card tile"><div class="label">Raw tokens${tipI("raw")}</div><div class="value">${fmtNum(t.raw)}</div><div class="foot">${fmtNum(t.cache_read)} of them cache reads</div></div>
        <div class="card tile"><div class="label">Est. API-equivalent cost${tipI("cost")}</div><div class="value">${fmtUsd(t.cost_usd)}</div><div class="foot">${unpriced.length ? `unpriced: ${esc(unpriced.join(", "))}` : isAdmin() ? "not billed on the subscription" : "at API list prices"}</div></div>
        ${isAdmin() ? `<div class="card tile"><div class="label">Active users${tipI("active_users")}</div><div class="value">${ov.active_users_24h}</div><div class="foot">in the last 24 h</div></div>` : ""}
        <div class="card tile"><div class="label">Burn rate${tipI("burn_rate")}</div><div class="value">${fmtNum(ov.burn_rate_weighted_per_min)}</div><div class="foot">weighted tokens / min, last 15 min</div></div>
      </div>
      <div class="grid${isAdmin() ? " cols-2" : ""}">
        ${isAdmin() ? `<div class="card"><h3>Account quota (reported by Anthropic)${tipI("quota")}</h3>
          <p class="sub">Bar length is the account's utilization. Segments are each user's ${tipT("<b>estimated share</b>", "share")}; grey is usage ${tipT("not attributed", "unattributed")} to any gateway user.</p>
          <div id="quota-bars"></div>
          ${ex && ex.pct_per_hour > 0 ? `<p class="sub" style="margin-top:12px">5-hour bucket rising ${ex.pct_per_hour.toFixed(1)} pts/h${ex.eta_s ? ` · at this pace it fills in <b>${fmtDur(ex.eta_s)}</b>${ex.before_reset ? " — before it resets" : ", after it resets"}` : ""}.${tipI("exhaustion")}</p>` : ""}
        </div>` : ""}
        <div class="card"><h3>${isAdmin() ? "Usage by user, last 7 days" : "Your limits"}</h3>
          <p class="sub">${isAdmin() ? "Daily, weighted tokens." : `Resets a window-length after the first request; ${tipT("the request that crosses a limit is still served", "served")}.`}</p>
          ${isAdmin() ? `<div class="chart short" id="ov-users"></div>` : limitsBlock(me.limits)}
        </div>
      </div>
    </section>`;
  wireSegs(main, render);
  if (isAdmin()) $("#quota-bars").innerHTML = ov.quota.map(quotaBar).join("") || `<p class="muted">No account figures yet. They arrive with the first response through the gateway.</p>`;
  if (isAdmin()) {
    const s = await api(`/api/series?range=7d&granularity=day&split=user&tz_offset=${tzOffset()}`);
    stackedTime($("#ov-users"), s.points, "weighted", "user", "day");
  }
}

function quotaBar(q) {
  if (q.utilization_pct == null) return `<div style="margin-bottom:14px"><b>${esc(q.bucket)}</b> <span class="muted">no data yet</span></div>`;
  const shares = Object.entries(q.shares).sort((a, b) => b[1] - a[1]);
  const segs = shares.map(([k, v]) => [k, v]).concat([["unattributed", q.unattributed || 0]]).filter(([, v]) => v > 0.05);
  const resets = q.resets_at ? `resets in ${fmtDur(q.resets_at - Date.now() / 1000)}` : "";
  return `<div style="margin-bottom:16px">
    <div style="display:flex;justify-content:space-between;align-items:baseline"><span><b>${tipT(q.bucket === "5h" ? "5-hour" : q.bucket === "7d" ? "7-day" : esc(q.bucket), `bucket:${q.bucket}`)}</b>
      <span style="font-size:22px;font-weight:650;margin-left:8px">${fmtPct(q.utilization_pct)}</span></span>
      <span class="muted">${esc(resets)}${q.stale ? ` · ${tipT("stale", "stale")}` : ""}</span></div>
    <div class="stack" role="img" aria-label="${esc(q.bucket)} utilization ${fmtPct(q.utilization_pct)}">
      ${segs.map(([k, v]) => `<span${tipAttr(k === "unattributed" ? `<b>Not attributed</b> · ${v.toFixed(1)} pts<p class="tm">Account usage the gateway can't pin on a user.</p>`
        : `<b>${esc(k)}</b> · ${v.toFixed(1)} pts, estimated`)} style="width:${v}%;background:${colorFor("user", k)}"></span>`).join("")}
    </div>
    <div class="legend">${segs.map(([k, v]) => `<span><i style="background:${colorFor("user", k)}"></i>${k === "unattributed" ? tipT("not attributed", "unattributed") : esc(k)} ${v.toFixed(1)}</span>`).join("")}</div>
  </div>`;
}

async function renderUsage(main) {
  const p = S.prefs;
  const splits = isAdmin() ? [["user", "By user"], ["model", "By model"], ["provider", "By provider", "split:provider"]] : [["model", "By model"], ["provider", "By provider", "split:provider"]];
  if (!isAdmin() && p.split === "user") p.split = "model";
  const d = await api(`/api/series?range=${p.range}&granularity=${p.granularity}&split=${p.split}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>Usage over time</h2>
    <p class="lede">${tipT("Weighted tokens", "weighted")} price each token type and model at API list-price ratios, in units of one ${esc(refModel())} input token, so they approximate what a request costs${isAdmin() ? " against the quota" : ""}. ${tipT("Raw tokens", "raw")} are dominated by cache reads.</p>
    <div class="controls">
      ${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range)}
      ${seg("granularity", [["hour", "Hourly"], ["day", "Daily"], ["week", "Weekly"]], p.granularity)}
      ${seg("split", splits, p.split)}
      ${seg("metric", Object.entries(METRICS).map(([k, v]) => [k, v, `metric:${k}`]), p.metric)}
    </div>
    <div class="card"><h3>${esc(METRICS[p.metric])} per ${p.granularity}</h3><p class="sub" id="usage-hint">Scroll or pinch to zoom.</p>
      <div class="chart tall" id="usage-chart"></div><div id="usage-table"></div></div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#usage-chart").outerHTML = `<p class="muted">No requests in this range.</p>`; return; }
  const { keys, times, series } = stackedTime($("#usage-chart"), d.points, p.metric, p.split, p.granularity);
  if (keys.length > 1) $("#usage-hint").textContent = "Scroll or pinch to zoom; click a legend item to hide it.";
  $("#usage-table").innerHTML = tableView("Show as table", ["Period", ...keys], times.map((t) =>
    [timeLabel(p.granularity)(t * 1000), ...keys.map((k) => fmtMetric(p.metric, series[k].get(t) || 0))]));
}

async function renderUsers(main) {
  const [{ users }, lim] = await Promise.all([api("/api/users"), api("/api/limits")]);
  S.kinds = lim.kinds;
  main.innerHTML = `<section class="view"><h2>Users & limits</h2>
    <p class="lede">Each person has their own gateway key. Limits are checked before every request against that person's recorded usage; share limits compare an ${tipT("estimated share", "share")} of the account's quota.</p>
    <div class="controls"><button class="btn primary" id="add-user">Add user</button></div>
    <div class="card table-wrap"><table class="data"><thead><tr>
      <th>User${tipI("key_prefix", "About the key prefix")}</th><th class="r">24 h</th><th class="r">7 d</th><th class="r">30 d</th><th class="r">Est. share 5 h / 7 d, pts${tipI("share_col")}</th><th>Limits${tipI("limits_col")}</th><th>Last request</th><th></th></tr></thead>
      <tbody>${users.map(userRow).join("")}</tbody></table></div>
    <p class="muted" style="font-size:12px;margin-top:8px">Usage columns are weighted tokens, with estimated cost underneath. Concurrent requests from one user can each pass a check before either is recorded, so a limit can be overshot by about one request per open session.</p>
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
  const state = u.revoked ? `<span class="badge">revoked</span>` : !u.enabled ? `<span class="badge">disabled</span>` : "";
  const cell = (k) => `<td class="r"><div>${fmtNum(u.usage[k].weighted)}</div><div class="muted">${fmtUsd(u.usage[k].cost_usd)}</div></td>`;
  const share = (b) => (u.share[b] == null ? "—" : `${u.share[b].toFixed(1)}`);
  return `<tr class="clickable" data-user="${u.id}">
    <td><span class="dot" style="background:${colorFor("user", u.name)};margin-right:6px"></span><a class="user-link" href="#user/${u.id}"><b>${esc(u.name)}</b></a> ${u.role === "admin" ? `<span class="badge">admin</span>` : ""} ${state}
      <div class="muted" style="font-size:12px">${esc(u.prefix)}…${u.routes_prefix ? ` · OpenCode ${esc(u.routes_prefix)}…` : ""}</div></td>
    ${cell("24h")}${cell("7d")}${cell("30d")}
    <td class="r">${share("5h")} / ${share("7d")}</td>
    <td style="min-width:240px">${limitsBlock(u.limits)}</td>
    <td class="muted nowrap">${fmtAgo(u.last_seen)}</td>
    <td>${userActions(u)}</td></tr>`;
}
function userActions(u) {
  const opencode = `<button class="btn small" data-act="routes_key" data-id="${u.id}" data-tip="act_routes_key">${u.routes_prefix ? "New OpenCode key" : "OpenCode key"}</button>` +
    (u.routes_prefix ? `<button class="btn small" data-act="routes_key_remove" data-id="${u.id}" data-tip="act_routes_key_remove">Remove OpenCode key</button>` : "");
  return `<div class="row-actions">${u.revoked ? `<button class="btn small danger" data-act="delete" data-id="${u.id}" data-tip="act_delete">Delete</button>` : u.id === S.user.id ? `<button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">Limits</button>${opencode}` : `
      <button class="btn small" data-act="limits" data-id="${u.id}" data-tip="act_limits">Limits</button>
      <button class="btn small" data-act="rotate" data-id="${u.id}" data-tip="act_rotate">Rotate key</button>
      ${opencode}
      <button class="btn small" data-act="${u.enabled ? "disable" : "enable"}" data-id="${u.id}" data-tip="act_${u.enabled ? "disable" : "enable"}">${u.enabled ? "Disable" : "Enable"}</button>
      <button class="btn small danger" data-act="revoke" data-id="${u.id}" data-tip="act_revoke">Revoke</button>`}</div>`;
}

function openDialog(html) {
  const d = $("#dialog");
  d.innerHTML = html;
  d.showModal();
  d.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => { d.close(); render(); }));
  return d;
}
function keyDialog(title, key, how = "The user sets it as <code>ANTHROPIC_AUTH_TOKEN</code>.") {
  openDialog(`<h3>${esc(title)}</h3><p>Copy this key now; it is not shown again. ${how}</p>
    <code class="key" id="new-key">${esc(key)}</code><p><button class="btn" id="copy-key">Copy</button> <button class="btn primary" data-close>Done</button></p>`);
  $("#copy-key").onclick = async () => { try { await navigator.clipboard.writeText(key); $("#copy-key").textContent = "Copied"; } catch { /* clipboard blocked */ } };
}
function addUserDialog() {
  const d = openDialog(`<h3>Add user</h3><form id="f-add" class="form-grid">
    <label>Name<input type="text" name="name" required maxlength="64"></label>
    <label><span>Role${tipI("role")}</span><select name="role"><option value="user">user</option><option value="admin">admin</option></select></label>
    <button class="btn primary" type="submit">Create</button></form><div class="error" id="add-err"></div>
    <p><button class="btn" data-close>Cancel</button></p>`);
  $("#f-add", d).onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { const r = await api("/api/admin/users", { method: "POST", body: { name: f.get("name"), role: f.get("role") } }); keyDialog(`Key for ${r.name}`, r.key); }
    catch (err) { $("#add-err").textContent = err.message; }
  };
}
async function userAction(act, id, u) {
  if (act === "limits") return limitsDialog(u);
  if (act === "revoke" && !confirmInline(`Revoke ${u.name}? Their key stops working immediately and cannot be re-enabled.`)) return;
  if (act === "delete" && !confirmInline(`Delete ${u.name} permanently? Their recorded usage is deleted too and disappears from account totals and charts. This cannot be undone.`)) return;
  try {
    const r = await api(`/api/admin/users/${id}/${act}`, { method: "POST", body: {} });
    if (act === "rotate") return keyDialog(`New key for ${u.name}`, r.key);
    if (act === "routes_key") return keyDialog(`OpenCode key for ${u.name}`, r.key,
      "It works only for third-party models. The user runs <code>claude-gateway on --opencode --url … --routes-key …</code> with it.");
    render();
  } catch (e) { alertInline(e.message); }
}
// Native confirm/alert block the page; use the dialog instead for anything but the irreversible revoke.
function confirmInline(msg) { return window.confirm(msg); }
function alertInline(msg) { openDialog(`<h3>Could not do that</h3><p>${esc(msg)}</p><button class="btn" data-close>OK</button>`); }

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
  const d = openDialog(`<h3>Limits for ${esc(u.name)}</h3>
    <div id="lim-list">${u.limits.length ? u.limits.map((l) => `<div class="limit-row"><span>${tipT(esc(limitLabel(l)), `kind:${l.kind}`)} = <b>${esc(l.value)}</b> <span class="muted">${esc(l.unit)}</span></span>
      <button class="btn small danger" data-del="${esc(l.kind)}" data-scope="${esc(l.scope)}">Remove</button></div>`).join("") : `<p class="muted">No limits yet.</p>`}</div>
    <h3 style="margin-top:16px">Set a limit</h3>
    <form id="f-lim" class="form-grid">
      <label><span>Kind${tipI("kind", "About the selected kind")}</span><select name="kind">${Object.keys(kinds).map((k) => `<option value="${esc(k)}">${esc(k.replace(/_/g, " "))}</option>`).join("")}</select></label>
      <label><span>Value${tipI("value")}</span><input type="text" name="value" required placeholder="e.g. 500000"></label>
      <label><span>Unit${tipI("unit", "About the selected unit")}</span><select name="unit"></select></label>
      <label><span>Models (glob)${tipI("scope")}</span><input type="text" name="scope" value="*"></label>
      <button class="btn primary" type="submit">Save</button>
    </form>
    <div class="hint" id="lim-hint" aria-live="polite"></div>
    <div class="error" id="lim-err"></div><p><button class="btn" data-close>Close</button></p>`);
  const f = $("#f-lim", d);
  tipSelect(f.kind, (k) => `<span class="th">${esc(k.replace(/_/g, " "))}</span><p>${KIND_SHORT[k] || KIND_TIPS[k] || ""}</p>`);
  // The Kind and Unit dots and the hint line describe whatever is selected; each Kind item also has its own short tip.
  const syncHint = () => {
    const k = f.kind.value, unit = f.unit.value;
    d.querySelector('[data-tip="kind"]').dataset.tipHtml = TIPS[`kind:${k}`] || `<b>${esc(k)}</b>`;
    d.querySelector('[data-tip="unit"]').dataset.tipHtml = `<span class="th">${esc(unit)}</span><p>${UNIT_TIPS[unit] || ""}</p>`;
    f.value.placeholder = LIMIT_PLACEHOLDER[unit] || "";
    $("#lim-hint", d).innerHTML = fillTip(`<b>${esc(k.replace(/_/g, " "))}</b>: ${KIND_TIPS[k] || ""} ${kindNote(k)}`
      + (UNIT_TIPS[unit] && unit !== "list" ? `<br><b>${esc(unit)}</b>: ${UNIT_TIPS[unit]}${f.unit.disabled ? " The only unit for this kind." : ""}` : ""));
  };
  const syncUnits = () => {
    const k = f.kind.value;
    f.unit.innerHTML = (kinds[k] || []).map((x) => `<option>${esc(x)}</option>`).join("");
    f.unit.disabled = f.unit.options.length < 2;   // requests_* only take count, share_* only pct: nothing to pick
    f.scope.disabled = k === "allowed_models";
    syncHint();
  };
  f.kind.onchange = syncUnits; f.unit.onchange = syncHint; syncUnits();
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/admin/limits", { method: "POST", body: { user: u.id, kind: f.kind.value, value: f.value.value, unit: f.unit.value, scope: f.scope.value || "*" } });
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
    main.innerHTML = `<section class="view"><p><a href="#users">← All users</a></p><p class="muted">There is no user with this id; they may have been deleted.</p></section>`;
    return;
  }
  const [ov, series, models, heat, sess, errs, reqs] = await Promise.all([
    api(`/api/overview?user_id=${id}`), api(`/api/series?range=${range}&granularity=${gran}&split=model&${q}`),
    api(`/api/models?range=${range}&${q}`), api(`/api/heatmap?range=${range}&${q}`), api(`/api/sessions?range=${range}&${q}`),
    api(`/api/errors?range=${range}&${q}`), api(`/api/requests?user_id=${id}&limit=100`)]);
  const t = ov.totals[period];
  const state = u.revoked ? `<span class="badge">revoked</span>` : !u.enabled ? `<span class="badge">disabled</span>` : "";
  const share = (b, label) => `<div class="card tile"><div class="label">Est. share, ${label}${tipI("share")}</div>
    <div class="value">${u.share[b] == null ? "—" : `${u.share[b].toFixed(1)}`}</div><div class="foot">${u.share[b] == null ? "no fresh report from Anthropic" : `points of the account's ${label} bucket`}</div></div>`;
  const span = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days" }[period];
  main.innerHTML = `
    <section class="view">
      <p class="crumb"><a href="#users">← All users</a></p>
      <h2><span class="dot" style="background:${colorFor("user", u.name)};margin-right:8px"></span>${esc(u.name)} ${u.role === "admin" ? `<span class="badge">admin</span>` : ""} ${state}</h2>
      <p class="lede">Key <code>${esc(u.prefix)}…</code> · added ${fmtTime(u.created_at)} · last request ${fmtAgo(u.last_seen)}</p>
      <div class="controls">${seg("userPeriod", [["24h", "Last 24 h"], ["7d", "7 days"], ["30d", "30 days"]], period)}<span class="spacer"></span>${userActions(u)}</div>
      <div class="tiles">
        <div class="card tile"><div class="label">Requests${tipI("requests")}</div><div class="value">${fmtNum(t.requests)}</div><div class="foot">${t.requests >= 1000 ? `${esc(nfFull.format(t.requests))} forwarded` : "forwarded to a provider"}</div></div>
        <div class="card tile"><div class="label">Weighted tokens${tipI("weighted")}</div><div class="value">${fmtNum(t.weighted)}</div><div class="foot">in ${esc(refModel())} input tokens</div></div>
        <div class="card tile"><div class="label">Raw tokens${tipI("raw")}</div><div class="value">${fmtNum(t.raw)}</div><div class="foot">${fmtNum(t.cache_read)} of them cache reads</div></div>
        <div class="card tile"><div class="label">Est. API-equivalent cost${tipI("cost")}</div><div class="value">${fmtUsd(t.cost_usd)}</div><div class="foot">not billed on the subscription</div></div>
        ${share("5h", "5-hour")}${share("7d", "7-day")}
      </div>
      <div class="grid cols-2">
        <div class="card"><h3>Limits${tipI("limits_col")}</h3><p class="sub">Resets a window-length after the first request; ${tipT("the request that crosses a limit is still served", "served")}.</p>${limitsBlock(u.limits)}</div>
        <div class="card table-wrap"><h3>Models</h3><p class="sub">${esc(span)}, largest first.</p>${models.models.length ? `<table class="data"><thead><tr><th>Model</th><th class="r">Requests</th><th class="r">Weighted</th><th class="r">Est. cost</th><th class="r">Cache hits${tipI("cache_ratio")}</th></tr></thead><tbody>
          ${models.models.map((m) => `<tr><td>${esc(m.model || NO_MODEL)}</td><td class="r">${fmtNum(m.requests)}</td><td class="r">${fmtNum(m.weighted)}</td><td class="r">${fmtUsd(m.cost_usd)}</td><td class="r">${m.cache_hit_ratio == null ? "—" : fmtPct(m.cache_hit_ratio * 100)}</td></tr>`).join("")}
          </tbody></table>` : `<p class="muted">No requests in range.</p>`}</div>
      </div>
      <div class="card" style="margin-top:16px"><h3>Weighted tokens per ${gran}, by model${tipI("weighted")}</h3><p class="sub">${esc(span)}.</p><div class="chart" id="u-usage"></div></div>
      <div class="grid cols-2" style="margin-top:16px">
        <div class="card"><h3>Activity</h3><p class="sub">Requests by weekday and hour, your local time, ${esc(span)}.</p><div class="heat-wrap"><div class="chart" id="u-heat"></div></div></div>
        <div class="card table-wrap"><h3>Recent errors</h3><p class="sub">${esc(span)}.</p>${errs.recent.length ? `<table class="data"><thead><tr><th>When</th><th>Kind</th><th>Model</th><th class="r">Status</th></tr></thead><tbody>
          ${errs.recent.slice(0, 10).map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td><td>${errorKind(r.k, r.rejected_by)}</td><td class="muted">${esc(r.model ?? "—")}</td><td class="r">${esc(r.status ?? "")}</td></tr>`).join("")}
          </tbody></table>` : `<p class="muted">No errors in range.</p>`}</div>
      </div>
      <div class="card table-wrap" style="margin-top:16px"><h3>Sessions</h3><p class="sub">${esc(span)}, most recently active first.</p>${sess.sessions.length ? sessionsTable(sess.sessions, false) : `<p class="muted">No sessions in range.</p>`}</div>
      <div class="card table-wrap" style="margin-top:16px"><h3>Recent requests${tipI("recent_requests")}</h3><p class="sub">The last ${reqs.requests.length} requests at any time, newest first; the range above doesn't apply.</p>${reqs.requests.length ? `<table class="data"><thead><tr><th>When</th><th>Model</th><th>Session</th><th class="r">Input</th><th class="r">Output</th><th class="r">Cache read</th><th class="r">Cache write</th><th class="r">Weighted</th><th class="r">Est. cost</th><th class="r">Took</th><th>Result</th></tr></thead><tbody>
        ${reqs.requests.map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td><td>${esc(r.model ?? "—")}</td><td>${sessionCell(r.title, r.session_id)}</td>
          <td class="r">${fmtNum(r.input)}</td><td class="r">${fmtNum(r.output)}</td><td class="r">${fmtNum(r.cache_read)}</td><td class="r">${fmtNum(r.cache_write)}</td>
          <td class="r">${fmtNum(r.weighted)}</td><td class="r">${fmtUsd(r.cost_usd)}</td><td class="r">${fmtDur(r.duration_s)}</td>
          <td>${r.kind ? `${errorKind(r.kind, r.rejected_by)}${r.status ? ` <span class="muted">${esc(r.status)}</span>` : ""}` : `<span class="muted">${esc(r.status ?? "—")}</span>`}</td></tr>`).join("")}
        </tbody></table>` : `<p class="muted">No requests yet.</p>`}</div>
    </section>`;
  wireSegs(main, render);
  wireUserActions(main, users);
  if (series.points.length) stackedTime($("#u-usage"), series.points, "weighted", "model", gran);
  else $("#u-usage").outerHTML = `<p class="muted">No requests in range.</p>`;
  if (heat.cells.length) heatmap($("#u-heat"), heat.cells);
  else $("#u-heat").outerHTML = `<p class="muted">No requests in range.</p>`;
}

async function renderQuota(main) {
  const p = S.prefs;
  const d = await api(`/api/quota/timeline?bucket=${encodeURIComponent(p.bucket)}&range=${p.range === "1d" ? "1d" : p.range}`);
  main.innerHTML = `<section class="view"><h2>Account quota</h2>
    <p class="lede">Utilization is <b>reported by Anthropic</b> for the whole subscription. Per-user figures are ${tipT("<b>estimated</b>", "share")}: each rise between two reports is split across the users whose requests finished in between, by weighted tokens. Reports come in whole percents.</p>
    <div class="controls">${seg("bucket", d.buckets.map((b) => [b, b, `bucket:${b}`]), p.bucket)} ${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"]], p.range)}</div>
    <div class="grid cols-2">
      <div class="card"><h3>Reported utilization${tipI("reported")}</h3><p class="sub">Each point is a report from response headers or the usage endpoint.</p><div class="chart" id="q-line"></div></div>
      <div class="card"><h3>Estimated share, current window${tipI("share")}</h3><p class="sub">Cumulative since the bucket last reset; grey is ${tipT("not attributed", "unattributed")}.</p><div class="chart" id="q-share"></div><div id="q-table"></div></div>
    </div></section>`;
  wireSegs(main, render);
  const o = baseOption();
  if (!d.snapshots.length) { $("#q-line").outerHTML = `<p class="muted">No reports for this bucket in range.</p>`; }
  else {
    chart($("#q-line")).setOption({
      ...o, legend: { show: false },
      tooltip: { ...o.tooltip, valueFormatter: (v) => fmtPct(v, 1) },
      xAxis: { ...o.xAxis, type: "time" }, yAxis: { ...o.yAxis, type: "value", max: 100, axisLabel: { ...o.yAxis.axisLabel, formatter: "{value}%" } },
      dataZoom: [{ type: "inside" }],
      series: [{ name: `${p.bucket} utilization`, type: "line", step: "end", showSymbol: false, lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1") },
                 data: d.snapshots.map((s) => [s.observed_at * 1000, s.utilization_pct]) }],
    });
  }
  const h = d.window_history;
  if (!h.length) { $("#q-share").outerHTML = `<p class="muted">No current window.</p>`; return; }
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
  if (hasOther) series.push({ name: OTHER, data: rows.map((r) => [r.t, r.other]), color: colorFor("user", OTHER) });
  series.push({ name: "not attributed", data: rows.map((r) => [r.t, r.un]), color: colorFor("user", "unattributed") });
  chart($("#q-share")).setOption({
    ...o, tooltip: { ...o.tooltip, valueFormatter: (v) => `${v.toFixed(1)} pts` },
    xAxis: { ...o.xAxis, type: "time" }, yAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: "{value}%" } },
    series: series.map((s) => ({ name: s.name, type: "line", stack: "share", step: "end", showSymbol: false, areaStyle: { color: s.color, opacity: 0.85 },
                                 lineStyle: { width: 0 }, itemStyle: { color: s.color }, data: s.data })),
  });
  const last = h[h.length - 1];
  $("#q-table").innerHTML = tableView("Show current shares as table", ["User", "Estimated share (pts)"],
    Object.entries(last.shares).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, v.toFixed(2)])
      .concat([["not attributed", Math.max(0, last.utilization_pct - Object.values(last.shares).reduce((a, b) => a + b, 0)).toFixed(2)]]));
}

async function renderModels(main) {
  const p = S.prefs;
  const d = await api(`/api/models?range=${p.range === "1d" ? "7d" : p.range}&tz_offset=${tzOffset()}`);
  const mm = p.modelMetric;
  main.innerHTML = `<section class="view"><h2>Models & cache</h2>
    <p class="lede">${isAdmin() ? "Claude models run on the shared subscription; other providers (such as Muse on Meta) are billed to their own API key." : "What each model's requests would cost at API list prices, and how much of your prompts the cache served."}</p>
    <div class="controls">${seg("range", [["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range === "1d" ? "7d" : p.range)} ${seg("modelMetric", [["cost_usd", "Est. cost", "metric:cost_usd"], ["weighted", "Weighted", "metric:weighted"], ["raw", "Raw tokens", "metric:raw"], ["requests", "Requests", "metric:requests"]], mm)}</div>
    <div class="tiles">${Object.entries(d.providers).map(([k, t]) => `<div class="card tile"><div class="label">${!isAdmin() ? `${k === "anthropic" ? "Claude" : esc(k)} (API-equivalent)${tipI("provider_sub")}` : k === "anthropic" ? `Claude (subscription, API-equivalent)${tipI("provider_sub")}` : `${esc(k)} (own API key)${tipI("provider_own")}`}</div><div class="value">${fmtUsd(t.cost_usd)}</div><div class="foot">${fmtNum(t.requests)} requests · ${fmtNum(t.raw)} tokens</div></div>`).join("")}</div>
    <div class="grid cols-2">
      <div class="card"><h3>${esc(METRICS[mm])} by model</h3><p class="sub">Largest first.</p><div class="chart" id="m-bars"></div></div>
      <div class="card"><h3>Cache hit ratio${tipI("cache_ratio")}</h3><p class="sub">Daily <code>${esc(d.formula)}</code></p><div class="chart" id="m-cache"></div></div>
    </div></section>`;
  wireSegs(main, render);
  const o = baseOption();
  const models = d.models.slice(0, 12).reverse();
  if (models.length) {
    chart($("#m-bars")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, trigger: "item", valueFormatter: (v) => fmtMetric(mm, v) },
      grid: { ...o.grid, top: 8 },
      xAxis: { ...o.yAxis, type: "value", splitNumber: narrow() ? 2 : 5, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => fmtMetric(mm, v) } },
      yAxis: { ...o.xAxis, type: "category", data: models.map((m) => m.model || NO_MODEL) },
      series: [{ type: "bar", barMaxWidth: 18, itemStyle: { color: css("--s1"), borderRadius: [0, 4, 4, 0] }, data: models.map((m) => m[mm]) }],
    });
  } else $("#m-bars").outerHTML = `<p class="muted">No requests in range.</p>`;
  const pts = d.cache_ratio.filter((p) => p.ratio != null);
  if (pts.length) {
    chart($("#m-cache")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, valueFormatter: (v) => fmtPct(v * 100, 1) },
      xAxis: { ...o.xAxis, type: "time", minInterval: 86400000, axisLabel: { ...o.xAxis.axisLabel, formatter: (v) => timeLabel("day")(v) } },
      yAxis: { ...o.yAxis, type: "value", min: 0, max: 1, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${Math.round(v * 100)}%` } },
      series: [{ name: "cache hit ratio", type: "line", showSymbol: pts.length < 40, symbolSize: 8, lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1") },
                 data: pts.map((p) => [p.t * 1000, p.ratio]) }],
    });
  } else $("#m-cache").outerHTML = `<p class="muted">No token data in range.</p>`;
}

async function renderActivity(main) {
  const p = S.prefs;
  const d = await api(`/api/heatmap?range=${p.range === "1d" ? "7d" : p.range}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>Activity</h2><p class="lede">Requests by weekday and hour of day, in your local time.</p>
    <div class="controls">${seg("range", [["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range === "1d" ? "7d" : p.range)}</div>
    <div class="card heat-wrap"><div class="chart" id="heat"></div></div></section>`;
  wireSegs(main, render);
  heatmap($("#heat"), d.cells);
}
function heatmap(el, cells) {
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const max = Math.max(1, ...cells.map((c) => c[2]));
  const o = baseOption();
  chart(el).setOption({
    ...o, legend: { show: false }, grid: { ...o.grid, top: 8, bottom: 48 },
    tooltip: { ...o.tooltip, trigger: "item", formatter: (x) => `${days[x.value[1]]} ${String(x.value[0]).padStart(2, "0")}:00 — <b>${x.value[2]}</b> requests` },
    xAxis: { ...o.xAxis, type: "category", data: [...Array(24).keys()].map((h) => String(h).padStart(2, "0")), splitArea: { show: false } },
    yAxis: { ...o.yAxis, type: "category", data: days, inverse: true, splitLine: { show: false } },
    visualMap: { min: 1, max: Math.max(2, max), calculable: false, orient: "horizontal", left: "center", bottom: 0, itemWidth: 12, itemHeight: 140, text: [`${max} requests`, "1"],
                 textStyle: { color: css("--muted"), fontSize: 11 },
                 inRange: { color: [css("--seq-1"), css("--seq-2"), css("--seq-3"), css("--seq-4"), css("--seq-5")] } },
    series: [{ type: "heatmap", data: cells, itemStyle: { borderColor: css("--surface"), borderWidth: 2, borderRadius: 3 } }],
  });
}

async function renderSessions(main) {
  const d = await api(`/api/sessions?range=${S.prefs.range === "1d" ? "1d" : "7d"}`);
  main.innerHTML = `<section class="view"><h2>Sessions</h2><p class="lede">Claude Code sessions seen in the last ${S.prefs.range === "1d" ? "24 hours" : "7 days"}, most recently active first.</p>
    <div class="card table-wrap">${d.sessions.length ? sessionsTable(d.sessions, isAdmin()) : `<p class="muted">No sessions recorded. Claude Code sends a session header on each request; if this stays empty, the header name has changed.</p>`}</div></section>`;
}
const sessionCell = (title, id) => (title ? `<div class="sess-title" title="${esc(title)}">${esc(title)}</div><div class="sess-id">${esc(id.slice(0, 8))}</div>`
  : id ? `<span class="muted">${esc(id.slice(0, 8))}</span>` : `<span class="muted">—</span>`);
function sessionsTable(sessions, showUser) {
  return `<table class="data"><thead><tr><th>Session${tipI("session")}</th>${showUser ? `<th>User${tipI("sess_user")}</th>` : ""}<th>Started${tipI("sess_started")}</th><th class="r">Duration${tipI("sess_duration")}</th><th class="r">Requests${tipI("sess_requests")}</th><th class="r">Weighted${tipI("sess_weighted")}</th><th class="r">Est. cost${tipI("sess_cost")}</th><th>Models${tipI("sess_models")}</th></tr></thead><tbody>
      ${sessions.map((s) => `<tr><td>${sessionCell(s.title, s.session_id)}</td>${showUser ? `<td>${esc(s.user)}</td>` : ""}<td class="nowrap">${fmtTime(s.first)}</td><td class="r">${fmtDur(s.duration_s)}</td>
        <td class="r">${s.requests}</td><td class="r">${fmtNum(s.weighted)}</td><td class="r">${fmtUsd(s.cost_usd)}</td><td class="muted">${esc(s.models.join(", "))}</td></tr>`).join("")}
    </tbody></table>`;
}

async function renderErrors(main) {
  const p = S.prefs;
  const range = p.range === "90d" ? "30d" : p.range;
  const d = await api(`/api/errors?range=${range}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>Errors</h2><p class="lede">${!isAdmin() ? "Requests the gateway or a provider refused. Hover a kind below for what it means." : `Gateway rejections and upstream errors. Upstream 429s are split into an ${tipT("exhausted account quota", "err:upstream_quota")}, a ${tipT("per-minute throttle", "err:upstream_throttle")}, and a ${tipT("refusal of one request", "err:upstream_request_scoped")}. Hover a kind below for what it means.`}</p>
    <div class="controls">${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"]], range)}</div>
    <div class="card"><div class="chart" id="err-chart"></div></div>
    <div class="card table-wrap" style="margin-top:16px"><h3>Most recent</h3>${d.recent.length ? `<table class="data"><thead><tr><th>When</th>${isAdmin() ? "<th>User</th>" : ""}<th>Kind</th><th>Model</th><th class="r">Status</th></tr></thead><tbody>
      ${d.recent.map((r) => `<tr><td class="nowrap">${fmtTime(r.started_at)}</td>${isAdmin() ? `<td>${esc(r.user ?? "—")}</td>` : ""}<td>${errorKind(r.k, r.rejected_by)}</td><td class="muted">${esc(r.model ?? "—")}</td><td class="r">${esc(r.status ?? "")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No errors in range.</p>`}</div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#err-chart").outerHTML = `<p class="muted">Nothing to plot.</p>`; return; }
  const pts = d.points.map((x) => ({ t: x.t, key: ERROR_LABELS[x.k] || x.k, n: x.n }));
  stackedTime($("#err-chart"), pts, "n", "error", d.bucket_s === 3600 ? "hour" : "day");
}

function auditDetail(json) {
  if (!json) return "";
  try { return Object.entries(JSON.parse(json)).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · "); } catch { return json; }
}
async function renderAudit(main) {
  const d = await api("/api/audit");
  main.innerHTML = `<section class="view"><h2>Audit log</h2><p class="lede">Admin actions from the dashboard and the CLI.</p>
    <div class="card table-wrap"><table class="data"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead><tbody>
      ${d.entries.map((e) => `<tr><td class="nowrap">${fmtTime(e.at)}</td><td>${esc(e.actor ?? "—")}</td><td>${esc(e.action)}</td><td>${esc(e.target ?? "")}</td><td class="muted">${esc(auditDetail(e.detail_json))}</td></tr>`).join("")}
    </tbody></table></div></section>`;
}

$("#dialog").addEventListener("close", hideTip);

// ---------- session ----------

function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  disposeCharts();
}
async function boot() {
  try {
    const s = await api("/api/session");
    S.user = s.user; S.csrf = s.csrf;
    if (s.settings) S.settings = { ...S.settings, ...s.settings };
    Object.assign(TIPS, isAdmin() ? ADMIN_TIPS : USER_TIPS);
    $("#cred-pill").hidden = !s.credential;
    if (s.credential) credentialPill(s.credential);
  } catch { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#who").textContent = `${S.user.name} · ${S.user.role}`;
  route();
  render();
}
// "#models" opens a tab, "#user/3" a user's page. Returns whether the address named a page.
function route() {
  const h = location.hash.slice(1), m = /^user\/(\d+)$/.exec(h);
  if (m) { S.tab = "user"; S.userId = +m[1]; return true; }
  if (VIEWS[h] && !VIEWS[h].hidden) { S.tab = h; return true; }
  return false;
}
async function login(path, body) {
  $("#login-error").textContent = "";
  try { const r = await api(path, { method: "POST", body }); S.csrf = r.csrf; await boot(); }
  catch (e) { $("#login-error").textContent = e.message; }
}
$("#form-admin").onsubmit = (e) => { e.preventDefault(); const f = new FormData(e.target); login("/api/login", { username: f.get("username"), password: f.get("password") }); };
$("#form-key").onsubmit = (e) => { e.preventDefault(); login("/api/login/key", { key: new FormData(e.target).get("key") }); };
$("#login-switch").onclick = (e) => {
  e.preventDefault();
  const keyMode = $("#form-key").classList.toggle("hidden") === false;
  $("#form-admin").classList.toggle("hidden", keyMode);
  e.target.textContent = keyMode ? "Sign in as admin instead" : "Use my gateway key instead";
};
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST", body: {} }).catch(() => {}); S.user = null; showLogin(); };
$("#theme-toggle").onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("cp-theme", document.documentElement.dataset.theme); } catch { /* ignore */ }
  render();
};
try { const t = localStorage.getItem("cp-theme"); if (t) document.documentElement.dataset.theme = t; } catch { /* ignore */ }
window.addEventListener("hashchange", () => { if (S.user && route()) render(); });
setInterval(() => { if (S.user && !document.hidden && !$("#dialog").open && ["overview", "users", "user"].includes(S.tab)) render(); }, 60000);

showLogin();
boot();
