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
from typing import Any, Dict, List, Optional, Tuple

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


#: Metodi di pagamento accettati. Decisi qui, non dal cruscotto Stripe, così
#: test e produzione si comportano allo stesso modo.
PAYMENT_METHOD_TYPES = ("card",)

#: Espansione chiesta a ogni chiamata che restituisce un Payment Intent: senza,
#: `latest_charge` è solo un identificativo e i dati della carta non ci sono.
_EXPAND_CHARGE = ["latest_charge"]


def _as_dict(obj: Any) -> Dict[str, Any]:
    """
    Confine fra la libreria `stripe` e il nostro codice: tutto ciò che ne esce
    passa da qui e diventa un dizionario semplice.

    Dalla versione 15 la libreria restituisce oggetti (`PaymentIntent`,
    `Event`) che **non hanno più `.get()`**: trattarli come dizionari solleva
    `AttributeError`. `to_dict()` li converte, oggetti annidati compresi
    (`latest_charge`, `data.object`). Un dizionario passa invariato: è ciò che
    restituiscono i moduli finti dei test.
    """
    if isinstance(obj, dict):
        return obj
    return obj.to_dict()


def card_details_of(intent: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Circuito e ultime quattro cifre della carta di un Payment Intent.

    Si leggono dall'addebito più recente (`latest_charge`): il vecchio campo
    `charges` è stato rimosso da Stripe con l'API `2022-11-15`, e leggerlo
    restituiva sempre niente, in silenzio (debito #24).

    Tre forme possibili di `latest_charge`, tutte gestite:

    - **oggetto** (chiamata con `expand=["latest_charge"]`): i dati ci sono;
    - **stringa** — l'identificativo dell'addebito, come nei webhook: niente;
    - **assente** — intent appena creato, nessun addebito ancora: niente.

    :return: `(brand, last4)`, entrambi `None` se non disponibili.
    """
    charge = intent.get("latest_charge")
    if not isinstance(charge, dict):
        return None, None

    card = (charge.get("payment_method_details") or {}).get("card") or {}
    return card.get("brand"), card.get("last4")


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

    def __init__(self, stripe_module: Any = None) -> None:
        """
        :param stripe_module: la libreria `stripe` da usare. Solo per i test,
            che la sostituiscono con un modulo finto: senza rete, senza chiavi.
            In produzione si omette e si usa quella installata.
        """
        if stripe_module is None:
            import stripe as stripe_module  # import locale: il pacchetto serve solo qui

            if not settings.stripe_secret_key:
                raise PaymentGatewayError("stripe_secret_key non configurata")
            stripe_module.api_key = settings.stripe_secret_key.get_secret_value()

        self._stripe = stripe_module

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
            # Solo carte (Apple Pay e Google Pay compresi: per Stripe sono
            # carte). Con `automatic_payment_methods` l'elenco lo decideva il
            # cruscotto, e comparivano Klarna, Amazon Pay, Satispay: metodi
            # che portano l'ospite fuori dal sito e gestiscono l'incasso
            # differito con regole proprie, mai collaudati (16/10/2026).
            payment_method_types=list(PAYMENT_METHOD_TYPES),
            metadata={"booking_code": booking_code, **(metadata or {})},
            idempotency_key=f"booking-{booking_code}",
            expand=_EXPAND_CHARGE,
        )
        return self._to_result(intent)

    async def retrieve_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        intent = await asyncio.to_thread(
            self._stripe.PaymentIntent.retrieve, intent_id, expand=_EXPAND_CHARGE
        )
        return self._to_result(intent)

    async def capture_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        """Preleva davvero l'importo già autorizzato."""
        intent = await asyncio.to_thread(
            self._stripe.PaymentIntent.capture, intent_id, expand=_EXPAND_CHARGE
        )
        return self._to_result(intent)

    async def cancel_payment_intent(self, intent_id: str) -> PaymentIntentResult:
        """
        Rilascia l'autorizzazione: la carta non verrà addebitata.

        Se Stripe rifiuta (`InvalidRequestError`) **non si tira a indovinare**:
        lo stesso errore copre «già incassato», «già annullato», un
        identificativo inesistente e una richiesta malformata. Si rilegge
        l'intent e si decide dal suo stato reale (debito #25.1):

        - `succeeded` → `PaymentIntentNotCancellable`: l'ospite ha pagato;
        - `canceled` → esito normale: l'autorizzazione era già rilasciata;
        - altro, o rilettura impossibile → `PaymentGatewayError`: lo sweeper
          non libera nulla e riprova al giro dopo.

        Prima ogni rifiuto diventava «già incassato», cioè «l'ospite ha
        pagato»: nel dubbio, la conclusione peggiore.

        :raises PaymentIntentNotCancellable: l'importo è già stato incassato.
            Non è un errore di sistema ma un esito: il chiamante deve
            concludere che il pagamento è andato a buon fine.
        :raises PaymentGatewayError: esito non determinabile.
        """
        try:
            intent = await asyncio.to_thread(
                self._stripe.PaymentIntent.cancel, intent_id, expand=_EXPAND_CHARGE
            )
        except self._stripe.error.InvalidRequestError as error:
            return await self._resolve_refused_cancel(intent_id, error)
        return self._to_result(intent)

    async def _resolve_refused_cancel(self, intent_id: str, error: Exception) -> PaymentIntentResult:
        """Decide l'esito di un annullamento rifiutato dallo stato reale dell'intent."""
        try:
            current = await self.retrieve_payment_intent(intent_id)
        except Exception as retrieve_error:
            raise PaymentGatewayError(
                "Annullamento rifiutato e stato del pagamento non verificabile"
            ) from retrieve_error

        if current.is_captured:
            raise PaymentIntentNotCancellable(str(error)) from error
        if current.status == "canceled":
            return current
        raise PaymentGatewayError(
            f"Annullamento rifiutato con il pagamento in stato '{current.status}'"
        ) from error

    async def refund(self, intent_id: str, amount_cents: Optional[int] = None) -> str:
        """Rimborsa un pagamento già incassato. `None` rimborsa l'intero importo."""
        parametri = {"payment_intent": intent_id}
        if amount_cents is not None:
            parametri["amount"] = amount_cents

        rimborso = await asyncio.to_thread(self._stripe.Refund.create, **parametri)
        return _as_dict(rimborso)["id"]

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
            return _as_dict(self._stripe.Webhook.construct_event(
                payload,
                signature,
                settings.stripe_webhook_secret.get_secret_value(),
                # Finestra anti-replay: senza, l'impostazione esisteva ma non
                # contava nulla, e cambiarla non aveva effetto (debito #25.2).
                tolerance=settings.stripe_webhook_tolerance_seconds,
            ))
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
        intent = _as_dict(intent)
        brand, last4 = card_details_of(intent)

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
