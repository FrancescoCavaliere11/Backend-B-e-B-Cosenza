import uuid

from sqlalchemy import UUID, TEXT
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database_config import Base
from src.security.audit_logging import Auditable


class ExtraService(Base, Auditable):
    __tablename__ = 'extra_service'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)

    img_url: Mapped[str] = mapped_column(TEXT, nullable=False, unique=False) # todo mettere unique a True

    description: Mapped[str] = mapped_column(TEXT, nullable=True)

