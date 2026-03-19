# OpenAI Setup

## APIs used

- Responses API for text generation
- Embeddings API for memory vectors
- Images API for companion image generation
- Audio Speech API for future voice output

## Required values

- `OPENAI_API_KEY`
- Optional model overrides:
  - `OPENAI_CHAT_MODEL`
  - `OPENAI_EMBEDDING_MODEL`
  - `OPENAI_IMAGE_MODEL`
  - `OPENAI_SPEECH_MODEL`

## Current implementation notes

- Text generation uses `POST /v1/responses`.
- Embeddings use `POST /v1/embeddings`.
- Images use `POST /v1/images/generations`.
- Speech uses `POST /v1/audio/speech`.

## Operational advice

- Keep `OPENAI_CHAT_MODEL` modest for Pi/home use. The default is configurable and can be changed later without code edits.
- Re-embed memories after switching embedding models.
- Use admin prompt/template versioning before changing production prompt behavior.
