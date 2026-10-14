"""Autenticazione: verifica delle credenziali ed emissione dei token di sessione."""
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from src.data.model.user import User
from src.data.repository.user_repository import UserRepository
from src.exception.custom_exception import InvalidCredentials
from src.security.jwt_handler import create_access_token, create_refresh_token
from src.security.password_handler import verify_dummy_password, verify_password


@dataclass(frozen=True)
class AuthResult:
    """Esito di un login riuscito: i due token di sessione e l'utente a cui appartengono."""

    user_id: UUID
    access_token: str
    refresh_token: str


class AuthService:
    """Logica di autenticazione. I limiti ai tentativi stanno nel router (`rate_limiter`)."""

    def __init__(self, user_repository: UserRepository) -> None:
        self.user_repository = user_repository

    async def authenticate_user(self, email: str, password: str) -> AuthResult:
        """
        Verifica email e password ed emette i token.

        Con un'email sconosciuta si verifica comunque una password finta: il
        tempo di risposta resta lo stesso e non rivela quali email esistono.

        :raises InvalidCredentials: email inesistente o password errata, con lo
            stesso messaggio nei due casi.
        """
        user: Optional[User] = await self.user_repository.get_by_email(email)
        if user is None:
            verify_dummy_password(password)
            raise InvalidCredentials()
        if not verify_password(password, user.password):
            raise InvalidCredentials()

        return AuthResult(
            user_id=user.id,
            access_token=create_access_token(user),
            refresh_token=create_refresh_token(user),
        )
