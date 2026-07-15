from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from upmovies.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Register every mapped model with `Base.metadata` on import. Because virtually all DB code
# imports `SessionLocal`/`Base` from here, this keeps metadata complete — and cross-schema
# foreign keys resolvable at flush time — even for standalone entrypoints (scripts, ad-hoc
# `python -`) that don't load the full app graph. Imported last, after `Base` is defined, to
# avoid the circular import (the model modules import `Base` from here).
from upmovies import models as _models  # noqa: E402, F401
