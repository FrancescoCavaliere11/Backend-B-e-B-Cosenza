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

**La pulizia dei token viaggia sullo stesso ciclo, ma con la sua cadenza.**
Eliminare i token scaduti è manutenzione: nulla dipende dal fatto che avvenga
entro un minuto, e farla a ogni passata sarebbe una DELETE ogni cinque minuti
per liberare quasi sempre zero righe. Riusa comunque questo ciclo invece di un
secondo task: un `while` in più da avviare, fermare e sorvegliare, per una
query al giorno, non si ripaga.

**Le autorizzazioni si annullano prima di liberare lo slot** (Step G). Una
prenotazione in attesa di pagamento può avere un'autorizzazione viva su
Stripe: rivendere quella camera senza prima rilasciarla significherebbe poter
incassare per una stanza che non abbiamo più. L'ordine è quindi rigido —
prima si rende impossibile l'incasso, poi si libera — e quando il rilascio non
riesce la prenotazione **non viene fatta scadere affatto**. Uno slot invenduto
costa una notte; un incasso senza camera costa molto di più.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.config.config import settings
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import SweepResultSchema
from src.exception.custom_exception import (
    PaymentGatewayError,
    PaymentIntentNotCancellable,
)
from src.service.booking_service import BookingService, ExpiredBookingNotice
from src.service.email.email_service import EmailService, get_email_service
from src.service.payment.gateway import StripeGateway
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
            email_service: Optional[EmailService] = None,
            gateway: Optional[StripeGateway] = None
    ) -> None:
        self._session_factory = session_factory
        self._email_service = email_service
        self._gateway = gateway
        self._task: Optional[asyncio.Task] = None
        # Istante dell'ultima pulizia dei token. `None` significa "mai in
        # questo processo": la prima passata dopo l'avvio la esegue, così un
        # riavvio quotidiano non la rimanda all'infinito.
        self._last_token_purge: Optional[datetime] = None
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
        # Prima di tutto: rilascio delle autorizzazioni ancora vive. Chi non
        # si riesce a rilasciare resta fuori dalla passata.
        esclusi, confermate = await self._release_authorizations()

        async with self._session_factory() as session:
            service = build_booking_service(session)
            notices: List[ExpiredBookingNotice] = await service.expire_pending(
                exclude_ids=esclusi
            )

        # Fuori dal `with`: la transazione è chiusa e il commit è avvenuto.
        notified = await dispatch_expiration_notices(notices, self._email_service)

        for risultato in confermate:
            await (self._email_service or get_email_service()).send_booking_confirmed(
                risultato.booking, risultato.manage_token
            )

        if esclusi:
            logger.warning(
                "Sweeper: %d prenotazioni non fatte scadere, autorizzazione ancora viva",
                len(esclusi),
            )

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
    # Pulizia dei token                                                   #
    # ------------------------------------------------------------------ #

    async def purge_tokens_if_due(self, force: bool = False) -> int:
        """
        Esegue la pulizia dei token scaduti se è passato abbastanza tempo.

        Il tempo si misura da quando la pulizia è stata *tentata*, e il
        segnaposto viene spostato prima di eseguirla: se la DELETE fallisce
        non si riprova cinque minuti dopo, e poi di nuovo, e di nuovo. È
        manutenzione — può aspettare il giorno seguente.

        Un fallimento non si propaga: interrompere la passata dello sweeper
        perché non si è riusciti a cancellare righe scadute significherebbe
        far dipendere la liberazione degli slot dalla pulizia di una tabella.

        :param force: esegue comunque, ignorando la cadenza. Serve ai test e a
            una pulizia manuale.
        :return: numero di token eliminati, `0` anche quando non era il turno.
        """
        now = datetime.now(timezone.utc)

        if not force and self._last_token_purge is not None:
            atteso = timedelta(hours=settings.token_purge_interval_hours)
            if now - self._last_token_purge < atteso:
                return 0

        self._last_token_purge = now

        try:
            async with self._session_factory() as session:
                service = build_booking_service(session)
                eliminati = await service.purge_expired_tokens()
        except Exception:
            logger.exception("Pulizia dei token fallita; riprovo al prossimo turno")
            return 0

        if eliminati:
            logger.info("Pulizia token: %d righe scadute eliminate", eliminati)

        return eliminati

    # ------------------------------------------------------------------ #
    # Autorizzazioni                                                      #
    # ------------------------------------------------------------------ #

    async def _release_authorizations(self):
        """
        Rilascia le autorizzazioni delle prenotazioni scadute, **prima** che i
        loro slot tornino in vendita.

        Tre esiti per ciascuna:

        - **rilasciata** → lo slot può essere liberato normalmente;
        - **Stripe risponde che è già incassata** → l'ospite ha completato il
          pagamento nell'istante esatto in cui stavamo annullando. Non si
          libera nulla: si conferma la prenotazione, che è ciò che ha comprato;
        - **gestore irraggiungibile** → non si libera nulla e si riprova al
          giro dopo. È la scelta prudente: uno slot invenduto per un'ora costa
          una notte, un incasso senza camera costa molto di più.

        Senza gateway configurato nessuna prenotazione con autorizzazione viene
        toccata. Normalmente non ce ne sono — senza Stripe non esiste il flusso
        di pagamento online — ma il residuo di una configurazione precedente
        non deve poter produrre un incasso orfano.

        :return: `(identificativi da escludere, conferme da notificare)`.
        """
        esclusi: List = []
        confermate: List = []

        async with self._session_factory() as session:
            service = build_booking_service(session)
            riferimenti = await service.list_pending_payment_with_intent()

        if not riferimenti:
            return esclusi, confermate

        if self._gateway is None:
            logger.warning(
                "%d autorizzazioni da rilasciare ma nessun gateway configurato",
                len(riferimenti),
            )
            return [ref.booking_id for ref in riferimenti], confermate

        for ref in riferimenti:
            try:
                await self._gateway.cancel_payment_intent(ref.payment_intent_id)

            except PaymentIntentNotCancellable:
                logger.info(
                    "Pagamento completato durante il rilascio: confermo %s", ref.code
                )
                esclusi.append(ref.booking_id)
                risultato = await self._confirm_paid(ref.payment_intent_id)
                if risultato is not None:
                    confermate.append(risultato)

            except PaymentGatewayError:
                logger.exception(
                    "Rilascio fallito per %s: la prenotazione non verrà fatta scadere",
                    ref.code,
                )
                esclusi.append(ref.booking_id)

        return esclusi, confermate

    async def _confirm_paid(self, intent_id: str):
        """Conferma una prenotazione il cui pagamento è arrivato all'ultimo istante."""
        intent = await self._gateway.retrieve_payment_intent(intent_id)

        async with self._session_factory() as session:
            service = build_booking_service(session)
            return await service.confirm_paid_booking(
                intent_id, intent.card_brand, intent.card_last4
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
            "Sweeper avviato: intervallo %d secondi, lotti da %d, "
            "pulizia token ogni %d ore",
            settings.sweeper_interval_seconds,
            settings.sweeper_batch_size,
            settings.token_purge_interval_hours,
        )

        while not self._stopping.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Passata dello sweeper fallita; riprovo al prossimo giro")

            # Dopo la passata, mai prima: liberare gli slot è il lavoro per
            # cui questo ciclo esiste, la pulizia è ciò che si fa se avanza
            # tempo. Non serve un `try` qui: `purge_tokens_if_due` cattura già
            # tutto al proprio interno.
            await self.purge_tokens_if_due()

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
