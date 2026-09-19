import os
from decimal import Decimal
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


current_dir = os.path.dirname(os.path.realpath(__file__))
env_file_path = os.path.join(current_dir, '../..', '.env')


class Settings(BaseSettings):
    """
    Configurazione applicativa caricata da `.env`.

    I parametri del modulo Booking hanno tutti un default sensato: il progetto
    resta avviabile senza toccare il `.env` esistente. Le integrazioni esterne
    (SMTP, captcha, Stripe) sono disattivate di default e vanno abilitate
    esplicitamente quando le relative credenziali sono disponibili.
    """

    app_name: str
    app_timezone: str = "Europe/Rome"

    #: Base URL della SPA Angular. Usato per comporre i link nelle email
    #: (conferma, gestione, cancellazione). Il token non viene mai inviato al
    #: backend in query string: la SPA lo inoltra nel body di una POST.
    frontend_base_url: str = "http://localhost:4200"

    max_file_size: int

    # --- Database ------------------------------------------------------------
    db_host: str
    db_port: int
    db_user: str
    db_password: SecretStr
    db_name: str

    # --- JWT -----------------------------------------------------------------
    jwt_secret_key: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_minutes: int

    # --- Booking: locking e regole di soggiorno ------------------------------
    #: Durata del blocco temporaneo dello slot in attesa di conferma/pagamento.
    booking_hold_minutes: int = 15
    #: Ore prima del check-in entro cui la cancellazione PAY_ON_ARRIVAL è gratuita.
    booking_free_cancellation_hours: int = 48
    booking_min_nights: int = 1
    booking_max_nights: int = 30
    booking_max_advance_days: int = 365
    booking_max_rooms_per_booking: int = 5
    #: Prefisso del codice prenotazione leggibile (es. "BB-2026-000123").
    booking_code_prefix: str = "BB"
    #: Intervallo di esecuzione dello sweeper delle prenotazioni scadute.
    sweeper_interval_seconds: int = 60

    # --- Booking: pricing ----------------------------------------------------
    #: Sconto applicato all'opzione PAY_NOW (pagamento online anticipato).
    online_payment_discount_percent: Decimal = Decimal("10.00")
    #: Penale in caso di cancellazione di una prenotazione PAY_NOW.
    #: 100 = completamente non rimborsabile.
    online_cancellation_penalty_percent: Decimal = Decimal("100.00")
    default_currency: str = "EUR"

    # --- Booking: anti-abuso -------------------------------------------------
    max_active_pending_per_email: int = 3
    rate_limit_enabled: bool = True
    rate_limit_booking_create_per_ip_hour: int = 5
    rate_limit_booking_create_per_email_day: int = 3
    rate_limit_availability_per_ip_minute: int = 20
    rate_limit_confirm_per_ip_hour: int = 10

    # --- Email (SMTP) --------------------------------------------------------
    email_enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_use_tls: bool = True
    email_from: str = "noreply@bbcosenza.it"
    email_from_name: str = "B&B Cosenza"

    # --- Captcha (Cloudflare Turnstile) --------------------------------------
    captcha_enabled: bool = False
    captcha_secret_key: Optional[SecretStr] = None
    captcha_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    # --- Pagamenti (Stripe) --------------------------------------------------
    # I dati di carta non transitano mai da questo backend: si usano Payment
    # Intents con Stripe.js lato browser. Qui servono solo le chiavi server.
    stripe_enabled: bool = False
    stripe_secret_key: Optional[SecretStr] = None
    stripe_webhook_secret: Optional[SecretStr] = None

    model_config = SettingsConfigDict(env_file=env_file_path, extra="ignore")


settings = Settings()
