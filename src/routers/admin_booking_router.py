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
from datetime import date, datetime, timezone
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

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
    SweepResultSchema,
)
from src.routers.booking_router import schedule_creation_email, get_booking_service
from src.security.authorization import is_admin_user
from src.service.booking_expiration_service import dispatch_expiration_notices
from src.service.booking_service import BookingService
from src.service.email.email_service import get_email_service

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
        background: BackgroundTasks,
        current_user: Annotated[User, Depends(is_admin_user)],
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> AdminBookingCreatedSchema:
    """
    L'ospite può essere un utente registrato (`user_id`) **oppure** un profilo
    inserito a mano (`guest`), mai entrambi.

    Con `skip_email_confirmation` attivo — il default — la prenotazione nasce
    già `CONFIRMED`: è il caso della prenotazione telefonica, dove l'identità è
    già stata verificata parlando con l'ospite. Non c'è nulla da confermare,
    quindi all'ospite arriva direttamente il riepilogo, con il link di
    gestione. Disattivandolo si ottiene il flusso normale con conferma via
    email, e il token viene restituito secondo le stesse regole del canale
    pubblico.

    A differenza del canale pubblico non serve un preventivo firmato: l'admin è
    un attore fidato e il prezzo viene calcolato direttamente dal server.
    """
    result = await service.create_admin_booking(payload, current_user.id)

    # Stesso instradamento del canale pubblico: se c'è un token di conferma
    # parte l'invito a confermare, altrimenti il riepilogo di prenotazione
    # confermata. Una prenotazione presa al telefono resta comunque una
    # prenotazione di cui l'ospite deve avere traccia scritta.
    schedule_creation_email(background, result)

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

    TODO [email di modifica]: questo endpoint **non** avvisa l'ospite, a
      differenza dell'annullamento. Non è una dimenticanza: un messaggio di
      modifica deve dire *cosa* è cambiato rispetto a prima e se l'ospite debba
      fare qualcosa, e richiede quindi il confronto fra stato precedente e
      successivo — un lavoro a sé, non una riga in coda a questa funzione. Fino
      ad allora la comunicazione resta a carico di chi opera al banco, che
      comunque sta già parlando con l'ospite al telefono.
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
        background: BackgroundTasks,
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

    **L'annullamento avvisa l'ospite.** È l'unica transizione che genera una
    email: le altre riguardano il funzionamento interno della struttura
    (arrivo, partenza, mancata presentazione) e l'ospite le conosce già perché
    era presente. Un annullamento deciso al banco, invece, lui potrebbe non
    saperlo affatto.
    """
    booking = await service.admin_change_status(booking_id, payload, current_user.id)

    if booking.status == BookingStatus.CANCELLED:
        background.add_task(get_email_service().send_booking_cancelled, booking)

    return booking


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


# --------------------------------------------------------------------------- #
# Manutenzione                                                                 #
# --------------------------------------------------------------------------- #

@admin_booking_router.post(
    "/sweep-expired",
    response_model=SweepResultSchema,
    summary="Esegue subito lo sweeper delle prenotazioni scadute",
)
async def sweep_expired(
        service: Annotated[BookingService, Depends(get_booking_service)],
) -> SweepResultSchema:
    """
    Porta a `EXPIRED` le prenotazioni temporanee con blocco scaduto, senza
    attendere il giro automatico.

    Serve a due cose: collaudare il meccanismo senza stare quindici minuti a
    guardare l'orologio, e avere una leva operativa quando lo sweeper in
    background è disattivato (`sweeper_enabled = false`) perché si preferisce
    pilotarlo da uno scheduler esterno.

    Non è un'operazione distruttiva né rischiosa: libera slot che il sistema
    considera già liberi. Eseguirla due volte di fila non cambia nulla, perché
    la seconda passata non trova più prenotazioni in attesa scadute.

    Le email partono **dopo** il commit, come nel giro automatico.
    """
    notices = await service.expire_pending()
    notified = await dispatch_expiration_notices(notices, get_email_service())

    return SweepResultSchema(
        expired_count=len(notices),
        notified_count=notified,
        swept_at=datetime.now(timezone.utc),
    )
