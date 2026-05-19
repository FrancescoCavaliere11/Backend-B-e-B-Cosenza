from backend.src.config.database_config import Base
import uuid
from typing import List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Text, UUID
from backend.src.data.model.room_service_association import room_service_association
from backend.src.security.audit_logging import Auditable


class RoomService(Base, Auditable):
    __tablename__ = 'room-services'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    rooms: Mapped[List["Room"]] = relationship(
        secondary=room_service_association,
        back_populates="services",
        lazy="select"
    )
