"""
Utilità condivise dai router.

Non contiene endpoint: solo ciò che vive sul confine HTTP e serve a più di un
router.
"""
import json
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


def parse_form_model(model: Type[TModel], raw_json: str) -> TModel:
    """
    Costruisce uno schema dal campo JSON di un form `multipart`.

    I form di camere e servizi extra viaggiano come `multipart` (c'è
    un'immagine) con i dati in un unico campo di testo JSON. Qualunque
    problema di quel testo è colpa del client e diventa un `422`:

    - JSON malformato → `JSONDecodeError`, tradotto dall'handler globale;
    - JSON valido ma non un oggetto (`[1, 2]`, `"testo"`) → `422` qui;
    - campi non validi → `422` con il dettaglio, via `build_request_model`.

    Prima i router facevano `Schema(**json.loads(...))`: un campo sbagliato
    usciva come `500` (lacuna B15).

    :param model: schema da costruire.
    :param raw_json: contenuto del campo JSON del form.
    :return: istanza validata.
    :raises RequestValidationError: se i dati non rispettano lo schema.
    """
    data = json.loads(raw_json)
    if not isinstance(data, dict):
        raise RequestValidationError([{
            "type": "value_error",
            "loc": ("body",),
            "msg": "I dati del form devono essere un oggetto JSON",
        }])
    return build_request_model(model, **data)
