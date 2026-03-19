from __future__ import annotations

from app.core.settings import RuntimeSettings


def test_config_loading_from_yaml_and_env(tmp_path, monkeypatch):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        """
app:
  name: Test App
safety:
  daily_message_cap: 12
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("OPENAI_API_KEY", "abc123")
    settings = RuntimeSettings(_env_file=None)
    assert settings.app.name == "Test App"
    assert settings.safety.daily_message_cap == 12
    assert settings.openai.api_key == "abc123"
