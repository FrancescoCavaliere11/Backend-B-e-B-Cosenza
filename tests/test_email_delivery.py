"""
Test della consegna SMTP e della configurazione delle email.

Nessuna rete: `smtplib` è sostituito da un finto server che registra cosa gli
viene chiesto. Si verifica *come* il messaggio parte — cifratura, certificato,
intestazioni — e cosa vede l'ospite in fondo a ogni email.
"""
import logging
import re
import ssl
from datetime import date, datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import List, Optional
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from src.config.config import Settings, settings
from src.data.enumerators import BookingStatus, PaymentOption, PaymentStatus
from src.data.schemas.booking_schema import BookingPublicSchema, BookingRoomItemSchema
from src.service.email import backend as email_backend
from src.service.email.backend import (
    ConsoleEmailBackend,
    EmailMessage,
    MemoryEmailBackend,
    SmtpEmailBackend,
    build_default_backend,
    build_mime_message,
    open_smtp_connection,
)
from src.service.email.email_service import BRAND_COLOR, EmailService

_MESSAGE = EmailMessage(to="ospite@example.com", subject="Prova", html_body="<p>ciao</p>", text_body="ciao")


class FakeSmtp:
    """Finto server SMTP: registra le chiamate invece di aprire un socket."""

    instances: List["FakeSmtp"] = []
    fail_starttls: Optional[Exception] = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: List[str] = []
        self.starttls_context: Optional[ssl.SSLContext] = None
        self.sent = []
        self.closed = False
        FakeSmtp.instances.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.starttls_context = context
        if FakeSmtp.fail_starttls:
            raise FakeSmtp.fail_starttls

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, mime):
        self.calls.append("send")
        self.sent.append(mime)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeSmtpSsl(FakeSmtp):
    """Variante per la porta 465."""


@pytest.fixture
def smtp(monkeypatch):
    """Sostituisce `smtplib` e configura un server di invio verosimile."""
    FakeSmtp.instances = []
    FakeSmtp.fail_starttls = None
    monkeypatch.setattr(email_backend.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(email_backend.smtplib, "SMTP_SSL", FakeSmtpSsl)
    for name, value in {
        "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_use_tls": True, "smtp_use_ssl": False,
        "smtp_user": "utente@example.com", "smtp_password": SecretStr("password-smtp"),
        "email_from": "prenotazioni@villarendano.it", "email_from_name": "Villa Rendano",
        "email_reply_to": None, "email_contact_phone": None,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return FakeSmtp


def _assert_verifies_certificate(context: Optional[ssl.SSLContext]) -> None:
    assert context is not None, "contesto TLS assente: Python non verificherebbe il certificato"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


# ===========================================================================
# Connessione
# ===========================================================================

def test_starttls_verifica_il_certificato(smtp):
    """`starttls()` senza contesto, in Python, accetta qualunque certificato."""
    connection = open_smtp_connection()

    assert connection.calls == ["starttls"]
    _assert_verifies_certificate(connection.starttls_context)


def test_porta_465_usa_ssl_dal_primo_byte(smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_use_ssl", True)
    monkeypatch.setattr(settings, "smtp_port", 465)

    connection = open_smtp_connection()

    assert isinstance(connection, FakeSmtpSsl)
    assert connection.calls == []  # niente STARTTLS su una connessione già cifrata
    _assert_verifies_certificate(connection.context)


def test_senza_cifratura_niente_starttls(smtp, monkeypatch):
    """Solo per server locali di sviluppo."""
    monkeypatch.setattr(settings, "smtp_use_tls", False)

    connection = open_smtp_connection()

    assert type(connection) is FakeSmtp
    assert connection.calls == []


def test_certificato_rifiutato_chiude_e_non_ripiega_sul_chiaro(smtp):
    smtp.fail_starttls = ssl.SSLCertVerificationError("certificato non valido")

    with pytest.raises(ssl.SSLError):
        open_smtp_connection()

    assert smtp.instances[0].closed
    assert "send" not in smtp.instances[0].calls


async def test_invio_completo(smtp):
    await SmtpEmailBackend().send(_MESSAGE)

    connection = smtp.instances[0]
    assert connection.calls == ["starttls", "login:utente@example.com", "send"]
    assert connection.closed
    assert connection.sent[0]["To"] == "ospite@example.com"


# ===========================================================================
# Intestazioni
# ===========================================================================

def test_date_e_message_id_presenti(smtp):
    """`smtplib` non li aggiunge: senza, il messaggio rischia lo spam o il rifiuto."""
    mime = build_mime_message(_MESSAGE)

    assert mime["Message-ID"].endswith("@villarendano.it>")
    assert abs((parsedate_to_datetime(mime["Date"]) - datetime.now(timezone.utc)).total_seconds()) < 60
    assert mime["From"] == "Villa Rendano <prenotazioni@villarendano.it>"


def test_message_id_unico_per_messaggio(smtp):
    assert build_mime_message(_MESSAGE)["Message-ID"] != build_mime_message(_MESSAGE)["Message-ID"]


def test_reply_to_solo_se_configurato(smtp, monkeypatch):
    assert build_mime_message(_MESSAGE)["Reply-To"] is None

    monkeypatch.setattr(settings, "email_reply_to", "info@villarendano.it")
    assert build_mime_message(_MESSAGE)["Reply-To"] == "info@villarendano.it"


# ===========================================================================
# Configurazione
# ===========================================================================

@pytest.mark.parametrize("host", [None, "", "  "])
def test_email_attive_senza_server_non_si_avvia(host):
    with pytest.raises(ValidationError, match="SMTP_HOST") as error:
        Settings(email_enabled=True, smtp_host=host, smtp_password="password-da-non-mostrare")
    assert "password-da-non-mostrare" not in str(error.value)


def test_email_attive_con_server_si_avvia():
    assert Settings(email_enabled=True, smtp_host="smtp.example.com").email_enabled is True


def test_reply_to_non_valido_rifiutato():
    with pytest.raises(ValidationError):
        Settings(email_reply_to="non-una-email")


def test_backend_predefinito(monkeypatch, caplog):
    monkeypatch.setattr(settings, "email_enabled", False)
    assert isinstance(build_default_backend(), ConsoleEmailBackend)

    monkeypatch.setattr(settings, "email_enabled", True)
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:4200")
    with caplog.at_level(logging.WARNING):
        assert isinstance(build_default_backend(), SmtpEmailBackend)
    assert "FRONTEND_BASE_URL" in caplog.text


# ===========================================================================
# Piè di pagina e marchio
# ===========================================================================

def _booking() -> BookingPublicSchema:
    check_in, check_out = date(2026, 11, 10), date(2026, 11, 12)
    return BookingPublicSchema(
        code="BB-2026-A7K3QX", status=BookingStatus.PENDING_CONFIRMATION,
        check_in=check_in, check_out=check_out, nights=2, guest_count=2,
        guest_firstname="Mario", guest_lastname="Rossi", guest_email="mario.rossi@example.com",
        rooms=[BookingRoomItemSchema(
            room_id=uuid4(), room_name="Camera Girasole", room_number=101, check_in=check_in,
            check_out=check_out, nights=2, unit_price=Decimal("100.00"), line_total=Decimal("200.00"),
        )],
        base_price=Decimal("200.00"), discount_amount=Decimal("0.00"), total_price=Decimal("200.00"),
        currency="EUR", payment_option=PaymentOption.PAY_ON_ARRIVAL, payment_status=PaymentStatus.PENDING,
        hold_expires_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        cancellation_deadline=None, confirmed_at=None,
    )


async def _all_messages(monkeypatch, reply_to=None, phone=None) -> List[EmailMessage]:
    monkeypatch.setattr(settings, "email_from_name", "Villa Rendano")
    monkeypatch.setattr(settings, "email_reply_to", reply_to)
    monkeypatch.setattr(settings, "email_contact_phone", phone)
    memory = MemoryEmailBackend()
    service = EmailService(backend=memory)
    booking = _booking()
    await service.send_booking_pending(booking, "token")
    await service.send_booking_confirmed(booking, "token")
    await service.send_booking_cancelled(booking)
    await service.send_booking_expired(booking)
    await service.send_booking_slot_lost(booking)
    return memory.messages


@pytest.mark.parametrize("reply_to, phone, expected, forbidden", [
    ("info@villarendano.it", "+39 0984 123456",
     "rispondi a questa email o chiamaci al +39 0984 123456", "non rispondere"),
    ("info@villarendano.it", None, "Per qualsiasi necessità rispondi a questa email.", "non rispondere"),
    (None, "+39 0984 123456", "chiamaci al +39 0984 123456", "rispondi a questa email"),
    (None, None, "non rispondere a questo indirizzo", "chiamaci"),
])
async def test_piede_con_i_contatti_della_struttura(monkeypatch, reply_to, phone, expected, forbidden):
    """I testi dicono «contattaci»: in fondo deve esserci come farlo."""
    messages = await _all_messages(monkeypatch, reply_to, phone)

    assert len(messages) == 5
    for message in messages:
        visible_html = " ".join(re.sub(r"<[^>]+>", "", message.html_body).split())
        for body in (" ".join(message.text_body.split()), visible_html):
            assert expected in body
            assert forbidden not in body
            assert "None" not in body


async def test_telefono_cliccabile_nell_html(monkeypatch):
    messages = await _all_messages(monkeypatch, phone="+39 0984 123456")
    assert 'href="tel:+390984123456"' in messages[0].html_body


async def test_nome_e_colore_della_struttura(monkeypatch):
    for message in await _all_messages(monkeypatch):
        assert "Villa Rendano" in message.html_body
        assert "Villa Rendano" in message.text_body
        assert BRAND_COLOR in message.html_body
        assert "#1f4e79" not in message.html_body
        assert "B&B Cosenza" not in message.text_body
