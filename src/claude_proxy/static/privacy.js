// Bilingual (i18n.js, loaded first): Persian by default, English on request; applyStatic()/t() give the page's own text.
applyStatic();
document.title = t("privacy.title");
document.getElementById("lang-toggle").onclick = () => setLang(LANG === "fa" ? "en" : "fa");

// The retention period is configurable: the server writes it into this meta tag (and into the paragraph's no-JS
// English), and it's shown in the reader's digits.
const days = new Intl.NumberFormat(LOC).format(+document.querySelector('meta[name="retention-days"]').content);
document.getElementById("privacy-retention").innerHTML = t("privacy.retention", { days });
