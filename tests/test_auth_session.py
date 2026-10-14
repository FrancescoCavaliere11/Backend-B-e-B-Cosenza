"""
Sessioni: logout, rinnovo con rotazione e lista di revoca (debito #30).

Login reali su `POST /api/v1/auth/token`. Per presentare un token preciso, per
esempio uno vecchio copiato prima del logout, lo si mette a mano nel cookie
jar del client, come farebbe chi lo ha rubato.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from sqlalchemy import update

from src.data.enumerators import TokenRevocationReason, TokenType
from src.data.model.revoked_token import RevokedToken
from src.data.model.user import User
from src.data.repository.revoked_token_repository import RevokedTokenRepository
from src.service.auth_service import build_auth_service

AUTH = "/api/v1/auth"
ME = "/api/v1/users/me"


def _cookie(response, name: str) -> str:
    """
    Valore di un cookie impostato dalla risposta.

    Si leggono le intestazioni invece del cookie jar della risposta: così si
    vede esattamente cosa il backend ha impostato, percorso compreso.
    """
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}=") and "Max-Age=0" not in header:
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"Cookie {name} non impostato")


def _cookies(access=None, refresh=None) -> dict:
    """
    Intestazione `Cookie` con i token indicati, e solo quelli.

    Il cookie jar del client va svuotato prima: è così che si presenta un token
    preciso, per esempio uno vecchio copiato prima del logout.
    """
    parts = [f"{name}={value}" for name, value in (("access_token", access), ("refresh_token", refresh)) if value]
    return {"Cookie": "; ".join(parts)} if parts else {}


async def _post(client, path: str, access=None, refresh=None):
    client.cookies.clear()
    return await client.post(f"{AUTH}{path}", headers=_cookies(access, refresh))


async def _login(client, email: str, password: str):
    client.cookies.clear()
    response = await client.post(f"{AUTH}/token", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return _cookie(response, "access_token"), _cookie(response, "refresh_token")


async def _refresh(client, access=None, refresh=None):
    return await _post(client, "/refresh", access, refresh)


async def _logout(client, access=None, refresh=None):
    return await _post(client, "/logout", access, refresh)


async def _me_status(client, access) -> int:
    client.cookies.clear()
    return (await client.get(ME, headers=_cookies(access))).status_code


def _jti(token: str) -> UUID:
    return UUID(jwt.decode(token, options={"verify_signature": False})["jti"])


def _set_cookie_headers(response, name: str):
    return [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{name}=")]


# ===========================================================================
# Login
# ===========================================================================

async def test_ogni_token_ha_un_jti_diverso(api_client, admin_user, user_password):
    access, refresh = await _login(api_client, admin_user.email, user_password)
    access2, _ = await _login(api_client, admin_user.email, user_password)

    assert len({_jti(access), _jti(refresh), _jti(access2)}) == 3


async def test_il_refresh_token_viaggia_solo_verso_le_rotte_di_autenticazione(api_client, admin_user, user_password):
    api_client.cookies.clear()
    response = await api_client.post(f"{AUTH}/token", data={"username": admin_user.email, "password": user_password})

    refresh_headers = _set_cookie_headers(response, "refresh_token")
    assert len(refresh_headers) == 1
    assert "Path=/api/v1/auth" in refresh_headers[0]
    assert "Max-Age=0" not in refresh_headers[0]


# ===========================================================================
# Logout
# ===========================================================================

async def test_dopo_il_logout_il_vecchio_access_token_non_vale(api_client, admin_user, user_password):
    access, refresh = await _login(api_client, admin_user.email, user_password)
    assert await _me_status(api_client, access) == 200

    assert (await _logout(api_client, access, refresh)).status_code == 204

    assert await _me_status(api_client, access) == 401


async def test_il_logout_cancella_i_cookie(api_client, admin_user, user_password):
    access, refresh = await _login(api_client, admin_user.email, user_password)

    response = await _logout(api_client, access, refresh)

    assert all("Max-Age=0" in h for h in _set_cookie_headers(response, "access_token"))
    assert any("Path=/api/v1/auth" in h and "Max-Age=0" in h for h in _set_cookie_headers(response, "refresh_token"))


async def test_il_logout_e_idempotente(api_client):
    assert (await _logout(api_client)).status_code == 204
    assert (await _logout(api_client, "non-un-token", "nemmeno-questo")).status_code == 204


async def test_un_refresh_token_dopo_il_logout_non_vale_e_non_tocca_le_altre_sessioni(
        api_client, admin_user, user_password
):
    """Due sessioni (es. due computer): chiuderne una lascia aperta l'altra."""
    access_a, refresh_a = await _login(api_client, admin_user.email, user_password)
    access_b, _ = await _login(api_client, admin_user.email, user_password)
    await _logout(api_client, access_a, refresh_a)

    assert (await _refresh(api_client, refresh=refresh_a)).status_code == 401
    assert await _me_status(api_client, access_b) == 200


# ===========================================================================
# Rinnovo con rotazione
# ===========================================================================

async def test_il_refresh_emette_una_coppia_nuova_e_revoca_la_vecchia(api_client, admin_user, user_password):
    access, refresh = await _login(api_client, admin_user.email, user_password)

    response = await _refresh(api_client, access, refresh)

    assert response.status_code == 200
    new_access, new_refresh = _cookie(response, "access_token"), _cookie(response, "refresh_token")
    assert {new_access, new_refresh}.isdisjoint({access, refresh})
    assert await _me_status(api_client, new_access) == 200
    assert await _me_status(api_client, access) == 401


async def test_un_refresh_ripresentato_subito_e_respinto_senza_revocare_tutto(
        api_client, admin_user, user_password
):
    """Due schede che rinnovano insieme: la seconda perde, nessuno viene buttato fuori."""
    access, refresh = await _login(api_client, admin_user.email, user_password)
    new_access = _cookie(await _refresh(api_client, access, refresh), "access_token")

    assert (await _refresh(api_client, refresh=refresh)).status_code == 401
    assert await _me_status(api_client, new_access) == 200


async def test_un_refresh_riusato_dopo_tempo_revoca_tutte_le_sessioni(
        api_client, admin_user, user_password, session
):
    """Il refresh token è stato copiato: anche la coppia emessa dopo non deve valere più."""
    access, refresh = await _login(api_client, admin_user.email, user_password)
    rotated = await _refresh(api_client, access, refresh)
    new_access, new_refresh = _cookie(rotated, "access_token"), _cookie(rotated, "refresh_token")
    await session.execute(
        update(RevokedToken)
        .where(RevokedToken.jti == _jti(refresh))
        .values(revoked_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await session.commit()

    assert (await _refresh(api_client, refresh=refresh)).status_code == 401

    assert await _me_status(api_client, new_access) == 401
    assert (await _refresh(api_client, refresh=new_refresh)).status_code == 401
    # L'utente vero rientra con la password, dal secondo successivo alla revoca.
    await asyncio.sleep(1.1)
    fresh_access, _ = await _login(api_client, admin_user.email, user_password)
    assert await _me_status(api_client, fresh_access) == 200


async def test_il_refresh_non_accetta_un_access_token(api_client, admin_user, user_password):
    access, _ = await _login(api_client, admin_user.email, user_password)

    assert (await _refresh(api_client, refresh=access)).status_code == 401


async def test_il_refresh_senza_cookie_risponde_401(api_client):
    response = await _refresh(api_client)

    assert response.status_code == 401
    assert response.json() == {"message": "Sessione scaduta o non valida"}


# ===========================================================================
# Revoca globale e pulizia
# ===========================================================================

async def test_un_token_emesso_prima_della_revoca_globale_non_vale(api_client, admin_user, user_password, session):
    access, _ = await _login(api_client, admin_user.email, user_password)
    await session.execute(
        update(User)
        .where(User.id == admin_user.id)
        .values(tokens_valid_after=datetime.now(timezone.utc) + timedelta(minutes=1))
    )
    await session.commit()

    assert await _me_status(api_client, access) == 401


async def test_la_pulizia_elimina_solo_le_revoche_scadute(session, admin_user):
    repository = RevokedTokenRepository(session)
    now = datetime.now(timezone.utc)
    expired, valid = uuid4(), uuid4()
    for jti, expires_at in ((expired, now - timedelta(minutes=1)), (valid, now + timedelta(days=1))):
        await repository.revoke(jti, admin_user.id, TokenType.ACCESS, TokenRevocationReason.LOGOUT, expires_at)
    await session.commit()

    assert await build_auth_service(session).purge_expired_revocations() == 1

    assert not await repository.is_revoked(expired)
    assert await repository.is_revoked(valid)


# ===========================================================================
# Log
# ===========================================================================

async def test_nei_log_non_finisce_nessun_token(api_client, admin_user, user_password, caplog):
    with caplog.at_level(logging.INFO):
        access, refresh = await _login(api_client, admin_user.email, user_password)
        rotated = await _refresh(api_client, access, refresh)
        new_access, new_refresh = _cookie(rotated, "access_token"), _cookie(rotated, "refresh_token")
        await _logout(api_client, new_access, new_refresh)

    assert "Sessione rinnovata" in caplog.text
    assert "Logout: utente_id=" in caplog.text
    for token in (access, refresh, new_access, new_refresh):
        assert token not in caplog.text
        assert str(_jti(token)) not in caplog.text
