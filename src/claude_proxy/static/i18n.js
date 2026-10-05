"use strict";
// The dashboard's words, in Persian (the default) and English. Loaded in <head> before the page paints, so the
// direction is right from the first frame; app.js asks t() for every string it shows (design 2026-10-05).

const I18N = {
  en: {
    "app.lang_other": "فارسی",
    "app.lang_title": "Switch language",
  },
  fa: {
    "app.lang_other": "English",
    "app.lang_title": "تغییر زبان",
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
