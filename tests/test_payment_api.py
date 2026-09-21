"""
Test di integrazione dei pagamenti.

Percorrono l'applicazione reale via `ASGITransport`, con Stripe sostituito da
un gateway in memoria: nessuna rete, nessuna chiave, nessun denaro.

**Il test che conta più di tutti è `test_lo_slot_perduto_non_produce_addebito`.**
È la ragione per cui esiste l'intero disegno a incasso differito: se le camere
sono state vendute ad altri, l'autorizzazione viene rilasciata e l'ospite non
paga nulla. Non "viene rimborsato": non paga.
"""
import json
from datetime import date, timedelta

from sqlalchemy import select, update

from src.data.enumerators import BookingStatus, PaymentStatus
from src.data.model.booking import Booking
from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.stripe_event import StripeEvent

BOOKINGS = "/api/v1/bookings"
PAYMENTS = "/api/v1/payments"

CHECK_IN = date.today() + timedelta(days=60)
CHECK_OUT = CHECK_IN + timedelta(days=2)

GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}

#: Due notti a 100 € meno il 10% di sconto per pagamento online.
TOTALE_ATTESO = "180.00"
CENTESIMI_ATTESI = 18000


# --------------------------------------------------------------------------- #
# Utilità                                                                      #
# --------------------------------------------------------------------------- #

async def _crea_prenotazione_da_pagare(api_client, room_id, guest=None) -> dict:
    """Prenotazione `PAY_NOW`, che nasce in attesa di pagamento."""
    quote = await api_client.post(
        f"{BOOKINGS}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(room_id)],
            "payment_option": "PAY_NOW",
        },
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["total_price"] == TOTALE_ATTESO

    creata = await api_client.post(
        f"{BOOKINGS}/",
        json={
            "quote_token": quote.json()["quote_token"],
            "guest": guest or GUEST,
            "accept_terms": True,
        },
    )
    assert creata.status_code == 201, creata.text
    assert creata.json()["booking"]["status"] == "PENDING_PAYMENT"
    return creata.json()["booking"]


async def _avvia_pagamento(api_client, code: str, email: str = None) -> dict:
    response = await api_client.post(
        f"{PAYMENTS}/intent",
        json={"code": code, "email": email or GUEST["email"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _evento(tipo: str, oggetto: dict, event_id: str = "evt_test_1") -> bytes:
    return json.dumps(
        {"id": event_id, "type": tipo, "data": {"object": oggetto}}
    ).encode("utf-8")


def _intent_autorizzato(intent_id: str, amount: int = CENTESIMI_ATTESI) -> dict:
    return {"id": intent_id, "amount": amount, "currency": "eur"}


async def _notifica(api_client, corpo: bytes, firma: str = "firma-valida"):
    return await api_client.post(
        f"{PAYMENTS}/webhook",
        content=corpo,
        headers={"Stripe-Signature": firma, "Content-Type": "application/json"},
    )


async def _ricarica(session, code: str) -> Booking:
    """
    Rilegge **solo** la prenotazione dal database.

    `populate_existing` sovrascrive con dati freschi l'oggetto già presente
    nella sessione, e nient'altro. Un `session.expire_all()` — la prima
    versione di questa funzione — scadrebbe invece **ogni** oggetto della
    sessione, comprese le camere della fixture `rooms`: il successivo
    `rooms[0].id` farebbe partire un caricamento pigro fuori dal contesto
    asincrono, cioè `MissingGreenlet`.
    """
    result = await session.execute(
        select(Booking)
        .where(Booking.code == code)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ===========================================================================
# Avvio del pagamento
# ===========================================================================

async def test_l_avvio_restituisce_il_segreto_e_l_importo(api_client, rooms, stripe_gateway):
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)

    intent = await _avvia_pagamento(api_client, prenotazione["code"])

    assert intent["client_secret"]
    assert intent["amount"] == TOTALE_ATTESO
    assert intent["booking_code"] == prenotazione["code"]


async def test_l_importo_comunicato_a_stripe_e_quello_della_prenotazione(
        api_client, rooms, stripe_gateway
):
    """
    L'importo non arriva mai dal client: viene letto dalla prenotazione, che a
    sua volta lo ha ricavato da un preventivo firmato dal server.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    creazioni = [c for c in stripe_gateway.calls if c[0] == "create"]
    assert len(creazioni) == 1
    assert creazioni[0][2] == CENTESIMI_ATTESI


async def test_avviare_due_volte_non_crea_due_pagamenti(api_client, rooms, stripe_gateway):
    """Due clic sul pulsante "Paga" non devono produrre due addebiti."""
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)

    primo = await _avvia_pagamento(api_client, prenotazione["code"])
    secondo = await _avvia_pagamento(api_client, prenotazione["code"])

    assert primo["client_secret"] == secondo["client_secret"]
    assert len([c for c in stripe_gateway.calls if c[0] == "create"]) == 1


async def test_l_avvio_proroga_il_blocco(api_client, rooms, session, stripe_gateway):
    """
    Un pagamento richiede più tempo di un clic su un link: autenticazione della
    banca, carta rifiutata e ritentata, ospite che si allontana.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    prima = (await _ricarica(session, prenotazione["code"])).hold_expires_at

    await _avvia_pagamento(api_client, prenotazione["code"])

    dopo = (await _ricarica(session, prenotazione["code"])).hold_expires_at
    assert dopo > prima


async def test_email_sbagliata_risponde_404(api_client, rooms, stripe_gateway):
    """Il solo codice non deve bastare ad aprire il pagamento di un altro."""
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)

    response = await api_client.post(
        f"{PAYMENTS}/intent",
        json={"code": prenotazione["code"], "email": "estraneo@example.com"},
    )
    assert response.status_code == 404


async def test_una_prenotazione_da_saldare_in_struttura_non_si_paga_online(
        api_client, rooms, stripe_gateway
):
    quote = await api_client.post(
        f"{BOOKINGS}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(rooms[0].id)],
            "payment_option": "PAY_ON_ARRIVAL",
        },
    )
    creata = await api_client.post(
        f"{BOOKINGS}/",
        json={
            "quote_token": quote.json()["quote_token"],
            "guest": GUEST,
            "accept_terms": True,
        },
    )

    response = await api_client.post(
        f"{PAYMENTS}/intent",
        json={"code": creata.json()["booking"]["code"], "email": GUEST["email"]},
    )
    assert response.status_code == 402


# ===========================================================================
# Il ciclo completo
# ===========================================================================

async def test_ciclo_completo_dal_pagamento_alla_conferma(
        api_client, rooms, session, stripe_gateway, email_backend
):
    """Criterio di completamento dello Step G."""
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)
    email_backend.clear()

    response = await _notifica(
        api_client,
        _evento("payment_intent.amount_capturable_updated", _intent_autorizzato(intent_id)),
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "CONFIRMED"

    booking = await _ricarica(session, prenotazione["code"])
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.payment_status == PaymentStatus.PAID
    assert booking.hold_expires_at is None
    assert booking.confirmed_at is not None
    assert booking.cancellation_deadline is None, "PAY_NOW non è rimborsabile"

    # Solo brand e ultime quattro cifre: nessun dato di carta sensibile.
    assert booking.card_brand == "visa"
    assert booking.card_last4 == "4242"

    messaggio = email_backend.last
    assert "confermata" in messaggio.subject.lower()
    assert "/prenotazione/gestisci?token=" in messaggio.text_body


async def test_l_incasso_avviene_solo_dopo_la_verifica(
        api_client, rooms, stripe_gateway
):
    """
    L'ordine delle chiamate a Stripe è il cuore del disegno: si crea, si
    incassa. Mai si incassa prima di aver verificato — e la verifica avviene
    nel database, fra le due chiamate.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)

    await _notifica(
        api_client,
        _evento("payment_intent.amount_capturable_updated", _intent_autorizzato(intent_id)),
    )

    operazioni = [c[0] for c in stripe_gateway.calls]
    assert operazioni == ["create", "capture"]


# ===========================================================================
# Lo slot perduto: il caso per cui esiste l'incasso differito
# ===========================================================================

async def test_lo_slot_perduto_non_produce_addebito(
        api_client, rooms, session, stripe_gateway, email_backend
):
    """
    **Il test più importante dello Step G.**

    L'ospite completa il pagamento, ma nel frattempo le camere sono state
    vendute a qualcun altro. Con l'incasso immediato avremmo i suoi soldi e
    nessuna stanza da dargli. Con l'incasso differito l'autorizzazione viene
    rilasciata: non c'è alcun addebito, quindi non serve alcun rimborso.
    """
    primo = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, primo["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)

    # Le camere vengono vendute a un altro ospite mentre il pagamento è in
    # corso. Si simula ciò che farebbe lo sweeper — righe camera disattivate —
    # con una UPDATE diretta: leggere `booking.items` dall'oggetto farebbe
    # partire un caricamento pigro fuori dal contesto asincrono.
    booking = await _ricarica(session, primo["code"])
    await session.execute(
        update(BookingRoomItem)
        .where(BookingRoomItem.booking_id == booking.id)
        .values(is_active=False)
    )
    await session.commit()

    secondo = await _crea_prenotazione_da_pagare(
        api_client, rooms[0].id, guest={**GUEST, "email": "giulia@example.com"}
    )
    assert secondo["code"] != primo["code"]

    email_backend.clear()
    response = await _notifica(
        api_client,
        _evento("payment_intent.amount_capturable_updated", _intent_autorizzato(intent_id)),
    )

    assert response.json()["outcome"] == "SLOT_LOST"

    # Nessun incasso e nessun rimborso: solo un rilascio.
    operazioni = [c[0] for c in stripe_gateway.calls]
    assert "capture" not in operazioni
    assert "cancel" in operazioni
    assert stripe_gateway.refunds == []

    perduta = await _ricarica(session, primo["code"])
    assert perduta.status == BookingStatus.CANCELLED
    assert perduta.payment_status != PaymentStatus.PAID

    # L'ospite deve sapere che non gli è stato addebitato nulla.
    messaggio = email_backend.last
    assert "non addebitato" in messaggio.text_body.lower() or \
           "NON TI ABBIAMO ADDEBITATO" in messaggio.text_body


# ===========================================================================
# Idempotenza
# ===========================================================================

async def test_lo_stesso_evento_due_volte_ha_un_solo_effetto(
        api_client, rooms, session, stripe_gateway, email_backend
):
    """
    Stripe consegna *at-least-once* e ritenta finché non riceve `200`. Una
    seconda consegna non deve produrre una seconda conferma né una seconda
    email.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)
    corpo = _evento(
        "payment_intent.amount_capturable_updated", _intent_autorizzato(intent_id)
    )

    email_backend.clear()
    prima = await _notifica(api_client, corpo)
    seconda = await _notifica(api_client, corpo)

    assert prima.json()["outcome"] == "CONFIRMED"
    assert seconda.json()["outcome"] == "DUPLICATE"
    assert len(email_backend.messages) == 1, "Una conferma, una email"
    assert len([c for c in stripe_gateway.calls if c[0] == "capture"]) == 1


async def test_l_evento_viene_registrato_e_marcato_elaborato(
        api_client, rooms, session, stripe_gateway
):
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)
    await _notifica(
        api_client,
        _evento(
            "payment_intent.amount_capturable_updated",
            _intent_autorizzato(intent_id),
            event_id="evt_unico",
        ),
    )

    session.expire_all()
    risultato = await session.execute(
        select(StripeEvent).where(StripeEvent.event_id == "evt_unico")
    )
    evento = risultato.scalar_one()

    assert evento.processed_at is not None
    assert evento.payload_digest, "Il digest serve a confrontare consegne ripetute"


async def test_la_conferma_di_incasso_successiva_non_duplica(
        api_client, rooms, stripe_gateway, email_backend
):
    """
    Dopo l'incasso Stripe invia anche `payment_intent.succeeded`. Normalmente
    non ha nulla da fare — ma è la rete di sicurezza se il processo fosse morto
    fra l'incasso e la scrittura.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)
    await _notifica(
        api_client,
        _evento(
            "payment_intent.amount_capturable_updated",
            _intent_autorizzato(intent_id),
            event_id="evt_1",
        ),
    )

    email_backend.clear()
    response = await _notifica(
        api_client,
        _evento("payment_intent.succeeded", _intent_autorizzato(intent_id), event_id="evt_2"),
    )

    assert response.json()["outcome"] == "ALREADY_CONFIRMED"
    assert email_backend.messages == []


# ===========================================================================
# Verifiche di sicurezza
# ===========================================================================

async def test_una_firma_non_valida_non_produce_effetti(
        api_client, rooms, session, stripe_gateway
):
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)

    response = await _notifica(
        api_client,
        _evento("payment_intent.amount_capturable_updated", _intent_autorizzato(intent_id)),
        firma="firma-falsificata",
    )

    assert response.status_code == 400
    booking = await _ricarica(session, prenotazione["code"])
    assert booking.status == BookingStatus.PENDING_PAYMENT
    assert "capture" not in [c[0] for c in stripe_gateway.calls]


async def test_un_importo_diverso_dal_dovuto_non_viene_incassato(
        api_client, rooms, session, stripe_gateway
):
    """
    L'importo si verifica, non si accetta. Vale in entrambe le direzioni: non
    si incassa né meno né più del dovuto.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])

    intent_id = list(stripe_gateway.intents)[0]
    stripe_gateway.authorize(intent_id)

    response = await _notifica(
        api_client,
        _evento(
            "payment_intent.amount_capturable_updated",
            _intent_autorizzato(intent_id, amount=100),
        ),
    )

    assert response.json()["outcome"] == "AMOUNT_MISMATCH"
    assert "capture" not in [c[0] for c in stripe_gateway.calls]

    booking = await _ricarica(session, prenotazione["code"])
    assert booking.status == BookingStatus.PENDING_PAYMENT


async def test_un_pagamento_senza_prenotazione_viene_rilasciato(
        api_client, rooms, stripe_gateway
):
    """Un'autorizzazione che non corrisponde a nulla non deve restare viva."""
    intent = await stripe_gateway.create_payment_intent(18000, "EUR", "BB-FANTASMA")
    stripe_gateway.authorize(intent.id)

    response = await _notifica(
        api_client,
        _evento("payment_intent.amount_capturable_updated", _intent_autorizzato(intent.id)),
    )

    assert response.json()["outcome"] == "UNKNOWN_BOOKING"
    assert "cancel" in [c[0] for c in stripe_gateway.calls]


# ===========================================================================
# Esiti negativi
# ===========================================================================

async def test_un_pagamento_rifiutato_resta_ritentabile(
        api_client, rooms, session, stripe_gateway
):
    """
    Una carta rifiutata è quasi sempre un problema di quella carta. Annullare
    la prenotazione costringerebbe l'ospite a rifare tutto per qualcosa che si
    risolve cambiando tessera.
    """
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])
    intent_id = list(stripe_gateway.intents)[0]

    response = await _notifica(
        api_client,
        _evento("payment_intent.payment_failed", _intent_autorizzato(intent_id)),
    )

    assert response.json()["outcome"] == "PAYMENT_FAILED"

    booking = await _ricarica(session, prenotazione["code"])
    assert booking.status == BookingStatus.PENDING_PAYMENT, "Lo slot resta suo"
    assert booking.payment_status == PaymentStatus.FAILED


async def test_un_rimborso_viene_registrato(api_client, rooms, session, stripe_gateway):
    prenotazione = await _crea_prenotazione_da_pagare(api_client, rooms[0].id)
    await _avvia_pagamento(api_client, prenotazione["code"])
    intent_id = list(stripe_gateway.intents)[0]

    response = await _notifica(
        api_client,
        _evento("charge.refunded", {"id": "ch_1", "payment_intent": intent_id}),
    )

    assert response.json()["outcome"] == "REFUNDED"
    booking = await _ricarica(session, prenotazione["code"])
    assert booking.payment_status == PaymentStatus.REFUNDED


async def test_un_evento_che_non_ci_riguarda_viene_confermato(api_client, stripe_gateway):
    """
    Rispondere con un errore a un evento che non ci interessa farebbe ritentare
    Stripe per giorni senza alcun motivo.
    """
    response = await _notifica(
        api_client, _evento("customer.created", {"id": "cus_1"})
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "IGNORED"
