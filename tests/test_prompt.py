from __future__ import annotations

from app.services.prompt import PromptService


async def test_prompt_service_renders_file_template(settings, monkeypatch):
    service = PromptService(settings)

    async def no_db_template(session, name):
        return None

    monkeypatch.setattr(service, "_load_db_template", no_db_template)
    rendered = await service.render(
        session=None,
        name="call_script",
        context={
            "user": type("User", (), {"display_name": "Sam", "timezone": "America/New_York"})(),
            "persona": type("Persona", (), {"display_name": "Rowan", "speech_style": "gentle"})(),
            "config": {},
            "opening_line": "Hello there",
        },
    )
    assert "Hello there" in rendered
    assert "Rowan" in rendered
