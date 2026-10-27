"use strict";
// The public home page: one card per tier for the chosen length, discounts with a live countdown, usage hints and
// sold-out lengths. Everything comes from /api/pricing; the page computes nothing about the account itself.
// "Get it" opens an order dialog with the ticket's price and terms; going on from it signs in, and the dashboard opens the order.
// Bilingual (i18n.js, loaded first): Persian by default, English on request; applyStatic()/t() give the page's own text.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bdi = (v) => `<bdi>${esc(v)}</bdi>`;   // a tier's own name or the admin's "how to pay" text, inside translated copy
// "Lite"/"Standard" are the tiers' default English names; only an exact, unrenamed match gets a Persian one, so
// an admin who renamed a tier keeps seeing their own label.
function tierName(t_) {
  const defaults = { lite: "Lite", standard: "Standard" }, key = `tier.${t_.tier}`;
  return defaults[t_.tier] === t_.label && (I18N.fa[key] || I18N.en[key]) ? t(key) : bdi(t_.label);
}
const LENGTHS = { day: ["home.per_day", 1], week: ["home.per_week", 7], month: ["home.per_month", 30] };
function lengthName(len) { return t(`price.1_${len}`); }   // "1 day" / "1 week" / "1 month", shared with the dashboard's price list
const nf0 = new Intl.NumberFormat(LOC, { useGrouping: false });
const nf2 = new Intl.NumberFormat(LOC, { minimumIntegerDigits: 2, useGrouping: false });
let data = null;
let length = "week";
let currency = null;   // set to the page's own currency (IRT today) once /api/pricing answers
let skew = 0;           // server clock minus this browser's, in seconds: countdowns run on the server's clock
let reloaded = false;
const serverNow = () => Date.now() / 1000 + skew;
// One reload per discount end, remembered across the reload itself: if the server still lists the offer
// afterwards, the page shows it ended rather than reloading again.
const reloadKey = (ends) => `pricing-reloaded-${ends}`;
function reloadedFor(ends) { try { return sessionStorage.getItem(reloadKey(ends)) != null; } catch { return false; } }
function markReloaded(ends) { try { sessionStorage.setItem(reloadKey(ends), "1"); } catch { /* no storage: reloads at most once per page */ } }
try { const th = localStorage.getItem("cp-theme"); if (th) document.documentElement.dataset.theme = th; } catch { /* the dashboard's theme choice, if any */ }
applyStatic();
document.getElementById("lang-toggle").onclick = () => setLang(LANG === "fa" ? "en" : "fa");

function money(amount, currency) {
  if (currency === "IRT") return t("cur.toman", { v: new Intl.NumberFormat(LOC, { maximumFractionDigits: 0 }).format(Math.round(amount)) });   // not ISO: Intl would print "IRT 1,250,000.00"
  try { return new Intl.NumberFormat(LOC, { style: "currency", currency }).format(amount); }
  catch { return `${nf0.format(amount)} ${currency}`; }
}
// Clock-style countdown: English keeps its zero-padded "00h 30m"; Persian spells the units out, unpadded.
function countdown(endsAt) {
  const s = Math.max(0, Math.floor(endsAt - serverNow()));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const hm = LANG === "fa" ? nf0 : nf2;
  const vars = { d: nf0.format(d), h: hm.format(h), m: hm.format(m) };
  return t(d ? "home.countdown_full" : "home.countdown", vars);
}
// What a week or a month saves against buying the day ticket every day, on the charged prices (discounts count).
function savePct(t_, len) {
  const daily = t_.lengths.day.usd * LENGTHS[len][1];
  return len === "day" || !(daily > 0) ? 0 : Math.max(0, Math.floor((1 - t_.lengths[len].usd / daily) * 100));
}
// A tier's price in the chosen currency: USD's own fields, or the page currency's converted ones.
function priceIn(l, currency) {
  return currency === "USD" ? { amount: l.usd, list_amount: l.list_usd } : { amount: l.amount, list_amount: l.list_amount };
}
// "60% of Claude Pro's weekly usage": compare_pct, worked out by the server, the same for every length.
function shareOfPlan(t_, len) {
  const what = bdi(t_.compare), n = t_.compare_pct / 100;
  // A whole number of plans reads as plans: "One week of Claude Pro", "One week of two Claude Pro plans".
  if (n >= 1 && Number.isInteger(n)) {
    const period = t(`home.len_${len}`);
    return n === 1 ? t("home.plan_one", { period, what }) : t("home.plan_n", { period, what, n: t("home.num_words").split(",")[n] || nf0.format(n) });
  }
  return t("home.share_of", { pct: nf0.format(t_.compare_pct), what, period: t(`home.usage_${len}`) });
}
// The plan the dashboard recommends, on its own length only; without one the second card stands out as before.
function isFeatured(d, t_, i, len) {
  return d.recommended ? d.recommended.tier === t_.tier && d.recommended.length === len : i === 1;
}
function card(t_, i, len, currency) {
  const l = t_.lengths[len];
  const rec = !!data.recommended && isFeatured(data, t_, i, len);
  const p = priceIn(l, currency);
  const off = l.discount_ends_at && l.list_usd > 0 ? Math.round((1 - l.usd / l.list_usd) * 100) : 0;
  const save = savePct(t_, len);
  return `<article class="tier${isFeatured(data, t_, i, len) ? " featured" : ""}${l.sold_out ? " sold-out" : ""}">
    ${off > 0 ? `<span class="ribbon">${t("home.off_pct", { n: nf0.format(off) })}</span>` : ""}
    <h3>${tierName(t_)}${rec ? ` <span class="rec">${t("home.recommended")}</span>` : ""}</h3>
    ${t_.compare_pct != null ? `<p class="share">${shareOfPlan(t_, len)}</p>` : ""}
    <p class="price">${l.discount_ends_at ? `<s>${esc(money(p.list_amount, currency))}</s> ` : ""}<b>${esc(money(p.amount, currency))}</b></p>
    <p class="per">${t(LENGTHS[len][0])}</p>
    ${l.discount_ends_at ? `<p class="countdown" data-ends="${l.discount_ends_at}">${t("home.offer_ends_in", { text: esc(countdown(l.discount_ends_at)) })}</p>` : ""}
    ${save > 0 ? `<p class="save">${t("home.save_pct", { n: nf0.format(save) })}</p>` : ""}
    ${l.sold_out ? `<span class="btn get" aria-disabled="true">${t("home.sold_out")}</span>`
      : `<button type="button" class="btn primary get" data-order="${esc(t_.tier)}:${len}">${t("home.get_it")}</button>`}
  </article>`;
}
// order=1: the buyer went on from the dialog, so the dashboard opens the order once they are in.
function signInHref(t_, len) { return `/dashboard?tier=${encodeURIComponent(t_.tier)}&length=${len}&order=1`; }
function cardsHtml(d, len, currency = d.currency) { return d.tiers.map((t_, i) => card(t_, i, len, currency)).join(""); }
// "Toman" for IRT (shared i18n key with the order dialog), "USD" otherwise translated, else the bare code.
function currencyLabel(c) { return c === "IRT" ? t("cur.IRT") : c === "USD" ? t("home.cur_usd") : c; }
// Built once the page's own currency is known: that currency and USD, deduplicated (same idiom as the order form's select).
function initCurrencySelect(d) {
  document.getElementById("currency-select").innerHTML = [...new Set([d.currency, "USD"])]
    .map((c) => `<option value="${esc(c)}"${c === currency ? " selected" : ""}>${esc(currencyLabel(c))}</option>`).join("");
}
function render() {
  document.getElementById("cards").innerHTML = cardsHtml(data, length, currency);
  document.querySelectorAll("#length-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.length === length)));
  document.getElementById("currency-select").value = currency;
  document.getElementById("rate-note").textContent = currency !== "USD" && data.rate_set_at
    ? t("home.rate_note", { date: new Date(data.rate_set_at * 1000).toLocaleDateString(LOC) }) : "";
}
function tick() {
  if (reloaded) return;
  let ended = null;
  document.querySelectorAll(".countdown").forEach((el) => {
    const ends = +el.dataset.ends;
    if (ends <= serverNow()) { el.textContent = t("price.offer_ended"); if (!reloadedFor(ends)) ended = ends; }
    else el.textContent = t("home.offer_ends_in", { text: countdown(ends) });
  });
  if (ended == null) return;
  reloaded = true; markReloaded(ended);
  // Not while someone is filling in an order: the reload would drop what they typed. The dialog shows the regular
  // price instead, and the cards get theirs back when it closes.
  if (orderDialog().open) { pendingEnd = ended; offerEnded(); return; }
  location.reload();   // the regular price returns by itself
}
// ---------- the order dialog ----------
// What the ticket costs and its terms; the order itself is placed from the dashboard, signed in.

let ordering = null;         // the tier and length in the open dialog
let pendingEnd = null;       // a discount that ended while the dialog was open: the page reloads once it closes
const orderDialog = () => document.getElementById("order-dialog");
orderDialog().addEventListener("close", () => { if (pendingEnd != null) location.reload(); });
// The open dialog's price becomes the regular one, which is what the server charges from now on.
function offerEnded() {
  if (!ordering) return;
  const dlg = orderDialog(), l = ordering.t.lengths[ordering.len], c = ordering.currency;
  const p = dlg.querySelector("#order-price");
  if (p) p.textContent = money(c === "USD" ? l.list_usd : l.list_amount, c);
}
function orderFormHtml(d, t_, len, c = currency || d.currency) {
  const l = t_.lengths[len];
  return `<h3 id="order-h">${t("ord.title", { what: t("ord.what", { label: tierName(t_), len: lengthName(len) }) })}</h3>
    <p class="order-price"><b id="order-price">${esc(money(c === "USD" ? l.usd : l.amount, c))}</b></p>
    <div class="terms"><ul class="muted"><li>${splitTerms(d.week_split, len, l.days) || t("ord.terms_l1")}</li><li>${t("ord.terms_l3")}</li></ul></div>
    <p class="muted">${t("home.sign_in_to_order")}</p>
    <p class="order-alt"><a class="btn primary" href="${esc(signInHref(t_, len))}">${t("home.continue_sign_in")}</a> <button type="button" class="btn" data-close>${t("app.cancel")}</button></p>`;
}
function openOrder(tierId, len) {
  const t_ = data.tiers.find((x) => x.tier === tierId);
  if (!t_ || !t_.lengths[len]) return;
  const dlg = orderDialog();
  ordering = { t: t_, len, currency: currency || data.currency };
  dlg.innerHTML = orderFormHtml(data, t_, len);
  dlg.showModal();
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
}
document.getElementById("cards").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-order]");
  if (!b || !data) return;
  const k = b.dataset.order.lastIndexOf(":");
  openOrder(b.dataset.order.slice(0, k), b.dataset.order.slice(k + 1));
});
document.getElementById("length-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-length]");
  if (!b || !data) return;
  length = b.dataset.length;
  render();
});
document.getElementById("currency-select").addEventListener("change", (e) => {
  if (!data) return;
  currency = e.target.value;
  render();
});
fetch("/api/pricing", { credentials: "omit" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }).then((d) => {
  data = d;
  currency = d.currency;   // default: the page's own currency (IRT today)
  if (d.recommended) length = d.recommended.length;   // open on the recommended plan's length
  if (typeof d.now === "number") skew = d.now - Date.now() / 1000;
  initCurrencySelect(d);
  render(); setInterval(tick, 1000);
})
  .catch(() => { document.getElementById("cards").innerHTML = `<p class="muted">${t("home.prices_unavailable")}</p>`; });
