# Progetto FastAPI - Guida all'Inizializzazione

Questo documento descrive i passaggi necessari per configurare l'ambiente di sviluppo, la gestione delle variabili d'ambiente e la connessione asincrona al database.

---


## 1. Installazione Dipendenze

Esegui i seguenti comandi per installare le librerie necessarie tramite `pip`:

### Core e Utility
```bash
pip install fastapi pydantic uvicorn pydantic-settings
```
### Database e Migrazioni
```bash
pip install sqlalchemy alembic asyncpg
```


## 2. Configurazione Variabili d'Ambiente
Il progetto utilizza un file `.env` per gestire i dati sensibili. La classe `Settings` in `src/config.py` funge da ponte tra il file e il codice.

### File `src/config.py`

```python
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr

# Calcolo del percorso del file .env (una cartella sopra quella corrente)
current_dir = os.path.dirname(os.path.realpath(__file__))
env_file_path = os.path.join(current_dir, '..', '.env')


class Settings(BaseSettings):
   app_name: str

   # Parametri Database
   db_host: str
   db_port: int
   db_user: str
   db_password: SecretStr
   db_name: str

   model_config = SettingsConfigDict(env_file=env_file_path)


settings = Settings()
```

## 3. Setup del Database (Asincrono)
La configurazione del database è gestita in `src/database_config.py`. Qui viene creato il motore asincrono e la factory per le sessioni.

### File `src/database_config.py`

```python
from typing import AsyncGenerator
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config.config import settings as s

# URL di connessione per PostgreSQL con driver asyncpg
DATABASE_URL = (
    f"postgresql+asyncpg://{s.db_user}:{s.db_password.get_secret_value()}@{s.db_host}:{s.db_port}/{s.db_name}"
)


class Base(DeclarativeBase):
    pass


# Motore asincrono
engine = create_async_engine(
    DATABASE_URL,
    echo=True,  # Log delle query SQL in console
    future=True,  # Supporto SQLAlchemy 2.0
    pool_pre_ping=True  # Verifica la connessione prima di ogni operazione
)

# Generatore di sessioni
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)


# Dependency Injection per le rotte FastAPI
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
```

La classe ```Base``` sarà la classe madre delle classi delle nostre tabelle, e contiene i metadati che dobbiamo specificare ad alembic


## 4. Gestione Migrazioni con Alembic

### Inizializzazione
Per configurare in modalità asincrona, bisogna eseguire:

```bash
alembic init -t async migrations
```

Questo comando genera una cartella `migrations` e un file `alembic.ini`

### Configurazione `alembic.ini`
Modifica la riga dell'URL nel file `alembic.ini`:

```Ini
sqlalchemy.url = postgresql+asyncpg://%(DB_USER)s:%(DB_PASS)s@%(DB_HOST)s:%(DB_PORT)s/%(DB_NAME)s
```

### Modifica `migrations/env.py`

Nel file `env.py` all'interno della cartella `migrations`, effettua le seguenti modifiche:
1. #### Import
    ```python
    from src.config.config import settings
    from src.config.config import Base
    # Importa qui i modelli delle tue tabelle per l'autogenerate
    ```
2. ####  Target Metadata
    ```python
   target_metadata = Base.metadata
   ```
   
3. #### Iniezione Parametri
   Sotto la riga `config = context.config`, aggiungi:
    ```python
    section = config.config_ini_section
    config.set_section_option(section, "DB_HOST", str(settings.db_host))
    config.set_section_option(section, "DB_PORT", str(settings.db_port))
    config.set_section_option(section, "DB_USER", str(settings.db_user))
    config.set_section_option(section, "DB_NAME", str(settings.db_name))
    config.set_section_option(section, "DB_PASS", str(settings.db_password.get_secret_value()))
    ```
   
### Comandi Migrazione
```bash
# Crea un nuovo file di migrazione (rileva i cambiamenti nei modelli)
alembic revision --autogenerate -m "nome del commit"

# Applica le modifiche al database
alembic upgrade head
```


## 5. Comandi Utili
### Avvio Applicazione

```bash
uvicorn main:app --reload
```

### Esportazione Dipendenze

```bash
pip freeze > requirements.txt
```