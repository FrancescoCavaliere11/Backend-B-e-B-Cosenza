from src.config.schemas_config import CustomModel


class AuthResponseSchema(CustomModel):
    access_token: str
    refresh_token: str
    token_type: str