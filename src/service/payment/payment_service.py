"""
Orchestrazione dei pagamenti: prenotazione, Stripe, email.

**Dove passa il confine.** Le transizioni di stato e le transazioni restano
nel `BookingService`, che possiede la macchina a stati e
`_sync_items_active_flag`. Qui c'è solo ciò che riguarda il coordinamento con
un servizio esterno: nessuna chiamata di rete deve mai avvenire dentro una
transazione del database, e nessuna decisione di dominio viene presa in questo
file.

**L'idea portante dello Step G.** Autorizzare e incassare sono due momenti
distinti (`capture_method="manual"`). Stripe blocca l'importo sulla carta ma
non lo preleva; siamo noi a prelevarlo, e prima di farlo verifichiamo che la
camera sia ancora dell'ospite. Se non lo è, l'autorizzazione viene rilasciata e
**l'ospite non viene addebitato di nulla**.

Il caso "abbiamo incassato e non abbiamo la stanza" non è gestito: è reso
impossibile. Al suo posto resta "autorizzazione rilasciata", che non muove
denaro e non richiede rimborsi.

**Sequenza del webhook**, in tre transazioni separate da due chiamate di rete:

1. *(DB)* presa in carico dell'evento e verifica — importo, stato, slot
2. *(Stripe)* incasso, oppure rilascio dell'autorizzazione
3. *(DB)* transizione della prenotazione, e solo dopo il commit l'email

Se qualcosa si rompe fra il 2 e il 3, il `payment_intent.succeeded` che Stripe
invia dopo l'incasso ripassa dallo stesso codice e completa il lavoro. È la
rete di sicurezza del disegno, e il motivo per cui ogni passo è idempotente.
"""
import hashlib
import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.data.repository.stripe_event_repository import StripeEventRepository
from src.data.schemas.booking_schema import PaymentIntentSchema, WebhookResultSchema
from src.exception.custom_exception import (
    PaymentGatewayError,
    PaymentIntentNotCancellable,
    RoomNotAvailable,
)
from src.service.booking_service import BookingService, PaymentOutcome
from src.service.email.email_service import EmailService, get_email_service
from src.service.payment.gateway import (
    StripeGateway,
    from_minor_units,
    to_minor_units,
)
from src.service.transaction import run_in_transaction

logger = logging.getLogger(__name__)

#: Eventi che producono un effetto. Tutti gli altri vengono confermati con un
#: `200` e ignorati: rispondere con un errore a un evento che non ci interessa
#: farebbe ritentare Stripe per giorni senza alcun motivo.
HANDLED_EVENTS = frozenset({
    "payment_intent.amount_capturable_updated",
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
    "charge.refunded",
})


class PaymentService:
    def __init__(
            self,
            session: AsyncSession,
            booking_service: BookingService,
            gateway: StripeGateway,
            stripe_event_repository: StripeEventRepository,
            email_service: Optional[EmailService] = None
    ) -> None:
        self.session = session
        self.booking_service = booking_service
        self.gateway = gateway
        self.event_repository = stripe_event_repository
        self.email_service = email_service or get_email_service()

    # ================================================================== #
    # Avvio del pagamento                                                #
    # ================================================================== #

    async def create_intent(self, code: str, email: str) -> PaymentIntentSchema:
        """
        Prepara il pagamento e restituisce al browser ciò che gli serve.

        Tre passi distinti, e la separazione non è pignoleria: la chiamata a
        Stripe sta **fra** due transazioni, mai dentro una. Un timeout del
        gestore, dentro una transazione, farebbe rollback della prenotazione —
        oppure lascerebbe dietro un Payment Intent pagabile senza nulla che gli
        corrisponda.

        **Idempotente.** Se la prenotazione ha già un intent lo si recupera
        invece di crearne un altro, e la creazione usa come chiave di
        idempotenza il codice prenotazione. Due clic sul pulsante "Paga" non
        possono produrre due addebiti.
        """
        contesto = await self.booking_service.start_payment(code, email)

        if contesto.existing_intent_id:
            intent = await self.gateway.retrieve_payment_intent(
                contesto.existing_intent_id
            )
        else:
            intent = await self.gateway.create_payment_intent(
                amount_cents=to_minor_units(contesto.total_price, contesto.currency),
                currency=contesto.currency,
                booking_code=contesto.code,
            )
            await self.booking_service.attach_payment_intent(contesto.code, intent.id)

        return PaymentIntentSchema(
            client_secret=intent.client_secret,
            amount=contesto.total_price,
            currency=contesto.currency,
            booking_code=contesto.code,
        )

    # ================================================================== #
    # Webhook                                                            #
    # ================================================================== #

    async def handle_webhook(self, payload: bytes, signature: str) -> WebhookResultSchema:
        """
        Verifica, prende in carico ed elabora una notifica di Stripe.

        La firma è l'**unica** autenticazione di questo endpoint: chiunque
        conosca l'URL può chiamarlo, solo Stripe sa firmarlo. La verifica
        avviene prima di qualunque altra cosa, sul corpo grezzo.
        """
        evento = self.gateway.verify_webhook(payload, signature)

        event_id = evento.get("id", "")
        event_type = evento.get("type", "")
        digest = hashlib.sha256(payload).hexdigest()

        nuovo = await run_in_transaction(
            self.session,
            lambda: self.event_repository.claim(event_id, event_type, digest),
        )
        if not nuovo:
            logger.info("Evento Stripe già elaborato, ignorato: %s", event_id)
            return WebhookResultSchema(
                event_id=event_id, event_type=event_type, outcome="DUPLICATE"
            )

        if event_type not in HANDLED_EVENTS:
            await self._mark_processed(event_id)
            logger.info("Evento Stripe non gestito, confermato: %s", event_type)
            return WebhookResultSchema(
                event_id=event_id, event_type=event_type, outcome="IGNORED"
            )

        try:
            esito = await self._dispatch(event_type, evento)
        except Exception as error:
            # L'evento resta non elaborato: Stripe ritenta, `claim` lo
            # riprenderà, e nel frattempo compare in `unprocessed_count`.
            await self._mark_failed(event_id, str(error))
            logger.exception("Elaborazione fallita per l'evento %s", event_id)
            raise

        await self._mark_processed(event_id)
        return WebhookResultSchema(
            event_id=event_id, event_type=event_type, outcome=esito
        )

    async def _dispatch(self, event_type: str, evento: Dict[str, Any]) -> str:
        oggetto = evento.get("data", {}).get("object", {}) or {}

        if event_type == "payment_intent.amount_capturable_updated":
            return await self._handle_authorized(oggetto)
        if event_type == "payment_intent.succeeded":
            return await self._handle_captured(oggetto)
        if event_type == "payment_intent.payment_failed":
            return await self._handle_failed(oggetto)
        if event_type == "payment_intent.canceled":
            return await self._handle_canceled(oggetto)
        if event_type == "charge.refunded":
            return await self._handle_refunded(oggetto)

        return "IGNORED"

    # ------------------------------------------------------------------ #
    # Autorizzazione: il momento della verità                             #
    # ------------------------------------------------------------------ #

    async def _handle_authorized(self, intent: Dict[str, Any]) -> str:
        """
        L'importo è autorizzato ma non ancora prelevato. Qui si decide.

        È il punto in cui l'intero disegno dello Step G si gioca: nessun denaro
        si è ancora mosso, e non si muoverà se la camera non è più dell'ospite.
        """
        intent_id = intent["id"]
        importo = from_minor_units(intent["amount"], intent["currency"])

        try:
            verifica = await self.booking_service.authorize_payment(
                intent_id, importo, intent["currency"]
            )
        except RoomNotAvailable:
            return await self._release_for_slot_lost(intent_id)

        if verifica.outcome == PaymentOutcome.UNKNOWN_BOOKING:
            # Un'autorizzazione senza prenotazione non deve restare viva.
            logger.warning("Nessuna prenotazione per il Payment Intent %s", intent_id)
            await self._cancel_quietly(intent_id)
            return "UNKNOWN_BOOKING"

        if verifica.outcome == PaymentOutcome.ALREADY_CONFIRMED:
            return "ALREADY_CONFIRMED"

        if verifica.outcome == PaymentOutcome.AMOUNT_MISMATCH:
            # Non si incassa un importo che non corrisponde al dovuto, in
            # nessuna delle due direzioni.
            logger.error(
                "Importo autorizzato diverso dal totale: prenotazione=%s intent=%s",
                verifica.booking_code,
                intent_id,
            )
            await self._cancel_quietly(intent_id)
            return "AMOUNT_MISMATCH"

        # Da qui in poi lo slot è nostro e l'importo è quello giusto.
        incassato = await self.gateway.capture_payment_intent(intent_id)
        return await self._finalize(intent_id, incassato.card_brand, incassato.card_last4)

    async def _handle_captured(self, intent: Dict[str, Any]) -> str:
        """
        Conferma dell'avvenuto incasso.

        Normalmente arriva dopo che siamo già passati da `_finalize`, e allora
        non fa nulla. Serve come **rete di sicurezza**: se il processo fosse
        morto fra l'incasso e la scrittura, questo evento completerebbe il
        lavoro.
        """
        carta = (intent.get("charges") or {}).get("data") or []
        dettagli = (carta[0].get("payment_method_details") or {}).get("card") or {} if carta else {}

        return await self._finalize(
            intent["id"], dettagli.get("brand"), dettagli.get("last4")
        )

    async def _finalize(
            self,
            intent_id: str,
            card_brand: Optional[str],
            card_last4: Optional[str]
    ) -> str:
        """Porta la prenotazione a `CONFIRMED` e avvisa l'ospite."""
        risultato = await self.booking_service.confirm_paid_booking(
            intent_id, card_brand, card_last4
        )
        if risultato is None:
            return "ALREADY_CONFIRMED"

        # Dopo il commit, mai prima: annunciare una conferma che potrebbe
        # ancora essere annullata da un rollback è un messaggio irrevocabile
        # su un fatto reversibile.
        await self.email_service.send_booking_confirmed(
            risultato.booking, risultato.manage_token
        )
        return "CONFIRMED"

    async def _release_for_slot_lost(self, intent_id: str) -> str:
        """
        Le camere sono state vendute ad altri mentre il pagamento era in corso.

        **Prima si rende impossibile l'incasso, poi si chiude la
        prenotazione.** L'ordine non è negoziabile: invertirlo lascerebbe
        aperta la possibilità di addebitare una camera che non abbiamo.
        """
        try:
            await self.gateway.cancel_payment_intent(intent_id)
        except PaymentIntentNotCancellable:
            # L'ospite ha completato il pagamento nell'istante esatto in cui
            # stavamo rilasciando. I soldi sono nostri e la camera no: è
            # l'unico caso residuo che richiede un rimborso.
            logger.error(
                "Incasso avvenuto su uno slot perduto: rimborso per %s", intent_id
            )
            await self.gateway.refund(intent_id)

        prenotazione = await self.booking_service.abandon_for_slot_lost(intent_id)

        if prenotazione is not None:
            await self.email_service.send_booking_slot_lost(prenotazione)

        return "SLOT_LOST"

    # ------------------------------------------------------------------ #
    # Esiti negativi                                                      #
    # ------------------------------------------------------------------ #

    async def _handle_failed(self, intent: Dict[str, Any]) -> str:
        """
        Pagamento rifiutato. La prenotazione resta **ritentabile**.

        Una carta rifiutata è quasi sempre un problema di quella carta:
        annullare subito costringerebbe l'ospite a rifare tutto per qualcosa
        che si risolve cambiando tessera. Lo slot resta suo fino alla scadenza
        del blocco.
        """
        await self.booking_service.mark_payment_failed(intent["id"])
        return "PAYMENT_FAILED"

    async def _handle_canceled(self, intent: Dict[str, Any]) -> str:
        """Autorizzazione rilasciata, di solito su nostra richiesta."""
        logger.info("Autorizzazione rilasciata per %s", intent["id"])
        return "CANCELED"

    async def _handle_refunded(self, charge: Dict[str, Any]) -> str:
        intent_id = charge.get("payment_intent")
        if not intent_id:
            return "IGNORED"

        await self.booking_service.mark_payment_refunded(intent_id)
        return "REFUNDED"

    # ------------------------------------------------------------------ #
    # Utilità                                                             #
    # ------------------------------------------------------------------ #

    async def cancel_authorization(self, intent_id: str) -> bool:
        """
        Rilascia un'autorizzazione, per conto dello sweeper.

        :return: `True` se rilasciata, `False` se Stripe ha risposto che
            l'importo era già stato incassato — nel qual caso lo slot **non**
            va liberato e il pagamento va portato a conferma.
        :raises PaymentGatewayError: gestore irraggiungibile. Chi chiama deve
            astenersi dal liberare lo slot.
        """
        try:
            await self.gateway.cancel_payment_intent(intent_id)
            return True
        except PaymentIntentNotCancellable:
            return False

    async def _cancel_quietly(self, intent_id: str) -> None:
        """Rilascia un'autorizzazione senza far fallire l'elaborazione."""
        try:
            await self.gateway.cancel_payment_intent(intent_id)
        except (PaymentIntentNotCancellable, PaymentGatewayError):
            logger.exception("Rilascio dell'autorizzazione fallito per %s", intent_id)

    async def _mark_processed(self, event_id: str) -> None:
        await run_in_transaction(
            self.session, lambda: self.event_repository.mark_processed(event_id)
        )

    async def _mark_failed(self, event_id: str, error: str) -> None:
        await run_in_transaction(
            self.session, lambda: self.event_repository.mark_failed(event_id, error)
        )
