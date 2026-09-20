"""
Test del motore di calcolo dei prezzi.

Logica pura: nessun database, nessuna sessione. Le entità `Room` vengono
istanziate direttamente, senza persisterle.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from src.config.config import settings
from src.data.enumerators import PaymentOption
from src.data.model.booking import Booking
from src.data.model.room import Room
from src.exception.custom_exception import InvalidQuoteToken
from src.security.quote_token import QuotePayload, create_quote_token, decode_quote_token
from src.service.pricing_service import PricingService

CHECK_IN = date.today() + timedelta(days=10)
CHECK_OUT = CHECK_IN + timedelta(days=3)


def _room(price: str, name: str = "Camera Test") -> Room:
    return Room(
        id=uuid4(),
        name=name,
        capacity=2,
        price=Decimal(price),
        number=101,
        enabled=True,
        img_url="https://placeholder.test/x.jpg",
    )


@pytest.fixture
def pricing() -> PricingService:
    return PricingService()


class TestCalculations:

    def test_notti_calcolate_sul_giorno_di_partenza_escluso(self, pricing):
        assert pricing.calculate_nights(CHECK_IN, CHECK_OUT) == 3

    def test_riga_prezzo_congela_il_listino(self, pricing):
        room = _room("100.00")
        lines = pricing.build_price_lines([room], nights=3)

        assert len(lines) == 1
        assert lines[0].unit_price == Decimal("100.00")
        assert lines[0].line_total == Decimal("300.00")

        # Il listino cambia dopo il calcolo: la riga già prodotta non si muove.
        room.price = Decimal("999.00")
        assert lines[0].unit_price == Decimal("100.00")

    def test_pagamento_in_struttura_senza_sconto(self, pricing):
        lines = pricing.build_price_lines([_room("100.00")], nights=3)
        base, discount, total = pricing.compute_totals(lines, PaymentOption.PAY_ON_ARRIVAL)

        assert base == Decimal("300.00")
        assert discount == Decimal("0.00")
        assert total == Decimal("300.00")

    def test_pagamento_online_scontato(self, pricing):
        lines = pricing.build_price_lines([_room("100.00")], nights=3)
        base, discount, total = pricing.compute_totals(lines, PaymentOption.PAY_NOW)

        atteso = (base * settings.online_payment_discount_percent / Decimal("100")).quantize(
            Decimal("0.01")
        )
        assert discount == atteso
        assert total == base - discount

    def test_piu_camere_sommate(self, pricing):
        lines = pricing.build_price_lines(
            [_room("100.00", "A"), _room("140.00", "B")], nights=2
        )
        base, _, _ = pricing.compute_totals(lines, PaymentOption.PAY_ON_ARRIVAL)
        assert base == Decimal("480.00")

    def test_arrotondamento_a_due_decimali(self, pricing):
        """Un prezzo con parte decimale non deve generare frazioni di centesimo."""
        lines = pricing.build_price_lines([_room("33.33")], nights=3)
        base, discount, total = pricing.compute_totals(lines, PaymentOption.PAY_NOW)

        assert base == Decimal("99.99")
        assert total == (base - discount)
        for importo in (base, discount, total):
            assert importo.as_tuple().exponent == -2

    def test_nessun_float_nei_totali(self, pricing):
        lines = pricing.build_price_lines([_room("89.90")], nights=7)
        base, discount, total = pricing.compute_totals(lines, PaymentOption.PAY_NOW)

        for importo in (base, discount, total):
            assert isinstance(importo, Decimal)


class TestQuoteToken:

    def _payload(self, **overrides) -> QuotePayload:
        data = dict(
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            guest_count=2,
            room_ids=[uuid4()],
            payment_option=PaymentOption.PAY_ON_ARRIVAL,
            base_price=Decimal("300.00"),
            discount_amount=Decimal("0.00"),
            total_price=Decimal("300.00"),
            currency="EUR",
        )
        data.update(overrides)
        return QuotePayload(**data)

    def test_andata_e_ritorno(self):
        payload = self._payload()
        token, expires_at = create_quote_token(payload)

        decoded = decode_quote_token(token)

        assert decoded.check_in == payload.check_in
        assert decoded.total_price == payload.total_price
        assert decoded.room_ids == payload.room_ids
        assert expires_at > datetime.now(timezone.utc)

    def test_importi_conservano_la_precisione(self):
        payload = self._payload(total_price=Decimal("299.97"))
        token, _ = create_quote_token(payload)

        assert decode_quote_token(token).total_price == Decimal("299.97")

    def test_firma_manomessa_rifiutata(self):
        token, _ = create_quote_token(self._payload())
        manomesso = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")

        with pytest.raises(InvalidQuoteToken):
            decode_quote_token(manomesso)

    def test_token_vuoto_rifiutato(self):
        with pytest.raises(InvalidQuoteToken):
            decode_quote_token("")

    def test_token_di_altro_tipo_rifiutato(self):
        """Un access token di autenticazione non deve valere come preventivo."""
        import jwt

        estraneo = jwt.encode(
            {"type": "access", "sub": str(uuid4())},
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(InvalidQuoteToken):
            decode_quote_token(estraneo)


class TestCancellationPolicy:

    def test_nessun_termine_per_il_pagamento_online(self, pricing):
        assert pricing.compute_cancellation_deadline(CHECK_IN, PaymentOption.PAY_NOW) is None

    def test_termine_calcolato_per_il_pagamento_in_struttura(self, pricing):
        deadline = pricing.compute_cancellation_deadline(
            CHECK_IN, PaymentOption.PAY_ON_ARRIVAL
        )

        assert deadline is not None
        assert deadline.tzinfo is not None
        # Precede l'arrivo di almeno le ore di gratuità previste.
        assert deadline.date() < CHECK_IN

    def test_penale_totale_sul_pagamento_online(self, pricing):
        booking = Booking(
            payment_option=PaymentOption.PAY_NOW,
            total_price=Decimal("300.00"),
            cancellation_deadline=None,
        )
        atteso = (
            Decimal("300.00") * settings.online_cancellation_penalty_percent / Decimal("100")
        ).quantize(Decimal("0.01"))

        assert pricing.compute_cancellation_penalty(booking) == atteso

    def test_nessuna_penale_entro_il_termine(self, pricing):
        booking = Booking(
            payment_option=PaymentOption.PAY_ON_ARRIVAL,
            total_price=Decimal("300.00"),
            cancellation_deadline=datetime.now(timezone.utc) + timedelta(days=5),
        )
        assert pricing.compute_cancellation_penalty(booking) == Decimal("0.00")

    def test_penale_piena_oltre_il_termine(self, pricing):
        booking = Booking(
            payment_option=PaymentOption.PAY_ON_ARRIVAL,
            total_price=Decimal("300.00"),
            cancellation_deadline=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert pricing.compute_cancellation_penalty(booking) == Decimal("300.00")
