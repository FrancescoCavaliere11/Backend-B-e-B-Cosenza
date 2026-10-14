"""
Mascheratura dei dati personali destinati ai log.

Nei log serve poter correlare un evento a una persona durante una diagnosi,
non ricostruire una rubrica leggendoli.
"""


def mask_email(address: str) -> str:
    """
    `mario.rossi@gmail.com` → `m***@gmail.com`.

    :param address: indirizzo, anche malformato.
    :return: forma registrabile; `***` se non contiene `@`.
    """
    local, separator, domain = address.strip().partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"
