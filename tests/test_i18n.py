"""The dashboard's dictionaries (static/i18n.js): both languages complete, and every key the page uses defined."""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "claude_proxy" / "static"

DUMP = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { localStorage: { getItem: () => process.argv[3] || null, setItem() {} }, console };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8") + "\n;this.out = { I18N, LANG, LOC, ERR_FA, ERR_PATTERNS: ERR_PATTERNS.map(([r, k]) => [r.source, k]) };", ctx);
const probe = process.argv[4];
if (probe) vm.runInNewContext(probe, ctx);
console.log(JSON.stringify(ctx.out));
"""


def node(tmp_path, lang=None, probe=""):
    if not shutil.which("node"):
        pytest.skip("no node here")
    h = tmp_path / "dump.js"
    h.write_text(DUMP)
    return json.loads(subprocess.run(["node", str(h), str(STATIC / "i18n.js"), lang or "", probe],
                                     capture_output=True, text=True, timeout=30, check=True).stdout)


def test_persian_is_the_default_and_english_is_remembered(tmp_path):
    assert node(tmp_path)["LANG"] == "fa" and node(tmp_path)["LOC"] == "fa-IR"
    assert node(tmp_path, "en")["LANG"] == "en" and node(tmp_path, "en")["LOC"] == "en-US"
    assert node(tmp_path, "xx")["LANG"] == "fa"   # junk in storage: the default


def test_both_languages_have_the_same_keys_and_placeholders(tmp_path):
    d = node(tmp_path)["I18N"]
    assert set(d["en"]) == set(d["fa"])
    ph = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))
    bad = [k for k in d["en"] if ph(d["en"][k]) != ph(d["fa"][k])]
    assert bad == []


def test_persian_uses_persian_letters(tmp_path):
    d = node(tmp_path)
    text = "".join(d["I18N"]["fa"].values()) + "".join(d["ERR_FA"].values())
    assert "ي" not in text and "ك" not in text


def test_t_falls_back_to_english_then_the_key(tmp_path):
    out = node(tmp_path, "fa", 'I18N.en["x.only_en"] = "Hi {name}";'
               ' I18N.en["x.n.one"] = "{n} thing"; I18N.en["x.n.other"] = "{n} things"; I18N.fa["x.n.one"] = I18N.fa["x.n.other"] = "{n} چیز";'
               ' this.out = [t("x.only_en", {name: "a"}), t("x.nowhere"), plural("x.n", 1), plural("x.n", 2)];')
    assert out == ["Hi a", "x.nowhere", "1 چیز", "2 چیز"]


def _used_keys():
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    keys = set(re.findall(r"""\bt\(\s*"([a-z][\w.:-]*)\"""", js))
    plurals = set(re.findall(r"""\bplural\(\s*"([a-z][\w.:-]*)\"""", js))
    keys |= set(re.findall(r'data-i18n="([^"]+)"', html))
    for spec in re.findall(r'data-i18n-attr="([^"]+)"', html):
        keys |= {part.split(":", 1)[1] for part in spec.split(";")}
    return keys, plurals


def test_every_key_the_page_uses_is_defined(tmp_path):
    en = node(tmp_path)["I18N"]["en"]
    keys, plurals = _used_keys()
    assert sorted(k for k in keys if k not in en) == []
    assert sorted(k for k in plurals if f"{k}.one" not in en or f"{k}.other" not in en) == []


def test_app_js_holds_no_persian():
    assert not re.search(r"[؀-ۿ]", (STATIC / "app.js").read_text())


def test_the_page_offers_the_switch_and_marks_its_text():
    html = (STATIC / "index.html").read_text()
    assert 'id="lang-toggle"' in html and 'id="lang-signin"' in html
    assert html.count("data-i18n") >= 30


def test_css_has_no_physical_sides_outside_the_tooltip():
    css = (STATIC / "app.css").read_text()
    lines = [l for l in css.splitlines() if re.search(r"\b(margin|padding|border)-(left|right)\b|text-align:\s*(left|right)|(^|[;{\s])(left|right):", l)
             and not l.lstrip().startswith("#tip")]
    assert lines == []


FMT = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const app = fs.readFileSync(process.argv[2], "utf8");
const i18n = fs.readFileSync(path.join(path.dirname(process.argv[2]), "i18n.js"), "utf8");
const grab = (re) => { const m = re.exec(app); if (!m) throw new Error("not found: " + re); return m[0]; };
const block = grab(/^\/\/ i18n-formatters:start[\s\S]*?^\/\/ i18n-formatters:end$/m);
const ctx = { console, Intl, Date, Math, Number, String, localStorage: { getItem: () => process.argv[3] } };
vm.runInNewContext(i18n + "\n" + block + "\n;this.out = [fmtNum(24900000), fmtUsd(12.5), fmtDur(9000), num('۲۵'), num('٣٫5'), bdi('<a>'), num('۱٬۲۳۴٬۵۶۷'), num('1,234.5')];", ctx);
console.log(JSON.stringify(ctx.out));
"""


def _fmt(tmp_path, lang):
    if not shutil.which("node"):
        pytest.skip("no node here")
    h = tmp_path / "fmt.js"
    h.write_text(FMT)
    return json.loads(subprocess.run(["node", str(h), str(STATIC / "app.js"), lang], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_formatters_follow_the_language(tmp_path):
    assert _fmt(tmp_path, "en") == ["24.9M", "$12.50", "2h 30m", 25, 3.5, "<bdi>&lt;a&gt;</bdi>", 1234567, 1234.5]   # grouped, as the page shows them
    fa = _fmt(tmp_path, "fa")
    assert fa[1] == "$۱۲٫۵۰" and fa[2] == "۲ ساعت و ۳۰ دقیقه" and fa[3] == 25 and fa[4] == 3.5
    assert re.search(r"[۰-۹]", fa[0])


def _fail_messages():
    exact, built = set(), []
    for py in (STATIC.parent).glob("*.py"):
        for node_ in ast.walk(ast.parse(py.read_text())):
            if isinstance(node_, ast.Call) and getattr(node_.func, "id", None) == "fail" and len(node_.args) > 1:
                a = node_.args[1]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value[:1].isupper():
                    exact.add(a.value)
                elif isinstance(a, ast.JoinedStr):
                    built.append("".join(v.value if isinstance(v, ast.Constant) else "X1" for v in a.values))
    return exact, built


def test_every_server_message_reads_in_persian(tmp_path):
    d = node(tmp_path)
    exact, built = _fail_messages()
    assert sorted(m for m in exact if m not in d["ERR_FA"]) == []
    pats = [re.compile(src) for src, _ in d["ERR_PATTERNS"]]
    assert [m for m in built if not any(p.search(m) for p in pats)] == []


def test_an_unknown_message_shows_as_sent(tmp_path):
    out = node(tmp_path, "fa", 'this.out = [errMsg("Brand new message."), errMsg(""), errMsg(undefined)];')
    assert out == ["Brand new message.", "", None]


def test_clerk_persian_is_vendored_and_loaded_only_for_persian():
    vend = (STATIC / "clerk-fa-IR.js").read_text()
    assert "export { faIR }" in vend and "@clerk/localizations@4.21.2" in vend.splitlines()[0]
    js = (STATIC / "app.js").read_text()
    assert 'import("/static/clerk-fa-IR.js")' in js and "localization" in js


def test_a_server_value_without_a_label_reads_as_itself(tmp_path):
    from tests.test_tickets_web import _app_fn
    out = _app_fn(tmp_path, ["labelOf", "limitLabel"], 'var out = [limitLabel({ kind: "share_30d", scope: "*" }), limitLabel({ kind: "share_day", scope: "*" }), labelOf("role.", "owner")];')
    assert out == ["share 30d", "share day", "owner"]




def test_the_persian_font_never_blocks_the_page():
    html = (STATIC / "index.html").read_text()
    assert "fonts.googleapis.com" not in html   # a slow or blocked Google must not hold the first paint
    assert "fonts.googleapis.com/css2?family=Vazirmatn" in (STATIC / "i18n.js").read_text()


def test_pages_are_read_as_utf8_on_any_system():
    # Windows defaults read_text() to cp1252, which can't decode the Persian in index.html: every page read names UTF-8.
    src = (STATIC.parent / "web.py").read_text(encoding="utf-8")
    assert re.findall(r"\.read_text\(\)", src) == []
