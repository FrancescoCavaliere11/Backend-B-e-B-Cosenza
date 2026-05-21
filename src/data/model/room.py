from src.config.database_config import Base
import uuid
from typing import List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Numeric, SmallInteger, Text, UUID
from decimal import Decimal
from src.data.model.room_service_association import room_service_association
from src.data.model.booking_room_association import booking_room_association
from src.security.audit_logging import Auditable


class Room(Base, Auditable):
    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10,2), nullable=False)

    room_images: Mapped[List["RoomImage"]] = relationship(back_populates="room", lazy="select",
                                                          cascade="all, delete-orphan")

    services: Mapped[List["RoomService"]] = relationship(
        secondary=room_service_association,
        back_populates="rooms",
        lazy="select"
    )

    bookings: Mapped[List["Booking"]] = relationship(
        secondary=booking_room_association,
        back_populates="rooms",
        lazy="select"
    )

