"""Alembic helpers for applying and authoring schema migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

from app.config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_PATH = PROJECT_ROOT / "alembic"
BASELINE_REVISION = "0001_baseline"
HEAD_REVISION = "0011_game_player_history_indexes"
BASELINE_TABLES = frozenset(
    {
        "chat_messages",
        "games",
        "moves",
        "puzzle_attempts",
        "puzzles",
        "tournament_pairings",
        "tournament_players",
        "tournament_rounds",
        "tournaments",
        "users",
    }
)
LEGACY_SCHEMA_FIX_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_games_disconnected_player_id_users'
    ) THEN
        ALTER TABLE games
        ADD CONSTRAINT fk_games_disconnected_player_id_users
        FOREIGN KEY (disconnected_player_id)
        REFERENCES users (id);
    END IF;
END $$;
"""

SYNC_DRIVER_BY_ASYNC_DRIVER = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
}


def to_migration_database_url(database_url: str) -> str:
    """Translate an application URL into a sync URL usable by Alembic."""
    parsed = make_url(database_url)
    sync_driver = SYNC_DRIVER_BY_ASYNC_DRIVER.get(parsed.drivername)
    if sync_driver is None:
        return render_url(parsed)
    return render_url(parsed.set(drivername=sync_driver))


def build_alembic_config(database_url: str | None = None) -> Config:
    """Create a configured Alembic Config rooted in this backend project."""
    resolved_database_url = to_migration_database_url(database_url or settings.DATABASE_URL)
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    config.set_main_option("sqlalchemy.url", resolved_database_url)
    config.attributes["database_url"] = resolved_database_url
    return config


async def run_database_migrations(database_url: str | None = None) -> None:
    """Upgrade the configured database to the latest Alembic revision."""
    config = build_alembic_config(database_url)
    await asyncio.to_thread(_upgrade_database, config)


def render_url(url: URL) -> str:
    """Render a SQLAlchemy URL without masking credentials."""
    return url.render_as_string(hide_password=False)


def should_stamp_baseline(existing_tables: set[str]) -> bool:
    """Return True when a legacy current-schema database should be adopted into Alembic."""
    return bool(existing_tables) and "alembic_version" not in existing_tables and BASELINE_TABLES.issubset(existing_tables)


def validate_existing_schema(existing_tables: set[str]) -> None:
    """Fail fast for partial unmanaged schemas that are unsafe to auto-adopt."""
    if not existing_tables or "alembic_version" in existing_tables or BASELINE_TABLES.issubset(existing_tables):
        return
    raise RuntimeError(
        "Database contains pre-existing tables but does not match the tracked baseline migration. "
        "Back up and recreate the database, or manually reconcile it before retrying migrations."
    )


def apply_legacy_schema_fixes(connection) -> None:
    """Bring pre-Alembic current-schema databases up to the tracked head shape."""
    connection.execute(sa.text(LEGACY_SCHEMA_FIX_SQL))


def _upgrade_database(config: Config) -> None:
    _stamp_legacy_database_if_needed(config)
    command.upgrade(config, "head")


def _stamp_legacy_database_if_needed(config: Config) -> None:
    database_url = config.get_main_option("sqlalchemy.url")
    if not database_url:
        return

    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            existing_tables = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()

    validate_existing_schema(existing_tables)
    if should_stamp_baseline(existing_tables):
        engine = create_engine(database_url, poolclass=NullPool)
        try:
            with engine.begin() as connection:
                apply_legacy_schema_fixes(connection)
        finally:
            engine.dispose()
        command.stamp(config, HEAD_REVISION)
