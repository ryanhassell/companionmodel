# Backup and Restore

## JSON export

```bash
uv run python -m scripts.export_backup var/backups/backup.json
```

## PostgreSQL dump

Preferred for production:

```bash
pg_dump -Fc companion > companion.dump
pg_restore -d companion companion.dump
```

## Files to preserve

- `var/media`
- `config/defaults.yaml`
- `.env`
- database backups

## Restore order

1. Restore Postgres.
2. Restore media files.
3. Restore config and env.
4. Run `alembic upgrade head` to ensure schema parity.
