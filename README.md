# Companion Pi

Companion Pi is a production-minded MVP+ AI companion platform for a vulnerable adult user who mainly interacts over SMS/MMS. It is designed for Raspberry Pi 4/5 orchestration with PostgreSQL + pgvector, Twilio for messaging and voice, and OpenAI for language, embeddings, image generation, and optional speech.

The system is intentionally configuration-heavy. Persona behavior, schedules, limits, prompts, models, quiet hours, phone numbers, disclosure policy, and operational thresholds are runtime-tunable through config, environment variables, database overrides, and the admin UI.

## What is included

- FastAPI application with async SQLAlchemy 2.x
- PostgreSQL + pgvector data model and Alembic migration
- Twilio inbound/outbound SMS, MMS, and voice foundations
- OpenAI chat, embeddings, image, and speech provider integrations
- APScheduler-based proactive outreach and maintenance jobs
- Multi-layer memory with semantic retrieval and admin inspection
- Server-rendered Pi-friendly admin dashboard
- Safety guardrails, distress escalation hooks, cooldown logic, and anti-dependency policy enforcement
- Prompt templating system with file-seeded and database-editable templates
- Docker, docker-compose, Makefile, docs, scripts, and pytest coverage

## Quick Start

1. Install `uv` and Docker, or run directly on Python 3.12.
2. Copy `.env.example` to `.env` and fill in secrets.
3. Review `config/defaults.yaml` and adjust defaults.
4. Start Postgres and the app:

```bash
docker compose up --build
```

5. Apply migrations:

```bash
uv run alembic upgrade head
```

6. Seed default prompts/persona/settings:

```bash
uv run python -m scripts.seed_defaults
```

7. Bootstrap the first admin account:

```bash
uv run python -m scripts.bootstrap_admin
```

8. Open `http://localhost:8000/admin`.

## Docs

- [Architecture](docs/architecture.md)
- [Raspberry Pi Setup](docs/raspberry-pi.md)
- [Twilio Setup](docs/twilio.md)
- [OpenAI Setup](docs/openai.md)
- [Postgres + pgvector](docs/postgres-pgvector.md)
- [Webhook Development](docs/webhook-dev.md)
- [Environment Reference](docs/env-reference.md)
- [Safety Model](docs/safety.md)
- [Admin Guide](docs/admin-guide.md)
- [Backup and Restore](docs/backup-restore.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Roadmap](docs/roadmap.md)

## Design Notes

- The companion can be warm, affectionate, playful, and supportive.
- The companion must not be romantic, sexual, coercive, exclusive, manipulative, or deceptive about being human when asked directly.
- The system never claims physical presence or in-person availability.
- Admin operators can tune disclosure style without permitting direct misrepresentation.
- Resource usage is intentionally modest: server-rendered admin, in-process scheduler, reusable HTTP clients, and low-complexity deployment.
