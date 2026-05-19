from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException, status
from backend.src.config.config import settings
from backend.src.data.model.user import User
from backend.src.data.enumerators import TokenType


def _create_token(user: User, expires_delta: timedelta, token_type: TokenType) -> str:
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    expiration = issued_at + expires_delta

    payload = {
        "sub": str(user.id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expiration.timestamp()),
        "role": user.role.value,
        "type": token_type.value
    }

    try:
        encoded_jwt = jwt.encode(
            payload,
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm
        )
        return encoded_jwt
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore durante la generazione del {token_type.value} token"
        )


def create_access_token(user: User) -> str:
    return _create_token(user, timedelta(minutes=settings.access_token_expire_minutes), TokenType.ACCESS)


def create_refresh_token(user: User) -> str:
    return _create_token(user, timedelta(minutes=settings.refresh_token_expire_minutes), TokenType.REFRESH)



def _decode_and_validate_token(token: str, expected_type: TokenType) -> dict:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Token di tipo {expected_type.value} non valido o scaduto",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token,settings.jwt_secret_key,algorithms=[settings.jwt_algorithm])

        token_type = payload.get("type")
        username = payload.get("sub")
        if token_type != expected_type.value or username is None:
            raise credentials_exception

        return payload

    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise credentials_exception


def decode_and_validate_access_token(token: str) -> dict:
    return _decode_and_validate_token(token, TokenType.ACCESS)


def decode_and_validate_refresh_token(token: str) -> dict:
    return _decode_and_validate_token(token, TokenType.REFRESH)