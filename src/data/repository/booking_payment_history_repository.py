"""
Accesso alla timeline dei cambi di stato del pagamento.

Come lo storico degli stati (`BookingStatusHistoryRepository`) è
**append-only** e non committa: la riga di storico vive o cade nella stessa
transazione del cambiamento che descrive. La lettura passa dalla relazione
`Booking.payment_history`, caricata con il resto della scheda.
"""
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.data.enumerators import AuditActorType, PaymentMethod, PaymentStatus
from src.data.model.booking_payment_history import BookingPaymentHistory


class BookingPaymentHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(
            self,
            booking_id: UUID,
            to_status: PaymentStatus,
            actor_type: AuditActorType,
            from_status: Optional[PaymentStatus] = None,
            payment_method: Optional[PaymentMethod] = None,
            actor_id: Optional[str] = None,
            reason: Optional[str] = None
    ) -> BookingPaymentHistory:
        """
        Registra un cambio di stato del pagamento nella sessione.

        :param from_status: `None` quando la prenotazione nasce già pagata.
        :param payment_method: metodo **dopo** il cambiamento.
        :param actor_id: UUID dell'admin; `None` per `GUEST` e `SYSTEM`.
        """
        entry = BookingPaymentHistory(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            payment_method=payment_method,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
        self.session.add(entry)
        return entry
