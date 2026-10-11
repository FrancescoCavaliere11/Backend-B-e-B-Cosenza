from src.config.database_config import Base
import uuid
from typing import TYPE_CHECKING, List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Text, UUID
from src.data.model.room_service_association import room_service_association
from src.security.audit_logging import Auditable

# Solo per l'analisi statica: a runtime i modelli si importano a vicenda,
# e le relazioni scritte come stringa le risolve SQLAlchemy (#17).
if TYPE_CHECKING:
    from src.data.model.room import Room


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
