import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UUID
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.database_config import Base
from src.data.enumerators import BookingTokenPurpose
from src.security.audit_logging import Auditable


class BookingToken(Base, Auditable):
    """
    Token monouso associato a una prenotazione (conferma email, gestione,
    cancellazione).

    Sicurezza:

    * Nel database viene salvato **esclusivamente l'hash SHA-256** del token
      (64 caratteri esadecimali), mai il valore in chiaro. Vale lo stesso
      principio applicato alle password: una compromissione del DB non deve
      consentire di confermare o cancellare prenotazioni altrui.
    * Il token è **monouso**: `used_at` viene valorizzato al primo utilizzo e
      i tentativi successivi vengono respinti.
    * Il valore in chiaro esiste solo in memoria il tempo di comporre l'email
      e non viene mai loggato.
    """

    __tablename__ = "booking_tokens"

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

    #: SHA-256 esadecimale del token in chiaro.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    purpose: Mapped[BookingTokenPurpose] = mapped_column(
        SQLEnum(BookingTokenPurpose, name="booking_token_purpose"),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    booking: Mapped["Booking"] = relationship(back_populates="tokens", lazy="select")

    __table_args__ = (
        Index("ix_booking_tokens_booking_purpose", "booking_id", "purpose"),
        Index("ix_booking_tokens_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<BookingToken purpose={self.purpose} used={self.used_at is not None}>"
