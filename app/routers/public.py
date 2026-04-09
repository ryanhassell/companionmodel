from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.templating import templates
from app.schemas.site import MarketingFeatureCard, PublicSiteContext, SafetyCapabilityCard

router = APIRouter(tags=["public"])


def _shared_context(request: Request) -> dict[str, object]:
    settings = request.app.state.container.settings
    context = PublicSiteContext(
        brand_name=settings.web.brand_name,
        canonical_domain=settings.web.canonical_domain,
        support_email=settings.web.support_email,
        privacy_url=settings.web.privacy_url,
        terms_url=settings.web.terms_url,
        safety_policy_url=settings.web.safety_policy_url,
    )
    return {"request": request, "site": context}


@router.get("/")
async def home_page(request: Request):
    features = [
        MarketingFeatureCard(
            title="Conversation Monitoring",
            summary="Parents can review full text timelines with clear direction, channel, and timestamps.",
            icon="timeline",
        ),
        MarketingFeatureCard(
            title="Safety Signals",
            summary="Events are surfaced with severity, detector reason, and the exact action taken by the system.",
            icon="shield",
        ),
        MarketingFeatureCard(
            title="Memory and Continuity",
            summary="Resona keeps context across days so replies stay coherent and less repetitive.",
            icon="memory",
        ),
        MarketingFeatureCard(
            title="Voice Continuity",
            summary="Voice calls and text sessions stay linked so families can follow one complete narrative.",
            icon="phone",
        ),
        MarketingFeatureCard(
            title="Billing Transparency",
            summary="Included monthly credits and overage behavior are visible in one billing surface.",
            icon="billing",
        ),
    ]
    return templates.TemplateResponse(
        "public/home.html",
        {
            **_shared_context(request),
            "features": features,
        },
    )


@router.get("/features")
async def features_page(request: Request):
    return templates.TemplateResponse("public/features.html", _shared_context(request))


@router.get("/safety")
async def safety_page(request: Request):
    capabilities = [
        SafetyCapabilityCard(
            title="Boundary Enforcement",
            summary="The assistant blocks manipulative, exclusive, or inappropriate relationship framing.",
            icon="shield",
        ),
        SafetyCapabilityCard(
            title="Distress Escalation",
            summary="High-risk language triggers stricter response handling and emergency-support guidance.",
            icon="alert",
        ),
        SafetyCapabilityCard(
            title="Transparent Event Logging",
            summary="Parents can inspect safety events with severity, detector source, and interventions.",
            icon="log",
        ),
    ]
    return templates.TemplateResponse(
        "public/safety.html",
        {
            **_shared_context(request),
            "capabilities": capabilities,
        },
    )


@router.get("/how-it-works")
async def how_it_works_page(request: Request):
    return templates.TemplateResponse("public/how_it_works.html", _shared_context(request))


@router.get("/pricing")
async def pricing_page(request: Request):
    return templates.TemplateResponse("public/pricing.html", _shared_context(request))


@router.get("/faq")
async def faq_page(request: Request):
    return templates.TemplateResponse("public/faq.html", _shared_context(request))


@router.get("/contact")
async def contact_page(request: Request):
    return templates.TemplateResponse("public/contact.html", _shared_context(request))


@router.get("/privacy-policy")
async def privacy_page(request: Request):
    return templates.TemplateResponse("public/privacy.html", _shared_context(request))


@router.get("/terms-and-conditions")
async def terms_page(request: Request):
    return templates.TemplateResponse("public/terms.html", _shared_context(request))
