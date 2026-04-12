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
    portal_preview_model: str = "gpt-4o-mini"
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
    memory_health_hour: int = 3
    memory_health_minute: int = 15
    retry_failed_sends_minutes: int = 5
    stale_followup_minutes: int = 15
    cleanup_hours: int = 24
    embed_pending_minutes: int = 10
    daily_life_refresh_minutes: int = 10
    usage_reconciliation_minutes: int = 30


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
    internal_only: bool = True
    allowlist_cidrs: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.1/32",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    trusted_header_name: str = "x-resona-admin-access"
    trusted_header_value: str | None = None
    clerk_enabled: bool = False
    clerk_role_allowlist: list[str] = Field(default_factory=lambda: ["org:admin", "admin", "owner"])
    clerk_user_allowlist: list[str] = Field(default_factory=list)
    clerk_email_allowlist: list[str] = Field(default_factory=list)
    require_clerk_mfa: bool = True


class AlertingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    webhook_url: str | None = None


class HumanLikenessConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    candidate_count: int = 3
    proactive_fatigue_threshold: float = 0.82
    memory_cooldown_minutes: int = 45
    style_adaptation_enabled: bool = True


class CustomerPortalConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    session_cookie_name: str = "companion_portal_session"
    session_max_age_seconds: int = 604800
    secure_cookies: bool = False
    email_token_minutes: int = 60
    otp_code_minutes: int = 10
    otp_max_attempts: int = 6
    max_login_failures: int = 8
    lockout_minutes: int = 20
    policy_version: str = "2026-04-07"


class ClerkConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    secret_key: str | None = None
    publishable_key: str | None = None
    frontend_api_url: str | None = None
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    session_cookie_name: str = "__session"
    backend_session_cookie_name: str = "resona_clerk_session"
    sign_in_url: str = "/sign-in"
    sign_up_url: str = "/sign-up"
    sign_out_url: str | None = None
    require_org: bool = True
    require_owner_mfa: bool = True


class StripeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    secret_key: str | None = None
    webhook_secret: str | None = None
    publishable_key: str | None = None
    default_price_id: str | None = None
    chat_price_id: str | None = None
    voice_price_id: str | None = None
    additional_child_price_id: str | None = None
    included_child_profiles: int = 1
    additional_child_monthly_usd: float = 12.0
    success_path: str = "/app/billing?checkout=success"
    cancel_path: str = "/app/billing?checkout=cancel"


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    from_address: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    use_starttls: bool = True
    use_ssl: bool = False


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = None
    key_prefix: str = "companion:ratelimit:"


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signup_limit: int = 5
    signup_window_seconds: int = 900
    login_limit: int = 12
    login_window_seconds: int = 900
    verify_email_limit: int = 8
    verify_email_window_seconds: int = 900
    otp_send_limit: int = 6
    otp_send_window_seconds: int = 900
    otp_check_limit: int = 12
    otp_check_window_seconds: int = 900
    initialize_preview_limit: int = 12
    initialize_preview_window_seconds: int = 3600


class WebPresentationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    brand_name: str = "Resona"
    canonical_domain: str = "resona.chat"
    support_email: str = "support@resona.chat"
    privacy_url: str = "/privacy-policy"
    terms_url: str = "/terms-and-conditions"
    safety_policy_url: str = "/safety"


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
    human_likeness: HumanLikenessConfig = Field(default_factory=HumanLikenessConfig)
    customer_portal: CustomerPortalConfig = Field(default_factory=CustomerPortalConfig)
    clerk: ClerkConfig = Field(default_factory=ClerkConfig)
    stripe: StripeConfig = Field(default_factory=StripeConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    web: WebPresentationConfig = Field(default_factory=WebPresentationConfig)

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
        # Preserve runtime/container environment variables (e.g., docker-compose).
        # .env should only fill missing values, not overwrite injected env.
        load_dotenv(".env", override=False)
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
        ("openai", "portal_preview_model"): os.getenv("OPENAI_PORTAL_PREVIEW_MODEL"),
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
        ("scheduling", "usage_reconciliation_minutes"): os.getenv("SCHEDULING_USAGE_RECONCILIATION_MINUTES"),
        ("admin", "bootstrap_username"): os.getenv("ADMIN_BOOTSTRAP_USERNAME"),
        ("admin", "bootstrap_password"): os.getenv("ADMIN_BOOTSTRAP_PASSWORD"),
        ("admin", "session_cookie_name"): os.getenv("ADMIN_SESSION_COOKIE_NAME"),
        ("admin", "session_max_age_seconds"): os.getenv("ADMIN_SESSION_MAX_AGE_SECONDS"),
        ("admin", "internal_only"): os.getenv("ADMIN_INTERNAL_ONLY"),
        ("admin", "allowlist_cidrs"): os.getenv("ADMIN_ALLOWLIST_CIDRS"),
        ("admin", "trusted_header_name"): os.getenv("ADMIN_TRUSTED_HEADER_NAME"),
        ("admin", "trusted_header_value"): os.getenv("ADMIN_TRUSTED_HEADER_VALUE"),
        ("admin", "clerk_enabled"): os.getenv("ADMIN_CLERK_ENABLED"),
        ("admin", "clerk_role_allowlist"): os.getenv("ADMIN_CLERK_ROLE_ALLOWLIST"),
        ("admin", "clerk_user_allowlist"): os.getenv("ADMIN_CLERK_USER_ALLOWLIST"),
        ("admin", "clerk_email_allowlist"): os.getenv("ADMIN_CLERK_EMAIL_ALLOWLIST"),
        ("admin", "require_clerk_mfa"): os.getenv("ADMIN_REQUIRE_CLERK_MFA"),
        ("customer_portal", "session_cookie_name"): os.getenv("PORTAL_SESSION_COOKIE_NAME"),
        ("customer_portal", "session_max_age_seconds"): os.getenv("PORTAL_SESSION_MAX_AGE_SECONDS"),
        ("customer_portal", "secure_cookies"): os.getenv("PORTAL_SECURE_COOKIES"),
        ("customer_portal", "email_token_minutes"): os.getenv("PORTAL_EMAIL_TOKEN_MINUTES"),
        ("customer_portal", "otp_code_minutes"): os.getenv("PORTAL_OTP_CODE_MINUTES"),
        ("customer_portal", "otp_max_attempts"): os.getenv("PORTAL_OTP_MAX_ATTEMPTS"),
        ("customer_portal", "max_login_failures"): os.getenv("PORTAL_MAX_LOGIN_FAILURES"),
        ("customer_portal", "lockout_minutes"): os.getenv("PORTAL_LOCKOUT_MINUTES"),
        ("clerk", "enabled"): os.getenv("CLERK_ENABLED"),
        ("clerk", "secret_key"): os.getenv("CLERK_SECRET_KEY"),
        ("clerk", "publishable_key"): os.getenv("CLERK_PUBLISHABLE_KEY"),
        ("clerk", "frontend_api_url"): os.getenv("CLERK_FRONTEND_API_URL"),
        ("clerk", "issuer"): os.getenv("CLERK_ISSUER"),
        ("clerk", "audience"): os.getenv("CLERK_AUDIENCE"),
        ("clerk", "jwks_url"): os.getenv("CLERK_JWKS_URL"),
        ("clerk", "session_cookie_name"): os.getenv("CLERK_SESSION_COOKIE_NAME"),
        ("clerk", "backend_session_cookie_name"): os.getenv("CLERK_BACKEND_SESSION_COOKIE_NAME"),
        ("clerk", "sign_in_url"): os.getenv("CLERK_SIGN_IN_URL"),
        ("clerk", "sign_up_url"): os.getenv("CLERK_SIGN_UP_URL"),
        ("clerk", "sign_out_url"): os.getenv("CLERK_SIGN_OUT_URL"),
        ("clerk", "require_org"): os.getenv("CLERK_REQUIRE_ORG"),
        ("clerk", "require_owner_mfa"): os.getenv("CLERK_REQUIRE_OWNER_MFA"),
        ("stripe", "enabled"): os.getenv("STRIPE_ENABLED"),
        ("stripe", "secret_key"): os.getenv("STRIPE_SECRET_KEY"),
        ("stripe", "webhook_secret"): os.getenv("STRIPE_WEBHOOK_SECRET"),
        ("stripe", "publishable_key"): os.getenv("STRIPE_PUBLISHABLE_KEY"),
        ("stripe", "default_price_id"): os.getenv("STRIPE_DEFAULT_PRICE_ID"),
        ("stripe", "chat_price_id"): os.getenv("STRIPE_CHAT_PRICE_ID"),
        ("stripe", "voice_price_id"): os.getenv("STRIPE_VOICE_PRICE_ID"),
        ("stripe", "additional_child_price_id"): os.getenv("STRIPE_ADDITIONAL_CHILD_PRICE_ID"),
        ("stripe", "included_child_profiles"): os.getenv("STRIPE_INCLUDED_CHILD_PROFILES"),
        ("stripe", "additional_child_monthly_usd"): os.getenv("STRIPE_ADDITIONAL_CHILD_MONTHLY_USD"),
        ("email", "enabled"): os.getenv("EMAIL_ENABLED"),
        ("email", "from_address"): os.getenv("EMAIL_FROM_ADDRESS"),
        ("email", "smtp_host"): os.getenv("EMAIL_SMTP_HOST"),
        ("email", "smtp_port"): os.getenv("EMAIL_SMTP_PORT"),
        ("email", "smtp_username"): os.getenv("EMAIL_SMTP_USERNAME"),
        ("email", "smtp_password"): os.getenv("EMAIL_SMTP_PASSWORD"),
        ("email", "use_starttls"): os.getenv("EMAIL_USE_STARTTLS"),
        ("email", "use_ssl"): os.getenv("EMAIL_USE_SSL"),
        ("redis", "url"): os.getenv("REDIS_URL"),
        ("redis", "key_prefix"): os.getenv("REDIS_KEY_PREFIX"),
        ("rate_limit", "initialize_preview_limit"): os.getenv("RATE_LIMIT_INITIALIZE_PREVIEW_LIMIT"),
        ("rate_limit", "initialize_preview_window_seconds"): os.getenv("RATE_LIMIT_INITIALIZE_PREVIEW_WINDOW_SECONDS"),
        ("web", "brand_name"): os.getenv("WEB_BRAND_NAME"),
        ("web", "canonical_domain"): os.getenv("WEB_CANONICAL_DOMAIN"),
        ("web", "support_email"): os.getenv("WEB_SUPPORT_EMAIL"),
        ("web", "privacy_url"): os.getenv("WEB_PRIVACY_URL"),
        ("web", "terms_url"): os.getenv("WEB_TERMS_URL"),
        ("web", "safety_policy_url"): os.getenv("WEB_SAFETY_POLICY_URL"),
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
