"""
Sweeper delle prenotazioni scadute.

Separazione dei ruoli: la **logica di dominio** — quali prenotazioni sono
scadute, come si transita a `EXPIRED`, quali righe camera vanno liberate —
vive in `BookingService.expire_pending`, che possiede già la macchina a stati e
`_sync_items_active_flag`. Qui c'è solo ciò che riguarda l'*esecuzione
periodica*: creare una sessione propria, contenere gli errori, scandire il
tempo, recapitare le notifiche.

La ragione è concreta: `_sync_items_active_flag` è l'unico punto che scrive
`is_active`, il predicato dell'exclusion constraint. Duplicarne la logica qui
metterebbe a rischio l'invariante più delicata del progetto per il gusto di
avere uno sweeper autosufficiente.

**L'invio delle email avviene dopo il commit**, mai dentro la transazione. Una
mail spedita in una transazione che poi fa rollback annuncia all'ospite una
scadenza che non è avvenuta — e quella mail non si richiama indietro.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.config.config import settings
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import SweepResultSchema
from src.service.booking_service import BookingService, ExpiredBookingNotice
from src.service.email.email_service import EmailService, get_email_service
from src.service.pricing_service import PricingService

logger = logging.getLogger(__name__)


def build_booking_service(session) -> BookingService:
    """
    Compone un `BookingService` su una sessione qualsiasi.

    Serve allo sweeper, che non passa dal sistema di dipendenze di FastAPI e
    quindi non può riusare `get_booking_service`.
    """
    return BookingService(
        session=session,
        booking_repository=BookingRepository(session),
        booking_token_repository=BookingTokenRepository(session),
        booking_status_history_repository=BookingStatusHistoryRepository(session),
        room_repository=RoomRepository(session),
        pricing_service=PricingService(),
    )


async def dispatch_expiration_notices(
        notices: Sequence[ExpiredBookingNotice],
        email_service: Optional[EmailService] = None
) -> int:
    """
    Recapita gli avvisi di scadenza.

    Da invocare **a transazione chiusa**. Le email non sollevano mai
    (`EmailService._send` cattura tutto), quindi un disservizio del canale non
    può annullare un lavoro già committato.

    :return: numero di avvisi effettivamente inviati.
    """
    service = email_service or get_email_service()

    sent = 0
    for notice in notices:
        if not notice.notify:
            continue
        await service.send_booking_expired(notice.booking)
        sent += 1

    return sent


class BookingExpirationService:
    """Esecutore periodico dello sweeper."""

    def __init__(
            self,
            session_factory: async_sessionmaker,
            email_service: Optional[EmailService] = None
    ) -> None:
        self._session_factory = session_factory
        self._email_service = email_service
        self._task: Optional[asyncio.Task] = None
        # Creato in `start()`, non qui: su Python 3.9 un `asyncio.Event`
        # costruito fuori da un event loop si lega a quello sbagliato, e il
        # servizio viene istanziato prima che il loop sia in esecuzione.
        self._stopping: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------ #
    # Una passata                                                         #
    # ------------------------------------------------------------------ #

    async def sweep_once(self) -> SweepResultSchema:
        """
        Esegue una passata completa: transizioni, commit, notifiche.

        La sessione è propria e viene chiusa a fine passata: una sessione
        tenuta aperta per l'intera vita del processo trattiene una connessione
        del pool anche mentre non fa nulla.
        """
        async with self._session_factory() as session:
            service = build_booking_service(session)
            notices: List[ExpiredBookingNotice] = await service.expire_pending()

        # Fuori dal `with`: la transazione è chiusa e il commit è avvenuto.
        notified = await dispatch_expiration_notices(notices, self._email_service)

        if notices:
            logger.info(
                "Sweeper: %d prenotazioni scadute, %d ospiti avvisati",
                len(notices),
                notified,
            )

        return SweepResultSchema(
            expired_count=len(notices),
            notified_count=notified,
            swept_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------ #
    # Ciclo di vita                                                       #
    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        """
        Ciclo principale.

        Ogni eccezione viene catturata e registrata: una passata che esplode
        non deve fermare le successive, e soprattutto non deve abbattere
        l'applicazione. Un `Task` che solleva muore in silenzio, e senza questo
        `except` lo sweeper risulterebbe attivo mentre in realtà è fermo da
        giorni — il modo peggiore di guastarsi.
        """
        logger.info(
            "Sweeper avviato: intervallo %d secondi, lotti da %d",
            settings.sweeper_interval_seconds,
            settings.sweeper_batch_size,
        )

        while not self._stopping.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Passata dello sweeper fallita; riprovo al prossimo giro")

            try:
                # `wait_for` sull'evento invece di uno `sleep` secco: allo
                # spegnimento il processo non resta appeso fino allo scadere
                # dell'intervallo.
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=settings.sweeper_interval_seconds
                )
            except asyncio.TimeoutError:
                continue

        logger.info("Sweeper fermato")

    def start(self) -> None:
        """Avvia il ciclo in background. Ripetere la chiamata non ha effetto."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="booking-expiration-sweeper")

    async def stop(self) -> None:
        """Chiede l'arresto e attende la fine della passata in corso."""
        if self._task is None:
            return

        if self._stopping is not None:
            self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
