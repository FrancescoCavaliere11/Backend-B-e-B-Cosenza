from datetime import date, datetime, timedelta
from typing import List, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import magic

from fastapi import UploadFile, HTTPException, status

from src.config.config import settings
from src.exception.custom_exception import InvalidFileType


# Image validators
async def validate_image(file: UploadFile):
    await _validate_file_size(file)
    await _validate_image_type(file)


async def _validate_image_type(file: UploadFile):
    header = await file.read(2048)
    await file.seek(0)

    mime = magic.from_buffer(header, mime=True)
    allowed_types = ["image/jpeg", "image/png", "image/webp"]

    if mime not in allowed_types:
        raise InvalidFileType(
            message=f"Tipo di file non consentito. Rilevato: {mime}. Sono ammessi solo JPEG, PNG e WEBP."
        )


async def _validate_file_size(file: UploadFile):
    file_size = file.size

    if file_size is None:
        raise InvalidFileType(
            message="Impossibile determinare la dimensione del file"
        )

    if file_size > settings.max_file_size:
        raise InvalidFileType(
            message="Il file è troppo grande. Il limite massimo è di 5MB."
        )


# General validators
def _validate_no_padding_space(value: str, field: str):
    if value != value.strip():
        raise ValueError(f"Non sono consentiti spazi iniziali o finali per il campo {field}")
    return value


# User validators
def validate_user_firstname(value: str):
    return _validate_no_padding_space(value, "nome")

def validate_user_lastname(value: str):
    return _validate_no_padding_space(value, "cognome")


def validate_phone_number(value: str):
    if not value.isdigit():
        raise ValueError("Il numero di telefono deve contenere solo numeri")
    return value


def validate_password_strength(value: str):
    if not any(char.isupper() for char in value):
        raise ValueError("La password deve contenere almeno una maiuscola")
    if not any(char.islower() for char in value):
        raise ValueError("La password deve contenere almeno una minuscola")
    if not any(char.isdigit() for char in value):
        raise ValueError("La password deve contenere almeno un numero")

    special_chars = "!@#$%^&*(),.?\":{}|<>"
    if not any(char in special_chars for char in value):
        raise ValueError("La password deve contenere almeno un carattere speciale")

    return value


# Room validators
def validate_room_name(value: str):
    return _validate_no_padding_space(value, "nome stanza")


# Room Service validators
def validate_room_services_ids(value: List):
    if len(value) != len(set(value)):
        raise ValueError("La lista dei servizi contiene ID duplicati.")

    return value

def validate_room_services_name(value: str):
    return _validate_no_padding_space(value, "nome stanza")


# Extra Service validators
def validate_extra_service_name(value: str):
    return _validate_no_padding_space(value, "nome servizio")

def validate_extra_service_description(value: Optional[str]):
    if value is not None:
        return _validate_no_padding_space(value, "descrizione servizio")
    return None


# ---------------------------------------------------------------------------
# Booking validators
# ---------------------------------------------------------------------------

def today_in_app_timezone() -> date:
    """
    Data odierna nel fuso orario della struttura (`settings.app_timezone`).

    Non si usa `date.today()`: il server potrebbe girare in UTC e, nelle ore
    serali italiane, considerare "ieri" un check-in che per la struttura è
    ancora oggi.

    :return: data corrente nel fuso della struttura.
    """
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def validate_booking_date_range(
        check_in: date,
        check_out: date,
        allow_past: bool = False
) -> None:
    """
    Valida l'intervallo di soggiorno rispetto alle regole di struttura.

    :param check_in: data di arrivo.
    :param check_out: data di partenza (esclusa dal soggiorno).
    :param allow_past: se True consente un check-in nel passato. Usato solo dai
        contratti amministrativi, per registrare a posteriori soggiorni già
        avvenuti o correggere errori di back-office.
    :raises ValueError: se una delle regole non è rispettata.
    """
    if check_out <= check_in:
        raise ValueError("La data di partenza deve essere successiva a quella di arrivo")

    nights = (check_out - check_in).days

    if nights < settings.booking_min_nights:
        raise ValueError(
            f"Il soggiorno minimo è di {settings.booking_min_nights} "
            f"{'notte' if settings.booking_min_nights == 1 else 'notti'}"
        )

    if nights > settings.booking_max_nights:
        raise ValueError(f"Il soggiorno non può superare {settings.booking_max_nights} notti")

    today = today_in_app_timezone()

    if not allow_past and check_in < today:
        raise ValueError("La data di arrivo non può essere nel passato")

    max_check_in = today + timedelta(days=settings.booking_max_advance_days)
    if check_in > max_check_in:
        raise ValueError(
            f"Non è possibile prenotare con più di {settings.booking_max_advance_days} giorni di anticipo"
        )


def validate_room_ids_list(value: List[UUID]) -> List[UUID]:
    """
    Valida la lista delle camere richieste.

    :raises ValueError: lista vuota, con duplicati o oltre il massimo consentito.
    """
    if not value:
        raise ValueError("Devi selezionare almeno una camera")

    if len(value) != len(set(value)):
        raise ValueError("La lista delle camere contiene ID duplicati")

    if len(value) > settings.booking_max_rooms_per_booking:
        raise ValueError(
            f"Non è possibile prenotare più di {settings.booking_max_rooms_per_booking} camere "
            f"in un'unica prenotazione"
        )

    return value


def validate_guest_firstname(value: str) -> str:
    return _validate_no_padding_space(value, "nome dell'ospite")


def validate_guest_lastname(value: str) -> str:
    return _validate_no_padding_space(value, "cognome dell'ospite")


def validate_booking_code(value: str) -> str:
    """
    Normalizza il codice prenotazione inserito dall'ospite.

    Gli ospiti lo ricopiano dall'email con spazi accidentali o in minuscolo:
    si normalizza invece di rifiutare.
    """
    normalized = value.strip().upper()

    if not normalized:
        raise ValueError("Il codice prenotazione è obbligatorio")

    return normalized


def validate_honeypot(value: Optional[str]) -> Optional[str]:
    """
    Campo trappola per i bot: invisibile nel form, un utente reale non lo
    compila mai. Se arriva valorizzato, la richiesta è automatizzata.
    """
    if value:
        raise ValueError("Richiesta non valida")
    return value


def validate_terms_accepted(value: bool) -> bool:
    if value is not True:
        raise ValueError("È necessario accettare le condizioni di prenotazione")
    return value
