"""
Test della verifica anti-bot (Cloudflare Turnstile).

Cloudflare non viene mai chiamato: il client HTTP usa un trasporto finto che
risponde come `siteverify`. Il database non serve.
"""
from typing import Callable, Optional

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from src.config.config import Settings, settings
from src.exception.custom_exception import CaptchaValidationFailed
from src.exception.exception_handler import setup_exception_handler
from src.security import captcha
from src.security.captcha import verify_captcha

_REAL_CLIENT = httpx.AsyncClient

#: Chiave segreta di prova di Cloudflare: in produzione viene dal `.env`.
_TEST_SECRET = "1x0000000000000000000000000000000AA"


@pytest.fixture
def captcha_on(monkeypatch):
    """Captcha attivo con una chiave segreta qualsiasi."""
    monkeypatch.setattr(settings, "captcha_enabled", True)
    monkeypatch.setattr(settings, "captcha_secret_key", SecretStr(_TEST_SECRET))


@pytest.fixture
def cloudflare(monkeypatch):
    """
    Sostituisce Cloudflare con `handler` e registra le richieste ricevute.

    :return: funzione che installa l'handler e restituisce la lista delle richieste.
    """
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list:
        calls: list = []

        def recording(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(
            captcha.httpx, "AsyncClient",
            lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(recording), **kwargs),
        )
        return calls

    return install


def _outcome(success: bool, codes: Optional[list] = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json={"success": success, "error-codes": codes or []})


# ===========================================================================
# Configurazione
# ===========================================================================

@pytest.mark.parametrize("secret", [None, "", "   "])
def test_captcha_attivo_senza_chiave_segreta_non_si_avvia(secret):
    """Prima la verifica veniva saltata in silenzio: captcha acceso solo in apparenza."""
    with pytest.raises(ValidationError, match="CAPTCHA_SECRET_KEY"):
        Settings(captcha_enabled=True, captcha_secret_key=secret)


def test_un_errore_di_configurazione_non_mostra_i_valori_del_env():
    """
    Senza `hide_input_in_errors` Pydantic metteva nell'errore tutti i valori
    letti, chiavi segrete comprese: il traceback d'avvio li scriveva nei log.
    """
    secret = "sk_test_NON_DEVE_COMPARIRE"

    with pytest.raises(ValidationError) as error:
        Settings(captcha_enabled=True, captcha_secret_key=None, stripe_secret_key=secret)

    assert "CAPTCHA_SECRET_KEY" in str(error.value)
    assert secret not in str(error.value)
    assert "input_value" not in str(error.value)


def test_captcha_disattivo_non_richiede_chiavi():
    assert Settings(captcha_enabled=False, captcha_secret_key=None).captcha_enabled is False


def test_captcha_attivo_con_chiave_si_avvia():
    assert Settings(captcha_enabled=True, captcha_secret_key=_TEST_SECRET).captcha_enabled is True


# ===========================================================================
# Verifica del token
# ===========================================================================

async def test_disattivo_non_chiama_cloudflare(monkeypatch, cloudflare):
    monkeypatch.setattr(settings, "captcha_enabled", False)
    calls = cloudflare(_outcome(False))

    await verify_captcha(None)

    assert calls == []


async def test_token_assente_e_rifiutato_senza_chiamare_cloudflare(captcha_on, cloudflare):
    calls = cloudflare(_outcome(True))

    with pytest.raises(CaptchaValidationFailed):
        await verify_captcha(None)
    assert calls == []


async def test_token_valido_passa_e_invia_chiave_e_ip(captcha_on, cloudflare):
    calls = cloudflare(_outcome(True))

    await verify_captcha("token-buono", "203.0.113.7")

    body = dict(httpx.QueryParams(calls[0].content.decode()))
    assert body == {"secret": _TEST_SECRET, "response": "token-buono", "remoteip": "203.0.113.7"}


async def test_token_rifiutato_non_rivela_il_motivo(captcha_on, cloudflare):
    """I codici di Cloudflare restano nei log: al client direbbero come aggirarlo."""
    cloudflare(_outcome(False, ["timeout-or-duplicate"]))

    with pytest.raises(CaptchaValidationFailed) as error:
        await verify_captcha("token-già-usato")
    assert "timeout" not in error.value.message


@pytest.mark.parametrize("failure", [
    lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("lento", request=request)),
    lambda request: httpx.Response(503),
    lambda request: httpx.Response(200, text="non json"),
])
async def test_cloudflare_irraggiungibile_lascia_passare(captcha_on, cloudflare, failure):
    """Fail-open deliberato: vedi la docstring di `verify_captcha`."""
    cloudflare(failure)

    await verify_captcha("token")


# ===========================================================================
# Risposta HTTP
# ===========================================================================

async def test_il_rifiuto_indica_il_campo_captcha_token():
    """
    Captcha rifiutato e preventivo scaduto sono entrambi `422`: senza il campo
    il sito non può distinguerli e ricalcolava il preventivo a vuoto.
    """
    app = FastAPI()
    setup_exception_handler(app)

    @app.post("/prenota")
    async def prenota():
        raise CaptchaValidationFailed()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/prenota")

    assert response.status_code == 422
    assert response.json() == {
        "message": "Verifica anti-bot non superata",
        "details": [{"loc": ["body", "captcha_token"], "msg": "Verifica anti-bot non superata", "type": "value_error"}],
    }
