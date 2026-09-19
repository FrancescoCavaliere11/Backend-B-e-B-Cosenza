import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UUID, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.database_config import Base
from src.data.enumerators import AuditActorType, BookingStatus


class BookingStatusHistory(Base):
    """
    Timeline immutabile delle transizioni di stato di una prenotazione.

    Serve a rispondere in modo incontrovertibile a domande come "chi ha
    cancellato questa prenotazione e quando", indispensabile in caso di
    contestazione con l'ospite.

    Scelta deliberata: questa entità **non** eredita da `Auditable`. È essa
    stessa una tabella di audit, append-only, e i campi del mixin
    (`updated_at`, `last_updated_by`) non avrebbero significato: una riga di
    storico non viene mai modificata. L'autore dell'azione è tracciato in modo
    più espressivo dalla coppia `actor_type` / `actor_id`.
    """

    __tablename__ = "booking_status_history"

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

    #: `None` sulla riga di creazione della prenotazione.
    from_status: Mapped[Optional[BookingStatus]] = mapped_column(
        SQLEnum(BookingStatus, name="booking_status"),
        nullable=True,
    )
    to_status: Mapped[BookingStatus] = mapped_column(
        SQLEnum(BookingStatus, name="booking_status"),
        nullable=False,
    )

    actor_type: Mapped[AuditActorType] = mapped_column(
        SQLEnum(AuditActorType, name="audit_actor_type"),
        nullable=False,
    )
    #: UUID dell'utente/admin, oppure `None` per attori GUEST e SYSTEM.
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    #: Motivazione testuale. Obbligatoria a livello applicativo per le
    #: cancellazioni disposte dall'admin.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    booking: Mapped["Booking"] = relationship(back_populates="status_history", lazy="select")

    __table_args__ = (
        Index("ix_booking_status_history_booking_created", "booking_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<BookingStatusHistory {self.from_status} -> {self.to_status} by {self.actor_type}>"
