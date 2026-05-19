from typing import AsyncGenerator

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from backend.src.config.config import settings as s

DATABASE_URL = (
    f"postgresql+asyncpg://{s.db_user}:{s.db_password.get_secret_value()}@{s.db_host}:{s.db_port}/{s.db_name}"
)

class Base(DeclarativeBase):
    pass

engine = create_async_engine(
    DATABASE_URL,
    # 'echo=True' è utile in sviluppo per vedere le query SQL generate nei log.
    echo=True,
    future=True,
    # Ottimizzazione: pool_pre_ping aiuta a recuperare connessioni interrotte
    pool_pre_ping=True
)

async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Evita che gli oggetti diventino inutilizzabili dopo il commit
    autoflush=False
)

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()