"""
Test di integrazione sulla cancellazione delle camere.

Una camera con prenotazioni non si elimina **mai**, nemmeno se disattivata:
la foreign key `booking_room_items.room_id` è `ON DELETE RESTRICT`, e
cancellarla distruggerebbe lo storico dei soggiorni. Disattivarla è
l'alternativa, non il prerequisito — ed è ciò che il messaggio deve dire.
"""
import json
from datetime import date, timedelta

ROOMS = "/api/v1/room/"
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


async def _disable(admin_client, room) -> None:
    """Disattiva la camera con lo stesso endpoint usato dalla schermata Stanze."""
    room_form = {
        "id": str(room.id),
        "name": room.name,
        "capacity": room.capacity,
        "price": str(room.price),
        "number": room.number,
        "room_services_ids": [],
        "enabled": False,
    }
    response = await admin_client.put(ROOMS, data={"room_form": json.dumps(room_form)})
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
