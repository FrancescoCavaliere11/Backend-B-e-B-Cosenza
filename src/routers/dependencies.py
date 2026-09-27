"""
Utilità condivise dai router.

Non contiene endpoint: solo ciò che vive sul confine HTTP e serve a più di un
router.
"""
from typing import Any, Type, TypeVar

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

TModel = TypeVar("TModel", bound=BaseModel)


def build_request_model(model: Type[TModel], **values: Any) -> TModel:
    """
    Costruisce uno schema a partire da dati del client.

    Serve alle dipendenze che compongono a mano un modello dai parametri di
    query — `get_availability_request`, `get_search_filters` — per far valere
    anche su una `GET` le stesse regole degli endpoint `POST`.

    **Il punto è la traduzione dell'errore.** Un modello costruito qui
    solleverebbe la `ValidationError` di Pydantic, indistinguibile da quella
    che il *server* produce quando sbaglia a comporre una risposta. Le due
    hanno colpevoli opposti: la prima è un `422` con il dettaglio dei campi,
    la seconda è un `500` da registrare nei log. Convertendola in
    `RequestValidationError` — la stessa eccezione che FastAPI solleva quando
    valida un body — il confine resta netto e l'handler globale non deve
    indovinare.

    :param model: schema da costruire.
    :param values: valori già estratti dalla richiesta.
    :return: istanza validata.
    :raises RequestValidationError: se i valori non rispettano lo schema.
    """
    try:
        return model(**values)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
