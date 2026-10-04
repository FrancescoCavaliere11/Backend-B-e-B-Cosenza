"""
Test di integrazione di `GET /api/v1/bookings/occupancy` (calendario).

Le prenotazioni vengono create dal back-office, con date esatte: il punto è
verificare *quali notti* risultano occupate, e il back-office è il modo più
diretto per collocarle. La regola di occupazione è la stessa della ricerca
per date: l'ultimo test lo verifica confrontando le due risposte.
"""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import update

from src.config.config import settings
from src.data.model.booking import Booking
from src.data.model.room import Room

PUBLIC = "/api/v1/bookings"
ADMIN = "/api/v1/admin/bookings"

#: Inizio della finestra interrogata: abbastanza avanti da non dipendere da oggi.
WINDOW_FROM = date.today() + timedelta(days=60)
WINDOW_TO = WINDOW_FROM + timedelta(days=61)

GUEST = {
    "firstname": "Anna",
    "lastname": "Calendario",
    "email": "anna.calendario@example.com",
    "phone_number": "3334445566",
}


def _day(offset: int) -> date:
    """Giorno della finestra: `_day(0)` è il primo."""
    return WINDOW_FROM + timedelta(days=offset)


async def _create(admin_client, room_ids, start: int, end: int, *, confirmed: bool = True) -> dict:
    """Prenotazione dal back-office dal giorno `start` al giorno `end` della finestra."""
    response = await admin_client.post(
        ADMIN + "/",
        json={
            "check_in": _day(start).isoformat(),
            "check_out": _day(end).isoformat(),
            "guest_count": 1,
            "room_ids": [str(room_id) for room_id in room_ids],
            "payment_option": "PAY_ON_ARRIVAL",
            "guest": GUEST,
            "skip_email_confirmation": confirmed,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["booking"]


async def _occupancy(client, room_ids, date_from=WINDOW_FROM, date_to=WINDOW_TO):
    return await client.get(
        PUBLIC + "/occupancy",
        params={
            "room_ids": [str(room_id) for room_id in room_ids],
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        },
    )


async def _nights(client, room_ids, **window) -> list:
    response = await _occupancy(client, room_ids, **window)
    assert response.status_code == 200, response.text
    return response.json()["unavailable_nights"]


async def _age_hold(session, code: str) -> None:
    """Porta nel passato la scadenza del blocco, senza far passare lo sweeper."""
    await session.execute(
        update(Booking)
        .where(Booking.code == code)
        .values(hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await session.commit()


class TestOccupancy:

    async def test_camere_libere_nessuna_notte_occupata(self, api_client, rooms):
        response = await _occupancy(api_client, [rooms[0].id])

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unavailable_nights"] == []
        assert body["date_from"] == WINDOW_FROM.isoformat()
        assert body["date_to"] == WINDOW_TO.isoformat()
        assert body["room_ids"] == [str(rooms[0].id)]

    async def test_e_pubblico_non_serve_autenticazione(self, api_client, rooms):
        """Nessun login: servirà anche alla prenotazione autonoma dell'ospite."""
        response = await _occupancy(api_client, [rooms[0].id])
        assert response.status_code == 200

    async def test_le_notti_occupate_escludono_il_giorno_di_partenza(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], 10, 12)

        nights = await _nights(admin_client, [rooms[0].id])

        assert nights == [_day(10).isoformat(), _day(11).isoformat()]

    async def test_la_risposta_non_contiene_dati_delle_prenotazioni(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], 10, 12)

        body = (await _occupancy(admin_client, [rooms[0].id])).json()

        assert set(body) == {"date_from", "date_to", "room_ids", "unavailable_nights"}
        assert "Calendario" not in str(body)

    async def test_le_notti_di_piu_camere_si_uniscono(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], 10, 12)
        await _create(admin_client, [rooms[1].id], 20, 21)

        nights = await _nights(admin_client, [rooms[0].id, rooms[1].id])

        assert nights == [_day(10).isoformat(), _day(11).isoformat(), _day(20).isoformat()]

    async def test_una_camera_non_vede_le_notti_dell_altra(self, admin_client, rooms):
        await _create(admin_client, [rooms[1].id], 20, 21)

        assert await _nights(admin_client, [rooms[0].id]) == []

    async def test_una_prenotazione_in_attesa_occupa(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], 5, 7, confirmed=False)

        nights = await _nights(admin_client, [rooms[0].id])

        assert nights == [_day(5).isoformat(), _day(6).isoformat()]

    async def test_un_blocco_scaduto_non_occupa_piu(self, admin_client, rooms, session):
        """Fra la scadenza e il passaggio dello sweeper lo slot è già libero, come in `/availability`."""
        booking = await _create(admin_client, [rooms[0].id], 5, 7, confirmed=False)
        await _age_hold(session, booking["code"])

        assert await _nights(admin_client, [rooms[0].id]) == []

    async def test_una_prenotazione_annullata_non_occupa(self, admin_client, rooms):
        booking = await _create(admin_client, [rooms[0].id], 5, 7)
        response = await admin_client.post(
            f"{ADMIN}/{booking['id']}/status",
            json={"new_status": "CANCELLED", "reason": "Prova del calendario"},
        )
        assert response.status_code == 200, response.text

        assert await _nights(admin_client, [rooms[0].id]) == []

    async def test_una_prenotazione_scaduta_non_occupa(self, admin_client, rooms, session):
        booking = await _create(admin_client, [rooms[0].id], 5, 7, confirmed=False)
        await _age_hold(session, booking["code"])
        sweep = await admin_client.post(f"{ADMIN}/sweep-expired")
        assert sweep.status_code == 200, sweep.text
        assert sweep.json()["expired_count"] == 1

        assert await _nights(admin_client, [rooms[0].id]) == []

    async def test_le_prenotazioni_a_cavallo_della_finestra_vengono_ritagliate(
            self, admin_client, rooms
    ):
        await _create(admin_client, [rooms[0].id], -2, 2)
        await _create(admin_client, [rooms[1].id], 60, 63)

        nights = await _nights(admin_client, [rooms[0].id, rooms[1].id])

        assert nights == [_day(0).isoformat(), _day(1).isoformat(), _day(60).isoformat()]

    async def test_camera_inesistente_404(self, api_client, rooms):
        response = await _occupancy(api_client, [rooms[0].id, uuid4()])

        assert response.status_code == 404

    async def test_camera_disattivata_409(self, api_client, rooms, session):
        await session.execute(update(Room).where(Room.id == rooms[0].id).values(enabled=False))
        await session.commit()

        response = await _occupancy(api_client, [rooms[0].id])

        assert response.status_code == 409
        assert rooms[0].name in response.json()["message"]

    async def test_finestra_troppo_ampia_422(self, api_client, rooms):
        oltre = WINDOW_FROM + timedelta(days=settings.occupancy_max_window_days + 1)

        response = await _occupancy(api_client, [rooms[0].id], date_to=oltre)

        assert response.status_code == 422

    async def test_finestra_nel_passato_422(self, api_client, rooms):
        ieri = date.today() - timedelta(days=1)

        response = await _occupancy(
            api_client, [rooms[0].id], date_from=ieri, date_to=ieri + timedelta(days=30)
        )

        assert response.status_code == 422

    async def test_senza_camere_422(self, api_client, rooms):
        response = await api_client.get(
            PUBLIC + "/occupancy",
            params={"date_from": WINDOW_FROM.isoformat(), "date_to": WINDOW_TO.isoformat()},
        )
        assert response.status_code == 422

    async def test_rate_limit(self, api_client, rooms):
        limite = settings.rate_limit_availability_per_ip_minute

        for _ in range(limite):
            assert (await _occupancy(api_client, [rooms[0].id])).status_code == 200

        response = await _occupancy(api_client, [rooms[0].id])

        assert response.status_code == 429
        assert "Retry-After" in response.headers

    async def test_coerente_con_la_ricerca_per_date(self, admin_client, rooms):
        """
        Le due modalità del form devono dirsi la stessa cosa: una camera con
        notti occupate in un intervallo non compare fra le libere di
        `/availability` per lo stesso intervallo, e viceversa.
        """
        await _create(admin_client, [rooms[0].id], 10, 12)

        async def libere(start: int, end: int) -> set:
            response = await admin_client.get(
                PUBLIC + "/availability",
                params={
                    "check_in": _day(start).isoformat(),
                    "check_out": _day(end).isoformat(),
                    "guest_count": 1,
                },
            )
            assert response.status_code == 200, response.text
            return {room["id"] for room in response.json()["rooms"]}

        nights = await _nights(admin_client, [rooms[0].id], date_from=_day(9), date_to=_day(13))
        assert nights == [_day(10).isoformat(), _day(11).isoformat()]

        assert str(rooms[0].id) not in await libere(10, 12)
        assert str(rooms[0].id) not in await libere(9, 11)
        # Arrivo nel giorno di partenza: libero in entrambe le letture.
        assert str(rooms[0].id) in await libere(12, 14)
        assert await _nights(admin_client, [rooms[0].id], date_from=_day(12), date_to=_day(14)) == []
