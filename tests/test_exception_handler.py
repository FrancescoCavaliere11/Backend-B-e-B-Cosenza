"""
Test dell'handler globale degli errori.

Non serve il database: l'oggetto in prova è la traduzione eccezione → risposta
HTTP, e si verifica su un'applicazione minima costruita qui dentro. Usare
quella vera costringerebbe a trovare un endpoint che sbaglia davvero, cioè a
tenere un bug in produzione per poterlo testare.
"""
from fastapi import Depends, FastAPI, Query
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from src.exception.exception_handler import setup_exception_handler
from src.routers.dependencies import build_request_model


class _Risposta(BaseModel):
    """Modello con un campo obbligatorio, usato per provocare l'errore."""
    campo: int


class _Filtri(BaseModel):
    """Modello costruito a mano da una dipendenza, come nei router reali."""
    pagina: int


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
