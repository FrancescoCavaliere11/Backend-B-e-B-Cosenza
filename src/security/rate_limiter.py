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
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import Request

from src.config.config import settings
from src.exception.custom_exception import RateLimitExceeded

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


class InMemoryRateLimitBackend(RateLimitBackend):
    """Finestra scorrevole tenuta in memoria, protetta da lock asincrono."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        async with self._lock:
            # time.monotonic() e non time.time(): non risente di modifiche
            # all'orologio di sistema né del cambio dell'ora legale.
            now = time.monotonic()
            cutoff = now - window_seconds

            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = int(bucket[0] + window_seconds - now) + 1
                return False, max(retry_after, 1)

            bucket.append(now)

            if len(self._hits) > _PRUNE_THRESHOLD:
                self._prune(cutoff)

            return True, 0

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

booking_create_rate_limit = RateLimit(
    "booking_create", settings.rate_limit_booking_create_per_ip_hour, 3_600
)

booking_confirm_rate_limit = RateLimit(
    "booking_confirm", settings.rate_limit_confirm_per_ip_hour, 3_600
)

booking_lookup_rate_limit = RateLimit(
    "booking_lookup", settings.rate_limit_confirm_per_ip_hour, 3_600
)
