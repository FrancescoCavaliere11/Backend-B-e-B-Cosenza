"""
Test dell'integrazione con Stripe che non richiedono database né rete.

Il grosso riguarda la **conversione degli importi**. Sembra il pezzo banale, ed
è quello dove un errore non produce un disallineamento contabile da correggere
ma un addebito sbagliato sulla carta di una persona.
"""
import json
from decimal import Decimal

import pytest

from src.exception.custom_exception import (
    InvalidWebhookSignature,
    PaymentGatewayError,
    PaymentIntentNotCancellable,
)
from src.service.payment.gateway import (
    FakeStripeGateway,
    from_minor_units,
    to_minor_units,
)


# --------------------------------------------------------------------------- #
# Conversione degli importi                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "importo, centesimi",
    [
        (Decimal("0.00"), 0),
        (Decimal("0.01"), 1),
        (Decimal("1.00"), 100),
        (Decimal("9.99"), 999),
        (Decimal("180.00"), 18000),
        (Decimal("1234.56"), 123456),
        # Decimal conserva gli zeri di coda: "200.0" e "200.00" sono lo stesso
        # numero ma rappresentazioni diverse, e devono dare lo stesso risultato.
        (Decimal("200.0"), 20000),
        (Decimal("200"), 20000),
    ],
)
def test_conversione_in_centesimi(importo, centesimi):
    assert to_minor_units(importo, "EUR") == centesimi


@pytest.mark.parametrize(
    "importo",
    [Decimal("0.00"), Decimal("9.99"), Decimal("180.00"), Decimal("1234.56")],
)
def test_la_conversione_e_reversibile(importo):
    """Andata e ritorno devono restituire esattamente lo stesso importo."""
    assert from_minor_units(to_minor_units(importo, "EUR"), "EUR") == importo


def test_il_risultato_e_un_intero_python():
    """
    Stripe vuole un intero. Un `Decimal` o un `float` verrebbero serializzati
    diversamente, e `18000.0` non è un importo valido per l'API.
    """
    risultato = to_minor_units(Decimal("180.00"), "EUR")
    assert isinstance(risultato, int)
    assert not isinstance(risultato, bool)


def test_le_valute_senza_decimali_vengono_rifiutate():
    """
    Lo yen non ha centesimi: trattarlo come l'euro moltiplicherebbe
    l'addebito per cento. Meglio un errore esplicito che un importo sbagliato.
    """
    with pytest.raises(PaymentGatewayError, match="decimali"):
        to_minor_units(Decimal("1000"), "JPY")


def test_lo_sconto_del_dieci_percento_si_converte_esatto():
    """
    Caso reale: due notti a 100 € con lo sconto per pagamento online.
    200 − 10% = 180,00 → 18000 centesimi, senza residui.
    """
    totale = (Decimal("200.00") - Decimal("20.00")).quantize(Decimal("0.01"))
    assert to_minor_units(totale, "EUR") == 18000


# --------------------------------------------------------------------------- #
# Il gateway finto riproduce la macchina a stati di Stripe                     #
# --------------------------------------------------------------------------- #

@pytest.fixture
def gateway() -> FakeStripeGateway:
    return FakeStripeGateway()


async def test_l_intent_nasce_in_attesa_di_pagamento(gateway):
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")

    assert intent.status == "requires_payment_method"
    assert intent.client_secret
    assert not intent.is_capturable
    assert not intent.is_captured


async def test_la_creazione_e_idempotente(gateway):
    """Due clic su "Paga" non devono produrre due addebiti."""
    primo = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")
    secondo = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")

    assert primo.id == secondo.id


async def test_dopo_l_autorizzazione_l_importo_e_incassabile(gateway):
    """
    Il passaggio chiave: autorizzato **ma non ancora prelevato**. È lo stato in
    cui possiamo ancora decidere di non incassare.
    """
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")
    autorizzato = gateway.authorize(intent.id)

    assert autorizzato.status == "requires_capture"
    assert autorizzato.is_capturable
    assert not autorizzato.is_captured


async def test_l_incasso_conclude_il_pagamento(gateway):
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")
    gateway.authorize(intent.id)

    incassato = await gateway.capture_payment_intent(intent.id)

    assert incassato.is_captured
    assert incassato.card_brand and incassato.card_last4


async def test_il_rilascio_non_muove_denaro(gateway):
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")
    gateway.authorize(intent.id)

    rilasciato = await gateway.cancel_payment_intent(intent.id)

    assert rilasciato.status == "canceled"
    assert gateway.refunds == [], "Un rilascio non è un rimborso"


async def test_il_rilascio_di_un_pagamento_gia_incassato_fallisce(gateway):
    """
    La corsa più stretta: l'ospite completa il pagamento nell'istante in cui
    stiamo annullando. Stripe rifiuta, e chi chiama deve concludere che il
    pagamento è andato a buon fine — non riprovare.
    """
    gateway.cancel_fails_as_captured = True
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")

    with pytest.raises(PaymentIntentNotCancellable):
        await gateway.cancel_payment_intent(intent.id)


# --------------------------------------------------------------------------- #
# Firma                                                                        #
# --------------------------------------------------------------------------- #

async def test_una_firma_non_valida_viene_respinta(gateway):
    """
    È l'unica autenticazione dell'endpoint: chiunque conosca l'URL può
    chiamarlo, solo Stripe sa firmarlo.
    """
    corpo = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded"}).encode()

    with pytest.raises(InvalidWebhookSignature):
        gateway.verify_webhook(corpo, "firma-inventata")


async def test_una_firma_valida_restituisce_l_evento(gateway):
    corpo = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded"}).encode()

    evento = gateway.verify_webhook(corpo, "firma-valida")

    assert evento["id"] == "evt_1"


# --------------------------------------------------------------------------- #
# Tracciamento delle chiamate                                                  #
# --------------------------------------------------------------------------- #

async def test_il_gateway_registra_cosa_gli_e_stato_chiesto(gateway):
    """
    Serve ai test di integrazione: verificare *che cosa abbiamo chiesto a
    Stripe* è diverso — e più utile — che verificare come abbiamo reagito.
    """
    intent = await gateway.create_payment_intent(18000, "EUR", "BB-2026-AAA111")
    gateway.authorize(intent.id)
    await gateway.capture_payment_intent(intent.id)

    operazioni = [chiamata[0] for chiamata in gateway.calls]
    assert operazioni == ["create", "capture"]
