// Bilingual (i18n.js, loaded first): Persian by default, English on request; applyStatic()/t() give the page's own text.
applyStatic();
document.getElementById("lang-toggle").onclick = () => setLang(LANG === "fa" ? "en" : "fa");

// The retention period is configurable, so it's templated rather than baked into the dictionary; the server writes
// the real value into this meta tag (and, for the no-JS fallback, into the paragraph below) at the same time.
const days = +document.querySelector('meta[name="retention-days"]').content;
document.getElementById("privacy-retention").innerHTML = t("privacy.retention", { days });
