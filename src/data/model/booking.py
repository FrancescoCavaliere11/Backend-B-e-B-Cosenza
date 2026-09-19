import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UUID,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.database_config import Base
from src.data.enumerators import (
    BookingChannel,
    BookingStatus,
    PaymentMethod,
    PaymentOption,
    PaymentStatus,
)
from src.security.audit_logging import Auditable

# Import necessari a registrare le entità correlate nel registry SQLAlchemy.
# `Booking.rooms` risolve `secondary="booking_room_items"` per nome: la tabella
# deve essere già presente in `Base.metadata` quando i mapper vengono
# configurati. Nessuno di questi moduli importa `booking.py` (i riferimenti
# incrociati sono annotazioni stringa), quindi non si creano import ciclici.
from src.data.model.booking_room_item import BookingRoomItem  # noqa: F401
from src.data.model.booking_token import BookingToken  # noqa: F401
from src.data.model.booking_status_history import BookingStatusHistory  # noqa: F401


class Booking(Base, Auditable):
    """
    Prenotazione di una o più camere per un intervallo di date.

    Principi di design:

    * **Un solo soggiorno per prenotazione**: `check_in` / `check_out` valgono
      per tutte le camere prenotate. Le righe `BookingRoomItem` ne portano una
      copia (necessaria all'exclusion constraint anti double-booking) che il
      database mantiene allineata tramite foreign key composita. Camere su date
      diverse = prenotazioni diverse.
    * **Documento contrattuale**: l'anagrafica dell'ospite è salvata come
      *snapshot* (`guest_*`) al momento della prenotazione e non viene mai
      riallineata al profilo utente. Una modifica successiva dell'email o del
      telefono su `User` non deve alterare una prenotazione già emessa.
    * **Guest booking**: `user_id` è opzionale. È valorizzato solo quando la
      prenotazione è riconducibile a un account registrato; la cancellazione
      dell'utente esegue `SET NULL` e preserva lo storico contabile.
    * **Nessun dato di carta**: si persistono esclusivamente riferimenti
      opachi emessi da Stripe (`stripe_payment_intent_id`) e, al più, brand e
      ultime quattro cifre a scopo di sola visualizzazione.
    * **Optimistic locking**: `version` è la colonna di versione del mapper;
      due scritture concorrenti sullo stesso record fanno fallire la seconda
      con `StaleDataError`.
    """

    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # --- Identificativo leggibile -------------------------------------------
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)

    # --- Macchina a stati ----------------------------------------------------
    status: Mapped[BookingStatus] = mapped_column(
        SQLEnum(BookingStatus, name="booking_status"),
        nullable=False,
        default=BookingStatus.PENDING_CONFIRMATION,
        index=True,
    )
    source_channel: Mapped[BookingChannel] = mapped_column(
        SQLEnum(BookingChannel, name="booking_channel"),
        nullable=False,
    )

    # --- Soggiorno -----------------------------------------------------------
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)
    guest_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Intestatario --------------------------------------------------------
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", onupdate="CASCADE", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user: Mapped[Optional["User"]] = relationship(back_populates="bookings", lazy="select")

    guest_firstname: Mapped[str] = mapped_column(Text, nullable=False)
    guest_lastname: Mapped[str] = mapped_column(Text, nullable=False)
    guest_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    guest_phone: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Importi -------------------------------------------------------------
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="EUR", server_default="EUR"
    )

    # --- Pagamento -----------------------------------------------------------
    payment_option: Mapped[PaymentOption] = mapped_column(
        SQLEnum(PaymentOption, name="payment_option"),
        nullable=False,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SQLEnum(PaymentStatus, name="payment_status"),
        nullable=False,
        default=PaymentStatus.PENDING,
    )
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        SQLEnum(PaymentMethod, name="payment_method"),
        nullable=True,
    )

    # TODO [Step G - Stripe]: valorizzato da `StripeService.create_payment_intent()`
    #   usando `code` come idempotency key. Non memorizzare MAI PAN, CVV o dati
    #   di banda magnetica: qui transita solo l'identificativo opaco del Payment
    #   Intent. Lo scope PCI-DSS resta SAQ-A/SAQ-A-EP.
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, unique=True
    )
    card_brand: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    card_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)

    # --- Locking temporaneo e ciclo di vita ----------------------------------
    hold_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Back-office ---------------------------------------------------------
    admin_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Optimistic locking --------------------------------------------------
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # --- Relazioni -----------------------------------------------------------
    items: Mapped[List["BookingRoomItem"]] = relationship(
        back_populates="booking",
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    rooms: Mapped[List["Room"]] = relationship(
        secondary="booking_room_items",
        back_populates="bookings",
        viewonly=True,
        lazy="select",
    )

    tokens: Mapped[List["BookingToken"]] = relationship(
        back_populates="booking",
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    status_history: Mapped[List["BookingStatusHistory"]] = relationship(
        back_populates="booking",
        lazy="select",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BookingStatusHistory.created_at",
    )

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        # Bersaglio della foreign key composita di `BookingRoomItem`.
        # Tecnicamente ridondante (`id` è già PK), ma PostgreSQL esige un
        # vincolo UNIQUE sulla tripla per poterla referenziare. È ciò che rende
        # impossibile a una riga camera di avere date diverse dal soggiorno.
        UniqueConstraint("id", "check_in", "check_out", name="uq_bookings_id_dates"),
        CheckConstraint("check_out > check_in", name="ck_bookings_date_range"),
        CheckConstraint("guest_count >= 1", name="ck_bookings_guest_count"),
        CheckConstraint("total_price >= 0", name="ck_bookings_total_price"),
        CheckConstraint("base_price >= 0", name="ck_bookings_base_price"),
        CheckConstraint("discount_amount >= 0", name="ck_bookings_discount_amount"),
        Index("ix_bookings_status_hold", "status", "hold_expires_at"),
        Index("ix_bookings_stay_dates", "check_in", "check_out"),
    )

    @property
    def nights(self) -> int:
        """Numero di notti del soggiorno, derivato dalle date."""
        return (self.check_out - self.check_in).days

    def __repr__(self) -> str:
        return f"<Booking code={self.code} status={self.status} {self.check_in}->{self.check_out}>"
