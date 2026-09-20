"""
Contratti di input/output del modulo Booking.

Principi applicati a tutti gli schemi di questo file:

* **Nessuno schema di input contiene un campo prezzo.** Il totale entra
  esclusivamente attraverso il `quote_token` firmato dal server e viene
  comunque ricalcolato prima della persistenza.
* **Gli schemi di output pubblici non espongono mai** identificativi interni,
  campi di audit, note amministrative, `user_id`, `version` o riferimenti
  Stripe. Per quelli esiste `BookingSchema`, riservato al back-office.
* Le regole di soggiorno (notti minime/massime, anticipo massimo, date nel
  passato) vivono in `src/security/validators.py`, così restano un'unica fonte
  di verità condivisa fra contratti pubblici e amministrativi.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import ConfigDict, EmailStr, Field, field_validator, model_validator

from src.config.schemas_config import CustomModel
from src.data.enumerators import (
    AuditActorType,
    BookingChannel,
    BookingStatus,
    PaymentMethod,
    PaymentOption,
    PaymentStatus,
)
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.security.validators import (
    validate_booking_code,
    validate_booking_date_range,
    validate_guest_firstname,
    validate_guest_lastname,
    validate_honeypot,
    validate_phone_number,
    validate_room_ids_list,
    validate_terms_accepted,
)

#: Limite di sicurezza sul numero di ospiti dichiarabile.
#: Il controllo che conta ("ospiti <= somma delle capienze delle camere
#: selezionate") richiede i dati delle camere e vive nel Service.
_MAX_GUEST_COUNT = 100


# ===========================================================================
# Disponibilità
# ===========================================================================

class AvailabilityRequestSchema(CustomModel):
    """Ricerca delle camere libere in un intervallo."""

    check_in: date
    check_out: date
    guest_count: int = Field(ge=1, le=_MAX_GUEST_COUNT)

    @model_validator(mode="after")
    def validate_dates(self) -> "AvailabilityRequestSchema":
        validate_booking_date_range(self.check_in, self.check_out)
        return self


class AvailableRoomSchema(CustomModel):
    """Camera disponibile, con il prezzo calcolato per l'intervallo richiesto."""

    id: UUID
    name: str
    number: int
    capacity: int
    price_per_night: Decimal
    nights: int
    subtotal: Decimal
    services: List[RoomServiceSchema] = Field(default_factory=list)

    #: La camera, da sola, ospita tutte le persone indicate nella ricerca.
    #: Consente al frontend la modalità "camera singola sufficiente" senza una
    #: seconda chiamata e senza che il backend nasconda le altre camere.
    fits_all_guests: bool = False

    model_config = ConfigDict(from_attributes=True)


class RoomCombinationSchema(CustomModel):
    """
    Combinazione di camere che insieme ospitano tutti gli ospiti.

    Porta solo gli identificativi: i dati delle camere sono già in
    `AvailabilityResponseSchema.rooms`, e ripeterli moltiplicherebbe il
    payload per il numero di combinazioni proposte.

    Vengono restituite solo combinazioni **minimali**: se togliendo una camera
    gli ospiti ci starebbero comunque, la combinazione non viene proposta.
    """

    room_ids: List[UUID]
    rooms_count: int
    total_capacity: int
    total_price: Decimal

    #: Posti letto eccedenti rispetto agli ospiti. A parità di numero di
    #: camere e di prezzo si preferisce la combinazione che spreca meno.
    wasted_capacity: int


class AvailabilityResponseSchema(CustomModel):
    """
    Esito della ricerca di disponibilità.

    Serve le tre modalità di prenotazione con una sola chiamata:

    * *camera singola sufficiente*: filtrare `rooms` su `fits_all_guests`;
    * *scelta manuale*: presentare `rooms` per intero — il vincolo
      "capienza totale ≥ ospiti" è verificato al preventivo, senza nascondere
      camere all'utente;
    * *combinazioni suggerite*: usare `suggested_combinations`.
    """

    check_in: date
    check_out: date
    nights: int
    guest_count: int
    rooms: List[AvailableRoomSchema] = Field(default_factory=list)
    suggested_combinations: List[RoomCombinationSchema] = Field(default_factory=list)


# ===========================================================================
# Preventivo
# ===========================================================================

class BookingQuoteRequestSchema(CustomModel):
    """
    Richiesta di preventivo per una selezione di camere.

    È il primo dei due passi obbligatori della prenotazione pubblica: il
    client non può creare una prenotazione senza presentare il token emesso
    da questo endpoint.
    """

    check_in: date
    check_out: date
    guest_count: int = Field(ge=1, le=_MAX_GUEST_COUNT)
    room_ids: List[UUID]
    payment_option: PaymentOption

    @field_validator("room_ids")
    @classmethod
    def validate_rooms(cls, value: List[UUID]) -> List[UUID]:
        return validate_room_ids_list(value)

    @model_validator(mode="after")
    def validate_dates(self) -> "BookingQuoteRequestSchema":
        validate_booking_date_range(self.check_in, self.check_out)
        return self


class PriceLineSchema(CustomModel):
    """Riga del preventivo: una camera per l'intero soggiorno."""

    room_id: UUID
    room_name: str
    unit_price: Decimal
    nights: int
    line_total: Decimal


class BookingQuoteResponseSchema(CustomModel):
    """
    Preventivo firmato.

    `quote_token` è un JWT a breve scadenza che racchiude camere, date, ospiti,
    opzione di pagamento e totale calcolato. Serve a due scopi: impedire la
    manomissione del prezzo e fare da nonce contro il doppio invio del form.
    """

    check_in: date
    check_out: date
    nights: int
    guest_count: int
    payment_option: PaymentOption
    lines: List[PriceLineSchema]
    base_price: Decimal
    discount_amount: Decimal
    total_price: Decimal
    currency: str
    quote_token: str
    quote_expires_at: datetime


# ===========================================================================
# Anagrafica ospite
# ===========================================================================

class GuestDataSchema(CustomModel):
    """
    Dati dell'ospite intestatario, forniti contestualmente alla prenotazione.

    Finiscono nello *snapshot* anagrafico del `Booking` e non vengono più
    riallineati a un eventuale profilo utente.
    """

    firstname: str = Field(min_length=2, max_length=50)
    lastname: str = Field(min_length=2, max_length=50)
    email: EmailStr
    phone_number: str = Field(min_length=10, max_length=10)

    @field_validator("firstname")
    @classmethod
    def validate_firstname(cls, value: str) -> str:
        return validate_guest_firstname(value)

    @field_validator("lastname")
    @classmethod
    def validate_lastname(cls, value: str) -> str:
        return validate_guest_lastname(value)

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return validate_phone_number(value)


# ===========================================================================
# Creazione prenotazione
# ===========================================================================

class GuestBookingCreateSchema(CustomModel):
    """Creazione da parte di un ospite non registrato (endpoint pubblico)."""

    quote_token: str = Field(min_length=20, max_length=4096)
    guest: GuestDataSchema
    accept_terms: bool
    captcha_token: Optional[str] = Field(default=None, max_length=4096)

    #: Campo trappola: nascosto via CSS nel form, un utente reale non lo
    #: compila mai. Se arriva valorizzato, la richiesta è automatizzata.
    website: Optional[str] = Field(default=None, max_length=255)

    @field_validator("accept_terms")
    @classmethod
    def validate_terms(cls, value: bool) -> bool:
        return validate_terms_accepted(value)

    @field_validator("website")
    @classmethod
    def validate_trap(cls, value: Optional[str]) -> Optional[str]:
        return validate_honeypot(value)


class UserBookingCreateSchema(CustomModel):
    """
    Creazione da parte di un utente autenticato.

    L'anagrafica non viene richiesta: il Service la copia dal profilo per
    costruire lo snapshot. Nessun captcha: la sessione autenticata è già una
    barriera sufficiente contro l'automazione.
    """

    quote_token: str = Field(min_length=20, max_length=4096)
    accept_terms: bool

    @field_validator("accept_terms")
    @classmethod
    def validate_terms(cls, value: bool) -> bool:
        return validate_terms_accepted(value)


class AdminBookingCreateSchema(CustomModel):
    """
    Creazione "on behalf of" dal back-office.

    Non usa il `quote_token`: l'admin è un attore fidato e il prezzo viene
    calcolato direttamente dal server. Può inoltre saltare la conferma via
    email e registrare subito l'incasso.

    L'intestatario è `user_id` **oppure** `guest`, mai entrambi né nessuno dei
    due.
    """

    check_in: date
    check_out: date
    guest_count: int = Field(ge=1, le=_MAX_GUEST_COUNT)
    room_ids: List[UUID]
    payment_option: PaymentOption
    payment_method: Optional[PaymentMethod] = None

    user_id: Optional[UUID] = None
    guest: Optional[GuestDataSchema] = None

    skip_email_confirmation: bool = True
    mark_as_paid: bool = False
    admin_notes: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("room_ids")
    @classmethod
    def validate_rooms(cls, value: List[UUID]) -> List[UUID]:
        return validate_room_ids_list(value)

    @model_validator(mode="after")
    def validate_dates(self) -> "AdminBookingCreateSchema":
        # `allow_past=True`: il back-office deve poter registrare a posteriori
        # un walk-in o correggere un inserimento sbagliato.
        #
        # TODO [FRONTEND]: nel form di creazione prenotazione lato admin,
        #   quando la data di arrivo è precedente a oggi mostrare un dialog di
        #   conferma esplicito ("Stai registrando una prenotazione con data di
        #   arrivo nel passato. Confermi?") prima di inviare la richiesta. Il
        #   backend lo consente di proposito, quindi l'unica difesa contro il
        #   refuso di digitazione è quell'avviso.
        validate_booking_date_range(self.check_in, self.check_out, allow_past=True)
        return self

    @model_validator(mode="after")
    def validate_holder(self) -> "AdminBookingCreateSchema":
        if self.user_id is not None and self.guest is not None:
            raise ValueError(
                "Indica un utente registrato oppure i dati di un ospite, non entrambi"
            )
        if self.user_id is None and self.guest is None:
            raise ValueError(
                "È necessario indicare un utente registrato oppure i dati dell'ospite"
            )
        return self


# ===========================================================================
# Azioni sulla prenotazione
# ===========================================================================

class BookingConfirmSchema(CustomModel):
    """
    Conferma via token ricevuto per email.

    Il token viaggia nel **body** e mai in query string: un token nell'URL
    finirebbe in access log, header `Referer` e cronologia del browser.
    """

    token: str = Field(min_length=20, max_length=512)


class BookingCancelSchema(CustomModel):
    token: str = Field(min_length=20, max_length=512)
    reason: Optional[str] = Field(default=None, max_length=500)


class OwnBookingCancelSchema(CustomModel):
    """
    Cancellazione da parte dell'utente autenticato intestatario.

    Non richiede token: l'identità è già provata dalla sessione, e la proprietà
    della prenotazione viene verificata dal Service.
    """

    reason: Optional[str] = Field(default=None, max_length=500)


class BookingLookupSchema(CustomModel):
    """
    Consultazione di una prenotazione da parte di un ospite non registrato.

    Richiede codice **e** email: il solo codice non basta, per evitare che
    tentativi a forza bruta espongano dati di terzi.
    """

    code: str = Field(min_length=3, max_length=20)
    email: EmailStr

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return validate_booking_code(value)


class BookingStatusUpdateSchema(CustomModel):
    """Cambio di stato disposto dall'admin."""

    new_status: BookingStatus
    reason: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_reason(self) -> "BookingStatusUpdateSchema":
        if self.new_status == BookingStatus.CANCELLED and not (self.reason or "").strip():
            raise ValueError("La motivazione è obbligatoria per annullare una prenotazione")
        return self


class AdminPaymentRegistrationSchema(CustomModel):
    """Registrazione manuale di un incasso (contanti, POS, bonifico)."""

    payment_method: PaymentMethod
    payment_status: PaymentStatus
    amount: Optional[Decimal] = Field(default=None, ge=0, max_digits=10, decimal_places=2)


class BookingExtendHoldSchema(CustomModel):
    """Proroga del blocco temporaneo su una prenotazione ancora in attesa."""

    minutes: int = Field(ge=1, le=120)


# ===========================================================================
# Output
# ===========================================================================

class BookingRoomItemSchema(CustomModel):
    """Riga camera di una prenotazione, con il prezzo congelato all'emissione."""

    room_id: UUID
    room_name: str
    room_number: int
    check_in: date
    check_out: date
    nights: int
    unit_price: Decimal
    line_total: Decimal

    model_config = ConfigDict(from_attributes=True)


class BookingStatusHistorySchema(CustomModel):
    """
    Voce della timeline di stato.

    `actor_id` è volutamente escluso: identifica un operatore interno e non
    riguarda chi consulta la prenotazione.
    """

    from_status: Optional[BookingStatus]
    to_status: BookingStatus
    actor_type: AuditActorType
    reason: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingPublicSchema(CustomModel):
    """
    Vista destinata all'ospite.

    Esclude identificativi interni, campi di audit, note amministrative,
    `user_id`, `version` e qualunque riferimento Stripe.
    """

    code: str
    status: BookingStatus
    check_in: date
    check_out: date
    nights: int
    guest_count: int
    guest_firstname: str
    guest_lastname: str
    guest_email: EmailStr
    rooms: List[BookingRoomItemSchema] = Field(default_factory=list)
    base_price: Decimal
    discount_amount: Decimal
    total_price: Decimal
    currency: str
    payment_option: PaymentOption
    payment_status: PaymentStatus
    hold_expires_at: Optional[datetime] = None
    cancellation_deadline: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class BookingCreatedSchema(CustomModel):
    """
    Risposta alla creazione di una prenotazione.

    `confirmation_token` è valorizzato **solo quando l'invio email è
    disattivato** (`settings.email_enabled is False`), cioè in sviluppo.

    Serve a rendere collaudabile il flusso completo prima che esista
    l'`EmailService` (Step F): senza, nessuno potrebbe confermare una
    prenotazione e il ciclo non sarebbe verificabile end-to-end.

    TODO [Step F]: con l'`EmailService` attivo il campo si spegne da sé, perché
      `email_enabled` passa a True e il token viaggia per email. Valutare in
      quel momento se rimuoverlo del tutto: finché resta condizionato alla
      configurazione non è una falla, ma un campo in meno è un campo in meno.
    """

    booking: BookingPublicSchema
    confirmation_token: Optional[str] = None


class BookingSchema(CustomModel):
    """Vista completa, riservata al back-office."""

    id: UUID
    code: str
    status: BookingStatus
    source_channel: BookingChannel
    check_in: date
    check_out: date
    nights: int
    guest_count: int

    user_id: Optional[UUID]
    guest_firstname: str
    guest_lastname: str
    guest_email: EmailStr
    guest_phone: str

    rooms: List[BookingRoomItemSchema] = Field(default_factory=list)

    base_price: Decimal
    discount_amount: Decimal
    total_price: Decimal
    currency: str

    payment_option: PaymentOption
    payment_status: PaymentStatus
    payment_method: Optional[PaymentMethod]

    hold_expires_at: Optional[datetime]
    confirmed_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    cancellation_deadline: Optional[datetime]
    cancellation_reason: Optional[str]
    admin_notes: Optional[str]

    created_at: datetime
    updated_at: datetime
    created_by: str
    last_updated_by: str

    status_history: List[BookingStatusHistorySchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class BookingListItemSchema(CustomModel):
    """Riga di elenco per il back-office: solo ciò che serve alla tabella."""

    id: UUID
    code: str
    status: BookingStatus
    check_in: date
    check_out: date
    guest_lastname: str
    guest_email: EmailStr
    rooms_count: int
    total_price: Decimal
    payment_status: PaymentStatus

    model_config = ConfigDict(from_attributes=True)


class PaginatedBookingsSchema(CustomModel):
    items: List[BookingListItemSchema]
    total: int
    page: int
    page_size: int
    pages: int


# ===========================================================================
# Filtri di ricerca
# ===========================================================================

class BookingSearchFiltersSchema(CustomModel):
    """Filtri dell'elenco amministrativo delle prenotazioni."""

    status: Optional[List[BookingStatus]] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    email: Optional[EmailStr] = None
    code: Optional[str] = Field(default=None, max_length=20)
    room_id: Optional[UUID] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_range(self) -> "BookingSearchFiltersSchema":
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError("La data finale non può precedere quella iniziale")
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
