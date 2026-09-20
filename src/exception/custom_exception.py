"""
Gerarchia delle eccezioni di business del dominio.

Tutte le eccezioni applicative derivano da `AppException`, che porta con sé lo
status code HTTP appropriato. Questo consente a `exception_handler.py` di
registrare **un unico handler** invece di uno per eccezione: aggiungere un
nuovo errore di dominio richiede una sola classe, non anche un handler.

Nota di design: questo modulo non importa nulla da FastAPI o Starlette. Gli
status code sono interi semplici, così il layer di dominio resta indipendente
dal framework web e le eccezioni restano riusabili (test, job, CLI).
"""
from typing import Optional


class AppException(Exception):
    """
    Base di tutte le eccezioni di business.

    :cvar status_code: status HTTP con cui l'handler globale risponderà.
    :cvar default_message: messaggio usato quando il chiamante non ne fornisce uno.
    :ivar message: messaggio effettivo, esposto al client.
    """

    status_code: int = 400
    default_message: str = "Si è verificato un errore durante l'elaborazione della richiesta."

    def __init__(self, message: Optional[str] = None):
        self.message = message if message is not None else self.default_message
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Eccezioni generiche di entità
# ---------------------------------------------------------------------------

class EntityNotFound(AppException):
    status_code = 404
    default_message = "Entity not found"


class EntityAlreadyExists(AppException):
    status_code = 409
    default_message = "Entity already exists"


class EntityInUse(AppException):
    """
    L'entità è referenziata da altri dati e non può essere eliminata.

    Caso tipico: una camera con prenotazioni associate. La foreign key
    `booking_room_items.room_id` è `ON DELETE RESTRICT` proprio per impedire
    che la cancellazione di una camera distrugga lo storico.
    """
    status_code = 409
    default_message = "L'elemento è utilizzato da altri dati e non può essere eliminato"


# ---------------------------------------------------------------------------
# Eccezioni su file e upload
# ---------------------------------------------------------------------------

class InvalidFileType(AppException):
    status_code = 422
    default_message = "Invalid file type"


class InvalidFileSize(AppException):
    status_code = 422
    default_message = "Invalid file size"


class InvalidRoomService(AppException):
    status_code = 422
    default_message = "Invalid room service"


# ---------------------------------------------------------------------------
# Eccezioni del modulo Booking
# ---------------------------------------------------------------------------

class RoomNotAvailable(AppException):
    """
    Una o più camere risultano occupate per l'intervallo richiesto.

    Sollevata sia dal controllo applicativo preventivo, sia come traduzione
    dell'`IntegrityError` generato dall'exclusion constraint quando due
    richieste concorrenti arrivano al database nello stesso istante.
    """
    status_code = 409
    default_message = "Una o più camere non sono disponibili per le date richieste"


class InvalidBookingStatusTransition(AppException):
    """Transizione non prevista dalla macchina a stati della prenotazione."""
    status_code = 409
    default_message = "L'operazione non è consentita nello stato attuale della prenotazione"


class BookingNotCancellable(AppException):
    """
    La prenotazione non è più cancellabile dall'ospite.

    Tipicamente: termine di cancellazione gratuita superato, oppure
    prenotazione pagata online e quindi non rimborsabile.
    """
    status_code = 409
    default_message = "La prenotazione non può più essere cancellata"


class BookingHoldExpired(AppException):
    """
    Il blocco temporaneo dello slot è scaduto.

    410 e non 404: la risorsa è esistita ed è stata volutamente rilasciata.
    """
    status_code = 410
    default_message = "Il tempo per completare la prenotazione è scaduto e le date sono state liberate"


class InvalidBookingToken(AppException):
    """Token di conferma, gestione o cancellazione non valido, già usato o scaduto."""
    status_code = 400
    default_message = "Il link utilizzato non è valido o è già stato usato"


class InvalidDateRange(AppException):
    status_code = 422
    default_message = "L'intervallo di date indicato non è valido"


class InvalidGuestCount(AppException):
    status_code = 422
    default_message = "Il numero di ospiti non è compatibile con le camere selezionate"


class InvalidQuoteToken(AppException):
    """
    Preventivo assente, scaduto, manomesso o non coerente con il ricalcolo
    lato server. Il client non invia mai un prezzo: lo presenta solo firmato.
    """
    status_code = 422
    default_message = "Il preventivo non è più valido: ripeti la ricerca delle disponibilità"


class CaptchaValidationFailed(AppException):
    status_code = 422
    default_message = "Verifica anti-bot non superata"


class RateLimitExceeded(AppException):
    """
    Limite di frequenza superato.

    Porta con sé i secondi di attesa suggeriti, che l'handler globale traduce
    nell'header `Retry-After`: un client corretto può così riprovare al momento
    giusto invece di insistere a vuoto.
    """
    status_code = 429
    default_message = "Troppe richieste: riprova più tardi"

    def __init__(self, message: Optional[str] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class PaymentRequired(AppException):
    status_code = 402
    default_message = "È necessario completare il pagamento per confermare la prenotazione"


class PaymentFailed(AppException):
    status_code = 402
    default_message = "Il pagamento non è andato a buon fine"
