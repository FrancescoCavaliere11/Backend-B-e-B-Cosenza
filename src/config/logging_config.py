"""
Configurazione dei log applicativi.

**Perché serve un file apposta.** Uvicorn installa una propria configurazione
di logging che riguarda soltanto i logger `uvicorn.*`. Tutti gli altri — cioè
tutti quelli del progetto, che si chiamano `src.qualcosa` — restano **senza
handler**, e un logger senza handler scarta i messaggi in silenzio.

L'effetto è insidioso perché non somiglia a un errore: il codice gira, la
funzione fa il suo lavoro, e semplicemente non compare nulla. Si è visto al
primo avvio dello Step F — lo sweeper partiva e interrogava il database, ma la
riga "Sweeper avviato" non arrivava a destinazione. Peggio ancora per il
`ConsoleEmailBackend`, che *esiste solo* per scrivere nei log: senza handler
l'intero flusso di collaudo in sviluppo sarebbe stato muto.

**L'handler si attacca al logger `src`, non alla radice.** Configurare la
radice funzionerebbe, ma trascinerebbe dentro anche i log di SQLAlchemy — che
con `echo=True` ha già un handler suo — producendo ogni query due volte.
Limitandosi al sottoalbero del progetto non si interferisce con nessuno.
"""
import logging
import sys

from src.config.config import settings

#: Radice dei logger del progetto. Tutti i moduli usano `logging.getLogger(__name__)`,
#: e `__name__` inizia sempre con `src.`, quindi sono tutti suoi discendenti.
APP_LOGGER_NAME = "src"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> logging.Logger:
    """
    Attacca un handler su stdout al sottoalbero `src`.

    Idempotente: chiamarla due volte non duplica i messaggi. Serve perché in
    sviluppo il reloader di uvicorn reimporta il modulo dell'applicazione a
    ogni modifica.

    :return: il logger configurato, utile ai test.
    """
    logger = logging.getLogger(APP_LOGGER_NAME)

    if logger.handlers:
        logger.setLevel(settings.log_level.upper())
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    logger.addHandler(handler)
    logger.setLevel(settings.log_level.upper())

    # ⚠️ **La propagazione resta attiva.** L'istinto sarebbe spegnerla per
    # evitare doppioni se qualcuno aggiungesse un handler alla radice, ma
    # costerebbe caro: la fixture `caplog` di pytest cattura i messaggi
    # **proprio** attraverso la propagazione, con un handler installato sulla
    # radice. Con `propagate = False` i test che verificano cosa finisce nei
    # log — il token che non deve comparire, il destinatario mascherato —
    # smetterebbero di vedere alcunché e fallirebbero senza che nulla sia
    # davvero rotto.
    #
    # Il doppione, d'altra parte, è ipotetico: né uvicorn né SQLAlchemy
    # installano un handler sulla radice. Si materializzerebbe solo se qualcuno
    # chiamasse `logging.basicConfig()`, e a quel punto è lì che va corretto.

    return logger
