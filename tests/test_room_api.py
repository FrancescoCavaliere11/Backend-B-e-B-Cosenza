"""
Test di integrazione sulle camere: cancellazione e descrizione.

Una camera con prenotazioni non si elimina **mai**, nemmeno se disattivata:
la foreign key `booking_room_items.room_id` è `ON DELETE RESTRICT`, e
cancellarla distruggerebbe lo storico dei soggiorni. Disattivarla è
l'alternativa, non il prerequisito — ed è ciò che il messaggio deve dire.
"""
import json
from datetime import date, timedelta

ROOMS = "/api/v1/room/"
PUBLIC_ROOMS = "/api/v1/room/enabled"
BOOKINGS = "/api/v1/admin/bookings/"

CHECK_IN = date.today() + timedelta(days=40)
CHECK_OUT = CHECK_IN + timedelta(days=2)


async def _book(admin_client, room) -> None:
    """Una prenotazione confermata sulla camera, creata dal back-office."""
    response = await admin_client.post(
        BOOKINGS,
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(room.id)],
            "payment_option": "PAY_ON_ARRIVAL",
            "guest": {
                "firstname": "Anna",
                "lastname": "Camera",
                "email": "anna.camera@example.com",
                "phone_number": "3331234567",
            },
        },
    )
    assert response.status_code == 201, response.text


def _room_form(room, **overrides) -> dict:
    """Il form completo che la schermata Stanze manda in modifica."""
    form = {
        "id": str(room.id),
        "name": room.name,
        "capacity": room.capacity,
        "price": str(room.price),
        "number": room.number,
        "room_services_ids": [],
        "enabled": True,
    }
    form.update(overrides)
    return form


async def _update(admin_client, room, **overrides):
    return await admin_client.put(ROOMS, data={"room_form": json.dumps(_room_form(room, **overrides))})


async def _description_of(client, room) -> object:
    """Rilegge la camera con una richiesta separata (regola §3 di Booking_Module_Plan)."""
    response = await client.get(ROOMS)
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["id"] == str(room.id))["description"]


async def _disable(admin_client, room) -> None:
    """Disattiva la camera con lo stesso endpoint usato dalla schermata Stanze."""
    response = await _update(admin_client, room, enabled=False)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False


async def test_una_camera_con_prenotazioni_non_si_elimina(admin_client, rooms):
    await _book(admin_client, rooms[0])

    response = await admin_client.delete(f"{ROOMS}{rooms[0].id}")

    assert response.status_code == 409
    messaggio = response.json()["message"]
    assert "nemmeno se disattivata" in messaggio
    assert "disattivala" in messaggio


async def test_disattivarla_non_la_rende_eliminabile(admin_client, rooms):
    """
    Il caso emerso dal collaudo del 01/10/2026: il vecchio messaggio veniva
    letto come «disattivala e poi potrai eliminarla».
    """
    await _book(admin_client, rooms[0])
    await _disable(admin_client, rooms[0])

    response = await admin_client.delete(f"{ROOMS}{rooms[0].id}")

    assert response.status_code == 409


async def test_una_camera_senza_prenotazioni_si_elimina(admin_client, rooms):
    """La regola non deve essere diventata più restrittiva del necessario."""
    response = await admin_client.delete(f"{ROOMS}{rooms[1].id}")
    assert response.status_code == 204

    # Riletta in una richiesta successiva: il 204 da solo non basta.
    rimaste = await admin_client.get(ROOMS)
    assert str(rooms[1].id) not in [room["id"] for room in rimaste.json()]


# ---------------------------------------------------------------------------
# Descrizione (12/10/2026)
# ---------------------------------------------------------------------------

DESCRIPTION = "Un letto matrimoniale e due lettini.\nBagno privato con doccia."


async def test_la_descrizione_si_salva_e_si_rilegge(admin_client, rooms):
    response = await _update(admin_client, rooms[0], description=DESCRIPTION)

    assert response.status_code == 200, response.text
    assert response.json()["description"] == DESCRIPTION
    assert await _description_of(admin_client, rooms[0]) == DESCRIPTION


async def test_spazi_e_a_capo_ai_bordi_vengono_tolti(admin_client, rooms):
    response = await _update(admin_client, rooms[0], description=f"  {DESCRIPTION}\n\n")

    assert response.status_code == 200, response.text
    assert await _description_of(admin_client, rooms[0]) == DESCRIPTION


async def test_descrizione_vuota_diventa_null(admin_client, rooms):
    await _update(admin_client, rooms[0], description=DESCRIPTION)

    response = await _update(admin_client, rooms[0], description="   ")

    assert response.status_code == 200, response.text
    assert await _description_of(admin_client, rooms[0]) is None


async def test_senza_descrizione_nel_form_resta_null(admin_client, rooms):
    response = await _update(admin_client, rooms[0])

    assert response.status_code == 200, response.text
    assert response.json()["description"] is None


async def test_descrizione_troppo_lunga_e_un_422(admin_client, rooms):
    """Prima del 12/10 un form non valido costruito nel router usciva come 500."""
    response = await _update(admin_client, rooms[0], description="x" * 1001)

    assert response.status_code == 422, response.text
    assert await _description_of(admin_client, rooms[0]) is None


async def test_la_descrizione_e_pubblica(admin_client, rooms):
    """Il sito la legge da `GET /room/enabled` senza autenticazione."""
    await _update(admin_client, rooms[0], description=DESCRIPTION)

    admin_client.cookies.clear()  # da qui in poi è un visitatore anonimo
    response = await admin_client.get(PUBLIC_ROOMS)
    assert response.status_code == 200, response.text
    assert next(item for item in response.json() if item["id"] == str(rooms[0].id))["description"] == DESCRIPTION


# ===========================================================================
# Elenco pubblico e elenco del back-office (lacuna B1)
# ===========================================================================

async def test_l_elenco_pubblico_mostra_solo_le_camere_in_vendita(admin_client, rooms):
    """Una camera disattivata non deve arrivare al sito, nemmeno nascosta dal browser."""
    await _disable(admin_client, rooms[0])
    admin_client.cookies.clear()

    response = await admin_client.get(PUBLIC_ROOMS)

    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()]
    assert str(rooms[0].id) not in ids
    assert str(rooms[1].id) in ids


async def test_l_elenco_pubblico_e_ordinato_e_senza_campi_del_back_office(api_client, rooms):
    response = await api_client.get(PUBLIC_ROOMS)

    items = response.json()
    assert [item["number"] for item in items] == sorted(item["number"] for item in items)
    assert set(items[0]) == {"id", "number", "name", "capacity", "price", "description", "services"}


async def test_l_elenco_completo_e_riservato_all_admin(user_client, rooms):
    assert (await user_client.get(ROOMS)).status_code == 403

    user_client.cookies.clear()  # da qui in poi è un visitatore anonimo
    assert (await user_client.get(ROOMS)).status_code == 401


async def test_l_admin_vede_anche_le_camere_disattivate(admin_client, rooms):
    await _disable(admin_client, rooms[0])

    response = await admin_client.get(ROOMS)

    assert response.status_code == 200, response.text
    item = next(item for item in response.json() if item["id"] == str(rooms[0].id))
    assert item["enabled"] is False
