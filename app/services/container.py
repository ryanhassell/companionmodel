from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.settings import RuntimeSettings, get_settings
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.alerting import AlertingService
from app.services.audit import AuditService
from app.services.auth import AuthService
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.daily_life import DailyLifeService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.message import MessageService
from app.services.prompt import PromptService
from app.services.proactive import ProactiveService
from app.services.safety import SafetyService
from app.services.schedule import ScheduleService
from app.services.voice import VoiceService


@dataclass(slots=True)
class ServiceContainer:
    settings: RuntimeSettings
    http_client: httpx.AsyncClient
    openai_provider: OpenAIProvider
    elevenlabs_provider: ElevenLabsProvider
    twilio_provider: TwilioProvider
    alerting_service: AlertingService
    audit_service: AuditService
    auth_service: AuthService
    config_service: ConfigService
    conversation_service: ConversationService
    daily_life_service: DailyLifeService
    prompt_service: PromptService
    schedule_service: ScheduleService
    safety_service: SafetyService
    memory_service: MemoryService
    image_service: ImageService
    voice_service: VoiceService
    message_service: MessageService
    proactive_service: ProactiveService
    scheduler_service: object | None = None

    @classmethod
    def build(cls, settings: RuntimeSettings | None = None) -> "ServiceContainer":
        actual_settings = settings or get_settings()
        http_client = httpx.AsyncClient(follow_redirects=True)
        openai_provider = OpenAIProvider(actual_settings, http_client)
        elevenlabs_provider = ElevenLabsProvider(actual_settings, http_client)
        twilio_provider = TwilioProvider(actual_settings, http_client)
        alerting_service = AlertingService(actual_settings, http_client)
        audit_service = AuditService()
        auth_service = AuthService()
        config_service = ConfigService(actual_settings)
        conversation_service = ConversationService()
        prompt_service = PromptService(actual_settings)
        schedule_service = ScheduleService()
        safety_service = SafetyService(alerting_service)
        memory_service = MemoryService(actual_settings, openai_provider, prompt_service)
        daily_life_service = DailyLifeService(memory_service)
        image_service = ImageService(actual_settings, openai_provider, prompt_service)
        voice_service = VoiceService(
            actual_settings,
            twilio_provider,
            openai_provider,
            elevenlabs_provider,
            prompt_service,
            memory_service,
            daily_life_service,
        )
        message_service = MessageService(
            actual_settings,
            twilio_provider,
            openai_provider,
            prompt_service,
            safety_service,
            memory_service,
            conversation_service,
            daily_life_service,
            schedule_service,
            config_service,
            image_service,
        )
        proactive_service = ProactiveService(
            config_service,
            conversation_service,
            prompt_service,
            message_service,
            schedule_service,
            daily_life_service,
            image_service,
            memory_service,
            voice_service,
        )
        return cls(
            settings=actual_settings,
            http_client=http_client,
            openai_provider=openai_provider,
            elevenlabs_provider=elevenlabs_provider,
            twilio_provider=twilio_provider,
            alerting_service=alerting_service,
            audit_service=audit_service,
            auth_service=auth_service,
            config_service=config_service,
            conversation_service=conversation_service,
            daily_life_service=daily_life_service,
            prompt_service=prompt_service,
            schedule_service=schedule_service,
            safety_service=safety_service,
            memory_service=memory_service,
            image_service=image_service,
            voice_service=voice_service,
            message_service=message_service,
            proactive_service=proactive_service,
        )

    async def aclose(self) -> None:
        await self.http_client.aclose()
