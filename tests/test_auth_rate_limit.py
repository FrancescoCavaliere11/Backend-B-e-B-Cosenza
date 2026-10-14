"""
Protezione del login contro i tentativi ripetuti (debito #19).

Login reali su `POST /api/v1/auth/token`. Per simulare IP diversi si attiva
`trusted_proxy_count` e si manda `X-Forwarded-For`, come farebbe un proxy.
Le soglie sono quelle di default: 20 tentativi per IP, 5 fallimenti per
account + IP, 10 fallimenti per account, in 15 minuti.
"""
import logging

import pytest

from src.config.config import settings

LOGIN = "/api/v1/auth/token"
WRONG = "Sbagliata1!"
PER_ACCOUNT_IP = settings.rate_limit_login_failures_per_account_ip
PER_ACCOUNT = settings.rate_limit_login_failures_per_account
PER_IP = settings.rate_limit_login_per_ip


@pytest.fixture(autouse=True)
def behind_proxy(monkeypatch):
    """Rende significativo `X-Forwarded-For`, per simulare IP diversi."""
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)


async def _login(client, email: str, password: str, ip: str = "203.0.113.1"):
    return await client.post(
        LOGIN, data={"username": email, "password": password}, headers={"X-Forwarded-For": ip}
    )


async def _fail(client, email: str, times: int, ip: str = "203.0.113.1") -> None:
    for _ in range(times):
        assert (await _login(client, email, WRONG, ip)).status_code == 401


# ===========================================================================
# Comportamento di base
# ===========================================================================

async def test_login_corretto(api_client, admin_user, user_password):
    response = await _login(api_client, admin_user.email, user_password)

    assert response.status_code == 200
    assert "access_token" in response.cookies


async def test_password_sbagliata_401_con_www_authenticate(api_client, admin_user):
    response = await _login(api_client, admin_user.email, WRONG)

    assert response.status_code == 401
    assert response.json() == {"message": "Email o password errati"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


# ===========================================================================
# Blocchi
# ===========================================================================

async def test_troppi_errori_bloccano_anche_la_password_giusta(api_client, admin_user, user_password):
    await _fail(api_client, admin_user.email, PER_ACCOUNT_IP)

    response = await _login(api_client, admin_user.email, user_password)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert "Troppi tentativi" in response.json()["message"]


async def test_l_utente_vero_entra_da_un_altro_ip(api_client, admin_user, user_password):
    """Chi sbaglia da un computer blocca sé stesso, non l'amministratore."""
    await _fail(api_client, admin_user.email, PER_ACCOUNT_IP, ip="198.51.100.66")

    response = await _login(api_client, admin_user.email, user_password, ip="203.0.113.1")

    assert response.status_code == 200


async def test_errori_da_molti_ip_bloccano_l_account(api_client, admin_user, user_password):
    """*Credential stuffing*: un tentativo per IP, ma tutti contro lo stesso account."""
    for n in range(PER_ACCOUNT):
        await _fail(api_client, admin_user.email, 1, ip=f"198.51.100.{n + 1}")

    response = await _login(api_client, admin_user.email, user_password, ip="203.0.113.250")

    assert response.status_code == 429


async def test_un_account_inesistente_si_comporta_allo_stesso_modo(api_client, admin_user):
    """Il limite non deve rivelare quali email sono registrate."""
    ghost = "nessuno@example.com"
    real_401 = await _login(api_client, admin_user.email, WRONG, ip="198.51.100.1")
    ghost_401 = await _login(api_client, ghost, WRONG, ip="198.51.100.2")
    assert (real_401.status_code, real_401.json()) == (ghost_401.status_code, ghost_401.json())

    await _fail(api_client, admin_user.email, PER_ACCOUNT_IP - 1, ip="198.51.100.1")
    await _fail(api_client, ghost, PER_ACCOUNT_IP - 1, ip="198.51.100.2")
    real_429 = await _login(api_client, admin_user.email, WRONG, ip="198.51.100.1")
    ghost_429 = await _login(api_client, ghost, WRONG, ip="198.51.100.2")

    assert real_429.status_code == ghost_429.status_code == 429
    assert real_429.json() == ghost_429.json()


async def test_un_login_riuscito_azzera_i_propri_errori(api_client, admin_user, user_password):
    await _fail(api_client, admin_user.email, PER_ACCOUNT_IP - 2)
    assert (await _login(api_client, admin_user.email, user_password)).status_code == 200

    await _fail(api_client, admin_user.email, PER_ACCOUNT_IP - 2)

    assert (await _login(api_client, admin_user.email, user_password)).status_code == 200


async def test_limite_per_ip_su_account_diversi(api_client, admin_user, user_password):
    """Un solo IP che prova molte email diverse: lo ferma il limite per IP."""
    for n in range(PER_IP):
        await _login(api_client, f"utente{n}@example.com", WRONG)

    response = await _login(api_client, admin_user.email, user_password)

    assert response.status_code == 429


async def test_maiuscole_e_spazi_non_aggirano_il_limite(api_client, admin_user, user_password):
    for n in range(PER_ACCOUNT_IP):
        variant = admin_user.email.upper() if n % 2 else f"  {admin_user.email} "
        await _login(api_client, variant, WRONG)

    response = await _login(api_client, admin_user.email, user_password)

    assert response.status_code == 429


# ===========================================================================
# Log
# ===========================================================================

async def test_i_log_mascherano_l_email_e_non_mostrano_la_password(api_client, admin_user, user_password, caplog):
    with caplog.at_level(logging.INFO):
        await _fail(api_client, admin_user.email, PER_ACCOUNT_IP)
        await _login(api_client, admin_user.email, user_password)  # bloccato
        await _login(api_client, admin_user.email, user_password, ip="198.51.100.9")  # riuscito

    assert "Login fallito: utente=a***@example.com ip=203.0.113.1" in caplog.text
    assert "Login bloccato: utente=a***@example.com" in caplog.text
    assert "Login riuscito: utente_id=" in caplog.text
    assert admin_user.email not in caplog.text
    assert WRONG not in caplog.text
    assert user_password not in caplog.text
