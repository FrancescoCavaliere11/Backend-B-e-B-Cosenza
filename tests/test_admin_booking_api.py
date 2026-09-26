"""
Test di integrazione delle API amministrative.

L'autenticazione passa da un **login reale** su `/api/v1/auth/token`, non da
una dipendenza sovrascritta: così i test attraversano l'intera catena e
verificano anche che `is_admin_user` respinga chi amministratore non è.
"""
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

BASE = "/api/v1/admin/bookings"

CHECK_IN = date.today() + timedelta(days=50)
CHECK_OUT = CHECK_IN + timedelta(days=2)

GUEST = {
    "firstname": "Giulia",
    "lastname": "Bianchi",
    "email": "giulia.bianchi@example.com",
    "phone_number": "3339998877",
}


def _create_payload(room_ids, **overrides) -> dict:
    payload = {
        "check_in": CHECK_IN.isoformat(),
        "check_out": CHECK_OUT.isoformat(),
        "guest_count": 2,
        "room_ids": [str(room_id) for room_id in room_ids],
        "payment_option": "PAY_ON_ARRIVAL",
        "guest": GUEST,
    }
    payload.update(overrides)
    return payload


async def _create_booking(admin_client, room_ids, **overrides) -> dict:
    response = await admin_client.post(BASE + "/", json=_create_payload(room_ids, **overrides))
    assert response.status_code == 201, response.text
    return response.json()["booking"]


# ===========================================================================
# Autorizzazione
# ===========================================================================

class TestAuthorization:

    async def test_senza_autenticazione_401(self, api_client, rooms):
        assert (await api_client.get(BASE + "/")).status_code == 401

    async def test_utente_normale_403(self, user_client, rooms):
        assert (await user_client.get(BASE + "/")).status_code == 403

    async def test_amministratore_200(self, admin_client, rooms):
        assert (await admin_client.get(BASE + "/")).status_code == 200

    async def test_creazione_negata_a_utente_normale(self, user_client, rooms):
        response = await user_client.post(BASE + "/", json=_create_payload([rooms[0].id]))
        assert response.status_code == 403


# ===========================================================================
# Creazione per conto di terzi
# ===========================================================================

class TestCreate:

    async def test_con_ospite_manuale_nasce_confermata(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        assert booking["status"] == "CONFIRMED"
        assert booking["source_channel"] == "ADMIN_BACKOFFICE"
        assert booking["hold_expires_at"] is None
        assert booking["confirmed_at"] is not None
        assert booking["guest_email"] == GUEST["email"]
        assert booking["user_id"] is None

    async def test_nessun_token_se_si_salta_la_conferma(self, admin_client, rooms):
        response = await admin_client.post(BASE + "/", json=_create_payload([rooms[0].id]))
        assert response.json()["confirmation_token"] is None

    async def test_con_utente_registrato_usa_il_profilo(self, admin_client, rooms, regular_user):
        booking = await _create_booking(
            admin_client, [rooms[0].id], guest=None, user_id=str(regular_user.id)
        )

        assert booking["user_id"] == str(regular_user.id)
        assert booking["guest_email"] == regular_user.email

    async def test_utente_e_ospite_insieme_rifiutati(self, admin_client, rooms, regular_user):
        response = await admin_client.post(
            BASE + "/", json=_create_payload([rooms[0].id], user_id=str(regular_user.id))
        )
        assert response.status_code == 422

    async def test_incasso_contanti_registrato_alla_creazione(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        assert booking["payment_method"] == "CASH_ON_SITE"
        assert booking["payment_status"] == "PAID"

    async def test_data_nel_passato_consentita(self, admin_client, rooms):
        ieri = date.today() - timedelta(days=1)
        booking = await _create_booking(
            admin_client,
            [rooms[0].id],
            check_in=ieri.isoformat(),
            check_out=(ieri + timedelta(days=2)).isoformat(),
        )
        assert booking["check_in"] == ieri.isoformat()

    async def test_slot_occupato_rifiutato(self, admin_client, rooms):
        await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(BASE + "/", json=_create_payload([rooms[0].id]))
        assert response.status_code == 409


# ===========================================================================
# Consultazione
# ===========================================================================

class TestRead:

    async def test_dettaglio_con_storico(self, admin_client, rooms):
        creata = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.get(f"{BASE}/{creata['id']}")
        assert response.status_code == 200
        booking = response.json()

        assert booking["code"] == creata["code"]
        assert booking["version"] >= 1
        assert len(booking["status_history"]) >= 1
        assert booking["status_history"][0]["to_status"] == "CONFIRMED"
        # La vista amministrativa espone i campi di audit.
        assert booking["created_by"]
        assert booking["updated_at"]

    async def test_dettaglio_inesistente(self, admin_client, rooms):
        assert (await admin_client.get(f"{BASE}/{uuid4()}")).status_code == 404

    async def test_elenco_paginato(self, admin_client, rooms):
        await _create_booking(admin_client, [rooms[0].id])
        await _create_booking(
            admin_client,
            [rooms[1].id],
            check_in=(CHECK_IN + timedelta(days=10)).isoformat(),
            check_out=(CHECK_OUT + timedelta(days=10)).isoformat(),
            guest={**GUEST, "email": "secondo@example.com"},
        )

        response = await admin_client.get(BASE + "/", params={"page": 1, "page_size": 1})
        body = response.json()

        assert body["total"] == 2
        assert body["pages"] == 2
        assert len(body["items"]) == 1
        assert body["items"][0]["rooms_count"] == 1

    async def test_filtro_per_stato(self, admin_client, rooms):
        await _create_booking(admin_client, [rooms[0].id])

        confermate = await admin_client.get(BASE + "/", params={"status": "CONFIRMED"})
        assert confermate.json()["total"] == 1

        annullate = await admin_client.get(BASE + "/", params={"status": "CANCELLED"})
        assert annullate.json()["total"] == 0

    async def test_filtro_per_email(self, admin_client, rooms):
        await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.get(BASE + "/", params={"email": GUEST["email"]})
        assert response.json()["total"] == 1

    async def test_intervallo_di_date_invertito_rifiutato(self, admin_client, rooms):
        response = await admin_client.get(
            BASE + "/",
            params={"date_from": CHECK_OUT.isoformat(), "date_to": CHECK_IN.isoformat()},
        )
        assert response.status_code == 422


# ===========================================================================
# Modifica: rimossa
# ===========================================================================

class TestUpdateRimossa:
    """
    La modifica amministrativa non esiste più.

    Ricalcolava il totale senza sapere quanto fosse già stato incassato — la
    prenotazione registra il dovuto, non il pagato — quindi su una
    prenotazione saldata cancellava l'unica traccia dell'importo reale.
    Debito tecnico #21.

    Questo test non verifica una funzionalità ma la sua assenza, e vale la
    pena averlo: una rotta si riaggiunge in tre righe, e un `PATCH` che torna
    a rispondere `200` senza che nessuno l'abbia deciso è esattamente il modo
    in cui un problema noto rientra dalla finestra.
    """

    async def test_patch_non_esiste_piu(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.patch(
            f"{BASE}/{booking['id']}",
            json={
                "version": booking["version"],
                "check_in": booking["check_in"],
                "check_out": booking["check_out"],
                "guest_count": booking["guest_count"],
                "room_ids": [str(rooms[0].id)],
            },
        )

        # 405 e non 404: il percorso esiste ancora per GET, è il metodo a non
        # essere più ammesso.
        assert response.status_code == 405


# ===========================================================================
# Stato, incassi e blocco
# ===========================================================================

class TestOperations:

    async def test_annullamento_senza_motivazione_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CANCELLED"}
        )
        assert response.status_code == 422

    async def test_check_in_anticipato_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CHECKED_IN"}
        )
        assert response.status_code == 409
        assert "prima della data di check-in" in response.json()["message"]

    async def test_transizione_illegale_rifiutata(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "COMPLETED"}
        )
        assert response.status_code == 409

    async def test_annullamento_libera_lo_slot(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        await admin_client.post(
            f"{BASE}/{booking['id']}/status",
            json={"new_status": "CANCELLED", "reason": "Test"},
        )

        # Le stesse date tornano prenotabili.
        nuova = await admin_client.post(
            BASE + "/",
            json=_create_payload([rooms[0].id], guest={**GUEST, "email": "nuovo@example.com"}),
        )
        assert nuova.status_code == 201

    async def test_registrazione_incasso(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/payment",
            json={"payment_method": "POS_ON_SITE", "payment_status": "PAID"},
        )

        assert response.status_code == 200
        assert response.json()["payment_status"] == "PAID"
        assert response.json()["payment_method"] == "POS_ON_SITE"

    async def test_proroga_hold_su_prenotazione_confermata_rifiutata(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/extend-hold", json={"minutes": 30}
        )
        assert response.status_code == 409

    async def test_proroga_hold_su_prenotazione_in_attesa(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], skip_email_confirmation=False
        )
        assert booking["status"] == "PENDING_CONFIRMATION"

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/extend-hold", json={"minutes": 30}
        )

        assert response.status_code == 200
        assert response.json()["hold_expires_at"] > booking["hold_expires_at"]
