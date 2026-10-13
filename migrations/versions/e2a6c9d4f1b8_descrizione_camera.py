"""descrizione della camera

Revision ID: e2a6c9d4f1b8
Revises: c4d8f2a61b3e
Create Date: 2026-10-12 12:00:00.000000

Aggiunge `rooms.description`: testo libero, facoltativo, mostrato sul sito
pubblico (es. «un letto matrimoniale e due lettini»). Prima i testi delle
camere erano scritti nel frontend; ora li gestisce l'admin.

Colonna nullable e senza default: le camere esistenti restano senza
descrizione, e il sito in quel caso non mostra nulla.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2a6c9d4f1b8'
down_revision: Union[str, Sequence[str], None] = 'c4d8f2a61b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rooms', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('rooms', 'description')
