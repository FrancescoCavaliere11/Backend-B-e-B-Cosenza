"""
Preventivo firmato.

Il client **non invia mai un prezzo**. Il flusso pubblico di prenotazione è a
due passi: `POST /quote` calcola il totale e lo restituisce racchiuso in un
JWT a breve scadenza; `POST /bookings` accetta soltanto quel token.

Il token protegge da due cose:

* **manomissione del prezzo**, perché la firma lo rende immodificabile;
* **spostamento della selezione**, perché camere, date e numero di ospiti
  viaggiano dentro il token e non possono essere sostituiti dopo il calcolo.

Resta comunque una difesa in profondità: anche con un token valido, il Service
ricalcola il totale e rifiuta la prenotazione se diverge (`InvalidQuoteToken`).
La firma dice "questo prezzo l'ho emesso io", non "questo prezzo è ancora
corretto".

Il token è firmato con lo stesso segreto dell'autenticazione ma porta un
claim `type` differente: un access token non può essere speso come preventivo,
né viceversa.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Tuple
from uuid import UUID

import jwt

from src.config.config import settings
from src.data.enumerators import PaymentOption
from src.exception.custom_exception import InvalidQuoteToken

#: Discriminante del tipo di token, per impedirne l'uso incrociato con i JWT
#: di autenticazione.
QUOTE_TOKEN_TYPE = "booking_quote"


@dataclass(frozen=True)
class QuotePayload:
    """Contenuto verificato di un preventivo."""

    check_in: date
    check_out: date
    guest_count: int
    room_ids: List[UUID]
    payment_option: PaymentOption
    base_price: Decimal
    discount_amount: Decimal
    total_price: Decimal
    currency: str


def create_quote_token(payload: QuotePayload) -> Tuple[str, datetime]:
    """
    Firma un preventivo.

    :param payload: dati calcolati dal `PricingService`.
    :return: tupla `(token, istante_di_scadenza)`.
    """
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    expiration = issued_at + timedelta(minutes=settings.quote_token_expire_minutes)

    claims = {
        "type": QUOTE_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expiration.timestamp()),
        "check_in": payload.check_in.isoformat(),
        "check_out": payload.check_out.isoformat(),
        "guest_count": payload.guest_count,
        # Ordinati: due preventivi sulle stesse camere producono lo stesso
        # contenuto indipendentemente dall'ordine di selezione.
        "room_ids": sorted(str(room_id) for room_id in payload.room_ids),
        "payment_option": payload.payment_option.value,
        # Gli importi viaggiano come stringa: un float perderebbe precisione
        # e la riconversione in Decimal non sarebbe più esatta.
        "base_price": str(payload.base_price),
        "discount_amount": str(payload.discount_amount),
        "total_price": str(payload.total_price),
        "currency": payload.currency,
    }

    token = jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expiration


def decode_quote_token(token: str) -> QuotePayload:
    """
    Verifica firma e scadenza di un preventivo e ne restituisce il contenuto.

    :raises InvalidQuoteToken: token assente, scaduto, manomesso, di tipo
        errato o con un contenuto non interpretabile.
    """
    if not token:
        raise InvalidQuoteToken()

    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError) as error:
        raise InvalidQuoteToken() from error

    if claims.get("type") != QUOTE_TOKEN_TYPE:
        raise InvalidQuoteToken()

    try:
        return QuotePayload(
            check_in=date.fromisoformat(claims["check_in"]),
            check_out=date.fromisoformat(claims["check_out"]),
            guest_count=int(claims["guest_count"]),
            room_ids=[UUID(room_id) for room_id in claims["room_ids"]],
            payment_option=PaymentOption(claims["payment_option"]),
            base_price=Decimal(claims["base_price"]),
            discount_amount=Decimal(claims["discount_amount"]),
            total_price=Decimal(claims["total_price"]),
            currency=claims["currency"],
        )
    except (KeyError, ValueError, TypeError, ArithmeticError) as error:
        raise InvalidQuoteToken() from error
