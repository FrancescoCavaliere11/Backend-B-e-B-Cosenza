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
    MANUAL_PAYMENT_METHODS,
    MANUAL_PAYMENT_STATUSES,
    AuditActorType,
    BookingChannel,
    BookingSortOrder,
    BookingStatus,
    GuestCancellationBlock,
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
    validate_occupancy_window,
    validate_planning_window,
    validate_phone_number,
    validate_room_ids_list,
    validate_terms_accepted,
)

#: Limite di sicurezza sul numero di ospiti dichiarabile.
#: Il controllo che conta ("ospiti <= somma delle capienze delle camere
#: selezionate") richiede i dati delle camere e vive nel Service.
_MAX_GUEST_COUNT = 100

#: Lunghezza massima della motivazione di un'operazione sul pagamento: la
#: stessa delle transizioni di stato (`BookingStatusUpdateSchema.reason`).
_PAYMENT_REASON_MAX_LENGTH = 500
#: Operazioni manuali sul pagamento che richiedono una motivazione.
_PAYMENT_STATUSES_REQUIRING_REASON = frozenset({PaymentStatus.REFUNDED, PaymentStatus.PENDING})


# ===========================================================================
# Disponibilità
# ===========================================================================

class OccupancyRequestSchema(CustomModel):
    """
    Notti occupate di alcune camere in una finestra di date (calendario).

    La finestra è `[date_from, date_to)`: `date_to` è esclusa, come la data di
    partenza di un soggiorno.
    """

    room_ids: List[UUID]
    date_from: date
    date_to: date

    @field_validator("room_ids")
    @classmethod
    def validate_rooms(cls, value: List[UUID]) -> List[UUID]:
        return validate_room_ids_list(value)

    @model_validator(mode="after")
    def validate_window(self) -> "OccupancyRequestSchema":
        validate_occupancy_window(self.date_from, self.date_to)
        return self


class OccupancyResponseSchema(CustomModel):
    """
    Notti in cui **almeno una** delle camere richieste non è disponibile.

    La notte del giorno `d` è quella fra `d` e `d + 1`: una notte occupata il 12
    impedisce di dormire il 12, non di partire il 12.

    Volutamente solo date, già unite fra le camere: nessun codice, nome,
    durata o camera delle singole prenotazioni. L'endpoint è pubblico, e a
    chi prenota serve sapere *se* è libero, non *chi* occupa.
    """

    date_from: date
    date_to: date
    room_ids: List[UUID]
    unavailable_nights: List[date] = Field(default_factory=list)


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

    Le opzioni di pagamento devono essere coerenti fra loro
    (`validate_payment`): una prenotazione nata dal back-office non passa mai
    da Stripe, quindi ciò che non viene incassato qui non verrà incassato
    altrove.
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

    @model_validator(mode="after")
    def validate_payment(self) -> "AdminBookingCreateSchema":
        """
        Rifiuta le combinazioni che lascerebbero la prenotazione in uno stato
        impossibile da completare.

        - `payment_method` descrive come l'ospite **ha già pagato**: esiste
          solo insieme a `mark_as_paid`, ed è sempre un metodo manuale.
        - Una prenotazione pagata non può nascere in attesa di conferma: se
          poi scadesse, l'incasso resterebbe su una prenotazione `EXPIRED`.
        - `PAY_NOW` va incassato subito: nata `CONFIRMED`, la prenotazione
          non potrebbe più essere pagata online (`start_payment` la rifiuta
          come già confermata), e resterebbe scontata e mai saldata.
        """
        if self.payment_method is not None and self.payment_method not in MANUAL_PAYMENT_METHODS:
            raise ValueError("Il pagamento con carta online non si registra dal back-office")

        if self.payment_method is not None and not self.mark_as_paid:
            raise ValueError(
                "Il metodo di pagamento si indica solo registrando l'incasso"
            )

        if self.mark_as_paid and self.payment_method is None:
            raise ValueError("Indica il metodo con cui l'ospite ha pagato")

        if self.mark_as_paid and not self.skip_email_confirmation:
            raise ValueError(
                "Una prenotazione già pagata non può restare in attesa di conferma"
            )

        if self.payment_option == PaymentOption.PAY_NOW and not self.mark_as_paid:
            raise ValueError(
                "Il pagamento anticipato dal back-office va registrato come incassato"
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


class BookingManageRequestSchema(CustomModel):
    """
    Lettura di una prenotazione dal link di gestione.

    Stesso token di `BookingCancelSchema`, senza `reason`: qui non si cancella
    nulla. Schema separato e non riuso del precedente perché i due endpoint
    hanno cicli di vita diversi — questo è idempotente, quello consuma il
    token — e un campo `reason` accettato su una lettura sarebbe solo un
    invito a fraintendere.
    """

    token: str = Field(min_length=20, max_length=512)


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
    """
    Registrazione manuale di un pagamento in struttura: incasso, rimborso o
    correzione di una registrazione sbagliata.

    **Non c'è un campo importo.** C'era, ma il modello non ha dove salvarlo:
    `Booking` registra quanto è dovuto, non quanto è stato incassato (debito
    #21). Accettarlo e scartarlo in silenzio faceva credere al back-office di
    averlo registrato. `extra="forbid"` fa sì che un client che lo invia
    ancora riceva un `422` esplicito invece di un `200` fuorviante.

    Le regole che dipendono dallo stato della prenotazione vivono nel Service
    (`_assert_manual_payment_allowed`); qui solo quelle che non ne dipendono.
    """

    model_config = ConfigDict(extra="forbid")

    payment_status: PaymentStatus
    payment_method: Optional[PaymentMethod] = None
    #: Finisce nella cronologia dei pagamenti. Obbligatoria per rimborso e
    #: correzione (`validate_reason`): sono le operazioni che tolgono un
    #: incasso, e senza una traccia del perché non si ricostruiscono.
    reason: Optional[str] = Field(default=None, max_length=_PAYMENT_REASON_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: Optional[str]) -> Optional[str]:
        # Spazi ai bordi tolti, e una motivazione fatta solo di spazi vale assente.
        if value is None:
            return None
        return value.strip() or None

    @field_validator("payment_status")
    @classmethod
    def validate_manual_status(cls, value: PaymentStatus) -> PaymentStatus:
        if value not in MANUAL_PAYMENT_STATUSES:
            raise ValueError(
                "Dal back-office si registra solo un incasso, un rimborso o una correzione"
            )
        return value

    @field_validator("payment_method")
    @classmethod
    def validate_manual_method(cls, value: Optional[PaymentMethod]) -> Optional[PaymentMethod]:
        if value is not None and value not in MANUAL_PAYMENT_METHODS:
            raise ValueError("Il pagamento con carta online non si registra dal back-office")
        return value

    @model_validator(mode="after")
    def validate_method_required(self) -> "AdminPaymentRegistrationSchema":
        if self.payment_status == PaymentStatus.PAID and self.payment_method is None:
            raise ValueError("Indica il metodo con cui l'ospite ha pagato")
        return self

    @model_validator(mode="after")
    def validate_reason(self) -> "AdminPaymentRegistrationSchema":
        if self.payment_status in _PAYMENT_STATUSES_REQUIRING_REASON and self.reason is None:
            raise ValueError("La motivazione è obbligatoria per un rimborso o una correzione")
        return self


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


class BookingPaymentHistorySchema(CustomModel):
    """
    Voce della timeline del pagamento.

    Come per lo storico degli stati, `actor_id` è escluso: identifica un
    operatore interno. Non c'è un importo (debito #21).
    """

    from_status: Optional[PaymentStatus]
    to_status: PaymentStatus
    payment_method: Optional[PaymentMethod]
    actor_type: AuditActorType
    reason: Optional[str]
    created_at: datetime

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


class GuestCancellationPolicySchema(CustomModel):
    """
    Se e a quali condizioni l'ospite può cancellare da sé.

    Serve alla pagina di gestione per **dire all'ospite cosa succederà prima
    che clicchi**, invece di scoprirlo con un `409`. Proviene dalla stessa
    funzione che `POST /cancel` usa per decidere, quindi ciò che la pagina
    mostra e ciò che l'endpoint farà non possono divergere.
    """

    #: `true` se `POST /cancel` andrebbe a buon fine adesso.
    can_cancel: bool
    #: Valorizzato solo quando `can_cancel` è `false`.
    blocked_by: Optional[GuestCancellationBlock] = None
    #: Lo stesso messaggio che `POST /cancel` restituirebbe nel `409`.
    #: Utilizzabile così com'è, o sostituibile dal frontend usando
    #: `blocked_by`.
    message: Optional[str] = None
    #: Termine entro cui la cancellazione è gratuita, quando esiste.
    free_until: Optional[datetime] = None


class BookingManageSchema(CustomModel):
    """Risposta di `POST /bookings/manage`."""

    booking: BookingPublicSchema
    cancellation: GuestCancellationPolicySchema


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

    #: Contatore di versione per l'optimistic locking, mantenuto dal database
    #: (`version_id_col`). Nessun endpoint lo accetta più in ingresso: la
    #: modifica amministrativa è stata rimossa (debito tecnico #21). Resta
    #: esposto perché è il valore che un client dovrà rimandare quando quel
    #: percorso verrà riscritto.
    #: Volutamente assente da `BookingPublicSchema`: all'ospite non serve.
    version: int

    status_history: List[BookingStatusHistorySchema] = Field(default_factory=list)
    payment_history: List[BookingPaymentHistorySchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AdminBookingCreatedSchema(CustomModel):
    """
    Risposta alla creazione amministrativa.

    Porta la vista completa, non quella pubblica: l'admin deve vedere audit,
    canale di origine e note interne.
    """

    booking: BookingSchema
    confirmation_token: Optional[str] = None


class BookingListItemSchema(CustomModel):
    """Riga di elenco per il back-office: solo ciò che serve alla tabella."""

    id: UUID
    code: str
    status: BookingStatus
    check_in: date
    check_out: date
    guest_firstname: str
    guest_lastname: str
    guest_email: EmailStr
    guest_count: int
    rooms_count: int
    #: Nomi delle camere, nell'ordine delle righe camera. Già caricati dalla
    #: query dell'elenco: nessun costo aggiuntivo.
    room_names: List[str] = Field(default_factory=list)
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
    sort: BookingSortOrder = BookingSortOrder.CHECK_IN_DESC

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


# ===========================================================================
# Manutenzione
# ===========================================================================

class SweepResultSchema(CustomModel):
    """
    Esito di una passata dello sweeper delle scadenze.

    `notified_count` è minore o uguale a `expired_count`: le scadenze più
    vecchie della soglia configurata vengono sistemate a database ma non
    notificate all'ospite.
    """

    expired_count: int = Field(description="Prenotazioni portate a EXPIRED")
    notified_count: int = Field(description="Ospiti avvisati via email")
    swept_at: datetime = Field(description="Istante di esecuzione, in UTC")


# ===========================================================================
# Tabellone del back-office
# ===========================================================================

class PlanningRequestSchema(CustomModel):
    """
    Finestra del tabellone: `[date_from, date_to)`, `date_to` esclusa come la
    data di partenza di un soggiorno. Il passato è ammesso.
    """

    date_from: date
    date_to: date

    @model_validator(mode="after")
    def validate_window(self) -> "PlanningRequestSchema":
        validate_planning_window(self.date_from, self.date_to)
        return self


class PlanningRoomSchema(CustomModel):
    """Riga del tabellone."""

    id: UUID
    number: int
    name: str
    #: Una camera disattivata compare solo se ha soggiorni nella finestra.
    enabled: bool


class PlanningStaySchema(CustomModel):
    """
    Barra del tabellone: **una per camera** di ogni prenotazione.

    Contiene solo ciò che si vede sulla barra e serve a riconoscerla. Email e
    telefono restano nel dettaglio (`GET /admin/bookings/{id}`): il tabellone
    elenca molti ospiti insieme, e meno dati personali porta meglio è.

    Le date sono quelle reali, anche quando sporgono dalla finestra: il
    ritaglio è un problema di disegno e spetta al client.
    """

    booking_id: UUID
    code: str
    room_id: UUID
    check_in: date
    check_out: date
    status: BookingStatus
    payment_status: PaymentStatus
    guest_name: str = Field(description="Nome e cognome dell'ospite")
    guest_count: int
    #: Valorizzato solo per le prenotazioni in attesa: scadenza del blocco.
    hold_expires_at: Optional[datetime] = None


class PlanningSchema(CustomModel):
    """
    Camere e soggiorni di una finestra di date.

    Compaiono solo i soggiorni che **occupano** le camere, con la stessa
    definizione di disponibilità e calendario: annullate, scadute, non
    presentate e prenotazioni in attesa con blocco scaduto non ci sono, perché
    le loro notti sono libere. Per questo due barre della stessa camera non si
    sovrappongono mai (vincolo anti-overbooking del database).
    """

    date_from: date
    date_to: date
    rooms: List[PlanningRoomSchema] = Field(default_factory=list)
    stays: List[PlanningStaySchema] = Field(default_factory=list)


# ===========================================================================
# Pagamenti
# ===========================================================================

class PaymentIntentRequestSchema(CustomModel):
    """
    Richiesta di avvio del pagamento.

    Richiede codice **e** email, come il lookup: il solo codice non deve
    bastare ad aprire un pagamento su una prenotazione altrui.
    """

    code: str = Field(max_length=20, description="Codice prenotazione")
    email: EmailStr = Field(description="Email indicata nella prenotazione")

    @field_validator("code")
    @classmethod
    def normalizza_codice(cls, value: str) -> str:
        return value.strip().upper()


class PaymentIntentSchema(CustomModel):
    """
    Ciò che serve al browser per completare il pagamento.

    `client_secret` consente di pagare **quella** prenotazione e nient'altro:
    non è una chiave segreta e va trasmesso al browser, ma non ha ragione di
    comparire nei log.

    `amount` viaggia per essere mostrato all'ospite. Non è un dato che il
    client possa modificare per cambiare l'addebito: l'importo vero è quello
    che il server ha comunicato a Stripe, e viene comunque riverificato prima
    dell'incasso.
    """

    client_secret: Optional[str] = Field(description="Segreto del Payment Intent")
    amount: Decimal = Field(description="Totale da pagare")
    currency: str
    booking_code: str


class WebhookResultSchema(CustomModel):
    """
    Esito dell'elaborazione di una notifica.

    Torna a Stripe, che guarda solo il codice di stato, ma è preziosa nei log
    e nei test: dice *quale ramo* è stato percorso, non solo che è andato bene.
    """

    event_id: str
    event_type: str
    outcome: str = Field(
        description=(
            "CONFIRMED · ALREADY_CONFIRMED · SLOT_LOST · AMOUNT_MISMATCH · "
            "PAYMENT_FAILED · REFUNDED · CANCELED · DUPLICATE · IGNORED · "
            "UNKNOWN_BOOKING · NOT_PAYABLE"
        )
    )
