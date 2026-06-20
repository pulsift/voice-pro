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


async def main() -> None:
    engine = create_async_engine(str(settings.DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print(f"voice-pro bootstrap: create_all complete ({len(Base.metadata.tables)} tables)")


if __name__ == "__main__":
    asyncio.run(main())
