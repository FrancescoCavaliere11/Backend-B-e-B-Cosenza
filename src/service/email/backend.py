"""
Backend di consegna delle email.

Tre implementazioni dietro una sola interfaccia, con la stessa forma adottata
per il rate limiter (decisione #17): cambiare *come* un messaggio parte non
deve toccare *chi* lo compone.

- `SmtpEmailBackend` — consegna reale via SMTP.
- `ConsoleEmailBackend` — scrive il messaggio nei log. È il backend attivo in
  sviluppo, quando `email_enabled` è False.
- `MemoryEmailBackend` — conserva i messaggi in una lista. È quello che rende
  verificabile l'intero flusso nei test senza toccare la rete: senza di lui
  l'unico modo di sapere se una mail è partita sarebbe leggere una casella.

**`smtplib` e non `aiosmtplib`.** La libreria standard è sincrona, quindi la
chiamata gira in un thread separato con `asyncio.to_thread`: l'event loop non
si blocca e il progetto non guadagna una dipendenza per un'operazione che è
già in background. Se un domani il volume lo giustificasse, sostituire questa
classe basta — nessun altro file la conosce.
"""
import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import List, Optional

try:  # pragma: no cover - dipende dalla versione di Python
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore

from src.config.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    """
    Un messaggio pronto per la consegna, in versione HTML e testo semplice.

    Entrambe le versioni sono obbligatorie: un client che rifiuta l'HTML deve
    comunque poter leggere il link di conferma, altrimenti la prenotazione
    resta bloccata fino alla scadenza.
    """

    to: str
    subject: str
    html_body: str
    text_body: str


def mask_recipient(address: str) -> str:
    """
    Riduce un indirizzo a una forma registrabile nei log.

    `mario.rossi@gmail.com` diventa `m***@gmail.com`: abbastanza per
    correlare un invio a una prenotazione durante una diagnosi, non abbastanza
    per ricostruire una rubrica leggendo i log.
    """
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


class EmailBackend(Protocol):
    """Contratto minimo di un canale di consegna."""

    async def send(self, message: EmailMessage) -> None:
        """Consegna il messaggio. Può sollevare: la gestione spetta al chiamante."""
        ...


def _sender_domain() -> Optional[str]:
    """Dominio del mittente, usato per il `Message-ID` (`noreply@dominio.it` → `dominio.it`)."""
    domain = settings.email_from.partition("@")[2]
    return domain or None


def build_mime_message(message: EmailMessage) -> MimeMessage:
    """
    Messaggio MIME pronto per l'invio.

    **`Date` e `Message-ID` si scrivono qui**: `smtplib` non li aggiunge, e non
    tutti i server di invio lo fanno al posto nostro. Senza, il messaggio viola
    la RFC 5322, guadagna punti di spam e Gmail può rifiutarlo.

    `Reply-To` solo se configurato: il mittente è spesso una casella `noreply`,
    e senza di esso la risposta di un ospite andrebbe persa.
    """
    mime = MimeMessage()
    mime["Subject"] = message.subject
    mime["From"] = formataddr((settings.email_from_name, settings.email_from))
    mime["To"] = message.to
    mime["Date"] = formatdate(usegmt=True)
    mime["Message-ID"] = make_msgid(domain=_sender_domain())
    if settings.email_reply_to:
        mime["Reply-To"] = str(settings.email_reply_to)

    # L'ordine conta: `set_content` definisce la parte testuale,
    # `add_alternative` aggiunge quella HTML come versione preferita.
    mime.set_content(message.text_body)
    mime.add_alternative(message.html_body, subtype="html")
    return mime


def open_smtp_connection() -> smtplib.SMTP:
    """
    Apre la connessione al server SMTP, cifrata secondo la configurazione.

    - `SMTP_USE_SSL` (porta 465): cifrata dal primo byte. Ha la precedenza.
    - `SMTP_USE_TLS` (porta 587): connessione in chiaro promossa con STARTTLS.
    - nessuno dei due: in chiaro, solo per server locali di sviluppo.

    **Il certificato del server viene sempre verificato.** `starttls()` senza
    contesto, in Python, usa un contesto che *non* lo controlla: chi si mette
    in mezzo potrebbe fingersi il server di posta e leggere la password SMTP.
    `create_default_context()` verifica catena e nome dell'host.
    """
    context = ssl.create_default_context()
    timeout = settings.email_send_timeout_seconds

    if settings.smtp_use_ssl:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)

    connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        if settings.smtp_use_tls:
            connection.starttls(context=context)
    except Exception:
        # Certificato rifiutato o STARTTLS non supportato: si chiude il socket
        # e si rinuncia, senza mai ripiegare su una connessione in chiaro.
        connection.close()
        raise
    return connection


class SmtpEmailBackend:
    """Consegna via SMTP, con la chiamata bloccante spostata fuori dall'event loop."""

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, message)

    @staticmethod
    def _send_sync(message: EmailMessage) -> None:
        mime = build_mime_message(message)
        with open_smtp_connection() as smtp:
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.send_message(mime)


class ConsoleEmailBackend:
    """
    Scrive il messaggio nei log invece di spedirlo.

    ⚠️ **Qui il link compare per intero, token compreso.** È l'unico punto del
    sistema in cui un token in chiaro finisce in un log, ed è una deroga
    consapevole: questo backend è attivo esattamente quando `email_enabled` è
    False, cioè in sviluppo, dove serve poter completare il flusso senza una
    casella di posta. In produzione `email_enabled` è True e il backend è un
    altro.

    Se un domani si volesse abilitare l'invio reale tenendo i log verbosi,
    questa classe non deve essere la scorciatoia: si aggiunga un backend che
    spedisce *e* registra solo i metadati.
    """

    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "[email:console] a=%s oggetto=%s\n%s",
            message.to,
            message.subject,
            message.text_body,
        )


@dataclass
class MemoryEmailBackend:
    """
    Conserva i messaggi in memoria. Destinato ai test.

    Non è un backend di ripiego per la produzione: i messaggi si accumulano
    senza limite e spariscono col processo.
    """

    messages: List[EmailMessage] = field(default_factory=list)

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)

    # ------------------------------------------------------------------ #
    # Utilità per i test                                                  #
    # ------------------------------------------------------------------ #

    def clear(self) -> None:
        self.messages.clear()

    def sent_to(self, address: str) -> List[EmailMessage]:
        """Messaggi indirizzati a `address`, confronto senza distinzione di maiuscole."""
        target = address.lower()
        return [message for message in self.messages if message.to.lower() == target]

    @property
    def last(self) -> EmailMessage:
        """Ultimo messaggio consegnato. Solleva se non ne è partito nessuno."""
        if not self.messages:
            raise AssertionError("Nessuna email è stata inviata")
        return self.messages[-1]


class FailingEmailBackend:
    """
    Fallisce sempre. Serve a un solo test: verificare che un disservizio del
    canale email non faccia fallire l'operazione che lo ha scatenato.
    """

    def __init__(self, error: Exception = None) -> None:
        self._error = error or RuntimeError("Server SMTP irraggiungibile")

    async def send(self, message: EmailMessage) -> None:
        raise self._error


def build_default_backend() -> EmailBackend:
    """Sceglie il backend in base alla configurazione."""
    if not settings.email_enabled:
        return ConsoleEmailBackend()
    if "localhost" in settings.frontend_base_url or "127.0.0.1" in settings.frontend_base_url:
        # Normale in sviluppo; in produzione i link delle email non funzionerebbero.
        logger.warning(
            "Email attive con FRONTEND_BASE_URL=%s: i link funzionano solo su questo computer",
            settings.frontend_base_url,
        )
    return SmtpEmailBackend()
