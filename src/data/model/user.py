from src.config.database_config import Base
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.data.enumerators import UserRole
from sqlalchemy import DateTime, Enum as SQLEnum, Text, UUID

from src.security.audit_logging import Auditable

# Solo per l'analisi statica: a runtime i modelli si importano a vicenda,
# e le relazioni scritte come stringa le risolve SQLAlchemy (#17).
if TYPE_CHECKING:
    from src.data.model.booking import Booking


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

    #: Revoca globale delle sessioni (debito #30): ogni token emesso **prima**
    #: di questo istante è rifiutato, senza doverli elencare uno per uno. Si
    #: imposta quando un refresh token già usato viene ripresentato, cioè
    #: quando qualcuno lo ha copiato. `None`: nessuna revoca.
    tokens_valid_after: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
