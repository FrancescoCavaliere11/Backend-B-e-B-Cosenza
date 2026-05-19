from src.config.database_config import Base
import uuid
from typing import List
from datetime import datetime
from sqlalchemy import UUID, Numeric, Integer, DateTime, func, ForeignKey
from decimal import Decimal
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.data.model.booking_room_association import booking_room_association
from src.security.audit_logging import Auditable


class Booking(Base, Auditable):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    total_price: Mapped[Decimal] = mapped_column(Numeric(10,2), nullable=False)
    guest_count: Mapped[int] = mapped_column(Integer, nullable=False)
    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    check_out: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", onupdate="CASCADE", ondelete="CASCADE"))
    user: Mapped["User"] = relationship(back_populates="bookings", lazy="select")

    rooms: Mapped[List["Room"]] = relationship(
        secondary=booking_room_association,
        back_populates="bookings",
        lazy="select"
    )

