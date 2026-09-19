import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    UUID,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.database_config import Base
from src.security.audit_logging import Auditable


class BookingRoomItem(Base, Auditable):
    """
    Riga di prenotazione: una camera per un intervallo di date.

    Sostituisce la precedente tabella ponte `booking_room_association` per due
    ragioni architetturali:

    1. **Snapshot del prezzo.** `unit_price` congela il prezzo della camera al
       momento della prenotazione: una successiva modifica di `Room.price` non
       deve alterare il valore economico delle prenotazioni già emesse.

    2. **Anti double-booking garantito dal database.** L'exclusion constraint
       GiST definita in `__table_args__` richiede che `room_id` e l'intervallo
       di date convivano nella stessa riga. Nessuna race condition applicativa
       può produrre un overbooking: è PostgreSQL a rifiutare la seconda
       scrittura sovrapposta.

    Le date sono **denormalizzate** dal `Booking` padre: un soggiorno ha un solo
    intervallo, valido per tutte le camere prenotate. La copia sulla riga esiste
    unicamente perché l'exclusion constraint la richiede.

    L'allineamento non è affidato alla disciplina applicativa ma è **imposto dal
    database** tramite la foreign key composita
    `(booking_id, check_in, check_out) -> bookings (id, check_in, check_out)`:

    * una riga con date diverse da quelle del padre viene rifiutata;
    * modificando le date della prenotazione, `ON UPDATE CASCADE` riallinea
      automaticamente tutte le righe figlie, mantenendo veritiero l'exclusion
      constraint senza alcun intervento del Service.

    Conseguenza voluta: le date **non** sono per camera. Un ospite che vuole due
    camere su intervalli diversi effettua due prenotazioni distinte.

    `is_active` rappresenta l'occupazione effettiva dello slot: è `True` solo
    se lo stato del booking è occupante **e** l'eventuale hold non è scaduto.
    È il predicato dell'exclusion constraint, quindi la sua correttezza è
    responsabilità esclusiva del `BookingService`.
    """

    __tablename__ = "booking_room_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Nessuna ForeignKey semplice qui: `booking_id` fa parte della foreign key
    # composita dichiarata in `__table_args__` insieme alle due date. Una
    # seconda FK verso la stessa tabella renderebbe ambigua la risoluzione
    # della relationship.
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # RESTRICT (non CASCADE): una camera con storico prenotazioni non può
    # essere cancellata fisicamente. La disattivazione avviene tramite
    # `Room.enabled = False`.
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rooms.id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    nights: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    booking: Mapped["Booking"] = relationship(back_populates="items", lazy="select")
    room: Mapped["Room"] = relationship(back_populates="booking_items", lazy="select")

    __table_args__ = (
        # Invariante "un solo soggiorno per prenotazione", garantita dal DB.
        # Referenzia il vincolo UNIQUE (id, check_in, check_out) su `bookings`.
        ForeignKeyConstraint(
            ["booking_id", "check_in", "check_out"],
            ["bookings.id", "bookings.check_in", "bookings.check_out"],
            name="fk_booking_room_items_booking_dates",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        CheckConstraint("check_out > check_in", name="ck_booking_room_items_date_range"),
        CheckConstraint("nights >= 1", name="ck_booking_room_items_nights"),
        CheckConstraint("unit_price >= 0", name="ck_booking_room_items_unit_price"),
        UniqueConstraint("booking_id", "room_id", name="uq_booking_room_items_booking_room"),
        Index("ix_booking_room_items_room_dates", "room_id", "check_in", "check_out"),
        Index(
            "ix_booking_room_items_active",
            "room_id",
            postgresql_where=text("is_active"),
        ),
        # Garanzia autorevole contro il double booking.
        # Richiede l'estensione `btree_gist` (creata dalla migrazione).
        # La semantica '[)' rende legittime le prenotazioni back-to-back:
        # il check-out del giorno X non collide con il check-in dello stesso X.
        ExcludeConstraint(
            (literal_column("room_id"), "="),
            (literal_column("daterange(check_in, check_out, '[)')"), "&&"),
            name="ex_booking_room_items_no_overlap",
            using="gist",
            where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<BookingRoomItem room={self.room_id} {self.check_in}->{self.check_out} "
            f"active={self.is_active}>"
        )
