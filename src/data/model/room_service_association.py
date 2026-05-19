from sqlalchemy import Table, Column, ForeignKey
from src.config.database_config import Base

room_service_association = Table(
    'room_service_association',
    Base.metadata,
    Column('room_id', ForeignKey("rooms.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey('room-services.id', ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
)