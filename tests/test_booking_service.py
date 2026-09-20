"""
Test del motore di prenotazione su database reale.

Il test che conta è `test_due_prenotazioni_concorrenti_una_sola_vince`: è il
criterio di completamento dello Step C e la rete di sicurezza permanente
sull'anti-overbooking. Gli altri verificano il ciclo di vita e le transizioni.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.enumerators import BookingStatus, PaymentOption
from src.data.model.booking import Booking
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.booking_status_history_repository import BookingStatusHistoryRepository
from src.data.repository.booking_token_repository import BookingTokenRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import GuestBookingCreateSchema
from src.exception.custom_exception import (
    BookingHoldExpired,
    InvalidBookingStatusTransition,
    InvalidQuoteToken,
    RoomNotAvailable,
)
from src.security.quote_token import QuotePayload, create_quote_token
from src.service.booking_service import BookingService
from src.service.pricing_service import PricingService

CHECK_IN = date.today() + timedelta(days=30)
CHECK_OUT = CHECK_IN + timedelta(days=2)
NIGHTS = 2

GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}


def build_service(session: AsyncSession) -> BookingService:
    return BookingService(
        session=session,
        booking_repository=BookingRepository(session),
        booking_token_repository=BookingTokenRepository(session),
        booking_status_history_repository=BookingStatusHistoryRepository(session),
        room_repository=RoomRepository(session),
        pricing_service=PricingService(),
    )


def build_quote_token(
        room_ids: List[UUID],
        unit_prices: List[Decimal],
        guest_count: int = 2,
        payment_option: PaymentOption = PaymentOption.PAY_ON_ARRIVAL,
        check_in: date = CHECK_IN,
        check_out: date = CHECK_OUT
) -> str:
    nights = (check_out - check_in).days
    base = sum((price * nights for price in unit_prices), Decimal("0.00")).quantize(
        Decimal("0.01")
    )
    discount = Decimal("0.00")
    if payment_option == PaymentOption.PAY_NOW:
        from src.config.config import settings
        discount = (base * settings.online_payment_discount_percent / Decimal("100")).quantize(
            Decimal("0.01")
        )

    token, _ = create_quote_token(
        QuotePayload(
            check_in=check_in,
            check_out=check_out,
            guest_count=guest_count,
            room_ids=list(room_ids),
            payment_option=payment_option,
            base_price=base,
            discount_amount=discount,
            total_price=base - discount,
            currency="EUR",
        )
    )
    return token


def guest_payload(token: str, email: Optional[str] = None) -> GuestBookingCreateSchema:
    guest = dict(GUEST)
    if email:
        guest["email"] = email
    return GuestBookingCreateSchema(quote_token=token, guest=guest, accept_terms=True)


# ===========================================================================
# Il test che certifica lo Step C
# ===========================================================================

@pytest.mark.parametrize("iterazione", range(10))
async def test_due_prenotazioni_concorrenti_una_sola_vince(
        session_factory, rooms, iterazione
):
    """
    Due richieste simultanee sulla stessa camera e stesse date.

    Deve passarne **esattamente una**. Ripetuto dieci volte perché una race
    condition che si manifesta una volta su dieci è comunque una race
    condition: un singolo tentativo andato bene non dimostra nulla.
    """
    room = rooms[0]
    token = build_quote_token([room.id], [Decimal(room.price)])

    async def tenta(email: str):
        async with session_factory() as session:
            service = build_service(session)
            return await service.create_guest_booking(guest_payload(token, email))

    esiti = await asyncio.gather(
        tenta("primo@example.com"),
        tenta("secondo@example.com"),
        return_exceptions=True,
    )

    successi = [e for e in esiti if not isinstance(e, Exception)]
    fallimenti = [e for e in esiti if isinstance(e, Exception)]

    assert len(successi) == 1, f"Attesa una sola prenotazione, ottenute {len(successi)}"
    assert len(fallimenti) == 1
    assert isinstance(fallimenti[0], RoomNotAvailable), (
        f"Atteso RoomNotAvailable, ottenuto {type(fallimenti[0]).__name__}: {fallimenti[0]}"
    )


# ===========================================================================
# Creazione
# ===========================================================================

async def test_creazione_guest_produce_prenotazione_in_attesa(session, rooms):
    service = build_service(session)
    room = rooms[0]

    result = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )

    assert result.booking.status == BookingStatus.PENDING_CONFIRMATION
    assert result.booking.hold_expires_at is not None
    assert result.confirmation_token, "Il token in chiaro deve tornare al chiamante"
    assert result.booking.total_price == Decimal(room.price) * NIGHTS
    assert result.booking.code.startswith("BB-")
    assert len(result.booking.rooms) == 1


async def test_pagamento_online_salta_la_conferma_email(session, rooms):
    """Il possesso della carta è già una verifica d'identità: niente token."""
    service = build_service(session)
    room = rooms[0]

    result = await service.create_guest_booking(
        guest_payload(
            build_quote_token(
                [room.id], [Decimal(room.price)], payment_option=PaymentOption.PAY_NOW
            )
        )
    )

    assert result.booking.status == BookingStatus.PENDING_PAYMENT
    assert result.confirmation_token is None
    assert result.booking.discount_amount > Decimal("0.00")


async def test_prezzo_manomesso_rifiutato(session, rooms):
    """Un preventivo firmato ma non più coerente col listino non passa."""
    service = build_service(session)
    room = rooms[0]

    token = build_quote_token([room.id], [Decimal("1.00")])  # prezzo inventato

    with pytest.raises(InvalidQuoteToken):
        await service.create_guest_booking(guest_payload(token))


async def test_slot_occupato_rifiutato(session, rooms):
    service = build_service(session)
    room = rooms[0]
    token = build_quote_token([room.id], [Decimal(room.price)])

    await service.create_guest_booking(guest_payload(token, "primo@example.com"))

    with pytest.raises(RoomNotAvailable):
        await service.create_guest_booking(guest_payload(token, "secondo@example.com"))


async def test_prenotazioni_consecutive_consentite(session, rooms):
    """Check-out e check-in nello stesso giorno non collidono."""
    service = build_service(session)
    room = rooms[0]

    await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]), "a@example.com")
    )

    successivo = build_quote_token(
        [room.id],
        [Decimal(room.price)],
        check_in=CHECK_OUT,
        check_out=CHECK_OUT + timedelta(days=2),
    )
    result = await service.create_guest_booking(guest_payload(successivo, "b@example.com"))

    assert result.booking.status == BookingStatus.PENDING_CONFIRMATION


async def test_slot_liberato_da_hold_scaduto(session, rooms):
    """
    Senza far girare lo sweeper: la just-in-time expiration deve bastare a
    rendere di nuovo prenotabile uno slot con blocco scaduto.
    """
    service = build_service(session)
    room = rooms[0]
    token = build_quote_token([room.id], [Decimal(room.price)])

    primo = await service.create_guest_booking(guest_payload(token, "a@example.com"))

    booking = await BookingRepository(session).get_by_code(primo.booking.code)
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    secondo = await service.create_guest_booking(guest_payload(token, "b@example.com"))
    assert secondo.booking.status == BookingStatus.PENDING_CONFIRMATION


# ===========================================================================
# Ciclo di vita
# ===========================================================================

async def test_conferma_completa_il_ciclo(session, rooms):
    service = build_service(session)
    room = rooms[0]

    creata = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )

    confermata = await service.confirm_booking(creata.confirmation_token)

    assert confermata.status == BookingStatus.CONFIRMED
    assert confermata.hold_expires_at is None
    assert confermata.confirmed_at is not None
    assert confermata.cancellation_deadline is not None


async def test_doppia_conferma_segnalata(session, rooms):
    service = build_service(session)
    room = rooms[0]

    creata = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )
    await service.confirm_booking(creata.confirmation_token)

    with pytest.raises(InvalidBookingStatusTransition, match="già stata confermata"):
        await service.confirm_booking(creata.confirmation_token)


async def test_conferma_dopo_la_scadenza_rifiutata(session, rooms):
    service = build_service(session)
    room = rooms[0]

    creata = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )

    booking = await BookingRepository(session).get_by_code(creata.booking.code)
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    with pytest.raises(BookingHoldExpired):
        await service.confirm_booking(creata.confirmation_token)

    # Lo stato resta PENDING_CONFIRMATION: la transizione a EXPIRED appartiene
    # allo sweeper (Step F). Marcarla qui sarebbe inutile, perché l'eccezione
    # fa comunque rollback della transazione. Ciò che conta è che lo slot sia
    # già tornato prenotabile, verificato dal test successivo.
    scaduta = await BookingRepository(session).get_by_code(creata.booking.code)
    assert scaduta.status == BookingStatus.PENDING_CONFIRMATION
    assert scaduta.hold_expires_at < datetime.now(timezone.utc)


async def test_slot_liberato_dopo_la_cancellazione(session, rooms):
    from uuid import uuid4

    from src.data.schemas.booking_schema import BookingStatusUpdateSchema

    service = build_service(session)
    room = rooms[0]
    token = build_quote_token([room.id], [Decimal(room.price)])

    creata = await service.create_guest_booking(guest_payload(token, "a@example.com"))
    booking = await BookingRepository(session).get_by_code(creata.booking.code)

    await service.admin_change_status(
        booking.id,
        BookingStatusUpdateSchema(
            new_status=BookingStatus.CANCELLED, reason="Annullata su richiesta"
        ),
        admin_id=uuid4(),
    )

    # Lo slot è tornato libero: una nuova prenotazione passa.
    nuova = await service.create_guest_booking(guest_payload(token, "b@example.com"))
    assert nuova.booking.status == BookingStatus.PENDING_CONFIRMATION


async def test_transizione_illegale_rifiutata(session, rooms):
    from uuid import uuid4

    from src.data.schemas.booking_schema import BookingStatusUpdateSchema

    service = build_service(session)
    room = rooms[0]

    creata = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )
    booking = await BookingRepository(session).get_by_code(creata.booking.code)

    await service.admin_change_status(
        booking.id,
        BookingStatusUpdateSchema(new_status=BookingStatus.CANCELLED, reason="Test"),
        admin_id=uuid4(),
    )

    # CANCELLED è terminale: non si torna indietro.
    with pytest.raises(InvalidBookingStatusTransition):
        await service.admin_change_status(
            booking.id,
            BookingStatusUpdateSchema(new_status=BookingStatus.CONFIRMED),
            admin_id=uuid4(),
        )


async def test_storico_registra_ogni_transizione(session, rooms):
    service = build_service(session)
    room = rooms[0]

    creata = await service.create_guest_booking(
        guest_payload(build_quote_token([room.id], [Decimal(room.price)]))
    )
    await service.confirm_booking(creata.confirmation_token)

    booking = await BookingRepository(session).get_by_code(creata.booking.code)
    storia = await BookingStatusHistoryRepository(session).get_by_booking_id(booking.id)

    assert len(storia) == 2
    assert storia[0].from_status is None
    assert storia[0].to_status == BookingStatus.PENDING_CONFIRMATION
    assert storia[1].to_status == BookingStatus.CONFIRMED


async def test_ospiti_oltre_la_capienza_rifiutati(session, rooms):
    from src.exception.custom_exception import InvalidGuestCount

    service = build_service(session)
    doppia = rooms[0]  # capienza 2

    token = build_quote_token([doppia.id], [Decimal(doppia.price)], guest_count=6)

    with pytest.raises(InvalidGuestCount):
        await service.create_guest_booking(guest_payload(token))
