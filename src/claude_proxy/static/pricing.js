"use strict";
// The public price list: tiers by length, discounts with a live countdown, usage hints and sold-out badges.
// Everything comes from /api/pricing; the page computes nothing about the account itself.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LENGTHS = [["day", "1 day"], ["week", "1 week"], ["month", "1 month"]];
const FAMILY = { sonnet: "Sonnet", opus: "Opus" };
let data = null;

function money(amount, currency) {
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount); }
  catch { return `${amount.toFixed(2)} ${currency}`; }
}
function countdown(endsAt) {
  const s = Math.max(0, Math.floor(endsAt - Date.now() / 1000));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d ? d + "d " : ""}${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
}
function hints(t) {
  const parts = [];
  for (const [fam, name] of Object.entries(FAMILY)) {
    const h = t.hours[fam];
    if (h && (h.per_5h != null || h.per_day != null)) {
      parts.push(`${name}: at least ${h.per_5h != null ? `${h.per_5h} h of steady use per 5-hour window` : ""}${h.per_5h != null && h.per_day != null ? ", " : ""}${h.per_day != null ? `${h.per_day} h per day` : ""}`);
    }
  }
  return `<div class="muted">≈ ${esc(t.compare)}${parts.length ? "<br>" + parts.map(esc).join("<br>") : ""}</div>`;
}
function cell(l, currency) {
  const badge = l.sold_out ? `<span class="badge">sold out</span>` : "";
  if (l.discount_ends_at) {
    return `<td class="r"><s class="muted">${esc(money(l.list_amount, currency))}</s> <b>${esc(money(l.amount, currency))}</b><br>
      <span class="muted countdown" data-ends="${l.discount_ends_at}">Offer ends in ${esc(countdown(l.discount_ends_at))}</span> ${badge}</td>`;
  }
  return `<td class="r"><b>${esc(money(l.amount, currency))}</b> ${badge}</td>`;
}
function render() {
  const root = document.getElementById("pricing");
  root.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Tier</th>${LENGTHS.map(([, n]) => `<th class="r">${n}</th>`).join("")}</tr></thead>
    <tbody>${data.tiers.map((t) => `<tr><td><b>${esc(t.label)}</b><div class="muted">${esc(t.share_pct)}% of the subscription</div>${hints(t)}</td>
      ${LENGTHS.map(([k]) => cell(t.lengths[k], data.currency)).join("")}</tr>`).join("")}</tbody></table></div>`;
  document.getElementById("rate-note").textContent = data.rate_set_at
    ? `Prices converted at the rate of ${new Date(data.rate_set_at * 1000).toLocaleDateString()}.` : "";
  document.getElementById("how-to-buy").textContent = data.how_to_buy || "Ask the gateway admin.";
}
function tick() {
  let reload = false;
  document.querySelectorAll(".countdown").forEach((el) => {
    const ends = +el.dataset.ends;
    if (ends <= Date.now() / 1000) reload = true;
    el.textContent = `Offer ends in ${countdown(ends)}`;
  });
  if (reload) location.reload();   // the regular price returns by itself
}
fetch("/api/pricing", { credentials: "omit" }).then((r) => r.json()).then((d) => { data = d; render(); setInterval(tick, 1000); })
  .catch(() => { document.getElementById("pricing").innerHTML = `<p class="muted">Prices are not available right now.</p>`; });
