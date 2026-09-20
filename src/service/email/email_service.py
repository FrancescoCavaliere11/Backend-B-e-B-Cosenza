"""
Composizione e invio delle email transazionali del modulo Booking.

**Un invio non fallisce mai l'operazione che lo ha scatenato.** `_send`
cattura qualunque eccezione, la registra e ritorna. È la stessa scelta fatta
per il captcha (§4.5 del piano): una prenotazione valida resta valida anche se
il server SMTP è irraggiungibile, mentre rifiutarla costerebbe un incasso vero.
Una mail non partita si recupera dal back-office; una prenotazione persa no.

**Nei log finiscono** codice prenotazione, nome del template, esito e
destinatario mascherato. **Non finiscono** il token, il corpo del messaggio né
l'indirizzo completo. L'unica eccezione è il `ConsoleEmailBackend`, attivo solo
in sviluppo e documentato nella sua stessa classe.

**Autoescape attivo sui template HTML.** Non è una preferenza stilistica: il
nome dell'ospite arriva da un form pubblico e finisce dentro un messaggio
spedito con il mittente della struttura. Senza escaping chiunque potrebbe
iniettare markup e link in una email che *sembra* provenire dal B&B. I template
di testo semplice non sono escapati perché non esiste markup da neutralizzare.
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Union
from urllib.parse import quote
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from pathlib import Path

from src.config.config import settings
from src.data.schemas.booking_schema import BookingPublicSchema, BookingSchema
from src.service.email.backend import (
    EmailBackend,
    EmailMessage,
    build_default_backend,
    mask_recipient,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Percorsi della SPA che ricevono i token. Il token viaggia nella query string
#: del **frontend**, non del backend: la SPA lo legge e lo inoltra nel body di
#: una POST, così non finisce negli access log del server né nell'header
#: `Referer` delle richieste successive.
CONFIRM_PATH = "/prenotazione/conferma"
MANAGE_PATH = "/prenotazione/gestisci"
BOOKING_PATH = "/prenotazione"

#: I template leggono i campi comuni alle due viste. La vista amministrativa è
#: un sovrainsieme di quella pubblica, quindi un'email generata da un'azione di
#: back-office (un annullamento al banco, per esempio) non ha bisogno di una
#: conversione: i campi che le servono ci sono già.
BookingView = Union[BookingPublicSchema, BookingSchema]


# --------------------------------------------------------------------------- #
# Filtri di formattazione                                                      #
# --------------------------------------------------------------------------- #

def format_date(value: date) -> str:
    """`2026-09-20` → `20/09/2026`."""
    return value.strftime("%d/%m/%Y")


def format_datetime(value: datetime) -> str:
    """
    Istante UTC → ora locale della struttura, leggibile.

    Gli istanti sono salvati in UTC; mostrarli così all'ospite significherebbe
    dargli una scadenza sbagliata di due ore in estate.
    """
    local = value.astimezone(ZoneInfo(settings.app_timezone))
    return local.strftime("%d/%m/%Y alle %H:%M")


def format_money(value: Decimal) -> str:
    """`1234.5` → `1.234,50` (separatori italiani)."""
    quantized = Decimal(value).quantize(Decimal("0.01"))
    intero, _, decimali = f"{quantized:.2f}".partition(".")
    negativo = intero.startswith("-")
    intero = intero.lstrip("-")

    gruppi = []
    while len(intero) > 3:
        gruppi.insert(0, intero[-3:])
        intero = intero[:-3]
    gruppi.insert(0, intero)

    return f"{'-' if negativo else ''}{'.'.join(gruppi)},{decimali}"


class EmailService:
    """Compone i messaggi transazionali e li affida a un backend."""

    def __init__(self, backend: Optional[EmailBackend] = None) -> None:
        self._backend = backend or build_default_backend()
        self._environment = self._build_environment()

    @property
    def backend(self) -> EmailBackend:
        return self._backend

    @staticmethod
    def _build_environment() -> Environment:
        environment = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            # Escape solo sull'HTML: nel testo semplice produrrebbe `&amp;`
            # dentro i link, rendendoli inutilizzabili.
            autoescape=select_autoescape(
                enabled_extensions=("html",),
                default_for_string=False,
                default=False,
            ),
            # Un campo mancante deve rompere il test, non produrre
            # silenziosamente una email con un buco al posto della data.
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        environment.filters["data"] = format_date
        environment.filters["istante"] = format_datetime
        environment.filters["importo"] = format_money
        return environment

    # ------------------------------------------------------------------ #
    # Composizione dei link                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_link(path: str, token: Optional[str] = None) -> str:
        base = settings.frontend_base_url.rstrip("/")
        if token is None:
            return f"{base}{path}"
        # `safe=""` codifica anche `/` e `+`: il token è generato da
        # `secrets.token_urlsafe`, ma non si dipende da quell'alfabeto.
        return f"{base}{path}?token={quote(token, safe='')}"

    # ------------------------------------------------------------------ #
    # Messaggi                                                            #
    # ------------------------------------------------------------------ #

    async def send_booking_pending(
            self,
            booking: BookingView,
            confirmation_token: str
    ) -> None:
        """Prenotazione creata, in attesa che l'ospite confermi dal link."""
        await self._send(
            template="booking_pending",
            subject=f"Conferma la tua prenotazione {booking.code}",
            booking=booking,
            context={"confirm_url": self._build_link(CONFIRM_PATH, confirmation_token)},
        )

    async def send_booking_confirmed(
            self,
            booking: BookingView,
            manage_token: Optional[str] = None
    ) -> None:
        """
        Prenotazione confermata.

        Il link di gestione è l'unico strumento di cancellazione di un ospite
        non registrato: non ha un account, e la consultazione con codice ed
        email è in sola lettura.
        """
        await self._send(
            template="booking_confirmed",
            subject=f"Prenotazione confermata — {booking.code}",
            booking=booking,
            context={
                "manage_url": (
                    self._build_link(MANAGE_PATH, manage_token) if manage_token else None
                )
            },
        )

    async def send_booking_cancelled(self, booking: BookingView) -> None:
        """Prenotazione annullata, dall'ospite o dal back-office."""
        await self._send(
            template="booking_cancelled",
            subject=f"Prenotazione annullata — {booking.code}",
            booking=booking,
            context={"booking_url": self._build_link(BOOKING_PATH)},
        )

    async def send_booking_expired(self, booking: BookingView) -> None:
        """Blocco scaduto senza conferma: le camere sono tornate disponibili."""
        await self._send(
            template="booking_expired",
            subject=f"Prenotazione {booking.code} scaduta",
            booking=booking,
            context={"booking_url": self._build_link(BOOKING_PATH)},
        )

    # ------------------------------------------------------------------ #
    # Invio                                                               #
    # ------------------------------------------------------------------ #

    async def _send(
            self,
            template: str,
            subject: str,
            booking: BookingView,
            context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Renderizza le due versioni del messaggio e lo consegna.

        Non solleva mai: qualunque problema viene registrato e la chiamata
        ritorna. Chi invia una email non sta compiendo l'operazione principale,
        sta notificando che è avvenuta.
        """
        recipient = str(booking.guest_email)

        try:
            payload = {"booking": booking, **(context or {})}
            message = EmailMessage(
                to=recipient,
                subject=subject,
                html_body=self._environment.get_template(f"{template}.html").render(**payload),
                text_body=self._environment.get_template(f"{template}.txt").render(**payload),
            )
            await self._backend.send(message)
        except Exception:
            logger.exception(
                "Invio email fallito: template=%s prenotazione=%s destinatario=%s",
                template,
                booking.code,
                mask_recipient(recipient),
            )
            return

        logger.info(
            "Email inviata: template=%s prenotazione=%s destinatario=%s",
            template,
            booking.code,
            mask_recipient(recipient),
        )


# --------------------------------------------------------------------------- #
# Istanza applicativa                                                          #
# --------------------------------------------------------------------------- #

_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    """
    Istanza condivisa, costruita alla prima richiesta.

    Tardiva di proposito: l'ambiente Jinja legge la cartella dei template e il
    backend legge la configurazione, e farlo all'import renderebbe l'avvio
    dipendente dall'ordine degli import.
    """
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service


def configure_email_service(backend: EmailBackend) -> EmailService:
    """Sostituisce il backend dell'istanza condivisa. Usato dai test."""
    global _email_service
    _email_service = EmailService(backend=backend)
    return _email_service


def reset_email_service() -> None:
    """Riporta l'istanza condivisa allo stato iniziale. Usato dai test."""
    global _email_service
    _email_service = None
