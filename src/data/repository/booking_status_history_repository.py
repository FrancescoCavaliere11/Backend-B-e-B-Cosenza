"""
Accesso alla timeline delle transizioni di stato delle prenotazioni.

Tabella **append-only**: non esistono metodi di aggiornamento o cancellazione,
di proposito. Una riga di storico che può essere modificata non è più una
prova utilizzabile in caso di contestazione con l'ospite.

Come gli altri repository del modulo, non committa: il confine transazionale
appartiene al Service, così la transizione di stato e la sua registrazione
storica vivono o cadono insieme.
"""
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.enumerators import AuditActorType, BookingStatus
from src.data.model.booking_status_history import BookingStatusHistory


class BookingStatusHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(
            self,
            booking_id: UUID,
            to_status: BookingStatus,
            actor_type: AuditActorType,
            from_status: Optional[BookingStatus] = None,
            actor_id: Optional[str] = None,
            reason: Optional[str] = None
    ) -> BookingStatusHistory:
        """
        Registra una transizione di stato nella sessione.

        Accetta i valori sciolti invece dell'entità già costruita: il
        chiamante è sempre il Service, che ha in mano i dati della transizione
        e non deve occuparsi di come sono modellati.

        :param from_status: `None` sulla riga di creazione della prenotazione.
        :param actor_id: UUID di utente o admin; `None` per attori `GUEST` e
            `SYSTEM`, che non hanno un identificativo applicativo.
        """
        entry = BookingStatusHistory(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
        self.session.add(entry)
        return entry

    async def get_by_booking_id(self, booking_id: UUID) -> List[BookingStatusHistory]:
        """Timeline completa di una prenotazione, dalla più vecchia alla più recente."""
        query = (
            select(BookingStatusHistory)
            .where(BookingStatusHistory.booking_id == booking_id)
            .order_by(BookingStatusHistory.created_at)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())
