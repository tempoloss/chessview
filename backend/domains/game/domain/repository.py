"""
Abstract game repository   port for game persistence.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from domains.game.domain.entities import Game, Move


class AbstractGameRepository(ABC):
    """Port for game and move persistence operations."""

    @abstractmethod
    async def create(self, game: Game) -> Game:
        """Persist a new game."""
        ...

    @abstractmethod
    async def get_by_id(self, game_id: UUID) -> Game | None:
        """Retrieve a game by primary key."""
        ...

    @abstractmethod
    async def get_active_by_user(self, user_id: UUID) -> Game | None:
        """Retrieve the user's active game if one exists."""
        ...

    @abstractmethod
    async def list_active(self) -> list[Game]:
        """Return all active games."""
        ...

    @abstractmethod
    async def update(self, game: Game) -> Game:
        """Persist changes to an existing game (FEN, status, result, etc.)."""
        ...

    @abstractmethod
    async def list_by_user(self, user_id: UUID, page: int = 1, size: int = 20) -> tuple[list[Game], int]:
        """Return paginated games for a user (as white or black) and total count."""
        ...

    @abstractmethod
    async def add_move(self, move: Move) -> Move:
        """Persist a new move."""
        ...

    @abstractmethod
    async def get_moves(self, game_id: UUID) -> list[Move]:
        """Retrieve all moves for a game, ordered by move_number."""
        ...

    @abstractmethod
    async def get_move_counts(self, game_ids: list[UUID]) -> dict[UUID, int]:
        """Return move counts for a collection of game IDs."""
        ...

    @abstractmethod
    async def commit(self) -> None:
        """Commit the current unit of work."""
        ...

    @abstractmethod
    async def rollback(self) -> None:
        """Roll back the current unit of work."""
        ...
