"""
Riconoscimento degli errori di integrità di PostgreSQL.

Un `IntegrityError` di SQLAlchemy avvolge l'errore del driver, e il **codice
SQLSTATE** dice quale vincolo è stato violato. Il riconoscimento vive qui, in
un posto solo, perché lo usano due livelli diversi: i Service, che traducono
la collisione in un errore di dominio (`RoomNotAvailable`), e l'handler
globale, che fa da rete di sicurezza per i percorsi che se ne dimenticano.
Due copie della stessa regola erano già divergute una volta: la conferma
dell'admin non intercettava la sovrapposizione, e rispondeva `500`.

Il messaggio dell'errore **non va mai restituito né registrato per intero**:
contiene l'istruzione SQL e i suoi parametri, cioè i dati dell'ospite.
"""
from typing import Optional

from sqlalchemy.exc import IntegrityError

#: Nome dell'exclusion constraint anti-overbooking (`booking_room_items`).
OVERLAP_CONSTRAINT = "ex_booking_room_items_no_overlap"

#: Codici SQLSTATE di PostgreSQL (classe 23, integrity constraint violation).
EXCLUSION_VIOLATION = "23P01"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


def sqlstate_of(error: IntegrityError) -> Optional[str]:
    """Codice SQLSTATE dell'errore del driver, se disponibile."""
    original = getattr(error, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def is_overlap_violation(error: IntegrityError) -> bool:
    """La collisione di due prenotazioni sulle stesse notti, fra le altre violazioni."""
    if sqlstate_of(error) == EXCLUSION_VIOLATION:
        return True
    return OVERLAP_CONSTRAINT in str(getattr(error, "orig", None) or error)


def is_conflict_violation(error: IntegrityError) -> bool:
    """
    Violazioni dovute allo **stato dei dati**, non a un errore del codice: un
    valore già presente o un riferimento a qualcosa che non esiste più. Sono
    conflitti (`409`). Le altre — un campo obbligatorio mancante, un vincolo
    di controllo — sono bug del server (`500`).
    """
    return sqlstate_of(error) in {EXCLUSION_VIOLATION, UNIQUE_VIOLATION, FOREIGN_KEY_VIOLATION}
