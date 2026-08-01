"""Railway deploy bootstrap: create all tables in correct FK-dependency order.

Why this exists (voice-pro deploy):
voice-noob's alembic migration chain has multiple root revisions (e.g.
002_add_crm_models has down_revision=None alongside 001_initial), so
`alembic upgrade head` can run migrations out of order and fail with
"relation 'users' does not exist" when creating FK-dependent tables.

SQLAlchemy's metadata.create_all() topologically sorts tables by their
foreign keys, so parents (users) are always created before children
(contacts). It is idempotent (checkfirst=True) — existing tables are
skipped — so this is safe to run on every deploy.

This is additive deploy scaffolding: it uses voice-noob's OWN models and
does not modify any application logic.
"""

import asyncio
import importlib
import pkgutil

import app.models  # noqa: F401
from app.core.config import settings
from app.db.base import Base
from sqlalchemy.ext.asyncio import create_async_engine

# Import every module under app.models so all tables register on Base.metadata.
for _m in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{_m.name}")


# Columns added to a table AFTER it already existed in production. create_all()
# (checkfirst=True) skips existing tables entirely, so it never adds a new column
# to an old table — reconcile those here idempotently. Keep each entry additive and
# nullable so it is safe to run on every deploy.
COLUMN_RECONCILE = (
    ("call_records", "booking_attempts", "JSON"),
    ("call_records", "variables", "JSON"),
    # B2 transparency stack: the secret behind a public transcript link.
    # Alembic migration d4b2f7c1a903 adds it for local/dev; PRODUCTION goes
    # through create_all + this list, so it must be here too — otherwise every
    # transcript save would fail on a column that does not exist.
    ("call_records", "share_token", "VARCHAR(32)"),
    ("call_records", "media_grant_cv_sha256", "VARCHAR(64)"),
    ("call_records", "media_grant_expires_at", "TIMESTAMPTZ"),
    ("call_records", "media_grant_consumed_at", "TIMESTAMPTZ"),
    ("call_records", "media_finalized_at", "TIMESTAMPTZ"),
    ("call_records", "dial_attempt_id", "UUID"),
    ("call_records", "dial_request_sha256", "VARCHAR(64)"),
    ("call_records", "dial_attempt_state", "VARCHAR(32)"),
    ("call_records", "dial_attempt_result", "JSON"),
)

# Indexes on reconciled columns (create_all only builds indexes for tables it
# creates). UNIQUE so one token can never point at two calls.
INDEX_RECONCILE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_call_records_share_token "
    "ON call_records (share_token)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_call_records_dial_attempt_id "
    "ON call_records (dial_attempt_id) WHERE dial_attempt_id IS NOT NULL",
)


async def main() -> None:
    engine = create_async_engine(str(settings.DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, column, coltype in COLUMN_RECONCILE:
            await conn.exec_driver_sql(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}'
            )
        for statement in INDEX_RECONCILE:
            await conn.exec_driver_sql(statement)
    await engine.dispose()
    print(
        f"voice-pro bootstrap: create_all complete ({len(Base.metadata.tables)} tables); "
        f"reconciled {len(COLUMN_RECONCILE)} column(s), "
        f"{len(INDEX_RECONCILE)} index(es)"
    )


if __name__ == "__main__":
    asyncio.run(main())
