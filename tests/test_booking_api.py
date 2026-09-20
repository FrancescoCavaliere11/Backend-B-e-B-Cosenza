"""
Test di integrazione delle API pubbliche del modulo Booking.

Attraversano l'applicazione reale — middleware, dipendenze, router, exception
handler — tramite `ASGITransport`, senza aprire una porta di rete. Sostituiscono
la verifica manuale da Swagger con una ri-eseguibile.
"""
from datetime import date, timedelta

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

async def test_le_mie_prenotazioni_richiedono_autenticazione(api_client):
    response = await api_client.get(f"{BASE}/me")
    assert response.status_code == 401


async def test_creazione_utente_richiede_autenticazione(api_client):
    response = await api_client.post(
        f"{BASE}/me", json={"quote_token": "x" * 40, "accept_terms": True}
    )
    assert response.status_code == 401


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
