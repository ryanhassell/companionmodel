from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.templating import templates
from app.schemas.site import MarketingFeatureCard, PublicSiteContext, SafetyCapabilityCard

router = APIRouter(tags=["public"])


def _shared_context(request: Request) -> dict[str, object]:
    container = request.app.state.container
    settings = container.settings
    context = PublicSiteContext(
        brand_name=settings.web.brand_name,
        canonical_domain=settings.web.canonical_domain,
        support_email=settings.web.support_email,
        privacy_url=settings.web.privacy_url,
        terms_url=settings.web.terms_url,
        safety_policy_url=settings.web.safety_policy_url,
    )
    return {
        "request": request,
        "site": context,
        "clerk_enabled": container.clerk_auth_service.enabled,
        "clerk_publishable_key": settings.clerk.publishable_key,
        "clerk_frontend_api_url": settings.clerk.frontend_api_url,
    }


@router.get("/")
async def home_page(request: Request):
    features = [
        MarketingFeatureCard(
            title="A companion that remembers",
            summary="Resona carries context from one conversation to the next, so it can remember routines, favorite topics, sensitivities, and important moments over time.",
            icon="timeline",
        ),
        MarketingFeatureCard(
            title="More like a familiar friend than a blank chatbot",
            summary="Instead of acting like a brand-new assistant every time, Resona is designed to feel steady, warm, and recognizable from day to day.",
            icon="shield",
        ),
        MarketingFeatureCard(
            title="Daily rhythm and routine awareness",
            summary="Resona can fit into everyday life with gentle check-ins, recurring patterns, and a more natural sense of timing.",
            icon="memory",
        ),
        MarketingFeatureCard(
            title="One relationship across text and voice",
            summary="A voice call does not reset the experience. The same personality, tone, and continuity carry across both formats.",
            icon="phone",
        ),
        MarketingFeatureCard(
            title="Parent visibility without guesswork",
            summary="Parents can see what was said, what mattered, what was remembered, and when safety support stepped in.",
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
            title="Supportive, not possessive",
            summary="Resona is designed to avoid unhealthy dependence and keep conversations grounded, appropriate, and family-safe.",
            icon="shield",
        ),
        SafetyCapabilityCard(
            title="Extra care when distress shows up",
            summary="If a conversation starts sounding more serious, Resona shifts into a more protective mode and surfaces that context for parents.",
            icon="alert",
        ),
        SafetyCapabilityCard(
            title="Clear parent visibility",
            summary="Parents are not left guessing. The portal shows when concern came up and how Resona responded.",
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
