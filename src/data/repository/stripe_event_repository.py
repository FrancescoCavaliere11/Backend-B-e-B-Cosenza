"""
Registro degli eventi Stripe, ai soli fini di idempotenza.

Stripe consegna *at-least-once*: lo stesso evento può arrivare più volte, e
arriva di sicuro più volte se la nostra prima risposta non è stata un `200` —
i ritentativi proseguono a intervalli crescenti per giorni. Senza questo
registro, un `payment_intent.succeeded` duplicato produrrebbe una seconda
conferma, una seconda email e, nel caso dei rimborsi, un secondo movimento di
denaro.

Come per gli altri repository del modulo, **qui non si committa mai**: il
confine transazionale appartiene al Service (decisione #14). È essenziale
proprio qui, perché la registrazione dell'evento e il suo effetto di business
devono stare nella **stessa transazione**. Se fossero separate, un guasto in
mezzo lascerebbe l'evento marcato come visto e il suo effetto mai applicato —
e i ritentativi di Stripe verrebbero scartati come duplicati.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.stripe_event import StripeEvent


class StripeEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
            self,
            event_id: str,
            event_type: str,
            payload_digest: Optional[str] = None
    ) -> bool:
        """
        Prende in carico un evento, se non è **già stato elaborato**.

        La distinzione fra "già visto" e "già elaborato" è il punto delicato.
        Un semplice `ON CONFLICT DO NOTHING` scarterebbe anche i ritentativi di
        un evento che avevamo registrato e poi **non** eravamo riusciti a
        elaborare — un guasto a metà strada resterebbe tale per sempre, e
        Stripe continuerebbe a ricevere `200` su un pagamento mai applicato.

        Per questo l'`ON CONFLICT` aggiorna, ma solo `WHERE processed_at IS
        NULL`: un evento concluso non restituisce nulla e viene scartato come
        duplicato, uno rimasto a metà viene ripreso.

        Si evita di proposito un "controlla poi inserisci": fra la lettura e la
        scrittura due consegne simultanee passerebbero entrambe. Qui decide il
        database, in una sola istruzione atomica.

        E si evita anche di intercettare l'`IntegrityError` di un insert
        normale: funzionerebbe, ma su PostgreSQL un errore invalida l'intera
        transazione, che contiene anche l'effetto di business. Servirebbe un
        SAVEPOINT; `ON CONFLICT` elimina il problema invece di aggirarlo.

        ⚠️ Resta una finestra teorica: due consegne simultanee dello stesso
        evento mai elaborato possono prenderlo in carico entrambe. È per questo
        che **gli effetti a valle sono idempotenti di loro** — la prenotazione
        viene bloccata con `FOR UPDATE` e `confirm_paid_booking` non fa nulla
        se lo stato è già `CONFIRMED`. Questo registro riduce il lavoro
        inutile; la correttezza non dipende solo da lui.

        :return: `True` se l'evento va elaborato adesso.
        """
        statement = (
            pg_insert(StripeEvent)
            .values(
                event_id=event_id,
                event_type=event_type,
                payload_digest=payload_digest,
            )
            .on_conflict_do_update(
                index_elements=["event_id"],
                set_={"payload_digest": payload_digest},
                where=StripeEvent.processed_at.is_(None),
            )
            .returning(StripeEvent.id)
        )

        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_processed(self, event_id: str) -> None:
        """Segna l'evento come elaborato con successo."""
        await self.session.execute(
            update(StripeEvent)
            .where(StripeEvent.event_id == event_id)
            .values(processed_at=datetime.now(timezone.utc), processing_error=None)
        )

    async def mark_failed(self, event_id: str, error: str) -> None:
        """
        Registra un fallimento di elaborazione, lasciando `processed_at` nullo.

        Il messaggio viene troncato: gli errori di Stripe possono contenere
        dettagli di configurazione che non hanno ragione di finire in tabella.
        """
        await self.session.execute(
            update(StripeEvent)
            .where(StripeEvent.event_id == event_id)
            .values(processing_error=error[:500])
        )

    async def get_by_event_id(self, event_id: str) -> Optional[StripeEvent]:
        result = await self.session.execute(
            select(StripeEvent).where(StripeEvent.event_id == event_id)
        )
        return result.scalar_one_or_none()

    async def unprocessed_count(self) -> int:
        """
        Eventi ricevuti ma mai elaborati con successo.

        Metrica di riconciliazione: un valore diverso da zero che non cala
        significa che qualche notifica di pagamento non ha prodotto il suo
        effetto, ed è la prima cosa da guardare quando un ospite dice di aver
        pagato e la prenotazione risulta non confermata.
        """
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(StripeEvent)
            .where(StripeEvent.processed_at.is_(None))
        )
        return result.scalar_one()
