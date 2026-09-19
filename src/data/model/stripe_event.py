import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, UUID, func
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database_config import Base


class StripeEvent(Base):
    """
    Registro degli eventi webhook ricevuti da Stripe, ai soli fini di
    **idempotenza**.

    Stripe garantisce la consegna *at-least-once*: lo stesso evento può essere
    recapitato più volte. Senza questo registro, un `payment_intent.succeeded`
    duplicato potrebbe generare una doppia conferma o un doppio accredito.
    Il vincolo di unicità su `event_id` rende l'elaborazione idempotente.

    Sicurezza / PCI-DSS: il payload grezzo **non** viene persistito. Si salva
    solo un digest SHA-256 utile a diagnosticare eventuali divergenze fra
    consegne ripetute dello stesso evento. Nessun dato di carta e nessun dato
    personale finisce in questa tabella né nei log applicativi.

    Non eredita da `Auditable`: è una tabella tecnica di infrastruttura, non
    un'entità di dominio; non ha un autore applicativo né viene mai modificata
    dopo l'elaborazione.
    """

    __tablename__ = "stripe_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    #: Identificativo dell'evento assegnato da Stripe (es. "evt_1A2b3C...").
    event_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: `None` finché l'evento non è stato elaborato con successo.
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: SHA-256 del payload ricevuto. Mai il payload in chiaro.
    payload_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    #: Messaggio di errore in caso di elaborazione fallita (già sanificato).
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<StripeEvent {self.event_type} id={self.event_id} processed={self.processed_at is not None}>"
