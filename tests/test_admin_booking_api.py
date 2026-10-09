"""
Test di integrazione delle API amministrative.

L'autenticazione passa da un **login reale** su `/api/v1/auth/token`, non da
una dipendenza sovrascritta: così i test attraversano l'intera catena e
verificano anche che `is_admin_user` respinga chi amministratore non è.
"""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import update

from src.data.enumerators import PaymentMethod, PaymentStatus
from src.data.model.booking import Booking

BASE = "/api/v1/admin/bookings"
PUBLIC = "/api/v1/bookings"

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


async def _create_pending_payment(client, room_id) -> dict:
    """
    Prenotazione `PAY_NOW` dal canale pubblico: nasce `PENDING_PAYMENT`.
    Dal back-office non si può ottenere, ed è proprio il punto.
    """
    quote = await client.post(
        f"{PUBLIC}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(room_id)],
            "payment_option": "PAY_NOW",
        },
    )
    assert quote.status_code == 200, quote.text

    created = await client.post(
        f"{PUBLIC}/",
        json={
            "quote_token": quote.json()["quote_token"],
            "guest": {**GUEST, "email": "online@example.com"},
            "accept_terms": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["booking"]["status"] == "PENDING_PAYMENT"

    # La risposta pubblica non espone l'id: lo si ricava dall'elenco admin.
    listed = await client.get(BASE + "/", params={"code": created.json()["booking"]["code"]})
    return listed.json()["items"][0]


async def _register_payment(client, booking_id, **payload):
    return await client.post(f"{BASE}/{booking_id}/payment", json=payload)


async def _cancel(client, booking_id):
    response = await client.post(
        f"{BASE}/{booking_id}/status",
        json={"new_status": "CANCELLED", "reason": "Richiesta dell'ospite"},
    )
    assert response.status_code == 200, response.text
    return response.json()


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
# Ordinamento e contenuto della riga (01/10/2026)
# ===========================================================================

async def _create_two(admin_client, rooms):
    """Due prenotazioni distinguibili: Bianchi (prima creata) e Verdi (seconda, arrivo prima)."""
    prima = await _create_booking(admin_client, [rooms[0].id])
    seconda = await _create_booking(
        admin_client,
        [rooms[1].id],
        check_in=(CHECK_IN - timedelta(days=10)).isoformat(),
        check_out=(CHECK_OUT - timedelta(days=10)).isoformat(),
        guest={**GUEST, "firstname": "Luca", "lastname": "Verdi", "email": "luca.verdi@example.com"},
    )
    return prima, seconda


async def _search(admin_client, **params) -> dict:
    response = await admin_client.get(BASE + "/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestSortAndRow:

    async def test_ordinamento_predefinito_arrivo_piu_lontano(self, admin_client, rooms):
        prima, seconda = await _create_two(admin_client, rooms)

        body = await _search(admin_client)

        assert [item["code"] for item in body["items"]] == [prima["code"], seconda["code"]]

    async def test_arrivo_piu_vicino(self, admin_client, rooms):
        prima, seconda = await _create_two(admin_client, rooms)

        body = await _search(admin_client, sort="CHECK_IN_ASC")

        assert [item["code"] for item in body["items"]] == [seconda["code"], prima["code"]]

    async def test_ultime_inserite(self, admin_client, rooms):
        prima, seconda = await _create_two(admin_client, rooms)

        body = await _search(admin_client, sort="CREATED_DESC")

        assert [item["code"] for item in body["items"]] == [seconda["code"], prima["code"]]

    async def test_ordinamento_sconosciuto_rifiutato(self, admin_client, rooms):
        response = await admin_client.get(BASE + "/", params={"sort": "A_CASO"})
        assert response.status_code == 422

    async def test_la_riga_porta_nome_ospiti_e_camere(self, admin_client, rooms):
        await _create_booking(admin_client, [rooms[0].id])

        riga = (await _search(admin_client))["items"][0]

        assert riga["guest_firstname"] == GUEST["firstname"]
        assert riga["guest_lastname"] == GUEST["lastname"]
        assert riga["guest_count"] == 2
        assert riga["room_names"] == [rooms[0].name]


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

    async def test_scadenza_manuale_rifiutata(self, admin_client, rooms):
        """
        `PENDING_CONFIRMATION → EXPIRED` è ammessa dalla macchina a stati, ma
        appartiene allo sweeper: l'admin che vuole liberare lo slot annulla.
        """
        booking = await _create_booking(
            admin_client, [rooms[0].id], skip_email_confirmation=False
        )

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "EXPIRED"}
        )

        assert response.status_code == 409
        assert "automaticamente" in response.json()["message"]

    async def test_conferma_manuale_di_un_pagamento_online_rifiutata(self, admin_client, rooms):
        """Confermata a mano, resterebbe scontata del 10% e mai pagata."""
        booking = await _create_pending_payment(admin_client, rooms[0].id)

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CONFIRMED"}
        )

        assert response.status_code == 409
        assert "pagamento online" in response.json()["message"]

    async def test_conferma_manuale_di_una_attesa_email_consentita(self, admin_client, rooms):
        """Il blocco riguarda solo i pagamenti online: questo percorso resta."""
        booking = await _create_booking(
            admin_client, [rooms[0].id], skip_email_confirmation=False
        )

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CONFIRMED"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "CONFIRMED"

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


# ===========================================================================
# Registrazione manuale dei pagamenti (28/09/2026)
# ===========================================================================

class TestManualPayment:
    """
    Regole che dipendono dallo stato della prenotazione. Quelle di solo
    contratto (metodo, stati ammessi, campo `amount`) sono in
    `test_booking_schema.py::TestAdminPaymentRegistration`.
    """

    async def test_importo_rifiutato(self, admin_client, rooms):
        """Il campo era accettato e scartato: ora il client lo sa."""
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await _register_payment(
            admin_client, booking["id"],
            payment_method="CASH_ON_SITE", payment_status="PAID", amount="180.00",
        )

        assert response.status_code == 422
        assert "amount" in response.json()["message"]

    async def test_incasso_su_prenotazione_annullata_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])
        await _cancel(admin_client, booking["id"])

        response = await _register_payment(
            admin_client, booking["id"], payment_method="CASH_ON_SITE", payment_status="PAID"
        )

        assert response.status_code == 409

    async def test_incasso_su_prenotazione_in_attesa_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], skip_email_confirmation=False
        )

        response = await _register_payment(
            admin_client, booking["id"], payment_method="CASH_ON_SITE", payment_status="PAID"
        )

        assert response.status_code == 409

    async def test_doppio_incasso_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        response = await _register_payment(
            admin_client, booking["id"], payment_method="POS_ON_SITE", payment_status="PAID"
        )

        assert response.status_code == 409
        assert "già registrato" in response.json()["message"]

    async def test_rimborso_su_prenotazione_annullata_consentito(self, admin_client, rooms):
        """
        Il caso tipico: l'admin annulla, poi restituisce i contanti. Senza
        metodo indicato resta quello dell'incasso.
        """
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )
        await _cancel(admin_client, booking["id"])

        response = await _register_payment(
            admin_client, booking["id"], payment_status="REFUNDED",
            reason="Restituito all'ospite",
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["payment_status"] == "REFUNDED"
        assert response.json()["payment_method"] == "CASH_ON_SITE"

        # Riletto in una richiesta successiva: il 200 da solo non basta.
        riletta = await admin_client.get(f"{BASE}/{booking['id']}")
        assert riletta.json()["payment_status"] == "REFUNDED"

    async def test_rimborso_senza_incasso_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await _register_payment(
            admin_client, booking["id"], payment_status="REFUNDED",
            reason="Restituito all'ospite",
        )

        assert response.status_code == 409
        assert "risulti pagata" in response.json()["message"]

    async def test_correzione_riporta_da_incassare(self, admin_client, rooms):
        """Un incasso registrato per errore si annulla, e con lui il metodo."""
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="POS_ON_SITE", mark_as_paid=True
        )

        response = await _register_payment(
            admin_client, booking["id"], payment_status="PENDING",
            reason="Incasso registrato per errore",
        )

        assert response.status_code == 200, response.text
        assert response.json()["payment_status"] == "PENDING"
        assert response.json()["payment_method"] is None

    async def test_l_incasso_lascia_traccia_senza_dati_personali(self, admin_client, rooms, caplog):
        """
        La riga di audit si scrive dopo il commit, con codice, stati, metodo e
        admin. Mai email o nome dell'ospite: i log hanno un pubblico e una
        durata diversi dal database.
        """
        booking = await _create_booking(admin_client, [rooms[0].id])

        with caplog.at_level("INFO", logger="src.service.booking_service"):
            response = await _register_payment(
                admin_client, booking["id"], payment_method="CASH_ON_SITE", payment_status="PAID"
            )
        assert response.status_code == 200, response.text

        righe = [r.getMessage() for r in caplog.records if "Pagamento manuale" in r.getMessage()]
        assert len(righe) == 1
        assert booking["code"] in righe[0]
        assert "PENDING -> PAID" in righe[0]
        assert "CASH_ON_SITE" in righe[0]
        assert GUEST["email"] not in righe[0]
        assert GUEST["lastname"] not in righe[0]

    async def test_un_incasso_rifiutato_non_lascia_traccia(self, admin_client, rooms, caplog):
        booking = await _create_booking(admin_client, [rooms[0].id])

        with caplog.at_level("INFO", logger="src.service.booking_service"):
            response = await _register_payment(
                admin_client, booking["id"], payment_status="REFUNDED",
                reason="Restituito all'ospite",
            )
        assert response.status_code == 409

        assert not [r for r in caplog.records if "Pagamento manuale" in r.getMessage()]

    async def test_pagamento_online_non_modificabile(self, admin_client, rooms, session):
        """
        Un pagamento con carta si rimborsa da Stripe, e il webhook aggiorna lo
        stato. Una registrazione manuale ne produrrebbe una seconda versione.
        """
        booking = await _create_booking(admin_client, [rooms[0].id])
        # Si simula l'esito del webhook: nessun percorso admin produce STRIPE_CARD.
        await session.execute(
            update(Booking)
            .where(Booking.id == UUID(booking["id"]))
            .values(payment_method=PaymentMethod.STRIPE_CARD, payment_status=PaymentStatus.PAID)
        )
        await session.commit()

        response = await _register_payment(
            admin_client, booking["id"], payment_status="REFUNDED",
            reason="Restituito all'ospite",
        )

        assert response.status_code == 409
        assert "Stripe" in response.json()["message"]


# ===========================================================================
# Cambi di stato: risposta completa ed email (incremento 4 del frontend)
# ===========================================================================

def _link_token(corpo: str, percorso: str) -> str:
    """Token estratto dal link di una email, come farebbe l'ospite."""
    import re
    from urllib.parse import unquote

    trovato = re.search(rf"{re.escape(percorso)}\?token=(\S+)", corpo)
    assert trovato, f"Nessun link {percorso} nel messaggio:\n{corpo}"
    return unquote(trovato.group(1))


PAST_CHECK_IN = date.today() - timedelta(days=5)
PAST_CHECK_OUT = date.today() - timedelta(days=3)


class TestStatusResponseAndEmails:

    async def test_la_risposta_di_creazione_contiene_la_voce_di_creazione(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        storia = booking["status_history"]
        assert len(storia) == 1
        assert storia[0]["from_status"] is None
        assert storia[0]["to_status"] == "CONFIRMED"
        assert storia[0]["actor_type"] == "ADMIN"

    async def test_la_risposta_del_cambio_di_stato_contiene_la_nuova_voce(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status",
            json={"new_status": "CANCELLED", "reason": "Richiesta dell'ospite"},
        )

        assert response.status_code == 200, response.text
        storia = response.json()["status_history"]
        assert [voce["to_status"] for voce in storia] == ["CONFIRMED", "CANCELLED"]
        assert storia[-1]["from_status"] == "CONFIRMED"
        assert storia[-1]["reason"] == "Richiesta dell'ospite"
        assert storia[-1]["actor_type"] == "ADMIN"

    async def test_la_conferma_dell_admin_manda_il_link_di_gestione(
            self, admin_client, rooms, email_backend
    ):
        booking = await _create_booking(admin_client, [rooms[0].id], skip_email_confirmation=False)
        assert booking["status"] == "PENDING_CONFIRMATION"
        email_backend.clear()

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CONFIRMED"}
        )

        assert response.status_code == 200, response.text
        messaggi = email_backend.sent_to(GUEST["email"])
        assert len(messaggi) == 1, "Una conferma, un messaggio"
        assert "confermata" in messaggi[0].subject.lower()

        # Il link funziona davvero: l'ospite vede la propria prenotazione.
        token = _link_token(messaggi[0].text_body, "/prenotazione/gestisci")
        gestione = await admin_client.post(f"{PUBLIC}/manage", json={"token": token})
        assert gestione.status_code == 200, gestione.text
        assert gestione.json()["booking"]["status"] == "CONFIRMED"

    async def test_dopo_la_conferma_dell_admin_il_link_di_conferma_dice_gia_confermata(
            self, admin_client, rooms, email_backend
    ):
        """
        Il link di conferma viene spento, e chi lo apre dopo riceve lo stesso
        messaggio del doppio clic: «già confermata», che rassicura, invece di un
        generico «link non valido», che lo farebbe dubitare della prenotazione.
        """
        email_backend.clear()
        booking = await _create_booking(admin_client, [rooms[0].id], skip_email_confirmation=False)
        token = _link_token(email_backend.last.text_body, "/prenotazione/conferma")

        await admin_client.post(f"{BASE}/{booking['id']}/status", json={"new_status": "CONFIRMED"})
        response = await admin_client.post(f"{PUBLIC}/confirm", json={"token": token})

        assert response.status_code == 409
        assert "già stata confermata" in response.json()["message"]

    async def test_conferma_di_un_soggiorno_gia_iniziato_senza_link(
            self, admin_client, rooms, email_backend
    ):
        """Il link scade all'arrivo: per un soggiorno passato non avrebbe senso mandarlo."""
        booking = await _create_booking(
            admin_client,
            [rooms[0].id],
            check_in=PAST_CHECK_IN.isoformat(),
            check_out=PAST_CHECK_OUT.isoformat(),
            skip_email_confirmation=False,
        )
        email_backend.clear()

        response = await admin_client.post(
            f"{BASE}/{booking['id']}/status", json={"new_status": "CONFIRMED"}
        )

        assert response.status_code == 200, response.text
        messaggio = email_backend.last
        assert "confermata" in messaggio.subject.lower()
        assert "/prenotazione/gestisci?token=" not in messaggio.text_body

    async def test_arrivo_e_conclusione_non_mandano_email(self, admin_client, rooms, email_backend):
        booking = await _create_booking(
            admin_client,
            [rooms[0].id],
            check_in=PAST_CHECK_IN.isoformat(),
            check_out=PAST_CHECK_OUT.isoformat(),
        )
        email_backend.clear()

        for stato in ("CHECKED_IN", "COMPLETED"):
            response = await admin_client.post(
                f"{BASE}/{booking['id']}/status", json={"new_status": stato}
            )
            assert response.status_code == 200, response.text

        assert email_backend.messages == []
        assert [voce["to_status"] for voce in response.json()["status_history"]] == [
            "CONFIRMED", "CHECKED_IN", "COMPLETED",
        ]


# ===========================================================================
# Partenza anticipata e mancata presentazione (07/10/2026)
# ===========================================================================

TODAY = date.today()
#: Soggiorno in corso: arrivato da due giorni, parte fra due.
ONGOING_CHECK_IN = TODAY - timedelta(days=2)
ONGOING_CHECK_OUT = TODAY + timedelta(days=2)


async def _ongoing_booking(admin_client, room_id, **overrides) -> dict:
    return await _create_booking(
        admin_client,
        [room_id],
        check_in=overrides.pop("check_in", ONGOING_CHECK_IN).isoformat(),
        check_out=overrides.pop("check_out", ONGOING_CHECK_OUT).isoformat(),
        **overrides,
    )


async def _status(admin_client, booking_id, new_status, reason=None):
    body = {"new_status": new_status}
    if reason is not None:
        body["reason"] = reason
    return await admin_client.post(f"{BASE}/{booking_id}/status", json=body)


class TestExpiredHoldConfirmation:

    async def test_conferma_dopo_che_le_notti_sono_state_rivendute(
            self, admin_client, rooms, session
    ):
        """
        Blocco scaduto e notti già vendute a un altro ospite: la conferma
        dall'admin rispondeva `500`. Ora `409`, e nulla cambia.
        """
        in_attesa = await _create_booking(
            admin_client, [rooms[0].id], skip_email_confirmation=False
        )
        assert in_attesa["status"] == "PENDING_CONFIRMATION"

        # Il blocco scade prima che passi la scadenza automatica.
        await session.execute(
            update(Booking)
            .where(Booking.id == UUID(in_attesa["id"]))
            .values(hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()

        # Un altro ospite prende le stesse notti: la creazione libera il blocco scaduto.
        altra = await _create_booking(
            admin_client, [rooms[0].id], guest={**GUEST, "email": "altro.ospite@example.com"}
        )
        assert altra["status"] == "CONFIRMED"

        response = await admin_client.post(
            f"{BASE}/{in_attesa['id']}/status", json={"new_status": "CONFIRMED"}
        )

        assert response.status_code == 409, response.text
        assert "prenotate da qualcun altro" in response.json()["message"]

        prima = await admin_client.get(f"{BASE}/{in_attesa['id']}")
        assert prima.json()["status"] == "PENDING_CONFIRMATION"
        assert len(prima.json()["status_history"]) == 1
        seconda = await admin_client.get(f"{BASE}/{altra['id']}")
        assert seconda.json()["status"] == "CONFIRMED"


class TestEarlyDepartureAndNoShow:

    async def test_conclusione_anticipata_con_motivazione_consentita(self, admin_client, rooms):
        booking = await _ongoing_booking(admin_client, rooms[0].id)
        assert (await _status(admin_client, booking["id"], "CHECKED_IN")).status_code == 200

        response = await _status(admin_client, booking["id"], "COMPLETED", "Partito prima per lavoro")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "COMPLETED"
        assert response.json()["status_history"][-1]["reason"] == "Partito prima per lavoro"

    async def test_conclusione_anticipata_senza_motivazione_rifiutata(self, admin_client, rooms):
        booking = await _ongoing_booking(admin_client, rooms[0].id)
        await _status(admin_client, booking["id"], "CHECKED_IN")

        for reason in (None, "   "):
            response = await _status(admin_client, booking["id"], "COMPLETED", reason)
            assert response.status_code == 422
            assert "prima della data di partenza" in response.json()["message"]

        dettaglio = await admin_client.get(f"{BASE}/{booking['id']}")
        assert dettaglio.json()["status"] == "CHECKED_IN"

    async def test_conclusione_il_giorno_dell_arrivo_rifiutata(self, admin_client, rooms):
        booking = await _ongoing_booking(admin_client, rooms[0].id, check_in=TODAY)
        await _status(admin_client, booking["id"], "CHECKED_IN")

        response = await _status(admin_client, booking["id"], "COMPLETED", "Ripensamento")

        assert response.status_code == 409
        assert "dal giorno dopo l'arrivo" in response.json()["message"]

    async def test_partenza_anticipata_tiene_occupate_le_notti_rimaste(self, admin_client, rooms):
        """Le notti rimaste restano pagate e occupate: liberarle è una modifica (debito #21)."""
        booking = await _ongoing_booking(admin_client, rooms[0].id)
        await _status(admin_client, booking["id"], "CHECKED_IN")
        await _status(admin_client, booking["id"], "COMPLETED", "Partito prima")

        nuova = await admin_client.post(
            BASE + "/",
            json=_create_payload(
                [rooms[0].id],
                check_in=TODAY.isoformat(),
                check_out=ONGOING_CHECK_OUT.isoformat(),
                guest={**GUEST, "email": "subentro@example.com"},
            ),
        )
        assert nuova.status_code == 409

    async def test_non_presentato_libera_le_camere(self, admin_client, rooms):
        booking = await _ongoing_booking(admin_client, rooms[0].id)

        response = await _status(admin_client, booking["id"], "NO_SHOW")
        assert response.status_code == 200, response.text

        # Il calendario non le mostra più occupate...
        occupazione = await admin_client.get(
            f"{PUBLIC}/occupancy",
            params={
                "room_ids": str(rooms[0].id),
                "date_from": TODAY.isoformat(),
                "date_to": (ONGOING_CHECK_OUT + timedelta(days=1)).isoformat(),
            },
        )
        assert occupazione.status_code == 200, occupazione.text
        assert occupazione.json()["unavailable_nights"] == []

        # ...e le notti rimaste si possono rivendere.
        nuova = await admin_client.post(
            BASE + "/",
            json=_create_payload(
                [rooms[0].id],
                check_in=TODAY.isoformat(),
                check_out=ONGOING_CHECK_OUT.isoformat(),
                guest={**GUEST, "email": "subentro@example.com"},
            ),
        )
        assert nuova.status_code == 201, nuova.text

    async def test_non_presentato_libera_anche_le_notti_passate(self, admin_client, rooms):
        """Nessuno vi ha dormito: si può registrare a posteriori un ospite senza prenotazione."""
        booking = await _ongoing_booking(admin_client, rooms[0].id)
        await _status(admin_client, booking["id"], "NO_SHOW")

        walk_in = await admin_client.post(
            BASE + "/",
            json=_create_payload(
                [rooms[0].id],
                check_in=ONGOING_CHECK_IN.isoformat(),
                check_out=TODAY.isoformat(),
                guest={**GUEST, "email": "walkin@example.com"},
            ),
        )
        assert walk_in.status_code == 201, walk_in.text


# ===========================================================================
# Storico dei pagamenti (incremento 5 del frontend)
# ===========================================================================

class TestPaymentHistory:

    async def test_l_incasso_lascia_una_voce(self, admin_client, rooms):
        booking = await _create_booking(admin_client, [rooms[0].id])
        assert booking["payment_history"] == []

        response = await _register_payment(
            admin_client, booking["id"], payment_method="POS_ON_SITE", payment_status="PAID"
        )

        assert response.status_code == 200, response.text
        storico = response.json()["payment_history"]
        assert len(storico) == 1
        assert storico[0]["from_status"] == "PENDING"
        assert storico[0]["to_status"] == "PAID"
        assert storico[0]["payment_method"] == "POS_ON_SITE"
        assert storico[0]["actor_type"] == "ADMIN"
        assert storico[0]["reason"] is None
        # L'operatore resta nel database, non nella risposta.
        assert "actor_id" not in storico[0]

    async def test_rimborso_e_correzione_lasciano_voce_con_motivazione(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        rimborso = await _register_payment(
            admin_client, booking["id"], payment_status="REFUNDED", reason="  Restituiti in contanti  "
        )
        assert rimborso.status_code == 200, rimborso.text
        voce = rimborso.json()["payment_history"][-1]
        assert (voce["from_status"], voce["to_status"]) == ("PAID", "REFUNDED")
        assert voce["payment_method"] == "CASH_ON_SITE"
        assert voce["reason"] == "Restituiti in contanti"

        incasso = await _register_payment(
            admin_client, booking["id"], payment_method="BANK_TRANSFER", payment_status="PAID"
        )
        assert incasso.status_code == 200, incasso.text

        correzione = await _register_payment(
            admin_client, booking["id"], payment_status="PENDING", reason="Bonifico mai arrivato"
        )
        assert correzione.status_code == 200, correzione.text
        storico = correzione.json()["payment_history"]
        assert [v["to_status"] for v in storico] == ["PAID", "REFUNDED", "PAID", "PENDING"]
        assert storico[-1]["payment_method"] is None
        assert storico[-1]["reason"] == "Bonifico mai arrivato"

    async def test_rimborso_senza_motivazione_rifiutato(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        for motivazione in (None, "   "):
            payload = {"payment_status": "REFUNDED"}
            if motivazione is not None:
                payload["reason"] = motivazione
            response = await _register_payment(admin_client, booking["id"], **payload)
            assert response.status_code == 422
            assert "motivazione" in response.json()["message"].lower()

        riletta = await admin_client.get(f"{BASE}/{booking['id']}")
        assert riletta.json()["payment_status"] == "PAID"
        assert len(riletta.json()["payment_history"]) == 1

    async def test_un_operazione_rifiutata_non_lascia_voce(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        doppio = await _register_payment(
            admin_client, booking["id"], payment_method="CASH_ON_SITE", payment_status="PAID"
        )
        assert doppio.status_code == 409

        riletta = await admin_client.get(f"{BASE}/{booking['id']}")
        assert len(riletta.json()["payment_history"]) == 1

    async def test_la_creazione_gia_pagata_lascia_una_voce(self, admin_client, rooms):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        storico = booking["payment_history"]
        assert len(storico) == 1
        assert storico[0]["from_status"] is None
        assert storico[0]["to_status"] == "PAID"
        assert storico[0]["payment_method"] == "CASH_ON_SITE"
        assert storico[0]["actor_type"] == "ADMIN"
        assert storico[0]["reason"] == "Incasso registrato alla creazione"

    async def test_lo_storico_dei_pagamenti_non_esce_dalle_risposte_pubbliche(
            self, admin_client, rooms
    ):
        booking = await _create_booking(
            admin_client, [rooms[0].id], payment_method="CASH_ON_SITE", mark_as_paid=True
        )

        pubblica = await admin_client.post(
            f"{PUBLIC}/lookup", json={"code": booking["code"], "email": GUEST["email"]}
        )
        assert pubblica.status_code == 200, pubblica.text
        assert "payment_history" not in pubblica.json()
        assert "status_history" not in pubblica.json()

    async def test_registrare_un_pagamento_e_riservato_all_admin(self, user_client):
        """
        Il controllo del ruolo è sul router e scatta prima di qualunque
        lettura: un utente normale riceve 403 anche su un id inesistente.
        """
        response = await user_client.post(
            f"{BASE}/{uuid4()}/payment",
            json={"payment_status": "PAID", "payment_method": "CASH_ON_SITE"},
        )
        assert response.status_code == 403

    async def test_senza_sessione_401(self, api_client):
        response = await api_client.post(
            f"{BASE}/{uuid4()}/payment",
            json={"payment_status": "PAID", "payment_method": "CASH_ON_SITE"},
        )
        assert response.status_code == 401
