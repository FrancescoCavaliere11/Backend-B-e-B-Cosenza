from typing import List
import magic

from fastapi import UploadFile, HTTPException, status

from backend.src.config.config import settings


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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo di file non consentito. Rilevato: {mime}. Sono ammessi solo JPEG, PNG e WEBP."
        )

async def _validate_file_size(file: UploadFile):
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)

    if file_size > settings.max_file_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Il file è troppo grande. Il limite massimo è di 5MB."
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

def validate_room_images(room_images: List["RoomImageCreateSchema"]) -> None:
    if not room_images or len(room_images) == 0:
        raise ValueError("La stanza deve avere almeno un'immagine.")


    primary_count = sum(1 for img in room_images if img.is_primary)

    if primary_count == 0:
        raise ValueError("Devi impostare esattamente un'immagine come principale (is_primary=True).")

    if primary_count > 1:
        raise ValueError("Non puoi avere più di un'immagine principale per stanza.")


# Room Service validators
def validate_room_services_ids(value: List):
    if len(value) != len(set(value)):
        raise ValueError("La lista dei servizi contiene ID duplicati.")

    return value

def validate_room_services_name(value: str):
    return _validate_no_padding_space(value, "nome stanza")