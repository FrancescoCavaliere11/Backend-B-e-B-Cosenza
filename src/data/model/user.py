from src.config.database_config import Base
import uuid
from typing import List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.data.enumerators import UserRole
from sqlalchemy import Enum as SQLEnum, Text, UUID

from src.security.audit_logging import Auditable


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

    # MODIFICA (Booking Module - Step A):
    # Rimosso `cascade="all, delete-orphan"`. La cancellazione di un utente NON
    # deve distruggere lo storico delle prenotazioni: sono documenti con
    # rilevanza contabile e fiscale, e conservano già uno snapshot anagrafico
    # autonomo (`Booking.guest_*`).
    # La foreign key è ora `ON DELETE SET NULL`: il booking sopravvive,
    # "orfano" dell'account ma integro nei suoi dati. `passive_deletes=True`
    # delega l'operazione al database invece di farla eseguire all'ORM riga
    # per riga.
    bookings: Mapped[List["Booking"]] = relationship(
        back_populates="user",
        lazy="select",
        passive_deletes=True,
    )
