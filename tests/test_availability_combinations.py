"""
Test della composizione delle proposte di soggiorno.

`build_combinations` è una funzione pura: nessun database, nessuna sessione.
"""
from decimal import Decimal
from uuid import uuid4

from src.data.schemas.booking_schema import AvailableRoomSchema
from src.service.availability_service import AvailabilityService


def _room(capacity: int, subtotal: str, number: int = 1) -> AvailableRoomSchema:
    return AvailableRoomSchema(
        id=uuid4(),
        name=f"Camera {number}",
        number=number,
        capacity=capacity,
        price_per_night=Decimal(subtotal),
        nights=1,
        subtotal=Decimal(subtotal),
        services=[],
        fits_all_guests=False,
    )


class TestCombinations:

    def test_camera_singola_sufficiente(self):
        rooms = [_room(4, "140.00", 1)]
        result = AvailabilityService.build_combinations(rooms, guest_count=4)

        assert len(result) == 1
        assert result[0].rooms_count == 1
        assert result[0].total_capacity == 4
        assert result[0].wasted_capacity == 0

    def test_combinazione_di_due_camere(self):
        doppia_a = _room(2, "100.00", 1)
        doppia_b = _room(2, "100.00", 2)

        result = AvailabilityService.build_combinations([doppia_a, doppia_b], guest_count=4)

        assert len(result) == 1
        assert result[0].rooms_count == 2
        assert result[0].total_price == Decimal("200.00")

    def test_combinazioni_ridondanti_scartate(self):
        """
        Con una doppia, un'altra doppia e una quadrupla, per 4 persone le
        proposte sensate sono due: la quadrupla da sola, oppure le due doppie.
        Le coppie doppia+quadrupla sono ridondanti — la quadrupla basta già —
        e non devono comparire.
        """
        rooms = [_room(2, "100.00", 1), _room(2, "100.00", 2), _room(4, "140.00", 3)]

        result = AvailabilityService.build_combinations(rooms, guest_count=4)

        assert len(result) == 2
        assert {c.rooms_count for c in result} == {1, 2}

        for combination in result:
            assert combination.total_capacity >= 4
            capienze = sorted(
                room.capacity for room in rooms if room.id in combination.room_ids
            )
            # Nessuna camera è superflua: togliendo la più piccola non ci si sta più.
            if combination.rooms_count > 1:
                assert combination.total_capacity - capienze[0] < 4

    def test_proposte_ordinate_per_numero_di_camere(self):
        rooms = [_room(2, "100.00", 1), _room(2, "100.00", 2), _room(4, "140.00", 3)]
        result = AvailabilityService.build_combinations(rooms, guest_count=4)

        assert result[0].rooms_count <= result[-1].rooms_count

    def test_a_parita_di_camere_vince_il_prezzo_piu_basso(self):
        economica = _room(4, "120.00", 1)
        costosa = _room(4, "180.00", 2)

        result = AvailabilityService.build_combinations([costosa, economica], guest_count=3)

        assert result[0].total_price == Decimal("120.00")

    def test_capienza_insufficiente_nessuna_proposta(self):
        rooms = [_room(2, "100.00", 1), _room(2, "100.00", 2)]
        assert AvailabilityService.build_combinations(rooms, guest_count=10) == []

    def test_nessuna_camera_libera(self):
        assert AvailabilityService.build_combinations([], guest_count=2) == []

    def test_limite_di_camere_per_proposta_rispettato(self):
        singole = [_room(1, "50.00", i) for i in range(1, 9)]

        result = AvailabilityService.build_combinations(
            singole, guest_count=6, max_rooms=3
        )

        assert result == []  # con al massimo 3 singole non si ospitano 6 persone

    def test_numero_massimo_di_proposte_rispettato(self):
        singole = [_room(1, f"{50 + i}.00", i) for i in range(1, 11)]

        result = AvailabilityService.build_combinations(
            singole, guest_count=2, max_results=3
        )

        assert len(result) == 3

    def test_capienza_sprecata_calcolata(self):
        rooms = [_room(4, "140.00", 1)]
        result = AvailabilityService.build_combinations(rooms, guest_count=2)

        assert result[0].wasted_capacity == 2

    def test_euristica_su_inventario_ampio(self):
        """Oltre la soglia si passa all'euristica, che resta rapida e sensata."""
        molte = [_room(2, f"{80 + i}.00", i) for i in range(1, 31)]

        result = AvailabilityService.build_combinations(molte, guest_count=6)

        assert result
        for combination in result:
            assert combination.total_capacity >= 6
            assert combination.rooms_count <= 5
