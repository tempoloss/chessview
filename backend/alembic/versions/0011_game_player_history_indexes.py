"""Add games-by-player history indexes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_game_player_history_indexes"
down_revision = "0010_clubs"
branch_labels = None
depends_on = None


IX_GAMES_WHITE_STARTED_AT = "ix_games_white_id_started_at_desc"
IX_GAMES_BLACK_STARTED_AT = "ix_games_black_id_started_at_desc"


def upgrade() -> None:
    op.create_index(
        IX_GAMES_WHITE_STARTED_AT,
        "games",
        ["white_id", sa.text("started_at DESC")],
    )
    op.create_index(
        IX_GAMES_BLACK_STARTED_AT,
        "games",
        ["black_id", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(IX_GAMES_BLACK_STARTED_AT, table_name="games")
    op.drop_index(IX_GAMES_WHITE_STARTED_AT, table_name="games")
