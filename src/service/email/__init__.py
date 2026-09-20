"""Servizio di invio email del modulo Booking."""
from src.service.email.backend import (
    ConsoleEmailBackend,
    EmailBackend,
    EmailMessage,
    MemoryEmailBackend,
    SmtpEmailBackend,
)
from src.service.email.email_service import (
    EmailService,
    configure_email_service,
    get_email_service,
    reset_email_service,
)

__all__ = [
    "ConsoleEmailBackend",
    "EmailBackend",
    "EmailMessage",
    "EmailService",
    "MemoryEmailBackend",
    "SmtpEmailBackend",
    "configure_email_service",
    "get_email_service",
    "reset_email_service",
]
