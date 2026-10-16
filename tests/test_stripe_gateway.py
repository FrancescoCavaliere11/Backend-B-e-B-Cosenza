"""
Test di `LiveStripeGateway` senza rete né chiavi (debito #24 e #25).

Il gateway reale riceve un modulo `stripe` finto (`stripe_module`), che
restituisce risposte con **la forma reale dell'API Stripe** — `latest_charge`
come oggetto, come stringa o assente — e registra le chiamate ricevute.

È proprio ciò che mancava: `FakeStripeGateway` lavora a valle di `_to_result`,
quindi nessun test leggeva mai una risposta di Stripe. Così è passato
inosservato che il campo `charges` non esisteva più.

Le risposte sono **oggetti Stripe veri** (`construct_from`), non dizionari:
dalla versione 15 la libreria restituisce oggetti senza `.get()`, e con dei
dizionari i test passavano mentre il gateway reale falliva al primo
pagamento (scoperto nel collaudo manuale del 10/10/2026).
"""
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import stripe as stripe_library
from pydantic import SecretStr

from src.config.config import settings
from src.exception.custom_exception import PaymentGatewayError, PaymentIntentNotCancellable
from src.service.payment.gateway import LiveStripeGateway, card_details_of


class _InvalidRequestError(Exception):
    """Stand-in di `stripe.error.InvalidRequestError`."""


class _SignatureVerificationError(Exception):
    """Stand-in di `stripe.error.SignatureVerificationError`."""


def _intent(status: str = "requires_capture", charge: Any = None) -> Dict[str, Any]:
    intent = {"id": "pi_1", "status": status, "amount": 18000, "currency": "eur", "client_secret": "cs"}
    if charge is not None:
        intent["latest_charge"] = charge
    return intent


def _charge(brand: str = "visa", last4: str = "4242") -> Dict[str, Any]:
    return {"id": "ch_1", "payment_method_details": {"card": {"brand": brand, "last4": last4}}}


class _FakeStripe:
    """
    Modulo `stripe` finto: risposte configurabili, chiamate registrate.

    `cancel_error` fa rifiutare l'annullamento; `retrieve_error` fa fallire la
    rilettura successiva.
    """

    def __init__(self, intent: Dict[str, Any], cancel_error: bool = False, retrieve_error: bool = False):
        self.intent = intent
        self.calls: List[tuple] = []
        fake = self

        def call(name):
            def method(*args, **kwargs):
                fake.calls.append((name, args, kwargs))
                if name == "cancel" and cancel_error:
                    raise _InvalidRequestError("This PaymentIntent's status is not cancelable")
                if name == "retrieve" and retrieve_error:
                    raise ConnectionError("rete assente")
                # Come la libreria vera: un oggetto, non un dizionario.
                return stripe_library.PaymentIntent.construct_from(fake.intent, "sk_test_finta")
            return method

        self.PaymentIntent = SimpleNamespace(
            create=call("create"), retrieve=call("retrieve"),
            capture=call("capture"), cancel=call("cancel"),
        )
        self.error = SimpleNamespace(
            InvalidRequestError=_InvalidRequestError,
            SignatureVerificationError=_SignatureVerificationError,
        )

        def construct_event(*args, **kwargs):
            fake.calls.append(("construct_event", args, kwargs))
            return stripe_library.Event.construct_from(
                {"id": "evt_1", "type": "payment_intent.succeeded",
                 "data": {"object": {"id": "pi_1", "latest_charge": "ch_1"}}},
                "sk_test_finta",
            )

        self.Webhook = SimpleNamespace(construct_event=construct_event)


# ---------------------------------------------------------------------------
# Dati della carta
# ---------------------------------------------------------------------------

class TestCardDetails:

    def test_addebito_espanso(self):
        assert card_details_of(_intent(charge=_charge("mastercard", "4444"))) == ("mastercard", "4444")

    def test_addebito_solo_identificativo_come_nei_webhook(self):
        assert card_details_of(_intent(charge="ch_1")) == (None, None)

    def test_nessun_addebito(self):
        assert card_details_of(_intent()) == (None, None)

    def test_il_vecchio_campo_charges_non_conta(self):
        """Il campo rimosso da Stripe nel 2022: leggerlo era il difetto #24."""
        intent = _intent()
        intent["charges"] = {"data": [_charge()]}
        assert card_details_of(intent) == (None, None)


class TestLiveGatewayResponses:

    async def test_incasso_chiede_l_addebito_espanso_e_ne_legge_la_carta(self):
        stripe = _FakeStripe(_intent("succeeded", _charge("visa", "1881")))

        result = await LiveStripeGateway(stripe_module=stripe).capture_payment_intent("pi_1")

        assert (result.card_brand, result.card_last4) == ("visa", "1881")
        name, args, kwargs = stripe.calls[0]
        assert name == "capture" and kwargs["expand"] == ["latest_charge"]

    async def test_tutte_le_letture_chiedono_l_espansione(self):
        stripe = _FakeStripe(_intent())
        gateway = LiveStripeGateway(stripe_module=stripe)

        await gateway.create_payment_intent(18000, "EUR", "BB-2026-000001")
        await gateway.retrieve_payment_intent("pi_1")

        assert all(kwargs.get("expand") == ["latest_charge"] for _, _, kwargs in stripe.calls)


    async def test_il_pagamento_accetta_solo_carte(self):
        """
        I metodi li decide il codice, non il cruscotto: con i metodi
        automatici comparivano Klarna, Amazon Pay e Satispay, che portano
        l'ospite fuori dal sito e non sono mai stati collaudati.
        """
        stripe = _FakeStripe(_intent())

        await LiveStripeGateway(stripe_module=stripe).create_payment_intent(
            18000, "EUR", "BB-2026-000001"
        )

        name, _, kwargs = stripe.calls[0]
        assert name == "create"
        assert kwargs["payment_method_types"] == ["card"]
        assert "automatic_payment_methods" not in kwargs


class TestStripeObjects:
    """La libreria restituisce oggetti senza `.get()`: il gateway li converte al confine."""

    async def test_l_avvio_del_pagamento_funziona_con_un_oggetto_stripe(self):
        stripe = _FakeStripe(_intent("requires_payment_method"))

        result = await LiveStripeGateway(stripe_module=stripe).create_payment_intent(
            18000, "EUR", "BB-2026-000001"
        )

        assert result.client_secret == "cs"
        assert (result.card_brand, result.card_last4) == (None, None)

    def test_il_webhook_restituisce_un_dizionario_anche_da_un_event(self, monkeypatch):
        monkeypatch.setattr(settings, "stripe_webhook_secret", SecretStr("whsec_prova"))
        stripe = _FakeStripe(_intent())

        event = LiveStripeGateway(stripe_module=stripe).verify_webhook(b"{}", "t=1,v1=x")

        assert type(event) is dict and type(event["data"]["object"]) is dict
        assert event.get("type") == "payment_intent.succeeded"


# ---------------------------------------------------------------------------
# Annullamento rifiutato (#25.1)
# ---------------------------------------------------------------------------

class TestRefusedCancel:

    async def test_gia_incassato_e_un_esito_non_un_errore(self):
        stripe = _FakeStripe(_intent("succeeded"), cancel_error=True)

        with pytest.raises(PaymentIntentNotCancellable):
            await LiveStripeGateway(stripe_module=stripe).cancel_payment_intent("pi_1")

    async def test_gia_annullato_e_un_esito_normale(self):
        stripe = _FakeStripe(_intent("canceled"), cancel_error=True)

        result = await LiveStripeGateway(stripe_module=stripe).cancel_payment_intent("pi_1")

        assert result.status == "canceled"

    async def test_stato_inatteso_non_viene_scambiato_per_un_incasso(self):
        stripe = _FakeStripe(_intent("processing"), cancel_error=True)

        with pytest.raises(PaymentGatewayError):
            await LiveStripeGateway(stripe_module=stripe).cancel_payment_intent("pi_1")

    async def test_rilettura_impossibile_non_viene_scambiata_per_un_incasso(self):
        stripe = _FakeStripe(_intent("succeeded"), cancel_error=True, retrieve_error=True)

        with pytest.raises(PaymentGatewayError):
            await LiveStripeGateway(stripe_module=stripe).cancel_payment_intent("pi_1")


# ---------------------------------------------------------------------------
# Webhook (#25.2)
# ---------------------------------------------------------------------------

def test_la_tolleranza_configurata_arriva_alla_verifica_della_firma(monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SecretStr("whsec_prova"))
    monkeypatch.setattr(settings, "stripe_webhook_tolerance_seconds", 120)
    stripe = _FakeStripe(_intent())

    LiveStripeGateway(stripe_module=stripe).verify_webhook(b"{}", "t=1,v1=x")

    name, _, kwargs = stripe.calls[0]
    assert name == "construct_event" and kwargs["tolerance"] == 120
