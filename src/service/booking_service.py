"""
Logica di business delle prenotazioni.

È il cuore del modulo: orchestra repository, applica la macchina a stati,
gestisce il locking a tre livelli e mantiene l'invariante da cui dipende
l'exclusion constraint del database.

**Confine transazionale.** Ogni operazione di scrittura vive dentro una sola
transazione (`async with self.session.begin()`). I repository del modulo non
committano mai: se lo facessero, fra un commit e l'altro un'altra richiesta si
infilerebbe e il lock pessimistico non servirebbe a nulla.

**Il punto delicato è `_sync_items_active_flag`.** `BookingRoomItem.is_active`
è il predicato dell'`EXCLUDE` constraint: se resta `true` su una prenotazione
scaduta, lo slot rimane bloccato per sempre; se diventa `false` su una
confermata, la camera si vende due volte. Quel metodo è l'unico punto in cui
il flag viene scritto, e ogni transizione di stato ci passa.
"""
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple, Union
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.config import settings
from src.data.enumerators import (
    OCCUPYING_BOOKING_STATUSES,
    PENDING_BOOKING_STATUSES,
    AuditActorType,
    BookingChannel,
    BookingStatus,
    BookingTokenPurpose,
    GuestCancellationBlock,
    PaymentMethod,
    PaymentOption,
    PaymentStatus,
)
from src.data.model.booking import Booking
from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.booking_token import BookingToken
from src.data.model.room import Room
from src.data.model.user import User
from src.data.repository.booking_repository import BookingRepository
from src.data.integrity_errors import is_overlap_violation
from src.data.repository.booking_payment_history_repository import BookingPaymentHistoryRepository
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import (
    AdminBookingCreateSchema,
    AdminPaymentRegistrationSchema,
    BookingExtendHoldSchema,
    BookingListItemSchema,
    BookingManageSchema,
    BookingPublicSchema,
    BookingQuoteRequestSchema,
    BookingQuoteResponseSchema,
    BookingRoomItemSchema,
    BookingSchema,
    BookingSearchFiltersSchema,
    BookingPaymentHistorySchema,
    BookingStatusHistorySchema,
    BookingStatusUpdateSchema,
    GuestCancellationPolicySchema,
    GuestBookingCreateSchema,
    PaginatedBookingsSchema,
    UserBookingCreateSchema,
)
from src.exception.custom_exception import (
    AppException,
    BookingHoldExpired,
    BookingNotCancellable,
    EntityNotFound,
    InvalidBookingStatusTransition,
    InvalidBookingToken,
    InvalidGuestCount,
    InvalidPaymentOperation,
    InvalidQuoteToken,
    PaymentRequired,
    RateLimitExceeded,
    RoomNotAvailable,
    StatusChangeReasonRequired,
)
from src.security.audit_logging import apply_audit_fields
from src.security.booking_tokens import generate_booking_token, hash_booking_token
from src.security.quote_token import QuotePayload
from src.security.validators import today_in_app_timezone
from src.service.bookable_rooms import load_bookable_rooms
from src.service.pricing_service import PricingService
from src.service.transaction import run_in_transaction

logger = logging.getLogger(__name__)

#: Transizioni ammesse dalla macchina a stati. Unica fonte di verità:
#: qualunque cambio di stato passa da qui.
ALLOWED_TRANSITIONS: Dict[BookingStatus, FrozenSet[BookingStatus]] = {
    BookingStatus.PENDING_CONFIRMATION: frozenset({
        BookingStatus.CONFIRMED,
        BookingStatus.EXPIRED,
        BookingStatus.CANCELLED,
    }),
    BookingStatus.PENDING_PAYMENT: frozenset({
        BookingStatus.CONFIRMED,
        BookingStatus.EXPIRED,
        BookingStatus.CANCELLED,
    }),
    BookingStatus.CONFIRMED: frozenset({
        BookingStatus.CHECKED_IN,
        BookingStatus.CANCELLED,
        BookingStatus.NO_SHOW,
    }),
    BookingStatus.CHECKED_IN: frozenset({BookingStatus.COMPLETED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.EXPIRED: frozenset(),
    BookingStatus.NO_SHOW: frozenset(),
}

#: Stati in cui il back-office può registrare un incasso in struttura: il
#: soggiorno è confermato, in corso o concluso (anche un no-show può saldare
#: quanto dovuto). Mai su una prenotazione in attesa, annullata o scaduta.
_PAYABLE_ON_SITE_STATUSES: FrozenSet[BookingStatus] = frozenset({
    BookingStatus.CONFIRMED,
    BookingStatus.CHECKED_IN,
    BookingStatus.COMPLETED,
    BookingStatus.NO_SHOW,
})

#: Alfabeto del codice prenotazione: niente 0/O né 1/I/L, che al telefono si
#: confondono. 31^6 ≈ 887 milioni di combinazioni.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_CODE_MAX_ATTEMPTS = 10

#: Marcatore di audit per le prenotazioni create da ospiti non registrati.
_GUEST_AUDIT_MARKER = "GUEST"
_SYSTEM_AUDIT_MARKER = "System"

#: Motivazione delle voci di storico scritte dagli esiti di Stripe.
_STRIPE_PAYMENT_REASONS: Dict[PaymentStatus, str] = {
    PaymentStatus.FAILED: "Pagamento online rifiutato",
    PaymentStatus.REFUNDED: "Rimborso emesso su Stripe",
}



@dataclass
class BookingCreationResult:
    """
    Esito di una creazione o di una conferma.

    I due token sono valori **in chiaro**: nel database esiste solo il loro
    hash, non vengono mai registrati nei log e vivono giusto il tempo di
    entrare nel link di un'email.

    `confirmation_token` serve a confermare una prenotazione temporanea. È
    `None` quando la conferma non serve: creazione dal back-office o pagamento
    online, dove la verifica d'identità è il pagamento stesso.

    `manage_token` è il link di gestione, e in particolare **l'unico modo che
    un ospite non registrato ha di annullare**: non possiede un account, e la
    consultazione con codice ed email è in sola lettura. Viene emesso quando la
    prenotazione diventa confermata — alla conferma via email, oppure subito se
    nasce già confermata.
    """

    booking: Union[BookingPublicSchema, BookingSchema]
    confirmation_token: Optional[str] = None
    manage_token: Optional[str] = None


class PaymentOutcome(str, Enum):
    """
    Esito della verifica che precede l'incasso.

    Esiste perché incassare non è la conseguenza automatica
    dell'autorizzazione: fra i due momenti il mondo può essere cambiato, e le
    risposte possibili sono più di "sì" e "no".
    """

    #: La camera è ancora dell'ospite: si può incassare.
    CAPTURE = "CAPTURE"
    #: Già confermata da una consegna precedente della stessa notifica.
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    #: L'importo autorizzato non corrisponde al totale. Non si incassa.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    #: Nessuna prenotazione risulta associata a quel Payment Intent.
    UNKNOWN_BOOKING = "UNKNOWN_BOOKING"
    #: La prenotazione non attende più un pagamento: annullata dal
    #: back-office o scaduta mentre l'ospite era sulla pagina di pagamento.
    #: Non si incassa e lo slot non si riprende.
    NOT_PAYABLE = "NOT_PAYABLE"


@dataclass
class PaymentAuthorizationResult:
    """Esito della verifica, con il codice prenotazione per log ed email."""

    outcome: PaymentOutcome
    booking_code: Optional[str] = None


@dataclass
class PaymentContext:
    """Dati necessari a creare il Payment Intent, estratti dalla prenotazione."""

    code: str
    total_price: Decimal
    currency: str
    guest_email: str
    existing_intent_id: Optional[str] = None


@dataclass
class PendingPaymentRef:
    """Riferimento leggero a un'autorizzazione che lo sweeper deve annullare."""

    booking_id: UUID
    code: str
    payment_intent_id: str


@dataclass(frozen=True)
class GuestCancellationPolicy:
    """
    Esito della verifica sulla cancellazione da parte dell'ospite.

    Oggetto di dominio, non DTO: `describe_guest_cancellation` lo restituisce
    a due chiamanti con esigenze opposte — `_assert_guest_can_cancel`, che lo
    trasforma in eccezione, e la lettura del link di gestione, che lo
    trasforma in risposta. Nessuno dei due conosce le regole.
    """

    can_cancel: bool
    blocked_by: Optional[GuestCancellationBlock] = None
    #: Testo già scritto per l'ospite. È lo stesso che finisce nel `409` di
    #: `POST /cancel`, ed è deliberato: se la pagina di gestione annunciasse
    #: una cosa e l'endpoint ne dicesse un'altra, la colpa sarebbe di due
    #: testi scritti in due posti.
    message: Optional[str] = None
    free_until: Optional[datetime] = None


@dataclass
class ExpiredBookingNotice:
    """
    Una prenotazione portata a `EXPIRED` dallo sweeper.

    `notify` distingue le scadenze appena avvenute da quelle recuperate con
    ritardo: se lo sweeper è rimasto fermo, gli stati vanno comunque sistemati,
    ma avvisare l'ospite di una richiesta dimenticata giorni fa è inutile per
    lui e dannoso per la reputazione del mittente.
    """

    booking: BookingPublicSchema
    notify: bool


class BookingService:
    def __init__(
            self,
            session: AsyncSession,
            booking_repository: BookingRepository,
            booking_token_repository: BookingTokenRepository,
            booking_status_history_repository: BookingStatusHistoryRepository,
            booking_payment_history_repository: BookingPaymentHistoryRepository,
            room_repository: RoomRepository,
            pricing_service: PricingService
    ) -> None:
        self.session = session
        self.booking_repository = booking_repository
        self.token_repository = booking_token_repository
        self.history_repository = booking_status_history_repository
        self.payment_history_repository = booking_payment_history_repository
        self.room_repository = room_repository
        self.pricing_service = pricing_service

    # ================================================================== #
    # Preventivo                                                         #
    # ================================================================== #

    async def build_quote(
            self,
            payload: BookingQuoteRequestSchema
    ) -> BookingQuoteResponseSchema:
        """
        Preventivo firmato per una selezione di camere.

        Operazione di sola lettura: non blocca nulla e non crea righe. La
        disponibilità viene comunque verificata, per dare all'utente un errore
        immediato invece di farglielo scoprire dopo aver compilato i propri
        dati — ma la garanzia resta al momento della prenotazione: fra il
        preventivo e la conferma qualcun altro può sempre arrivare prima.

        Vive qui e non in `AvailabilityService` per riusare le stesse verifiche
        su camere e capienza del percorso di creazione: due implementazioni che
        col tempo divergono sarebbero peggio di una sola condivisa.
        """
        rooms = await self._load_and_validate_rooms(payload.room_ids)
        self._assert_capacity(rooms, payload.guest_count)

        conflicts = await self.booking_repository.get_active_overlapping_items(
            [room.id for room in rooms], payload.check_in, payload.check_out
        )
        if conflicts:
            raise RoomNotAvailable(self._describe_conflicts(conflicts))

        return self.pricing_service.build_quote(
            rooms=rooms,
            check_in=payload.check_in,
            check_out=payload.check_out,
            guest_count=payload.guest_count,
            payment_option=payload.payment_option,
        )

    # ================================================================== #
    # Creazione                                                          #
    # ================================================================== #

    async def create_guest_booking(
            self,
            payload: GuestBookingCreateSchema
    ) -> BookingCreationResult:
        """Prenotazione da parte di un ospite non registrato (endpoint pubblico)."""
        quote = self.pricing_service.verify_quote(payload.quote_token)

        # Pagando online, il possesso della carta è una verifica d'identità più
        # forte di un clic su un link: si salta la conferma via email e si
        # attende l'incasso.
        pay_now = quote.payment_option == PaymentOption.PAY_NOW
        initial_status = (
            BookingStatus.PENDING_PAYMENT if pay_now else BookingStatus.PENDING_CONFIRMATION
        )

        return await self._in_transaction(
            lambda: self._execute_create(
                quote=quote,
                guest_firstname=payload.guest.firstname,
                guest_lastname=payload.guest.lastname,
                guest_email=str(payload.guest.email),
                guest_phone=payload.guest.phone_number,
                user_id=None,
                channel=BookingChannel.PUBLIC_GUEST,
                actor_type=AuditActorType.GUEST,
                audit_marker=_GUEST_AUDIT_MARKER,
                audit_user_id=None,
                initial_status=initial_status,
                payment_status=PaymentStatus.PENDING,
                payment_method=PaymentMethod.STRIPE_CARD if pay_now else None,
                admin_notes=None,
                with_confirmation_token=not pay_now,
                enforce_pending_limit=True,
                admin_view=False,
            )
        )

    async def create_user_booking(
            self,
            payload: UserBookingCreateSchema,
            user: User
    ) -> BookingCreationResult:
        """
        Prenotazione da parte di un utente autenticato.

        L'anagrafica viene copiata dal profilo per costruire lo snapshot: da
        quel momento la prenotazione non segue più eventuali modifiche
        all'account.
        """
        quote = self.pricing_service.verify_quote(payload.quote_token)

        pay_now = quote.payment_option == PaymentOption.PAY_NOW
        initial_status = (
            BookingStatus.PENDING_PAYMENT if pay_now else BookingStatus.PENDING_CONFIRMATION
        )

        return await self._in_transaction(
            lambda: self._execute_create(
                quote=quote,
                guest_firstname=user.firstname,
                guest_lastname=user.lastname,
                guest_email=user.email,
                guest_phone=user.phone_number,
                user_id=user.id,
                channel=BookingChannel.PUBLIC_USER,
                actor_type=AuditActorType.USER,
                audit_marker=None,
                audit_user_id=user.id,
                initial_status=initial_status,
                payment_status=PaymentStatus.PENDING,
                payment_method=PaymentMethod.STRIPE_CARD if pay_now else None,
                admin_notes=None,
                with_confirmation_token=not pay_now,
                enforce_pending_limit=True,
                admin_view=False,
            )
        )

    async def create_admin_booking(
            self,
            payload: AdminBookingCreateSchema,
            admin_id: UUID
    ) -> BookingCreationResult:
        """
        Prenotazione creata dal back-office per conto di terzi.

        Non passa dal preventivo firmato: l'admin è un attore fidato e il
        prezzo viene calcolato direttamente qui. Può nascere già confermata,
        saltando la verifica via email.
        """
        return await self._in_transaction(
            lambda: self._execute_create_admin(payload, admin_id)
        )

    async def _execute_create_admin(
            self,
            payload: AdminBookingCreateSchema,
            admin_id: UUID
    ) -> BookingCreationResult:
        rooms = await self._load_and_validate_rooms(payload.room_ids)
        self._assert_capacity(rooms, payload.guest_count)

        nights = self.pricing_service.calculate_nights(payload.check_in, payload.check_out)
        lines = self.pricing_service.build_price_lines(rooms, nights)
        base_price, discount_amount, total_price = self.pricing_service.compute_totals(
            lines, payload.payment_option
        )

        # Preventivo sintetico: riusa lo stesso percorso di creazione degli
        # altri canali senza duplicare la logica.
        quote = QuotePayload(
            check_in=payload.check_in,
            check_out=payload.check_out,
            guest_count=payload.guest_count,
            room_ids=list(payload.room_ids),
            payment_option=payload.payment_option,
            base_price=base_price,
            discount_amount=discount_amount,
            total_price=total_price,
            currency=settings.default_currency,
        )

        guest_firstname = payload.guest.firstname if payload.guest else None
        guest_lastname = payload.guest.lastname if payload.guest else None
        guest_email = str(payload.guest.email) if payload.guest else None
        guest_phone = payload.guest.phone_number if payload.guest else None

        if payload.user_id is not None:
            user = await self.session.get(User, payload.user_id)
            if user is None:
                raise EntityNotFound("L'utente indicato non esiste")
            guest_firstname = user.firstname
            guest_lastname = user.lastname
            guest_email = user.email
            guest_phone = user.phone_number

        initial_status = (
            BookingStatus.CONFIRMED
            if payload.skip_email_confirmation
            else BookingStatus.PENDING_CONFIRMATION
        )
        payment_status = PaymentStatus.PAID if payload.mark_as_paid else PaymentStatus.PENDING

        return await self._execute_create(
            quote=quote,
            guest_firstname=guest_firstname,
            guest_lastname=guest_lastname,
            guest_email=guest_email,
            guest_phone=guest_phone,
            user_id=payload.user_id,
            channel=BookingChannel.ADMIN_BACKOFFICE,
            actor_type=AuditActorType.ADMIN,
            audit_marker=None,
            audit_user_id=admin_id,
            initial_status=initial_status,
            payment_status=payment_status,
            payment_method=payload.payment_method,
            admin_notes=payload.admin_notes,
            with_confirmation_token=not payload.skip_email_confirmation,
            enforce_pending_limit=False,
            admin_view=True,
        )

    async def _execute_create(
            self,
            *,
            quote: QuotePayload,
            guest_firstname: str,
            guest_lastname: str,
            guest_email: str,
            guest_phone: str,
            user_id: Optional[UUID],
            channel: BookingChannel,
            actor_type: AuditActorType,
            audit_marker: Optional[str],
            audit_user_id: Optional[UUID],
            initial_status: BookingStatus,
            payment_status: PaymentStatus,
            payment_method: Optional[PaymentMethod],
            admin_notes: Optional[str],
            with_confirmation_token: bool,
            enforce_pending_limit: bool,
            admin_view: bool
    ) -> BookingCreationResult:
        """
        Percorso di creazione condiviso da tutti i canali.

        L'ordine dei passi non è arbitrario: le verifiche economiche e di
        capienza vengono prima del lock, così una richiesta palesemente
        invalida non trattiene righe del database; il lock precede la verifica
        di disponibilità, altrimenti fra il controllo e l'insert resterebbe una
        finestra sfruttabile.
        """
        rooms = await self._load_and_validate_rooms(quote.room_ids)
        self._assert_capacity(rooms, quote.guest_count)

        nights = self.pricing_service.calculate_nights(quote.check_in, quote.check_out)
        lines = self.pricing_service.build_price_lines(rooms, nights)
        base_price, discount_amount, total_price = self.pricing_service.compute_totals(
            lines, quote.payment_option
        )

        # Difesa in profondità sul prezzo: la firma del preventivo dice "questo
        # importo l'ho emesso io", non "questo importo è ancora corretto". Se
        # nel frattempo è cambiato il listino, la prenotazione non parte.
        if (base_price, discount_amount, total_price) != (
                quote.base_price, quote.discount_amount, quote.total_price
        ):
            raise InvalidQuoteToken()

        if enforce_pending_limit:
            pending = await self.booking_repository.count_active_pending_by_email(guest_email)
            if pending >= settings.max_active_pending_per_email:
                raise RateLimitExceeded(
                    "Hai già diverse prenotazioni in attesa di conferma. "
                    "Completale o attendi che scadano prima di crearne altre."
                )

        room_ids = [room.id for room in rooms]

        # Lock, liberazione degli hold scaduti e verifica disponibilità:
        # condiviso con la modifica amministrativa (vedi `_secure_slots`).
        await self._secure_slots(room_ids, quote.check_in, quote.check_out)

        now = datetime.now(timezone.utc)
        booking = Booking(
            code=await self._generate_unique_code(),
            status=initial_status,
            source_channel=channel,
            check_in=quote.check_in,
            check_out=quote.check_out,
            guest_count=quote.guest_count,
            user_id=user_id,
            guest_firstname=guest_firstname,
            guest_lastname=guest_lastname,
            guest_email=guest_email,
            guest_phone=guest_phone,
            base_price=base_price,
            discount_amount=discount_amount,
            total_price=total_price,
            currency=quote.currency,
            payment_option=quote.payment_option,
            payment_status=payment_status,
            payment_method=payment_method,
            admin_notes=admin_notes,
        )

        if initial_status in PENDING_BOOKING_STATUSES:
            booking.hold_expires_at = now + timedelta(minutes=settings.booking_hold_minutes)
        else:
            booking.confirmed_at = now
            booking.cancellation_deadline = self.pricing_service.compute_cancellation_deadline(
                quote.check_in, quote.payment_option
            )

        apply_audit_fields(audit=booking, user_id=audit_user_id, is_create=True)
        if audit_user_id is None and audit_marker:
            booking.created_by = audit_marker
            booking.last_updated_by = audit_marker

        lines_by_room = {line.room_id: line for line in lines}
        for room in rooms:
            line = lines_by_room[room.id]
            booking.items.append(
                BookingRoomItem(
                    room_id=room.id,
                    check_in=quote.check_in,
                    check_out=quote.check_out,
                    unit_price=line.unit_price,
                    nights=nights,
                    line_total=line.line_total,
                )
            )

        self._sync_items_active_flag(booking)
        self.booking_repository.add(booking)

        # 4. Il flush fa scattare l'exclusion constraint. Da qui in poi
        #    l'overbooking è impossibile, qualunque cosa faccia il codice.
        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            if self._is_overlap_violation(error):
                raise RoomNotAvailable() from error
            raise

        self.history_repository.add(
            booking_id=booking.id,
            from_status=None,
            to_status=initial_status,
            actor_type=actor_type,
            actor_id=str(audit_user_id) if audit_user_id else None,
            reason="Creazione della prenotazione",
        )
        if booking.payment_status == PaymentStatus.PAID:
            # Nata già pagata (incasso registrato dal back-office alla
            # creazione): anche questo incasso deve comparire nello storico.
            self._record_payment_change(
                booking,
                None,
                actor_type,
                str(audit_user_id) if audit_user_id else None,
                reason="Incasso registrato alla creazione",
            )

        confirmation_token: Optional[str] = None
        manage_token: Optional[str] = None

        if with_confirmation_token:
            confirmation_token = self._issue_confirmation_token(booking)
        elif initial_status == BookingStatus.CONFIRMED:
            # Nasce già confermata (pagamento online, oppure creazione dal
            # back-office con identità già verificata al telefono): non ci sarà
            # un passaggio di conferma in cui emettere il link di gestione, e
            # senza quello l'ospite non registrato resterebbe senza alcun modo
            # di annullare.
            manage_token = self._issue_manage_token(booking)

        await self.booking_repository.flush()

        schema = (
            await self._to_admin_schema(booking, rooms)
            if admin_view
            else self._to_public_schema(booking, rooms)
        )
        return BookingCreationResult(
            booking=schema,
            confirmation_token=confirmation_token,
            manage_token=manage_token,
        )

    # ================================================================== #
    # Conferma e cancellazione                                           #
    # ================================================================== #

    async def confirm_booking(self, plain_token: str) -> BookingCreationResult:
        """
        Conferma una prenotazione tramite il token ricevuto per email.

        Restituisce un `BookingCreationResult` e non il solo schema perché la
        conferma emette il token di gestione, che il chiamante deve poter
        inserire nell'email successiva.
        """
        return await self._in_transaction(lambda: self._execute_confirm(plain_token))

    async def _execute_confirm(self, plain_token: str) -> BookingCreationResult:
        token = await self.token_repository.get_by_hash(
            hash_booking_token(plain_token), BookingTokenPurpose.CONFIRM_EMAIL
        )
        if token is None:
            raise InvalidBookingToken()

        booking = await self.booking_repository.get_by_id(token.booking_id, with_items=True)
        if booking is None:
            raise InvalidBookingToken()

        if token.used_at is not None:
            # Distinguere i due casi è una scelta di prodotto: dire "già
            # confermata" rassicura chi clicca due volte il link, mentre un
            # generico "link non valido" lo farebbe dubitare della
            # prenotazione.
            if booking.status == BookingStatus.CONFIRMED:
                raise InvalidBookingStatusTransition("La prenotazione è già stata confermata")
            raise InvalidBookingToken()

        now = datetime.now(timezone.utc)
        hold_expired = booking.hold_expires_at is not None and booking.hold_expires_at <= now

        if token.expires_at <= now or hold_expired:
            # Nessuna transizione a EXPIRED in questo punto. Sollevare
            # l'eccezione fa rollback della transazione, quindi la scrittura
            # verrebbe comunque persa: marcarla qui darebbe solo l'illusione di
            # aver aggiornato lo stato.
            #
            # Non serve neppure: lo slot è già libero, perché le query di
            # disponibilità scartano i pending con blocco scaduto, e la
            # just-in-time expiration disattiva le righe camera alla prossima
            # prenotazione sulle stesse date. Il passaggio di stato a EXPIRED
            # appartiene allo sweeper (Step F), unico proprietario di quella
            # transizione.
            raise BookingHoldExpired()

        self._apply_transition(
            booking,
            BookingStatus.CONFIRMED,
            AuditActorType.GUEST,
            reason="Conferma tramite link email",
        )

        booking.confirmed_at = now
        booking.hold_expires_at = None
        booking.cancellation_deadline = self.pricing_service.compute_cancellation_deadline(
            booking.check_in, booking.payment_option
        )
        booking.last_updated_by = _GUEST_AUDIT_MARKER

        self.token_repository.mark_used(token)
        await self.token_repository.invalidate_all_for_booking(
            booking.id, BookingTokenPurpose.CONFIRM_EMAIL
        )

        # Rifatto dopo aver azzerato l'hold: la prenotazione non è più
        # temporanea e lo slot resta occupato in modo definitivo.
        self._sync_items_active_flag(booking)

        manage_token = self._issue_manage_token(booking)

        await self.booking_repository.flush()
        return BookingCreationResult(
            booking=self._to_public_schema(booking),
            manage_token=manage_token,
        )

    async def cancel_by_token(
            self,
            plain_token: str,
            reason: Optional[str] = None
    ) -> BookingPublicSchema:
        """Cancellazione richiesta dall'ospite tramite link."""
        return await self._in_transaction(lambda: self._execute_cancel_by_token(plain_token, reason))

    async def _execute_cancel_by_token(
            self,
            plain_token: str,
            reason: Optional[str]
    ) -> BookingPublicSchema:
        token = await self._load_guest_token(
            plain_token, (BookingTokenPurpose.CANCEL, BookingTokenPurpose.MANAGE)
        )

        booking = await self.booking_repository.get_by_id(token.booking_id, with_items=True)
        if booking is None:
            raise InvalidBookingToken()

        self._assert_guest_can_cancel(booking)

        self._apply_transition(
            booking,
            BookingStatus.CANCELLED,
            AuditActorType.GUEST,
            reason=reason or "Cancellazione richiesta dall'ospite",
        )
        booking.cancelled_at = datetime.now(timezone.utc)
        booking.cancellation_reason = reason
        booking.last_updated_by = _GUEST_AUDIT_MARKER

        self.token_repository.mark_used(token)
        await self.token_repository.invalidate_all_for_booking(booking.id)

        await self.booking_repository.flush()
        return self._to_public_schema(booking)

    async def cancel_own_booking(
            self,
            code: str,
            user: User,
            reason: Optional[str] = None
    ) -> BookingPublicSchema:
        """
        Cancellazione da parte dell'utente autenticato intestatario.

        Identifica la prenotazione tramite il **codice**, non l'identificativo
        interno: `BookingPublicSchema` non espone `id`, quindi un client non
        avrebbe modo di procurarselo. Il codice è l'unico riferimento pubblico
        di una prenotazione, ed è quello che l'utente vede.
        """
        return await self._in_transaction(
            lambda: self._execute_cancel_own(code, user, reason)
        )

    async def _execute_cancel_own(
            self,
            code: str,
            user: User,
            reason: Optional[str]
    ) -> BookingPublicSchema:
        booking = await self.booking_repository.get_by_code(code, with_items=True)

        # Risposta identica a "non esiste" quando la prenotazione è di un
        # altro utente: confermarne l'esistenza consentirebbe di sondare i
        # codici altrui.
        if booking is None or booking.user_id != user.id:
            raise EntityNotFound("Prenotazione non trovata")

        self._assert_guest_can_cancel(booking)

        self._apply_transition(
            booking,
            BookingStatus.CANCELLED,
            AuditActorType.USER,
            actor_id=str(user.id),
            reason=reason or "Cancellazione richiesta dall'utente",
        )
        booking.cancelled_at = datetime.now(timezone.utc)
        booking.cancellation_reason = reason
        apply_audit_fields(audit=booking, user_id=user.id)

        await self.token_repository.invalidate_all_for_booking(booking.id)
        await self.booking_repository.flush()
        return self._to_public_schema(booking)

    # ================================================================== #
    # Operazioni amministrative                                          #
    # ================================================================== #

    async def admin_change_status(
            self,
            booking_id: UUID,
            payload: BookingStatusUpdateSchema,
            admin_id: UUID
    ) -> BookingCreationResult:
        """
        Transizione di stato disposta dal back-office.

        Sulla **conferma** emette anche il link di gestione, come la conferma
        dell'ospite via email: è l'unico strumento con cui un ospite non
        registrato vede o annulla la propria prenotazione, e chi viene
        confermato al telefono non lo riceverebbe in nessun altro modo.
        Il link torna in chiaro solo dentro il risultato, per l'email.
        """
        return await self._in_transaction(
            lambda: self._execute_admin_change_status(booking_id, payload, admin_id)
        )

    async def _execute_admin_change_status(
            self,
            booking_id: UUID,
            payload: BookingStatusUpdateSchema,
            admin_id: UUID
    ) -> BookingCreationResult:
        booking = await self.booking_repository.get_by_id(
            booking_id, with_items=True, with_history=True
        )
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        self._assert_admin_transition(booking, payload.new_status)
        self._assert_status_change_is_coherent(booking, payload.new_status)
        self._assert_reason_when_required(booking, payload)

        now = datetime.now(timezone.utc)
        manage_token: Optional[str] = None
        self._apply_transition(
            booking,
            payload.new_status,
            AuditActorType.ADMIN,
            actor_id=str(admin_id),
            reason=payload.reason,
        )

        if payload.new_status == BookingStatus.CONFIRMED:
            booking.confirmed_at = now
            booking.hold_expires_at = None
            booking.cancellation_deadline = self.pricing_service.compute_cancellation_deadline(
                booking.check_in, booking.payment_option
            )
            self._sync_items_active_flag(booking)
            # Il link di conferma ricevuto dall'ospite non serve più: spenderlo
            # ora darebbe solo un errore. Al suo posto, il link di gestione.
            await self.token_repository.invalidate_all_for_booking(
                booking.id, BookingTokenPurpose.CONFIRM_EMAIL
            )
            # Il link scade all'arrivo: per un soggiorno già iniziato (inserito a
            # posteriori) sarebbe morto prima di arrivare nella casella.
            if booking.check_in > today_in_app_timezone():
                manage_token = self._issue_manage_token(booking)
        elif payload.new_status == BookingStatus.CANCELLED:
            booking.cancelled_at = now
            booking.cancellation_reason = payload.reason

        if payload.new_status in (
                BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED
        ):
            await self.token_repository.invalidate_all_for_booking(booking.id)

        apply_audit_fields(audit=booking, user_id=admin_id)
        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            # Conferma di una prenotazione il cui blocco è scaduto: nel
            # frattempo le notti possono essere state vendute a qualcun altro
            # (la creazione libera i blocchi scaduti), e riattivarle
            # violerebbe il vincolo anti-overbooking. La transazione viene
            # annullata e la prenotazione resta in attesa: scadrà da sola.
            if self._is_overlap_violation(error):
                raise RoomNotAvailable(
                    "Le camere sono state prenotate da qualcun altro nel frattempo: "
                    "la prenotazione non si può più confermare"
                ) from error
            raise
        return BookingCreationResult(
            booking=await self._to_admin_schema(booking),
            manage_token=manage_token,
        )

    async def admin_register_payment(
            self,
            booking_id: UUID,
            payload: AdminPaymentRegistrationSchema,
            admin_id: UUID
    ) -> BookingSchema:
        """
        Registrazione manuale di un pagamento in struttura.

        La riga di audit si scrive **qui, dopo** la transazione: dentro
        precederebbe il commit, e un salvataggio fallito — due operatori
        sulla stessa prenotazione, per esempio — lascerebbe nei log un
        pagamento che il database non ha mai registrato.
        """
        booking, previous_status = await self._in_transaction(
            lambda: self._execute_register_payment(booking_id, payload, admin_id)
        )

        # Lo storico degli stati registra solo le transizioni della
        # prenotazione, non quelle dell'incasso. Nessun dato dell'ospite, solo
        # il codice.
        logger.info(
            "Pagamento manuale: prenotazione=%s %s -> %s metodo=%s admin=%s",
            booking.code,
            previous_status.value,
            booking.payment_status.value,
            booking.payment_method.value if booking.payment_method else "-",
            admin_id,
        )
        return booking

    async def _execute_register_payment(
            self,
            booking_id: UUID,
            payload: AdminPaymentRegistrationSchema,
            admin_id: UUID
    ) -> Tuple[BookingSchema, PaymentStatus]:
        """:return: la prenotazione aggiornata e lo stato dell'incasso precedente."""
        booking = await self.booking_repository.get_by_id(booking_id, with_items=True)
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        self._assert_manual_payment_allowed(booking, payload.payment_status)

        previous_status = booking.payment_status

        if payload.payment_status == PaymentStatus.PAID:
            booking.payment_method = payload.payment_method
        elif payload.payment_status == PaymentStatus.PENDING:
            # Correzione: la registrazione precedente era sbagliata, quindi
            # anche il metodo che indicava.
            booking.payment_method = None
        elif payload.payment_method is not None:
            # Rimborso: se non indicato, resta il metodo dell'incasso.
            booking.payment_method = payload.payment_method

        # Le regole garantiscono che lo stato cambi davvero (`PAID` solo da
        # un pagamento non incassato, il resto solo da `PAID`): la voce di
        # storico c'è sempre.
        self._set_payment_status(
            booking,
            payload.payment_status,
            AuditActorType.ADMIN,
            actor_id=str(admin_id),
            reason=payload.reason,
        )

        apply_audit_fields(audit=booking, user_id=admin_id)
        await self.booking_repository.flush()
        return await self._to_admin_schema(booking), previous_status

    async def admin_extend_hold(
            self,
            booking_id: UUID,
            payload: BookingExtendHoldSchema,
            admin_id: UUID
    ) -> BookingSchema:
        """Proroga il blocco temporaneo di una prenotazione ancora in attesa."""
        return await self._in_transaction(
            lambda: self._execute_extend_hold(booking_id, payload, admin_id)
        )

    async def _execute_extend_hold(
            self,
            booking_id: UUID,
            payload: BookingExtendHoldSchema,
            admin_id: UUID
    ) -> BookingSchema:
        booking = await self.booking_repository.get_by_id(booking_id, with_items=True)
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        if booking.status not in PENDING_BOOKING_STATUSES:
            raise InvalidBookingStatusTransition(
                "Solo una prenotazione in attesa può avere il blocco prorogato"
            )

        base = max(datetime.now(timezone.utc), booking.hold_expires_at or datetime.now(timezone.utc))
        booking.hold_expires_at = base + timedelta(minutes=payload.minutes)

        # La proroga può "resuscitare" uno slot già liberato dalla just-in-time
        # expiration: il flag va ricalcolato, e se nel frattempo la camera è
        # stata venduta a qualcun altro il vincolo del database lo impedirà.
        self._sync_items_active_flag(booking)
        apply_audit_fields(audit=booking, user_id=admin_id)

        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            if self._is_overlap_violation(error):
                raise RoomNotAvailable(
                    "Le camere sono state prenotate da qualcun altro nel frattempo"
                ) from error
            raise

        return await self._to_admin_schema(booking)

    # ================================================================== #
    # Letture                                                            #
    # ================================================================== #

    # ================================================================== #
    # Scadenze                                                           #
    # ================================================================== #

    async def expire_pending(
            self,
            limit: Optional[int] = None,
            exclude_ids: Optional[Sequence[UUID]] = None
    ) -> List[ExpiredBookingNotice]:
        """
        Porta a `EXPIRED` le prenotazioni temporanee con blocco scaduto.

        È **l'unico proprietario di questa transizione** (§4.1 del piano): in
        nessun altro punto del codice una prenotazione diventa `EXPIRED`.
        `confirm_booking` ci aveva provato, ma scriveva su un percorso che
        subito dopo sollevava un'eccezione, quindi la modifica veniva annullata
        dal rollback nell'istante stesso in cui avveniva.

        Va detto che il sistema resta corretto anche se questo metodo non gira
        mai: gli slot tornano prenotabili per altre due vie — le query di
        disponibilità scartano i pending scaduti, e la *just-in-time
        expiration* disattiva le righe camera durante la creazione successiva
        sulle stesse date. Quello che manca senza sweeper è la pulizia degli
        stati e l'avviso all'ospite, non la correttezza.

        :param limit: prenotazioni trattate in questa passata.
        :param exclude_ids: prenotazioni da **non** toccare in questa passata.
            Serve a un caso preciso: quando lo sweeper non è riuscito ad
            annullare un'autorizzazione su Stripe — gestore irraggiungibile,
            per esempio — quella prenotazione non va fatta scadere. Liberare
            lo slot lasciando viva un'autorizzazione significherebbe poter
            incassare per una camera già rivenduta, cioè esattamente il caso
            che tutto questo disegno esiste per rendere impossibile.
        :return: un avviso per prenotazione, con l'indicazione se valga ancora
            la pena notificarla.
        """
        return await self._in_transaction(
            lambda: self._execute_expire_pending(limit, exclude_ids)
        )

    async def _execute_expire_pending(
            self,
            limit: Optional[int],
            exclude_ids: Optional[Sequence[UUID]] = None
    ) -> List[ExpiredBookingNotice]:
        bookings = await self.booking_repository.get_expired_pending(
            limit=limit or settings.sweeper_batch_size,
            # Con più worker uvicorn girano più sweeper insieme: il lock con
            # SKIP LOCKED fa sì che si dividano le righe invece di lavorare
            # due volte sulle stesse.
            for_update=True,
        )

        notify_threshold = datetime.now(timezone.utc) - timedelta(
            hours=settings.sweeper_notify_max_age_hours
        )
        notices: List[ExpiredBookingNotice] = []
        esclusi = set(exclude_ids or ())

        for booking in bookings:
            if booking.id in esclusi:
                continue

            # Letto prima della transizione: è il dato che distingue una
            # scadenza appena avvenuta da una recuperata in ritardo.
            expired_at = booking.hold_expires_at

            self._apply_transition(
                booking,
                BookingStatus.EXPIRED,
                AuditActorType.SYSTEM,
                reason="Blocco scaduto senza conferma",
            )
            booking.last_updated_by = _SYSTEM_AUDIT_MARKER

            # `hold_expires_at` resta valorizzato: su una prenotazione scaduta
            # racconta *quando* è scaduta, e non rischia di riportarla nel
            # bacino dello sweeper, che filtra per stato.

            # Il token di conferma non deve sopravvivere alla prenotazione che
            # confermava: un link ancora valido su una prenotazione scaduta
            # darebbe all'ospite un errore incomprensibile.
            await self.token_repository.invalidate_all_for_booking(booking.id)

            notices.append(
                ExpiredBookingNotice(
                    booking=self._to_public_schema(booking),
                    notify=expired_at is not None and expired_at >= notify_threshold,
                )
            )

        await self.booking_repository.flush()
        return notices

    # ================================================================== #
    # Manutenzione                                                       #
    # ================================================================== #

    async def purge_expired_tokens(self, limit: Optional[int] = None) -> int:
        """
        Elimina i token scaduti da più del periodo di ritenzione.

        Pulizia, non sicurezza: un token scaduto non è già più spendibile,
        perché `confirm_booking` e `cancel_with_token` ne verificano la
        scadenza a ogni uso. Quello che si evita qui è una tabella che cresce
        indefinitamente — ogni prenotazione ne produce fino a due — con un
        indice che rallenta ricerche a cui quelle righe non risponderanno mai.

        **La soglia esiste perché la riga scaduta conserva una sola
        informazione utile**: che un token era stato emesso, e quando. È ciò
        che serve a rispondere all'ospite che scrive "il link non mi è mai
        arrivato". Non di più: in tabella c'è l'hash, non il valore, quindi
        nessuno può rimandargli lo stesso link. Passato un mese, la
        contestazione o è arrivata o non arriverà.

        :param limit: tetto alle righe eliminate; se omesso vale
            `settings.token_purge_batch_size`. Chiamate successive smaltiscono
            un eventuale arretrato.
        :return: numero di token eliminati.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.token_retention_days
        )
        batch = limit if limit is not None else settings.token_purge_batch_size

        return await self._in_transaction(
            lambda: self.token_repository.purge_expired(cutoff, batch)
        )

    # ================================================================== #
    # Pagamenti                                                          #
    # ================================================================== #

    async def start_payment(self, code: str, email: str) -> PaymentContext:
        """
        Prepara una prenotazione al pagamento e proroga il blocco.

        Il blocco standard è tarato su un clic in un'email; un pagamento
        richiede molto di più — autenticazione della banca, carta rifiutata e
        ritentata, ospite che si allontana. La proroga a
        `booking_payment_hold_minutes` riflette quel tempo reale.

        Non crea nulla su Stripe: la chiamata di rete non deve stare dentro una
        transazione del database. Un timeout farebbe rollback della
        prenotazione, o peggio lascerebbe dietro un Payment Intent pagabile
        senza nulla che gli corrisponda.
        """
        return await self._in_transaction(lambda: self._execute_start_payment(code, email))

    async def _execute_start_payment(self, code: str, email: str) -> PaymentContext:
        booking = await self.booking_repository.get_by_code_and_email(code, email)
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        if booking.payment_option != PaymentOption.PAY_NOW:
            raise PaymentRequired("Questa prenotazione si salda in struttura")

        if booking.status == BookingStatus.CONFIRMED:
            raise InvalidBookingStatusTransition("La prenotazione è già confermata")

        if booking.status != BookingStatus.PENDING_PAYMENT:
            raise BookingHoldExpired()

        booking.hold_expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.booking_payment_hold_minutes
        )
        # Riattiva le righe camera se erano state liberate: la proroga vale
        # solo se lo slot è ancora disponibile, e a deciderlo è il vincolo del
        # database, non un controllo applicativo.
        self._sync_items_active_flag(booking)

        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            if self._is_overlap_violation(error):
                raise RoomNotAvailable() from error
            raise

        return PaymentContext(
            code=booking.code,
            total_price=Decimal(booking.total_price),
            currency=booking.currency,
            guest_email=booking.guest_email,
            existing_intent_id=booking.stripe_payment_intent_id,
        )

    async def attach_payment_intent(self, code: str, intent_id: str) -> None:
        """Associa il Payment Intent alla prenotazione, dopo averlo creato."""
        return await self._in_transaction(
            lambda: self._execute_attach_intent(code, intent_id)
        )

    async def _execute_attach_intent(self, code: str, intent_id: str) -> None:
        booking = await self.booking_repository.get_by_code(code)
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        booking.stripe_payment_intent_id = intent_id
        booking.payment_method = PaymentMethod.STRIPE_CARD
        await self.booking_repository.flush()

    async def authorize_payment(
            self,
            intent_id: str,
            amount: Decimal,
            currency: str
    ) -> PaymentAuthorizationResult:
        """
        Verifica che si possa incassare. **È il controllo che rende impossibile
        incassare per una camera che non abbiamo più.**

        Stripe ha autorizzato l'importo ma non lo ha prelevato: siamo noi a
        decidere se prelevarlo, e questo è il momento in cui decidiamo. Se lo
        slot non è più nostro, l'autorizzazione viene rilasciata e l'ospite non
        viene addebitato di nulla — molto meglio di un addebito seguito da un
        rimborso.

        La riga viene bloccata: notifiche di Stripe e sweeper possono arrivare
        insieme sullo stesso pagamento, e senza lock potrebbero concludere
        entrambi di poter agire.

        :raises RoomNotAvailable: lo slot è stato venduto a qualcun altro. Il
            chiamante deve rilasciare l'autorizzazione. È un'eccezione e non un
            valore di ritorno perché il tentativo di riattivare le righe camera
            fallisce a livello di database, e da lì la transazione va comunque
            annullata.
        """
        return await self._in_transaction(
            lambda: self._execute_authorize_payment(intent_id, amount, currency)
        )

    async def _execute_authorize_payment(
            self,
            intent_id: str,
            amount: Decimal,
            currency: str
    ) -> PaymentAuthorizationResult:
        booking = await self.booking_repository.get_by_stripe_payment_intent(
            intent_id, with_items=True, for_update=True
        )
        if booking is None:
            return PaymentAuthorizationResult(PaymentOutcome.UNKNOWN_BOOKING)

        if booking.status == BookingStatus.CONFIRMED:
            return PaymentAuthorizationResult(
                PaymentOutcome.ALREADY_CONFIRMED, booking.code
            )

        # Solo una prenotazione che attende il pagamento può essere incassata.
        # Senza questo controllo, una prenotazione annullata dal back-office
        # mentre l'ospite era sulla pagina di pagamento arrivava fin qui: le
        # righe camera venivano riattivate — una prenotazione annullata si
        # riprendeva lo slot — e l'importo veniva incassato, per poi fallire
        # sulla transizione CANCELLED → CONFIRMED. Denaro preso, nessuna
        # camera, nessuna email.
        if booking.status != BookingStatus.PENDING_PAYMENT:
            return PaymentAuthorizationResult(PaymentOutcome.NOT_PAYABLE, booking.code)

        # L'importo si verifica, non si accetta. Il Payment Intent lo abbiamo
        # creato noi, ma fra creazione e autorizzazione il totale potrebbe
        # essere stato modificato dal back-office.
        importo_atteso = Decimal(booking.total_price).quantize(Decimal("0.01"))
        if amount != importo_atteso or currency.upper() != booking.currency.upper():
            return PaymentAuthorizationResult(
                PaymentOutcome.AMOUNT_MISMATCH, booking.code
            )

        # Lo slot è ancora nostro? Non lo si chiede a una query: lo si prova a
        # riprendere, e lascia rispondere l'exclusion constraint.
        for item in booking.items:
            item.is_active = True

        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            if self._is_overlap_violation(error):
                raise RoomNotAvailable(
                    "Le camere sono state prenotate da un altro ospite"
                ) from error
            raise

        self._set_payment_status(
            booking, PaymentStatus.AUTHORIZED, AuditActorType.SYSTEM,
            reason="Pagamento online autorizzato",
        )
        await self.booking_repository.flush()

        return PaymentAuthorizationResult(PaymentOutcome.CAPTURE, booking.code)

    async def confirm_paid_booking(
            self,
            intent_id: str,
            card_brand: Optional[str] = None,
            card_last4: Optional[str] = None
    ) -> Optional[BookingCreationResult]:
        """
        Porta a `CONFIRMED` una prenotazione il cui importo è stato incassato.

        :return: `None` se la prenotazione era già confermata. Serve
            all'idempotenza: Stripe consegna lo stesso evento più volte, e la
            seconda non deve produrre né una transizione né una seconda email.
        """
        return await self._in_transaction(
            lambda: self._execute_confirm_paid(intent_id, card_brand, card_last4)
        )

    async def _execute_confirm_paid(
            self,
            intent_id: str,
            card_brand: Optional[str],
            card_last4: Optional[str]
    ) -> Optional[BookingCreationResult]:
        booking = await self.booking_repository.get_by_stripe_payment_intent(
            intent_id, with_items=True, for_update=True
        )
        if booking is None or booking.status == BookingStatus.CONFIRMED:
            return None

        now = datetime.now(timezone.utc)

        self._apply_transition(
            booking,
            BookingStatus.CONFIRMED,
            AuditActorType.SYSTEM,
            reason="Pagamento online incassato",
        )

        self._set_payment_status(
            booking, PaymentStatus.PAID, AuditActorType.SYSTEM,
            reason="Pagamento online incassato",
        )
        booking.card_brand = card_brand
        booking.card_last4 = card_last4
        booking.confirmed_at = now
        booking.hold_expires_at = None
        booking.cancellation_deadline = self.pricing_service.compute_cancellation_deadline(
            booking.check_in, booking.payment_option
        )
        booking.last_updated_by = _SYSTEM_AUDIT_MARKER

        # Rifatto dopo aver azzerato l'hold: la prenotazione non è più
        # temporanea e lo slot resta occupato in modo definitivo.
        self._sync_items_active_flag(booking)

        manage_token = self._issue_manage_token(booking)
        await self.booking_repository.flush()

        return BookingCreationResult(
            booking=self._to_public_schema(booking),
            manage_token=manage_token,
        )

    async def mark_payment_failed(self, intent_id: str) -> Optional[BookingPublicSchema]:
        """
        Registra un pagamento rifiutato **lasciando la prenotazione
        ritentabile**.

        Lo stato resta `PENDING_PAYMENT` finché il blocco non scade: una carta
        rifiutata è quasi sempre un problema di quella carta, e annullare
        subito costringerebbe l'ospite a rifare tutto da capo per un motivo che
        si risolve cambiando tessera.
        """
        return await self._in_transaction(
            lambda: self._execute_mark_payment(intent_id, PaymentStatus.FAILED)
        )

    async def mark_payment_refunded(self, intent_id: str) -> Optional[BookingPublicSchema]:
        """Registra un rimborso emesso su Stripe."""
        return await self._in_transaction(
            lambda: self._execute_mark_payment(intent_id, PaymentStatus.REFUNDED)
        )

    async def _execute_mark_payment(
            self,
            intent_id: str,
            status: PaymentStatus
    ) -> Optional[BookingPublicSchema]:
        booking = await self.booking_repository.get_by_stripe_payment_intent(
            intent_id, with_items=True, for_update=True
        )
        if booking is None:
            return None

        self._set_payment_status(
            booking, status, AuditActorType.SYSTEM,
            reason=_STRIPE_PAYMENT_REASONS.get(status),
        )
        booking.last_updated_by = _SYSTEM_AUDIT_MARKER
        await self.booking_repository.flush()

        return self._to_public_schema(booking)

    async def abandon_for_slot_lost(self, intent_id: str) -> Optional[BookingPublicSchema]:
        """
        Chiude una prenotazione il cui slot è stato venduto ad altri mentre il
        pagamento era in corso.

        Da invocare **solo dopo** aver rilasciato l'autorizzazione: l'ospite non
        deve essere addebitato, e la sequenza corretta è prima rendere
        impossibile l'incasso, poi chiudere la prenotazione.

        Stato `CANCELLED` e non `EXPIRED`: non è scaduto nulla, è una corsa
        persa, e lo storico deve dirlo con la sua motivazione.
        """
        return await self._in_transaction(
            lambda: self._execute_abandon_slot_lost(intent_id)
        )

    async def _execute_abandon_slot_lost(
            self,
            intent_id: str
    ) -> Optional[BookingPublicSchema]:
        booking = await self.booking_repository.get_by_stripe_payment_intent(
            intent_id, with_items=True, for_update=True
        )
        if booking is None or booking.status.is_terminal:
            return None

        motivo = "Camere non più disponibili al momento del pagamento"

        self._apply_transition(
            booking,
            BookingStatus.CANCELLED,
            AuditActorType.SYSTEM,
            reason=motivo,
        )
        booking.cancelled_at = datetime.now(timezone.utc)
        booking.cancellation_reason = motivo
        self._set_payment_status(
            booking, PaymentStatus.NOT_REQUIRED, AuditActorType.SYSTEM, reason=motivo,
        )
        booking.last_updated_by = _SYSTEM_AUDIT_MARKER

        await self.token_repository.invalidate_all_for_booking(booking.id)
        await self.booking_repository.flush()

        return self._to_public_schema(booking)

    async def list_pending_payment_with_intent(
            self,
            limit: Optional[int] = None
    ) -> List[PendingPaymentRef]:
        """
        Prenotazioni scadute con un'autorizzazione ancora viva.

        Sola lettura: serve allo sweeper per sapere **quali autorizzazioni
        annullare prima di liberare gli slot**.
        """
        bookings = await self.booking_repository.get_pending_payment_with_intent(
            limit or settings.sweeper_batch_size
        )
        return [
            PendingPaymentRef(
                booking_id=booking.id,
                code=booking.code,
                payment_intent_id=booking.stripe_payment_intent_id,
            )
            for booking in bookings
        ]

    async def get_admin_booking(self, booking_id: UUID) -> BookingSchema:
        """Dettaglio completo con timeline degli stati, per il back-office."""
        booking = await self.booking_repository.get_by_id(
            booking_id, with_items=True, with_history=True
        )
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        return await self._to_admin_schema(booking)

    async def _load_guest_token(
            self,
            plain_token: str,
            purposes: Sequence[BookingTokenPurpose]
    ) -> BookingToken:
        """
        Carica un token dell'ospite e ne verifica la spendibilità.

        Unico punto in cui è scritto che cosa rende valido un token di
        gestione: esiste, ha uno degli scopi attesi, non è stato speso, non è
        scaduto. Prima che questo helper esistesse `POST /cancel` controllava
        i primi tre e **non la scadenza**, il che rendeva cancellabile una
        prenotazione con un link ormai morto — e una `MANAGE` scade
        all'arrivo, quindi la finestra non era teorica.

        L'ordine degli scopi conta solo per il numero di query: il primo che
        corrisponde vince, e il risultato è lo stesso.

        :raises InvalidBookingToken: in tutti i casi, con lo stesso messaggio.
            Distinguere "scaduto" da "inesistente" direbbe a chi prova token
            a caso quali abbia senso continuare a provare.
        """
        token_hash = hash_booking_token(plain_token)

        token = None
        for purpose in purposes:
            token = await self.token_repository.get_by_hash(token_hash, purpose)
            if token is not None:
                break

        if token is None or token.used_at is not None:
            raise InvalidBookingToken()

        if token.expires_at is not None and token.expires_at <= datetime.now(timezone.utc):
            raise InvalidBookingToken()

        return token

    async def read_by_manage_token(self, plain_token: str) -> BookingManageSchema:
        """
        Legge una prenotazione a partire dal token del link di gestione.

        **Non consuma il token.** È una lettura, e la pagina che la usa viene
        ricaricata: un token speso al primo caricamento renderebbe il link
        monouso nel senso sbagliato — l'ospite lo aprirebbe, leggerebbe, e al
        primo aggiornamento della pagina non avrebbe più niente.

        Esiste perché senza di essa il link di gestione non porta da nessuna
        parte. `POST /lookup` chiede codice ed email, che il link non
        contiene; `POST /cancel` accetta il token ma annulla. La pagina
        poteva quindi offrire solo un pulsante "Annulla" cieco, senza dire
        all'ospite che cosa stesse annullando. È la stessa forma del difetto
        dello Step F, dove `POST /cancel` richiedeva una credenziale che
        nessun percorso emetteva: una credenziale e il suo utilizzo che non si
        incontrano.

        Accetta `MANAGE` e `CANCEL`, come `POST /cancel`: chi possiede un
        token che consente di annullare può a maggior ragione leggere ciò che
        sta per annullare.

        :raises InvalidBookingToken: token inesistente, di scopo diverso, già
            speso o scaduto. Risposta identica in tutti i casi: distinguerli
            direbbe a chi prova token a caso quali esistono.
        """
        token = await self._load_guest_token(
            plain_token, (BookingTokenPurpose.MANAGE, BookingTokenPurpose.CANCEL)
        )

        booking = await self.booking_repository.get_by_id(token.booking_id, with_items=True)
        if booking is None:
            raise InvalidBookingToken()

        policy = self.describe_guest_cancellation(booking)

        return BookingManageSchema(
            booking=self._to_public_schema(booking),
            cancellation=GuestCancellationPolicySchema(
                can_cancel=policy.can_cancel,
                blocked_by=policy.blocked_by,
                message=policy.message,
                free_until=policy.free_until,
            ),
        )

    async def lookup(self, code: str, email: str) -> BookingPublicSchema:
        """Consultazione da parte di un ospite non registrato (codice + email)."""
        booking = await self.booking_repository.get_by_code_and_email(code, email)
        if booking is None:
            # Messaggio volutamente generico: non deve rivelare se il codice
            # esista e sia solo l'email a non corrispondere.
            raise EntityNotFound("Nessuna prenotazione trovata con i dati indicati")
        return self._to_public_schema(booking)

    async def get_user_bookings(
            self,
            user_id: UUID,
            limit: int = 20,
            offset: int = 0
    ) -> List[BookingPublicSchema]:
        bookings = await self.booking_repository.get_by_user(user_id, limit, offset)
        return [self._to_public_schema(booking) for booking in bookings]

    async def search(self, filters: BookingSearchFiltersSchema) -> PaginatedBookingsSchema:
        """Elenco filtrato e paginato per il back-office."""
        bookings, total = await self.booking_repository.search(filters)

        pages = (total + filters.page_size - 1) // filters.page_size if total else 0

        return PaginatedBookingsSchema(
            items=[
                BookingListItemSchema(
                    id=booking.id,
                    code=booking.code,
                    status=booking.status,
                    check_in=booking.check_in,
                    check_out=booking.check_out,
                    guest_firstname=booking.guest_firstname,
                    guest_lastname=booking.guest_lastname,
                    guest_email=booking.guest_email,
                    guest_count=booking.guest_count,
                    rooms_count=len(booking.items),
                    room_names=[item.room.name for item in booking.items if item.room],
                    total_price=booking.total_price,
                    payment_status=booking.payment_status,
                )
                for booking in bookings
            ],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            pages=pages,
        )

    # ================================================================== #
    # Macchina a stati                                                   #
    # ================================================================== #

    @staticmethod
    def _assert_transition_allowed(current: BookingStatus, new: BookingStatus) -> None:
        if new not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise InvalidBookingStatusTransition(
                f"Non è possibile passare dallo stato '{current.value}' a '{new.value}'"
            )

    def _set_payment_status(
            self,
            booking: Booking,
            new_status: PaymentStatus,
            actor_type: AuditActorType,
            actor_id: Optional[str] = None,
            reason: Optional[str] = None
    ) -> None:
        """
        Cambia lo stato del pagamento e ne registra la traccia storica.

        **Unico punto in cui `booking.payment_status` viene modificato** dopo
        la creazione, come `_apply_transition` per lo stato della
        prenotazione: incasso e rimborso manuali, autorizzazione, incasso,
        rifiuto e rimborso di Stripe, chiusura per camere perse. Il metodo va
        impostato **prima** di chiamarlo: la voce registra quello risultante.

        Un evento ripetuto che non cambia nulla — Stripe consegna lo stesso
        webhook più volte — non lascia una seconda voce.
        """
        previous = booking.payment_status
        if previous == new_status:
            return
        booking.payment_status = new_status
        self._record_payment_change(booking, previous, actor_type, actor_id, reason)

    def _record_payment_change(
            self,
            booking: Booking,
            from_status: Optional[PaymentStatus],
            actor_type: AuditActorType,
            actor_id: Optional[str] = None,
            reason: Optional[str] = None
    ) -> None:
        """Scrive la voce dello storico pagamenti con lo stato e il metodo attuali."""
        self.payment_history_repository.add(
            booking_id=booking.id,
            from_status=from_status,
            to_status=booking.payment_status,
            payment_method=booking.payment_method,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )

    def _apply_transition(
            self,
            booking: Booking,
            new_status: BookingStatus,
            actor_type: AuditActorType,
            actor_id: Optional[str] = None,
            reason: Optional[str] = None
    ) -> None:
        """
        Esegue una transizione di stato, aggiorna l'occupazione degli slot e
        ne registra la traccia storica. Unico punto in cui `booking.status`
        viene modificato.
        """
        previous = booking.status
        self._assert_transition_allowed(previous, new_status)

        booking.status = new_status
        self._sync_items_active_flag(booking)

        self.history_repository.add(
            booking_id=booking.id,
            from_status=previous,
            to_status=new_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )

    @staticmethod
    def _sync_items_active_flag(booking: Booking) -> None:
        """
        Allinea `is_active` delle righe camera allo stato della prenotazione.

        È il predicato dell'exclusion constraint: da questo metodo dipende
        tanto l'impossibilità del doppio booking quanto il fatto che uno slot
        scaduto torni prenotabile. Ogni transizione ci passa.
        """
        occupying = booking.status in OCCUPYING_BOOKING_STATUSES

        if occupying and booking.status in PENDING_BOOKING_STATUSES:
            occupying = (
                booking.hold_expires_at is not None
                and booking.hold_expires_at > datetime.now(timezone.utc)
            )

        for item in booking.items:
            item.is_active = occupying

    @staticmethod
    def describe_guest_cancellation(booking: Booking) -> GuestCancellationPolicy:
        """
        Dice se l'ospite può cancellare da sé, e se no perché.

        **È l'unico posto in cui questa regola è scritta.** `POST /cancel` la
        usa per decidere, `POST /manage` per raccontarla all'ospite prima che
        clicchi. Due implementazioni — una che decide e una che spiega —
        divergerebbero alla prima modifica della politica commerciale, e
        divergerebbero in modo invisibile: la pagina direbbe "annulla
        gratuitamente" e l'endpoint risponderebbe `409`.

        Le tre condizioni, nell'ordine in cui vanno valutate:

        1. una prenotazione ancora in attesa si annulla sempre — non c'è nulla
           da disdire, solo un blocco da liberare;
        2. una già pagata online non è auto-cancellabile: l'importo è dovuto e
           l'eventuale rimborso lo valuta la struttura;
        3. oltre il termine di cancellazione gratuita serve un intervento
           umano.

        Fuori dallo stato `CONFIRMED` e da quelli in attesa non resta nulla da
        annullare: annullata, scaduta, conclusa, arrivo registrato.
        """
        if booking.status in PENDING_BOOKING_STATUSES:
            return GuestCancellationPolicy(can_cancel=True)

        if booking.status != BookingStatus.CONFIRMED:
            return GuestCancellationPolicy(
                can_cancel=False,
                blocked_by=GuestCancellationBlock.STATUS_NOT_CANCELLABLE,
                message="La prenotazione non si trova in uno stato che consente la cancellazione",
            )

        if booking.payment_option == PaymentOption.PAY_NOW:
            return GuestCancellationPolicy(
                can_cancel=False,
                blocked_by=GuestCancellationBlock.NON_REFUNDABLE,
                message=(
                    "Le prenotazioni con pagamento online anticipato non sono rimborsabili. "
                    "Contatta la struttura per valutare la tua situazione."
                ),
            )

        deadline = booking.cancellation_deadline
        if deadline is not None and datetime.now(timezone.utc) > deadline:
            return GuestCancellationPolicy(
                can_cancel=False,
                blocked_by=GuestCancellationBlock.DEADLINE_PASSED,
                message=(
                    "Il termine per la cancellazione gratuita è scaduto. "
                    "Contatta la struttura per assistenza."
                ),
                free_until=deadline,
            )

        return GuestCancellationPolicy(can_cancel=True, free_until=deadline)

    def _assert_guest_can_cancel(self, booking: Booking) -> None:
        """
        Interrompe la cancellazione quando la politica non la consente.

        Nessuna regola qui: solo la traduzione in eccezione di ciò che
        `describe_guest_cancellation` ha già deciso.
        """
        policy = self.describe_guest_cancellation(booking)
        if not policy.can_cancel:
            raise BookingNotCancellable(policy.message)

    @staticmethod
    def _assert_status_change_is_coherent(booking: Booking, new_status: BookingStatus) -> None:
        """
        Controlli temporali sulle transizioni operative.

        Impediscono i refusi più comuni del back-office: registrare un arrivo
        con settimane di anticipo o un soggiorno concluso prima della partenza.
        """
        today = today_in_app_timezone()

        if new_status == BookingStatus.CHECKED_IN and today < booking.check_in:
            raise InvalidBookingStatusTransition(
                "Non è possibile registrare l'arrivo prima della data di check-in"
            )

        if new_status == BookingStatus.NO_SHOW and today <= booking.check_in:
            raise InvalidBookingStatusTransition(
                "La mancata presentazione può essere registrata solo dopo la data di arrivo"
            )

        # Una partenza anticipata è ammessa (dal 07/10/2026), ma non il giorno
        # stesso dell'arrivo: almeno una notte deve essere stata passata.
        if new_status == BookingStatus.COMPLETED and today <= booking.check_in:
            raise InvalidBookingStatusTransition(
                "Il soggiorno può risultare concluso solo dal giorno dopo l'arrivo"
            )

    @staticmethod
    def _assert_reason_when_required(booking: Booking, payload: BookingStatusUpdateSchema) -> None:
        """
        Motivazione obbligatoria per la **partenza anticipata**: le notti
        rimaste restano occupate e pagate, ed è il caso che più facilmente
        genera una contestazione. Serve una traccia del perché.

        Non sta nello schema, che non conosce le date della prenotazione;
        l'obbligo per l'annullamento invece sì (`BookingStatusUpdateSchema`).
        """
        is_early_departure = (
            payload.new_status == BookingStatus.COMPLETED
            and today_in_app_timezone() < booking.check_out
        )
        if is_early_departure and not (payload.reason or "").strip():
            raise StatusChangeReasonRequired(
                "Per concludere il soggiorno prima della data di partenza serve una motivazione"
            )

    @staticmethod
    def _assert_admin_transition(booking: Booking, new_status: BookingStatus) -> None:
        """
        Transizioni che la macchina a stati ammette ma che l'admin non può
        disporre, perché appartengono a un altro attore.

        `ALLOWED_TRANSITIONS` resta com'è: lo sweeper ha bisogno di
        `→ EXPIRED` e il webhook di `PENDING_PAYMENT → CONFIRMED`. Il vincolo
        riguarda **chi** chiede la transizione, non la transizione in sé.
        """
        if new_status == BookingStatus.EXPIRED:
            raise InvalidBookingStatusTransition(
                "La scadenza è gestita automaticamente dal sistema"
            )

        if (
                new_status == BookingStatus.CONFIRMED
                and booking.status == BookingStatus.PENDING_PAYMENT
        ):
            # Confermarla a mano lascerebbe una prenotazione scontata e mai
            # pagata, con un'autorizzazione Stripe eventualmente ancora viva.
            raise InvalidBookingStatusTransition(
                "Questa prenotazione si conferma solo con il pagamento online"
            )

    @staticmethod
    def _assert_manual_payment_allowed(booking: Booking, new_status: PaymentStatus) -> None:
        """
        Regole della registrazione manuale di un pagamento.

        | Richiesta  | Consentita se                                              |
        |:-----------|:-----------------------------------------------------------|
        | qualsiasi  | il pagamento non è avvenuto online                         |
        | `PAID`     | soggiorno confermato o in corso/concluso, non già pagato   |
        | `REFUNDED` | pagamento `PAID`, qualunque stato (anche annullata)        |
        | `PENDING`  | pagamento `PAID`: correzione di una registrazione sbagliata |

        Un pagamento online si rimborsa dalla dashboard di Stripe, e il
        webhook aggiorna lo stato: una registrazione manuale produrrebbe due
        versioni dello stesso fatto.
        """
        if booking.payment_method == PaymentMethod.STRIPE_CARD:
            raise InvalidPaymentOperation(
                "Il pagamento è avvenuto online: rimborsi e correzioni si gestiscono da Stripe"
            )

        if new_status == PaymentStatus.PAID:
            if booking.status not in _PAYABLE_ON_SITE_STATUSES:
                raise InvalidPaymentOperation(
                    "Non è possibile registrare un incasso su una prenotazione in questo stato"
                )
            if booking.payment_status == PaymentStatus.PAID:
                raise InvalidPaymentOperation("L'incasso risulta già registrato")
            return

        # REFUNDED e PENDING partono entrambi da un incasso esistente.
        if booking.payment_status != PaymentStatus.PAID:
            raise InvalidPaymentOperation(
                "L'operazione richiede che la prenotazione risulti pagata"
            )

    # ================================================================== #
    # Helper                                                             #
    # ================================================================== #

    async def _in_transaction(self, operation):
        """
        Esegue l'operazione come unità atomica.

        Delega a `run_in_transaction`, condivisa con il `PaymentService`: la
        logica è identica e una sua divergenza non produrrebbe un errore ma
        una scrittura persa in silenzio. Il perché di quella implementazione —
        e dell'autobegin di SQLAlchemy 2.0 che l'ha resa necessaria — è
        documentato lì.
        """
        return await run_in_transaction(self.session, operation)

    async def _secure_slots(
            self,
            room_ids: Sequence[UUID],
            check_in,
            check_out
    ) -> None:
        """
        Acquisisce gli slot richiesti dentro la transazione in corso.

        Tre passi, in quest'ordine preciso:

        1. **lock pessimistico** sulle camere, ordinato per id — due
           transazioni che bloccano le stesse camere in ordine diverso si
           attenderebbero a vicenda, ordinando sempre allo stesso modo il
           deadlock è impossibile;
        2. **just-in-time expiration** — libera gli slot il cui blocco è
           scaduto, così il vincolo del database non respinge una prenotazione
           legittima solo perché lo sweeper non è ancora passato;
        3. **verifica applicativa** — serve a produrre un `409` leggibile con
           i nomi delle camere, non a garantire la correttezza: quella la dà
           l'exclusion constraint all'INSERT.

        Condiviso da tutti i percorsi di creazione: due implementazioni di
        questo punto sarebbero destinate a divergere, e divergerebbero proprio
        dove fa più male.

        Il repository accetta anche un `exclude_booking_id`, qui non passato.
        Serviva alla modifica amministrativa, rimossa perché non sapeva
        trattare le prenotazioni già incassate (debito tecnico #21); resta
        disponibile per quando quel percorso verrà riscritto, perché una
        prenotazione che si allunga deve poter ignorare le proprie righe.
        """
        ids = list(room_ids)
        if not ids:
            return

        await self.booking_repository.lock_rooms_for_update(ids)
        await self.booking_repository.deactivate_expired_holds(ids)

        conflicts = await self.booking_repository.get_active_overlapping_items(
            ids, check_in, check_out
        )
        if conflicts:
            raise RoomNotAvailable(self._describe_conflicts(conflicts))

    async def _load_and_validate_rooms(self, room_ids: Sequence[UUID]) -> List[Room]:
        return await load_bookable_rooms(self.room_repository, room_ids)

    @staticmethod
    def _assert_capacity(rooms: Sequence[Room], guest_count: int) -> None:
        total_capacity = sum(room.capacity for room in rooms)
        if total_capacity < guest_count:
            raise InvalidGuestCount(
                f"Le camere selezionate ospitano al massimo {total_capacity} persone, "
                f"ne sono state indicate {guest_count}"
            )

    async def _generate_unique_code(self) -> str:
        """
        Genera il codice leggibile della prenotazione.

        Casuale e non sequenziale: un contatore progressivo rivelerebbe a
        chiunque prenoti il volume d'affari della struttura, e renderebbe i
        codici altrui indovinabili.
        """
        year = today_in_app_timezone().year

        for _ in range(_CODE_MAX_ATTEMPTS):
            suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
            code = f"{settings.booking_code_prefix}-{year}-{suffix}"

            if not await self.booking_repository.code_exists(code):
                return code

        raise AppException("Impossibile generare un codice prenotazione univoco, riprova")

    def _issue_confirmation_token(self, booking: Booking) -> str:
        """
        Emette il token di conferma e ne persiste solo l'hash.

        :return: valore in chiaro, destinato unicamente al link dell'email.
        """
        plain_token, token_hash = generate_booking_token()

        self.token_repository.add(
            BookingToken(
                booking_id=booking.id,
                token_hash=token_hash,
                purpose=BookingTokenPurpose.CONFIRM_EMAIL,
                # Il token scade insieme al blocco dello slot: confermare dopo
                # che le date sono tornate disponibili non avrebbe senso.
                expires_at=booking.hold_expires_at,
            )
        )
        return plain_token

    def _issue_manage_token(self, booking: Booking) -> str:
        """
        Emette il token di gestione e ne persiste solo l'hash.

        È la credenziale che `POST /bookings/cancel` richiede. Prima dello
        Step F nessun percorso di codice la emetteva: l'endpoint era scritto,
        testato a livello di servizio e documentato, ma nessun client poteva
        ottenerne una, quindi era di fatto irraggiungibile.

        **Scadenza all'arrivo**, con un tetto di sicurezza. Le regole su
        *cosa* l'ospite può fare stanno già in `_assert_guest_can_cancel`: un
        token che scadesse al termine di cancellazione gratuita gli toglierebbe
        anche la cancellazione a pagamento, che invece deve poter esercitare.

        Oltre il check-in, invece, non resta nulla da esercitare: la
        cancellazione richiede lo stato `CONFIRMED`, e dalla registrazione
        dell'arrivo in poi la prenotazione è `CHECKED_IN`. Il token vivrebbe
        quanto il soggiorno senza poter più fare nulla — una credenziale
        inutile che continua a circolare via email.

        :return: valore in chiaro, destinato unicamente al link dell'email.
        """
        plain_token, token_hash = generate_booking_token()

        expires_at = datetime.combine(
            booking.check_in, time.min, tzinfo=timezone.utc
        )
        ceiling = datetime.now(timezone.utc) + timedelta(days=settings.manage_token_max_days)
        expires_at = min(expires_at, ceiling)

        self.token_repository.add(
            BookingToken(
                booking_id=booking.id,
                token_hash=token_hash,
                purpose=BookingTokenPurpose.MANAGE,
                expires_at=expires_at,
            )
        )
        return plain_token

    @staticmethod
    def _is_overlap_violation(error: IntegrityError) -> bool:
        """Riconosce la collisione sull'exclusion constraint fra le altre violazioni."""
        return is_overlap_violation(error)

    @staticmethod
    def _describe_conflicts(conflicts: Sequence[BookingRoomItem]) -> str:
        names = sorted({item.room.name for item in conflicts if item.room is not None})
        if not names:
            return "Le date richieste non sono più disponibili"
        return f"Non più disponibili per le date richieste: {', '.join(names)}"

    # ------------------------------------------------------------------ #
    # Conversione verso i DTO                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_room_items(
            booking: Booking,
            rooms: Optional[Sequence[Room]] = None
    ) -> List[BookingRoomItemSchema]:
        rooms_by_id = {room.id: room for room in (rooms or [])}

        items: List[BookingRoomItemSchema] = []
        for item in booking.items:
            room = rooms_by_id.get(item.room_id) or item.room
            items.append(
                BookingRoomItemSchema(
                    room_id=item.room_id,
                    room_name=room.name if room else "",
                    room_number=room.number if room else 0,
                    check_in=item.check_in,
                    check_out=item.check_out,
                    nights=item.nights,
                    unit_price=item.unit_price,
                    line_total=item.line_total,
                )
            )
        return items

    def _to_public_schema(
            self,
            booking: Booking,
            rooms: Optional[Sequence[Room]] = None
    ) -> BookingPublicSchema:
        return BookingPublicSchema(
            code=booking.code,
            status=booking.status,
            check_in=booking.check_in,
            check_out=booking.check_out,
            nights=booking.nights,
            guest_count=booking.guest_count,
            guest_firstname=booking.guest_firstname,
            guest_lastname=booking.guest_lastname,
            guest_email=booking.guest_email,
            rooms=self._build_room_items(booking, rooms),
            base_price=booking.base_price,
            discount_amount=booking.discount_amount,
            total_price=booking.total_price,
            currency=booking.currency,
            payment_option=booking.payment_option,
            payment_status=booking.payment_status,
            hold_expires_at=booking.hold_expires_at,
            cancellation_deadline=booking.cancellation_deadline,
            confirmed_at=booking.confirmed_at,
        )

    async def _to_admin_schema(
            self,
            booking: Booking,
            rooms: Optional[Sequence[Room]] = None
    ) -> BookingSchema:
        """
        Vista amministrativa completa.

        È `async` per una ragione precisa: `created_at` e `updated_at` sono
        valorizzati dal database (`server_default` e `onupdate = now()`), quindi
        dopo ogni INSERT o UPDATE l'ORM li marca come scaduti. Leggerli
        direttamente farebbe partire una query implicita fuori dal contesto
        asincrono, con un `MissingGreenlet`. Vanno ricaricati esplicitamente.

        `_to_public_schema` non ha questo problema: non espone campi di audit.
        """
        # La cronologia si ricarica insieme alle date: chi ha appena cambiato
        # stato (o creato la prenotazione) deve vedere nella risposta anche la
        # voce appena scritta, che la relazione già caricata non conosce.
        # Senza, la risposta era incompleta e il client doveva rileggere.
        await self.session.refresh(
            booking, ["created_at", "updated_at", "status_history", "payment_history"]
        )

        history: List[BookingStatusHistorySchema] = []
        if "status_history" in booking.__dict__:
            history = [
                BookingStatusHistorySchema.model_validate(entry)
                for entry in booking.status_history
            ]
        payment_history = [
            BookingPaymentHistorySchema.model_validate(entry)
            for entry in booking.__dict__.get("payment_history", [])
        ]

        return BookingSchema(
            id=booking.id,
            code=booking.code,
            status=booking.status,
            source_channel=booking.source_channel,
            check_in=booking.check_in,
            check_out=booking.check_out,
            nights=booking.nights,
            guest_count=booking.guest_count,
            user_id=booking.user_id,
            guest_firstname=booking.guest_firstname,
            guest_lastname=booking.guest_lastname,
            guest_email=booking.guest_email,
            guest_phone=booking.guest_phone,
            rooms=self._build_room_items(booking, rooms),
            base_price=booking.base_price,
            discount_amount=booking.discount_amount,
            total_price=booking.total_price,
            currency=booking.currency,
            payment_option=booking.payment_option,
            payment_status=booking.payment_status,
            payment_method=booking.payment_method,
            hold_expires_at=booking.hold_expires_at,
            confirmed_at=booking.confirmed_at,
            cancelled_at=booking.cancelled_at,
            cancellation_deadline=booking.cancellation_deadline,
            cancellation_reason=booking.cancellation_reason,
            admin_notes=booking.admin_notes,
            created_at=booking.created_at,
            updated_at=booking.updated_at,
            created_by=booking.created_by,
            last_updated_by=booking.last_updated_by,
            # Contatore dell'optimistic locking: SQLAlchemy lo mantiene in
            # memoria e lo aggiorna dopo ogni flush, quindi non va ricaricato
            # come created_at/updated_at.
            version=booking.version,
            status_history=history,
            payment_history=payment_history,
        )


def build_booking_service(session: AsyncSession) -> BookingService:
    """
    Compone un `BookingService` completo su una sessione.

    Unico punto di composizione: lo usano il provider delle rotte
    (`get_booking_service`), lo sweeper e i test. Prima erano tre copie, e una
    dipendenza nuova andava ricordata in tutte e tre — dimenticarne una non dà
    un errore all'avvio ma al primo uso, in produzione.
    """
    return BookingService(
        session=session,
        booking_repository=BookingRepository(session),
        booking_token_repository=BookingTokenRepository(session),
        booking_status_history_repository=BookingStatusHistoryRepository(session),
        booking_payment_history_repository=BookingPaymentHistoryRepository(session),
        room_repository=RoomRepository(session),
        pricing_service=PricingService(),
    )
