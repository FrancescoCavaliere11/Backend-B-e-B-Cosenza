"""
Data Access Layer del modulo Booking.

**Politica transazionale.** A differenza dei repository più vecchi del
progetto (`RoomRepository`, `RoomServiceRepository`), questo repository **non
esegue mai `commit()`**. La creazione di una prenotazione è una singola
transazione atomica che comprende lock pessimistico sulle camere, liberazione
degli hold scaduti, insert della prenotazione, delle righe camera, del token e
dello storico: se ogni repository committasse per conto proprio l'atomicità
andrebbe persa e il lock pessimistico non servirebbe a nulla.

Il confine transazionale appartiene quindi al Service
(`async with self.session.begin()`), coerentemente con l'architettura descritta
in `PROJECT_CONTEXT.md`.

Per lo stesso motivo non esiste un metodo `update(booking)` basato su
`session.merge()`: con `version_id_col` attivo sul `Booking`, riagganciare un
oggetto staccato che porta un `version` non aggiornato produce `StaleDataError`
o, peggio, un aggiornamento perso. Il Service carica l'entità nella sessione,
la modifica e lascia che sia il commit a persisterla.
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.data.enumerators import OCCUPYING_BOOKING_STATUSES, PENDING_BOOKING_STATUSES, BookingStatus
from src.data.model.booking import Booking
from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.room import Room
from src.data.schemas.booking_schema import BookingSearchFiltersSchema


class BookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Scrittura                                                           #
    # ------------------------------------------------------------------ #

    def add(self, booking: Booking) -> Booking:
        """
        Registra la prenotazione nella sessione. Non committa: la transazione
        è responsabilità del Service.
        """
        self.session.add(booking)
        return booking

    async def flush(self) -> None:
        """
        Invia al database le modifiche pendenti senza chiudere la transazione.

        Serve quando occorre leggere un valore generato dal database (o far
        scattare subito un vincolo, come l'exclusion constraint) prima della
        fine della transazione.
        """
        await self.session.flush()

    # ------------------------------------------------------------------ #
    # Letture per identificativo                                          #
    # ------------------------------------------------------------------ #

    async def get_by_id(
            self,
            booking_id: UUID,
            with_items: bool = True,
            with_history: bool = False
    ) -> Optional[Booking]:
        query = select(Booking).where(Booking.id == booking_id)
        query = self._apply_eager_options(query, with_items, with_history)

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_code(
            self,
            code: str,
            with_items: bool = True,
            with_history: bool = False
    ) -> Optional[Booking]:
        query = select(Booking).where(Booking.code == code)
        query = self._apply_eager_options(query, with_items, with_history)

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_code_and_email(self, code: str, email: str) -> Optional[Booking]:
        """
        Consultazione da parte di un ospite non registrato.

        Il confronto sull'email è case-insensitive: gli ospiti la riscrivono a
        memoria e la capitalizzazione non è significativa.
        """
        query = (
            select(Booking)
            .options(selectinload(Booking.items).selectinload(BookingRoomItem.room))
            .where(
                Booking.code == code,
                func.lower(Booking.guest_email) == email.lower(),
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_stripe_payment_intent(self, payment_intent_id: str) -> Optional[Booking]:
        query = select(Booking).where(Booking.stripe_payment_intent_id == payment_intent_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_user(
            self,
            user_id: UUID,
            limit: int = 20,
            offset: int = 0
    ) -> List[Booking]:
        query = (
            select(Booking)
            .options(selectinload(Booking.items).selectinload(BookingRoomItem.room))
            .where(Booking.user_id == user_id)
            .order_by(Booking.check_in.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def code_exists(self, code: str) -> bool:
        query = select(Booking.id).where(Booking.code == code).limit(1)
        result = await self.session.execute(query)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------ #
    # Disponibilità e concorrenza                                         #
    # ------------------------------------------------------------------ #

    async def get_occupied_room_ids(
            self,
            check_in,
            check_out,
            exclude_booking_id: Optional[UUID] = None
    ) -> List[UUID]:
        """
        Identificativi delle camere occupate nell'intervallo richiesto.

        Restituisce volutamente **solo UUID** e non entità `Room`: la domanda
        "quali camere sono libere" attraversa due aggregati distinti, e
        costringerla dentro un solo repository ne sporcherebbe i confini. Sarà
        il Service a chiedere le camere abilitate a `RoomRepository` e a
        sottrarre questi identificativi.

        Una riga occupa lo slot quando è attiva, lo stato della prenotazione è
        occupante e — per gli stati temporanei — il blocco non è ancora
        scaduto. La condizione sull'hold è indispensabile: fra la scadenza e il
        passaggio dello sweeper la riga è ancora `is_active`, ma lo slot è di
        fatto libero.

        :param check_in: inizio dell'intervallo (incluso).
        :param check_out: fine dell'intervallo (escluso).
        :param exclude_booking_id: prenotazione da ignorare, usata quando si
            modificano le date di una prenotazione esistente.
        """
        now = datetime.now(timezone.utc)

        query = (
            select(BookingRoomItem.room_id)
            .join(Booking, Booking.id == BookingRoomItem.booking_id)
            .where(
                BookingRoomItem.is_active.is_(True),
                BookingRoomItem.check_in < check_out,
                BookingRoomItem.check_out > check_in,
                Booking.status.in_(list(OCCUPYING_BOOKING_STATUSES)),
                or_(
                    Booking.status.notin_(list(PENDING_BOOKING_STATUSES)),
                    Booking.hold_expires_at > now,
                ),
            )
            .distinct()
        )

        if exclude_booking_id is not None:
            query = query.where(Booking.id != exclude_booking_id)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_active_overlapping_items(
            self,
            room_ids: List[UUID],
            check_in,
            check_out,
            exclude_booking_id: Optional[UUID] = None
    ) -> List[BookingRoomItem]:
        """
        Righe camera che collidono con l'intervallo richiesto, per le camere
        indicate. Usata dal Service per produrre un `RoomNotAvailable` con un
        messaggio utile, invece di attendere l'`IntegrityError` del database.
        """
        if not room_ids:
            return []

        now = datetime.now(timezone.utc)

        query = (
            select(BookingRoomItem)
            .join(Booking, Booking.id == BookingRoomItem.booking_id)
            .options(selectinload(BookingRoomItem.room))
            .where(
                BookingRoomItem.room_id.in_(room_ids),
                BookingRoomItem.is_active.is_(True),
                BookingRoomItem.check_in < check_out,
                BookingRoomItem.check_out > check_in,
                Booking.status.in_(list(OCCUPYING_BOOKING_STATUSES)),
                or_(
                    Booking.status.notin_(list(PENDING_BOOKING_STATUSES)),
                    Booking.hold_expires_at > now,
                ),
            )
        )

        if exclude_booking_id is not None:
            query = query.where(Booking.id != exclude_booking_id)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def lock_rooms_for_update(self, room_ids: List[UUID]) -> None:
        """
        Lock pessimistico sulle camere coinvolte, dentro la transazione in corso.

        L'ordinamento per `id` è obbligatorio: due transazioni che bloccano le
        stesse camere in ordine diverso si bloccherebbero a vicenda. Bloccando
        sempre nello stesso ordine il deadlock è impossibile.

        Serve a ottenere un `409` pulito nel caso comune di concorrenza; la
        garanzia ultima resta comunque l'exclusion constraint del database.
        """
        if not room_ids:
            return

        query = (
            select(Room.id)
            .where(Room.id.in_(room_ids))
            .order_by(Room.id)
            .with_for_update()
        )
        await self.session.execute(query)

    async def deactivate_expired_holds(self, room_ids: Optional[List[UUID]] = None) -> int:
        """
        Libera gli slot delle prenotazioni temporanee con blocco scaduto.

        È la *just-in-time expiration*: va invocata dentro la transazione di
        creazione, prima dell'insert, così l'exclusion constraint non blocca
        mai su un lock già decaduto **anche se lo sweeper in background è
        fermo**. Il sistema resta corretto senza dipendere dallo scheduler.

        Agisce solo su `is_active`. Il passaggio di stato a `EXPIRED` e le
        notifiche sono responsabilità dello sweeper (Step F): qui interessa
        unicamente che lo slot torni prenotabile.

        :param room_ids: se valorizzato, limita l'operazione a quelle camere,
            riducendo le righe toccate nel percorso critico della creazione.
        :return: numero di righe liberate.
        """
        now = datetime.now(timezone.utc)

        expired_bookings = (
            select(Booking.id)
            .where(
                Booking.status.in_(list(PENDING_BOOKING_STATUSES)),
                Booking.hold_expires_at.isnot(None),
                Booking.hold_expires_at <= now,
            )
        )

        statement = (
            update(BookingRoomItem)
            .where(
                BookingRoomItem.is_active.is_(True),
                BookingRoomItem.booking_id.in_(expired_bookings),
            )
        )

        if room_ids:
            statement = statement.where(BookingRoomItem.room_id.in_(room_ids))

        statement = statement.values(is_active=False).execution_options(
            synchronize_session=False
        )

        result = await self.session.execute(statement)
        return result.rowcount or 0

    # ------------------------------------------------------------------ #
    # Sweeper e anti-abuso                                                #
    # ------------------------------------------------------------------ #

    async def get_expired_pending(self, limit: int = 100) -> List[Booking]:
        """Prenotazioni temporanee il cui blocco è scaduto, da portare a `EXPIRED`."""
        now = datetime.now(timezone.utc)

        query = (
            select(Booking)
            .options(selectinload(Booking.items))
            .where(
                Booking.status.in_(list(PENDING_BOOKING_STATUSES)),
                Booking.hold_expires_at.isnot(None),
                Booking.hold_expires_at <= now,
            )
            .order_by(Booking.hold_expires_at)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_active_pending_by_email(self, email: str) -> int:
        """
        Quante prenotazioni temporanee ancora valide fanno capo a questa email.

        Limita il flooding: un bot che apre decine di prenotazioni mai
        confermate bloccherebbe l'inventario per l'intera durata dell'hold.
        """
        now = datetime.now(timezone.utc)

        query = (
            select(func.count(Booking.id))
            .where(
                func.lower(Booking.guest_email) == email.lower(),
                Booking.status.in_(list(PENDING_BOOKING_STATUSES)),
                Booking.hold_expires_at > now,
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one()

    # ------------------------------------------------------------------ #
    # Ricerca amministrativa                                              #
    # ------------------------------------------------------------------ #

    async def search(
            self,
            filters: BookingSearchFiltersSchema
    ) -> Tuple[List[Booking], int]:
        """
        Elenco filtrato e paginato per il back-office.

        :return: tupla (risultati della pagina, totale complessivo).
        """
        conditions = []

        if filters.status:
            conditions.append(Booking.status.in_(filters.status))

        if filters.date_from:
            conditions.append(Booking.check_out > filters.date_from)

        if filters.date_to:
            conditions.append(Booking.check_in < filters.date_to)

        if filters.email:
            conditions.append(func.lower(Booking.guest_email) == str(filters.email).lower())

        if filters.code:
            conditions.append(Booking.code == filters.code.strip().upper())

        if filters.room_id:
            conditions.append(
                Booking.id.in_(
                    select(BookingRoomItem.booking_id)
                    .where(BookingRoomItem.room_id == filters.room_id)
                )
            )

        count_query = select(func.count(Booking.id))
        if conditions:
            count_query = count_query.where(*conditions)

        total_result = await self.session.execute(count_query)
        total = total_result.scalar_one()

        # selectinload evita l'N+1: una query aggiuntiva per tutte le righe
        # camera della pagina, invece di una per ogni prenotazione.
        query = (
            select(Booking)
            .options(selectinload(Booking.items).selectinload(BookingRoomItem.room))
            .order_by(Booking.check_in.desc(), Booking.created_at.desc())
            .limit(filters.page_size)
            .offset(filters.offset)
        )
        if conditions:
            query = query.where(*conditions)

        result = await self.session.execute(query)
        return list(result.scalars().all()), total

    # ------------------------------------------------------------------ #
    # Helper interni                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_eager_options(query, with_items: bool, with_history: bool):
        """Applica il caricamento esplicito delle relazioni richieste."""
        if with_items:
            query = query.options(
                selectinload(Booking.items).selectinload(BookingRoomItem.room)
            )
        if with_history:
            query = query.options(selectinload(Booking.status_history))
        return query
