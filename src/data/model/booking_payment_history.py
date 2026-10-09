import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UUID, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.database_config import Base
from src.data.enumerators import AuditActorType, PaymentMethod, PaymentStatus


class BookingPaymentHistory(Base):
    """
    Timeline immutabile dei cambi di stato del **pagamento** di una
    prenotazione: incassi, rimborsi, correzioni, esiti di Stripe.

    È la sorella di `BookingStatusHistory`, che registra le transizioni della
    prenotazione. Le due timeline sono separate perché descrivono due macchine
    a stati diverse (`BookingStatus` e `PaymentStatus`), che cambiano anche in
    momenti diversi: un incasso al banco non è una transizione di stato.

    Come la sorella, è append-only e non eredita da `Auditable`: una riga di
    storico non si modifica, e l'autore è la coppia `actor_type` / `actor_id`.

    **Nessun importo**, di proposito: il modello registra quanto è dovuto, non
    quanto è stato incassato (debito #21). Una colonna importo qui darebbe
    l'impressione di una contabilità che non esiste. **Nessun dato dell'ospite**:
    la riga identifica la prenotazione, non la persona.
    """

    __tablename__ = "booking_payment_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: `None` quando la prenotazione nasce già con un incasso registrato.
    from_status: Mapped[Optional[PaymentStatus]] = mapped_column(
        SQLEnum(PaymentStatus, name="payment_status"),
        nullable=True,
    )
    to_status: Mapped[PaymentStatus] = mapped_column(
        SQLEnum(PaymentStatus, name="payment_status"),
        nullable=False,
    )

    #: Metodo della prenotazione **dopo** il cambiamento (`None` su una correzione).
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(
        SQLEnum(PaymentMethod, name="payment_method"),
        nullable=True,
    )

    actor_type: Mapped[AuditActorType] = mapped_column(
        SQLEnum(AuditActorType, name="audit_actor_type"),
        nullable=False,
    )
    #: UUID dell'admin, oppure `None` per attori `GUEST` e `SYSTEM`.
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    #: Motivazione. Obbligatoria a livello applicativo per rimborsi e
    #: correzioni registrati dal back-office.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    booking: Mapped["Booking"] = relationship(back_populates="payment_history", lazy="select")

    __table_args__ = (
        Index("ix_booking_payment_history_booking_created", "booking_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<BookingPaymentHistory {self.from_status} -> {self.to_status} by {self.actor_type}>"
