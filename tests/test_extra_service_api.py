"""
Test di integrazione sui servizi extra: form non validi (lacuna B15).

I form di camere e servizi extra viaggiano come `multipart`, con i dati in un
campo JSON. Fino al 14/10/2026 i servizi extra costruivano lo schema a mano, e
un campo sbagliato usciva come `500` invece che come `422`.
"""
import json

EXTRAS = "/api/v1/extra-service/"
ROOMS = "/api/v1/room/"

#: PNG di 1×1 pixel: il backend controlla il tipo reale del file, non il nome.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _create(client, form):
    raw = form if isinstance(form, str) else json.dumps(form)
    return await client.post(
        EXTRAS, data={"extra_service_form": raw}, files={"image": ("servizio.png", PNG_1X1, "image/png")}
    )


async def _update(client, form):
    raw = form if isinstance(form, str) else json.dumps(form)
    return await client.put(EXTRAS, data={"extra_service_form": raw})


async def _names(client) -> list:
    response = await client.get(EXTRAS)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()]


async def test_creazione_valida(admin_client):
    response = await _create(admin_client, {"name": "Transfer aeroporto", "description": "Su prenotazione"})

    assert response.status_code == 201, response.text
    assert "Transfer aeroporto" in await _names(admin_client)


async def test_nome_troppo_corto_e_un_422_e_non_crea_nulla(admin_client):
    response = await _create(admin_client, {"name": "A"})

    assert response.status_code == 422, response.text
    assert any(detail["loc"][-1] == "name" for detail in response.json()["details"])
    assert "A" not in await _names(admin_client)


async def test_descrizione_troppo_lunga_in_modifica_e_un_422(admin_client):
    created = (await _create(admin_client, {"name": "Colazione", "description": "Dolce e salata"})).json()

    response = await _update(admin_client, {"id": created["id"], "name": "Colazione", "description": "x" * 201})

    assert response.status_code == 422, response.text
    extras = (await admin_client.get(EXTRAS)).json()
    assert next(item for item in extras if item["id"] == created["id"])["description"] == "Dolce e salata"


async def test_modifica_senza_id_e_un_422(admin_client):
    response = await _update(admin_client, {"name": "Colazione"})

    assert response.status_code == 422, response.text


async def test_json_che_non_e_un_oggetto_e_un_422(admin_client):
    for send in (_create, _update):
        response = await send(admin_client, "[1, 2]")
        assert response.status_code == 422, response.text
        assert response.json()["message"] == "I dati del form devono essere un oggetto JSON"


async def test_json_malformato_e_un_422(admin_client):
    response = await _create(admin_client, "{non è json")

    assert response.status_code == 422, response.text


async def test_anche_le_camere_rifiutano_un_json_che_non_e_un_oggetto(admin_client):
    response = await admin_client.put(ROOMS, data={"room_form": '"testo"'})

    assert response.status_code == 422, response.text
