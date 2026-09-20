"""
Generazione e verifica dei token monouso legati a una prenotazione
(conferma email, gestione, cancellazione).

Modello di sicurezza, identico a quello delle password: il valore in chiaro
esiste **solo in memoria**, il tempo di comporre l'email. Nel database finisce
esclusivamente il suo hash SHA-256. Una compromissione del database non
consente quindi di confermare o cancellare prenotazioni altrui.

SHA-256 senza salt né stretching è la scelta corretta *in questo caso*: il
token è già 256 bit di entropia crittografica generati da `secrets`, non una
password scelta da un umano. Non è attaccabile per forza bruta né con
dizionari, quindi Argon2 aggiungerebbe solo latenza su ogni clic.
"""
import hashlib
import hmac
import secrets
from typing import Tuple

#: Byte di entropia del token. 32 byte -> 43 caratteri URL-safe.
_TOKEN_BYTES = 32


def generate_booking_token() -> Tuple[str, str]:
    """
    Genera un nuovo token monouso.

    :return: tupla `(valore_in_chiaro, hash)`. Il primo va inserito nel link
        dell'email e poi dimenticato; il secondo è l'unico da persistere.
    """
    plain_token = secrets.token_urlsafe(_TOKEN_BYTES)
    return plain_token, hash_booking_token(plain_token)


def hash_booking_token(plain_token: str) -> str:
    """
    Calcola l'hash con cui cercare il token nel database.

    :param plain_token: valore ricevuto dal client.
    :return: digest SHA-256 esadecimale (64 caratteri).
    """
    return hashlib.sha256(plain_token.encode("utf-8")).hexdigest()


def tokens_match(plain_token: str, stored_hash: str) -> bool:
    """
    Confronta un token con l'hash memorizzato a tempo costante.

    La ricerca avviene comunque per hash (quindi tramite indice), ma dove
    serve un confronto diretto si usa `compare_digest` invece di `==`: un
    confronto che termina al primo byte diverso è misurabile e, ripetuto,
    consente di ricostruire il valore atteso.
    """
    return hmac.compare_digest(hash_booking_token(plain_token), stored_hash)
