from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.ai import AiRuntime
from app.core.settings import RuntimeSettings, get_settings
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.alerting import AlertingService
from app.services.admin_authz import AdminAuthzService
from app.services.audit import AuditService
from app.services.auth import AuthService
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.conversation_state import ConversationStateService
from app.services.candidate_reply import CandidateReplyService
from app.services.billing import BillingService
from app.services.clerk_auth import ClerkAuthService
from app.services.customer_auth import CustomerAuthService
from app.services.daily_life import DailyLifeService
from app.services.human_likeness import HumanLikenessService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.message import MessageService
from app.services.notifications import NotificationService
from app.services.parent_chat import ParentChatService
from app.services.portal_initialization import PortalInitializationService
from app.services.portal_preview import PortalPreviewService
from app.services.prompt import PromptService
from app.services.proactive import ProactiveService
from app.services.pricing_simulation import PricingSimulationService
from app.services.reply_ranker import ReplyRankerService
from app.services.safety import SafetyService
from app.services.safety_rewrite import SafetyRewriteService
from app.services.schedule import ScheduleService
from app.services.rate_limiter import RateLimiterService
from app.services.turn_classifier import TurnClassifierService
from app.services.usage_ingestion import UsageIngestionService
from app.services.usage_reconciliation import UsageReconciliationService
from app.services.voice import VoiceService


@dataclass(slots=True)
class ServiceContainer:
    settings: RuntimeSettings
    http_client: httpx.AsyncClient
    ai_runtime: AiRuntime
    openai_provider: OpenAIProvider
    elevenlabs_provider: ElevenLabsProvider
    twilio_provider: TwilioProvider
    alerting_service: AlertingService
    admin_authz_service: AdminAuthzService
    audit_service: AuditService
    auth_service: AuthService
    config_service: ConfigService
    conversation_service: ConversationService
    conversation_state_service: ConversationStateService
    turn_classifier_service: TurnClassifierService
    candidate_reply_service: CandidateReplyService
    reply_ranker_service: ReplyRankerService
    safety_rewrite_service: SafetyRewriteService
    customer_auth_service: CustomerAuthService
    clerk_auth_service: ClerkAuthService
    billing_service: BillingService
    portal_initialization_service: PortalInitializationService
    portal_preview_service: PortalPreviewService
    usage_ingestion_service: UsageIngestionService
    usage_reconciliation_service: UsageReconciliationService
    pricing_simulation_service: PricingSimulationService
    notification_service: NotificationService
    parent_chat_service: ParentChatService
    rate_limiter_service: RateLimiterService
    human_likeness_service: HumanLikenessService
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
        ai_runtime = AiRuntime(actual_settings, http_client)
        openai_provider = OpenAIProvider(actual_settings, http_client)
        elevenlabs_provider = ElevenLabsProvider(actual_settings, http_client)
        twilio_provider = TwilioProvider(actual_settings, http_client)
        alerting_service = AlertingService(actual_settings, http_client)
        audit_service = AuditService()
        auth_service = AuthService()
        config_service = ConfigService(actual_settings)
        conversation_service = ConversationService()
        conversation_state_service = ConversationStateService()
        prompt_service = PromptService(actual_settings)
        schedule_service = ScheduleService()
        safety_service = SafetyService(alerting_service)
        turn_classifier_service = TurnClassifierService(ai_runtime, prompt_service)
        candidate_reply_service = CandidateReplyService(ai_runtime, prompt_service)
        reply_ranker_service = ReplyRankerService()
        safety_rewrite_service = SafetyRewriteService(ai_runtime, prompt_service)
        customer_auth_service = CustomerAuthService(actual_settings)
        clerk_auth_service = ClerkAuthService(actual_settings)
        admin_authz_service = AdminAuthzService(actual_settings, clerk_auth_service=clerk_auth_service)
        billing_service = BillingService(actual_settings)
        portal_initialization_service = PortalInitializationService(actual_settings, billing_service)
        usage_ingestion_service = UsageIngestionService()
        portal_preview_service = PortalPreviewService(
            actual_settings,
            ai_runtime,
            usage_ingestion_service,
        )
        usage_reconciliation_service = UsageReconciliationService(usage_ingestion_service)
        pricing_simulation_service = PricingSimulationService()
        notification_service = NotificationService(actual_settings, twilio_provider)
        rate_limiter_service = RateLimiterService(actual_settings)
        human_likeness_service = HumanLikenessService(
            turn_classifier_service,
            candidate_reply_service,
            reply_ranker_service,
        )
        memory_service = MemoryService(actual_settings, ai_runtime, prompt_service)
        parent_chat_service = ParentChatService(
            actual_settings,
            ai_runtime,
            config_service,
            conversation_service,
            memory_service,
        )
        daily_life_service = DailyLifeService(memory_service)
        image_service = ImageService(actual_settings, openai_provider, prompt_service)
        voice_service = VoiceService(
            actual_settings,
            twilio_provider,
            openai_provider,
            ai_runtime,
            elevenlabs_provider,
            prompt_service,
            memory_service,
            daily_life_service,
            conversation_state_service,
            usage_ingestion_service,
        )
        message_service = MessageService(
            actual_settings,
            twilio_provider,
            openai_provider,
            ai_runtime,
            prompt_service,
            safety_service,
            memory_service,
            conversation_service,
            daily_life_service,
            schedule_service,
            config_service,
            image_service,
            conversation_state_service,
            turn_classifier_service,
            candidate_reply_service,
            reply_ranker_service,
            safety_rewrite_service,
            usage_ingestion_service,
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
            conversation_state_service,
            usage_ingestion_service,
        )
        return cls(
            settings=actual_settings,
            http_client=http_client,
            ai_runtime=ai_runtime,
            openai_provider=openai_provider,
            elevenlabs_provider=elevenlabs_provider,
            twilio_provider=twilio_provider,
            alerting_service=alerting_service,
            admin_authz_service=admin_authz_service,
            audit_service=audit_service,
            auth_service=auth_service,
            config_service=config_service,
            conversation_service=conversation_service,
            conversation_state_service=conversation_state_service,
            turn_classifier_service=turn_classifier_service,
            candidate_reply_service=candidate_reply_service,
            reply_ranker_service=reply_ranker_service,
            safety_rewrite_service=safety_rewrite_service,
            customer_auth_service=customer_auth_service,
            clerk_auth_service=clerk_auth_service,
            billing_service=billing_service,
            portal_initialization_service=portal_initialization_service,
            portal_preview_service=portal_preview_service,
            usage_ingestion_service=usage_ingestion_service,
            usage_reconciliation_service=usage_reconciliation_service,
            pricing_simulation_service=pricing_simulation_service,
            notification_service=notification_service,
            parent_chat_service=parent_chat_service,
            rate_limiter_service=rate_limiter_service,
            human_likeness_service=human_likeness_service,
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
        await self.rate_limiter_service.close()
        await self.http_client.aclose()
