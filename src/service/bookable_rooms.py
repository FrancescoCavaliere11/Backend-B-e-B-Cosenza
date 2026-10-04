"""
Regola condivisa: quali camere si possono prenotare (o interrogare).

Prima di questo modulo la verifica viveva solo in `BookingService`; il
calendario (`AvailabilityService.occupancy`) ne ha bisogno identica. Tenerla
in un punto solo evita che le due strade rispondano in modo diverso alla
stessa camera inesistente o disattivata.
"""
from typing import List, Sequence
from uuid import UUID

from src.data.model.room import Room
from src.data.repository.room_repository import RoomRepository
from src.exception.custom_exception import EntityNotFound, RoomNotAvailable


async def load_bookable_rooms(
        room_repository: RoomRepository,
        room_ids: Sequence[UUID]
) -> List[Room]:
    """
    Carica le camere indicate e verifica che esistano e siano in vendita.

    :raises EntityNotFound: almeno una camera non esiste (`404`).
    :raises RoomNotAvailable: almeno una camera è disattivata (`409`).
    """
    rooms = await room_repository.get_all_by_ids(list(room_ids))

    if len(rooms) != len(set(room_ids)):
        raise EntityNotFound("Una o più camere selezionate non esistono")

    disabled = [room.name for room in rooms if not room.enabled]
    if disabled:
        raise RoomNotAvailable(
            f"Le seguenti camere non sono attualmente prenotabili: {', '.join(disabled)}"
        )

    return rooms
