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
import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
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
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import (
    AdminBookingCreateSchema,
    AdminBookingUpdateSchema,
    AdminPaymentRegistrationSchema,
    BookingExtendHoldSchema,
    BookingListItemSchema,
    BookingPublicSchema,
    BookingQuoteRequestSchema,
    BookingQuoteResponseSchema,
    BookingRoomItemSchema,
    BookingSchema,
    BookingSearchFiltersSchema,
    BookingStatusHistorySchema,
    BookingStatusUpdateSchema,
    GuestBookingCreateSchema,
    PaginatedBookingsSchema,
    UserBookingCreateSchema,
)
from src.exception.custom_exception import (
    AppException,
    BookingHoldExpired,
    BookingNotCancellable,
    ConcurrentModification,
    EntityNotFound,
    InvalidBookingStatusTransition,
    InvalidBookingToken,
    InvalidGuestCount,
    InvalidQuoteToken,
    RateLimitExceeded,
    RoomNotAvailable,
)
from src.security.audit_logging import apply_audit_fields
from src.security.booking_tokens import generate_booking_token, hash_booking_token
from src.security.quote_token import QuotePayload
from src.security.validators import today_in_app_timezone
from src.service.pricing_service import PricingService

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

#: Alfabeto del codice prenotazione: niente 0/O né 1/I/L, che al telefono si
#: confondono. 31^6 ≈ 887 milioni di combinazioni.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_CODE_MAX_ATTEMPTS = 10

#: Marcatore di audit per le prenotazioni create da ospiti non registrati.
_GUEST_AUDIT_MARKER = "GUEST"
_SYSTEM_AUDIT_MARKER = "System"

#: Nome dell'exclusion constraint, usato per riconoscere la collisione fra
#: prenotazioni concorrenti e distinguerla da altri errori di integrità.
_OVERLAP_CONSTRAINT = "ex_booking_room_items_no_overlap"
_EXCLUSION_VIOLATION_SQLSTATE = "23P01"


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
            room_repository: RoomRepository,
            pricing_service: PricingService
    ) -> None:
        self.session = session
        self.booking_repository = booking_repository
        self.token_repository = booking_token_repository
        self.history_repository = booking_status_history_repository
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
        token_hash = hash_booking_token(plain_token)

        token = await self.token_repository.get_by_hash(token_hash, BookingTokenPurpose.CANCEL)
        if token is None:
            token = await self.token_repository.get_by_hash(
                token_hash, BookingTokenPurpose.MANAGE
            )
        if token is None or token.used_at is not None:
            raise InvalidBookingToken()

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
    ) -> BookingSchema:
        return await self._in_transaction(
            lambda: self._execute_admin_change_status(booking_id, payload, admin_id)
        )

    async def _execute_admin_change_status(
            self,
            booking_id: UUID,
            payload: BookingStatusUpdateSchema,
            admin_id: UUID
    ) -> BookingSchema:
        booking = await self.booking_repository.get_by_id(
            booking_id, with_items=True, with_history=True
        )
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        self._assert_status_change_is_coherent(booking, payload.new_status)

        now = datetime.now(timezone.utc)
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
        elif payload.new_status == BookingStatus.CANCELLED:
            booking.cancelled_at = now
            booking.cancellation_reason = payload.reason

        if payload.new_status in (
                BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED
        ):
            await self.token_repository.invalidate_all_for_booking(booking.id)

        apply_audit_fields(audit=booking, user_id=admin_id)
        await self.booking_repository.flush()
        return await self._to_admin_schema(booking)

    async def admin_register_payment(
            self,
            booking_id: UUID,
            payload: AdminPaymentRegistrationSchema,
            admin_id: UUID
    ) -> BookingSchema:
        """Registrazione manuale di un incasso in struttura."""
        return await self._in_transaction(
            lambda: self._execute_register_payment(booking_id, payload, admin_id)
        )

    async def _execute_register_payment(
            self,
            booking_id: UUID,
            payload: AdminPaymentRegistrationSchema,
            admin_id: UUID
    ) -> BookingSchema:
        booking = await self.booking_repository.get_by_id(booking_id, with_items=True)
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        booking.payment_method = payload.payment_method
        booking.payment_status = payload.payment_status
        apply_audit_fields(audit=booking, user_id=admin_id)

        await self.booking_repository.flush()
        return await self._to_admin_schema(booking)

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

    async def admin_update_booking(
            self,
            booking_id: UUID,
            payload: AdminBookingUpdateSchema,
            admin_id: UUID
    ) -> BookingSchema:
        """
        Modifica una prenotazione esistente: date, camere, ospiti, anagrafica, note.

        È l'operazione più delicata dopo la creazione, perché può spostare uno
        slot già occupato su un altro. Riusa lo stesso `_secure_slots` della
        creazione, con l'accortezza di **escludere la prenotazione stessa**:
        senza, una prenotazione che si allunga di un giorno collidererebbe con
        le proprie righe e si rifiuterebbe da sola.
        """
        return await self._in_transaction(
            lambda: self._execute_admin_update(booking_id, payload, admin_id)
        )

    async def _execute_admin_update(
            self,
            booking_id: UUID,
            payload: AdminBookingUpdateSchema,
            admin_id: UUID
    ) -> BookingSchema:
        booking = await self.booking_repository.get_by_id(
            booking_id, with_items=True, with_history=True
        )
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        # Optimistic locking lato client: il valore arriva dalla GET che
        # l'operatore ha fatto prima di aprire il form.
        if booking.version != payload.version:
            raise ConcurrentModification()

        if booking.status.is_terminal:
            raise InvalidBookingStatusTransition(
                f"Una prenotazione in stato '{booking.status.value}' non è più modificabile"
            )

        rooms = await self._load_and_validate_rooms(payload.room_ids)
        self._assert_capacity(rooms, payload.guest_count)

        previous_check_in = booking.check_in
        previous_check_out = booking.check_out
        previous_room_ids = {item.room_id for item in booking.items}

        dates_changed = (
            booking.check_in != payload.check_in or booking.check_out != payload.check_out
        )
        rooms_changed = previous_room_ids != {room.id for room in rooms}

        if dates_changed or rooms_changed:
            await self._secure_slots(
                [room.id for room in rooms],
                payload.check_in,
                payload.check_out,
                exclude_booking_id=booking.id,
            )
            await self._rebuild_items(booking, rooms, payload)

        booking.guest_count = payload.guest_count
        booking.admin_notes = payload.admin_notes

        if payload.guest is not None:
            booking.guest_firstname = payload.guest.firstname
            booking.guest_lastname = payload.guest.lastname
            booking.guest_email = str(payload.guest.email)
            booking.guest_phone = payload.guest.phone_number

        if dates_changed and booking.status == BookingStatus.CONFIRMED:
            booking.cancellation_deadline = self.pricing_service.compute_cancellation_deadline(
                booking.check_in, booking.payment_option
            )

        # Lo storico registra anche le modifiche che non cambiano stato: "chi
        # ha spostato le date" è esattamente l'informazione che serve in caso
        # di contestazione con l'ospite.
        self.history_repository.add(
            booking_id=booking.id,
            from_status=booking.status,
            to_status=booking.status,
            actor_type=AuditActorType.ADMIN,
            actor_id=str(admin_id),
            reason=self._describe_update(
                payload, previous_check_in, previous_check_out,
                previous_room_ids, {room.id for room in rooms}
            ),
        )

        apply_audit_fields(audit=booking, user_id=admin_id)

        try:
            await self.booking_repository.flush()
        except IntegrityError as error:
            if self._is_overlap_violation(error):
                raise RoomNotAvailable() from error
            raise

        return await self._to_admin_schema(booking)

    async def _rebuild_items(
            self,
            booking: Booking,
            rooms: Sequence[Room],
            payload: AdminBookingUpdateSchema
    ) -> None:
        """
        Ricostruisce le righe camera e ricalcola gli importi.

        Le righe vecchie vengono cancellate e svuotate **prima** di toccare le
        date della prenotazione: la foreign key composita propaga le date ai
        figli con `ON UPDATE CASCADE`, e aggiornare righe che stiamo per
        eliminare produrrebbe un ordine di operazioni inutilmente fragile.

        Le nuove righe nascono con le date nuove; l'INSERT finale è il punto in
        cui l'exclusion constraint verifica che lo spostamento sia legittimo.
        """
        for item in list(booking.items):
            booking.items.remove(item)
        await self.booking_repository.flush()

        booking.check_in = payload.check_in
        booking.check_out = payload.check_out

        nights = self.pricing_service.calculate_nights(payload.check_in, payload.check_out)
        lines = self.pricing_service.build_price_lines(rooms, nights)
        base_price, discount_amount, total_price = self.pricing_service.compute_totals(
            lines, booking.payment_option
        )

        booking.base_price = base_price
        booking.discount_amount = discount_amount
        booking.total_price = total_price

        lines_by_room = {line.room_id: line for line in lines}
        for room in rooms:
            line = lines_by_room[room.id]
            booking.items.append(
                BookingRoomItem(
                    room_id=room.id,
                    check_in=payload.check_in,
                    check_out=payload.check_out,
                    unit_price=line.unit_price,
                    nights=nights,
                    line_total=line.line_total,
                )
            )

        self._sync_items_active_flag(booking)

    # ================================================================== #
    # Letture                                                            #
    # ================================================================== #

    # ================================================================== #
    # Scadenze                                                           #
    # ================================================================== #

    async def expire_pending(self, limit: Optional[int] = None) -> List[ExpiredBookingNotice]:
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
        :return: un avviso per prenotazione, con l'indicazione se valga ancora
            la pena notificarla.
        """
        return await self._in_transaction(lambda: self._execute_expire_pending(limit))

    async def _execute_expire_pending(
            self,
            limit: Optional[int]
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

        for booking in bookings:
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

    async def get_admin_booking(self, booking_id: UUID) -> BookingSchema:
        """Dettaglio completo con timeline degli stati, per il back-office."""
        booking = await self.booking_repository.get_by_id(
            booking_id, with_items=True, with_history=True
        )
        if booking is None:
            raise EntityNotFound("Prenotazione non trovata")

        return await self._to_admin_schema(booking)

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
                    guest_lastname=booking.guest_lastname,
                    guest_email=booking.guest_email,
                    rooms_count=len(booking.items),
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

    def _assert_guest_can_cancel(self, booking: Booking) -> None:
        """
        Verifica che l'ospite possa cancellare da sé.

        Una prenotazione già pagata online non è auto-cancellabile: l'importo
        è dovuto e l'eventuale rimborso passa da una valutazione della
        struttura. L'ospite viene indirizzato al contatto diretto invece di
        ricevere un rifiuto muto.
        """
        if booking.status in PENDING_BOOKING_STATUSES:
            return

        if booking.status != BookingStatus.CONFIRMED:
            raise BookingNotCancellable(
                "La prenotazione non si trova in uno stato che consente la cancellazione"
            )

        if booking.payment_option == PaymentOption.PAY_NOW:
            raise BookingNotCancellable(
                "Le prenotazioni con pagamento online anticipato non sono rimborsabili. "
                "Contatta la struttura per valutare la tua situazione."
            )

        deadline = booking.cancellation_deadline
        if deadline is not None and datetime.now(timezone.utc) > deadline:
            raise BookingNotCancellable(
                "Il termine per la cancellazione gratuita è scaduto. "
                "Contatta la struttura per assistenza."
            )

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

        if new_status == BookingStatus.COMPLETED and today < booking.check_out:
            raise InvalidBookingStatusTransition(
                "Il soggiorno non può risultare concluso prima della data di partenza"
            )

    # ================================================================== #
    # Helper                                                             #
    # ================================================================== #

    async def _in_transaction(self, operation):
        """
        Esegue l'operazione come unità atomica: commit in caso di successo,
        rollback a fronte di qualunque eccezione.

        ⚠️ **Non si usa `session.in_transaction()` per decidere se aprire una
        transazione.** SQLAlchemy 2.0 ha l'autobegin: una transazione si apre
        da sola alla **prima query**, anche di sola lettura. La dipendenza di
        autenticazione interroga la tabella utenti prima ancora di entrare
        nell'endpoint, quindi su ogni rotta protetta la sessione risulta già
        "in transazione". Il pattern `if in_transaction(): ... else: begin()`
        concludeva perciò che il commit spettasse a qualcun altro — e la
        scrittura non veniva mai persistita. Silenziosamente: la risposta HTTP
        era `201`, ma a database non restava nulla.

        Qui la transazione viene chiusa esplicitamente, senza ipotesi su chi
        l'abbia aperta.
        """
        try:
            result = await operation()
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def _secure_slots(
            self,
            room_ids: Sequence[UUID],
            check_in,
            check_out,
            exclude_booking_id: Optional[UUID] = None
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

        Condiviso da creazione e modifica: due implementazioni di questo punto
        sarebbero destinate a divergere, e divergerebbero proprio dove fa più
        male.

        :param exclude_booking_id: prenotazione da ignorare nel calcolo dei
            conflitti. Indispensabile in modifica: senza, una prenotazione che
            si allunga collidererebbe con le proprie righe.
        """
        ids = list(room_ids)
        if not ids:
            return

        await self.booking_repository.lock_rooms_for_update(ids)
        await self.booking_repository.deactivate_expired_holds(ids)

        conflicts = await self.booking_repository.get_active_overlapping_items(
            ids, check_in, check_out, exclude_booking_id=exclude_booking_id
        )
        if conflicts:
            raise RoomNotAvailable(self._describe_conflicts(conflicts))

    @staticmethod
    def _describe_update(
            payload: AdminBookingUpdateSchema,
            previous_check_in,
            previous_check_out,
            previous_room_ids,
            new_room_ids
    ) -> str:
        """Compone la descrizione della modifica registrata nello storico."""
        changes: List[str] = []

        if previous_check_in != payload.check_in or previous_check_out != payload.check_out:
            changes.append(
                f"date da {previous_check_in}/{previous_check_out} "
                f"a {payload.check_in}/{payload.check_out}"
            )

        if previous_room_ids != new_room_ids:
            changes.append(f"camere da {len(previous_room_ids)} a {len(new_room_ids)}")

        description = "Modifica amministrativa"
        if changes:
            description += ": " + ", ".join(changes)
        if payload.reason:
            description += f" — {payload.reason}"

        return description[:500]

    async def _load_and_validate_rooms(self, room_ids: Sequence[UUID]) -> List[Room]:
        rooms = await self.room_repository.get_all_by_ids(list(room_ids))

        if len(rooms) != len(set(room_ids)):
            raise EntityNotFound("Una o più camere selezionate non esistono")

        disabled = [room.name for room in rooms if not room.enabled]
        if disabled:
            raise RoomNotAvailable(
                f"Le seguenti camere non sono attualmente prenotabili: {', '.join(disabled)}"
            )

        return rooms

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

        **Scadenza alla partenza**, con un tetto di sicurezza. Le regole su
        *cosa* l'ospite può fare stanno già in `_assert_guest_can_cancel`: un
        token che scadesse al termine di cancellazione gratuita gli toglierebbe
        anche la cancellazione a pagamento, che invece deve poter esercitare.

        :return: valore in chiaro, destinato unicamente al link dell'email.
        """
        plain_token, token_hash = generate_booking_token()

        expires_at = datetime.combine(
            booking.check_out, time.min, tzinfo=timezone.utc
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
        original = getattr(error, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)

        if sqlstate == _EXCLUSION_VIOLATION_SQLSTATE:
            return True

        return _OVERLAP_CONSTRAINT in str(original or error)

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
        await self.session.refresh(booking, ["created_at", "updated_at"])

        history: List[BookingStatusHistorySchema] = []
        if "status_history" in booking.__dict__:
            history = [
                BookingStatusHistorySchema.model_validate(entry)
                for entry in booking.status_history
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
        )
