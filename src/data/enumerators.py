from enum import Enum
from typing import FrozenSet


class UserRole(str, Enum):
    """Ruoli applicativi gestiti dall'RBAC."""
    user = "user"
    admin = "admin"


class TokenType(str, Enum):
    """Tipologie di JWT emessi dal modulo di autenticazione."""
    ACCESS = "access"
    REFRESH = "refresh"


class BookingStatus(str, Enum):
    """
    Stati della macchina a stati di una prenotazione.

    NOTA: nome e valore coincidono volutamente, così il tipo ENUM creato su
    PostgreSQL contiene esattamente queste stringhe (SQLAlchemy persiste il
    *nome* del membro, non il valore).
    """
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NO_SHOW = "NO_SHOW"

    @property
    def is_pending(self) -> bool:
        """True se lo stato è temporaneo e soggetto a scadenza dell'hold."""
        return self in PENDING_BOOKING_STATUSES

    @property
    def is_occupying(self) -> bool:
        """
        True se lo stato occupa fisicamente lo slot camera.

        Attenzione: per gli stati *pending* l'occupazione è condizionata anche
        alla validità di `hold_expires_at`; questa property esprime solo la
        componente dipendente dallo stato.
        """
        return self in OCCUPYING_BOOKING_STATUSES

    @property
    def is_terminal(self) -> bool:
        """True se dallo stato non è ammessa alcuna ulteriore transizione."""
        return self in TERMINAL_BOOKING_STATUSES


#: Stati temporanei: lo slot è bloccato solo finché `hold_expires_at` è futuro.
PENDING_BOOKING_STATUSES: FrozenSet[BookingStatus] = frozenset({
    BookingStatus.PENDING_CONFIRMATION,
    BookingStatus.PENDING_PAYMENT,
})

#: Stati che rendono `BookingRoomItem.is_active = True` (slot occupato).
OCCUPYING_BOOKING_STATUSES: FrozenSet[BookingStatus] = frozenset({
    BookingStatus.PENDING_CONFIRMATION,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.CONFIRMED,
    BookingStatus.CHECKED_IN,
    BookingStatus.COMPLETED,
    BookingStatus.NO_SHOW,
})

#: Stati terminali: nessuna transizione in uscita ammessa.
TERMINAL_BOOKING_STATUSES: FrozenSet[BookingStatus] = frozenset({
    BookingStatus.COMPLETED,
    BookingStatus.CANCELLED,
    BookingStatus.EXPIRED,
    BookingStatus.NO_SHOW,
})


class PaymentOption(str, Enum):
    """Strategia di pagamento scelta dall'ospite in fase di prenotazione."""
    PAY_NOW = "PAY_NOW"
    PAY_ON_ARRIVAL = "PAY_ON_ARRIVAL"


class PaymentStatus(str, Enum):
    """Stato dell'incasso associato alla prenotazione."""
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"


class PaymentMethod(str, Enum):
    """
    Strumento di pagamento effettivo.

    `STRIPE_CARD` è l'unico canale online: il PAN non transita mai dal backend
    (cfr. strategia PCI-DSS del modulo). Gli altri sono metodi registrati
    manualmente dall'admin al momento dell'incasso in struttura.
    """
    STRIPE_CARD = "STRIPE_CARD"
    CASH_ON_SITE = "CASH_ON_SITE"
    POS_ON_SITE = "POS_ON_SITE"
    BANK_TRANSFER = "BANK_TRANSFER"


class BookingChannel(str, Enum):
    """Canale di origine della prenotazione (reportistica commerciale)."""
    PUBLIC_GUEST = "PUBLIC_GUEST"
    PUBLIC_USER = "PUBLIC_USER"
    ADMIN_BACKOFFICE = "ADMIN_BACKOFFICE"


class BookingTokenPurpose(str, Enum):
    """Scopo di un token monouso legato a una prenotazione."""
    CONFIRM_EMAIL = "CONFIRM_EMAIL"
    MANAGE = "MANAGE"
    CANCEL = "CANCEL"


class AuditActorType(str, Enum):
    """Natura dell'attore che ha determinato una transizione di stato."""
    GUEST = "GUEST"
    USER = "USER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"
