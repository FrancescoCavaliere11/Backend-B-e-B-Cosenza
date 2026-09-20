"""
Test del servizio email. **Nessun database, nessuna rete.**

Il canale è un `MemoryEmailBackend`, quindi qui si verifica ciò che il servizio
*produce*: i template si compongono, i link puntano dove devono, i dati
dell'ospite non possono iniettare markup, e un disservizio del canale non si
propaga a chi ha chiamato.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from src.config.config import settings
from src.data.enumerators import BookingStatus, PaymentOption, PaymentStatus
from src.data.schemas.booking_schema import BookingPublicSchema, BookingRoomItemSchema
from src.service.email.backend import FailingEmailBackend, MemoryEmailBackend, mask_recipient
from src.service.email.email_service import EmailService, format_money

# `asyncio_mode = auto` in pytest.ini: ogni `async def test_` viene eseguito
# senza marcatura esplicita.


# --------------------------------------------------------------------------- #
# Dati di prova                                                                #
# --------------------------------------------------------------------------- #

def build_booking(**overrides) -> BookingPublicSchema:
    """Una prenotazione pubblica verosimile, personalizzabile campo per campo."""
    check_in = overrides.pop("check_in", date(2026, 11, 10))
    check_out = overrides.pop("check_out", date(2026, 11, 13))

    defaults = dict(
        code="BB-2026-A7K3QX",
        status=BookingStatus.PENDING_CONFIRMATION,
        check_in=check_in,
        check_out=check_out,
        nights=(check_out - check_in).days,
        guest_count=2,
        guest_firstname="Mario",
        guest_lastname="Rossi",
        guest_email="mario.rossi@example.com",
        rooms=[
            BookingRoomItemSchema(
                room_id=uuid4(),
                room_name="Camera Girasole",
                room_number=101,
                check_in=check_in,
                check_out=check_out,
                nights=(check_out - check_in).days,
                unit_price=Decimal("100.00"),
                line_total=Decimal("300.00"),
            )
        ],
        base_price=Decimal("300.00"),
        discount_amount=Decimal("0.00"),
        total_price=Decimal("300.00"),
        currency="EUR",
        payment_option=PaymentOption.PAY_ON_ARRIVAL,
        payment_status=PaymentStatus.PENDING,
        hold_expires_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        cancellation_deadline=None,
        confirmed_at=None,
    )
    defaults.update(overrides)
    return BookingPublicSchema(**defaults)


@pytest.fixture
def backend() -> MemoryEmailBackend:
    return MemoryEmailBackend()


@pytest.fixture
def service(backend) -> EmailService:
    return EmailService(backend=backend)


# --------------------------------------------------------------------------- #
# Composizione                                                                 #
# --------------------------------------------------------------------------- #

async def test_tutti_e_quattro_i_messaggi_si_compongono(service, backend):
    """Ogni template esiste nelle due versioni e produce contenuto."""
    booking = build_booking()

    await service.send_booking_pending(booking, "token-conferma")
    await service.send_booking_confirmed(booking, "token-gestione")
    await service.send_booking_cancelled(booking)
    await service.send_booking_expired(booking)

    assert len(backend.messages) == 4

    for message in backend.messages:
        assert message.to == "mario.rossi@example.com"
        assert booking.code in message.subject
        assert "<html" in message.html_body
        assert booking.code in message.html_body
        assert booking.code in message.text_body
        # La versione testuale non deve essere un residuo vuoto: chi legge la
        # posta senza HTML deve trovarci tutto quello che gli serve.
        assert len(message.text_body.strip()) > 200


async def test_il_riepilogo_riporta_soggiorno_e_totale(service, backend):
    booking = build_booking()
    await service.send_booking_confirmed(booking, "token")

    corpo = backend.last.text_body
    assert "10/11/2026" in corpo
    assert "13/11/2026" in corpo
    assert "Camera Girasole" in corpo
    assert "300,00 EUR" in corpo


async def test_email_di_conferma_senza_link_di_gestione(service, backend):
    """
    Il token di gestione può mancare: il template non deve rompersi.

    Succede quando la conferma arriva da un percorso che non lo emette. Con
    `StrictUndefined` attivo, un `{{ manage_url }}` non gestito farebbe
    fallire il rendering invece di produrre un buco silenzioso.
    """
    await service.send_booking_confirmed(build_booking(), manage_token=None)

    messaggio = backend.last
    assert "Gestisci" not in messaggio.html_body
    assert "confermata" in messaggio.text_body.lower()


# --------------------------------------------------------------------------- #
# Sicurezza del contenuto                                                      #
# --------------------------------------------------------------------------- #

async def test_il_nome_ospite_non_puo_iniettare_markup(service, backend):
    """
    Il nome arriva da un form pubblico e finisce in un messaggio spedito con il
    mittente della struttura: senza escaping, chiunque potrebbe infilare un
    link in una email che *sembra* del B&B.
    """
    booking = build_booking(
        guest_firstname='<a href="http://evil.test">Clicca</a>',
    )

    await service.send_booking_pending(booking, "token")

    html = backend.last.html_body
    assert '<a href="http://evil.test">' not in html
    assert "&lt;a href=" in html


async def test_il_testo_semplice_non_viene_escapato(service, backend):
    """
    Nel testo semplice l'escaping sarebbe un danno: trasformerebbe i caratteri
    dei link in entità HTML, rendendoli inutilizzabili. E non c'è nulla da
    neutralizzare, perché non esiste markup da interpretare.

    La firma "B&B Cosenza" è il caso di prova più immediato: nella versione
    HTML compare come `B&amp;B`, in quella testuale deve restare `B&B`.
    """
    await service.send_booking_pending(build_booking(), "token")

    messaggio = backend.last
    assert "B&B Cosenza" in messaggio.text_body
    assert "&amp;" not in messaggio.text_body


async def test_l_oggetto_contiene_solo_dati_generati_dal_server(service, backend):
    """
    L'oggetto è un header: un ritorno a capo al suo interno consentirebbe di
    aggiungerne altri. Per questo interpola il **codice prenotazione**, che il
    server genera da un alfabeto chiuso, e mai il nome dell'ospite.
    """
    booking = build_booking(guest_firstname="Mario\nBcc: vittima@example.com")

    await service.send_booking_pending(booking, "token")

    assert "\n" not in backend.last.subject
    assert "vittima@example.com" not in backend.last.subject


# --------------------------------------------------------------------------- #
# Link                                                                         #
# --------------------------------------------------------------------------- #

async def test_il_link_di_conferma_punta_alla_spa(service, backend):
    await service.send_booking_pending(build_booking(), "token-abc")

    corpo = backend.last.text_body
    assert f"{settings.frontend_base_url.rstrip('/')}/prenotazione/conferma?token=token-abc" in corpo


async def test_il_token_viene_codificato_nell_url(service, backend):
    """Un token con caratteri speciali non deve spezzare la query string."""
    await service.send_booking_confirmed(build_booking(), manage_token="a/b+c=d")

    corpo = backend.last.text_body
    assert "token=a%2Fb%2Bc%3Dd" in corpo
    assert "token=a/b+c=d" not in corpo


async def test_la_scadenza_e_mostrata_in_ora_locale(service, backend):
    """
    Gli istanti sono salvati in UTC. Mostrarli così darebbe all'ospite una
    scadenza sbagliata di un'ora o due, a seconda della stagione.
    """
    booking = build_booking(
        hold_expires_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    )

    await service.send_booking_pending(booking, "token")

    # Europe/Rome a luglio è UTC+2.
    assert "15/07/2026 alle 14:00" in backend.last.text_body


# --------------------------------------------------------------------------- #
# Robustezza                                                                   #
# --------------------------------------------------------------------------- #

async def test_un_canale_guasto_non_propaga_l_errore():
    """
    Se il server SMTP è irraggiungibile la prenotazione resta valida.

    Rifiutarla costerebbe un incasso vero; una mail non partita si recupera dal
    back-office. È la stessa scelta fatta per il captcha.
    """
    service = EmailService(backend=FailingEmailBackend())

    await service.send_booking_pending(build_booking(), "token")  # non solleva


async def test_un_template_mancante_non_propaga_l_errore(service, monkeypatch):
    """Anche un errore di rendering resta confinato all'invio."""
    monkeypatch.setattr(
        service, "_environment", None
    )  # qualunque uso dell'ambiente solleverà

    await service.send_booking_pending(build_booking(), "token")  # non solleva


# --------------------------------------------------------------------------- #
# Log                                                                          #
# --------------------------------------------------------------------------- #

async def test_il_token_non_finisce_nei_log(service, caplog):
    """
    Il token in chiaro non esiste nel database — c'è solo il suo hash — e non
    deve esistere nemmeno nei log, che hanno tutt'altra durata e tutt'altro
    pubblico.
    """
    with caplog.at_level(logging.DEBUG):
        await service.send_booking_pending(build_booking(), "token-segretissimo")

    assert "token-segretissimo" not in caplog.text


async def test_il_destinatario_nei_log_e_mascherato(service, caplog):
    with caplog.at_level(logging.INFO):
        await service.send_booking_pending(build_booking(), "token")

    assert "mario.rossi@example.com" not in caplog.text
    assert "m***@example.com" in caplog.text


async def test_anche_un_invio_fallito_maschera_il_destinatario(caplog):
    service = EmailService(backend=FailingEmailBackend())

    with caplog.at_level(logging.ERROR):
        await service.send_booking_pending(build_booking(), "token-segreto")

    assert "mario.rossi@example.com" not in caplog.text
    assert "token-segreto" not in caplog.text
    assert "BB-2026-A7K3QX" in caplog.text


# --------------------------------------------------------------------------- #
# Formattazione                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "valore, atteso",
    [
        (Decimal("0"), "0,00"),
        (Decimal("9.5"), "9,50"),
        (Decimal("100"), "100,00"),
        (Decimal("1234.5"), "1.234,50"),
        (Decimal("1234567.89"), "1.234.567,89"),
        (Decimal("-45.10"), "-45,10"),
    ],
)
def test_formattazione_degli_importi(valore, atteso):
    """Separatori italiani: punto per le migliaia, virgola per i decimali."""
    assert format_money(valore) == atteso


@pytest.mark.parametrize(
    "indirizzo, atteso",
    [
        ("mario.rossi@example.com", "m***@example.com"),
        ("a@b.it", "a***@b.it"),
        ("senza-chiocciola", "***"),
    ],
)
def test_mascheramento_del_destinatario(indirizzo, atteso):
    assert mask_recipient(indirizzo) == atteso


# --------------------------------------------------------------------------- #
# Configurazione dei log                                                       #
# --------------------------------------------------------------------------- #

def test_i_log_applicativi_hanno_una_destinazione():
    """
    Senza questa configurazione ogni `logger.info` del progetto viene scartato
    in silenzio: uvicorn configura soltanto i logger `uvicorn.*`, e un logger
    senza handler non solleva nulla — semplicemente non scrive.

    Non è un dettaglio estetico. Il `ConsoleEmailBackend` **esiste solo** per
    scrivere nei log: se nessuno li raccoglie, l'intero collaudo delle email in
    sviluppo è muto e sembra che la funzionalità non parta.
    """
    from src.config.logging_config import configure_logging

    logger = configure_logging()

    assert logger.handlers, "Il sottoalbero 'src' deve avere un handler"
    assert logging.getLogger("src.service.email.email_service").isEnabledFor(logging.INFO)


def test_configurare_i_log_due_volte_non_duplica_i_messaggi():
    """Il reloader di uvicorn reimporta l'applicazione a ogni modifica."""
    from src.config.logging_config import configure_logging

    quanti = len(configure_logging().handlers)
    assert len(configure_logging().handlers) == quanti


def test_la_propagazione_resta_attiva():
    """
    Spegnere `propagate` sembrerebbe prudente contro i doppioni, ma romperebbe
    `caplog`, che cattura i messaggi proprio attraverso la propagazione: i test
    su cosa finisce nei log smetterebbero di vedere qualunque cosa e
    passerebbero a vuoto.
    """
    from src.config.logging_config import configure_logging

    assert configure_logging().propagate is True
