# Migration Notes

## Initial schema

- Revision: `20260318_0001`
- Creates core relational tables plus `memory_items.embedding_vector`
- Enables `pgvector`
- Adds IVFFLAT index for cosine similarity

## Safe changes going forward

- Add new prompt templates or prompt versions without destructive edits
- Prefer additive config keys
- Re-embed memories after embedding model changes
