"""
Test di integrazione delle API pubbliche del modulo Booking.

Attraversano l'applicazione reale — middleware, dipendenze, router, exception
handler — tramite `ASGITransport`, senza aprire una porta di rete. Sostituiscono
la verifica manuale da Swagger con una ri-eseguibile.
"""
from datetime import date, timedelta

import pytest

from src.config.config import settings

BASE = "/api/v1/bookings"

CHECK_IN = date.today() + timedelta(days=40)
CHECK_OUT = CHECK_IN + timedelta(days=2)

GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}


def _availability_params(guest_count: int = 2) -> dict:
    return {
        "check_in": CHECK_IN.isoformat(),
        "check_out": CHECK_OUT.isoformat(),
        "guest_count": guest_count,
    }


async def _get_quote(api_client, room_id, guest_count: int = 2) -> dict:
    response = await api_client.post(
        f"{BASE}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": guest_count,
            "room_ids": [str(room_id)],
            "payment_option": "PAY_ON_ARRIVAL",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# Il flusso end-to-end: criterio di completamento dello Step D
# ===========================================================================

async def test_flusso_completo_availability_quote_create_confirm(api_client, rooms):
    room = rooms[0]

    # 1. Disponibilità
    response = await api_client.get(f"{BASE}/availability", params=_availability_params())
    assert response.status_code == 200
    availability = response.json()

    room_ids = [item["id"] for item in availability["rooms"]]
    assert str(room.id) in room_ids
    assert availability["nights"] == 2

    # 2. Preventivo firmato
    quote = await _get_quote(api_client, room.id)
    assert quote["quote_token"]
    assert quote["total_price"] == "200.00"

    # 3. Creazione
    response = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    assert response.status_code == 201, response.text
    created = response.json()

    assert created["booking"]["status"] == "PENDING_CONFIRMATION"
    assert created["booking"]["code"].startswith("BB-")
    assert created["confirmation_token"], "In sviluppo il token deve tornare al client"

    # La vista pubblica non espone dati interni.
    for campo_interno in ("id", "user_id", "version", "admin_notes", "created_by"):
        assert campo_interno not in created["booking"]

    # 4. Conferma
    response = await api_client.post(
        f"{BASE}/confirm", json={"token": created["confirmation_token"]}
    )
    assert response.status_code == 200, response.text
    confirmed = response.json()

    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["hold_expires_at"] is None
    assert confirmed["cancellation_deadline"] is not None


# ===========================================================================
# Disponibilità e preventivo
# ===========================================================================

async def test_availability_marca_le_camere_che_bastano_da_sole(api_client, rooms):
    response = await api_client.get(f"{BASE}/availability", params=_availability_params(4))
    assert response.status_code == 200

    per_numero = {item["number"]: item for item in response.json()["rooms"]}
    assert per_numero[101]["fits_all_guests"] is False   # doppia
    assert per_numero[102]["fits_all_guests"] is True    # quadrupla


async def test_availability_propone_combinazioni(api_client, rooms):
    response = await api_client.get(f"{BASE}/availability", params=_availability_params(4))
    combinazioni = response.json()["suggested_combinations"]

    assert combinazioni
    for combinazione in combinazioni:
        assert combinazione["total_capacity"] >= 4


async def test_availability_date_incoerenti_rifiutate(api_client, rooms):
    response = await api_client.get(
        f"{BASE}/availability",
        params={
            "check_in": CHECK_OUT.isoformat(),
            "check_out": CHECK_IN.isoformat(),
            "guest_count": 2,
        },
    )
    assert response.status_code == 422
    assert "successiva" in response.json()["message"]


async def test_preventivo_su_camera_inesistente(api_client, rooms):
    from uuid import uuid4

    response = await api_client.post(
        f"{BASE}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(uuid4())],
            "payment_option": "PAY_ON_ARRIVAL",
        },
    )
    assert response.status_code == 404


# ===========================================================================
# Protezioni degli endpoint pubblici
# ===========================================================================

async def test_honeypot_compilato_rifiutato(api_client, rooms):
    quote = await _get_quote(api_client, rooms[0].id)

    response = await api_client.post(
        f"{BASE}/",
        json={
            "quote_token": quote["quote_token"],
            "guest": GUEST,
            "accept_terms": True,
            "website": "http://spam.example",
        },
    )
    assert response.status_code == 422


async def test_condizioni_non_accettate_rifiutate(api_client, rooms):
    quote = await _get_quote(api_client, rooms[0].id)

    response = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": False},
    )
    assert response.status_code == 422
    assert "condizioni" in response.json()["message"]


async def test_rate_limit_con_retry_after(api_client, rooms):
    """Superato il limite per IP, l'endpoint risponde 429 con `Retry-After`."""
    limite = settings.rate_limit_availability_per_ip_minute

    for _ in range(limite):
        response = await api_client.get(f"{BASE}/availability", params=_availability_params())
        assert response.status_code == 200

    response = await api_client.get(f"{BASE}/availability", params=_availability_params())

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) >= 1


async def test_slot_occupato_risponde_409(api_client, rooms):
    room = rooms[0]
    quote = await _get_quote(api_client, room.id)

    primo = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    assert primo.status_code == 201

    altro_ospite = {**GUEST, "email": "altro@example.com"}
    secondo = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": altro_ospite, "accept_terms": True},
    )
    assert secondo.status_code == 409


async def test_doppia_conferma_risponde_409(api_client, rooms):
    quote = await _get_quote(api_client, rooms[0].id)
    created = (
        await api_client.post(
            f"{BASE}/",
            json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
        )
    ).json()

    token = created["confirmation_token"]
    assert (await api_client.post(f"{BASE}/confirm", json={"token": token})).status_code == 200

    seconda = await api_client.post(f"{BASE}/confirm", json={"token": token})
    assert seconda.status_code == 409
    assert "già stata confermata" in seconda.json()["message"]


async def test_token_di_conferma_non_esposto_con_email_attiva(
        api_client, rooms, monkeypatch
):
    """
    Con l'invio email attivo il token deve viaggiare solo per email.

    È la garanzia che l'affordance di sviluppo si spenga da sé allo Step F,
    senza che nessuno debba ricordarsi di rimuoverla.
    """
    monkeypatch.setattr(settings, "email_enabled", True)

    quote = await _get_quote(api_client, rooms[0].id)
    response = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    assert response.status_code == 201
    assert response.json()["confirmation_token"] is None


# ===========================================================================
# Endpoint autenticati
# ===========================================================================
#
# I cinque test di questa sezione sono sospesi insieme alle rotte che
# verificano. Non sono rotti: falliscono perché il blocco `/me` di
# `booking_router.py` è stato disattivato, e un `404` al posto di un `401` è
# esattamente ciò che ci si aspetta quando un endpoint non è registrato.
#
# Restano qui, e marcati uno per uno invece che cancellati, perché sono la
# specifica di quelle rotte: il giorno in cui l'autenticazione dell'ospite
# finale esisterà, togliere il marcatore è tutto ciò che serve per sapere se
# funzionano ancora.

ROTTE_ME_DISATTIVATE = pytest.mark.skip(
    reason="Le rotte /me sono disattivate in booking_router.py: il blocco è "
           "racchiuso in una stringa. Riattivandole, togliere questo marcatore "
           "dai cinque test. Debito tecnico #23."
)


@ROTTE_ME_DISATTIVATE
async def test_le_mie_prenotazioni_richiedono_autenticazione(api_client):
    response = await api_client.get(f"{BASE}/me")
    assert response.status_code == 401


@ROTTE_ME_DISATTIVATE
async def test_creazione_utente_richiede_autenticazione(api_client):
    response = await api_client.post(
        f"{BASE}/me", json={"quote_token": "x" * 40, "accept_terms": True}
    )
    assert response.status_code == 401


@ROTTE_ME_DISATTIVATE
async def test_prenotazione_utente_autenticato_viene_persistita(
        user_client, rooms, regular_user
):
    """
    Verifica che la prenotazione **sopravviva alla richiesta**.

    Un `201` da solo non basta come prova. Fino allo Step E questo endpoint
    rispondeva correttamente ma non scriveva nulla: la dipendenza di
    autenticazione apriva una transazione per leggere l'utente, e il Service —
    vedendo la sessione già "in transazione" — concludeva che il commit
    spettasse a qualcun altro.

    L'unico modo per accorgersene è rileggere in una **richiesta successiva**,
    che usa una sessione diversa. Un assert sul corpo della risposta alla
    creazione non avrebbe visto niente di sbagliato.
    """
    quote = await _get_quote(user_client, rooms[0].id)

    creata = await user_client.post(
        f"{BASE}/me",
        json={"quote_token": quote["quote_token"], "accept_terms": True},
    )
    assert creata.status_code == 201, creata.text
    booking = creata.json()["booking"]

    # L'anagrafica viene copiata dal profilo, non richiesta all'utente.
    assert booking["guest_email"] == regular_user.email
    assert booking["guest_firstname"] == regular_user.firstname
    assert booking["status"] == "PENDING_CONFIRMATION"

    # Richiesta separata, sessione diversa: se il commit non fosse avvenuto,
    # qui non ci sarebbe nulla.
    elenco = await user_client.get(f"{BASE}/me")
    assert elenco.status_code == 200
    assert booking["code"] in [item["code"] for item in elenco.json()]


@ROTTE_ME_DISATTIVATE
async def test_utente_cancella_la_propria_prenotazione(user_client, rooms):
    """
    La cancellazione usa il **codice**, non l'identificativo interno: è l'unico
    riferimento che il client possiede, perché la vista pubblica non espone
    `id`.
    """
    quote = await _get_quote(user_client, rooms[0].id)
    booking = (
        await user_client.post(
            f"{BASE}/me",
            json={"quote_token": quote["quote_token"], "accept_terms": True},
        )
    ).json()["booking"]

    response = await user_client.post(
        f"{BASE}/me/{booking['code']}/cancel", json={"reason": "Cambio programma"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"

    # Anche la cancellazione deve essere persistita.
    elenco = await user_client.get(f"{BASE}/me")
    cancellata = next(b for b in elenco.json() if b["code"] == booking["code"])
    assert cancellata["status"] == "CANCELLED"


async def test_non_si_cancella_la_prenotazione_di_un_altro(user_client, api_client, rooms):
    """Una prenotazione altrui risponde `404`, non `403`."""
    quote = await _get_quote(api_client, rooms[0].id)
    altrui = (
        await api_client.post(
            f"{BASE}/",
            json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
        )
    ).json()["booking"]

    response = await user_client.post(
        f"{BASE}/me/{altrui['code']}/cancel", json={"reason": "Tentativo"}
    )
    assert response.status_code == 404


# ===========================================================================
# Consultazione
# ===========================================================================

async def test_lookup_con_dati_errati_non_rivela_nulla(api_client, rooms):
    response = await api_client.post(
        f"{BASE}/lookup", json={"code": "BB-2026-XXXXXX", "email": "nessuno@example.com"}
    )

    assert response.status_code == 404
    # Messaggio generico: non deve lasciar capire se il codice esista e sia
    # soltanto l'email a non corrispondere.
    assert response.json()["message"] == "Nessuna prenotazione trovata con i dati indicati"


async def test_lookup_con_codice_ed_email_corretti(api_client, rooms):
    quote = await _get_quote(api_client, rooms[0].id)
    created = (
        await api_client.post(
            f"{BASE}/",
            json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
        )
    ).json()

    response = await api_client.post(
        f"{BASE}/lookup",
        json={"code": created["booking"]["code"], "email": GUEST["email"]},
    )

    assert response.status_code == 200
    assert response.json()["code"] == created["booking"]["code"]


# ===========================================================================
# Email: criterio di completamento dello Step F
# ===========================================================================

def _estrai_token(corpo: str, percorso: str) -> str:
    """
    Recupera un token dal link contenuto nel corpo di una email.

    Fa quello che farebbe l'ospite: apre il messaggio e segue il link. È
    l'unico modo di verificare davvero che un token emesso dal server arrivi
    fino a un endpoint utilizzabile — assertire sul valore restituito dal
    servizio dimostrerebbe solo che il servizio parla con sé stesso.
    """
    import re
    from urllib.parse import unquote

    trovato = re.search(rf"{re.escape(percorso)}\?token=(\S+)", corpo)
    assert trovato, f"Nessun link {percorso} nel messaggio:\n{corpo}"
    return unquote(trovato.group(1))


async def test_la_creazione_manda_una_sola_email_di_conferma(
        api_client, rooms, email_backend
):
    quote = await _get_quote(api_client, rooms[0].id)
    await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    messaggi = email_backend.sent_to(GUEST["email"])
    assert len(messaggi) == 1, "Una creazione, un messaggio"
    assert "/prenotazione/conferma?token=" in messaggi[0].text_body


async def test_la_conferma_manda_il_riepilogo_con_il_link_di_gestione(
        api_client, rooms, email_backend
):
    quote = await _get_quote(api_client, rooms[0].id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    email_backend.clear()
    await api_client.post(
        f"{BASE}/confirm", json={"token": creata.json()["confirmation_token"]}
    )

    messaggio = email_backend.last
    assert "confermata" in messaggio.subject.lower()
    assert "/prenotazione/gestisci?token=" in messaggio.text_body


async def test_il_link_ricevuto_per_email_permette_davvero_di_annullare(
        api_client, rooms, email_backend
):
    """
    Il test che chiude il difetto scoperto pianificando lo Step F.

    `POST /cancel` cerca un token con scopo `CANCEL` o `MANAGE`, ma **nessun
    percorso di codice ne emetteva uno**: l'endpoint era scritto, testato a
    livello di servizio e documentato, e nessun client poteva raggiungerlo.
    Non se n'era accorto nessuno perché i test del servizio costruivano il
    token a mano, cosa che un ospite non può fare.

    Qui il token viene estratto dal corpo dell'email, come farebbe lui.
    """
    quote = await _get_quote(api_client, rooms[0].id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    await api_client.post(
        f"{BASE}/confirm", json={"token": creata.json()["confirmation_token"]}
    )

    token_di_gestione = _estrai_token(email_backend.last.text_body, "/prenotazione/gestisci")

    response = await api_client.post(
        f"{BASE}/cancel",
        json={"token": token_di_gestione, "reason": "Cambio di programma"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"


async def test_l_annullamento_avvisa_l_ospite(api_client, rooms, email_backend):
    quote = await _get_quote(api_client, rooms[0].id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    await api_client.post(
        f"{BASE}/confirm", json={"token": creata.json()["confirmation_token"]}
    )
    token = _estrai_token(email_backend.last.text_body, "/prenotazione/gestisci")

    email_backend.clear()
    await api_client.post(f"{BASE}/cancel", json={"token": token, "reason": "Imprevisto"})

    assert "annullata" in email_backend.last.subject.lower()


async def test_il_link_di_gestione_si_spende_una_volta_sola(
        api_client, rooms, email_backend
):
    """Un link di annullamento che resta valido è un link riutilizzabile."""
    quote = await _get_quote(api_client, rooms[0].id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    await api_client.post(
        f"{BASE}/confirm", json={"token": creata.json()["confirmation_token"]}
    )
    token = _estrai_token(email_backend.last.text_body, "/prenotazione/gestisci")

    await api_client.post(f"{BASE}/cancel", json={"token": token, "reason": "Imprevisto"})
    seconda = await api_client.post(
        f"{BASE}/cancel", json={"token": token, "reason": "Di nuovo"}
    )

    assert seconda.status_code == 400


async def test_il_pagamento_online_non_manda_ancora_nulla(
        api_client, rooms, email_backend
):
    """
    Con `PAY_NOW` la prenotazione nasce `PENDING_PAYMENT`: l'ospite è ancora
    sulla pagina di pagamento e non c'è niente da comunicargli. Annunciargli
    una prenotazione che l'incasso potrebbe far fallire sarebbe peggio del
    silenzio. La notifica arriverà dal webhook Stripe (Step G).
    """
    response = await api_client.post(
        f"{BASE}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(rooms[0].id)],
            "payment_option": "PAY_NOW",
        },
    )
    quote = response.json()

    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    assert creata.json()["booking"]["status"] == "PENDING_PAYMENT"
    assert email_backend.messages == []


async def test_un_canale_email_guasto_non_impedisce_la_prenotazione(
        api_client, rooms
):
    """
    Se il server SMTP è irraggiungibile la prenotazione deve comunque nascere.

    Rifiutarla costerebbe un incasso vero; una mail non partita si recupera dal
    back-office. Stessa scelta fatta per il captcha.
    """
    from src.service.email.backend import FailingEmailBackend
    from src.service.email.email_service import configure_email_service

    configure_email_service(FailingEmailBackend())

    quote = await _get_quote(api_client, rooms[0].id)
    response = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    assert response.status_code == 201, response.text


@ROTTE_ME_DISATTIVATE
async def test_la_prenotazione_di_un_utente_autenticato_avvisa_il_suo_indirizzo(
        user_client, regular_user, rooms, email_backend
):
    quote = await _get_quote(user_client, rooms[0].id)
    response = await user_client.post(
        f"{BASE}/me", json={"quote_token": quote["quote_token"], "accept_terms": True}
    )

    assert response.status_code == 201, response.text
    assert email_backend.sent_to(regular_user.email)


# ===========================================================================
# La pagina di gestione: POST /manage
# ===========================================================================
#
# Il link dell'email porta l'ospite sulla SPA con **solo un token**. Senza
# questi endpoint la pagina potrebbe offrirgli un pulsante "Annulla" e nulla
# più: non il soggiorno, non il prezzo, non se annullare gli costerà qualcosa.


async def _prenota_e_conferma(api_client, room_id, email_backend) -> str:
    """Percorso completo fino alla conferma; restituisce il token di gestione."""
    quote = await _get_quote(api_client, room_id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )
    assert creata.status_code == 201, creata.text

    conferma = await api_client.post(
        f"{BASE}/confirm", json={"token": creata.json()["confirmation_token"]}
    )
    assert conferma.status_code == 200, conferma.text

    return _estrai_token(email_backend.last.text_body, "/prenotazione/gestisci")


async def test_il_link_di_gestione_mostra_la_prenotazione(
        api_client, rooms, email_backend
):
    """
    Il test che chiude il difetto scoperto pianificando il frontend.

    Stessa forma di quello dello Step F: una credenziale emessa e recapitata,
    e nessun modo di usarla per lo scopo per cui l'ospite la riceve. Il token
    qui viene estratto dall'email, come farebbe lui.
    """
    token = await _prenota_e_conferma(api_client, rooms[0].id, email_backend)

    response = await api_client.post(f"{BASE}/manage", json={"token": token})

    assert response.status_code == 200, response.text
    corpo = response.json()

    # C'è abbastanza da riempire la pagina.
    assert corpo["booking"]["status"] == "CONFIRMED"
    assert corpo["booking"]["check_in"] == CHECK_IN.isoformat()
    assert corpo["booking"]["total_price"]
    assert corpo["booking"]["rooms"]

    # E non c'è niente che l'ospite non debba vedere.
    assert "id" not in corpo["booking"]
    assert "admin_notes" not in corpo["booking"]
    assert "version" not in corpo["booking"]


async def test_la_lettura_non_consuma_il_token(api_client, rooms, email_backend):
    """
    La pagina si ricarica. Un token speso alla prima lettura lascerebbe
    l'ospite davanti a un errore al primo aggiornamento — e senza più modo di
    annullare.
    """
    token = await _prenota_e_conferma(api_client, rooms[0].id, email_backend)

    for _ in range(3):
        assert (await api_client.post(f"{BASE}/manage", json={"token": token})).status_code == 200

    # E dopo tre letture il token annulla ancora.
    annulla = await api_client.post(f"{BASE}/cancel", json={"token": token})
    assert annulla.status_code == 200, annulla.text
    assert annulla.json()["status"] == "CANCELLED"


async def test_dice_che_la_cancellazione_e_possibile(api_client, rooms, email_backend):
    token = await _prenota_e_conferma(api_client, rooms[0].id, email_backend)

    politica = (
        await api_client.post(f"{BASE}/manage", json={"token": token})
    ).json()["cancellation"]

    assert politica["can_cancel"] is True
    assert politica["blocked_by"] is None


async def test_la_politica_annunciata_e_quella_applicata(
        api_client, rooms, email_backend
):
    """
    Il test che conta di più su questo endpoint.

    La pagina promette all'ospite un esito; l'endpoint di cancellazione lo
    mantiene. Le due risposte vengono dalla stessa funzione proprio perché
    non possano divergere, e questo verifica che la catena regga da un capo
    all'altro invece di fidarsi della struttura del codice.
    """
    token = await _prenota_e_conferma(api_client, rooms[0].id, email_backend)

    annunciato = (
        await api_client.post(f"{BASE}/manage", json={"token": token})
    ).json()["cancellation"]

    esito = await api_client.post(f"{BASE}/cancel", json={"token": token})

    assert annunciato["can_cancel"] is (esito.status_code == 200)


async def test_dopo_l_annullamento_il_token_non_legge_piu(
        api_client, rooms, email_backend
):
    """
    Il token viene speso dalla cancellazione, quindi anche la lettura si
    chiude. È deliberato: la risposta a `POST /cancel` porta già la
    prenotazione annullata, e la SPA ha ciò che le serve per mostrare l'esito
    senza rileggere.
    """
    token = await _prenota_e_conferma(api_client, rooms[0].id, email_backend)
    assert (await api_client.post(f"{BASE}/cancel", json={"token": token})).status_code == 200

    response = await api_client.post(f"{BASE}/manage", json={"token": token})
    assert response.status_code in (400, 410)


async def test_un_token_inventato_viene_respinto(api_client):
    response = await api_client.post(f"{BASE}/manage", json={"token": "x" * 43})
    assert response.status_code in (400, 410)


async def test_un_token_troppo_corto_e_un_errore_di_validazione(api_client):
    """`422` e non `400`: qui è la forma del dato a essere sbagliata."""
    response = await api_client.post(f"{BASE}/manage", json={"token": "corto"})
    assert response.status_code == 422


async def test_il_token_di_conferma_non_apre_la_pagina_di_gestione(
        api_client, rooms, email_backend
):
    """
    Gli scopi non sono intercambiabili: un token di conferma vale solo per
    confermare, altrimenti distinguerli non servirebbe a nulla.
    """
    quote = await _get_quote(api_client, rooms[0].id)
    creata = await api_client.post(
        f"{BASE}/",
        json={"quote_token": quote["quote_token"], "guest": GUEST, "accept_terms": True},
    )

    response = await api_client.post(
        f"{BASE}/manage", json={"token": creata.json()["confirmation_token"]}
    )
    assert response.status_code in (400, 410)
