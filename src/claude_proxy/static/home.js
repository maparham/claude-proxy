"use strict";
// The public home page: one card per tier for the chosen length, discounts with a live countdown, usage hints and
// sold-out lengths. Everything comes from /api/pricing; the page computes nothing about the account itself.
// With Turnstile configured (pricing's turnstile_site_key), "Get it" opens an order dialog instead of linking to the dashboard.
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
function card(t, i, len, currency, ordering) {
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
      : ordering ? `<button type="button" class="btn primary get" data-order="${esc(t.tier)}:${len}">Get it</button>`
      : `<a class="btn primary get" href="${signInHref(t, len)}">Get it</a>`}
  </article>`;
}
function signInHref(t, len) { return `/dashboard?tier=${encodeURIComponent(t.tier)}&length=${len}`; }
function cardsHtml(d, len) { return d.tiers.map((t, i) => card(t, i, len, d.currency, !!d.turnstile_site_key)).join(""); }
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
  if (ended == null) return;
  reloaded = true; markReloaded(ended);
  // Not while someone is filling in an order: the reload would drop what they typed. The dialog shows the regular
  // price instead, and the cards get theirs back when it closes.
  if (orderDialog().open) { pendingEnd = ended; offerEnded(); return; }
  location.reload();   // the regular price returns by itself
}
// ---------- the order dialog (design 2026-10-04, section 2) ----------

const TURNSTILE_JS = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
const LENGTH_NAME = { day: "1 day", week: "1 week", month: "1 month" };
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
  if (hint) hint.textContent = `The offer ended while you were ordering; this is the regular price. ${hint.textContent}`;
}
// After a sent order. Only a queued confirmation is promised: there is none without [email] or past the buyer cap.
function receivedHtml(email, confirmation) {
  return `<h3>Order received</h3><p>The admin will contact you at <b>${esc(email)}</b>.${confirmation ? " A confirmation email is on its way." : ""}</p>
    <p><button type="button" class="btn primary" data-close>Close</button></p>`;
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
function orderFormHtml(d, t, len) {
  const l = t.lengths[len];
  const price = (c) => money(c === "USD" ? l.usd : l.amount, c);
  return `<h3 id="order-h">Order ${esc(t.label)}, ${LENGTH_NAME[len]}</h3>
    <p class="order-price"><b id="order-price">${esc(price(d.currency))}</b> <span class="muted">at today’s rate</span></p>
    <form class="form-grid order-form" novalidate>
      <label>Name<input type="text" name="name" required maxlength="100" autocomplete="name"></label>
      <label>Email<input type="email" name="email" required maxlength="254" autocomplete="email"></label>
      <label>Currency<select name="currency">${[...new Set([d.currency, "USD"])].map((c) =>
        `<option value="${esc(c)}"${c === d.currency ? " selected" : ""} data-price="${esc(price(c))}">${esc(c)}</option>`).join("")}</select></label>
      <label class="wide">Message (optional)<textarea name="message" maxlength="1000" rows="3"></textarea></label>
      <div class="turnstile wide"></div>
      <p class="hint">This is a request, not a payment. The admin will contact you with payment details.</p>
      <button class="btn primary" type="submit">Send order</button>
    </form>
    <div class="error" role="alert"></div>
    <p class="order-alt"><a href="${esc(signInHref(t, len))}">Or sign in to order</a> <button type="button" class="btn" data-close>Cancel</button></p>`;
}
function dropWidget() {
  if (widget != null) { try { window.turnstile.remove(widget); } catch { /* already gone with its container */ } }
  widget = null;
}
function openOrder(tierId, len) {
  const t = data.tiers.find((x) => x.tier === tierId);
  if (!t || !t.lengths[len]) return;
  const dlg = orderDialog();
  dropWidget();
  ordering = { t, len };
  dlg.innerHTML = orderFormHtml(data, t, len);
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
  }).catch(() => { err.textContent = "The verification could not load. Sign in to order instead."; });
  f.onsubmit = async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (!f.reportValidity()) return;
    if (!token) { err.textContent = "Please wait for the verification to finish."; return; }
    go.disabled = true;
    try {
      let r;
      try {
        r = await fetch("/api/orders", { method: "POST", credentials: "omit", headers: { "content-type": "application/json" },
          body: JSON.stringify({ tier: t.tier, length: len, currency: f.currency.value, name: f.name.value, email: f.email.value,
                                 message: f.message.value, turnstile_token: token }) });
      } catch { throw new Error("The order could not be sent; check your connection and try again."); }
      let body = null;
      try { body = await r.json(); } catch { /* not JSON */ }
      if (!r.ok) throw new Error((body && body.error) || `The order could not be sent (HTTP ${r.status}).`);
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
  document.getElementById("how-to-buy").textContent = d.how_to_buy || "Ask the gateway admin.";
  render(); setInterval(tick, 1000);
})
  .catch(() => {
    document.getElementById("cards").innerHTML = `<p class="muted">Prices are not available right now.</p>`;
    document.getElementById("how-to-buy").textContent = "Ask the gateway admin.";
  });
