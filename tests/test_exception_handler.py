"""
Test dell'handler globale degli errori.

Non serve il database: l'oggetto in prova è la traduzione eccezione → risposta
HTTP, e si verifica su un'applicazione minima costruita qui dentro. Usare
quella vera costringerebbe a trovare un endpoint che sbaglia davvero, cioè a
tenere un bug in produzione per poterlo testare.
"""
from typing import List
from uuid import UUID

from fastapi import Depends, FastAPI, Query
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from src.exception.exception_handler import setup_exception_handler
from src.routers.dependencies import build_request_model


class _ErroreDelDriver(Exception):
    """Errore del driver PostgreSQL ridotto all'essenziale: il codice SQLSTATE."""

    def __init__(self, sqlstate: str, testo: str):
        super().__init__(testo)
        self.sqlstate = sqlstate


#: Dato dell'ospite che compare nei parametri dell'istruzione SQL fallita.
_EMAIL_NEI_PARAMETRI = "mario.rossi@example.com"


def _violazione(sqlstate: str) -> IntegrityError:
    """`IntegrityError` come la solleva SQLAlchemy: istruzione, parametri, errore del driver."""
    return IntegrityError(
        "INSERT INTO bookings (guest_email) VALUES ($1)",
        (_EMAIL_NEI_PARAMETRI,),
        _ErroreDelDriver(sqlstate, f"violazione {sqlstate} per {_EMAIL_NEI_PARAMETRI}"),
    )


class _Risposta(BaseModel):
    """Modello con un campo obbligatorio, usato per provocare l'errore."""
    campo: int


class _Filtri(BaseModel):
    """Modello costruito a mano da una dipendenza, come nei router reali."""
    pagina: int


class _Chiuso(BaseModel):
    """Modello che rifiuta i campi non dichiarati, come `AdminPaymentRegistrationSchema`."""
    model_config = ConfigDict(extra="forbid")
    stato: str


class _ConLista(BaseModel):
    """Modello con una lista di UUID, come `room_ids` nelle prenotazioni."""
    room_ids: List[UUID]


def _app() -> FastAPI:
    application = FastAPI()
    setup_exception_handler(application)

    @application.get("/errore-del-server")
    async def errore_del_server():
        # Il server costruisce male un proprio modello: colpa nostra.
        return _Risposta()

    def filtri(pagina: str = Query(...)) -> _Filtri:
        # Una dipendenza che valida input del client, come
        # `get_availability_request` e `get_search_filters`.
        return build_request_model(_Filtri, pagina=pagina)

    @application.get("/errore-del-client")
    async def errore_del_client(f: _Filtri = Depends(filtri)):
        return {"pagina": f.pagina}

    @application.post("/campo-in-piu")
    async def campo_in_piu(corpo: _Chiuso):
        return {"stato": corpo.stato}

    @application.get("/vincolo/{sqlstate}")
    async def vincolo(sqlstate: str):
        # Un vincolo del database violato in un percorso che non lo intercetta.
        raise _violazione(sqlstate)

    @application.post("/lista")
    async def lista(corpo: _ConLista):
        return {"n": len(corpo.room_ids)}

    return application


async def _chiama(percorso: str, **params):
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(percorso, params=params)


# ===========================================================================
# I due colpevoli
# ===========================================================================

async def test_un_errore_del_server_e_un_500():
    """
    Era il difetto più insidioso dell'handler: un modello di risposta
    costruito male produceva un `422` *"il campo X è obbligatorio"*, che dice
    al chiamante di aver sbagliato lui. È già costato due sessioni di
    diagnosi nella direzione opposta, allo Step B e allo Step E.
    """
    response = await _chiama("/errore-del-server")

    assert response.status_code == 500
    assert response.json() == {"message": "Errore interno del server"}


async def test_il_500_non_descrive_i_modelli_interni():
    """
    Al client non va il nome del campo mancante: non può farci nulla, e
    descriverebbe la forma dei nostri schemi a chiunque sappia provocare
    l'errore. Il dettaglio vive nei log.
    """
    response = await _chiama("/errore-del-server")

    assert "campo" not in response.text
    assert "details" not in response.json()


async def test_un_errore_del_client_resta_un_422():
    """
    Il contrappeso: la separazione non deve trasformare in `500` ciò che è
    davvero colpa di chi chiama. Un modello costruito da una dipendenza a
    partire dai parametri di query è input del client a tutti gli effetti.
    """
    response = await _chiama("/errore-del-client", pagina="non-un-numero")

    assert response.status_code == 422
    assert response.json()["details"]


async def test_il_422_non_restituisce_il_valore_rifiutato():
    """
    La sanificazione resta sul ramo del client: la chiave `input` contiene il
    valore appena inviato, che su una registrazione sarebbe la password.
    """
    response = await _chiama("/errore-del-client", pagina="non-un-numero")

    for dettaglio in response.json()["details"]:
        assert "input" not in dettaglio


async def test_un_campo_non_previsto_e_un_422_leggibile():
    """
    Un client che invia un campo rimosso dal contratto — l'`amount`
    dell'incasso manuale — deve ricevere un messaggio che lo nomini, non il
    testo inglese di Pydantic.
    """
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/campo-in-piu", json={"stato": "PAID", "amount": "180.00"})

    assert response.status_code == 422
    assert response.json()["message"] == "Il campo 'amount' non è previsto."
    assert "180" not in response.text


async def test_un_errore_in_una_lista_nomina_il_campo_non_l_indice():
    """
    Il percorso di un errore su un elemento di lista termina con l'indice
    (`["body", "room_ids", 0]`): il messaggio diceva «campo '0'».
    """
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/lista", json={"room_ids": ["ROOM_ID"]})

    assert response.status_code == 422
    assert response.json()["message"].startswith("Errore sul campo 'room_ids'")


# ===========================================================================
# Vincoli del database: rete di sicurezza
# ===========================================================================

async def test_una_sovrapposizione_non_intercettata_e_un_409():
    """
    Il caso che ha motivato l'handler: la conferma dall'admin di una
    prenotazione con il blocco scaduto e le notti rivendute rispondeva `500`.
    """
    response = await _chiama("/vincolo/23P01")

    assert response.status_code == 409
    assert response.json() == {
        "message": "Una o più camere non sono disponibili per le date richieste"
    }


async def test_un_valore_duplicato_e_un_409_senza_dati_dell_istruzione():
    response = await _chiama("/vincolo/23505")

    assert response.status_code == 409
    # Né l'istruzione SQL né i suoi parametri: contengono i dati dell'ospite.
    assert _EMAIL_NEI_PARAMETRI not in response.text
    assert "INSERT" not in response.text


async def test_un_campo_obbligatorio_mancante_e_un_500():
    """Un `NOT NULL` violato è un bug del server, non un conflitto da far ritentare."""
    response = await _chiama("/vincolo/23502")

    assert response.status_code == 500
    assert response.json() == {"message": "Errore interno del server"}
    assert _EMAIL_NEI_PARAMETRI not in response.text
