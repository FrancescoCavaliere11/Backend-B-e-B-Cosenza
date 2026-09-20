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

import pytest
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
async def api_client(session_factory):
    """
    Client HTTP sull'applicazione reale, senza aprire una porta di rete.

    `ASGITransport` invoca l'app in-process: le richieste attraversano
    middleware, dipendenze, router ed exception handler esattamente come in
    produzione, ma senza server. La dipendenza `get_async_session` è sostituita
    per puntare al database di test, e ogni richiesta ottiene una sessione
    propria, come avviene davvero.

    I contatori del rate limiter vengono azzerati prima e dopo ogni test:
    vivono in memoria di processo e altrimenti un test si porterebbe dietro le
    richieste di quello precedente.
    """
    from httpx import ASGITransport, AsyncClient

    from src.config.database_config import get_async_session
    from src.main import app
    from src.security.rate_limiter import reset_rate_limiter

    async def override_session():
        async with session_factory() as db_session:
            yield db_session

    app.dependency_overrides[get_async_session] = override_session
    reset_rate_limiter()

    async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client

    app.dependency_overrides.clear()
    reset_rate_limiter()


@pytest.fixture(autouse=True)
def email_backend():
    """
    Sostituisce il canale email con uno che registra i messaggi in memoria.

    È `autouse` per due ragioni. La prima è igienica: nessun test deve toccare
    un server SMTP, nemmeno per sbaglio. La seconda è che rende *osservabile*
    una parte del sistema che altrimenti non lo sarebbe — senza questo backend
    l'unico modo di sapere se una email è partita sarebbe aprire una casella di
    posta, e un comportamento che non si può verificare in un test è un
    comportamento che prima o poi si rompe senza che nessuno se ne accorga.

    I test che non guardano le email non ne risentono; quelli che lo fanno
    ricevono la fixture e leggono `backend.messages`.

    È una fixture **sincrona** di proposito: non compie alcuna operazione
    asincrona, e resa `async` sarebbe inservibile per i test sincroni di
    `test_email_service.py`, che pure la ricevono essendo `autouse`.

    ⚠️ Le email partono da `BackgroundTasks`, cioè **dopo** che la risposta è
    stata prodotta. Con `ASGITransport` questo avviene comunque prima che
    `await client.post(...)` ritorni, quindi le asserzioni subito dopo la
    chiamata sono attendibili.
    """
    from src.service.email.backend import MemoryEmailBackend
    from src.service.email.email_service import configure_email_service, reset_email_service

    backend = MemoryEmailBackend()
    configure_email_service(backend)

    yield backend

    reset_email_service()


#: Password usata dagli utenti di prova. Rispetta i validatori del progetto:
#: maiuscola, minuscola, cifra e carattere speciale.
TEST_PASSWORD = "Password1!"


async def _create_user(session, email: str, phone: str, role) -> "User":
    from src.data.model.user import User
    from src.security.password_handler import get_password_hash

    user = User(
        firstname="Utente",
        lastname="Prova",
        email=email,
        phone_number=phone,
        password=get_password_hash(TEST_PASSWORD),
        role=role,
        created_by="System",
        last_updated_by="System",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_user(session):
    from src.data.enumerators import UserRole

    return await _create_user(session, "admin@example.com", "3330000001", UserRole.admin)


@pytest_asyncio.fixture
async def regular_user(session):
    from src.data.enumerators import UserRole

    return await _create_user(session, "utente@example.com", "3330000002", UserRole.user)


async def _login(client, email: str):
    """
    Esegue un login reale e lascia che il client conservi i cookie.

    Si passa dall'endpoint vero invece di sovrascrivere `get_current_user`:
    così i test attraversano l'intera catena di autenticazione e verificano
    anche che `is_admin_user` respinga davvero chi non è amministratore —
    cosa che con una dipendenza finta non sapremmo.
    """
    response = await client.post(
        "/api/v1/auth/token", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


@pytest_asyncio.fixture
async def admin_client(api_client, admin_user):
    """Client autenticato come amministratore."""
    return await _login(api_client, admin_user.email)


@pytest_asyncio.fixture
async def user_client(api_client, regular_user):
    """Client autenticato come utente normale. Serve a verificare i 403."""
    return await _login(api_client, regular_user.email)


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
