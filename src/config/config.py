import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


current_dir = os.path.dirname(os.path.realpath(__file__))
env_file_path = os.path.join(current_dir, '../..', '.env')


class Settings(BaseSettings):
    app_name: str

    max_file_size: int

    db_host: str
    db_port: int
    db_user: str
    db_password: SecretStr
    db_name: str

    jwt_secret_key: str
    jwt_algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_minutes: int

    model_config = SettingsConfigDict(env_file=env_file_path)


settings = Settings()