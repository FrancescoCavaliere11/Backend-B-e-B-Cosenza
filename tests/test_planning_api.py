"""
Test di integrazione di `GET /api/v1/admin/bookings/planning` (tabellone).

Le prenotazioni si creano dal back-office con date esatte, come nei test del
calendario pubblico. Il punto da verificare è **quali soggiorni compaiono**:
la regola deve essere la stessa della disponibilità, altrimenti il tabellone
mostrerebbe libera una camera che il sistema considera presa, o viceversa.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update

from src.config.config import settings
from src.data.model.booking import Booking
from src.data.model.room import Room

ADMIN = "/api/v1/admin/bookings"
PLANNING = ADMIN + "/planning"

#: Inizio della finestra futura: abbastanza avanti da non dipendere da oggi.
WINDOW_FROM = date.today() + timedelta(days=60)
WINDOW_TO = WINDOW_FROM + timedelta(days=31)

GUEST = {
    "firstname": "Anna",
    "lastname": "Tabellone",
    "email": "anna.tabellone@example.com",
    "phone_number": "3334445566",
}


def _day(offset: int, base: date = WINDOW_FROM) -> date:
    return base + timedelta(days=offset)


async def _create(admin_client, room_ids, check_in: date, check_out: date, *, confirmed: bool = True) -> dict:
    response = await admin_client.post(
        ADMIN + "/",
        json={
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guest_count": 2,
            "room_ids": [str(room_id) for room_id in room_ids],
            "payment_option": "PAY_ON_ARRIVAL",
            "guest": GUEST,
            "skip_email_confirmation": confirmed,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["booking"]


async def _planning(client, date_from: date = WINDOW_FROM, date_to: date = WINDOW_TO):
    return await client.get(
        PLANNING,
        params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )


async def _stays(client, **window) -> list:
    response = await _planning(client, **window)
    assert response.status_code == 200, response.text
    return response.json()["stays"]


async def _set_status(admin_client, booking_id: str, new_status: str, reason: str = None) -> None:
    payload = {"new_status": new_status}
    if reason:
        payload["reason"] = reason
    response = await admin_client.post(f"{ADMIN}/{booking_id}/status", json=payload)
    assert response.status_code == 200, response.text


class TestPlanningAccess:

    async def test_senza_autenticazione_401(self, api_client):
        assert (await _planning(api_client)).status_code == 401

    async def test_utente_semplice_403(self, user_client):
        assert (await _planning(user_client)).status_code == 403


class TestPlanningWindow:

    async def test_date_invertite_o_uguali_rifiutate(self, admin_client):
        assert (await _planning(admin_client, WINDOW_TO, WINDOW_FROM)).status_code == 422
        assert (await _planning(admin_client, WINDOW_FROM, WINDOW_FROM)).status_code == 422

    async def test_finestra_troppo_ampia_rifiutata(self, admin_client):
        limit = settings.planning_max_window_days
        assert (await _planning(admin_client, WINDOW_FROM, _day(limit))).status_code == 200
        response = await _planning(admin_client, WINDOW_FROM, _day(limit + 1))
        assert response.status_code == 422
        assert str(limit) in response.text

    async def test_il_passato_e_consultabile(self, admin_client, rooms):
        """A differenza del calendario pubblico: l'admin rivede i soggiorni conclusi."""
        past_from = date.today() - timedelta(days=40)
        booking = await _create(admin_client, [rooms[0].id], _day(2, past_from), _day(4, past_from))

        stays = await _stays(admin_client, date_from=past_from, date_to=_day(30, past_from))

        assert [stay["code"] for stay in stays] == [booking["code"]]


class TestPlanningContent:

    async def test_camere_ordinate_e_risposta_vuota(self, admin_client, rooms):
        body = (await _planning(admin_client)).json()

        assert body["date_from"] == WINDOW_FROM.isoformat()
        assert body["date_to"] == WINDOW_TO.isoformat()
        assert [room["number"] for room in body["rooms"]] == [101, 102]
        assert body["stays"] == []

    async def test_una_barra_per_camera_con_i_dati_della_prenotazione(self, admin_client, rooms):
        booking = await _create(admin_client, [rooms[0].id, rooms[1].id], _day(3), _day(6))

        stays = await _stays(admin_client)

        assert sorted(stay["room_id"] for stay in stays) == sorted([str(rooms[0].id), str(rooms[1].id)])
        for stay in stays:
            assert stay["booking_id"] == booking["id"]
            assert stay["code"] == booking["code"]
            assert stay["check_in"] == _day(3).isoformat()
            assert stay["check_out"] == _day(6).isoformat()
            assert stay["status"] == "CONFIRMED"
            assert stay["guest_name"] == "Anna Tabellone"
            assert stay["guest_count"] == 2

    async def test_nessuna_email_ne_telefono_nella_risposta(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], _day(3), _day(6))

        body = (await _planning(admin_client)).text

        assert GUEST["email"] not in body
        assert GUEST["phone_number"] not in body

    async def test_soggiorno_a_cavallo_del_bordo_con_le_date_reali(self, admin_client, rooms):
        await _create(admin_client, [rooms[0].id], _day(-2), _day(2))

        stays = await _stays(admin_client)

        assert len(stays) == 1
        assert stays[0]["check_in"] == _day(-2).isoformat()
        assert stays[0]["check_out"] == _day(2).isoformat()

    async def test_il_giorno_di_partenza_non_appartiene_alla_finestra_successiva(self, admin_client, rooms):
        """Un soggiorno che parte il primo giorno della finestra non la occupa."""
        await _create(admin_client, [rooms[0].id], _day(-3), _day(0))

        assert await _stays(admin_client) == []

    async def test_attesa_valida_inclusa_attesa_scaduta_esclusa(self, admin_client, rooms, session):
        pending = await _create(admin_client, [rooms[0].id], _day(3), _day(5), confirmed=False)

        stays = await _stays(admin_client)
        assert [stay["status"] for stay in stays] == ["PENDING_CONFIRMATION"]
        assert stays[0]["hold_expires_at"] is not None

        # Blocco scaduto, sweeper non ancora passato: le notti sono già libere.
        await session.execute(
            update(Booking)
            .where(Booking.code == pending["code"])
            .values(hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()

        assert await _stays(admin_client) == []

    async def test_annullata_esclusa(self, admin_client, rooms):
        booking = await _create(admin_client, [rooms[0].id], _day(3), _day(5))
        await _set_status(admin_client, booking["id"], "CANCELLED", "Prova del tabellone")

        assert await _stays(admin_client) == []

    async def test_non_presentato_escluso_conclusa_inclusa(self, admin_client, rooms):
        """Non presentato libera le notti; una conclusa le occupa ancora."""
        today = date.today()
        window = {"date_from": today - timedelta(days=5), "date_to": today + timedelta(days=5)}

        no_show = await _create(admin_client, [rooms[0].id], today - timedelta(days=2), today + timedelta(days=1))
        await _set_status(admin_client, no_show["id"], "NO_SHOW")

        completed = await _create(admin_client, [rooms[1].id], today - timedelta(days=3), today - timedelta(days=1))
        await _set_status(admin_client, completed["id"], "CHECKED_IN")
        await _set_status(admin_client, completed["id"], "COMPLETED")

        stays = await _stays(admin_client, **window)

        assert [(stay["code"], stay["status"]) for stay in stays] == [(completed["code"], "COMPLETED")]

    async def test_camera_disattivata_solo_se_ha_soggiorni(self, admin_client, rooms, session):
        await session.execute(update(Room).where(Room.id == rooms[1].id).values(enabled=False))
        await session.commit()

        body = (await _planning(admin_client)).json()
        assert [room["number"] for room in body["rooms"]] == [101]

        # Una prenotazione nata prima della disattivazione resta visibile, con la sua riga.
        await session.execute(update(Room).where(Room.id == rooms[1].id).values(enabled=True))
        await session.commit()
        await _create(admin_client, [rooms[1].id], _day(3), _day(5))
        await session.execute(update(Room).where(Room.id == rooms[1].id).values(enabled=False))
        await session.commit()

        body = (await _planning(admin_client)).json()
        assert [(room["number"], room["enabled"]) for room in body["rooms"]] == [(101, True), (102, False)]
        assert [stay["room_id"] for stay in body["stays"]] == [str(rooms[1].id)]
