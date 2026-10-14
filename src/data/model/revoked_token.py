import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UUID, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database_config import Base
from src.data.enumerators import TokenRevocationReason, TokenType


class RevokedToken(Base):
    """
    Lista di revoca dei token di sessione (debito #30).

    Un JWT è valido finché firma e scadenza lo dicono: per poterne rifiutare
    uno prima della scadenza serve ricordarsi del suo identificativo (`jti`).
    Il token in sé non viene salvato.

    Una riga serve solo fino a `expires_at`: dopo, il token è rifiutato comunque
    dalla firma. Lo sweeper elimina le righe scadute.
    """

    __tablename__ = "revoked_tokens"

    #: Identificativo casuale del JWT (UUID4), nel payload come `jti`.
    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    token_type: Mapped[TokenType] = mapped_column(
        SQLEnum(TokenType, name="token_type"), nullable=False
    )

    reason: Mapped[TokenRevocationReason] = mapped_column(
        SQLEnum(TokenRevocationReason, name="token_revocation_reason"), nullable=False
    )

    #: Scadenza naturale del token: dopo questo istante la riga si può eliminare.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_revoked_tokens_expires_at", "expires_at"),)
