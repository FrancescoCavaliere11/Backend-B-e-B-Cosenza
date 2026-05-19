from src.config.database_config import Base
import uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Text, Boolean, ForeignKey, UUID

from src.security.audit_logging import Auditable


class RoomImage(Base, Auditable):
    __tablename__ = "room_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE", onupdate="CASCADE"))
    room: Mapped["Room"] = relationship(back_populates="room_images", lazy="select",)

