"use strict";

// ---------- state & helpers ----------

const S = {
  user: null, csrf: null, tab: "overview", charts: [],
  prefs: loadPrefs(),
  colorSlots: {},   // dimension -> {key: slot}; colour follows the entity for the whole session
};
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tzOffset = () => -new Date().getTimezoneOffset() * 60;

function loadPrefs() {
  const d = { range: "7d", granularity: "day", split: "user", metric: "weighted", period: "24h", bucket: "5h", modelMetric: "cost_usd" };
  try { return { ...d, ...JSON.parse(localStorage.getItem("cp-prefs") || "{}") }; } catch { return d; }
}
function savePrefs() { try { localStorage.setItem("cp-prefs", JSON.stringify(S.prefs)); } catch { /* private mode */ } }

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const SLOTS = 8;
const OTHER = "Other";
function colorFor(dim, key) {
  if (key === OTHER) return css("--other");
  if (key === "unattributed") return css("--unattributed");
  const m = (S.colorSlots[dim] ||= {});
  if (!(key in m)) m[key] = Object.keys(m).length;
  const slot = m[key];
  return slot < SLOTS ? css(`--s${slot + 1}`) : css("--other");
}
// Keep at most 7 named series; fold the tail into "Other" (never a 9th hue).
function topKeys(totals, max = 7) {
  const keys = Object.entries(totals).sort((a, b) => b[1] - a[1]).map(([k]) => k);
  return keys.length <= max + 1 ? keys : keys.slice(0, max);
}

const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, notation: "compact" });
const nfFull = new Intl.NumberFormat();
function fmtNum(v) { return v == null ? "—" : nf.format(v); }
function fmtUsd(v) { return v == null ? "—" : v === 0 ? "$0" : "$" + (v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(3)); }
function fmtPct(v, d = 0) { return v == null ? "—" : `${v.toFixed(d)}%`; }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
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

// ---------- charts ----------

function disposeCharts() { S.charts.forEach((c) => c.dispose()); S.charts = []; }
function chart(el) {
  const c = echarts.init(el, null, { renderer: "svg" });
  S.charts.push(c);
  return c;
}
window.addEventListener("resize", () => S.charts.forEach((c) => c.resize()));

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
    legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, textStyle: { color: ink2, fontSize: 12 } },
    xAxis: { axisLine: { lineStyle: { color: axis } }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: 11 }, splitLine: { show: false } },
    yAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: 11 }, splitLine: { lineStyle: { color: grid } } },
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
  const totals = {};
  points.forEach((p) => { totals[p.key ?? "—"] = (totals[p.key ?? "—"] || 0) + (p[metric] || 0); });
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
    const k = fold(p.key ?? "—");
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

function seg(name, options, value) {
  return `<span class="seg" data-pref="${name}">${options.map(([v, label]) =>
    `<button type="button" data-v="${v}" aria-pressed="${v === value}">${esc(label)}</button>`).join("")}</span>`;
}
function wireSegs(root, rerender) {
  root.querySelectorAll(".seg[data-pref]").forEach((s) => s.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
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
  if (l.skipped) return `skipped · ${l.skipped}`;
  const f = l.unit === "usd" ? fmtUsd : l.unit === "pct" ? (v) => `${v.toFixed(1)} pts` : fmtNum;
  const reset = l.reset_in ? ` · ${l.estimated ? "resets" : "frees"} in ${fmtDur(l.reset_in)}` : "";
  return `${l.estimated ? "est. " : ""}${f(l.current || 0)} / ${f(l.limit)}${reset}`;
}
function limitsBlock(ls) {
  if (!ls.length) return `<span class="muted">No limits</span>`;
  return ls.map((l) => `<div class="limit-row"><span>${esc(limitLabel(l))}</span><span class="muted num">${esc(limitValue(l))}</span>
    <div style="grid-column:1/-1">${l.kind === "allowed_models" || l.skipped ? "" : meter(l.pct)}</div></div>`).join("");
}

// ---------- views ----------

const VIEWS = {
  overview: { label: "Overview", render: renderOverview },
  usage: { label: "Usage over time", render: renderUsage },
  users: { label: "Users & limits", render: renderUsers, admin: true },
  quota: { label: "Account quota", render: renderQuota },
  models: { label: "Models & cache", render: renderModels },
  activity: { label: "Activity", render: renderActivity },
  sessions: { label: "Sessions", render: renderSessions },
  errors: { label: "Errors", render: renderErrors },
  audit: { label: "Audit log", render: renderAudit, admin: true },
};
const isAdmin = () => S.user && S.user.role === "admin";

function renderTabs() {
  const tabs = Object.entries(VIEWS).filter(([, v]) => !v.admin || isAdmin());
  $("#tabs").innerHTML = tabs.map(([k, v]) => `<button role="tab" data-tab="${k}" aria-selected="${k === S.tab}">${esc(v.label)}</button>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  S.tab = b.dataset.tab; location.hash = S.tab; render();
});

async function render() {
  renderTabs();
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
  pill.title = c.detail || "";
}

async function renderOverview(main) {
  const [ov, me] = await Promise.all([api("/api/overview"), isAdmin() ? null : api("/api/me/status")]);
  credentialPill(ov.credential);
  const t = ov.totals[S.prefs.period] || ov.totals["24h"];
  const banners = [];
  if (!ov.credential.healthy) banners.push(`<div class="banner critical"><span class="icon">!</span><span><b>Claude requests will fail:</b> the gateway has no working Claude subscription login. ${isAdmin() ? `Run <code>claude-proxy login</code> on the gateway host.${ov.credential.detail ? ` <span class="muted">(${esc(ov.credential.detail)})</span>` : ""}` : "Ask the admin to re-link it."}</span></div>`);
  const stale = ov.quota.filter((q) => q.utilization_pct != null && q.stale);
  if (stale.length) banners.push(`<div class="banner warning"><span class="icon">⚠</span><span>No fresh account figures from Anthropic for ${stale.map((q) => q.bucket).join(", ")}. Share limits are skipped until one arrives; token limits still apply.</span></div>`);
  const unpriced = t.unpriced_models || [];
  const ex = ov.exhaustion;
  main.innerHTML = `
    <section class="view">
      <h2>${isAdmin() ? "Account overview" : `Your usage, ${esc(S.user.name)}`}</h2>
      <p class="lede">${isAdmin() ? "Everything that went through the gateway, all users." : "Only your own requests. Account figures are for the whole shared subscription."}</p>
      ${banners.join("")}
      <div class="controls">${seg("period", [["24h", "Last 24 h"], ["7d", "7 days"], ["30d", "30 days"]], S.prefs.period)}</div>
      <div class="tiles">
        <div class="card tile"><div class="label">Requests</div><div class="value">${fmtNum(t.requests)}</div><div class="foot">${esc(nfFull.format(t.requests))} forwarded</div></div>
        <div class="card tile"><div class="label">Weighted tokens</div><div class="value">${fmtNum(t.weighted)}</div><div class="foot">Sonnet-input-equivalent</div></div>
        <div class="card tile"><div class="label">Raw tokens</div><div class="value">${fmtNum(t.raw)}</div><div class="foot">${fmtNum(t.cache_read)} of them cache reads</div></div>
        <div class="card tile"><div class="label">Est. API-equivalent cost</div><div class="value">${fmtUsd(t.cost_usd)}</div><div class="foot">${unpriced.length ? `unpriced: ${esc(unpriced.join(", "))}` : "not billed on the subscription"}</div></div>
        ${isAdmin() ? `<div class="card tile"><div class="label">Active users</div><div class="value">${ov.active_users_24h}</div><div class="foot">in the last 24 h</div></div>` : ""}
        <div class="card tile"><div class="label">Burn rate</div><div class="value">${fmtNum(ov.burn_rate_weighted_per_min)}</div><div class="foot">weighted tokens / min, last 15 min</div></div>
      </div>
      <div class="grid cols-2">
        <div class="card"><h3>Account quota (reported by Anthropic)</h3>
          <p class="sub">Bar length is the account's utilization. Segments are each user's <b>estimated share</b>; grey is usage not attributed to any gateway user.</p>
          <div id="quota-bars"></div>
          ${ex && ex.pct_per_hour > 0 ? `<p class="sub" style="margin-top:12px">5-hour bucket rising ${ex.pct_per_hour.toFixed(1)} pts/h${ex.eta_s ? ` · at this pace it fills in <b>${fmtDur(ex.eta_s)}</b>${ex.before_reset ? " — before it resets" : ", after it resets"}` : ""}.</p>` : ""}
        </div>
        <div class="card"><h3>${isAdmin() ? "Requests by user, last 7 days" : "Your limits"}</h3>
          <p class="sub">${isAdmin() ? "Daily, weighted tokens." : "Rolling windows; the request that crosses a limit is still served."}</p>
          ${isAdmin() ? `<div class="chart short" id="ov-users"></div>` : limitsBlock(me.limits)}
        </div>
      </div>
    </section>`;
  wireSegs(main, render);
  $("#quota-bars").innerHTML = ov.quota.map(quotaBar).join("") || `<p class="muted">No account figures yet. They arrive with the first response through the gateway.</p>`;
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
    <div style="display:flex;justify-content:space-between;align-items:baseline"><span><b>${q.bucket === "5h" ? "5-hour" : q.bucket === "7d" ? "7-day" : esc(q.bucket)}</b>
      <span style="font-size:22px;font-weight:650;margin-left:8px">${fmtPct(q.utilization_pct)}</span></span>
      <span class="muted">${esc(resets)}${q.stale ? " · stale" : ""}</span></div>
    <div class="stack" role="img" aria-label="${esc(q.bucket)} utilization ${fmtPct(q.utilization_pct)}">
      ${segs.map(([k, v]) => `<span title="${esc(k)}: ${v.toFixed(1)} pts" style="width:${v}%;background:${colorFor("user", k)}"></span>`).join("")}
    </div>
    <div class="legend">${segs.map(([k, v]) => `<span><i style="background:${colorFor("user", k)}"></i>${esc(k === "unattributed" ? "not attributed" : k)} ${v.toFixed(1)}</span>`).join("")}</div>
  </div>`;
}

async function renderUsage(main) {
  const p = S.prefs;
  const splits = isAdmin() ? [["user", "By user"], ["model", "By model"], ["provider", "By provider"]] : [["model", "By model"], ["provider", "By provider"]];
  if (!isAdmin() && p.split === "user") p.split = "model";
  const d = await api(`/api/series?range=${p.range}&granularity=${p.granularity}&split=${p.split}&tz_offset=${tzOffset()}`);
  main.innerHTML = `<section class="view"><h2>Usage over time</h2>
    <p class="lede">Weighted tokens price each token type and model at API list-price ratios, in units of a Claude Sonnet 5 input token, so they approximate what a request costs against the quota. Raw tokens are dominated by cache reads.</p>
    <div class="controls">
      ${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range)}
      ${seg("granularity", [["hour", "Hourly"], ["day", "Daily"], ["week", "Weekly"]], p.granularity)}
      ${seg("split", splits, p.split)}
      ${seg("metric", Object.entries(METRICS), p.metric)}
    </div>
    <div class="card"><h3>${esc(METRICS[p.metric])} per ${p.granularity}</h3><p class="sub">Scroll or pinch to zoom; click a legend item to hide it.</p>
      <div class="chart tall" id="usage-chart"></div><div id="usage-table"></div></div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#usage-chart").outerHTML = `<p class="muted">No requests in this range.</p>`; return; }
  const { keys, times, series } = stackedTime($("#usage-chart"), d.points, p.metric, p.split, p.granularity);
  $("#usage-table").innerHTML = tableView("Show as table", ["Period", ...keys], times.map((t) =>
    [timeLabel(p.granularity)(t * 1000), ...keys.map((k) => fmtMetric(p.metric, series[k].get(t) || 0))]));
}

async function renderUsers(main) {
  const [{ users }, lim] = await Promise.all([api("/api/users"), api("/api/limits")]);
  S.kinds = lim.kinds;
  main.innerHTML = `<section class="view"><h2>Users & limits</h2>
    <p class="lede">Each person has their own gateway key. Limits are checked before every request against that person's recorded usage; share limits compare an estimated share of the account's quota.</p>
    <div class="controls"><button class="btn primary" id="add-user">Add user</button></div>
    <div class="card table-wrap"><table class="data"><thead><tr>
      <th>User</th><th class="r">24 h</th><th class="r">7 d</th><th class="r">30 d</th><th class="r">Est. share 5 h / 7 d</th><th>Limits</th><th>Last seen</th><th></th></tr></thead>
      <tbody>${users.map(userRow).join("")}</tbody></table></div>
    <p class="muted" style="font-size:12px;margin-top:8px">Usage columns are weighted tokens, with estimated cost underneath. Concurrent requests from one user can each pass a check before either is recorded, so a limit can be overshot by about one request per open session.</p>
  </section>`;
  $("#add-user").onclick = addUserDialog;
  main.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => userAction(b.dataset.act, +b.dataset.id, users.find((u) => u.id === +b.dataset.id))));
}

function userRow(u) {
  const state = u.revoked ? `<span class="badge">revoked</span>` : !u.enabled ? `<span class="badge">disabled</span>` : "";
  const cell = (k) => `<td class="r"><div>${fmtNum(u.usage[k].weighted)}</div><div class="muted">${fmtUsd(u.usage[k].cost_usd)}</div></td>`;
  const share = (b) => (u.share[b] == null ? "—" : `${u.share[b].toFixed(1)}`);
  return `<tr>
    <td><span class="dot" style="background:${colorFor("user", u.name)};margin-right:6px"></span><b>${esc(u.name)}</b> ${u.role === "admin" ? `<span class="badge">admin</span>` : ""} ${state}
      <div class="muted" style="font-size:12px">${esc(u.prefix)}…</div></td>
    ${cell("24h")}${cell("7d")}${cell("30d")}
    <td class="r">${share("5h")} / ${share("7d")}</td>
    <td style="min-width:240px">${limitsBlock(u.limits)}</td>
    <td class="muted">${fmtAgo(u.last_seen)}</td>
    <td><div class="row-actions">${u.revoked ? `<button class="btn small danger" data-act="delete" data-id="${u.id}">Delete</button>` : u.id === S.user.id ? `<button class="btn small" data-act="limits" data-id="${u.id}">Limits</button>` : `
      <button class="btn small" data-act="limits" data-id="${u.id}">Limits</button>
      <button class="btn small" data-act="rotate" data-id="${u.id}">Rotate key</button>
      <button class="btn small" data-act="${u.enabled ? "disable" : "enable"}" data-id="${u.id}">${u.enabled ? "Disable" : "Enable"}</button>
      <button class="btn small danger" data-act="revoke" data-id="${u.id}">Revoke</button>`}</div></td></tr>`;
}

function openDialog(html) {
  const d = $("#dialog");
  d.innerHTML = html;
  d.showModal();
  d.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => { d.close(); render(); }));
  return d;
}
function keyDialog(title, key) {
  openDialog(`<h3>${esc(title)}</h3><p>Copy this key now; it is not shown again. The user sets it as <code>ANTHROPIC_AUTH_TOKEN</code>.</p>
    <code class="key" id="new-key">${esc(key)}</code><p><button class="btn" id="copy-key">Copy</button> <button class="btn primary" data-close>Done</button></p>`);
  $("#copy-key").onclick = async () => { try { await navigator.clipboard.writeText(key); $("#copy-key").textContent = "Copied"; } catch { /* clipboard blocked */ } };
}
function addUserDialog() {
  const d = openDialog(`<h3>Add user</h3><form id="f-add" class="form-grid">
    <label>Name<input type="text" name="name" required maxlength="64"></label>
    <label>Role<select name="role"><option value="user">user</option><option value="admin">admin</option></select></label>
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
    render();
  } catch (e) { alertInline(e.message); }
}
// Native confirm/alert block the page; use the dialog instead for anything but the irreversible revoke.
function confirmInline(msg) { return window.confirm(msg); }
function alertInline(msg) { openDialog(`<h3>Could not do that</h3><p>${esc(msg)}</p><button class="btn" data-close>OK</button>`); }

function limitsDialog(u) {
  const kinds = S.kinds || {};
  const d = openDialog(`<h3>Limits for ${esc(u.name)}</h3>
    <div id="lim-list">${u.limits.length ? u.limits.map((l) => `<div class="limit-row"><span>${esc(limitLabel(l))} = <b>${esc(l.value)}</b> <span class="muted">${esc(l.unit)}</span></span>
      <button class="btn small danger" data-del="${esc(l.kind)}" data-scope="${esc(l.scope)}">Remove</button></div>`).join("") : `<p class="muted">No limits yet.</p>`}</div>
    <h3 style="margin-top:16px">Set a limit</h3>
    <form id="f-lim" class="form-grid">
      <label>Kind<select name="kind">${Object.keys(kinds).map((k) => `<option>${esc(k)}</option>`).join("")}</select></label>
      <label>Value<input type="text" name="value" required placeholder="e.g. 500000"></label>
      <label>Unit<select name="unit"></select></label>
      <label>Models (glob)<input type="text" name="scope" value="*" title="Only count and limit requests for matching models, e.g. claude-opus-*"></label>
      <button class="btn primary" type="submit">Save</button>
    </form>
    <p class="muted" style="font-size:12px">Windows are rolling. <b>share_5h / share_7d</b> are percentage points of the account's reported bucket, compared with the user's <i>estimated</i> share. <b>allowed_models</b> takes comma-separated globs such as <code>claude-sonnet-*,muse-spark</code>.</p>
    <div class="error" id="lim-err"></div><p><button class="btn" data-close>Close</button></p>`);
  const f = $("#f-lim", d);
  const syncUnits = () => {
    const k = f.kind.value;
    f.unit.innerHTML = (kinds[k] || []).map((x) => `<option>${esc(x)}</option>`).join("");
    f.scope.disabled = k === "allowed_models";
  };
  f.kind.onchange = syncUnits; syncUnits();
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

async function renderQuota(main) {
  const p = S.prefs;
  const d = await api(`/api/quota/timeline?bucket=${encodeURIComponent(p.bucket)}&range=${p.range === "1d" ? "1d" : p.range}`);
  main.innerHTML = `<section class="view"><h2>Account quota</h2>
    <p class="lede">Utilization is <b>reported by Anthropic</b> for the whole subscription. Per-user figures are <b>estimated</b>: each rise between two reports is split across the users whose requests finished in between, by weighted tokens. Reports come in whole percents.</p>
    <div class="controls">${seg("bucket", d.buckets.map((b) => [b, b]), p.bucket)} ${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"]], p.range)}</div>
    <div class="grid cols-2">
      <div class="card"><h3>Reported utilization</h3><p class="sub">Each point is a report from response headers or the usage endpoint.</p><div class="chart" id="q-line"></div></div>
      <div class="card"><h3>Estimated share, current window</h3><p class="sub">Cumulative since the bucket last reset; grey is not attributed.</p><div class="chart" id="q-share"></div><div id="q-table"></div></div>
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
  const rows = h.map((pt) => {
    const named = users.map((k) => pt.shares[k] || 0);
    const other = Object.entries(pt.shares).filter(([k]) => !users.includes(k)).reduce((a, [, v]) => a + v, 0);
    const un = Math.max(0, pt.utilization_pct - named.reduce((a, b) => a + b, 0) - other);
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
    <p class="lede">Claude models run on the shared subscription; other providers (such as Muse on Meta) are billed to their own API key.</p>
    <div class="controls">${seg("range", [["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range === "1d" ? "7d" : p.range)} ${seg("modelMetric", [["cost_usd", "Est. cost"], ["weighted", "Weighted"], ["raw", "Raw tokens"], ["requests", "Requests"]], mm)}</div>
    <div class="tiles">${Object.entries(d.providers).map(([k, t]) => `<div class="card tile"><div class="label">${k === "anthropic" ? "Claude (subscription, API-equivalent)" : `${esc(k)} (own API key)`}</div><div class="value">${fmtUsd(t.cost_usd)}</div><div class="foot">${fmtNum(t.requests)} requests · ${fmtNum(t.raw)} tokens</div></div>`).join("")}</div>
    <div class="grid cols-2">
      <div class="card"><h3>${esc(METRICS[mm])} by model</h3><p class="sub">Largest first.</p><div class="chart" id="m-bars"></div></div>
      <div class="card"><h3>Cache hit ratio</h3><p class="sub">Daily <code>${esc(d.formula)}</code></p><div class="chart" id="m-cache"></div></div>
    </div></section>`;
  wireSegs(main, render);
  const o = baseOption();
  const models = d.models.slice(0, 12).reverse();
  if (models.length) {
    chart($("#m-bars")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, trigger: "item", valueFormatter: (v) => fmtMetric(mm, v) },
      grid: { ...o.grid, top: 8 },
      xAxis: { ...o.yAxis, type: "value", axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => fmtMetric(mm, v) } },
      yAxis: { ...o.xAxis, type: "category", data: models.map((m) => m.model || "unknown") },
      series: [{ type: "bar", barMaxWidth: 18, itemStyle: { color: css("--s1"), borderRadius: [0, 4, 4, 0] }, data: models.map((m) => m[mm]) }],
    });
  } else $("#m-bars").outerHTML = `<p class="muted">No requests in range.</p>`;
  const pts = d.cache_ratio.filter((p) => p.ratio != null);
  if (pts.length) {
    chart($("#m-cache")).setOption({
      ...o, legend: { show: false }, tooltip: { ...o.tooltip, valueFormatter: (v) => fmtPct(v * 100, 1) },
      xAxis: { ...o.xAxis, type: "time" }, yAxis: { ...o.yAxis, type: "value", min: 0, max: 1, axisLabel: { ...o.yAxis.axisLabel, formatter: (v) => `${Math.round(v * 100)}%` } },
      series: [{ name: "cache hit ratio", type: "line", showSymbol: pts.length < 40, symbolSize: 8, lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1") },
                 data: pts.map((p) => [p.t * 1000, p.ratio]) }],
    });
  } else $("#m-cache").outerHTML = `<p class="muted">No token data in range.</p>`;
}

async function renderActivity(main) {
  const p = S.prefs;
  const d = await api(`/api/heatmap?range=${p.range === "1d" ? "7d" : p.range}&tz_offset=${tzOffset()}`);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  main.innerHTML = `<section class="view"><h2>Activity</h2><p class="lede">Requests by weekday and hour of day, in your local time.</p>
    <div class="controls">${seg("range", [["7d", "7 d"], ["30d", "30 d"], ["90d", "90 d"]], p.range === "1d" ? "7d" : p.range)}</div>
    <div class="card"><div class="chart" id="heat"></div></div></section>`;
  wireSegs(main, render);
  const max = Math.max(1, ...d.cells.map((c) => c[2]));
  const o = baseOption();
  chart($("#heat")).setOption({
    ...o, legend: { show: false }, grid: { ...o.grid, top: 8, bottom: 48 },
    tooltip: { ...o.tooltip, trigger: "item", formatter: (x) => `${days[x.value[1]]} ${String(x.value[0]).padStart(2, "0")}:00 — <b>${x.value[2]}</b> requests` },
    xAxis: { ...o.xAxis, type: "category", data: [...Array(24).keys()].map((h) => String(h).padStart(2, "0")), splitArea: { show: false } },
    yAxis: { ...o.yAxis, type: "category", data: days, splitLine: { show: false } },
    visualMap: { min: 0, max, calculable: false, orient: "horizontal", left: "center", bottom: 0, itemWidth: 12, itemHeight: 140, text: [`${max} requests`, "0"],
                 textStyle: { color: css("--muted"), fontSize: 11 },
                 inRange: { color: [css("--seq-0"), css("--seq-1"), css("--seq-2"), css("--seq-3"), css("--seq-4"), css("--seq-5")] } },
    series: [{ type: "heatmap", data: d.cells, itemStyle: { borderColor: css("--surface"), borderWidth: 2, borderRadius: 3 } }],
  });
}

async function renderSessions(main) {
  const d = await api(`/api/sessions?range=${S.prefs.range === "1d" ? "1d" : "7d"}`);
  main.innerHTML = `<section class="view"><h2>Sessions</h2><p class="lede">Claude Code sessions seen in the last ${S.prefs.range === "1d" ? "24 hours" : "7 days"}, newest first.</p>
    <div class="card table-wrap">${d.sessions.length ? `<table class="data"><thead><tr><th>Session</th>${isAdmin() ? "<th>User</th>" : ""}<th>Started</th><th class="r">Duration</th><th class="r">Requests</th><th class="r">Weighted</th><th class="r">Est. cost</th><th>Models</th></tr></thead><tbody>
      ${d.sessions.map((s) => `<tr><td class="muted">${esc(s.session_id.slice(0, 8))}</td>${isAdmin() ? `<td>${esc(s.user)}</td>` : ""}<td>${fmtTime(s.first)}</td><td class="r">${fmtDur(s.duration_s)}</td>
        <td class="r">${s.requests}</td><td class="r">${fmtNum(s.weighted)}</td><td class="r">${fmtUsd(s.cost_usd)}</td><td class="muted">${esc(s.models.join(", "))}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No sessions recorded. Claude Code sends a session header on each request; if this stays empty, the header name has changed.</p>`}</div></section>`;
}

async function renderErrors(main) {
  const p = S.prefs;
  const range = p.range === "90d" ? "30d" : p.range;
  const d = await api(`/api/errors?range=${range}&tz_offset=${tzOffset()}`);
  const LABELS = { gateway_limit: "Gateway limit", gateway_auth: "Bad gateway key", upstream_quota: "Account quota exhausted (429)",
                   upstream_throttle: "Per-minute throttle (429)", upstream_request_scoped: "Request refused (429)",
                   gateway_needs_login: "Subscription login needed", gateway_upstream_unreachable: "Upstream unreachable",
                   gateway_route_unconfigured: "Route key missing", overloaded_error: "Upstream overloaded (529)",
                   gateway_refresh_unavailable: "Token refresh temporarily failing",
                   api_error: "Upstream server error" };
  main.innerHTML = `<section class="view"><h2>Errors</h2><p class="lede">Gateway rejections and upstream errors. Upstream 429s are split into an exhausted account quota, a per-minute throttle, and a refusal of one request.</p>
    <div class="controls">${seg("range", [["1d", "24 h"], ["7d", "7 d"], ["30d", "30 d"]], range)}</div>
    <div class="card"><div class="chart" id="err-chart"></div></div>
    <div class="card table-wrap" style="margin-top:16px"><h3>Most recent</h3>${d.recent.length ? `<table class="data"><thead><tr><th>When</th>${isAdmin() ? "<th>User</th>" : ""}<th>Kind</th><th>Model</th><th class="r">Status</th></tr></thead><tbody>
      ${d.recent.map((r) => `<tr><td>${fmtTime(r.started_at)}</td>${isAdmin() ? `<td>${esc(r.user ?? "—")}</td>` : ""}<td>${esc(LABELS[r.k] || r.k)}${r.rejected_by && r.rejected_by !== "auth" ? ` <span class="muted">(${esc(r.rejected_by)})</span>` : ""}</td><td class="muted">${esc(r.model ?? "")}</td><td class="r">${esc(r.status ?? "")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No errors in range.</p>`}</div></section>`;
  wireSegs(main, render);
  if (!d.points.length) { $("#err-chart").outerHTML = `<p class="muted">Nothing to plot.</p>`; return; }
  const pts = d.points.map((x) => ({ t: x.t, key: LABELS[x.k] || x.k, n: x.n }));
  stackedTime($("#err-chart"), pts, "n", "error", d.bucket_s === 3600 ? "hour" : "day");
}

async function renderAudit(main) {
  const d = await api("/api/audit");
  main.innerHTML = `<section class="view"><h2>Audit log</h2><p class="lede">Admin actions from the dashboard and the CLI.</p>
    <div class="card table-wrap"><table class="data"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead><tbody>
      ${d.entries.map((e) => `<tr><td>${fmtTime(e.at)}</td><td>${esc(e.actor ?? "—")}</td><td>${esc(e.action)}</td><td>${esc(e.target ?? "")}</td><td class="muted">${esc(e.detail_json ?? "")}</td></tr>`).join("")}
    </tbody></table></div></section>`;
}

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
    credentialPill(s.credential);
  } catch { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#who").textContent = `${S.user.name} · ${S.user.role}`;
  const h = location.hash.slice(1);
  if (VIEWS[h]) S.tab = h;
  render();
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
window.addEventListener("hashchange", () => { const h = location.hash.slice(1); if (VIEWS[h] && h !== S.tab) { S.tab = h; render(); } });
setInterval(() => { if (S.user && !document.hidden && !$("#dialog").open && ["overview", "users"].includes(S.tab)) render(); }, 60000);

showLogin();
boot();
