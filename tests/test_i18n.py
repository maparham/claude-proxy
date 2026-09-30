"""scripts/i18n.json: gclaude's own text in English and Persian (gclaude bilingual design, "Strings")."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scripts" / "i18n.json"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_every_key_has_english_and_persian():
    for key, texts in catalog().items():
        assert set(texts) == {"en", "fa"}, key
        assert texts["en"].strip() and texts["fa"].strip(), key


def test_persian_uses_the_same_placeholders_as_english():
    for key, texts in catalog().items():
        assert sorted(PLACEHOLDER.findall(texts["fa"])) == sorted(PLACEHOLDER.findall(texts["en"])), key


def test_the_persian_is_persian():
    """Catches an English text copied into fa and never translated."""
    for key, texts in catalog().items():
        assert re.search("[؀-ۿ]", texts["fa"]), key


def test_the_windows_copy_is_the_same():
    assert (ROOT / "scripts" / "windows" / "i18n.json").read_bytes() == CATALOG.read_bytes()


def test_launcher_text_is_safe_to_quote_in_sh():
    """The launcher puts these in single quotes and splices "$why" in: none may carry sh's special characters."""
    for key, texts in catalog().items():
        if key.startswith("launch."):
            for text in texts.values():
                assert not set(text) & set('"$`\\\n'), key
