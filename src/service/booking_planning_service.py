"""
Tabellone del back-office: camere sulle righe, soggiorni come barre.

Servizio di **sola lettura**, separato da `BookingService` perché non ne
condivide nulla: niente transizioni, niente transazioni da governare, niente
email. Tenerlo a parte evita di far crescere ancora una classe che ha già
molte responsabilità.
"""
from typing import Set
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.booking_room_item import BookingRoomItem
from src.data.model.room import Room
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import (
    PlanningRequestSchema,
    PlanningRoomSchema,
    PlanningSchema,
    PlanningStaySchema,
)


class BookingPlanningService:
    """Compone camere e soggiorni di una finestra di date."""

    def __init__(
            self,
            booking_repository: BookingRepository,
            room_repository: RoomRepository,
    ) -> None:
        self.booking_repository = booking_repository
        self.room_repository = room_repository

    async def get_planning(self, request: PlanningRequestSchema) -> PlanningSchema:
        """
        Camere e soggiorni della finestra richiesta.

        Righe: tutte le camere in vendita, più quelle disattivate che hanno
        soggiorni nella finestra — altrimenti quelle barre non avrebbero una
        riga su cui stare. Ordine per numero di camera.
        """
        items = await self.booking_repository.get_planning_items(
            request.date_from, request.date_to
        )
        rooms = await self.room_repository.get_all()

        occupied_room_ids: Set[UUID] = {item.room_id for item in items}
        visible_rooms = [
            room for room in rooms
            if room.enabled or room.id in occupied_room_ids
        ]

        return PlanningSchema(
            date_from=request.date_from,
            date_to=request.date_to,
            rooms=[self._to_room(room) for room in visible_rooms],
            stays=[self._to_stay(item) for item in items],
        )

    @staticmethod
    def _to_room(room: Room) -> PlanningRoomSchema:
        return PlanningRoomSchema(
            id=room.id,
            number=room.number,
            name=room.name,
            enabled=room.enabled,
        )

    @staticmethod
    def _to_stay(item: BookingRoomItem) -> PlanningStaySchema:
        booking = item.booking
        return PlanningStaySchema(
            booking_id=booking.id,
            code=booking.code,
            room_id=item.room_id,
            check_in=item.check_in,
            check_out=item.check_out,
            status=booking.status,
            payment_status=booking.payment_status,
            guest_name=f"{booking.guest_firstname} {booking.guest_lastname}".strip(),
            guest_count=booking.guest_count,
            hold_expires_at=booking.hold_expires_at,
        )


def build_planning_service(session: AsyncSession) -> BookingPlanningService:
    """Unico punto di composizione del servizio, come `build_booking_service`."""
    return BookingPlanningService(
        booking_repository=BookingRepository(session),
        room_repository=RoomRepository(session),
    )
