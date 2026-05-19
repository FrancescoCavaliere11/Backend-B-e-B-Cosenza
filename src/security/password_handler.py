from pwdlib import PasswordHash

_password_context = PasswordHash.recommended()

_dummy_hash = _password_context.hash("dummypassword")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return _password_context.verify(plain_password, hashed_password)
    except Exception as e:
        raise e

def get_password_hash(password: str) -> str:
    return _password_context.hash(password)


def verify_dummy_password(password: str) -> bool:
    return verify_password(password, _dummy_hash)
