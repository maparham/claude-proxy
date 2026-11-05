"""Telegram bot design, section 2: the [telegram] section and its environment secrets."""
import pytest

from claude_proxy.config import Config, ConfigError, TelegramConfig


def write(tmp_path, body):
    p = tmp_path / "c.toml"
    p.write_text(body)
    return str(p)


def test_no_section_is_none(tmp_path):
    assert Config.load(write(tmp_path, "")).telegram is None


def test_section_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    cfg = Config.load(write(tmp_path, '[telegram]\nbot_username = "rahkar_bot"\nadmin_chat_id = -100123\n'))
    assert cfg.telegram == TelegramConfig(bot_username="rahkar_bot", admin_chat_id=-100123)
    assert (cfg.telegram.token(), cfg.telegram.webhook_secret()) == ("123:abc", "s3cret")


def test_secrets_missing_read_none(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    t = TelegramConfig(bot_username="b", admin_chat_id=-1)
    assert (t.token(), t.webhook_secret()) == (None, None)


@pytest.mark.parametrize("body", [
    '[telegram]\nbot_username = "b"\nadmin_chat_id = -1\nbot_token = "x"\n',   # a secret in the file: refused
    '[telegram]\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = ""\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = "@b"\nadmin_chat_id = -1\n',
    '[telegram]\nbot_username = "b"\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = 0\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = true\n',
    '[telegram]\nbot_username = "b"\nadmin_chat_id = "-1"\n',
])
def test_bad_section_refused(tmp_path, body):
    with pytest.raises(ConfigError, match=r"\[telegram\]"):
        Config.load(write(tmp_path, body))
