"""
Autenticazione e sessioni: login, rinnovo con rotazione, logout, verifica.

Le sessioni sono coppie di JWT in cookie `httponly`. Un JWT resta valido fino
alla scadenza, quindi per chiudere una sessione prima serve una **lista di
revoca** (`revoked_tokens`) e, per chiuderle tutte, `User.tokens_valid_after`
(debito #30). I limiti ai tentativi di login stanno nel router (`rate_limiter`).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.config.config import settings
from src.data.enumerators import TokenRevocationReason, TokenType
from src.data.model.user import User
from src.data.repository.revoked_token_repository import RevokedTokenRepository
from src.data.repository.user_repository import UserRepository
from src.exception.custom_exception import InvalidCredentials, InvalidSession
from src.security.jwt_handler import TokenClaims, create_access_token, create_refresh_token, decode_token
from src.security.password_handler import verify_dummy_password, verify_password
from src.service.transaction import run_in_transaction

logger = logging.getLogger(__name__)

#: Un refresh token ripresentato entro questo intervallo dalla sua rotazione è
#: quasi certamente una richiesta parallela dello stesso utente (due schede
#: aperte che rinnovano insieme), non un furto: si rifiuta senza revocare tutto.
_CONCURRENT_REFRESH_GRACE = timedelta(seconds=30)


@dataclass(frozen=True)
class AuthResult:
    """Coppia di token emessa da un login o da un rinnovo."""

    user_id: UUID
    access_token: str
    refresh_token: str


class AuthService:
    """Logica di autenticazione e di sessione, con le proprie transazioni."""

    def __init__(
            self,
            session: AsyncSession,
            user_repository: UserRepository,
            revoked_token_repository: RevokedTokenRepository,
    ) -> None:
        self.session = session
        self.user_repository = user_repository
        self.revoked_token_repository = revoked_token_repository

    # ------------------------------------------------------------------ #
    # Login                                                               #
    # ------------------------------------------------------------------ #

    async def authenticate_user(self, email: str, password: str) -> AuthResult:
        """
        Verifica email e password ed emette una coppia di token.

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
        return self._issue_pair(user)

    # ------------------------------------------------------------------ #
    # Verifica di ogni richiesta autenticata                              #
    # ------------------------------------------------------------------ #

    async def resolve_current_user(self, access_token: Optional[str]) -> User:
        """
        Utente a cui appartiene l'access token, se la sessione è ancora valida.

        Oltre a firma e scadenza: il token non deve essere in lista di revoca né
        precedere una revoca globale dell'utente.

        :raises InvalidSession: in ogni caso di sessione non valida.
        """
        if not access_token:
            raise InvalidSession()
        claims = decode_token(access_token, TokenType.ACCESS)
        if await self.revoked_token_repository.is_revoked(claims.jti):
            raise InvalidSession()
        return await self._session_owner(claims)

    # ------------------------------------------------------------------ #
    # Rinnovo con rotazione                                               #
    # ------------------------------------------------------------------ #

    async def refresh(self, refresh_token: Optional[str], access_token: Optional[str]) -> AuthResult:
        """
        Emette una coppia nuova e revoca quella vecchia (**rotazione**).

        Ogni refresh token vale una volta sola. Se ne arriva uno già revocato per
        rotazione, qualcuno ne ha una copia: si revocano **tutte** le sessioni
        dell'utente, quella del ladro compresa. Fa eccezione un ripresentarsi
        entro pochi secondi, tipico di due schede che rinnovano insieme.

        :raises InvalidSession: refresh token assente, non valido, revocato o
            ripresentato.
        """
        if not refresh_token:
            raise InvalidSession()
        claims = decode_token(refresh_token, TokenType.REFRESH)
        user = await self._session_owner(claims)

        async def rotate() -> Optional[AuthResult]:
            first_use = await self.revoked_token_repository.revoke(
                claims.jti, user.id, TokenType.REFRESH, TokenRevocationReason.ROTATION, claims.expires_at
            )
            if not first_use:
                return None
            await self._revoke_access_if_valid(access_token, user.id, TokenRevocationReason.ROTATION)
            return self._issue_pair(user)

        result = await run_in_transaction(self.session, rotate)
        if result is None:
            await self._handle_reused_refresh(claims, user)
            raise InvalidSession()

        logger.info("Sessione rinnovata: utente_id=%s", user.id)
        return result

    # ------------------------------------------------------------------ #
    # Logout                                                              #
    # ------------------------------------------------------------------ #

    async def logout(self, access_token: Optional[str], refresh_token: Optional[str]) -> Optional[UUID]:
        """
        Revoca i token della sessione, se ancora validi.

        Idempotente: token assenti, scaduti o già revocati non sono un errore,
        perché l'obiettivo, una sessione chiusa, è comunque raggiunto.

        :return: utente a cui appartenevano i token, se riconoscibile (per il log).
        """
        async def revoke_both() -> Optional[UUID]:
            access_owner = await self._revoke_if_valid(access_token, TokenType.ACCESS, TokenRevocationReason.LOGOUT)
            refresh_owner = await self._revoke_if_valid(refresh_token, TokenType.REFRESH, TokenRevocationReason.LOGOUT)
            return access_owner or refresh_owner

        return await run_in_transaction(self.session, revoke_both)

    # ------------------------------------------------------------------ #
    # Manutenzione                                                        #
    # ------------------------------------------------------------------ #

    async def purge_expired_revocations(self) -> int:
        """
        Elimina dalla lista di revoca i token già scaduti: dopo la scadenza li
        rifiuta comunque la firma, e la riga non serve più.

        :return: numero di righe eliminate.
        """
        async def purge() -> int:
            return await self.revoked_token_repository.purge_expired(
                datetime.now(timezone.utc), settings.token_purge_batch_size
            )

        return await run_in_transaction(self.session, purge)

    # ------------------------------------------------------------------ #
    # Interni                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _issue_pair(user: User) -> AuthResult:
        return AuthResult(
            user_id=user.id,
            access_token=create_access_token(user),
            refresh_token=create_refresh_token(user),
        )

    async def _session_owner(self, claims: TokenClaims) -> User:
        """
        Titolare della sessione: l'utente del token, con due controlli.

        1. Esiste ancora: un utente cancellato non usa i token emessi prima.
        2. Nessuna revoca globale dopo l'emissione del token (`tokens_valid_after`).

        :raises InvalidSession: se uno dei due controlli fallisce.
        """
        user = await self.user_repository.get_by_id(claims.user_id)
        if user is None:
            raise InvalidSession()
        # `<=`: `iat` è al secondo, quindi un token emesso nello stesso secondo
        # della revoca potrebbe esserle precedente. Nel dubbio si rifiuta: chi
        # è stato buttato fuori rifà il login un istante dopo.
        if user.tokens_valid_after is not None and claims.issued_at <= user.tokens_valid_after:
            raise InvalidSession()
        return user

    async def _revoke_if_valid(
            self, token: Optional[str], token_type: TokenType, reason: TokenRevocationReason
    ) -> Optional[UUID]:
        """Revoca un token se è autentico e non scaduto; altrimenti non fa nulla."""
        if not token:
            return None
        try:
            claims = decode_token(token, token_type)
        except InvalidSession:
            return None
        await self.revoked_token_repository.revoke(
            claims.jti, claims.user_id, token_type, reason, claims.expires_at
        )
        return claims.user_id

    async def _revoke_access_if_valid(
            self, access_token: Optional[str], user_id: UUID, reason: TokenRevocationReason
    ) -> None:
        """Al rinnovo, revoca anche il vecchio access token, ma solo se è dello stesso utente."""
        if not access_token:
            return
        try:
            claims = decode_token(access_token, TokenType.ACCESS)
        except InvalidSession:
            return
        if claims.user_id == user_id:
            await self.revoked_token_repository.revoke(
                claims.jti, user_id, TokenType.ACCESS, reason, claims.expires_at
            )

    async def _handle_reused_refresh(self, claims: TokenClaims, user: User) -> None:
        """
        Un refresh token già usato è stato ripresentato.

        - revocato con il **logout**: la sessione è chiusa, lo si rifiuta e basta;
        - ruotato **da pochi secondi**: richiesta parallela dello stesso utente,
          rifiutata senza conseguenze;
        - ruotato **da più tempo**: qualcuno ne ha una copia. Revoca globale.
        """
        revocation = await self.revoked_token_repository.get(claims.jti)
        now = datetime.now(timezone.utc)
        if revocation is None or revocation.reason != TokenRevocationReason.ROTATION:
            return
        if now - revocation.revoked_at <= _CONCURRENT_REFRESH_GRACE:
            logger.info("Rinnovo concorrente respinto: utente_id=%s", user.id)
            return

        async def revoke_all() -> None:
            # Al secondo, come `iat` (vedi `_session_owner`).
            user.tokens_valid_after = now.replace(microsecond=0)

        await run_in_transaction(self.session, revoke_all)
        logger.warning(
            "Refresh token riutilizzato: revocate tutte le sessioni dell'utente %s", user.id
        )


def build_auth_service(session: AsyncSession) -> AuthService:
    """Composizione unica del service, per le dipendenze FastAPI e per lo sweeper."""
    return AuthService(session, UserRepository(session), RevokedTokenRepository(session))
