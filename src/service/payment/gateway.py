"""
Accesso a Stripe, dietro un'interfaccia sostituibile.

Terza applicazione dello stesso schema già usato per il rate limiter (#17) e
per le email (#24): un `Protocol` con un'implementazione reale e una finta. Qui
serve più che altrove, perché l'alternativa sarebbe provare i rimborsi
spostando denaro vero.

**Autorizzazione e incasso sono separati** (`capture_method="manual"`). Stripe
blocca l'importo sulla carta ma non lo preleva finché non lo chiediamo noi, e
quel momento è l'unico in cui possiamo verificare che la camera sia ancora
dell'ospite. È ciò che rende impossibile il caso "abbiamo incassato e non
abbiamo la stanza": se lo slot è andato perso, l'autorizzazione viene
rilasciata e **l'ospite non viene addebitato di un centesimo**.

**Il PAN non passa mai da qui.** Il browser parla direttamente con Stripe
tramite Stripe.js; questo modulo maneggia solo identificativi opachi
(`pi_...`) e, dopo l'incasso, il circuito e le ultime quattro cifre — che non
sono dati di autenticazione. Ambito PCI-DSS: SAQ-A-EP.

**Le chiamate sono sincrone, spostate fuori dall'event loop** con
`asyncio.to_thread`, come per SMTP: la libreria ufficiale di Stripe non è
asincrona, e un'operazione di rete bloccante fermerebbe l'intero server.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - dipende dalla versione di Python
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore

from src.config.config import settings
from src.exception.custom_exception import (
    InvalidWebhookSignature,
    PaymentGatewayError,
    PaymentIntentNotCancellable,
)

logger = logging.getLogger(__name__)

#: Valute senza decimali (yen, won...). Non ne usiamo, ma convertire un importo
#: come se ne avesse due produrrebbe un addebito cento volte più grande: meglio
#: rifiutare che sbagliare.
ZERO_DECIMAL_CURRENCIES = frozenset({"BIF", "CLP", "DJF", "GNF", "JPY", "KMF",
                                     "KRW", "MGA", "PYG", "RWF", "UGX", "VND",
                                     "VUV", "XAF", "XOF", "XPF"})

_HUNDRED = Decimal("100")


def to_minor_units(amount: Decimal, currency: str) -> int:
    """
    Converte un importo nella sua unità minima (euro → centesimi).

    Opera su `Decimal` e mai su `float`: un arrotondamento qui non è un
    disallineamento contabile, è un addebito sbagliato sulla carta di una
    persona.

    :raises PaymentGatewayError: se la valuta non ha due decimali. Meglio un
        errore esplicito che un importo centuplicato.
    """
    codice = currency.upper()
    if codice in ZERO_DECIMAL_CURRENCIES:
        raise PaymentGatewayError(
            f"Valuta '{codice}' senza decimali: conversione non supportata"
        )

    centesimi = (Decimal(amount) * _HUNDRED).quantize(Decimal("1"))
    return int(centesimi)


def from_minor_units(amount: int, currency: str) -> Decimal:
    """Inverso di `to_minor_units`, per confrontare con i totali in Decimal."""
    codice = currency.upper()
    if codice in ZERO_DECIMAL_CURRENCIES:
        raise PaymentGatewayError(
            f"Valuta '{codice}' senza decimali: conversione non supportata"
        )
    return (Decimal(amount) / _HUNDRED).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PaymentIntentResult:
    """
    Stato di un Payment Intent, ridotto a ciò che serve al dominio.

    `client_secret` è l'unico campo destinato al browser: consente di
    completare *quel* pagamento e nient'altro. Non è una chiave segreta e non
    va confuso con `stripe_secret_key`, ma non ha comunque ragione di comparire
    nei log.
    """

    id: str
    status: str
    amount: int
    currency: str
    client_secret: Optional[str] = None
    card_brand: Optional[str] = None
    card_last4: Optional[str] = None

    @property
    def is_capturable(self) -> bool:
        """L'autorizzazione è andata a buon fine e attende il nostro incasso."""
        return self.status == "requires_capture"

    @property
    def is_captured(self) -> bool:
        return self.status == "succeeded"


class StripeGateway(Protocol):
    """Operazioni su Stripe usate dal dominio."""

    async def create_payment_intent(
            self,
            amount_cents: int,
            currency: str,
            booking_code: str,
            metadata: Optional[Dict[str, str]] = None
    ) -> PaymentIntentResult:
        ...

    async def retrieve_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        ...

    async def capture_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        ...

    async def cancel_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        ...

    async def refund(self, intent_id: str, amount_cents: Optional[int] = None) -> str:
        ...

    def verify_webhook(self, payload: bytes, signature: str) -> Dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# Implementazione reale                                                        #
# --------------------------------------------------------------------------- #

class LiveStripeGateway:
    """Implementazione sopra la libreria ufficiale `stripe`."""

    def __init__(self) -> None:
        import stripe  # import locale: il pacchetto serve solo qui

        if not settings.stripe_secret_key:
            raise PaymentGatewayError("stripe_secret_key non configurata")

        stripe.api_key = settings.stripe_secret_key.get_secret_value()
        self._stripe = stripe

    # ------------------------------------------------------------------ #
    # Payment Intent                                                      #
    # ------------------------------------------------------------------ #

    async def create_payment_intent(
            self,
            amount_cents: int,
            currency: str,
            booking_code: str,
            metadata: Optional[Dict[str, str]] = None
    ) -> PaymentIntentResult:
        """
        Crea un Payment Intent in **sola autorizzazione**.

        La chiave di idempotenza è il codice prenotazione: due clic sul
        pulsante "Paga" non possono produrre due addebiti, perché Stripe
        riconosce la seconda richiesta come ripetizione della prima.
        """
        intent = await asyncio.to_thread(
            self._stripe.PaymentIntent.create,
            amount=amount_cents,
            currency=currency.lower(),
            capture_method="manual",
            automatic_payment_methods={"enabled": True},
            metadata={"booking_code": booking_code, **(metadata or {})},
            idempotency_key=f"booking-{booking_code}",
        )
        return self._to_result(intent)

    async def retrieve_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        intent = await asyncio.to_thread(self._stripe.PaymentIntent.retrieve, intent_id)
        return self._to_result(intent)

    async def capture_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        """Preleva davvero l'importo già autorizzato."""
        intent = await asyncio.to_thread(self._stripe.PaymentIntent.capture, intent_id)
        return self._to_result(intent)

    async def cancel_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        """
        Rilascia l'autorizzazione: la carta non verrà addebitata.

        :raises PaymentIntentNotCancellable: se Stripe rifiuta perché l'importo
            è già stato incassato. Non è un errore di sistema ma un esito: il
            chiamante deve concludere che il pagamento è andato a buon fine e
            comportarsi di conseguenza.
        """
        try:
            intent = await asyncio.to_thread(self._stripe.PaymentIntent.cancel, intent_id)
        except self._stripe.error.InvalidRequestError as error:
            raise PaymentIntentNotCancellable(str(error)) from error
        return self._to_result(intent)

    async def refund(self, intent_id: str, amount_cents: Optional[int] = None) -> str:
        """Rimborsa un pagamento già incassato. `None` rimborsa l'intero importo."""
        parametri = {"payment_intent": intent_id}
        if amount_cents is not None:
            parametri["amount"] = amount_cents

        rimborso = await asyncio.to_thread(self._stripe.Refund.create, **parametri)
        return rimborso["id"]

    # ------------------------------------------------------------------ #
    # Webhook                                                             #
    # ------------------------------------------------------------------ #

    def verify_webhook(self, payload: bytes, signature: str) -> Dict[str, Any]:
        """
        Verifica la firma e restituisce l'evento.

        **Il payload deve essere il corpo grezzo della richiesta**, byte per
        byte. Se viene deserializzato e riserializzato — cosa che FastAPI
        farebbe da sé se l'endpoint dichiarasse un modello — la firma non torna
        più, perché cambia anche solo l'ordine delle chiavi o la spaziatura.

        La verifica è anche il controllo di autenticità dell'endpoint: chiunque
        conosca l'URL può chiamarlo, solo Stripe sa firmarlo.
        """
        if not settings.stripe_webhook_secret:
            raise PaymentGatewayError("stripe_webhook_secret non configurata")

        try:
            return self._stripe.Webhook.construct_event(
                payload,
                signature,
                settings.stripe_webhook_secret.get_secret_value(),
            )
        except ValueError as error:
            raise InvalidWebhookSignature("Payload non valido") from error
        except self._stripe.error.SignatureVerificationError as error:
            raise InvalidWebhookSignature("Firma non valida") from error

    # ------------------------------------------------------------------ #
    # Conversione                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_result(intent) -> PaymentIntentResult:
        """Estrae dal Payment Intent solo i campi che il dominio usa."""
        brand = None
        last4 = None

        charges = (intent.get("charges") or {}).get("data") or []
        if charges:
            dettagli = (charges[0].get("payment_method_details") or {}).get("card") or {}
            brand = dettagli.get("brand")
            last4 = dettagli.get("last4")

        return PaymentIntentResult(
            id=intent["id"],
            status=intent["status"],
            amount=intent["amount"],
            currency=intent["currency"],
            client_secret=intent.get("client_secret"),
            card_brand=brand,
            card_last4=last4,
        )


# --------------------------------------------------------------------------- #
# Implementazione per i test                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class FakeStripeGateway:
    """
    Gateway in memoria. Nessuna rete, nessuna chiave, nessun denaro.

    Riproduce la macchina a stati di Stripe che ci interessa —
    `requires_payment_method` → `requires_capture` → `succeeded`, oppure
    `canceled` — e registra le chiamate ricevute, così un test può verificare
    **che cosa abbiamo chiesto a Stripe** e non solo come abbiamo reagito.

    È l'unico modo di provare un rimborso o un rilascio di autorizzazione senza
    spostare denaro vero.
    """

    intents: Dict[str, PaymentIntentResult] = field(default_factory=dict)
    calls: List[tuple] = field(default_factory=list)
    refunds: List[tuple] = field(default_factory=list)
    #: Se valorizzato, `cancel_payment_intent` fallisce come se l'importo
    #: fosse già stato incassato. Serve a provare la corsa fra sweeper e
    #: pagamento riuscito nello stesso istante.
    cancel_fails_as_captured: bool = False
    _counter: int = 0

    # ------------------------------------------------------------------ #

    async def create_payment_intent(
            self,
            amount_cents: int,
            currency: str,
            booking_code: str,
            metadata: Optional[Dict[str, str]] = None
    ) -> PaymentIntentResult:
        self.calls.append(("create", booking_code, amount_cents))

        # Idempotenza: stessa prenotazione, stesso intent.
        for intent in self.intents.values():
            if intent.id.endswith(f"_{booking_code}"):
                return intent

        self._counter += 1
        intent = PaymentIntentResult(
            id=f"pi_test{self._counter}_{booking_code}",
            status="requires_payment_method",
            amount=amount_cents,
            currency=currency.lower(),
            client_secret=f"pi_test{self._counter}_secret_xyz",
        )
        self.intents[intent.id] = intent
        return intent

    async def retrieve_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        self.calls.append(("retrieve", intent_id, None))
        return self._get(intent_id)

    async def capture_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        self.calls.append(("capture", intent_id, None))
        intent = self._get(intent_id)
        aggiornato = PaymentIntentResult(
            id=intent.id,
            status="succeeded",
            amount=intent.amount,
            currency=intent.currency,
            client_secret=intent.client_secret,
            card_brand=intent.card_brand or "visa",
            card_last4=intent.card_last4 or "4242",
        )
        self.intents[intent_id] = aggiornato
        return aggiornato

    async def cancel_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        self.calls.append(("cancel", intent_id, None))

        if self.cancel_fails_as_captured:
            raise PaymentIntentNotCancellable(
                "Il Payment Intent risulta già incassato"
            )

        intent = self._get(intent_id)
        aggiornato = PaymentIntentResult(
            id=intent.id,
            status="canceled",
            amount=intent.amount,
            currency=intent.currency,
        )
        self.intents[intent_id] = aggiornato
        return aggiornato

    async def refund(self, intent_id: str, amount_cents: Optional[int] = None) -> str:
        self.calls.append(("refund", intent_id, amount_cents))
        self.refunds.append((intent_id, amount_cents))
        return f"re_test_{len(self.refunds)}"

    def verify_webhook(self, payload: bytes, signature: str) -> Dict[str, Any]:
        """
        Nel finto gateway la firma è un confronto letterale con un valore
        convenzionale. La verifica vera è coperta da `LiveStripeGateway`, e
        riprodurne la crittografia qui significherebbe testare la libreria di
        Stripe invece del nostro codice.
        """
        import json

        if signature != "firma-valida":
            raise InvalidWebhookSignature("Firma non valida")
        return json.loads(payload.decode("utf-8"))

    # ------------------------------------------------------------------ #
    # Utilità per i test                                                  #
    # ------------------------------------------------------------------ #

    def authorize(self, intent_id: str) -> PaymentIntentResult:
        """Simula l'ospite che completa il pagamento sul sito."""
        intent = self._get(intent_id)
        aggiornato = PaymentIntentResult(
            id=intent.id,
            status="requires_capture",
            amount=intent.amount,
            currency=intent.currency,
            client_secret=intent.client_secret,
            card_brand="visa",
            card_last4="4242",
        )
        self.intents[intent_id] = aggiornato
        return aggiornato

    def _get(self, intent_id: str) -> PaymentIntentResult:
        if intent_id not in self.intents:
            raise PaymentGatewayError(f"Payment Intent inesistente: {intent_id}")
        return self.intents[intent_id]
