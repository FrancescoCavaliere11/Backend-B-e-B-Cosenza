from backend.src.config.database_config import Base
import uuid
from typing import List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.src.data.enumerators import UserRole
from sqlalchemy import Enum as SQLEnum, Text, UUID

from backend.src.security.audit_logging import Auditable


class User(Base, Auditable):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    firstname: Mapped[str] = mapped_column(Text, nullable=False)
    lastname: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole), nullable=False)

    bookings: Mapped[List["Booking"]] = relationship(back_populates="user", lazy="select", cascade="all, delete-orphan")

