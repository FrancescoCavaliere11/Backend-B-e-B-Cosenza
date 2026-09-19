"""booking module - step A (macchina a stati, guest booking, anti double-booking)

Revision ID: d3f1a07c9b45
Revises: b36303dbba32
Create Date: 2026-09-19 10:00:00.000000

Questa migrazione è stata scritta a mano (non autogenerata) perché contiene
operazioni che Alembic non è in grado di dedurre dai modelli:

* creazione delle estensioni PostgreSQL `btree_gist` e `pgcrypto`;
* conversione di `check_in` / `check_out` da `timestamptz` a `date`;
* migrazione dei dati da `booking_room_association` a `booking_room_items`;
* creazione dell'EXCLUDE constraint GiST anti double-booking.

⚠️ NOTE OPERATIVE PRIMA DI ESEGUIRE

1. Verificare che `b36303dbba32` sia davvero la revisione corrente:
       alembic heads
   Se il progetto ha revisioni più recenti, aggiornare `down_revision`.

2. L'utente PostgreSQL deve poter creare estensioni (`CREATE EXTENSION`).
   In caso contrario farle creare una volta da un superuser:
       CREATE EXTENSION IF NOT EXISTS btree_gist;
       CREATE EXTENSION IF NOT EXISTS pgcrypto;

3. Le prenotazioni preesistenti erano nate con `check_in`/`check_out` a
   `now()`, quindi con soggiorni di durata zero. La migrazione le sana
   portando il check-out a un giorno dopo il check-in, altrimenti i nuovi
   CHECK constraint non sarebbero soddisfacibili.

4. Se nei dati legacy esistono due prenotazioni sovrapposte sulla stessa
   camera, la creazione dell'EXCLUDE constraint fallirà. È il comportamento
   desiderato: significa che il database conteneva già un overbooking, da
   risolvere manualmente prima di procedere.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd3f1a07c9b45'
down_revision: Union[str, Sequence[str], None] = 'b36303dbba32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- Tipi ENUM -------------------------------------------------------------
# `create_type=False` impedisce ad Alembic di ricreare il tipo a ogni
# riferimento: i tipi vengono creati una sola volta, esplicitamente.

booking_status = postgresql.ENUM(
    "PENDING_CONFIRMATION",
    "PENDING_PAYMENT",
    "CONFIRMED",
    "CHECKED_IN",
    "COMPLETED",
    "CANCELLED",
    "EXPIRED",
    "NO_SHOW",
    name="booking_status",
    create_type=False,
)

booking_channel = postgresql.ENUM(
    "PUBLIC_GUEST",
    "PUBLIC_USER",
    "ADMIN_BACKOFFICE",
    name="booking_channel",
    create_type=False,
)

payment_option = postgresql.ENUM(
    "PAY_NOW",
    "PAY_ON_ARRIVAL",
    name="payment_option",
    create_type=False,
)

payment_status = postgresql.ENUM(
    "NOT_REQUIRED",
    "PENDING",
    "AUTHORIZED",
    "PAID",
    "FAILED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
    name="payment_status",
    create_type=False,
)

payment_method = postgresql.ENUM(
    "STRIPE_CARD",
    "CASH_ON_SITE",
    "POS_ON_SITE",
    "BANK_TRANSFER",
    name="payment_method",
    create_type=False,
)

booking_token_purpose = postgresql.ENUM(
    "CONFIRM_EMAIL",
    "MANAGE",
    "CANCEL",
    name="booking_token_purpose",
    create_type=False,
)

audit_actor_type = postgresql.ENUM(
    "GUEST",
    "USER",
    "ADMIN",
    "SYSTEM",
    name="audit_actor_type",
    create_type=False,
)

_ALL_ENUMS = (
    booking_status,
    booking_channel,
    payment_option,
    payment_status,
    payment_method,
    booking_token_purpose,
    audit_actor_type,
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # ------------------------------------------------------------------ #
    # 1. Estensioni PostgreSQL                                            #
    # ------------------------------------------------------------------ #
    # btree_gist: indispensabile per combinare `room_id WITH =` e
    # `daterange WITH &&` nello stesso EXCLUDE constraint.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    # pgcrypto: gen_random_uuid() per la migrazione dati (nativa da PG 13).
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------ #
    # 2. Tipi ENUM                                                        #
    # ------------------------------------------------------------------ #
    for enum_type in _ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # ------------------------------------------------------------------ #
    # 3. Bonifica dei dati legacy                                         #
    # ------------------------------------------------------------------ #
    # Le prenotazioni preesistenti hanno check_in == check_out (entrambi
    # server_default now()): non soddisferebbero `check_out > check_in`.
    op.execute(
        """
        UPDATE bookings
        SET check_out = check_in + INTERVAL '1 day'
        WHERE check_out <= check_in
        """
    )
    op.execute("UPDATE bookings SET guest_count = 1 WHERE guest_count IS NULL OR guest_count < 1")

    # ------------------------------------------------------------------ #
    # 4. Conversione delle date del soggiorno a tipo DATE                 #
    # ------------------------------------------------------------------ #
    # Una data di arrivo non può essere implicita: si rimuove il vecchio
    # `server_default = now()` prima di convertire il tipo.
    op.alter_column(
        "bookings",
        "check_in",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "bookings",
        "check_out",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "bookings",
        "check_in",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        type_=sa.Date(),
        existing_nullable=False,
        postgresql_using="check_in::date",
    )
    op.alter_column(
        "bookings",
        "check_out",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        type_=sa.Date(),
        existing_nullable=False,
        postgresql_using="check_out::date",
    )

    # ------------------------------------------------------------------ #
    # 5. Nuove colonne su `bookings`                                      #
    # ------------------------------------------------------------------ #
    # Aggiunte come nullable, popolate sui dati legacy, quindi rese NOT NULL:
    # evita di introdurre default permanenti su campi che devono essere
    # sempre valorizzati esplicitamente dal Service.
    op.add_column("bookings", sa.Column("code", sa.String(length=20), nullable=True))
    op.add_column("bookings", sa.Column("status", booking_status, nullable=True))
    op.add_column("bookings", sa.Column("source_channel", booking_channel, nullable=True))
    op.add_column("bookings", sa.Column("guest_firstname", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("guest_lastname", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("guest_email", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("guest_phone", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("base_price", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column("bookings", sa.Column("payment_option", payment_option, nullable=True))
    op.add_column("bookings", sa.Column("payment_status", payment_status, nullable=True))
    op.add_column("bookings", sa.Column("payment_method", payment_method, nullable=True))

    op.add_column(
        "bookings",
        sa.Column(
            "discount_amount",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
    )
    op.add_column(
        "bookings",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.add_column("bookings", sa.Column("stripe_payment_intent_id", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("card_brand", sa.String(length=20), nullable=True))
    op.add_column("bookings", sa.Column("card_last4", sa.String(length=4), nullable=True))

    op.add_column("bookings", sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("cancellation_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("admin_notes", sa.Text(), nullable=True))

    # ------------------------------------------------------------------ #
    # 6. Backfill delle nuove colonne sui record esistenti                #
    # ------------------------------------------------------------------ #
    # Il codice legacy è derivato dall'UUID: garantito univoco e dentro i 20
    # caratteri (7 + 10 = 17).
    op.execute(
        """
        UPDATE bookings
        SET code = 'BB-LEG-' || upper(substr(replace(id::text, '-', ''), 1, 10))
        WHERE code IS NULL
        """
    )
    op.execute("UPDATE bookings SET status = 'CONFIRMED' WHERE status IS NULL")
    op.execute("UPDATE bookings SET source_channel = 'ADMIN_BACKOFFICE' WHERE source_channel IS NULL")
    op.execute("UPDATE bookings SET payment_option = 'PAY_ON_ARRIVAL' WHERE payment_option IS NULL")
    op.execute("UPDATE bookings SET payment_status = 'PENDING' WHERE payment_status IS NULL")
    op.execute("UPDATE bookings SET base_price = total_price WHERE base_price IS NULL")
    op.execute(
        """
        UPDATE bookings AS b
        SET guest_firstname = u.firstname,
            guest_lastname  = u.lastname,
            guest_email     = u.email,
            guest_phone     = u.phone_number
        FROM users AS u
        WHERE u.id = b.user_id
          AND b.guest_email IS NULL
        """
    )
    # Rete di sicurezza per eventuali booking senza utente collegato.
    op.execute(
        """
        UPDATE bookings
        SET guest_firstname = COALESCE(guest_firstname, 'N/D'),
            guest_lastname  = COALESCE(guest_lastname,  'N/D'),
            guest_email     = COALESCE(guest_email,     'sconosciuto@example.invalid'),
            guest_phone     = COALESCE(guest_phone,     '0000000000')
        """
    )

    # ------------------------------------------------------------------ #
    # 7. Vincoli su `bookings`                                            #
    # ------------------------------------------------------------------ #
    for column in (
        "code",
        "status",
        "source_channel",
        "guest_firstname",
        "guest_lastname",
        "guest_email",
        "guest_phone",
        "base_price",
        "payment_option",
        "payment_status",
    ):
        op.alter_column("bookings", column, nullable=False)

    # `user_id` diventa opzionale (guest booking) e la FK passa a SET NULL:
    # la cancellazione di un utente non deve più distruggere lo storico.
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_user_id_fkey")
    op.alter_column("bookings", "user_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(
        "fk_bookings_user_id",
        "bookings",
        "users",
        ["user_id"],
        ["id"],
        onupdate="CASCADE",
        ondelete="SET NULL",
    )

    # `code` è dichiarato nel modello con unique=True + index=True: SQLAlchemy
    # lo rende un UNIQUE INDEX (non un UniqueConstraint separato). La
    # migrazione rispecchia esattamente quella forma per evitare drift in
    # `alembic revision --autogenerate`.
    op.create_unique_constraint(
        "uq_bookings_stripe_payment_intent_id", "bookings", ["stripe_payment_intent_id"]
    )

    # Bersaglio della foreign key composita di `booking_room_items`: rende
    # impossibile a una riga camera avere date diverse dal soggiorno.
    # Deve esistere PRIMA della creazione di `booking_room_items`.
    op.create_unique_constraint(
        "uq_bookings_id_dates", "bookings", ["id", "check_in", "check_out"]
    )

    op.create_check_constraint("ck_bookings_date_range", "bookings", "check_out > check_in")
    op.create_check_constraint("ck_bookings_guest_count", "bookings", "guest_count >= 1")
    op.create_check_constraint("ck_bookings_total_price", "bookings", "total_price >= 0")
    op.create_check_constraint("ck_bookings_base_price", "bookings", "base_price >= 0")
    op.create_check_constraint("ck_bookings_discount_amount", "bookings", "discount_amount >= 0")

    op.create_index("ix_bookings_code", "bookings", ["code"], unique=True)
    op.create_index("ix_bookings_status", "bookings", ["status"])
    op.create_index("ix_bookings_guest_email", "bookings", ["guest_email"])
    op.create_index("ix_bookings_user_id", "bookings", ["user_id"])
    op.create_index("ix_bookings_hold_expires_at", "bookings", ["hold_expires_at"])
    op.create_index("ix_bookings_status_hold", "bookings", ["status", "hold_expires_at"])
    op.create_index("ix_bookings_stay_dates", "bookings", ["check_in", "check_out"])

    # ------------------------------------------------------------------ #
    # 8. Nuove tabelle                                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "booking_room_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("room_id", sa.UUID(), nullable=False),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("check_out", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("nights", sa.Integer(), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False, server_default="System"),
        sa.Column("last_updated_by", sa.String(length=36), nullable=False, server_default="System"),
        # Foreign key COMPOSITA: le date della riga devono coincidere con
        # quelle del soggiorno. ON UPDATE CASCADE riallinea automaticamente le
        # righe figlie quando le date della prenotazione cambiano.
        sa.ForeignKeyConstraint(
            ["booking_id", "check_in", "check_out"],
            ["bookings.id", "bookings.check_in", "bookings.check_out"],
            name="fk_booking_room_items_booking_dates",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], onupdate="CASCADE", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", "room_id", name="uq_booking_room_items_booking_room"),
        sa.CheckConstraint("check_out > check_in", name="ck_booking_room_items_date_range"),
        sa.CheckConstraint("nights >= 1", name="ck_booking_room_items_nights"),
        sa.CheckConstraint("unit_price >= 0", name="ck_booking_room_items_unit_price"),
    )
    op.create_index("ix_booking_room_items_booking_id", "booking_room_items", ["booking_id"])
    op.create_index("ix_booking_room_items_room_id", "booking_room_items", ["room_id"])
    op.create_index(
        "ix_booking_room_items_room_dates",
        "booking_room_items",
        ["room_id", "check_in", "check_out"],
    )
    op.create_index(
        "ix_booking_room_items_active",
        "booking_room_items",
        ["room_id"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "booking_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", booking_token_purpose, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False, server_default="System"),
        sa.Column("last_updated_by", sa.String(length=36), nullable=False, server_default="System"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], onupdate="CASCADE", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_tokens_booking_id", "booking_tokens", ["booking_id"])
    op.create_index("ix_booking_tokens_token_hash", "booking_tokens", ["token_hash"], unique=True)
    op.create_index("ix_booking_tokens_booking_purpose", "booking_tokens", ["booking_id", "purpose"])
    op.create_index("ix_booking_tokens_expires_at", "booking_tokens", ["expires_at"])

    op.create_table(
        "booking_status_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("from_status", booking_status, nullable=True),
        sa.Column("to_status", booking_status, nullable=False),
        sa.Column("actor_type", audit_actor_type, nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], onupdate="CASCADE", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_status_history_booking_id", "booking_status_history", ["booking_id"])
    op.create_index(
        "ix_booking_status_history_booking_created",
        "booking_status_history",
        ["booking_id", "created_at"],
    )

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_digest", sa.String(length=64), nullable=True),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stripe_events_event_id", "stripe_events", ["event_id"], unique=True)
    op.create_index("ix_stripe_events_event_type", "stripe_events", ["event_type"])

    # ------------------------------------------------------------------ #
    # 9. Migrazione dati: booking_room_association -> booking_room_items  #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        INSERT INTO booking_room_items (
            id, booking_id, room_id, check_in, check_out, is_active,
            unit_price, nights, line_total,
            created_at, updated_at, created_by, last_updated_by
        )
        SELECT
            gen_random_uuid(),
            a.booking_id,
            a.room_id,
            b.check_in,
            b.check_out,
            b.status IN ('PENDING_CONFIRMATION', 'PENDING_PAYMENT', 'CONFIRMED',
                         'CHECKED_IN', 'COMPLETED', 'NO_SHOW'),
            r.price,
            GREATEST(b.check_out - b.check_in, 1),
            r.price * GREATEST(b.check_out - b.check_in, 1),
            now(), now(), 'System', 'System'
        FROM booking_room_association AS a
        JOIN bookings AS b ON b.id = a.booking_id
        JOIN rooms    AS r ON r.id = a.room_id
        """
    )

    op.execute("DROP TABLE IF EXISTS booking_room_association")

    # ------------------------------------------------------------------ #
    # 10. EXCLUDE constraint anti double-booking                          #
    # ------------------------------------------------------------------ #
    # Da qui in poi nessuna race condition applicativa può generare un
    # overbooking: è PostgreSQL a rifiutare la scrittura sovrapposta.
    # La semantica '[)' consente le prenotazioni back-to-back.
    op.execute(
        """
        ALTER TABLE booking_room_items
        ADD CONSTRAINT ex_booking_room_items_no_overlap
        EXCLUDE USING gist (
            room_id WITH =,
            daterange(check_in, check_out, '[)') WITH &&
        ) WHERE (is_active)
        """
    )

    # ------------------------------------------------------------------ #
    # 11. Riga di storico per le prenotazioni preesistenti                #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        INSERT INTO booking_status_history (
            id, booking_id, from_status, to_status, actor_type, actor_id, reason, created_at
        )
        SELECT
            gen_random_uuid(), b.id, NULL, b.status, 'SYSTEM', NULL,
            'Stato iniziale assegnato dalla migrazione del modulo Booking',
            COALESCE(b.created_at, now())
        FROM bookings AS b
        """
    )


def downgrade() -> None:
    """
    Downgrade schema.

    ⚠️ PERDITA DI DATI INEVITABILE: lo schema precedente non è in grado di
    rappresentare le prenotazioni guest (richiede `user_id NOT NULL`). Le
    prenotazioni senza account collegato vengono quindi eliminate. Tutti i dati
    di stato, pagamento, token e storico vengono persi.
    """
    bind = op.get_bind()

    # Ricostruzione della vecchia tabella ponte.
    op.create_table(
        "booking_room_association",
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("room_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], onupdate="CASCADE", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], onupdate="CASCADE", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("booking_id", "room_id"),
    )
    op.execute(
        """
        INSERT INTO booking_room_association (booking_id, room_id)
        SELECT DISTINCT booking_id, room_id FROM booking_room_items
        """
    )

    op.execute("ALTER TABLE booking_room_items DROP CONSTRAINT IF EXISTS ex_booking_room_items_no_overlap")
    op.drop_table("stripe_events")
    op.drop_table("booking_status_history")
    op.drop_table("booking_tokens")
    op.drop_table("booking_room_items")

    # Le prenotazioni guest non sono rappresentabili nello schema precedente.
    op.execute("DELETE FROM bookings WHERE user_id IS NULL")

    for index_name in (
        "ix_bookings_stay_dates",
        "ix_bookings_status_hold",
        "ix_bookings_hold_expires_at",
        "ix_bookings_user_id",
        "ix_bookings_guest_email",
        "ix_bookings_status",
        "ix_bookings_code",
    ):
        op.drop_index(index_name, table_name="bookings")

    for constraint_name in (
        "ck_bookings_discount_amount",
        "ck_bookings_base_price",
        "ck_bookings_total_price",
        "ck_bookings_guest_count",
        "ck_bookings_date_range",
    ):
        op.drop_constraint(constraint_name, "bookings", type_="check")

    op.drop_constraint("uq_bookings_stripe_payment_intent_id", "bookings", type_="unique")
    op.drop_constraint("uq_bookings_id_dates", "bookings", type_="unique")

    op.drop_constraint("fk_bookings_user_id", "bookings", type_="foreignkey")
    op.alter_column("bookings", "user_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "bookings_user_id_fkey",
        "bookings",
        "users",
        ["user_id"],
        ["id"],
        onupdate="CASCADE",
        ondelete="CASCADE",
    )

    for column in (
        "admin_notes",
        "cancellation_reason",
        "cancellation_deadline",
        "cancelled_at",
        "confirmed_at",
        "hold_expires_at",
        "card_last4",
        "card_brand",
        "stripe_payment_intent_id",
        "version",
        "currency",
        "discount_amount",
        "payment_method",
        "payment_status",
        "payment_option",
        "base_price",
        "guest_phone",
        "guest_email",
        "guest_lastname",
        "guest_firstname",
        "source_channel",
        "status",
        "code",
    ):
        op.drop_column("bookings", column)

    op.alter_column(
        "bookings",
        "check_in",
        existing_type=sa.Date(),
        type_=postgresql.TIMESTAMP(timezone=True),
        existing_nullable=False,
        postgresql_using="check_in::timestamptz",
        server_default=sa.text("now()"),
    )
    op.alter_column(
        "bookings",
        "check_out",
        existing_type=sa.Date(),
        type_=postgresql.TIMESTAMP(timezone=True),
        existing_nullable=False,
        postgresql_using="check_out::timestamptz",
        server_default=sa.text("now()"),
    )

    for enum_type in reversed(_ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)

    # Le estensioni non vengono rimosse: potrebbero essere usate da altro.
