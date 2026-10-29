from pathlib import Path

import yaml

from models.BotConfigurator import BotConfigurator


def test_validate_requires_token_and_super_user(tmp_path: Path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text("bot: {}\n", encoding="utf-8")
    original = BotConfigurator.config_file
    try:
        BotConfigurator.config_file = str(config_file)
        assert BotConfigurator().validate() is False
    finally:
        BotConfigurator.config_file = original


def test_safe_round_trip_preserves_unicode(tmp_path: Path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text(
        yaml.safe_dump({"bot": {"token": "token", "super_user": 7}}),
        encoding="utf-8",
    )
    original = BotConfigurator.config_file
    try:
        BotConfigurator.config_file = str(config_file)
        configurator = BotConfigurator()
        configurator.set("bot.label", "Київ")
        assert (
            yaml.safe_load(config_file.read_text(encoding="utf-8"))["bot"]["label"]
            == "Київ"
        )
    finally:
        BotConfigurator.config_file = original


def test_runtime_secrets_are_not_persisted(tmp_path: Path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text("bot: {}\n", encoding="utf-8")
    original_file = BotConfigurator.config_file
    original_overrides = BotConfigurator._runtime_overrides
    try:
        BotConfigurator.config_file = str(config_file)
        BotConfigurator._runtime_overrides = {"bot.token": "runtime-secret"}
        assert BotConfigurator().get("bot.token") == "runtime-secret"
        assert "runtime-secret" not in config_file.read_text(encoding="utf-8")
    finally:
        BotConfigurator.config_file = original_file
        BotConfigurator._runtime_overrides = original_overrides


def test_add_user_does_not_persist_runtime_secret(tmp_path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text("bot: {}\n", encoding="utf-8")
    original_file = BotConfigurator.config_file
    original_overrides = BotConfigurator._runtime_overrides
    try:
        BotConfigurator.config_file = str(config_file)
        BotConfigurator._runtime_overrides = {"bot.token": "runtime-secret"}
        configurator = BotConfigurator()
        configurator.add_user(42)
        stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert 42 in stored["bot"]["allowed_users"]
        assert "runtime-secret" not in config_file.read_text(encoding="utf-8")
    finally:
        BotConfigurator.config_file = original_file
        BotConfigurator._runtime_overrides = original_overrides


def test_config_reloads_after_other_instance_saves(tmp_path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text(
        yaml.safe_dump({"bot": {"token": "token", "super_user": 7, "allowed_users": []}}),
        encoding="utf-8",
    )
    original_file = BotConfigurator.config_file
    try:
        BotConfigurator.config_file = str(config_file)
        first = BotConfigurator()
        second = BotConfigurator()
        assert second.get("bot.allowed_users") == []
        first.add_user(42)
        assert 42 in second.get("bot.allowed_users")
    finally:
        BotConfigurator.config_file = original_file


def test_save_before_loading_clears_overridden_disk_secrets(tmp_path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text(
        "bot:\n  token: old-test-token\n  label: keep\n"
        "transmission:\n  password: old-test-password\n",
        encoding="utf-8",
    )
    original_file = BotConfigurator.config_file
    original_overrides = BotConfigurator._runtime_overrides
    try:
        BotConfigurator.config_file = str(config_file)
        BotConfigurator._runtime_overrides = {
            "bot.token": "runtime-test-token",
            "transmission.password": "runtime-test-password",
        }
        configurator = BotConfigurator()
        configurator.save_config()
        stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert stored["bot"]["token"] is None
        assert stored["transmission"]["password"] is None
        assert stored["bot"]["label"] == "keep"
        assert configurator.get("bot.token") == "runtime-test-token"
        assert configurator.get("transmission.password") == "runtime-test-password"
    finally:
        BotConfigurator.config_file = original_file
        BotConfigurator._runtime_overrides = original_overrides


def test_add_user_does_not_leak_missing_section_runtime_secrets(tmp_path):
    config_file = tmp_path / "torrentino.yaml"
    config_file.write_text("bot: {}\n", encoding="utf-8")
    original_file = BotConfigurator.config_file
    original_overrides = BotConfigurator._runtime_overrides
    try:
        BotConfigurator.config_file = str(config_file)
        BotConfigurator._runtime_overrides = {
            "bot.token": "runtime-token",
            "transmission.host": "tr-bt",
            "transmission.password": "runtime-rpc-password",
        }
        BotConfigurator().add_user(42)
        text = config_file.read_text(encoding="utf-8")
        assert "runtime-token" not in text
        assert "runtime-rpc-password" not in text
        stored = yaml.safe_load(text)
        assert 42 in stored["bot"]["allowed_users"]
        assert (stored.get("transmission") or {}).get("password") in (None, "")
    finally:
        BotConfigurator.config_file = original_file
        BotConfigurator._runtime_overrides = original_overrides
