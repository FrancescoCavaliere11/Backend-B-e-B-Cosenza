"""
Test dei contratti Pydantic del modulo Booking.

Sono test **puri**: nessun database, nessun client HTTP, nessuna fixture
asincrona. Validano esclusivamente le regole dichiarate negli schemi e nei
validator, e girano in frazioni di secondo.

Esecuzione, dalla root del progetto:

    pytest
"""
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.config.config import settings
from src.data.enumerators import (
    BookingSortOrder,
    BookingStatus,
    PaymentMethod,
    PaymentOption,
    PaymentStatus,
)
from src.data.schemas.booking_schema import (
    AdminBookingCreateSchema,
    AdminPaymentRegistrationSchema,
    AvailabilityRequestSchema,
    BookingLookupSchema,
    BookingQuoteRequestSchema,
    BookingSearchFiltersSchema,
    BookingStatusUpdateSchema,
    GuestBookingCreateSchema,
    GuestDataSchema,
    OccupancyRequestSchema,
)
from src.security.validators import today_in_app_timezone

# Funzioni e non costanti: calcolate al momento del test. Una costante
# calcolata al caricamento del modulo diventava «ieri» se la suite passava la
# mezzanotte mentre girava, e i test sulle date fallivano.


def today() -> date:
    return today_in_app_timezone()


def tomorrow() -> date:
    return today() + timedelta(days=1)


def next_week() -> date:
    return today() + timedelta(days=7)

VALID_GUEST = {
    "firstname": "Mario",
    "lastname": "Rossi",
    "email": "mario.rossi@example.com",
    "phone_number": "3331234567",
}

VALID_QUOTE_TOKEN = "x" * 40


def _quote_payload(**overrides):
    payload = {
        "check_in": tomorrow(),
        "check_out": tomorrow() + timedelta(days=3),
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
            check_in=tomorrow(), check_out=tomorrow() + timedelta(days=2), guest_count=2
        )
        assert schema.check_out > schema.check_in

    def test_soggiorno_di_zero_notti_rifiutato(self):
        with pytest.raises(ValidationError, match="successiva"):
            AvailabilityRequestSchema(check_in=tomorrow(), check_out=tomorrow(), guest_count=1)

    def test_check_out_precedente_al_check_in_rifiutato(self):
        with pytest.raises(ValidationError, match="successiva"):
            AvailabilityRequestSchema(
                check_in=next_week(), check_out=tomorrow(), guest_count=1
            )

    def test_check_in_nel_passato_rifiutato_sui_contratti_pubblici(self):
        with pytest.raises(ValidationError, match="passato"):
            AvailabilityRequestSchema(
                check_in=today() - timedelta(days=1), check_out=tomorrow(), guest_count=1
            )

    def test_check_in_oggi_consentito(self):
        """Una prenotazione last-minute per la notte stessa è legittima."""
        schema = AvailabilityRequestSchema(
            check_in=today(), check_out=tomorrow(), guest_count=1
        )
        assert schema.check_in == today()

    def test_soggiorno_troppo_lungo_rifiutato(self):
        troppo_lungo = tomorrow() + timedelta(days=settings.booking_max_nights + 1)
        with pytest.raises(ValidationError, match="non può superare"):
            AvailabilityRequestSchema(
                check_in=tomorrow(), check_out=troppo_lungo, guest_count=1
            )

    def test_anticipo_eccessivo_rifiutato(self):
        troppo_avanti = today() + timedelta(days=settings.booking_max_advance_days + 10)
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
# Calendario: finestra di occupazione
# ===========================================================================

def _occupancy_payload(**overrides) -> dict:
    payload = {
        "room_ids": [uuid4()],
        "date_from": today(),
        "date_to": today() + timedelta(days=61),
    }
    payload.update(overrides)
    return payload


class TestOccupancyRequest:

    def test_finestra_valida(self):
        schema = OccupancyRequestSchema(**_occupancy_payload())
        assert schema.date_from == today()

    def test_finestra_della_massima_ampiezza_ammessa(self):
        massima = settings.occupancy_max_window_days
        OccupancyRequestSchema(**_occupancy_payload(date_to=today() + timedelta(days=massima)))

    def test_finestra_troppo_ampia_rifiutata(self):
        oltre = settings.occupancy_max_window_days + 1
        with pytest.raises(ValidationError, match="non può superare"):
            OccupancyRequestSchema(**_occupancy_payload(date_to=today() + timedelta(days=oltre)))

    @pytest.mark.parametrize("giorni", [0, -1])
    def test_data_finale_non_successiva_rifiutata(self, giorni):
        with pytest.raises(ValidationError, match="successiva"):
            OccupancyRequestSchema(**_occupancy_payload(date_to=today() + timedelta(days=giorni)))

    def test_finestra_che_parte_nel_passato_rifiutata(self):
        with pytest.raises(ValidationError, match="passato"):
            OccupancyRequestSchema(**_occupancy_payload(date_from=today() - timedelta(days=1)))

    def test_finestra_oltre_il_limite_di_prenotazione_rifiutata(self):
        limite = settings.booking_max_advance_days + settings.booking_max_nights
        date_to = today() + timedelta(days=limite + 1)
        with pytest.raises(ValidationError, match="limite di prenotazione"):
            OccupancyRequestSchema(
                **_occupancy_payload(date_from=date_to - timedelta(days=30), date_to=date_to)
            )

    def test_nessuna_camera_rifiutata(self):
        with pytest.raises(ValidationError, match="almeno una camera"):
            OccupancyRequestSchema(**_occupancy_payload(room_ids=[]))

    def test_camere_duplicate_rifiutate(self):
        room_id = uuid4()
        with pytest.raises(ValidationError, match="duplicati"):
            OccupancyRequestSchema(**_occupancy_payload(room_ids=[room_id, room_id]))

    def test_troppe_camere_rifiutate(self):
        troppe = [uuid4() for _ in range(settings.booking_max_rooms_per_booking + 1)]
        with pytest.raises(ValidationError, match="più di"):
            OccupancyRequestSchema(**_occupancy_payload(room_ids=troppe))


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
        with pytest.raises(ValidationError, match="solo cifre"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "333123456a"})

    def test_telefono_troppo_corto_rifiutato(self):
        with pytest.raises(ValidationError):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "333"})


class TestInternationalPhone:
    """Telefono dell'ospite: solo cellulari, regole per Paese, salvato in E.164."""

    @pytest.mark.parametrize("raw, expected", [
        ("3331234567", "+393331234567"),           # senza prefisso: Italia
        ("+39 333 123 4567", "+393331234567"),
        ("0039 333-123-4567", "+393331234567"),    # 00 al posto di +
        (" +39 312 345 6789 ", "+393123456789"),   # spazi ai bordi tollerati
        ("+33 6 12 34 56 78", "+33612345678"),     # Francia: 9 cifre
        ("+49 1512 3456789", "+4915123456789"),    # Germania: 11 cifre
        ("+34 612 34 56 78", "+34612345678"),      # Spagna: 9 cifre
        ("+41 78 123 45 67", "+41781234567"),      # Svizzera
        ("+43 664 123456", "+43664123456"),        # Austria
        ("+31 6 12345678", "+31612345678"),        # Paesi Bassi
        ("+32 470 12 34 56", "+32470123456"),      # Belgio
        ("+44 7400 123456", "+447400123456"),      # Regno Unito
        ("+1 (201) 555-0123", "+12015550123"),     # Stati Uniti: «fisso o cellulare», accettato
        ("+351 912 345 678", "+351912345678"),     # «Altro prefisso»: Portogallo
    ])
    def test_cellulari_validi_normalizzati(self, raw, expected):
        assert GuestDataSchema(**{**VALID_GUEST, "phone_number": raw}).phone_number == expected

    @pytest.mark.parametrize("raw", [
        "0984 123456",            # Italia
        "+39 0984 1234567",
        "+33 1 23 45 67 89",      # Francia
        "+49 30 123456",          # Germania
        "+34 810 12 34 56",       # Spagna
        "+41 44 668 18 00",       # Svizzera
        "+31 10 123 4567",        # Paesi Bassi
        "+32 12 34 56 78",        # Belgio
        "+44 121 234 5678",       # Regno Unito
    ])
    def test_i_fissi_sono_rifiutati(self, raw):
        with pytest.raises(ValidationError, match="cellulare"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": raw})

    @pytest.mark.parametrize("raw", [
        "+39 333 12a 4567",       # lettere
        "+0 333 1234567",         # il prefisso non inizia con 0
        "+1 234",                 # troppo corto
        "+39 1234 5678 9012 345", # troppo lungo
        "++39 3331234567",        # «+» ripetuto
        "333+1234567",            # «+» in mezzo
        "+33 6 12 34 56 7",       # Francia: una cifra in meno
        "+999 123456789",         # prefisso inesistente
    ])
    def test_numeri_non_validi_rifiutati(self, raw):
        with pytest.raises(ValidationError):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": raw})

    def test_il_messaggio_indica_prefisso_ed_esempio(self):
        with pytest.raises(ValidationError, match=r"prefisso \+33\. Esempio: \+33 6"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "+33 6 12 34 56 7"})

    def test_per_un_fisso_il_messaggio_chiede_un_cellulare(self):
        with pytest.raises(ValidationError, match="Serve un numero di cellulare"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "0984 123456"})

    def test_il_messaggio_d_errore_e_in_italiano(self):
        with pytest.raises(ValidationError, match="prefisso internazionale"):
            GuestDataSchema(**{**VALID_GUEST, "phone_number": "+0 333 1234567"})

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
            "check_in": tomorrow(),
            "check_out": tomorrow() + timedelta(days=2),
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
        ieri = today() - timedelta(days=1)
        schema = AdminBookingCreateSchema(
            **self._payload(check_in=ieri, check_out=ieri + timedelta(days=2))
        )
        assert schema.check_in == ieri

    def test_intervallo_incoerente_rifiutato_anche_allo_staff(self):
        with pytest.raises(ValidationError, match="successiva"):
            AdminBookingCreateSchema(
                **self._payload(check_in=next_week(), check_out=tomorrow())
            )

    def test_metodo_di_pagamento_manuale_accettato(self):
        schema = AdminBookingCreateSchema(
            **self._payload(payment_method=PaymentMethod.CASH_ON_SITE, mark_as_paid=True)
        )
        assert schema.payment_method == PaymentMethod.CASH_ON_SITE

    # --- Coerenza delle opzioni di pagamento (28/09/2026) -------------------

    def test_carta_online_rifiutata(self):
        with pytest.raises(ValidationError, match="carta online"):
            AdminBookingCreateSchema(
                **self._payload(payment_method=PaymentMethod.STRIPE_CARD, mark_as_paid=True)
            )

    def test_metodo_senza_incasso_rifiutato(self):
        """Il metodo descrive come l'ospite ha pagato, non come pagherà."""
        with pytest.raises(ValidationError, match="registrando l'incasso"):
            AdminBookingCreateSchema(**self._payload(payment_method=PaymentMethod.POS_ON_SITE))

    def test_incasso_senza_metodo_rifiutato(self):
        with pytest.raises(ValidationError, match="metodo"):
            AdminBookingCreateSchema(**self._payload(mark_as_paid=True))

    def test_pagata_in_attesa_di_conferma_rifiutata(self):
        """Se scadesse, l'incasso resterebbe su una prenotazione EXPIRED."""
        with pytest.raises(ValidationError, match="attesa di conferma"):
            AdminBookingCreateSchema(
                **self._payload(
                    payment_method=PaymentMethod.CASH_ON_SITE,
                    mark_as_paid=True,
                    skip_email_confirmation=False,
                )
            )

    def test_pagamento_anticipato_senza_incasso_rifiutato(self):
        """
        Nata confermata, non potrebbe più essere pagata online: resterebbe
        scontata del 10% e mai saldata.
        """
        with pytest.raises(ValidationError, match="anticipato"):
            AdminBookingCreateSchema(**self._payload(payment_option=PaymentOption.PAY_NOW))

    def test_pagamento_anticipato_incassato_accettato(self):
        schema = AdminBookingCreateSchema(
            **self._payload(
                payment_option=PaymentOption.PAY_NOW,
                payment_method=PaymentMethod.BANK_TRANSFER,
                mark_as_paid=True,
            )
        )
        assert schema.mark_as_paid is True

    def test_in_struttura_senza_incasso_accettato(self):
        schema = AdminBookingCreateSchema(**self._payload(skip_email_confirmation=False))
        assert schema.payment_method is None


class TestAdminPaymentRegistration:
    """
    Regole che non dipendono dallo stato della prenotazione. Quelle che ne
    dipendono sono nel Service e si verificano in `test_admin_booking_api.py`.
    """

    def test_incasso_manuale_accettato(self):
        schema = AdminPaymentRegistrationSchema(
            payment_status=PaymentStatus.PAID, payment_method=PaymentMethod.CASH_ON_SITE
        )
        assert schema.payment_method == PaymentMethod.CASH_ON_SITE

    def test_importo_rifiutato(self):
        """
        Il campo esisteva ed era scartato in silenzio: il modello non ha dove
        salvarlo (debito #21). Ora chi lo invia riceve un errore esplicito.
        """
        with pytest.raises(ValidationError, match="amount"):
            AdminPaymentRegistrationSchema(
                payment_status=PaymentStatus.PAID,
                payment_method=PaymentMethod.CASH_ON_SITE,
                amount=Decimal("180.00"),
            )

    def test_carta_online_rifiutata(self):
        with pytest.raises(ValidationError, match="carta online"):
            AdminPaymentRegistrationSchema(
                payment_status=PaymentStatus.PAID, payment_method=PaymentMethod.STRIPE_CARD
            )

    @pytest.mark.parametrize(
        "stato",
        [
            PaymentStatus.AUTHORIZED,
            PaymentStatus.FAILED,
            PaymentStatus.NOT_REQUIRED,
            PaymentStatus.PARTIALLY_REFUNDED,
        ],
    )
    def test_stati_non_manuali_rifiutati(self, stato):
        with pytest.raises(ValidationError, match="incasso, un rimborso o una correzione"):
            AdminPaymentRegistrationSchema(
                payment_status=stato, payment_method=PaymentMethod.CASH_ON_SITE
            )

    def test_incasso_senza_metodo_rifiutato(self):
        with pytest.raises(ValidationError, match="metodo"):
            AdminPaymentRegistrationSchema(payment_status=PaymentStatus.PAID)

    @pytest.mark.parametrize("stato", [PaymentStatus.REFUNDED, PaymentStatus.PENDING])
    def test_rimborso_e_correzione_senza_metodo_accettati(self, stato):
        schema = AdminPaymentRegistrationSchema(payment_status=stato, reason="Restituito in contanti")
        assert schema.payment_method is None

    @pytest.mark.parametrize("stato", [PaymentStatus.REFUNDED, PaymentStatus.PENDING])
    @pytest.mark.parametrize("motivazione", [None, "", "   "])
    def test_rimborso_e_correzione_senza_motivazione_rifiutati(self, stato, motivazione):
        with pytest.raises(ValidationError, match="motivazione è obbligatoria"):
            AdminPaymentRegistrationSchema(payment_status=stato, reason=motivazione)

    def test_incasso_senza_motivazione_accettato(self):
        schema = AdminPaymentRegistrationSchema(
            payment_status=PaymentStatus.PAID, payment_method=PaymentMethod.CASH_ON_SITE
        )
        assert schema.reason is None

    def test_motivazione_ripulita_dagli_spazi(self):
        schema = AdminPaymentRegistrationSchema(
            payment_status=PaymentStatus.REFUNDED, reason="  Bonifico del 10/10  "
        )
        assert schema.reason == "Bonifico del 10/10"

    def test_motivazione_troppo_lunga_rifiutata(self):
        with pytest.raises(ValidationError):
            AdminPaymentRegistrationSchema(payment_status=PaymentStatus.REFUNDED, reason="a" * 501)


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
            BookingSearchFiltersSchema(date_from=next_week(), date_to=today())

    def test_page_size_oltre_il_limite_rifiutata(self):
        with pytest.raises(ValidationError):
            BookingSearchFiltersSchema(page_size=500)

    # --- Ordinamento (01/10/2026) ------------------------------------------

    def test_ordinamento_predefinito_dell_api(self):
        """Il predefinito dell'API resta quello storico: nessun client cambia comportamento."""
        assert BookingSearchFiltersSchema().sort == BookingSortOrder.CHECK_IN_DESC

    def test_ordinamento_sconosciuto_rifiutato(self):
        with pytest.raises(ValidationError):
            BookingSearchFiltersSchema(sort="A_CASO")
