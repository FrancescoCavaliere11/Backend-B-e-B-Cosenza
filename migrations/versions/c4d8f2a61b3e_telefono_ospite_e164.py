"""telefono dell'ospite in formato internazionale

Revision ID: c4d8f2a61b3e
Revises: b5e8d2c47a19
Create Date: 2026-10-10 12:00:00.000000

Dal 10/10/2026 il telefono dell'ospite (`bookings.guest_phone`) si salva
normalizzato in formato E.164 — `+393331234567` — e accetta numeri stranieri
(debito tecnico #32). Le prenotazioni già registrate hanno il vecchio formato:
dieci cifre italiane senza prefisso.

Questa migrazione le porta allo stesso formato, aggiungendo `+39`: un solo
formato a database significa un solo modo di mostrarlo, chiamarlo (`tel:`) e,
in futuro, usarlo per SMS. Tocca **solo** i numeri di esattamente dieci cifre:
qualunque altra cosa è lasciata com'è.

Solo dati, nessuna modifica di schema: la colonna è `Text`, senza limiti.
Il telefono degli **utenti registrati** (`users.phone_number`) non cambia:
si allinea con la parte customer (decisione P3).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4d8f2a61b3e'
down_revision: Union[str, Sequence[str], None] = 'b5e8d2c47a19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE bookings
        SET guest_phone = '+39' || guest_phone
        WHERE guest_phone ~ '^[0-9]{10}$'
        """
    )


def downgrade() -> None:
    """
    Riporta a dieci cifre i numeri italiani `+39` seguiti da dieci cifre.

    Non distingue quelli convertiti da questa migrazione da quelli inseriti
    dopo con il prefisso: tornando al codice precedente, che accetta solo
    dieci cifre, è comunque il formato che quel codice si aspetta. I numeri
    stranieri restano come sono.
    """
    op.execute(
        """
        UPDATE bookings
        SET guest_phone = substr(guest_phone, 4)
        WHERE guest_phone ~ '^\\+39[0-9]{10}$'
        """
    )
