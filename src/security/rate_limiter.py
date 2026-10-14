"""
Rate limiting degli endpoint pubblici.

Implementazione a **finestra scorrevole**: per ogni chiave si conservano gli
istanti delle richieste recenti e si scartano quelle uscite dalla finestra. A
differenza del conteggio a finestra fissa, non consente il raddoppio del limite
a cavallo di due intervalli (5 richieste alle 10:59:59 più altre 5 alle
11:00:01).

⚠️ **Lo storage predefinito è in memoria, quindi per processo.** Con
`uvicorn --workers 4` ogni worker ha i propri contatori e il limite effettivo
diventa il quadruplo di quello configurato. In sviluppo e su un singolo worker
il conteggio è esatto. Il giorno in cui servirà scalare orizzontalmente, basterà
implementare `RateLimitBackend` su Redis o PostgreSQL: i router non cambiano.
"""
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import Request

from src.config.config import settings
from src.exception.custom_exception import RateLimitExceeded
from src.security.masking import mask_email

logger = logging.getLogger(__name__)

#: Oltre questo numero di chiavi attive si ripulisce quelle ormai vuote, per
#: evitare che la mappa cresca indefinitamente sotto attacco distribuito.
_PRUNE_THRESHOLD = 10_000


class RateLimitBackend(ABC):
    """Contatore delle richieste. Sostituibile senza toccare i router."""

    @abstractmethod
    async def hit(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """
        Registra una richiesta e dice se è ammessa.

        :return: tupla `(ammessa, secondi_di_attesa)`. Il secondo valore è
            significativo solo quando la richiesta è rifiutata.
        """

    @abstractmethod
    async def check(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """Come `hit`, ma **senza registrare** nulla: dice solo se il limite è già pieno."""

    @abstractmethod
    async def record(self, key: str, window_seconds: int) -> None:
        """Registra un evento senza verificare il limite (es. un login fallito)."""

    @abstractmethod
    async def clear(self, key: str) -> None:
        """Dimentica gli eventi di una chiave (es. dopo un login riuscito)."""


class InMemoryRateLimitBackend(RateLimitBackend):
    """Finestra scorrevole tenuta in memoria, protetta da lock asincrono."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        async with self._lock:
            now, bucket = self._current(key, window_seconds)
            verdict = self._verdict(bucket, limit, window_seconds, now)
            if verdict[0]:
                self._append(key, bucket, now, window_seconds)
            return verdict

    async def check(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        async with self._lock:
            now, bucket = self._current(key, window_seconds)
            return self._verdict(bucket, limit, window_seconds, now)

    async def record(self, key: str, window_seconds: int) -> None:
        async with self._lock:
            now, bucket = self._current(key, window_seconds)
            self._append(key, bucket, now, window_seconds)

    async def clear(self, key: str) -> None:
        async with self._lock:
            self._hits.pop(key, None)

    def _current(self, key: str, window_seconds: int) -> Tuple[float, Deque[float]]:
        """Istante attuale e richieste della chiave ancora dentro la finestra."""
        # time.monotonic() e non time.time(): non risente di modifiche
        # all'orologio di sistema né del cambio dell'ora legale.
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        return now, bucket

    @staticmethod
    def _verdict(bucket: Deque[float], limit: int, window_seconds: int, now: float) -> Tuple[bool, int]:
        if len(bucket) >= limit:
            retry_after = int(bucket[0] + window_seconds - now) + 1
            return False, max(retry_after, 1)
        return True, 0

    def _append(self, key: str, bucket: Deque[float], now: float, window_seconds: int) -> None:
        bucket.append(now)
        if len(self._hits) > _PRUNE_THRESHOLD:
            self._prune(now - window_seconds)

    def _prune(self, cutoff: float) -> None:
        """Elimina le chiavi senza richieste recenti."""
        stale = [
            key for key, bucket in self._hits.items()
            if not bucket or bucket[-1] <= cutoff
        ]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        """Azzera tutti i contatori. Usato dai test fra un caso e l'altro."""
        self._hits.clear()


_backend: RateLimitBackend = InMemoryRateLimitBackend()


def get_rate_limit_backend() -> RateLimitBackend:
    return _backend


def reset_rate_limiter() -> None:
    """Azzera i contatori, se il backend lo supporta. Riservato ai test."""
    if isinstance(_backend, InMemoryRateLimitBackend):
        _backend.reset()


def client_ip(request: Request) -> str:
    """
    Indirizzo IP da usare come chiave di conteggio.

    `X-Forwarded-For` lo scrive il client: fidarsene senza condizioni
    significherebbe consentire a chiunque di cambiare IP a ogni richiesta e
    azzerare il rate limiting. Per questo viene considerato solo se
    `settings.trusted_proxy_count` è maggiore di zero, e si prende l'N-esimo
    valore **da destra** — quello scritto dal proxy più interno di cui ci si
    fida, l'unico che un client esterno non può falsificare.
    """
    if settings.trusted_proxy_count > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            chain = [part.strip() for part in forwarded.split(",") if part.strip()]
            index = len(chain) - settings.trusted_proxy_count
            if 0 <= index < len(chain):
                return chain[index]

    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(name: str, key: str, limit: int, window_seconds: int) -> None:
    """
    Applica un limite a una chiave arbitraria.

    :raises RateLimitExceeded: limite superato; l'eccezione porta i secondi di
        attesa, che l'handler globale traduce nell'header `Retry-After`.
    """
    if not settings.rate_limit_enabled:
        return

    allowed, retry_after = await _backend.hit(f"{name}:{key}", limit, window_seconds)
    if not allowed:
        raise RateLimitExceeded(retry_after=retry_after)


class RateLimit:
    """
    Dipendenza FastAPI che applica un limite per indirizzo IP.

    Si dichiara sull'endpoint, così la logica di conteggio non entra nel corpo
    della rotta:

        @router.post("/", dependencies=[Depends(booking_create_rate_limit)])
    """

    def __init__(self, name: str, limit: int, window_seconds: int) -> None:
        self.name = name
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        await enforce_rate_limit(
            self.name, client_ip(request), self.limit, self.window_seconds
        )


async def enforce_email_rate_limit(email: str) -> None:
    """
    Limite giornaliero per indirizzo email.

    Complementare al limite per IP: un attaccante con molti indirizzi IP resta
    comunque vincolato sul numero di prenotazioni riconducibili alla stessa
    email, e viceversa.
    """
    await enforce_rate_limit(
        "booking_create_email",
        email.lower(),
        settings.rate_limit_booking_create_per_email_day,
        86_400,
    )


# --- Limiti preconfigurati, uno per endpoint pubblico --------------------- #

availability_rate_limit = RateLimit(
    "availability", settings.rate_limit_availability_per_ip_minute, 60
)

quote_rate_limit = RateLimit(
    "quote", settings.rate_limit_availability_per_ip_minute, 60
)

occupancy_rate_limit = RateLimit(
    "occupancy", settings.rate_limit_availability_per_ip_minute, 60
)

booking_create_rate_limit = RateLimit(
    "booking_create", settings.rate_limit_booking_create_per_ip_hour, 3_600
)

booking_confirm_rate_limit = RateLimit(
    "booking_confirm", settings.rate_limit_confirm_per_ip_hour, 3_600
)

booking_lookup_rate_limit = RateLimit(
    "booking_lookup", settings.rate_limit_confirm_per_ip_hour, 3_600
)


# --- Login (debito #19) ---------------------------------------------------- #
#
# Tre limiti, con ruoli diversi:
#
# - **per IP**, su tutti i tentativi: una dipendenza, come gli altri endpoint;
# - **per account + IP**, sui soli fallimenti: il più stretto. Ferma chi prova
#   le password da un computer, senza toccare l'utente vero, che entra dal suo;
# - **per account**, sui soli fallimenti da qualunque IP: ferma chi distribuisce
#   i tentativi su molti indirizzi (*credential stuffing*).
#
# Si contano solo i fallimenti perché un limite sull'account che contasse anche
# i successi bloccherebbe chi la password ce l'ha. Il blocco dura al massimo la
# finestra (15 minuti): nessun blocco permanente, che permetterebbe a chiunque
# di chiudere fuori un utente sbagliando apposta la sua password.
#
# Le chiavi si calcolano dall'email **inviata**, che l'account esista o no: la
# risposta a un blocco è identica nei due casi, e il limite non diventa un modo
# per scoprire quali email sono registrate.

#: Messaggio del blocco, uguale per account esistenti e inesistenti.
LOGIN_BLOCKED_MESSAGE = "Troppi tentativi di accesso: riprova più tardi"

login_rate_limit = RateLimit(
    "login", settings.rate_limit_login_per_ip, settings.rate_limit_login_window_seconds
)

#: Il rinnovo della sessione ha lo stesso tetto per IP del login: un sito sano
#: rinnova una volta ogni 15 minuti.
refresh_rate_limit = RateLimit(
    "auth_refresh", settings.rate_limit_login_per_ip, settings.rate_limit_login_window_seconds
)


def _login_failure_keys(email: str, ip: str) -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """Chiavi e limiti dei due contatori di fallimenti: `(account, IP)` e `account`."""
    account = email.strip().lower()
    return (
        (f"login_fail_account_ip:{account}|{ip}", settings.rate_limit_login_failures_per_account_ip),
        (f"login_fail_account:{account}", settings.rate_limit_login_failures_per_account),
    )


async def enforce_login_attempt_limits(email: str, ip: str) -> None:
    """
    Rifiuta il tentativo se l'account ha già troppi fallimenti recenti.

    Si chiama **prima** di verificare la password: un tentativo bloccato non
    consuma la CPU dell'hashing, e un blocco vale anche per la password giusta.

    :raises RateLimitExceeded: con i secondi di attesa del limite più lontano.
    """
    if not settings.rate_limit_enabled:
        return

    window = settings.rate_limit_login_window_seconds
    waits = []
    for key, limit in _login_failure_keys(email, ip):
        allowed, retry_after = await _backend.check(key, limit, window)
        if not allowed:
            waits.append(retry_after)

    if waits:
        logger.warning("Login bloccato: utente=%s ip=%s attesa=%ss", mask_email(email), ip, max(waits))
        raise RateLimitExceeded(LOGIN_BLOCKED_MESSAGE, retry_after=max(waits))


async def record_login_failure(email: str, ip: str) -> None:
    """Conta un login fallito su entrambi i contatori dell'account."""
    if not settings.rate_limit_enabled:
        return
    for key, _ in _login_failure_keys(email, ip):
        await _backend.record(key, settings.rate_limit_login_window_seconds)


async def clear_login_failures(email: str, ip: str) -> None:
    """
    Dopo un login riuscito azzera il contatore `(account, IP)`.

    Quello per solo account resta: i fallimenti arrivati da *altri* IP non sono
    dell'utente che è appena entrato, e potrebbero essere un attacco in corso.
    """
    pair_key, _ = _login_failure_keys(email, ip)[0]
    await _backend.clear(pair_key)
