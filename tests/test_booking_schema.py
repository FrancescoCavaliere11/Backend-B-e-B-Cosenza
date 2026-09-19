"""
Test dei contratti Pydantic del modulo Booking.

Sono test **puri**: nessun database, nessun client HTTP, nessuna fixture
asincrona. Validano esclusivamente le regole dichiarate negli schemi e nei
validator, e girano in frazioni di secondo.

Esecuzione, dalla root del progetto:

    pytest
"""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.config.config import settings
from src.data.enumerators import BookingStatus, PaymentMethod, PaymentOption
from src.data.schemas.booking_schema import (
    AdminBookingCreateSchema,
    AvailabilityRequestSchema,
    BookingLookupSchema,
    BookingQuoteRequestSchema,
    BookingSearchFiltersSchema,
    BookingStatusUpdateSchema,
    GuestBookingCreateSchema,
    GuestDataSchema,
)
from src.security.validators import today_in_app_timezone

TODAY = today_in_app_timezone()
TOMORROW = TODAY + timedelta(days=1)
NEXT_WEEK = TODAY + timedelta(days=7)

VALID_GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}

VALID_QUOTE_TOKEN = "x" * 40


def _quote_payload(**overrides):
    payload = {
        "check_in": TOMORROW,
        "check_out": TOMORROW + timedelta(days=3),
        "guest_count": 2,
        "room_ids": [uuid4()],
        "payment_option": PaymentOption.PAY_ON_ARRIVAL,
    }
    payload.update(overrides)
    return payload


# ===========================================================================
# Intervallo di date
# ===========================================================================

class TestDateRange:

    def test_intervallo_valido(self):
        schema = AvailabilityRequestSchema(
            check_in=TOMORROW, check_out=TOMORROW + timedelta(days=2), guest_count=2
        )
        assert schema.check_out > schema.check_in

    def test_soggiorno_di_zero_notti_rifiutato(self):
        with pytest.raises(ValidationError, match="successiva"):
            AvailabilityRequestSchema(check_in=TOMORROW, check_out=TOMORROW, guest_count=1)

    def test_check_out_precedente_al_check_in_rifiutato(self):
        with pytest.raises(ValidationError, match="successiva"):
            AvailabilityRequestSchema(
                check_in=NEXT_WEEK, check_out=TOMORROW, guest_count=1
            )

    def test_check_in_nel_passato_rifiutato_sui_contratti_pubblici(self):
        with pytest.raises(ValidationError, match="passato"):
            AvailabilityRequestSchema(
                check_in=TODAY - timedelta(days=1), check_out=TOMORROW, guest_count=1
            )

    def test_check_in_oggi_consentito(self):
        """Una prenotazione last-minute per la notte stessa è legittima."""
        schema = AvailabilityRequestSchema(
            check_in=TODAY, check_out=TOMORROW, guest_count=1
        )
        assert schema.check_in == TODAY

    def test_soggiorno_troppo_lungo_rifiutato(self):
        troppo_lungo = TOMORROW + timedelta(days=settings.booking_max_nights + 1)
        with pytest.raises(ValidationError, match="non può superare"):
            AvailabilityRequestSchema(
                check_in=TOMORROW, check_out=troppo_lungo, guest_count=1
            )

    def test_anticipo_eccessivo_rifiutato(self):
        troppo_avanti = TODAY + timedelta(days=settings.booking_max_advance_days + 10)
        with pytest.raises(ValidationError, match="anticipo"):
            AvailabilityRequestSchema(
                check_in=troppo_avanti,
                check_out=troppo_avanti + timedelta(days=2),
                guest_count=1,
            )


# ===========================================================================
# Selezione camere e ospiti
# ===========================================================================

class TestRoomSelection:

    def test_selezione_valida(self):
        schema = BookingQuoteRequestSchema(**_quote_payload())
        assert len(schema.room_ids) == 1

    def test_lista_camere_vuota_rifiutata(self):
        with pytest.raises(ValidationError, match="almeno una camera"):
            BookingQuoteRequestSchema(**_quote_payload(room_ids=[]))

    def test_camere_duplicate_rifiutate(self):
        room_id = uuid4()
        with pytest.raises(ValidationError, match="duplicati"):
            BookingQuoteRequestSchema(**_quote_payload(room_ids=[room_id, room_id]))

    def test_troppe_camere_rifiutate(self):
        troppe = [uuid4() for _ in range(settings.booking_max_rooms_per_booking + 1)]
        with pytest.raises(ValidationError, match="più di"):
            BookingQuoteRequestSchema(**_quote_payload(room_ids=troppe))

    def test_zero_ospiti_rifiutato(self):
        with pytest.raises(ValidationError):
            BookingQuoteRequestSchema(**_quote_payload(guest_count=0))

    def test_nessuno_schema_di_input_accetta_un_prezzo(self):
        """
        Il prezzo non deve mai arrivare dal client: entra solo attraverso il
        `quote_token` firmato ed è comunque ricalcolato lato server.
        """
        schema = BookingQuoteRequestSchema(**_quote_payload())
        assert not hasattr(schema, "total_price")
        assert not hasattr(schema, "price")


# ===========================================================================
# Anagrafica ospite
# ===========================================================================

class TestGuestData:

    def test_anagrafica_valida(self):
        guest = GuestDataSchema(**VALID_GUEST)
        assert guest.email == "mario.rossi@example.com"

    def test_email_non_valida_rifiutata(self):
        with pytest.raises(ValidationError):
            GuestDataSchema(**{**VALID_GUEST, "email": "non-una-email"})

    def test_telefono_non_numerico_rifiutato(self):
        with pytest.raises(ValidationError, match="solo numeri"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "333123456a"})

    def test_telefono_di_lunghezza_errata_rifiutato(self):
        with pytest.raises(ValidationError):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "333"})

    def test_spazi_esterni_nel_nome_rifiutati(self):
        with pytest.raises(ValidationError, match="spazi"):
            GuestDataSchema(**{**VALID_GUEST, "firstname": " Mario"})

    def test_nome_troppo_corto_rifiutato(self):
        with pytest.raises(ValidationError):
            GuestDataSchema(**{**VALID_GUEST, "firstname": "M"})


# ===========================================================================
# Creazione pubblica
# ===========================================================================

class TestGuestBookingCreate:

    def test_creazione_valida(self):
        schema = GuestBookingCreateSchema(
            quote_token=VALID_QUOTE_TOKEN, guest=VALID_GUEST, accept_terms=True
        )
        assert schema.website is None

    def test_condizioni_non_accettate_rifiutate(self):
        with pytest.raises(ValidationError, match="condizioni"):
            GuestBookingCreateSchema(
                quote_token=VALID_QUOTE_TOKEN, guest=VALID_GUEST, accept_terms=False
            )

    def test_honeypot_compilato_rifiutato(self):
        """Un utente reale non vede il campo: se arriva pieno è un bot."""
        with pytest.raises(ValidationError, match="non valida"):
            GuestBookingCreateSchema(
                quote_token=VALID_QUOTE_TOKEN,
                guest=VALID_GUEST,
                accept_terms=True,
                website="http://spam.example",
            )

    def test_quote_token_troppo_corto_rifiutato(self):
        with pytest.raises(ValidationError):
            GuestBookingCreateSchema(
                quote_token="abc", guest=VALID_GUEST, accept_terms=True
            )


# ===========================================================================
# Creazione amministrativa
# ===========================================================================

class TestAdminBookingCreate:

    def _payload(self, **overrides):
        payload = {
            "check_in": TOMORROW,
            "check_out": TOMORROW + timedelta(days=2),
            "guest_count": 2,
            "room_ids": [uuid4()],
            "payment_option": PaymentOption.PAY_ON_ARRIVAL,
            "guest": VALID_GUEST,
        }
        payload.update(overrides)
        return payload

    def test_con_ospite_manuale(self):
        schema = AdminBookingCreateSchema(**self._payload())
        assert schema.user_id is None
        assert schema.skip_email_confirmation is True

    def test_con_utente_registrato(self):
        schema = AdminBookingCreateSchema(
            **self._payload(guest=None, user_id=uuid4())
        )
        assert schema.guest is None

    def test_utente_e_ospite_insieme_rifiutati(self):
        with pytest.raises(ValidationError, match="non entrambi"):
            AdminBookingCreateSchema(**self._payload(user_id=uuid4()))

    def test_ne_utente_ne_ospite_rifiutati(self):
        with pytest.raises(ValidationError, match="necessario indicare"):
            AdminBookingCreateSchema(**self._payload(guest=None))

    def test_data_nel_passato_consentita_allo_staff(self):
        """
        Il back-office deve poter registrare a posteriori un walk-in o
        correggere un inserimento sbagliato. La protezione dal refuso è
        l'avviso di conferma lato frontend.
        """
        ieri = TODAY - timedelta(days=1)
        schema = AdminBookingCreateSchema(
            **self._payload(check_in=ieri, check_out=ieri + timedelta(days=2))
        )
        assert schema.check_in == ieri

    def test_intervallo_incoerente_rifiutato_anche_allo_staff(self):
        with pytest.raises(ValidationError, match="successiva"):
            AdminBookingCreateSchema(
                **self._payload(check_in=NEXT_WEEK, check_out=TOMORROW)
            )

    def test_metodo_di_pagamento_manuale_accettato(self):
        schema = AdminBookingCreateSchema(
            **self._payload(payment_method=PaymentMethod.CASH_ON_SITE, mark_as_paid=True)
        )
        assert schema.payment_method == PaymentMethod.CASH_ON_SITE


# ===========================================================================
# Azioni e filtri
# ===========================================================================

class TestActions:

    def test_annullamento_senza_motivazione_rifiutato(self):
        with pytest.raises(ValidationError, match="motivazione"):
            BookingStatusUpdateSchema(new_status=BookingStatus.CANCELLED)

    def test_annullamento_con_motivazione_vuota_rifiutato(self):
        with pytest.raises(ValidationError, match="motivazione"):
            BookingStatusUpdateSchema(new_status=BookingStatus.CANCELLED, reason="   ")

    def test_annullamento_con_motivazione_accettato(self):
        schema = BookingStatusUpdateSchema(
            new_status=BookingStatus.CANCELLED, reason="Richiesta telefonica dell'ospite"
        )
        assert schema.reason

    def test_altre_transizioni_non_richiedono_motivazione(self):
        schema = BookingStatusUpdateSchema(new_status=BookingStatus.CHECKED_IN)
        assert schema.reason is None

    def test_codice_prenotazione_normalizzato(self):
        schema = BookingLookupSchema(code="  bb-2026-000123 ", email="a@example.com")
        assert schema.code == "BB-2026-000123"


class TestSearchFilters:

    def test_valori_predefiniti(self):
        filters = BookingSearchFiltersSchema()
        assert filters.page == 1
        assert filters.offset == 0

    def test_offset_calcolato(self):
        filters = BookingSearchFiltersSchema(page=3, page_size=25)
        assert filters.offset == 50

    def test_intervallo_invertito_rifiutato(self):
        with pytest.raises(ValidationError, match="precedere"):
            BookingSearchFiltersSchema(date_from=NEXT_WEEK, date_to=TODAY)

    def test_page_size_oltre_il_limite_rifiutata(self):
        with pytest.raises(ValidationError):
            BookingSearchFiltersSchema(page_size=500)
