"""
Test dello sweeper delle prenotazioni scadute.

Richiedono il database: la correttezza dello sweeper si misura su `is_active`,
che è il predicato dell'exclusion constraint. Verificare che uno slot torni
davvero prenotabile significa provare a prenotarlo di nuovo, non ispezionare
una variabile.

La scadenza viene simulata spostando `hold_expires_at` nel passato con una
`UPDATE` diretta: aspettare quindici minuti reali non è un test.
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from src.data.enumerators import BookingStatus, BookingTokenPurpose
from src.data.model.booking import Booking
from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.booking_status_history import BookingStatusHistory
from src.data.model.booking_token import BookingToken
from src.service.booking_expiration_service import BookingExpirationService

BASE = "/api/v1/bookings"
ADMIN_BASE = "/api/v1/admin/bookings"

CHECK_IN = date.today() + timedelta(days=45)
CHECK_OUT = CHECK_IN + timedelta(days=2)

GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}


# --------------------------------------------------------------------------- #
# Utilità                                                                      #
# --------------------------------------------------------------------------- #

async def _create_pending(api_client, room_id, guest=None) -> dict:
    """Crea una prenotazione in attesa di conferma e ne restituisce il corpo."""
    quote = await api_client.post(
        f"{BASE}/quote",
        json={
            "check_in": CHECK_IN.isoformat(),
            "check_out": CHECK_OUT.isoformat(),
            "guest_count": 2,
            "room_ids": [str(room_id)],
            "payment_option": "PAY_ON_ARRIVAL",
        },
    )
    assert quote.status_code == 200, quote.text

    response = await api_client.post(
        f"{BASE}/",
        json={
            "quote_token": quote.json()["quote_token"],
            "guest": guest or GUEST,
            "accept_terms": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _age_hold(session, code: str, minutes_ago: int = 1) -> None:
    """Sposta indietro nel tempo la scadenza del blocco."""
    await session.execute(
        update(Booking)
        .where(Booking.code == code)
        .values(hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    )
    await session.commit()


async def _age_tokens(session, booking_id, days: int) -> None:
    """Sposta indietro nel tempo la scadenza dei token di una prenotazione."""
    await session.execute(
        update(BookingToken)
        .where(BookingToken.booking_id == booking_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(days=days))
    )
    await session.commit()


async def _count_tokens(session, booking_id) -> int:
    result = await session.execute(
        select(BookingToken).where(BookingToken.booking_id == booking_id)
    )
    return len(result.scalars().all())


async def _reload(session, code: str) -> Booking:
    """
    Rilegge una prenotazione scavalcando la cache della sessione.

    **Non usare `session.expire_all()` qui.** Fa la cosa giusta per la
    prenotazione — la costringe a rileggere da database, che è il punto — ma la
    fa per *ogni* oggetto della sessione, comprese le camere della fixture
    `rooms`. Il primo accesso successivo a `rooms[1].id` scatena allora un
    caricamento pigro fuori dal contesto asincrono, e il test muore con
    `MissingGreenlet` in un punto che non c'entra nulla con ciò che sta
    verificando.

    `populate_existing=True` ottiene lo stesso risultato restando circoscritto
    a questa query. Il `selectinload` serve perché l'opzione aggiorna le
    colonne ma non ricaricherebbe la collezione.
    """
    result = await session.execute(
        select(Booking)
        .where(Booking.code == code)
        .options(selectinload(Booking.items))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


def _sweeper(session_factory, email_backend) -> BookingExpirationService:
    from src.service.email.email_service import EmailService

    return BookingExpirationService(
        session_factory, email_service=EmailService(backend=email_backend)
    )


# ===========================================================================
# Transizione
# ===========================================================================

async def test_la_prenotazione_scaduta_passa_a_expired(
        api_client, rooms, session, session_factory, email_backend
):
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    await _age_hold(session, code)
    result = await _sweeper(session_factory, email_backend).sweep_once()

    assert result.expired_count == 1

    booking = await _reload(session, code)
    assert booking.status == BookingStatus.EXPIRED


async def test_le_righe_camera_vengono_disattivate(
        api_client, rooms, session, session_factory, email_backend
):
    """
    `is_active` è il predicato dell'exclusion constraint: finché resta vero,
    lo slot è bloccato per il database, qualunque cosa dica lo stato.
    """
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    await _age_hold(session, code)
    await _sweeper(session_factory, email_backend).sweep_once()

    booking = await _reload(session, code)
    righe = await session.execute(
        select(BookingRoomItem).where(BookingRoomItem.booking_id == booking.id)
    )
    assert all(riga.is_active is False for riga in righe.scalars().all())


async def test_lo_slot_torna_davvero_prenotabile(
        api_client, rooms, session, session_factory, email_backend
):
    """
    La verifica che conta: dopo lo sweep, le stesse date sulla stessa camera
    devono poter essere prenotate da qualcun altro.

    Guardare `is_active` proverebbe solo che una colonna è stata scritta;
    questo prova che il vincolo del database la considera libera.
    """
    primo = await _create_pending(api_client, rooms[0].id)
    await _age_hold(session, primo["booking"]["code"])

    await _sweeper(session_factory, email_backend).sweep_once()

    secondo = await _create_pending(
        api_client,
        rooms[0].id,
        guest={**GUEST, "email": "giulia.bianchi@example.com"},
    )
    assert secondo["booking"]["status"] == "PENDING_CONFIRMATION"
    assert secondo["booking"]["code"] != primo["booking"]["code"]


async def test_viene_scritta_la_traccia_storica_con_attore_sistema(
        api_client, rooms, session, session_factory, email_backend
):
    """La transizione automatica deve restare distinguibile da quelle umane."""
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    await _age_hold(session, code)
    await _sweeper(session_factory, email_backend).sweep_once()

    booking = await _reload(session, code)
    righe = await session.execute(
        select(BookingStatusHistory)
        .where(BookingStatusHistory.booking_id == booking.id)
        .order_by(BookingStatusHistory.created_at)
    )
    storia = righe.scalars().all()

    ultima = storia[-1]
    assert ultima.to_status == BookingStatus.EXPIRED
    assert ultima.actor_type.value == "SYSTEM"
    assert ultima.reason


async def test_i_token_vengono_invalidati(
        api_client, rooms, session, session_factory, email_backend
):
    """
    Un link di conferma ancora valido su una prenotazione scaduta darebbe
    all'ospite un errore incomprensibile.
    """
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    await _age_hold(session, code)
    await _sweeper(session_factory, email_backend).sweep_once()

    booking = await _reload(session, code)
    tokens = await session.execute(
        select(BookingToken).where(BookingToken.booking_id == booking.id)
    )
    assert all(token.used_at is not None for token in tokens.scalars().all())

    # E il link non funziona più.
    response = await api_client.post(
        f"{BASE}/confirm", json={"token": created["confirmation_token"]}
    )
    assert response.status_code in (400, 410)


# ===========================================================================
# Quello che NON deve succedere
# ===========================================================================

async def test_una_prenotazione_non_ancora_scaduta_resta_intatta(
        api_client, rooms, session, session_factory, email_backend
):
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    result = await _sweeper(session_factory, email_backend).sweep_once()

    assert result.expired_count == 0
    booking = await _reload(session, code)
    assert booking.status == BookingStatus.PENDING_CONFIRMATION


async def test_una_prenotazione_confermata_resta_intatta(
        api_client, rooms, session, session_factory, email_backend
):
    """
    Una confermata non ha più `hold_expires_at`, ma il filtro che la protegge è
    lo **stato**: lo sweeper guarda solo le prenotazioni in attesa.
    """
    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]

    await api_client.post(f"{BASE}/confirm", json={"token": created["confirmation_token"]})

    # Anche forzando una scadenza nel passato, non deve essere toccata.
    await session.execute(
        update(Booking)
        .where(Booking.code == code)
        .values(hold_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    )
    await session.commit()

    result = await _sweeper(session_factory, email_backend).sweep_once()

    assert result.expired_count == 0
    booking = await _reload(session, code)
    assert booking.status == BookingStatus.CONFIRMED


async def test_la_seconda_passata_non_trova_piu_nulla(
        api_client, rooms, session, session_factory, email_backend
):
    """Idempotenza: rieseguire lo sweeper non deve avere effetti."""
    created = await _create_pending(api_client, rooms[0].id)
    await _age_hold(session, created["booking"]["code"])

    sweeper = _sweeper(session_factory, email_backend)

    prima = await sweeper.sweep_once()
    seconda = await sweeper.sweep_once()

    assert prima.expired_count == 1
    assert seconda.expired_count == 0
    assert seconda.notified_count == 0


# ===========================================================================
# Notifiche
# ===========================================================================

async def test_una_scadenza_recente_avvisa_l_ospite(
        api_client, rooms, session, session_factory, email_backend
):
    created = await _create_pending(api_client, rooms[0].id)
    await _age_hold(session, created["booking"]["code"], minutes_ago=5)

    email_backend.clear()
    result = await _sweeper(session_factory, email_backend).sweep_once()

    assert result.notified_count == 1
    messaggio = email_backend.last
    assert messaggio.to == GUEST["email"]
    assert "scadut" in messaggio.subject.lower()


async def test_una_scadenza_vecchia_sistema_lo_stato_ma_non_avvisa(
        api_client, rooms, session, session_factory, email_backend
):
    """
    Se lo sweeper è rimasto fermo per giorni, al riavvio trova un arretrato.
    Gli stati vanno comunque sistemati, ma spedire email su richieste che
    l'ospite ha dimenticato da un pezzo è solo un modo per farsi segnalare come
    spam.
    """
    created = await _create_pending(api_client, rooms[0].id)
    await _age_hold(session, created["booking"]["code"], minutes_ago=60 * 24 * 3)

    email_backend.clear()
    result = await _sweeper(session_factory, email_backend).sweep_once()

    assert result.expired_count == 1
    assert result.notified_count == 0
    assert email_backend.messages == []

    booking = await _reload(session, created["booking"]["code"])
    assert booking.status == BookingStatus.EXPIRED


async def test_un_canale_email_guasto_non_annulla_lo_sweep(
        api_client, rooms, session, session_factory
):
    """
    Le transizioni sono già committate quando parte la prima email: un
    disservizio SMTP non può riportare indietro il lavoro fatto.
    """
    from src.service.email.backend import FailingEmailBackend
    from src.service.email.email_service import EmailService

    created = await _create_pending(api_client, rooms[0].id)
    code = created["booking"]["code"]
    await _age_hold(session, code)

    sweeper = BookingExpirationService(
        session_factory, email_service=EmailService(backend=FailingEmailBackend())
    )
    result = await sweeper.sweep_once()

    assert result.expired_count == 1
    booking = await _reload(session, code)
    assert booking.status == BookingStatus.EXPIRED


# ===========================================================================
# Endpoint amministrativo
# ===========================================================================

async def test_l_admin_puo_eseguire_lo_sweeper_a_mano(
        admin_client, rooms, session
):
    created = await _create_pending(admin_client, rooms[0].id)
    code = created["booking"]["code"]
    await _age_hold(session, code)

    response = await admin_client.post(f"{ADMIN_BASE}/sweep-expired")

    assert response.status_code == 200, response.text
    corpo = response.json()
    assert corpo["expired_count"] == 1
    assert corpo["swept_at"]

    booking = await _reload(session, code)
    assert booking.status == BookingStatus.EXPIRED


async def test_lo_sweeper_a_mano_e_idempotente(admin_client, rooms, session):
    created = await _create_pending(admin_client, rooms[0].id)
    await _age_hold(session, created["booking"]["code"])

    prima = await admin_client.post(f"{ADMIN_BASE}/sweep-expired")
    seconda = await admin_client.post(f"{ADMIN_BASE}/sweep-expired")

    assert prima.json()["expired_count"] == 1
    assert seconda.json()["expired_count"] == 0


async def test_un_utente_semplice_non_puo_eseguire_lo_sweeper(user_client):
    response = await user_client.post(f"{ADMIN_BASE}/sweep-expired")
    assert response.status_code == 403


async def test_senza_autenticazione_lo_sweeper_e_inaccessibile(api_client):
    response = await api_client.post(f"{ADMIN_BASE}/sweep-expired")
    assert response.status_code == 401


# ===========================================================================
# Scadenza e pulizia dei token
# ===========================================================================

async def test_il_token_di_gestione_scade_all_arrivo(api_client, rooms, session):
    """
    Non alla partenza, come faceva prima.

    Dal check-in in poi non c'è più nulla che quel token consenta di fare:
    `_assert_guest_can_cancel` richiede lo stato `CONFIRMED`, e dalla
    registrazione dell'arrivo la prenotazione è `CHECKED_IN`. Farlo vivere
    fino alla partenza significava tenere in circolazione, dentro un'email,
    una credenziale che non apre più nulla.
    """
    created = await _create_pending(api_client, rooms[0].id)

    confirm = await api_client.post(
        f"{BASE}/confirm", json={"token": created["confirmation_token"]}
    )
    assert confirm.status_code == 200, confirm.text

    booking = await _reload(session, created["booking"]["code"])
    result = await session.execute(
        select(BookingToken).where(
            BookingToken.booking_id == booking.id,
            BookingToken.purpose == BookingTokenPurpose.MANAGE,
        )
    )
    token = result.scalar_one()

    assert token.expires_at.date() == CHECK_IN
    assert token.expires_at.date() != CHECK_OUT


async def test_la_pulizia_non_tocca_i_token_scaduti_da_poco(
        api_client, rooms, session, session_factory, email_backend
):
    """
    La soglia di ritenzione esiste per questo: un token scaduto ieri è
    l'unica traccia che possiamo consultare se l'ospite scrive che il link
    non gli è mai arrivato.
    """
    created = await _create_pending(api_client, rooms[0].id)
    booking = await _reload(session, created["booking"]["code"])

    await _age_tokens(session, booking.id, days=1)
    eliminati = await _sweeper(session_factory, email_backend).purge_tokens_if_due(
        force=True
    )

    assert eliminati == 0
    assert await _count_tokens(session, booking.id) > 0


async def test_la_pulizia_elimina_i_token_scaduti_da_tempo(
        api_client, rooms, session, session_factory, email_backend
):
    created = await _create_pending(api_client, rooms[0].id)
    booking = await _reload(session, created["booking"]["code"])
    prima = await _count_tokens(session, booking.id)
    assert prima > 0

    await _age_tokens(session, booking.id, days=120)
    eliminati = await _sweeper(session_factory, email_backend).purge_tokens_if_due(
        force=True
    )

    assert eliminati >= prima
    assert await _count_tokens(session, booking.id) == 0


async def test_la_pulizia_rispetta_la_cadenza(
        api_client, rooms, session, session_factory, email_backend
):
    """
    Senza il controllo di cadenza la DELETE girerebbe a ogni passata dello
    sweeper — ogni cinque minuti — per liberare quasi sempre zero righe.
    """
    sweeper = _sweeper(session_factory, email_backend)

    primo = await _create_pending(api_client, rooms[0].id)
    booking_uno = await _reload(session, primo["booking"]["code"])
    await _age_tokens(session, booking_uno.id, days=120)

    assert await sweeper.purge_tokens_if_due() > 0

    # Secondo giro a distanza di un istante: c'è di nuovo lavoro da fare, ma
    # non è ancora il turno.
    secondo = await _create_pending(api_client, rooms[1].id)
    booking_due = await _reload(session, secondo["booking"]["code"])
    await _age_tokens(session, booking_due.id, days=120)

    assert await sweeper.purge_tokens_if_due() == 0
    assert await _count_tokens(session, booking_due.id) > 0

    # Con `force` il lavoro arretrato viene comunque smaltito.
    assert await sweeper.purge_tokens_if_due(force=True) > 0
    assert await _count_tokens(session, booking_due.id) == 0
