"""
Ricerca delle camere disponibili e composizione delle proposte di soggiorno.

La ricerca **non filtra per capienza**: restituisce tutte le camere libere
nell'intervallo, marcando quelle che da sole bastano per gli ospiti e
proponendo le combinazioni utili. Filtrare a monte renderebbe impossibile
prenotare due camere doppie per quattro persone, che in un B&B è il caso
normale delle famiglie.
"""
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import List, Optional, Sequence

from src.config.config import settings
from src.data.model.room import Room
from src.data.repository.booking_repository import BookingRepository
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.booking_schema import (
    AvailabilityResponseSchema,
    AvailableRoomSchema,
    RoomCombinationSchema,
)
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.service.pricing_service import PricingService

#: Oltre questa soglia di camere libere l'enumerazione esaustiva viene
#: sostituita da un'euristica. Per un B&B non scatterà mai, ma evita che
#: l'endpoint degradi il giorno in cui l'inventario cresce.
_EXHAUSTIVE_SEARCH_LIMIT = 20

#: Numero massimo di combinazioni restituite. Oltre, diventa una lista che
#: nessuno legge.
_MAX_COMBINATIONS = 10


class AvailabilityService:
    def __init__(
            self,
            room_repository: RoomRepository,
            booking_repository: BookingRepository,
            pricing_service: PricingService
    ) -> None:
        self.room_repository = room_repository
        self.booking_repository = booking_repository
        self.pricing_service = pricing_service

    async def search(
            self,
            check_in: date,
            check_out: date,
            guest_count: int
    ) -> AvailabilityResponseSchema:
        """
        Camere libere nell'intervallo, con proposte di combinazione.

        La disponibilità si ottiene sottraendo le camere occupate da quelle
        abilitate. La domanda attraversa due aggregati distinti, quindi
        nessuno dei due repository la risolve da solo: `BookingRepository`
        restituisce gli identificativi occupati, `RoomRepository` le camere
        in vendita, e la sottrazione avviene qui. Con l'inventario di un B&B
        il costo è irrilevante e i confini restano puliti.
        """
        nights = self.pricing_service.calculate_nights(check_in, check_out)

        enabled_rooms = await self.room_repository.get_all_enabled()
        occupied_ids = set(
            await self.booking_repository.get_occupied_room_ids(check_in, check_out)
        )

        free_rooms = [room for room in enabled_rooms if room.id not in occupied_ids]
        available = self._to_available_schemas(free_rooms, nights, guest_count)

        return AvailabilityResponseSchema(
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            guest_count=guest_count,
            rooms=available,
            suggested_combinations=self.build_combinations(available, guest_count),
        )

    def _to_available_schemas(
            self,
            rooms: Sequence[Room],
            nights: int,
            guest_count: int
    ) -> List[AvailableRoomSchema]:
        """Converte le entità in DTO, calcolando il subtotale per il soggiorno."""
        result: List[AvailableRoomSchema] = []

        for room in rooms:
            unit_price = Decimal(room.price)
            result.append(
                AvailableRoomSchema(
                    id=room.id,
                    name=room.name,
                    number=room.number,
                    capacity=room.capacity,
                    price_per_night=unit_price,
                    nights=nights,
                    subtotal=self.pricing_service.to_money(unit_price * nights),
                    services=[
                        RoomServiceSchema.model_validate(service) for service in room.services
                    ],
                    fits_all_guests=room.capacity >= guest_count,
                )
            )

        result.sort(key=lambda room: room.number)
        return result

    # ------------------------------------------------------------------ #
    # Combinazioni                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_combinations(
            rooms: Sequence[AvailableRoomSchema],
            guest_count: int,
            max_rooms: Optional[int] = None,
            max_results: int = _MAX_COMBINATIONS
    ) -> List[RoomCombinationSchema]:
        """
        Compone le proposte di camere che insieme ospitano tutti gli ospiti.

        Funzione **pura**: nessun database, nessuno stato. Regole applicate:

        * solo combinazioni **minimali** — se togliendo la camera più piccola
          gli ospiti ci starebbero ancora, la proposta è ridondante e viene
          scartata. Senza questa regola, con 8 camere libere si otterrebbero
          centinaia di varianti dello stesso soggiorno;
        * al massimo `booking_max_rooms_per_booking` camere per proposta;
        * ordinamento per numero di camere, poi prezzo, poi capienza sprecata:
          la prima proposta è quella che un ospite sceglierebbe comunque;
        * oltre `_EXHAUSTIVE_SEARCH_LIMIT` camere libere si passa a
          un'euristica, per non far esplodere il numero di sottoinsiemi.

        :param max_rooms: default `settings.booking_max_rooms_per_booking`.
        :return: al massimo `max_results` combinazioni, già ordinate.
        """
        if not rooms or guest_count < 1:
            return []

        if max_rooms is None:
            max_rooms = settings.booking_max_rooms_per_booking

        if len(rooms) > _EXHAUSTIVE_SEARCH_LIMIT:
            return AvailabilityService._build_combinations_greedy(
                rooms, guest_count, max_rooms
            )

        found: List[RoomCombinationSchema] = []

        for size in range(1, min(max_rooms, len(rooms)) + 1):
            for group in combinations(rooms, size):
                total_capacity = sum(room.capacity for room in group)

                if total_capacity < guest_count:
                    continue

                # Minimalità: rimuovendo la camera con la capienza più piccola
                # gli ospiti non devono più starci. Verifica O(1) che sostituisce
                # il confronto con tutti i sottoinsiemi.
                smallest = min(room.capacity for room in group)
                if size > 1 and total_capacity - smallest >= guest_count:
                    continue

                found.append(
                    AvailabilityService._to_combination(group, total_capacity, guest_count)
                )

        found.sort(key=lambda c: (c.rooms_count, c.total_price, c.wasted_capacity))
        return found[:max_results]

    @staticmethod
    def _build_combinations_greedy(
            rooms: Sequence[AvailableRoomSchema],
            guest_count: int,
            max_rooms: int
    ) -> List[RoomCombinationSchema]:
        """
        Euristica per inventari grandi.

        Propone due sole soluzioni, costruite con criteri opposti: la più
        compatta (capienze maggiori per prime, quindi meno camere) e la più
        economica (prezzi crescenti). Coprono i due desideri che un ospite
        esprime davvero, senza enumerare milioni di sottoinsiemi.
        """
        proposals: List[RoomCombinationSchema] = []
        seen = set()

        strategies = (
            # Più compatta: capienze maggiori per prime -> meno camere.
            sorted(rooms, key=lambda room: (-room.capacity, room.subtotal)),
            # Più economica: prezzi crescenti, a parità il posto letto in più.
            sorted(rooms, key=lambda room: (room.subtotal, -room.capacity)),
        )

        for ordered in strategies:
            selected: List[AvailableRoomSchema] = []
            capacity = 0

            for room in ordered:
                if capacity >= guest_count or len(selected) >= max_rooms:
                    break
                selected.append(room)
                capacity += room.capacity

            if capacity < guest_count:
                continue

            key = tuple(sorted(str(room.id) for room in selected))
            if key in seen:
                continue
            seen.add(key)

            proposals.append(
                AvailabilityService._to_combination(selected, capacity, guest_count)
            )

        proposals.sort(key=lambda c: (c.rooms_count, c.total_price, c.wasted_capacity))
        return proposals

    @staticmethod
    def _to_combination(
            group: Sequence[AvailableRoomSchema],
            total_capacity: int,
            guest_count: int
    ) -> RoomCombinationSchema:
        return RoomCombinationSchema(
            room_ids=[room.id for room in group],
            rooms_count=len(group),
            total_capacity=total_capacity,
            total_price=sum((room.subtotal for room in group), Decimal("0.00")),
            wasted_capacity=total_capacity - guest_count,
        )
