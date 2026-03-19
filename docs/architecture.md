# Architecture Overview

## Runtime shape

- One FastAPI app process handles admin UI, JSON APIs, Twilio webhooks, health checks, and APScheduler jobs.
- PostgreSQL is the primary store for users, personas, conversations, messages, safety, scheduling, and prompt versions.
- `pgvector` is used on `memory_items.embedding_vector` for semantic retrieval in production; tests fall back to Python similarity.
- Twilio handles SMS/MMS transport and voice call initiation.
- OpenAI handles text generation, embeddings, images, and optional speech generation.

## Core flow

1. Twilio hits `/webhooks/twilio/sms`.
2. The app validates the signature, persists the inbound message and media metadata, then loads the effective config for the user + persona.
3. Safety checks run first.
4. Recent conversation plus retrieved memory are assembled into prompt context.
5. A reply is generated, validated, persisted, and sent through Twilio.
6. Memory extraction runs on the new user message and stores candidate durable facts.

## Safety layering

- Global defaults from `config/defaults.yaml`
- Environment overrides for deploy-time secrets and model/provider values
- DB `app_settings` overrides for runtime tuning
- Persona overrides for style/safety/prompt behavior
- User overrides for schedule/safety specifics

## Pi-oriented design choices

- Server-rendered admin, not a heavy SPA
- In-process scheduler, not separate workers
- Reused `httpx.AsyncClient`
- Lean provider integrations over HTTP instead of large SDK chains
- Memory consolidation jobs to cap prompt context growth
