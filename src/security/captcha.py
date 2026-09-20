"""
Verifica anti-bot tramite Cloudflare Turnstile.

Turnstile è stato scelto al posto di reCAPTCHA perché non profila gli utenti e
non richiede quindi un consenso ai cookie aggiuntivo: su un form di
prenotazione europeo è una differenza concreta.

Con `captcha_enabled = False` (default) la verifica è disattivata: in sviluppo
non serve configurare nulla.
"""
import logging
from typing import Optional

import httpx

from src.config.config import settings
from src.exception.custom_exception import CaptchaValidationFailed

logger = logging.getLogger(__name__)

#: Oltre questo tempo si considera il servizio non raggiungibile.
_VERIFY_TIMEOUT_SECONDS = 5.0


async def verify_captcha(token: Optional[str], remote_ip: Optional[str] = None) -> None:
    """
    Valida il token prodotto dal widget Turnstile nel browser dell'utente.

    **Comportamento in caso di indisponibilità del servizio.** Se Cloudflare non
    risponde o va in timeout, la richiesta viene **lasciata passare** e l'evento
    registrato come warning. È una scelta deliberata: bloccare tutte le
    prenotazioni durante un disservizio di terze parti costerebbe incassi reali,
    mentre le poche richieste automatiche che passassero nel frattempo creerebbero
    prenotazioni non confermate, che scadono da sole in quindici minuti senza
    aver occupato nulla di definitivo. Il danno atteso del fallire-aperto è
    quindi molto minore di quello del fallire-chiuso.

    Un token assente o rifiutato è invece un errore netto: lì l'informazione
    c'è, ed è negativa.

    :param token: valore inviato dal client nel campo `captcha_token`.
    :param remote_ip: IP del richiedente, opzionale, migliora l'analisi di
        Cloudflare.
    :raises CaptchaValidationFailed: token assente o non valido.
    """
    if not settings.captcha_enabled:
        return

    if not settings.captcha_secret_key:
        logger.warning(
            "captcha_enabled è attivo ma captcha_secret_key non è configurata: "
            "verifica saltata"
        )
        return

    if not token:
        raise CaptchaValidationFailed("Verifica anti-bot mancante")

    payload = {
        "secret": settings.captcha_secret_key.get_secret_value(),
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT_SECONDS) as client:
            response = await client.post(settings.captcha_verify_url, data=payload)
            response.raise_for_status()
            outcome = response.json()
    except (httpx.HTTPError, ValueError) as error:
        # Fail-open: vedi la motivazione nella docstring.
        logger.warning("Verifica captcha non riuscita, richiesta ammessa: %s", error)
        return

    if not outcome.get("success", False):
        # I codici di errore di Cloudflare restano nei log per la diagnosi, ma
        # non vengono esposti al client: direbbero a un attaccante perché il
        # suo token è stato scartato.
        logger.info("Captcha rifiutato: %s", outcome.get("error-codes"))
        raise CaptchaValidationFailed()
