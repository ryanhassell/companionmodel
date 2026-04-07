from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from dotenv import load_dotenv


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "Companion Pi"
    env: str = "development"
    base_url: str = "http://localhost:8000"
    public_webhook_base_url: str = "http://localhost:8000"
    secret_key: str = "dev-insecure-change-me"
    media_root: str = "var/media"
    log_path: str = "var/log/companion.log"
    prompt_template_root: str = "app/prompts"
    timezone: str = "America/New_York"
    trust_proxy_headers: bool = True


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = "postgresql+asyncpg://companion:companion@localhost:5432/companion"
    sync_url: str = "postgresql+psycopg://companion:companion@localhost:5432/companion"
    pool_size: int = 5
    max_overflow: int = 5
    echo: bool = False


class TwilioConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_sid: str | None = None
    auth_token: str | None = None
    messaging_service_sid: str | None = None
    from_number: str | None = None
    status_callback_url: str | None = None
    voice_callback_url: str | None = None
    voice_status_callback_url: str | None = None
    sip_domain: str | None = None
    validate_signatures: bool = True
    api_timeout_seconds: int = 15
    max_retries: int = 3


class OpenAIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    api_timeout_seconds: int = 30
    image_api_timeout_seconds: int = 900
    max_retries: int = 3
    chat_model: str = "gpt-5.4-mini"
    embedding_model: str = "text-embedding-3-small"
    image_model: str = "gpt-image-1"
    speech_model: str = "gpt-4o-mini-tts"
    realtime_model: str = "gpt-realtime-mini"
    realtime_webhook_secret: str | None = None
    validate_realtime_webhooks: bool = True
    reasoning_effort: str = "low"
    temperature: float = 0.8
    max_output_tokens: int = 220
    memory_max_output_tokens: int = 600
    proactive_max_output_tokens: int = 160
    image_size: str = "1024x1024"


class ElevenLabsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_key: str | None = None
    base_url: str = "https://api.elevenlabs.io/v1"
    api_timeout_seconds: int = 60
    tts_model: str = "eleven_flash_v2_5"


class SchedulingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proactive_scan_seconds: int = 120
    memory_consolidation_minutes: int = 30
    retry_failed_sends_minutes: int = 5
    stale_followup_minutes: int = 15
    cleanup_hours: int = 24
    embed_pending_minutes: int = 10
    daily_life_refresh_minutes: int = 10


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    daily_message_cap: int = 60
    daily_image_cap: int = 3
    daily_call_cap: int = 2
    cooldown_minutes: int = 8
    obsessive_window_minutes: int = 20
    obsessive_message_threshold: int = 10
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"
    proactive_min_gap_minutes: int = 120
    proactive_max_gap_minutes: int = 360
    proactive_probability: float = 0.7
    proactive_morning_start: str = "08:20"
    proactive_morning_end: str = "10:30"
    proactive_midday_start: str = "11:45"
    proactive_midday_end: str = "13:45"
    proactive_evening_start: str = "16:30"
    proactive_evening_end: str = "18:45"
    distress_patterns: list[str] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)
    prohibited_topics: list[str] = Field(default_factory=list)
    deescalation_templates: list[str] = Field(default_factory=list)
    distress_fallback: list[str] = Field(default_factory=list)


class MessagingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_message_length: int = 480
    target_reply_length: str = "short"
    emoji_tendency: str = "low"
    duplicate_similarity_threshold: float = 0.9
    max_recent_context_messages: int = 18
    proactive_image_probability: float = 0.2
    reactive_image_probability: float = 0.04


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    top_k: int = 8
    similarity_threshold: float = 0.2
    max_facts_per_extraction: int = 6
    consolidation_batch_size: int = 20
    summary_target_messages: int = 40
    embedding_dimensions: int = 1536


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    realtime_enabled: bool = False
    driver: str = "twilio_media_streams_openai_stt_elevenlabs"
    max_duration_seconds: int = 300
    default_voice: str = "coral"
    default_tts_mode: str = "twilio_say"
    realtime_voice: str = "coral"
    realtime_sip_uri: str | None = None
    media_streams_websocket_path: str = "/webhooks/twilio/voice/media-stream"
    stt_model: str = "gpt-4o-transcribe"
    text_model: str = "gpt-4o-mini"
    elevenlabs_default_voice_id: str | None = None
    elevenlabs_call_tts_model: str = "eleven_flash_v2_5"
    elevenlabs_creative_tts_model: str = "eleven_v3"
    proactive_call_probability: float = 0.12
    proactive_second_call_probability: float = 0.015
    stream_chunk_ms: int = 40
    vad_rms_threshold: int = 320
    vad_min_speech_ms: int = 80
    vad_silence_ms: int = 120
    sideband_connect_timeout_seconds: int = 20
    sideband_idle_timeout_seconds: int = 900
    max_tool_roundtrips: int = 12


class AdminConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_cookie_name: str = "companion_admin_session"
    session_max_age_seconds: int = 86400
    csrf_protection_enabled: bool = True
    secure_cookies: bool = False
    bootstrap_username: str | None = None
    bootstrap_password: str | None = None


class AlertingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    webhook_url: str | None = None


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_config_file: str = "config/defaults.yaml"
    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    twilio: TwilioConfig = Field(default_factory=TwilioConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            YamlConfigSettingsSource(settings_cls),
        )

    @property
    def media_root_path(self) -> Path:
        return Path(self.app.media_root).resolve()

    @property
    def prompt_template_root_path(self) -> Path:
        return Path(self.app.prompt_template_root).resolve()

    @property
    def log_path(self) -> Path:
        return Path(self.app.log_path).resolve()

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        secret_keys = {"secret_key", "api_key", "auth_token", "bootstrap_password"}
        return _redact_mapping(data, secret_keys)


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        data = self()
        return data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        config_path = os.getenv("APP_CONFIG_FILE", "config/defaults.yaml")
        load_dotenv(".env", override=True)
        path = Path(config_path)
        if not path.exists():
            return {}
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return _apply_flat_env_overrides(raw)


def _apply_flat_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL")
    database_sync_url = os.getenv("DATABASE_SYNC_URL")
    if database_url:
        raw.setdefault("database", {})["url"] = database_url
    if database_sync_url:
        raw.setdefault("database", {})["sync_url"] = database_sync_url
    env_map = {
        ("app", "secret_key"): os.getenv("APP_SECRET_KEY"),
        ("app", "base_url"): os.getenv("APP_BASE_URL"),
        ("app", "public_webhook_base_url"): os.getenv("APP_PUBLIC_WEBHOOK_BASE_URL"),
        ("app", "log_path"): os.getenv("APP_LOG_PATH"),
        ("app", "media_root"): os.getenv("APP_MEDIA_ROOT"),
        ("app", "prompt_template_root"): os.getenv("APP_PROMPT_TEMPLATE_ROOT"),
        ("twilio", "account_sid"): os.getenv("TWILIO_ACCOUNT_SID"),
        ("twilio", "auth_token"): os.getenv("TWILIO_AUTH_TOKEN"),
        ("twilio", "messaging_service_sid"): os.getenv("TWILIO_MESSAGING_SERVICE_SID"),
        ("twilio", "from_number"): os.getenv("TWILIO_FROM_NUMBER"),
        ("twilio", "status_callback_url"): os.getenv("TWILIO_STATUS_CALLBACK_URL"),
        ("twilio", "voice_callback_url"): os.getenv("TWILIO_VOICE_CALLBACK_URL"),
        ("twilio", "voice_status_callback_url"): os.getenv("TWILIO_VOICE_STATUS_CALLBACK_URL"),
        ("twilio", "sip_domain"): os.getenv("TWILIO_SIP_DOMAIN"),
        ("openai", "api_key"): os.getenv("OPENAI_API_KEY"),
        ("openai", "base_url"): os.getenv("OPENAI_BASE_URL"),
        ("openai", "chat_model"): os.getenv("OPENAI_CHAT_MODEL"),
        ("openai", "embedding_model"): os.getenv("OPENAI_EMBEDDING_MODEL"),
        ("openai", "image_model"): os.getenv("OPENAI_IMAGE_MODEL"),
        ("openai", "speech_model"): os.getenv("OPENAI_SPEECH_MODEL"),
        ("openai", "realtime_model"): os.getenv("OPENAI_REALTIME_MODEL"),
        ("openai", "realtime_webhook_secret"): os.getenv("OPENAI_REALTIME_WEBHOOK_SECRET"),
        ("openai", "validate_realtime_webhooks"): os.getenv("OPENAI_VALIDATE_REALTIME_WEBHOOKS"),
        ("elevenlabs", "api_key"): os.getenv("ELEVENLABS_API_KEY"),
        ("elevenlabs", "base_url"): os.getenv("ELEVENLABS_BASE_URL"),
        ("elevenlabs", "tts_model"): os.getenv("ELEVENLABS_TTS_MODEL"),
        ("voice", "driver"): os.getenv("VOICE_DRIVER"),
        ("voice", "realtime_sip_uri"): os.getenv("VOICE_REALTIME_SIP_URI"),
        ("voice", "media_streams_websocket_path"): os.getenv("VOICE_MEDIA_STREAMS_WEBSOCKET_PATH"),
        ("voice", "stt_model"): os.getenv("VOICE_STT_MODEL"),
        ("voice", "text_model"): os.getenv("VOICE_TEXT_MODEL"),
        ("voice", "elevenlabs_default_voice_id"): os.getenv("VOICE_ELEVENLABS_DEFAULT_VOICE_ID"),
        ("voice", "elevenlabs_call_tts_model"): os.getenv("VOICE_ELEVENLABS_CALL_TTS_MODEL"),
        ("voice", "elevenlabs_creative_tts_model"): os.getenv("VOICE_ELEVENLABS_CREATIVE_TTS_MODEL"),
        ("admin", "bootstrap_username"): os.getenv("ADMIN_BOOTSTRAP_USERNAME"),
        ("admin", "bootstrap_password"): os.getenv("ADMIN_BOOTSTRAP_PASSWORD"),
        ("admin", "session_cookie_name"): os.getenv("ADMIN_SESSION_COOKIE_NAME"),
        ("admin", "session_max_age_seconds"): os.getenv("ADMIN_SESSION_MAX_AGE_SECONDS"),
        ("alerting", "webhook_url"): os.getenv("ALERT_WEBHOOK_URL"),
    }
    for key_path, value in env_map.items():
        if value in (None, ""):
            continue
        cursor = raw
        for segment in key_path[:-1]:
            cursor = cursor.setdefault(segment, {})
        cursor[key_path[-1]] = value
    return raw


def _redact_mapping(data: Any, secret_keys: set[str]) -> Any:
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if key in secret_keys and value:
                redacted[key] = "***redacted***"
            else:
                redacted[key] = _redact_mapping(value, secret_keys)
        return redacted
    if isinstance(data, list):
        return [_redact_mapping(item, secret_keys) for item in data]
    return data


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    return RuntimeSettings()
