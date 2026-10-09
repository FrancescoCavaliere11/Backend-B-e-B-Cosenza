from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.config import settings
from src.config.logging_config import configure_logging
from src.routers.extra_service_router import extra_service_router
from src.routers.user_router import user_router
from src.routers.auth_router import auth_router
from src.routers.room_service_router import room_service_router
from src.routers.room_router import room_router
from src.routers.booking_router import booking_router
from src.routers.admin_booking_router import admin_booking_router
from src.routers.payment_router import payment_router
from src.exception.exception_handler import setup_exception_handler
from src.service.booking_expiration_service import build_expiration_service

from src.data.model.user import User
from src.data.model.room import Room
from src.data.model.booking import Booking
from src.data.model.room_service_association import room_service_association
from src.data.model.room_service import RoomService
from src.data.model.extra_service import ExtraService

# Entità del modulo Booking: l'import le registra nel registry SQLAlchemy
# prima della configurazione dei mapper.
from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.booking_token import BookingToken
from src.data.model.booking_status_history import BookingStatusHistory
from src.data.model.booking_payment_history import BookingPaymentHistory
from src.data.model.stripe_event import StripeEvent


# Prima di qualunque cosa: senza questa chiamata i logger del progetto non
# hanno un handler e ogni `logger.info` viene scartato in silenzio — uvicorn
# configura soltanto i propri. Vedi `logging_config.py`.
configure_logging()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    Avvio e arresto dei processi di background.

    Lo sweeper delle scadenze gira qui e non in un cron esterno perché
    condivide il pool di connessioni e la configurazione dell'applicazione, e
    perché un processo in meno da installare è un processo in meno da
    dimenticare acceso.

    Con più worker uvicorn ne parte uno per processo. Non è un problema: le
    passate si dividono il lavoro grazie al `FOR UPDATE ... SKIP LOCKED` di
    `get_expired_pending`.

    L'arresto attende la fine della passata in corso, così un riavvio non
    interrompe una transazione a metà.
    """
    # La composizione — sessioni e gateway Stripe — sta in
    # `build_expiration_service`, condivisa con l'endpoint amministrativo
    # `POST /admin/bookings/sweep-expired`. Due composizioni separate avevano
    # già prodotto una divergenza silenziosa: il giro automatico rilasciava le
    # autorizzazioni, quello manuale no.
    sweeper = build_expiration_service()

    if settings.sweeper_enabled:
        sweeper.start()

    try:
        yield
    finally:
        await sweeper.stop()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_exception_handler(app)

app.include_router(user_router)
app.include_router(auth_router)
app.include_router(room_service_router)
app.include_router(room_router)
app.include_router(extra_service_router)
app.include_router(booking_router)
app.include_router(admin_booking_router)
app.include_router(payment_router)
