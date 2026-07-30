"""
SQLAlchemy ORM models for games and moves tables.

Domain layer must never import this module.
"""

from datetime import datetime
import uuid

from sqlalchemy import Boolean, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.database import Base
from infrastructure.orm import created_at_column, utc_timestamp_column, uuid_primary_key, uuid_reference


TIME_CONTROL_NAME_LENGTH = 20
GAME_STATUS_LENGTH = 20
GAME_RESULT_LENGTH = 10
GAME_TERMINATION_REASON_LENGTH = 40
UCI_MOVE_LENGTH = 5


class GameModel(Base):
    """ORM model for the `games` table."""

    __tablename__ = "games"

    # A game has two player columns, so "my games, newest first" is an OR over
    # white_id and black_id. PostgreSQL answers that with a BitmapOr over one
    # index per branch, which is why this is two indexes and not one composite.
    # They are declared here as well as in 0011_game_player_history_indexes so
    # `alembic check` does not read them as indexes the models never asked for.
    __table_args__ = (
        Index("ix_games_white_id_started_at_desc", "white_id", text("started_at DESC")),
        Index("ix_games_black_id_started_at_desc", "black_id", text("started_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_primary_key()

    white_id: Mapped[uuid.UUID] = uuid_reference("users.id")
    black_id: Mapped[uuid.UUID] = uuid_reference("users.id")

    time_control_name: Mapped[str] = mapped_column(String(TIME_CONTROL_NAME_LENGTH), nullable=False)
    initial_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    increment_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    white_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    black_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    last_clock_started_at: Mapped[datetime | None] = utc_timestamp_column(nullable=True)
    disconnected_player_id: Mapped[uuid.UUID | None] = uuid_reference("users.id", nullable=True)
    disconnect_grace_deadline_at: Mapped[datetime | None] = utc_timestamp_column(nullable=True)

    rated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    white_rating_before: Mapped[int] = mapped_column(Integer, nullable=False)
    black_rating_before: Mapped[int] = mapped_column(Integer, nullable=False)
    white_rating_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    black_rating_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(GAME_STATUS_LENGTH), nullable=False)
    result: Mapped[str | None] = mapped_column(String(GAME_RESULT_LENGTH), nullable=True)
    fen: Mapped[str] = mapped_column(Text, nullable=False)
    pgn: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = created_at_column()
    ended_at: Mapped[datetime | None] = utc_timestamp_column(nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(GAME_TERMINATION_REASON_LENGTH), nullable=True)
    rating_applied_at: Mapped[datetime | None] = utc_timestamp_column(nullable=True)

    moves: Mapped[list["MoveModel"]] = relationship(
        "MoveModel",
        back_populates="game",
        order_by="MoveModel.move_number",
    )


class MoveModel(Base):
    """ORM model for the `moves` table."""

    __tablename__ = "moves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[uuid.UUID] = uuid_reference("games.id")
    user_id: Mapped[uuid.UUID] = uuid_reference("users.id")
    uci: Mapped[str] = mapped_column(String(UCI_MOVE_LENGTH), nullable=False)
    fen_after: Mapped[str] = mapped_column(Text, nullable=False)
    move_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    game: Mapped[GameModel] = relationship("GameModel", back_populates="moves")
