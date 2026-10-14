"""
Emissione e verifica dei JWT di sessione.

Ogni token porta un identificativo casuale (`jti`): è ciò che permette di
revocarne uno prima della scadenza (debito #30), senza toccare gli altri.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt

from src.config.config import settings
from src.data.enumerators import TokenType
from src.data.model.user import User
from src.exception.custom_exception import InvalidSession

@dataclass(frozen=True)
class TokenClaims:
    """Contenuto verificato di un token: firma, scadenza e tipo sono già stati controllati."""

    user_id: UUID
    jti: UUID
    token_type: TokenType
    issued_at: datetime
    expires_at: datetime


def _create_token(user: User, expires_delta: timedelta, token_type: TokenType) -> str:
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "sub": str(user.id),
        # UUID4: 122 bit casuali, come le altre chiavi del progetto.
        "jti": str(uuid4()),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + expires_delta).timestamp()),
        "role": user.role.value,
        "type": token_type.value,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user: User) -> str:
    return _create_token(user, timedelta(minutes=settings.access_token_expire_minutes), TokenType.ACCESS)


def create_refresh_token(user: User) -> str:
    return _create_token(user, timedelta(minutes=settings.refresh_token_expire_minutes), TokenType.REFRESH)


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """
    Verifica firma, scadenza e tipo di un token.

    Non controlla la lista di revoca: quella sta nel database, e la consulta
    `AuthService`.

    :raises InvalidSession: token malformato, manomesso, scaduto, del tipo
        sbagliato o privo dei campi attesi. Un solo errore per tutti i casi: il
        dettaglio non serve al client e aiuterebbe chi prova a falsificarne uno.
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != expected_type.value:
            raise InvalidSession()
        return TokenClaims(
            user_id=UUID(payload["sub"]),
            jti=UUID(payload["jti"]),
            token_type=expected_type,
            issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        # Comprende i token emessi prima del 14/10/2026, che non hanno `jti`.
        raise InvalidSession() from None
