"""no-show libera le camere

Revision ID: a7c3e91f5d20
Revises: d3f1a07c9b45
Create Date: 2026-10-07 09:00:00.000000

Dal 07/10/2026 `NO_SHOW` non è più uno stato occupante
(`OCCUPYING_BOOKING_STATUSES`): una prenotazione segnata come "non
presentato" libera le sue camere. Le nuove transizioni lo fanno da sé
(`_sync_items_active_flag`); questa migrazione allinea le prenotazioni
segnate **prima** del cambiamento, le cui righe camera sono ancora attive.

Senza, il calendario e la ricerca le vedrebbero libere (le letture filtrano
per stato) mentre l'EXCLUDE constraint, che guarda solo `is_active`,
rifiuterebbe una nuova prenotazione su quelle notti: un 409 inspiegabile.

Solo dati, nessuna modifica di schema.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91f5d20'
down_revision: Union[str, Sequence[str], None] = 'd3f1a07c9b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE booking_room_items AS item
        SET is_active = false
        FROM bookings AS booking
        WHERE item.booking_id = booking.id
          AND booking.status = 'NO_SHOW'
          AND item.is_active
        """
    )


def downgrade() -> None:
    """
    Nessun ripristino, di proposito: nel frattempo le notti liberate possono
    essere state rivendute, e riattivare quelle righe violerebbe l'EXCLUDE
    constraint. Con il codice precedente le righe resterebbero inattive, che
    per uno stato terminale non ha effetti oltre a liberare lo slot.
    """
