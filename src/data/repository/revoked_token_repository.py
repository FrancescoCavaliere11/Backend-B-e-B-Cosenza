from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.enumerators import TokenRevocationReason, TokenType
from src.data.model.revoked_token import RevokedToken


class RevokedTokenRepository:
    """Accesso alla lista di revoca dei token di sessione. Non fa commit: decide il service."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def revoke(
            self,
            jti: UUID,
            user_id: UUID,
            token_type: TokenType,
            reason: TokenRevocationReason,
            expires_at: datetime,
    ) -> bool:
        """
        Aggiunge un token alla lista di revoca.

        `ON CONFLICT DO NOTHING` sulla chiave primaria: due richieste che
        revocano lo stesso token insieme non producono un errore, e il valore
        restituito dice quale delle due è arrivata prima.

        :return: `True` se il token è stato revocato ora, `False` se lo era già.
        """
        statement = (
            insert(RevokedToken)
            .values(jti=jti, user_id=user_id, token_type=token_type, reason=reason, expires_at=expires_at)
            .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
        )
        result = await self.session.execute(statement)
        return result.rowcount == 1

    async def is_revoked(self, jti: UUID) -> bool:
        result = await self.session.execute(select(exists().where(RevokedToken.jti == jti)))
        return bool(result.scalar())

    async def get(self, jti: UUID) -> Optional[RevokedToken]:
        """La revoca di un token, o `None` se il token non è in lista."""
        return await self.session.get(RevokedToken, jti)

    async def purge_expired(self, now: datetime, limit: int) -> int:
        """
        Elimina le righe dei token già scaduti, al massimo `limit` per chiamata.

        :return: numero di righe eliminate.
        """
        expired = select(RevokedToken.jti).where(RevokedToken.expires_at < now).limit(limit)
        jtis = list((await self.session.execute(expired)).scalars().all())
        if not jtis:
            return 0

        await self.session.execute(
            delete(RevokedToken)
            .where(RevokedToken.jti.in_(jtis))
            .execution_options(synchronize_session=False)
        )
        return len(jtis)
