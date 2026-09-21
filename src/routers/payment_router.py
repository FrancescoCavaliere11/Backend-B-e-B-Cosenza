"""
API dei pagamenti.

Due endpoint con due modelli di sicurezza opposti.

`POST /intent` è pubblico e si difende come il lookup: servono codice **e**
email, più il rate limiting. Non espone nulla di riservato — il
`client_secret` consente di pagare quella prenotazione e nient'altro.

`POST /webhook` non ha autenticazione applicativa: la sua unica difesa è la
**firma di Stripe**. Chiunque conosca l'URL può chiamarlo, solo Stripe sa
firmarlo. Per questo non ha rate limiting: bloccare le notifiche di Stripe
significherebbe perdere pagamenti, e un attaccante senza firma viene comunque
respinto.

Come negli altri router, nessun `try/except`: le eccezioni di dominio sono
tutte `AppException` e le traduce l'handler globale.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.config import settings
from src.config.database_config import get_async_session
from src.data.repository.stripe_event_repository import StripeEventRepository
from src.data.schemas.booking_schema import (
    PaymentIntentRequestSchema,
    PaymentIntentSchema,
    WebhookResultSchema,
)
from src.exception.custom_exception import PaymentGatewayError
from src.routers.booking_router import get_booking_service
from src.security.rate_limiter import booking_lookup_rate_limit
from src.service.booking_service import BookingService
from src.service.payment.gateway import LiveStripeGateway, StripeGateway
from src.service.payment.payment_service import PaymentService

logger = logging.getLogger(__name__)

payment_router = APIRouter(prefix="/api/v1/payments", tags=["Pagamenti"])

#: Gateway condiviso, costruito alla prima richiesta. La costruzione legge la
#: configurazione e importa la libreria di Stripe: farlo all'avvio
#: impedirebbe di lanciare l'applicazione senza chiavi, cosa che in sviluppo e
#: nei test serve poter fare.
_gateway: StripeGateway = None


def get_stripe_gateway() -> StripeGateway:
    """
    Fornisce il gateway. I test lo sostituiscono con `FakeStripeGateway`
    tramite `dependency_overrides`.
    """
    global _gateway

    if not settings.stripe_enabled:
        raise PaymentGatewayError("I pagamenti online non sono attivi")

    if _gateway is None:
        _gateway = LiveStripeGateway()
    return _gateway


async def get_payment_service(
        db: AsyncSession = Depends(get_async_session),
        booking_service: BookingService = Depends(get_booking_service),
        gateway: StripeGateway = Depends(get_stripe_gateway),
) -> PaymentService:
    return PaymentService(
        session=db,
        booking_service=booking_service,
        gateway=gateway,
        stripe_event_repository=StripeEventRepository(db),
    )


# --------------------------------------------------------------------------- #
# Avvio del pagamento                                                          #
# --------------------------------------------------------------------------- #

@payment_router.post(
    "/intent",
    response_model=PaymentIntentSchema,
    dependencies=[Depends(booking_lookup_rate_limit)],
    summary="Avvia il pagamento di una prenotazione",
)
async def create_payment_intent(
        payload: PaymentIntentRequestSchema,
        service: Annotated[PaymentService, Depends(get_payment_service)],
) -> PaymentIntentSchema:
    """
    Prepara il pagamento e restituisce il `client_secret` da dare a Stripe.js.

    **L'importo non viene mai accettato dal client**: è letto dalla
    prenotazione, che a sua volta lo ha ricavato da un preventivo firmato e
    ricalcolato dal server.

    **Solo autorizzazione.** Il Payment Intent nasce con incasso differito:
    Stripe blocca l'importo sulla carta ma non lo preleva. Il prelievo avviene
    dopo che il webhook ha verificato che la camera sia ancora dell'ospite.

    **Ripetibile senza conseguenze.** Chiamarlo due volte restituisce lo stesso
    Payment Intent, non due addebiti.

    Il blocco dello slot viene prorogato a `BOOKING_PAYMENT_HOLD_MINUTES`: un
    pagamento richiede più tempo di un clic su un link.

    **Codici**: `200` · `402` prenotazione da saldare in struttura · `404` codice
    o email errati · `409` già confermata · `410` blocco scaduto e slot perduto
    · `429` · `502` gestore non raggiungibile
    """
    return await service.create_intent(payload.code, str(payload.email))


# --------------------------------------------------------------------------- #
# Notifiche                                                                    #
# --------------------------------------------------------------------------- #

@payment_router.post(
    "/webhook",
    response_model=WebhookResultSchema,
    status_code=status.HTTP_200_OK,
    summary="Riceve le notifiche di Stripe",
    include_in_schema=False,
)
async def stripe_webhook(
        request: Request,
        service: Annotated[PaymentService, Depends(get_payment_service)],
        stripe_signature: str = Header(default="", alias="Stripe-Signature"),
) -> WebhookResultSchema:
    """
    Elabora una notifica firmata.

    ⚠️ **Il corpo viene letto grezzo**, con `await request.body()`, e
    l'endpoint non dichiara un modello Pydantic per il payload. Non è una
    scorciatoia: se FastAPI deserializzasse il JSON e la libreria lo
    riserializzasse per verificare la firma, basterebbe un ordine di chiavi
    diverso o uno spazio in più perché il confronto fallisse. È l'errore
    classico di questa integrazione, e produce un endpoint che rifiuta tutte le
    notifiche legittime.

    **Idempotente**: Stripe consegna *at-least-once* e ritenta per giorni
    finché non riceve `200`. Lo stesso evento elaborato due volte non produce
    una seconda conferma né una seconda email.

    **Un errore qui deve restare un errore.** Rispondere `200` a una notifica
    che non siamo riusciti a elaborare significherebbe dire a Stripe di non
    riprovare più, e perdere quel pagamento per sempre. Le eccezioni risalgono
    all'handler globale, che risponde `5xx`, e Stripe ritenta.

    `include_in_schema=False`: non è una API pubblica e non ha ragione di
    comparire in Swagger.
    """
    corpo = await request.body()
    return await service.handle_webhook(corpo, stripe_signature)
