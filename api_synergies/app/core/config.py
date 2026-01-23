from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Ontology-API"
    VERSION: str = "1.0.0"
    ENV: str = "dev"
    LOG_LEVEL: str = "INFO"
    GRAPHDB_TIMEOUT_SEC: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

settings = Settings()
