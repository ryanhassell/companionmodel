from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.routers import public


class _FakeClerkAuthService:
    enabled = False


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()


async def test_public_pricing_page_mentions_additional_child_profiles(settings):
    app = FastAPI()
    app.include_router(public.router)
    app.state.container = _FakeContainer(settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/pricing")

    assert response.status_code == 200
    assert "Additional active child profiles are $12/month each." in response.text
    assert "Each household starts with one active child profile included." in response.text
    assert "additional credits can be purchased" in response.text.lower()
    assert "one active child profile, the parent portal, long-term memory, routines, safety visibility" in response.text
    assert "one active Resona for that child" in response.text
    assert "There is no separate extra-persona charge" in response.text


async def test_public_features_page_mentions_honest_pricing_and_extra_credits(settings):
    app = FastAPI()
    app.include_router(public.router)
    app.state.container = _FakeContainer(settings)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/features")

    assert response.status_code == 200
    assert "Honest pricing" in response.text
    assert "additional credits can be purchased" in response.text.lower()
    assert "The pricing is meant to be understandable, not sneaky." in response.text
    assert "One active Resona included for each active child profile." in response.text
