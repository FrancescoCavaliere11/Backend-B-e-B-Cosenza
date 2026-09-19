from src.config.database_config import Base
import uuid
from typing import List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Numeric, SmallInteger, Text, UUID, Boolean
from decimal import Decimal
from src.data.model.room_service_association import room_service_association
from src.security.audit_logging import Auditable

# Registra la tabella `booking_room_items` in Base.metadata: `Room.bookings` la
# referenzia per nome come `secondary`. `booking_room_item.py` non importa
# `room.py`, quindi non si crea un ciclo.
from src.data.model.booking_room_item import BookingRoomItem  # noqa: F401


class Room(Base, Auditable):
    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    number: Mapped[int] = mapped_column(SmallInteger, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)

    img_url: Mapped[str] = mapped_column(Text, nullable=False, unique=False)  # todo mettere unique a true

    services: Mapped[List["RoomService"]] = relationship(
        secondary=room_service_association,
        back_populates="rooms",
        lazy="select"
    )

    # MODIFICA (Booking Module - Step A):
    # La vecchia relazione many-to-many via `booking_room_association` è stata
    # sostituita dall'association object `BookingRoomItem`, che porta con sé il
    # prezzo congelato e le date su cui poggia l'exclusion constraint anti
    # double-booking.
    #
    # `booking_items` è la relazione canonica (scrittura); `bookings` resta
    # disponibile in sola lettura per comodità di navigazione.
    #
    # ATTENZIONE: la FK di `BookingRoomItem.room_id` è `ON DELETE RESTRICT`.
    # Una camera con prenotazioni associate non è più cancellabile
    # fisicamente: l'eliminazione va gestita come disattivazione logica
    # (`enabled = False`). Il `RoomService.delete_room` andrà adeguato per
    # tradurre l'IntegrityError in un errore di business leggibile.
    booking_items: Mapped[List["BookingRoomItem"]] = relationship(
        back_populates="room",
        lazy="select",
        passive_deletes=True,
    )

    bookings: Mapped[List["Booking"]] = relationship(
        secondary="booking_room_items",
        back_populates="rooms",
        viewonly=True,
        lazy="select",
    )
