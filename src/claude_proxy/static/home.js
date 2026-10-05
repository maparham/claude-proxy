"use strict";
// The public home page: one card per tier for the chosen length, discounts with a live countdown, usage hints and
// sold-out lengths. Everything comes from /api/pricing; the page computes nothing about the account itself.
// With Turnstile configured (pricing's turnstile_site_key), "Get it" opens an order dialog instead of linking to the dashboard.
// Bilingual (i18n.js, loaded first): Persian by default, English on request; applyStatic()/t() give the page's own text.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const bdi = (v) => `<bdi>${esc(v)}</bdi>`;   // a tier's own name or the admin's "how to pay" text, inside translated copy
const LENGTHS = { day: ["home.per_day", 1], week: ["home.per_week", 7], month: ["home.per_month", 30] };
function lengthName(len) { return t(`price.1_${len}`); }   // "1 day" / "1 week" / "1 month", shared with the dashboard's price list
const FAMILY = { sonnet: "Sonnet", opus: "Opus" };
const nf0 = new Intl.NumberFormat(LOC, { useGrouping: false });
const nf2 = new Intl.NumberFormat(LOC, { minimumIntegerDigits: 2, useGrouping: false });
const nfShare = new Intl.NumberFormat(LOC, { maximumFractionDigits: 2 });
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
try { const th = localStorage.getItem("cp-theme"); if (th) document.documentElement.dataset.theme = th; } catch { /* the dashboard's theme choice, if any */ }
applyStatic();
document.getElementById("lang-toggle").onclick = () => setLang(LANG === "fa" ? "en" : "fa");

function money(amount, currency) {
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
// "Sonnet: at least 4 h per 5-hour window, 2.8 h per day"; a missing number is left out, a family with none is omitted.
function hintLines(t_) {
  return Object.entries(FAMILY).map(([fam, name]) => {
    const h = (t_.hours || {})[fam] || {};
    const parts = [h.per_5h != null ? t("home.h_per_5h", { n: nf0.format(h.per_5h) }) : null, h.per_day != null ? t("home.h_per_day", { n: nf0.format(h.per_day) }) : null].filter(Boolean);
    return parts.length ? t("home.at_least", { model: name, what: parts.join(t("app.list_sep")) }) : null;   // Sonnet/Opus: fixed brand names, not server data
  }).filter(Boolean);
}
// What a week or a month saves against buying the day ticket every day, on the charged prices (discounts count).
function savePct(t_, len) {
  const daily = t_.lengths.day.usd * LENGTHS[len][1];
  return len === "day" || !(daily > 0) ? 0 : Math.max(0, Math.floor((1 - t_.lengths[len].usd / daily) * 100));
}
function card(t_, i, len, currency, ordering) {
  const l = t_.lengths[len];
  const off = l.discount_ends_at && l.list_usd > 0 ? Math.round((1 - l.usd / l.list_usd) * 100) : 0;
  const save = savePct(t_, len);
  return `<article class="tier${i === 1 ? " featured" : ""}${l.sold_out ? " sold-out" : ""}">
    ${off > 0 ? `<span class="ribbon">${t("home.off_pct", { n: nf0.format(off) })}</span>` : ""}
    <h3>${bdi(t_.label)}</h3>
    ${t_.compare ? `<p class="compare">${t("home.compare", { what: bdi(t_.compare) })}</p>` : ""}
    <p class="share">${t("home.share_of", { pct: nfShare.format(+t_.share_pct) })}</p>
    <p class="price">${l.discount_ends_at ? `<s>${esc(money(l.list_amount, currency))}</s> ` : ""}<b>${esc(money(l.amount, currency))}</b><span class="unit">${t(LENGTHS[len][0])}</span></p>
    ${l.discount_ends_at ? `<p class="countdown" data-ends="${l.discount_ends_at}">${t("home.offer_ends_in", { text: esc(countdown(l.discount_ends_at)) })}</p>` : ""}
    ${save > 0 ? `<p class="save">${t("home.save_pct", { n: nf0.format(save) })}</p>` : ""}
    <ul class="hints">${hintLines(t_).map((h) => `<li>${h}</li>`).join("")}</ul>
    ${l.sold_out ? `<span class="btn get" aria-disabled="true">${t("home.sold_out")}</span>`
      : ordering ? `<button type="button" class="btn primary get" data-order="${esc(t_.tier)}:${len}">${t("home.get_it")}</button>`
      : `<a class="btn primary get" href="${signInHref(t_, len)}">${t("home.get_it")}</a>`}
  </article>`;
}
function signInHref(t_, len) { return `/dashboard?tier=${encodeURIComponent(t_.tier)}&length=${len}`; }
function cardsHtml(d, len) { return d.tiers.map((t_, i) => card(t_, i, len, d.currency, !!d.turnstile_site_key)).join(""); }
function render() {
  document.getElementById("cards").innerHTML = cardsHtml(data, length);
  document.querySelectorAll("#length-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.length === length)));
  document.getElementById("rate-note").textContent = data.rate_set_at
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
// ---------- the order dialog (design 2026-10-04, section 2) ----------

const TURNSTILE_JS = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
let turnstileReady = null;   // loaded when the dialog first opens, never for a visitor who only reads the page
let widget = null;
let ordering = null;         // the tier and length in the open dialog
let pendingEnd = null;       // a discount that ended while the dialog was open: the page reloads once it closes
const orderDialog = () => document.getElementById("order-dialog");
orderDialog().addEventListener("close", () => { if (pendingEnd != null) location.reload(); });
// The open dialog keeps its form; its price becomes the regular one, which is what the server charges from now on.
function offerEnded() {
  if (!ordering) return;
  const dlg = orderDialog(), l = ordering.t.lengths[ordering.len];
  const price = (c) => money(c === "USD" ? l.list_usd : l.list_amount, c);
  dlg.querySelectorAll("option[data-price]").forEach((o) => { o.dataset.price = price(o.value); });
  const sel = dlg.querySelector("select[name=currency]"), p = dlg.querySelector("#order-price"), hint = dlg.querySelector(".hint");
  if (sel && p) p.textContent = price(sel.value);
  if (hint) hint.textContent = t("home.offer_ended_mid", { rest: hint.textContent });
}
// After a sent order. Only a queued confirmation is promised: there is none without [email] or past the buyer cap.
function receivedHtml(email, confirmation) {
  return `<h3>${t("home.order_received")}</h3><p>${t("home.admin_contact", { email: `<b>${bdi(email)}</b>` })}${confirmation ? ` ${t("home.confirmation_coming")}` : ""}</p>
    <p><button type="button" class="btn primary" data-close>${t("app.close")}</button></p>`;
}
function loadTurnstile() {
  turnstileReady ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = TURNSTILE_JS; s.async = true;
    s.onload = () => resolve(window.turnstile);
    s.onerror = () => { turnstileReady = null; reject(new Error("Turnstile did not load")); };   // the next open tries again
    document.head.appendChild(s);
  });
  return turnstileReady;
}
// What a visitor can order in: the page's currency and USD, which always has a rate.
function orderFormHtml(d, t_, len) {
  const l = t_.lengths[len];
  const price = (c) => money(c === "USD" ? l.usd : l.amount, c);
  return `<h3 id="order-h">${t("ord.title", { what: t("ord.what", { label: bdi(t_.label), len: lengthName(len) }) })}</h3>
    <p class="order-price"><b id="order-price">${esc(price(d.currency))}</b> <span class="muted">${t("ord.todays_rate")}</span></p>
    <form class="form-grid order-form" novalidate>
      <label>${t("home.name")}<input type="text" name="name" required maxlength="100" autocomplete="name"></label>
      <label>${t("ord.email")}<input type="email" name="email" dir="ltr" required maxlength="254" autocomplete="email"></label>
      <label>${t("ord.currency")}<select name="currency">${[...new Set([d.currency, "USD"])].map((c) =>
        `<option value="${esc(c)}"${c === d.currency ? " selected" : ""} data-price="${esc(price(c))}">${esc(c)}</option>`).join("")}</select></label>
      <label class="wide">${t("ord.message")}<textarea name="message" maxlength="1000" rows="3"></textarea></label>
      <div class="turnstile wide"></div>
      <p class="hint">${t("ord.not_payment")}</p>
      <button class="btn primary" type="submit">${t("ord.send")}</button>
    </form>
    <div class="error" role="alert"></div>
    <p class="order-alt"><a href="${esc(signInHref(t_, len))}">${t("home.sign_in_instead")}</a> <button type="button" class="btn" data-close>${t("app.cancel")}</button></p>`;
}
function dropWidget() {
  if (widget != null) { try { window.turnstile.remove(widget); } catch { /* already gone with its container */ } }
  widget = null;
}
function openOrder(tierId, len) {
  const t_ = data.tiers.find((x) => x.tier === tierId);
  if (!t_ || !t_.lengths[len]) return;
  const dlg = orderDialog();
  dropWidget();
  ordering = { t: t_, len };
  dlg.innerHTML = orderFormHtml(data, t_, len);
  dlg.showModal();
  const f = dlg.querySelector("form"), err = dlg.querySelector(".error"), go = f.querySelector('button[type="submit"]');
  dlg.querySelector("[data-close]").onclick = () => dlg.close();
  f.currency.onchange = () => { dlg.querySelector("#order-price").textContent = f.currency.selectedOptions[0].dataset.price; };
  let token = "";
  loadTurnstile().then((ts) => {
    if (!dlg.open || !dlg.contains(f)) return;
    const narrowDialog = typeof matchMedia === "function" && matchMedia("(max-width: 400px)").matches;
    widget = ts.render(dlg.querySelector(".turnstile"), {
      sitekey: data.turnstile_site_key, theme: document.documentElement.dataset.theme || "auto", size: narrowDialog ? "compact" : "flexible",
      callback: (v) => { token = v; }, "expired-callback": () => { token = ""; }, "error-callback": () => { token = ""; },
    });
  }).catch(() => { err.textContent = t("home.verify_could_not_load"); });
  f.onsubmit = async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (!f.reportValidity()) return;
    if (!token) { err.textContent = t("home.wait_for_verification"); return; }
    go.disabled = true;
    try {
      let r;
      try {
        r = await fetch("/api/orders", { method: "POST", credentials: "omit", headers: { "content-type": "application/json" },
          body: JSON.stringify({ tier: t_.tier, length: len, currency: f.currency.value, name: f.name.value, email: f.email.value,
                                 message: f.message.value, turnstile_token: token }) });
      } catch { throw new Error(t("home.order_not_sent")); }
      let body = null;
      try { body = await r.json(); } catch { /* not JSON */ }
      if (!r.ok) throw new Error((body && body.error && errMsg(body.error)) || t("home.order_not_sent_status", { status: r.status }));
      dropWidget();
      dlg.innerHTML = receivedHtml(f.email.value, !!(body && body.confirmation));
      dlg.querySelector("[data-close]").onclick = () => dlg.close();
    } catch (x) {
      err.textContent = x.message;
      go.disabled = false;
      token = "";   // a token works once: the widget gives a new one
      try { window.turnstile.reset(widget); } catch { /* not rendered */ }
    }
  };
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
fetch("/api/pricing", { credentials: "omit" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }).then((d) => {
  data = d;
  if (typeof d.now === "number") skew = d.now - Date.now() / 1000;
  // The admin's own free-text answer (any language they typed it in) stays isolated, like other admin-written notes.
  document.getElementById("how-to-buy").innerHTML = d.how_to_buy ? bdi(d.how_to_buy) : t("ov.ask_admin");
  render(); setInterval(tick, 1000);
})
  .catch(() => {
    document.getElementById("cards").innerHTML = `<p class="muted">${t("home.prices_unavailable")}</p>`;
    document.getElementById("how-to-buy").innerHTML = t("ov.ask_admin");
  });
