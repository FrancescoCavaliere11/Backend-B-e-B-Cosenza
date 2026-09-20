"""
API amministrative del modulo Booking.

Ogni rotta è protetta da `is_admin_user`: un utente autenticato ma con ruolo
`user` riceve `403`, uno non autenticato `401`.

Il provider `get_booking_service` è importato dal router pubblico invece di
essere riscritto: è la stessa composizione di repository e servizi, e due copie
divergerebbero alla prima dipendenza aggiunta.

Come nel router pubblico, nessun `try/except`: le eccezioni di dominio sono
tutte `AppException` e vengono tradotte dall'handler globale.
"""
from datetime import date
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.data.enumerators import BookingStatus
from src.data.model.user import User
from src.data.schemas.booking_schema import (
    AdminBookingCreateSchema,
    AdminBookingCreatedSchema,
    AdminBookingUpdateSchema,
    AdminPaymentRegistrationSchema,
    BookingExtendHoldSchema,
    BookingSchema,
    BookingSearchFiltersSchema,
    BookingStatusUpdateSchema,
    PaginatedBookingsSchema,
)
from src.routers.booking_router import get_booking_service
from src.security.authorization import is_admin_user
from src.service.booking_service import BookingService

admin_booking_router = APIRouter(
    prefix="/api/v1/admin/bookings",
    tags=["Admin · Booking"],
    dependencies=[Depends(is_admin_user)],
)


def get_search_filters(
        status: Optional[List[BookingStatus]] = Query(
            None, description="Uno o più stati; ripetere il parametro per filtrarne più di uno"
        ),
        date_from: Optional[date] = Query(None, description="Soggiorni che terminano dopo questa data"),
        date_to: Optional[date] = Query(None, description="Soggiorni che iniziano prima di questa data"),
        email: Optional[str] = Query(None, description="Email dell'ospite (confronto esatto)"),
        code: Optional[str] = Query(None, description="Codice prenotazione"),
        room_id: Optional[UUID] = Query(None, description="Prenotazioni che includono questa camera"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100)
) -> BookingSearchFiltersSchema:
    """
    Converte i parametri di query nello schema dei filtri.

    Costruire il modello qui fa sì che le validazioni — intervallo coerente,
    email ben formata, dimensione di pagina entro il limite — valgano anche su
    una `GET`, con gli stessi messaggi d'errore degli endpoint `POST`.
    """
    return BookingSearchFiltersSchema(
        status=status,
        date_from=date_from,
        date_to=date_to,
        email=email,
        code=code,
        room_id=room_id,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# Consultazione                                                                #
# --------------------------------------------------------------------------- #

@admin_booking_router.get(
    "/",
    response_model=PaginatedBookingsSchema,
    summary="Elenco filtrato e paginato delle prenotazioni",
)
async def list_bookings(
        filters: Annotated[BookingSearchFiltersSchema, Depends(get_search_filters)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> PaginatedBookingsSchema:
    """
    Ordinate per data di arrivo decrescente, poi per data di creazione.

    Le righe camera sono caricate con `selectinload`: una query aggiuntiva per
    l'intera pagina invece di una per prenotazione.
    """
    return await service.search(filters)


@admin_booking_router.get(
    "/{booking_id}",
    response_model=BookingSchema,
    summary="Dettaglio completo con timeline degli stati",
)
async def get_booking(
        booking_id: UUID,
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingSchema:
    """
    Vista completa: audit, canale di origine, note interne, riferimenti di
    pagamento e storico delle transizioni.

    Il campo `version` restituito qui va rimandato nella `PATCH`: è quello che
    impedisce a due operatori di sovrascriversi a vicenda.
    """
    return await service.get_admin_booking(booking_id)


# --------------------------------------------------------------------------- #
# Scrittura                                                                    #
# --------------------------------------------------------------------------- #

@admin_booking_router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminBookingCreatedSchema,
    summary="Crea una prenotazione per conto di terzi",
)
async def create_booking(
        payload: AdminBookingCreateSchema,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> AdminBookingCreatedSchema:
    """
    L'ospite può essere un utente registrato (`user_id`) **oppure** un profilo
    inserito a mano (`guest`), mai entrambi.

    Con `skip_email_confirmation` attivo — il default — la prenotazione nasce
    già `CONFIRMED` e non viene emesso alcun token: è il caso della
    prenotazione telefonica, dove l'identità è già stata verificata parlando
    con l'ospite. Disattivandolo si ottiene il flusso normale con conferma via
    email, e il token viene restituito secondo le stesse regole del canale
    pubblico.

    A differenza del canale pubblico non serve un preventivo firmato: l'admin è
    un attore fidato e il prezzo viene calcolato direttamente dal server.
    """
    result = await service.create_admin_booking(payload, current_user.id)
    return AdminBookingCreatedSchema(
        booking=result.booking, confirmation_token=result.confirmation_token
    )


@admin_booking_router.patch(
    "/{booking_id}",
    response_model=BookingSchema,
    summary="Modifica date, camere, ospiti, anagrafica o note",
)
async def update_booking(
        booking_id: UUID,
        payload: AdminBookingUpdateSchema,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingSchema:
    """
    Copre il caso più frequente del banco: l'ospite telefona per spostare il
    soggiorno o cambiare camera.

    `version` deve corrispondere al valore letto con `GET /{booking_id}`,
    altrimenti la risposta è `409`: significa che un altro operatore ha salvato
    nel frattempo e la modifica sovrascriverebbe il suo lavoro.

    Se cambiano date o camere il prezzo viene ricalcolato e le righe camera
    ricostruite. Lo spostamento su uno slot già occupato risponde `409`.
    Una prenotazione in stato terminale non è modificabile.
    """
    return await service.admin_update_booking(booking_id, payload, current_user.id)


@admin_booking_router.post(
    "/{booking_id}/status",
    response_model=BookingSchema,
    summary="Transizione di stato",
)
async def change_status(
        booking_id: UUID,
        payload: BookingStatusUpdateSchema,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingSchema:
    """
    Le transizioni ammesse sono quelle della macchina a stati; ogni altra
    risponde `409`.

    Valgono anche i controlli temporali: non si registra un arrivo prima della
    data di check-in, né una mancata presentazione prima che l'ospite fosse
    atteso, né un soggiorno concluso prima della partenza. Impediscono i refusi
    più comuni del back-office.

    La motivazione è obbligatoria per l'annullamento.
    """
    return await service.admin_change_status(booking_id, payload, current_user.id)


@admin_booking_router.post(
    "/{booking_id}/payment",
    response_model=BookingSchema,
    summary="Registra un incasso manuale",
)
async def register_payment(
        booking_id: UUID,
        payload: AdminPaymentRegistrationSchema,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingSchema:
    """
    Per gli incassi in struttura: contanti, POS, bonifico.

    I pagamenti online passano invece dal webhook Stripe (Step G) e non vanno
    registrati da qui.
    """
    return await service.admin_register_payment(booking_id, payload, current_user.id)


@admin_booking_router.post(
    "/{booking_id}/extend-hold",
    response_model=BookingSchema,
    summary="Proroga il blocco temporaneo",
)
async def extend_hold(
        booking_id: UUID,
        payload: BookingExtendHoldSchema,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> BookingSchema:
    """
    Concede più tempo a un ospite che sta completando la prenotazione.

    Solo su prenotazioni ancora in attesa. Se nel frattempo le camere sono
    state vendute a qualcun altro, la proroga viene respinta con `409`: il
    blocco non si può riattivare su uno slot ormai occupato.
    """
    return await service.admin_extend_hold(booking_id, payload, current_user.id)
