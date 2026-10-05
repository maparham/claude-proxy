"use strict";
// The dashboard's words, in Persian (the default) and English. Loaded in <head> before the page paints, so the
// direction is right from the first frame; app.js asks t() for every string it shows (design 2026-10-05).

const I18N = {
  en: {
    "app.lang_other": "فارسی",
    "app.lang_title": "Switch language",
    "app.title": "Claude Gateway",
    "app.credential": "credential",
    "app.who_title": "Change your name",
    "app.pricing": "Pricing",
    "app.pricing_title": "The price list for tickets",
    "app.theme": "Theme",
    "app.theme_title": "Switch theme",
    "app.signout": "Sign out",
    "login.about_title": "Use Claude Code on your team’s Claude subscription.",
    "login.about_lede": "Sign in to connect a computer and see your own usage.",
    "login.auth_title": "Sign in to connect your computer.",
    "login.auth_lede": "The link you opened carries this code. After you sign in, you check that your terminal shows the same one, then approve it.",
    "login.n1": "1",
    "login.n2": "2",
    "login.n3": "3",
    "login.step1": "<b>Sign in</b> with Google, GitHub or a code sent to your email.",
    "login.step2": "<b>Run one command</b> in the computer’s terminal.",
    "login.step3": "<b>Approve the computer</b> when this page opens. Then start Claude Code with <code>gclaude</code>.",
    "login.os_label": "Operating system",
    "login.os_unix": "macOS / Linux",
    "login.os_windows": "Windows",
    "login.copy": "Copy command",
    "login.privacy": "Privacy",
    "login.pricing": "Pricing",
    "login.title": "Sign in",
    "login.signup_hint": "New here? The same buttons create your account.",
    "login.other_ways": "Other ways to sign in",
    "login.key_hint": "Sign in with your own gateway key to see your usage.",
    "login.about_keys": "About gateway keys",
    "login.admin_user": "Admin username",
    "login.password": "Password",
    "login.submit": "Sign in",
    "login.key_label": "Gateway key",
    "login.submit_key": "Sign in with key",
    "dur.s": "{n}s",
    "dur.m": "{n}m",
    "dur.h": "{n}h",
    "dur.d": "{n}d",
    "dur.join": "{a} {b}",
    "app.ago": "{d} ago",
    "app.never": "never",
    // en:end
  },
  fa: {
    "app.lang_other": "English",
    "app.lang_title": "تغییر زبان",
    "app.title": "درگاه Claude",
    "app.credential": "اعتبارنامه",
    "app.who_title": "تغییر نام",
    "app.pricing": "تعرفه‌ها",
    "app.pricing_title": "فهرست قیمت تیکت‌ها",
    "app.theme": "پوسته",
    "app.theme_title": "تغییر پوسته",
    "app.signout": "خروج",
    "login.about_title": "Claude Code را با اشتراک Claude تیم‌تان به کار ببرید.",
    "login.about_lede": "وارد شوید تا رایانه‌ای را متصل کنید و مصرف خودتان را ببینید.",
    "login.auth_title": "برای اتصال رایانه‌تان وارد شوید.",
    "login.auth_lede": "پیوندی که باز کردید این کد را همراه دارد. پس از ورود، بررسی کنید که ترمینال‌تان همین کد را نشان می‌دهد و سپس آن را تأیید کنید.",
    "login.n1": "۱",
    "login.n2": "۲",
    "login.n3": "۳",
    "login.step1": "با Google، GitHub یا کدی که به ایمیل‌تان فرستاده می‌شود <b>وارد شوید</b>.",
    "login.step2": "در ترمینال رایانه <b>یک فرمان اجرا کنید</b>.",
    "login.step3": "وقتی این صفحه باز شد، <b>رایانه را تأیید کنید</b>. سپس Claude Code را با <code>gclaude</code> اجرا کنید.",
    "login.os_label": "سیستم‌عامل",
    "login.os_unix": "macOS / Linux",
    "login.os_windows": "Windows",
    "login.copy": "کپی فرمان",
    "login.privacy": "حریم خصوصی",
    "login.pricing": "تعرفه‌ها",
    "login.title": "ورود",
    "login.signup_hint": "تازه‌وارد هستید؟ همین دکمه‌ها حساب‌تان را می‌سازند.",
    "login.other_ways": "راه‌های دیگر ورود",
    "login.key_hint": "برای دیدن مصرف‌تان با کلید درگاه خودتان وارد شوید.",
    "login.about_keys": "دربارهٔ کلیدهای درگاه",
    "login.admin_user": "نام کاربری مدیر",
    "login.password": "گذرواژه",
    "login.submit": "ورود",
    "login.key_label": "کلید درگاه",
    "login.submit_key": "ورود با کلید",
    "dur.s": "{n} ثانیه",
    "dur.m": "{n} دقیقه",
    "dur.h": "{n} ساعت",
    "dur.d": "{n} روز",
    "dur.join": "{a} و {b}",
    "app.ago": "{d} پیش",
    "app.never": "هرگز",
    // fa:end
  },
};

// Persian unless this browser picked English.
let LANG = "fa";
try { if (localStorage.getItem("cp-lang") === "en") LANG = "en"; } catch { /* private mode: the default */ }
const LOC = LANG === "fa" ? "fa-IR" : "en-US";
if (typeof document !== "undefined") {
  document.documentElement.lang = LANG;
  document.documentElement.dir = LANG === "fa" ? "rtl" : "ltr";
}

// The active language's text, else English's, else the key itself; {name} filled from vars, unescaped (callers pass
// markup-safe values, as the template literals did).
function t(key, vars) {
  let s = I18N[LANG][key] ?? I18N.en[key];
  if (s === undefined) { if (typeof console !== "undefined") console.warn(`i18n: missing ${key}`); return key; }
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : s;
}
function plural(key, n, vars = {}) { return t(`${key}.${n === 1 ? "one" : "other"}`, { n, ...vars }); }

// data-i18n="key" sets an element's innerHTML (dictionary markup is trusted); data-i18n-attr="placeholder:key;title:key"
// sets attributes. The English in index.html is what shows without JS.
function applyStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.innerHTML = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => el.dataset.i18nAttr.split(";").forEach((p) => {
    const [attr, key] = p.split(":"); el.setAttribute(attr, t(key));
  }));
}

// A reload rebuilds formatters, charts and Clerk's widget in the new language; the address keeps the open tab.
function setLang(lang) {
  try { localStorage.setItem("cp-lang", lang); } catch { /* private mode: this page only */ }
  location.reload();
}

// Server messages (web.py's fail()) are English; the known ones read in Persian. Exact texts first, then the
// messages built from values, whose groups fill {1}, {2} of the Persian.
const ERR_FA = {};
const ERR_PATTERNS = [];
function errMsg(text) {
  if (LANG !== "fa" || !text) return text;
  if (ERR_FA[text]) return ERR_FA[text];
  for (const [re, fa] of ERR_PATTERNS) {
    const m = re.exec(text);
    if (m) return fa.replace(/\{(\d)\}/g, (_, i) => m[+i] ?? "");
  }
  return text;
}
