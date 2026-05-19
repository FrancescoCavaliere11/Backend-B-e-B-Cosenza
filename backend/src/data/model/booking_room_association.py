from sqlalchemy import Table, Column, ForeignKey
from backend.src.config.database_config import Base

booking_room_association = Table(
    "booking_room_association",
    Base.metadata,
    Column("booking_id", ForeignKey("bookings.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True),
    Column("room_id", ForeignKey("rooms.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True),
)