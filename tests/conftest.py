"""
Infrastruttura dei test che richiedono un database.

I test di concorrenza **devono** girare su PostgreSQL vero: `btree_gist` e gli
`EXCLUDE` constraint non esistono su SQLite, e senza di essi il test passerebbe
sempre, anche con l'anti-overbooking rotto.

Non possono nemmeno girare sul database di sviluppo. Il test di concorrenza è
progettato perché una delle due esecuzioni **fallisca**, e una prenotazione di
prova lasciata indietro con `is_active = true` occuperebbe fisicamente uno slot
in vendita.

Da qui la scelta di un database separato, che queste fixture **creano da sole**
alla prima esecuzione: non serve alcuna preparazione manuale. Il nome è
`settings.db_test_name`, oppure `<db_name>_test`.

Le fixture sono volutamente a scope *function*: lo schema viene ricreato a ogni
test. È più lento, ma evita del tutto i problemi di condivisione dell'event
loop fra fixture di scope diverso, che cambiano da una versione di
`pytest-asyncio` all'altra.
"""
from decimal import Decimal
from typing import List

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.config.config import settings
from src.config.database_config import Base

# Import necessari a popolare `Base.metadata` prima di create_all.
import src.data.model.user  # noqa: F401
import src.data.model.room  # noqa: F401
import src.data.model.room_service  # noqa: F401
import src.data.model.extra_service  # noqa: F401
import src.data.model.booking  # noqa: F401
import src.data.model.booking_room_item  # noqa: F401
import src.data.model.booking_token  # noqa: F401
import src.data.model.booking_status_history  # noqa: F401
import src.data.model.stripe_event  # noqa: F401

from src.data.model.room import Room

TEST_DB_NAME = settings.db_test_name or f"{settings.db_name}_test"

#: Tipi ENUM da rimuovere esplicitamente: se un'esecuzione precedente si è
#: interrotta a metà possono sopravvivere alle tabelle e far fallire create_all.
_ENUM_TYPES = (
    "booking_status",
    "booking_channel",
    "payment_option",
    "payment_status",
    "payment_method",
    "booking_token_purpose",
    "audit_actor_type",
    "userrole",
)


def _database_url(database_name: str) -> str:
    password = settings.db_password.get_secret_value()
    return (
        f"postgresql+asyncpg://{settings.db_user}:{password}"
        f"@{settings.db_host}:{settings.db_port}/{database_name}"
    )


async def _ensure_test_database() -> None:
    """
    Crea il database di test se non esiste.

    :raises RuntimeError: se il nome coincide con quello di sviluppo, oppure
        se l'utente non ha i permessi per creare database.
    """
    if TEST_DB_NAME == settings.db_name:
        raise RuntimeError(
            "Il database di test coincide con quello di sviluppo. "
            "Valorizza DB_TEST_NAME nel .env con un nome diverso."
        )

    admin_engine = create_async_engine(
        _database_url("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )

    try:
        async with admin_engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    except Exception as error:  # noqa: BLE001 - il messaggio deve essere azionabile
        raise RuntimeError(
            f"Impossibile creare automaticamente il database di test '{TEST_DB_NAME}'. "
            f"Creane uno a mano con:\n\n    CREATE DATABASE \"{TEST_DB_NAME}\";\n\n"
            f"Causa: {error}"
        ) from error
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture
async def engine():
    """Motore sul database di test, con schema ricreato da zero."""
    await _ensure_test_database()

    test_engine = create_async_engine(
        _database_url(TEST_DB_NAME), poolclass=NullPool, future=True, echo=False
    )

    async with test_engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await connection.run_sync(Base.metadata.drop_all)
        for enum_name in _ENUM_TYPES:
            await connection.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))
        await connection.run_sync(Base.metadata.create_all)

    yield test_engine

    await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def rooms(session) -> List[Room]:
    """Due camere di prova: una doppia a 100 €, una quadrupla a 140 €."""
    created = [
        Room(
            name="Camera Girasole",
            capacity=2,
            price=Decimal("100.00"),
            number=101,
            enabled=True,
            img_url="https://placeholder.test/girasole.jpg",
            created_by="System",
            last_updated_by="System",
        ),
        Room(
            name="Camera Lavanda",
            capacity=4,
            price=Decimal("140.00"),
            number=102,
            enabled=True,
            img_url="https://placeholder.test/lavanda.jpg",
            created_by="System",
            last_updated_by="System",
        ),
    ]

    session.add_all(created)
    await session.commit()

    for room in created:
        await session.refresh(room)

    return created
