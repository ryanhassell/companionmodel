# PostgreSQL and pgvector

## Docker path

`docker-compose.yml` uses `pgvector/pgvector:pg16`.

## Native path

Install PostgreSQL 16 and the `pgvector` extension package for your platform, then run:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The initial Alembic migration also attempts to create the extension.

## Memory table

- `memory_items.embedding_vector` stores the semantic vector.
- An IVFFLAT index is created for cosine search in Postgres.
- The app falls back to Python cosine similarity in non-Postgres test environments.

## Migration flow

```bash
uv run alembic upgrade head
```
