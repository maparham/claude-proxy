"use strict";
// The public home page: one card per tier for the chosen length, discounts with a live countdown, usage hints and
// sold-out lengths. Everything comes from /api/pricing; the page computes nothing about the account itself.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LENGTHS = { day: ["/day", 1], week: ["/week", 7], month: ["/month", 30] };
const FAMILY = { sonnet: "Sonnet", opus: "Opus" };
let data = null;
let length = "week";
let skew = 0;           // server clock minus this browser's, in seconds: countdowns run on the server's clock
let reloaded = false;
const serverNow = () => Date.now() / 1000 + skew;
// One reload per discount end, remembered across the reload itself: if the server still lists the offer
// afterwards, the page shows it ended rather than reloading again.
const reloadKey = (ends) => `pricing-reloaded-${ends}`;
function reloadedFor(ends) { try { return sessionStorage.getItem(reloadKey(ends)) != null; } catch { return false; } }
function markReloaded(ends) { try { sessionStorage.setItem(reloadKey(ends), "1"); } catch { /* no storage: reloads at most once per page */ } }
try { const t = localStorage.getItem("cp-theme"); if (t) document.documentElement.dataset.theme = t; } catch { /* the dashboard's theme choice, if any */ }

function money(amount, currency) {
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount); }
  catch { return `${amount.toFixed(2)} ${currency}`; }
}
function countdown(endsAt) {
  const s = Math.max(0, Math.floor(endsAt - serverNow()));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d ? d + "d " : ""}${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
}
// "Sonnet: at least 4 h per 5-hour window, 2.8 h per day"; a missing number is left out, a family with none is omitted.
function hintLines(t) {
  return Object.entries(FAMILY).map(([fam, name]) => {
    const h = (t.hours || {})[fam] || {};
    const parts = [h.per_5h != null ? `${h.per_5h} h per 5-hour window` : null, h.per_day != null ? `${h.per_day} h per day` : null].filter(Boolean);
    return parts.length ? `${name}: at least ${parts.join(", ")}` : null;
  }).filter(Boolean);
}
// What a week or a month saves against buying the day ticket every day, on the charged prices (discounts count).
function savePct(t, len) {
  const daily = t.lengths.day.usd * LENGTHS[len][1];
  return len === "day" || !(daily > 0) ? 0 : Math.max(0, Math.floor((1 - t.lengths[len].usd / daily) * 100));
}
function card(t, i, len, currency) {
  const l = t.lengths[len];
  const off = l.discount_ends_at && l.list_usd > 0 ? Math.round((1 - l.usd / l.list_usd) * 100) : 0;
  const save = savePct(t, len);
  return `<article class="tier${i === 1 ? " featured" : ""}${l.sold_out ? " sold-out" : ""}">
    ${off > 0 ? `<span class="ribbon">−${off}%</span>` : ""}
    <h3>${esc(t.label)}</h3>
    ${t.compare ? `<p class="compare">≈ ${esc(t.compare)}</p>` : ""}
    <p class="share">${esc(+(+t.share_pct).toFixed(2))}% of the subscription</p>
    <p class="price">${l.discount_ends_at ? `<s>${esc(money(l.list_amount, currency))}</s> ` : ""}<b>${esc(money(l.amount, currency))}</b><span class="unit">${LENGTHS[len][0]}</span></p>
    ${l.discount_ends_at ? `<p class="countdown" data-ends="${l.discount_ends_at}">Offer ends in ${esc(countdown(l.discount_ends_at))}</p>` : ""}
    ${save > 0 ? `<p class="save">Save ${save}% vs daily</p>` : ""}
    <ul class="hints">${hintLines(t).map((h) => `<li>${esc(h)}</li>`).join("")}</ul>
    ${l.sold_out ? `<span class="btn get" aria-disabled="true">Sold out</span>`
      : `<a class="btn primary get" href="/dashboard?tier=${encodeURIComponent(t.tier)}&length=${len}">Get it</a>`}
  </article>`;
}
function cardsHtml(d, len) { return d.tiers.map((t, i) => card(t, i, len, d.currency)).join(""); }
function render() {
  document.getElementById("cards").innerHTML = cardsHtml(data, length);
  document.querySelectorAll("#length-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.length === length)));
  document.getElementById("rate-note").textContent = data.rate_set_at
    ? `Prices converted at the rate of ${new Date(data.rate_set_at * 1000).toLocaleDateString()}.` : "";
}
function tick() {
  if (reloaded) return;
  let ended = null;
  document.querySelectorAll(".countdown").forEach((el) => {
    const ends = +el.dataset.ends;
    if (ends <= serverNow()) { el.textContent = "Offer ended"; if (!reloadedFor(ends)) ended = ends; }
    else el.textContent = `Offer ends in ${countdown(ends)}`;
  });
  if (ended != null) { reloaded = true; markReloaded(ended); location.reload(); }   // the regular price returns by itself
}
document.getElementById("length-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-length]");
  if (!b || !data) return;
  length = b.dataset.length;
  render();
});
fetch("/api/pricing", { credentials: "omit" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }).then((d) => {
  data = d;
  if (typeof d.now === "number") skew = d.now - Date.now() / 1000;
  document.getElementById("how-to-buy").textContent = d.how_to_buy || "Ask the gateway admin.";
  render(); setInterval(tick, 1000);
})
  .catch(() => {
    document.getElementById("cards").innerHTML = `<p class="muted">Prices are not available right now.</p>`;
    document.getElementById("how-to-buy").textContent = "Ask the gateway admin.";
  });
