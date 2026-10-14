"""revoca dei token di sessione (logout e refresh)

Revision ID: f7b3c1d9e2a4
Revises: e2a6c9d4f1b8
Create Date: 2026-10-14 18:30:00.000000

Debito #30: logout e refresh con rotazione.

- `revoked_tokens`: identificativi (`jti`) dei token di sessione revocati
  prima della scadenza, con la scadenza naturale per poterli poi eliminare.
- `users.tokens_valid_after`: revoca globale, rifiuta ogni token emesso prima.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f7b3c1d9e2a4'
down_revision: Union[str, Sequence[str], None] = 'e2a6c9d4f1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOKEN_TYPE = sa.Enum('ACCESS', 'REFRESH', name='token_type')
_REASON = sa.Enum('LOGOUT', 'ROTATION', name='token_revocation_reason')


def upgrade() -> None:
    op.add_column('users', sa.Column('tokens_valid_after', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'revoked_tokens',
        sa.Column('jti', sa.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', sa.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', onupdate='CASCADE', ondelete='CASCADE'), nullable=False),
        sa.Column('token_type', _TOKEN_TYPE, nullable=False),
        sa.Column('reason', _REASON, nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_revoked_tokens_user_id', 'revoked_tokens', ['user_id'])
    op.create_index('ix_revoked_tokens_expires_at', 'revoked_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_revoked_tokens_expires_at', table_name='revoked_tokens')
    op.drop_index('ix_revoked_tokens_user_id', table_name='revoked_tokens')
    op.drop_table('revoked_tokens')
    _REASON.drop(op.get_bind(), checkfirst=True)
    _TOKEN_TYPE.drop(op.get_bind(), checkfirst=True)
    op.drop_column('users', 'tokens_valid_after')
