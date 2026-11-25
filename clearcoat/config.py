from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://clearcoat:replace_me@localhost:5432/clearcoat"
    ghl_webhook_public_key: str = ""
    ghl_allowed_location_ids: str = ""
    ghl_write_enabled: bool = False
    customer_send_enabled: bool = False
    app_env: str = "development"
    ghl_private_token: str = ""
    ghl_location_id: str = ""
    sync_trigger_secret: str = ""

    @property
    def allowed_locations(self) -> set[str]:
        return {part.strip() for part in self.ghl_allowed_location_ids.split(",") if part.strip()}


settings = Settings()

