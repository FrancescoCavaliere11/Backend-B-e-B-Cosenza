"""storico dei pagamenti

Revision ID: b5e8d2c47a19
Revises: a7c3e91f5d20
Create Date: 2026-10-09 10:00:00.000000

Nuova tabella `booking_payment_history`: la timeline dei cambi di stato del
pagamento (incassi, rimborsi, correzioni, esiti di Stripe), sorella di
`booking_status_history`. Vedi il modello `BookingPaymentHistory`.

I tipi ENUM `payment_status`, `payment_method` e `audit_actor_type` esistono
già (migrazione `d3f1a07c9b45`): qui si riusano con `create_type=False`,
senza ricrearli.

**Nessun riempimento a ritroso**: le prenotazioni pagate prima di questa
migrazione non hanno voci. Ricostruirle inventerebbe autore e data di un
incasso che il sistema non ha mai registrato, e uno storico con voci
inventate non vale più come prova.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b5e8d2c47a19'
down_revision: Union[str, Sequence[str], None] = 'a7c3e91f5d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


payment_status = postgresql.ENUM(name="payment_status", create_type=False)
payment_method = postgresql.ENUM(name="payment_method", create_type=False)
audit_actor_type = postgresql.ENUM(name="audit_actor_type", create_type=False)


def upgrade() -> None:
    op.create_table(
        "booking_payment_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("from_status", payment_status, nullable=True),
        sa.Column("to_status", payment_status, nullable=False),
        sa.Column("payment_method", payment_method, nullable=True),
        sa.Column("actor_type", audit_actor_type, nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], onupdate="CASCADE", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_payment_history_booking_id", "booking_payment_history", ["booking_id"])
    op.create_index(
        "ix_booking_payment_history_booking_created",
        "booking_payment_history",
        ["booking_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_booking_payment_history_booking_created", table_name="booking_payment_history")
    op.drop_index("ix_booking_payment_history_booking_id", table_name="booking_payment_history")
    op.drop_table("booking_payment_history")
