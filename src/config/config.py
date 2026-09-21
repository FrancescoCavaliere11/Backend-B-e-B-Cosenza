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

    #: Soglia dei log applicativi (`src.*`). Vale solo per il codice del
    #: progetto: i log di uvicorn e di SQLAlchemy restano governati dalle loro
    #: configurazioni.
    log_level: str = "INFO"

    max_file_size: int

    # --- Database ------------------------------------------------------------
    db_host: str
    db_port: int
    db_user: str
    db_password: SecretStr
    db_name: str

    #: Database usato dai test automatici. Se omesso vale `<db_name>_test`, e
    #: le fixture di pytest lo creano da sole alla prima esecuzione.
    #: Non deve mai coincidere con `db_name`: i test committano dati reali e
    #: uno di essi è progettato per fallire, quindi può lasciare righe dietro
    #: di sé — una riga di prova attiva blocca fisicamente uno slot in vendita.
    db_test_name: Optional[str] = None

    # --- JWT -----------------------------------------------------------------
    jwt_secret_key: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_minutes: int

    #: Validità del preventivo firmato presentato al momento della prenotazione.
    quote_token_expire_minutes: int = 15

    # --- Booking: locking e regole di soggiorno ------------------------------
    #: Durata del blocco temporaneo dello slot in attesa di conferma.
    booking_hold_minutes: int = 15
    #: Durata del blocco quando è in corso un pagamento online.
    #:
    #: Più lunga di `booking_hold_minutes` perché misura una cosa diversa:
    #: quindici minuti bastano per un clic su un link, non per un pagamento —
    #: autenticazione della banca, carta rifiutata e ritentata, ospite che si
    #: allontana dal computer.
    #:
    #: Allungarla non apre falle: ciò che tiene davvero lo slot non è questo
    #: timer ma l'autorizzazione viva su Stripe, e lo sweeper non libera nulla
    #: prima di averla annullata. Questo valore è il tetto agli abbandoni, non
    #: una finestra di rischio.
    booking_payment_hold_minutes: int = 30
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
    #: Avvio automatico dello sweeper insieme all'applicazione. Disattivabile
    #: quando si preferisce pilotarlo da `POST /admin/bookings/sweep-expired`
    #: o da uno scheduler esterno.
    sweeper_enabled: bool = True
    #: Prenotazioni trattate a ogni passata. Limita la durata della singola
    #: transazione e il numero di righe tenute sotto lock.
    sweeper_batch_size: int = 100
    #: Età massima di una scadenza per cui vale ancora la pena avvisare l'ospite.
    #:
    #: Serve a una situazione precisa: se lo sweeper resta fermo per giorni, al
    #: riavvio trova un arretrato di prenotazioni scadute. Gli stati vanno
    #: comunque sistemati, ma spedire centinaia di email su richieste che
    #: l'ospite ha dimenticato da un pezzo è solo un modo per farsi segnalare
    #: come spam.
    sweeper_notify_max_age_hours: int = 24
    #: Tetto alla validità del token di gestione inviato con l'email di
    #: conferma. Normalmente il token scade alla partenza; questo limite entra
    #: in gioco solo per soggiorni prenotati con grande anticipo, perché un
    #: link valido per anni è un link che prima o poi finisce altrove.
    manage_token_max_days: int = 400

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

    #: Numero di reverse proxy di cui fidarsi davanti all'applicazione.
    #:
    #: `0` (default) ignora `X-Forwarded-For` e usa l'IP del socket: corretto in
    #: sviluppo e quando l'app è esposta direttamente. Con un valore `N` si
    #: prende l'N-esimo indirizzo **da destra** nella catena, cioè quello
    #: scritto dal proxy più interno di cui ci si fida.
    #:
    #: Va alzato solo dopo aver messo davvero un proxy davanti: quell'header lo
    #: scrive il client, e fidarsene senza un proxy che lo riscriva significa
    #: consentire a chiunque di cambiare IP a ogni richiesta e azzerare il
    #: rate limiting.
    trusted_proxy_count: int = 0

    # --- Email (SMTP) --------------------------------------------------------
    email_enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_use_tls: bool = True
    email_from: str = "noreply@bbcosenza.it"
    email_from_name: str = "B&B Cosenza"
    #: Timeout della connessione SMTP. Volutamente basso: l'invio gira in
    #: background, ma un socket appeso tiene comunque occupato un thread.
    email_send_timeout_seconds: int = 10

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

    #: Rimborso automatico quando si annulla una prenotazione già incassata.
    #:
    #: Non riguarda il caso "slot perduto durante il pagamento", che non
    #: produce mai un addebito: l'autorizzazione viene rilasciata prima di
    #: incassare. Riguarda gli annullamenti successivi alla conferma, dove i
    #: soldi sono davvero nostri e restituirli è una decisione commerciale.
    stripe_auto_refund_on_cancellation: bool = False

    #: Tolleranza sull'età della firma del webhook, in secondi. Oltre questa
    #: soglia una notifica viene respinta anche se firmata correttamente: è la
    #: difesa contro il riuso di una notifica catturata in passato.
    stripe_webhook_tolerance_seconds: int = 300

    model_config = SettingsConfigDict(env_file=env_file_path, extra="ignore")


settings = Settings()
