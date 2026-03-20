"""initial schema

Revision ID: 20260318_0001
Revises:
Create Date: 2026-03-18 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = "20260318_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    app_setting_scope = postgresql.ENUM("global", "persona", "user", name="appsettingscope", create_type=False)
    direction = postgresql.ENUM("inbound", "outbound", name="direction", create_type=False)
    channel = postgresql.ENUM("sms", "mms", "voice", "system", name="channel", create_type=False)
    message_status = postgresql.ENUM(
        "queued",
        "processing",
        "sent",
        "delivered",
        "failed",
        "received",
        "blocked",
        name="messagestatus",
        create_type=False,
    )
    media_role = postgresql.ENUM("inbound", "outbound", "generated", name="mediarole", create_type=False)
    memory_type = postgresql.ENUM(
        "fact",
        "episode",
        "summary",
        "preference",
        "follow_up",
        "operator_note",
        "safety",
        name="memorytype",
        create_type=False,
    )
    safety_severity = postgresql.ENUM("low", "medium", "high", "critical", name="safetyseverity", create_type=False)
    schedule_rule_type = postgresql.ENUM(
        "proactive_window",
        "quiet_hours",
        "follow_up",
        "call_window",
        name="scheduleruletype",
        create_type=False,
    )
    delivery_status = postgresql.ENUM("pending", "sent", "failed", "acknowledged", name="deliverystatus", create_type=False)
    call_direction = postgresql.ENUM("outbound", "inbound", name="calldirection", create_type=False)
    call_status = postgresql.ENUM(
        "queued",
        "ringing",
        "in_progress",
        "completed",
        "failed",
        "no_answer",
        name="callstatus",
        create_type=False,
    )
    job_status = postgresql.ENUM("idle", "running", "success", "failed", name="jobstatus", create_type=False)

    bind = op.get_bind()
    for enum in [
        app_setting_scope,
        direction,
        channel,
        message_status,
        media_role,
        memory_type,
        safety_severity,
        schedule_rule_type,
        delivery_status,
        call_direction,
        call_status,
        job_status,
    ]:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "personas",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("style", sa.Text()),
        sa.Column("tone", sa.Text()),
        sa.Column("boundaries", sa.Text()),
        sa.Column("topics_of_interest", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("favorite_activities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("image_appearance", sa.Text()),
        sa.Column("speech_style", sa.Text()),
        sa.Column("disclosure_policy", sa.Text()),
        sa.Column("texting_length_preference", sa.String(length=32)),
        sa.Column("emoji_tendency", sa.String(length=32)),
        sa.Column("proactive_outreach_style", sa.Text()),
        sa.Column("visual_bible", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("prompt_overrides", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("safety_overrides", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("operator_notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_personas_key", "personas", ["key"], unique=True)
    op.create_index("ix_personas_active", "personas", ["is_active"])

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("display_name", sa.String(length=120)),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="America/New_York"),
        sa.Column("notes", sa.Text()),
        sa.Column("profile_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("schedule_overrides", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("safety_overrides", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("preferred_persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_users_phone_number", "users", ["phone_number"], unique=True)
    op.create_index("ix_users_enabled", "users", ["is_enabled"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("state_label", sa.String(length=64)),
        sa.Column("unresolved_thread_summary", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_conversations_user_persona", "conversations", ["user_id", "persona_id"])
    op.create_index("ix_conversations_status", "conversations", ["status"])

    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("direction", direction, nullable=False),
        sa.Column("channel", channel, nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="twilio"),
        sa.Column("provider_message_sid", sa.String(length=80)),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("normalized_body", sa.Text()),
        sa.Column("status", message_status, nullable=False),
        sa.Column("is_proactive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("safety_state", sa.String(length=40)),
        sa.Column("repetition_score", sa.Float()),
        sa.Column("prompt_template_name", sa.String(length=80)),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("ix_messages_provider_sid", "messages", ["provider_message_sid"], unique=True)
    op.create_index("ix_messages_direction_status", "messages", ["direction", "status"])
    op.create_index("ix_messages_idempotency_key", "messages", ["idempotency_key"], unique=True)

    op.create_table(
        "media_assets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("message_id", sa.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("provider_asset_id", sa.String(length=120)),
        sa.Column("role", media_role, nullable=False),
        sa.Column("mime_type", sa.String(length=120)),
        sa.Column("local_path", sa.String(length=300)),
        sa.Column("remote_url", sa.String(length=500)),
        sa.Column("prompt_text", sa.Text()),
        sa.Column("negative_prompt", sa.Text()),
        sa.Column("generation_status", sa.String(length=40), nullable=False, server_default="ready"),
        sa.Column("error_message", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_media_assets_user_created", "media_assets", ["user_id", "created_at"])
    op.create_index("ix_media_assets_status", "media_assets", ["generation_status"])

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("message_id", sa.UUID(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", delivery_status, nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("request_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("response_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_delivery_attempts_message_created", "delivery_attempts", ["message_id", "created_at"])
    op.create_index("ix_delivery_attempts_status", "delivery_attempts", ["status"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("source_message_id", sa.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("consolidated_into_id", sa.UUID(), sa.ForeignKey("memory_items.id")),
        sa.Column("memory_type", memory_type, nullable=False),
        sa.Column("title", sa.String(length=120)),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("retrieval_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("embedding_model", sa.String(length=120)),
        sa.Column("embedding_text", sa.Text()),
        sa.Column("embedding_vector", Vector(1536)),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_memory_items_user_persona", "memory_items", ["user_id", "persona_id"])
    op.create_index("ix_memory_items_type", "memory_items", ["memory_type"])
    op.create_index("ix_memory_items_active", "memory_items", ["disabled", "pinned"])
    op.create_index(
        "ix_memory_items_embedding_vector_ivfflat",
        "memory_items",
        ["embedding_vector"],
        postgresql_using="ivfflat",
        postgresql_with={"lists": 100},
        postgresql_ops={"embedding_vector": "vector_cosine_ops"},
    )

    op.create_table(
        "schedule_rules",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("rule_type", schedule_rule_type, nullable=False),
        sa.Column("weekday", sa.Integer()),
        sa.Column("start_time", sa.Time()),
        sa.Column("end_time", sa.Time()),
        sa.Column("min_gap_minutes", sa.Integer()),
        sa.Column("max_gap_minutes", sa.Integer()),
        sa.Column("probability", sa.Float()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_schedule_rules_scope", "schedule_rules", ["user_id", "persona_id", "enabled"])
    op.create_index("ix_schedule_rules_type", "schedule_rules", ["rule_type"])

    op.create_table(
        "app_settings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("scope", app_setting_scope, nullable=False),
        sa.Column("namespace", sa.String(length=80), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("value_json", sa.JSON()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
    )
    op.create_index("ix_app_settings_scope_key", "app_settings", ["scope", "namespace", "key"])
    op.create_index("ix_app_settings_user_scope", "app_settings", ["user_id", "persona_id"])

    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False, server_default="sms"),
        sa.Column("description", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("variables_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="file_seed"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_prompt_templates_name_version", "prompt_templates", ["name", "version"], unique=True)
    op.create_index("ix_prompt_templates_active", "prompt_templates", ["name", "is_active"])

    op.create_table(
        "safety_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("conversations.id")),
        sa.Column("message_id", sa.UUID(), sa.ForeignKey("messages.id")),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("severity", safety_severity, nullable=False),
        sa.Column("detector", sa.String(length=80), nullable=False),
        sa.Column("action_taken", sa.Text()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_safety_events_user_created", "safety_events", ["user_id", "created_at"])
    op.create_index("ix_safety_events_severity", "safety_events", ["severity"])

    op.create_table(
        "call_records",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("provider_call_sid", sa.String(length=80)),
        sa.Column("direction", call_direction, nullable=False),
        sa.Column("status", call_status, nullable=False),
        sa.Column("from_number", sa.String(length=32)),
        sa.Column("to_number", sa.String(length=32)),
        sa.Column("script", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_call_records_provider_sid", "call_records", ["provider_call_sid"], unique=True)
    op.create_index("ix_call_records_user_created", "call_records", ["user_id", "created_at"])

    op.create_table(
        "admin_users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_admin_users_username", "admin_users", ["username"], unique=True)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("admin_user_id", sa.UUID(), sa.ForeignKey("admin_users.id")),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=80)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("job_name", sa.String(length=120), nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="idle"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_job_runs_name_created", "job_runs", ["job_name", "created_at"])


def downgrade() -> None:
    for table in [
        "job_runs",
        "audit_events",
        "admin_users",
        "call_records",
        "safety_events",
        "prompt_templates",
        "app_settings",
        "schedule_rules",
        "memory_items",
        "delivery_attempts",
        "media_assets",
        "messages",
        "conversations",
        "users",
        "personas",
    ]:
        op.drop_table(table)

    bind = op.get_bind()
    for enum_name in [
        "jobstatus",
        "callstatus",
        "calldirection",
        "deliverystatus",
        "scheduleruletype",
        "safetyseverity",
        "memorytype",
        "mediarole",
        "messagestatus",
        "channel",
        "direction",
        "appsettingscope",
    ]:
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)
