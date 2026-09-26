"""
API pubbliche e utente del modulo Booking.

Il flusso pubblico è a due passi per costruzione: `POST /quote` calcola il
prezzo e lo restituisce firmato, `POST /` accetta solo quel preventivo. Il
client non invia mai un importo.

`GET /availability` è l'unico endpoint con parametri in query: è una ricerca,
idempotente e condivisibile come link. Gli altri sono `POST` anche quando
leggono (`/lookup`, `/confirm`), perché portano token o dati identificativi che
non devono finire negli access log, nell'header `Referer` o nella cronologia
del browser.

**Ordine delle protezioni** su `POST /`: rate limit per IP, rate limit per
email, captcha, e solo allora la logica di prenotazione. Le verifiche più
economiche per prime, così una richiesta automatizzata viene respinta senza
toccare il database.

Nessun `try/except` in questo modulo: le eccezioni di dominio derivano tutte da
`AppException` e vengono tradotte in risposta HTTP dall'handler globale.
"""
from datetime import date
from typing import Annotated, List
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.config import settings
from src.config.database_config import get_async_session
from src.data.enumerators import BookingStatus
from src.data.model.user import User
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import (
    AvailabilityRequestSchema,
    AvailabilityResponseSchema,
    BookingCancelSchema,
    BookingConfirmSchema,
    BookingCreatedSchema,
    BookingLookupSchema,
    BookingPublicSchema,
    BookingQuoteRequestSchema,
    BookingQuoteResponseSchema,
    GuestBookingCreateSchema,
    OwnBookingCancelSchema,
    UserBookingCreateSchema,
)
from src.security.authentication import get_current_user
from src.security.captcha import verify_captcha
from src.security.rate_limiter import (
    availability_rate_limit,
    booking_confirm_rate_limit,
    booking_create_rate_limit,
    booking_lookup_rate_limit,
    client_ip,
    enforce_email_rate_limit,
    quote_rate_limit,
)
from src.service.availability_service import AvailabilityService
from src.service.booking_service import BookingCreationResult, BookingService
from src.service.email.email_service import get_email_service
from src.service.pricing_service import PricingService

booking_router = APIRouter(prefix="/api/v1/bookings", tags=["Booking"])


# --------------------------------------------------------------------------- #
# Provider di dipendenze                                                       #
# --------------------------------------------------------------------------- #

async def get_booking_service(db: AsyncSession = Depends(get_async_session)) -> BookingService:
    return BookingService(
        session=db,
        booking_repository=BookingRepository(db),
        booking_token_repository=BookingTokenRepository(db),
        booking_status_history_repository=BookingStatusHistoryRepository(db),
        room_repository=RoomRepository(db),
        pricing_service=PricingService(),
    )


async def get_availability_service(
        db: AsyncSession = Depends(get_async_session)
) -> AvailabilityService:
    return AvailabilityService(
        room_repository=RoomRepository(db),
        booking_repository=BookingRepository(db),
        pricing_service=PricingService(),
    )


def get_availability_request(
        check_in: date = Query(..., description="Data di arrivo (YYYY-MM-DD)"),
        check_out: date = Query(..., description="Data di partenza, esclusa dal soggiorno"),
        guest_count: int = Query(1, ge=1, description="Numero di ospiti")
) -> AvailabilityRequestSchema:
    """
    Converte i parametri di query nello schema di ricerca.

    Costruire il modello qui fa sì che le regole di soggiorno (notti minime e
    massime, anticipo, date nel passato) valgano anche per una `GET`, con lo
    stesso messaggio d'errore degli endpoint `POST`.
    """
    return AvailabilityRequestSchema(
        check_in=check_in, check_out=check_out, guest_count=guest_count
    )


def _to_created_response(result: BookingCreationResult) -> BookingCreatedSchema:
    """
    Compone la risposta di creazione.

    Il token di conferma viene esposto **solo se l'invio email è disattivato**.
    Con `email_enabled` attivo il campo si spegne da sé e il token viaggia
    unicamente nel link dell'email.
    """
    token = result.confirmation_token if not settings.email_enabled else None
    return BookingCreatedSchema(booking=result.booking, confirmation_token=token)


def schedule_creation_email(
        background: BackgroundTasks,
        result: BookingCreationResult
) -> None:
    """
    Accoda l'email che segue una creazione.

    **Perché `BackgroundTasks` e non una chiamata diretta.** Spedire dentro la
    transazione sbaglia in entrambe le direzioni: se la mail parte e poi la
    transazione fa rollback, l'ospite riceve la conferma di una prenotazione
    che non esiste; se invece si sollevasse per un errore SMTP, si perderebbe
    anche la prenotazione. `BackgroundTasks` esegue dopo che la risposta è
    stata prodotta, quindi dopo il commit di `_in_transaction` — per
    costruzione, non per fortuna.

    Il `BookingService` resta così logica di dominio pura e non sa nulla di
    SMTP, coerentemente con la decisione #20: l'invio è un fatto di canale.

    ⚠️ Non è un outbox pattern: se il processo muore fra il commit e l'invio,
    quella email è persa e nessuno se ne accorge. È un debito accettato
    consapevolmente, perché ogni messaggio di questo modulo è ricostruibile a
    mano dal back-office.
    """
    email_service = get_email_service()

    if result.confirmation_token:
        background.add_task(
            email_service.send_booking_pending,
            result.booking,
            result.confirmation_token,
        )
    elif result.booking.status == BookingStatus.CONFIRMED:
        # Nasce già confermata: pagamento online andato a buon fine, o
        # prenotazione presa al telefono. L'ospite riceve subito il riepilogo,
        # con il link di gestione.
        background.add_task(
            email_service.send_booking_confirmed,
            result.booking,
            result.manage_token,
        )
    # Resta il caso `PENDING_PAYMENT`: nessuna email, ed è voluto. L'ospite è
    # ancora sulla pagina di pagamento e non c'è niente da dirgli — annunciargli
    # una prenotazione che il pagamento potrebbe far fallire sarebbe peggio del
    # silenzio. La notifica parte quando il webhook Stripe conferma l'incasso
    # (Step G).


# --------------------------------------------------------------------------- #
# Endpoint pubblici                                                            #
# --------------------------------------------------------------------------- #

@booking_router.get(
    "/availability",
    response_model=AvailabilityResponseSchema,
    dependencies=[Depends(availability_rate_limit)],
    summary="Camere disponibili nell'intervallo richiesto",
)
async def get_availability(
        request: Annotated[AvailabilityRequestSchema, Depends(get_availability_request)],
        service: Annotated[AvailabilityService, Depends(get_availability_service)],
) -> AvailabilityResponseSchema:
    """
    Restituisce tutte le camere libere, senza filtrare per capienza.

    Serve le tre modalità di prenotazione con una sola chiamata: camera singola
    sufficiente (`fits_all_guests`), scelta manuale (`rooms`), combinazioni
    suggerite (`suggested_combinations`).
    """
    return await service.search(
        check_in=request.check_in,
        check_out=request.check_out,
        guest_count=request.guest_count,
    )


@booking_router.post(
    "/quote",
    response_model=BookingQuoteResponseSchema,
    dependencies=[Depends(quote_rate_limit)],
    summary="Preventivo firmato per una selezione di camere",
)
async def create_quote(
        payload: BookingQuoteRequestSchema,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingQuoteResponseSchema:
    """
    Primo dei due passi obbligatori della prenotazione pubblica.

    Il `quote_token` restituito va presentato a `POST /`: è l'unico modo in cui
    un prezzo può entrare nel sistema.
    """
    return await service.build_quote(payload)


@booking_router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingCreatedSchema,
    dependencies=[Depends(booking_create_rate_limit)],
    summary="Crea una prenotazione come ospite non registrato",
)
async def create_guest_booking(
        payload: GuestBookingCreateSchema,
        request: Request,
        background: BackgroundTasks,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingCreatedSchema:
    await enforce_email_rate_limit(str(payload.guest.email))
    await verify_captcha(payload.captcha_token, client_ip(request))

    result = await service.create_guest_booking(payload)
    schedule_creation_email(background, result)
    return _to_created_response(result)


@booking_router.post(
    "/confirm",
    response_model=BookingPublicSchema,
    dependencies=[Depends(booking_confirm_rate_limit)],
    summary="Conferma una prenotazione tramite il token ricevuto per email",
)
async def confirm_booking(
        payload: BookingConfirmSchema,
        background: BackgroundTasks,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingPublicSchema:
    """
    La conferma emette anche il **token di gestione**, che viaggia nell'email
    di riepilogo: per un ospite non registrato è l'unico modo di annullare in
    autonomia, perché non ha un account e la consultazione con codice ed email
    è in sola lettura.
    """
    result = await service.confirm_booking(payload.token)

    background.add_task(
        get_email_service().send_booking_confirmed,
        result.booking,
        result.manage_token,
    )
    return result.booking


@booking_router.post(
    "/cancel",
    response_model=BookingPublicSchema,
    dependencies=[Depends(booking_confirm_rate_limit)],
    summary="Cancella una prenotazione tramite il link ricevuto per email",
)
async def cancel_booking(
        payload: BookingCancelSchema,
        background: BackgroundTasks,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingPublicSchema:
    """
    Il token accettato qui è quello di **gestione**, recapitato con l'email di
    conferma. Fino allo Step F nessun percorso di codice lo emetteva: questo
    endpoint esisteva, ma nessun client poteva procurarsi la credenziale che
    richiede.
    """
    booking = await service.cancel_by_token(payload.token, payload.reason)

    background.add_task(get_email_service().send_booking_cancelled, booking)
    return booking


@booking_router.post(
    "/lookup",
    response_model=BookingPublicSchema,
    dependencies=[Depends(booking_lookup_rate_limit)],
    summary="Consulta una prenotazione con codice ed email",
)
async def lookup_booking(
        payload: BookingLookupSchema,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingPublicSchema:
    """
    Richiede codice **e** email: il solo codice non basta, per evitare che
    tentativi a forza bruta espongano i dati di altri ospiti.
    """
    return await service.lookup(payload.code, str(payload.email))


# --------------------------------------------------------------------------- #
# Endpoint per utenti autenticati                                              #
# --------------------------------------------------------------------------- #

''' @booking_router.get(
    "/me",
    response_model=List[BookingPublicSchema],
    summary="Le mie prenotazioni",
)
async def get_my_bookings(
        current_user: Annotated[User, Depends(get_current_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
) -> List[BookingPublicSchema]:
    return await service.get_user_bookings(current_user.id, limit, offset)


@booking_router.post(
    "/me",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingCreatedSchema,
    summary="Crea una prenotazione come utente autenticato",
)
async def create_user_booking(
        payload: UserBookingCreateSchema,
        background: BackgroundTasks,
        current_user: Annotated[User, Depends(get_current_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingCreatedSchema:
    """
    Nessun captcha e nessun limite per email: la sessione autenticata è già una
    barriera contro l'automazione, e l'anagrafica viene copiata dal profilo.
    """
    result = await service.create_user_booking(payload, current_user)
    schedule_creation_email(background, result)
    return _to_created_response(result) 

@booking_router.post(
    "/me/{code}/cancel",
    response_model=BookingPublicSchema,
    summary="Cancella una mia prenotazione",
)
async def cancel_my_booking(
        code: str,
        payload: OwnBookingCancelSchema,
        background: BackgroundTasks,
        current_user: Annotated[User, Depends(get_current_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingPublicSchema:
    """
    La prenotazione è identificata dal **codice** (es. `BB-2026-A7K3QX`), non
    dall'identificativo interno: `BookingPublicSchema` non espone `id`, quindi
    un client non avrebbe modo di procurarselo. Il codice è l'unico riferimento
    pubblico di una prenotazione.

    Una prenotazione di un altro utente risponde `404`, non `403`: distinguere
    i due casi confermerebbe l'esistenza del codice e consentirebbe di sondare
    le prenotazioni altrui.
    """
    booking = await service.cancel_own_booking(code, current_user, payload.reason)

    background.add_task(get_email_service().send_booking_cancelled, booking)
    return booking '''
