"""
Accesso ai token monouso legati a una prenotazione.

Come `BookingRepository`, questo repository **non committa**: il confine
transazionale appartiene al Service.

Nel database esiste solo l'hash SHA-256 del token; il valore in chiaro vive in
memoria il tempo di comporre l'email e non viene mai persistito né loggato. La
ricerca avviene quindi sempre per hash, mai per valore.
"""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.data.enumerators import BookingTokenPurpose
from src.data.model.booking_token import BookingToken


class BookingTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, token: BookingToken) -> BookingToken:
        """Registra il token nella sessione senza committare."""
        self.session.add(token)
        return token

    async def get_by_hash(
            self,
            token_hash: str,
            purpose: Optional[BookingTokenPurpose] = None,
            with_booking: bool = True
    ) -> Optional[BookingToken]:
        """
        Recupera un token dal suo hash.

        Restituisce il token anche se scaduto o già usato: distinguere fra
        "inesistente", "scaduto" e "già utilizzato" è una decisione di business
        e spetta al Service, che sceglierà quale eccezione sollevare e quanto
        rivelare al client.

        :param purpose: se valorizzato, il token deve avere anche questo scopo.
            Impedisce che un link di gestione venga speso come conferma.
        """
        query = select(BookingToken).where(BookingToken.token_hash == token_hash)

        if purpose is not None:
            query = query.where(BookingToken.purpose == purpose)

        if with_booking:
            query = query.options(selectinload(BookingToken.booking))

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_booking(
            self,
            booking_id: UUID,
            purpose: Optional[BookingTokenPurpose] = None
    ) -> List[BookingToken]:
        query = select(BookingToken).where(BookingToken.booking_id == booking_id)

        if purpose is not None:
            query = query.where(BookingToken.purpose == purpose)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    def mark_used(self, token: BookingToken) -> BookingToken:
        """
        Marca il token come speso.

        L'entità è già gestita dalla sessione: basta modificarla, il commit del
        Service la persiste.
        """
        token.used_at = datetime.now(timezone.utc)
        return token

    async def invalidate_all_for_booking(
            self,
            booking_id: UUID,
            purpose: Optional[BookingTokenPurpose] = None
    ) -> int:
        """
        Invalida in blocco i token ancora spendibili di una prenotazione.

        Serve quando la prenotazione raggiunge uno stato terminale: un link di
        conferma relativo a una prenotazione già scaduta o annullata non deve
        restare utilizzabile.

        :return: numero di token invalidati.
        """
        now = datetime.now(timezone.utc)

        statement = (
            update(BookingToken)
            .where(
                BookingToken.booking_id == booking_id,
                BookingToken.used_at.is_(None),
            )
        )

        if purpose is not None:
            statement = statement.where(BookingToken.purpose == purpose)

        statement = statement.values(used_at=now).execution_options(
            synchronize_session=False
        )

        result = await self.session.execute(statement)
        return result.rowcount or 0

    async def purge_expired(self, limit: int = 1000) -> int:
        """
        Elimina i token scaduti da tempo.

        Manutenzione periodica: un token scaduto non è più spendibile, ma
        lasciarlo in tabella fa crescere un indice senza alcun beneficio.

        :return: numero di token eliminati.
        """
        now = datetime.now(timezone.utc)

        expired_ids = (
            select(BookingToken.id)
            .where(BookingToken.expires_at < now)
            .limit(limit)
        )
        result = await self.session.execute(expired_ids)
        ids = list(result.scalars().all())

        if not ids:
            return 0

        await self.session.execute(
            delete(BookingToken)
            .where(BookingToken.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        return len(ids)
