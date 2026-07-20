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
)


async def main() -> None:
    engine = create_async_engine(str(settings.DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, column, coltype in COLUMN_RECONCILE:
            await conn.exec_driver_sql(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}'
            )
    await engine.dispose()
    print(
        f"voice-pro bootstrap: create_all complete ({len(Base.metadata.tables)} tables); "
        f"reconciled {len(COLUMN_RECONCILE)} column(s)"
    )


if __name__ == "__main__":
    asyncio.run(main())
