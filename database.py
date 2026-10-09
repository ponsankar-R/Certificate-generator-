import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Load .env from current working directory and from project directory
load_dotenv()
project_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(project_env):
    load_dotenv(project_env)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is missing. Copy .env.example to .env and set your Neon PostgreSQL connection string."
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)

Base = declarative_base()