"""
Calcolo dei prezzi di soggiorno, degli sconti e delle condizioni di
cancellazione.

Isolato dal `BookingService` di proposito: è logica pura, senza database né
sessione, quindi testabile senza alcuna infrastruttura. È anche il punto in
cui un domani entreranno le tariffe stagionali senza toccare la prenotazione.

**Nessun `float` compare in questo modulo.** Tutti gli importi sono `Decimal`
e ogni risultato monetario passa da `to_money`, che quantizza a due decimali
con `ROUND_HALF_UP`. Un solo `float` in mezzo al calcolo produrrebbe totali
che non tornano di un centesimo, e su un preventivo firmato significherebbe
rifiutare prenotazioni legittime al ricalcolo.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config.config import settings
from src.data.enumerators import PaymentOption
from src.data.model.booking import Booking
from src.data.model.room import Room
from src.data.schemas.booking_schema import BookingQuoteResponseSchema, PriceLineSchema
from src.security.quote_token import QuotePayload, create_quote_token, decode_quote_token

_CENTS = Decimal("0.01")
_HUNDRED = Decimal("100")


class PricingService:
    """Motore di calcolo dei prezzi. Senza stato e senza dipendenze."""

    # ------------------------------------------------------------------ #
    # Utilità                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def calculate_nights(check_in: date, check_out: date) -> int:
        """Notti di soggiorno. Il giorno di partenza non si paga."""
        return (check_out - check_in).days

    @staticmethod
    def to_money(value: Decimal) -> Decimal:
        """Arrotonda a due decimali con `ROUND_HALF_UP` (arrotondamento commerciale)."""
        return value.quantize(_CENTS, rounding=ROUND_HALF_UP)

    # ------------------------------------------------------------------ #
    # Calcolo                                                             #
    # ------------------------------------------------------------------ #

    def build_price_lines(self, rooms: Sequence[Room], nights: int) -> List[PriceLineSchema]:
        """
        Compone una riga di preventivo per camera.

        Il prezzo unitario viene letto qui e congelato: da questo momento una
        modifica di `Room.price` non influenza più il soggiorno.
        """
        lines: List[PriceLineSchema] = []

        for room in rooms:
            unit_price = self.to_money(Decimal(room.price))
            lines.append(
                PriceLineSchema(
                    room_id=room.id,
                    room_name=room.name,
                    unit_price=unit_price,
                    nights=nights,
                    line_total=self.to_money(unit_price * nights),
                )
            )

        return lines

    def compute_totals(
            self,
            lines: Sequence[PriceLineSchema],
            payment_option: PaymentOption
    ) -> tuple:
        """
        Calcola imponibile, sconto e totale.

        Lo sconto si applica solo al pagamento online anticipato: è il
        corrispettivo del fatto che l'incasso è certo e immediato.

        :return: tupla `(base_price, discount_amount, total_price)`.
        """
        base_price = self.to_money(sum((line.line_total for line in lines), Decimal("0")))

        discount_amount = Decimal("0.00")
        if payment_option == PaymentOption.PAY_NOW:
            discount_amount = self.to_money(
                base_price * settings.online_payment_discount_percent / _HUNDRED
            )

        total_price = self.to_money(base_price - discount_amount)
        return base_price, discount_amount, total_price

    def build_quote(
            self,
            rooms: Sequence[Room],
            check_in: date,
            check_out: date,
            guest_count: int,
            payment_option: PaymentOption
    ) -> BookingQuoteResponseSchema:
        """
        Produce il preventivo completo, già firmato.

        :param rooms: camere selezionate, già verificate come disponibili.
        """
        nights = self.calculate_nights(check_in, check_out)
        lines = self.build_price_lines(rooms, nights)
        base_price, discount_amount, total_price = self.compute_totals(lines, payment_option)

        payload = QuotePayload(
            check_in=check_in,
            check_out=check_out,
            guest_count=guest_count,
            room_ids=[room.id for room in rooms],
            payment_option=payment_option,
            base_price=base_price,
            discount_amount=discount_amount,
            total_price=total_price,
            currency=settings.default_currency,
        )

        token, expires_at = create_quote_token(payload)

        return BookingQuoteResponseSchema(
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            guest_count=guest_count,
            payment_option=payment_option,
            lines=lines,
            base_price=base_price,
            discount_amount=discount_amount,
            total_price=total_price,
            currency=settings.default_currency,
            quote_token=token,
            quote_expires_at=expires_at,
        )

    @staticmethod
    def verify_quote(token: str) -> QuotePayload:
        """Verifica firma e scadenza del preventivo presentato dal client."""
        return decode_quote_token(token)

    # ------------------------------------------------------------------ #
    # Condizioni di cancellazione                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def compute_cancellation_deadline(
            check_in: date,
            payment_option: PaymentOption
    ) -> Optional[datetime]:
        """
        Termine ultimo per la cancellazione gratuita.

        Solo per `PAY_ON_ARRIVAL`: il pagamento online è non rimborsabile e
        non ha quindi un termine di gratuità.

        Il calcolo parte dalla **mezzanotte del giorno di arrivo nel fuso
        della struttura**, non dall'orario di check-in: è la lettura più
        favorevole all'ospite e la più semplice da comunicare.

        :return: istante in UTC, oppure `None` se non applicabile.
        """
        if payment_option != PaymentOption.PAY_ON_ARRIVAL:
            return None

        local_midnight = datetime.combine(
            check_in, time.min, tzinfo=ZoneInfo(settings.app_timezone)
        )
        deadline = local_midnight - timedelta(hours=settings.booking_free_cancellation_hours)
        return deadline.astimezone(timezone.utc)

    def compute_cancellation_penalty(self, booking: Booking) -> Decimal:
        """
        Penale dovuta in caso di cancellazione, allo stato attuale.

        :return: importo trattenuto. `0.00` se la cancellazione è gratuita.
        """
        if booking.payment_option == PaymentOption.PAY_NOW:
            return self.to_money(
                Decimal(booking.total_price)
                * settings.online_cancellation_penalty_percent
                / _HUNDRED
            )

        deadline = booking.cancellation_deadline
        if deadline is not None and datetime.now(timezone.utc) > deadline:
            # Cancellazione tardiva su tariffa "paga in struttura": l'intero
            # importo è dovuto.
            #
            # TODO [Business]: valutare una penale parziale (es. la prima
            #   notte) invece dell'intero soggiorno. Richiede un nuovo
            #   parametro di configurazione e una modifica ai testi delle
            #   condizioni di prenotazione mostrate all'ospite.
            return self.to_money(Decimal(booking.total_price))

        return Decimal("0.00")
