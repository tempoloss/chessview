"""Game application service orchestration."""

from datetime import datetime, timezone
from uuid import UUID

from domains.game.application.commands import (
    AcceptDrawCommand,
    CreateGameCommand,
    IdentityVerificationFailureCommand,
    MakeMoveCommand,
    ResignCommand,
)
from domains.game.domain.entities import Game, Move
from domains.game.domain.exceptions import GameAccessDenied, GameNotActive, GameNotFound
from domains.game.domain.clock import active_player_id, active_remaining_time_ms, capture_clock_snapshot
from domains.game.domain.moves import apply_player_move
from domains.game.domain.outcomes import (
    abort_game,
    accept_draw,
    forfeit_game_for_identity_failure,
    pause_for_disconnect,
    resign_game,
    resume_after_reconnect,
    timeout_game,
)
from domains.game.domain.policies import DEFAULT_DISCONNECT_GRACE_SECONDS, is_meaningfully_started
from domains.game.domain.repository import AbstractGameRepository
from domains.game.domain.value_objects import GameStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def current_clock_snapshot(game: Game, now: datetime | None = None) -> dict:
    return capture_clock_snapshot(game, now or utc_now()).to_payload()


class GameService:
    """Application service for the game domain."""

    def __init__(self, game_repo: AbstractGameRepository) -> None:
        self._repo = game_repo

    async def create_game(self, cmd: CreateGameCommand) -> Game:
        """Create a new game with the standard starting position and active white clock."""
        now = utc_now()
        game = Game.new(
            white_id=cmd.white_id,
            black_id=cmd.black_id,
            time_control=cmd.time_control,
            starting_ratings=cmd.starting_ratings,
            now=now,
            rated=cmd.rated,
        )
        return await self._repo.create(game)

    async def make_move(self, cmd: MakeMoveCommand) -> tuple[Game, Move | None]:
        """Validate and apply a chess move, or end the game on time if the clock has expired."""
        game = await self._require_active_game(cmd.game_id)
        now = utc_now()
        expired_game = await self._expire_on_clock_if_needed(game, now)
        if expired_game is not None:
            return expired_game, None

        existing_moves = await self._repo.get_moves(cmd.game_id)
        move_entity = apply_player_move(
            game,
            user_id=cmd.user_id,
            uci=cmd.uci,
            move_number=len(existing_moves) + 1,
            now=now,
            previous_moves=existing_moves,
        )
        move_entity = await self._repo.add_move(move_entity)
        game = await self._repo.update(game)
        return game, move_entity

    async def resign(self, cmd: ResignCommand) -> Game:
        """Handle a player's resignation."""
        game = await self._require_active_game(cmd.game_id)
        self._require_participant(game, cmd.user_id)
        now = utc_now()
        snapshot = capture_clock_snapshot(game, now)
        resign_game(game, cmd.user_id, snapshot, now)
        return await self._repo.update(game)

    async def stop_for_identity_verification_failure(self, cmd: IdentityVerificationFailureCommand) -> Game:
        """Forfeit an active game when a participant fails identity verification."""
        game = await self._require_active_game(cmd.game_id)
        self._require_participant(game, cmd.user_id)
        now = utc_now()
        snapshot = capture_clock_snapshot(game, now)
        forfeit_game_for_identity_failure(game, cmd.user_id, snapshot, now)
        return await self._repo.update(game)

    async def accept_draw(self, cmd: AcceptDrawCommand) -> Game:
        """Finalize a draw agreement for an active game."""
        game = await self._require_active_game(cmd.game_id)
        self._require_participant(game, cmd.user_id)
        now = utc_now()
        snapshot = capture_clock_snapshot(game, now)
        accept_draw(game, snapshot, now)
        return await self._repo.update(game)

    async def get_game(self, game_id: UUID) -> Game:
        game = await self._repo.get_by_id(game_id)
        if game is None:
            raise GameNotFound()
        return game

    async def get_game_with_moves(self, game_id: UUID) -> tuple[Game, list[Move]]:
        game = await self.get_game(game_id)
        moves = await self._repo.get_moves(game_id)
        return game, moves

    async def get_user_games(self, user_id: UUID, page: int = 1, size: int = 20) -> tuple[list[Game], int]:
        return await self._repo.list_by_user(user_id, page, size)

    async def mark_disconnected(self, user_id: UUID, grace_seconds: int = DEFAULT_DISCONNECT_GRACE_SECONDS) -> Game | None:
        game = await self._repo.get_active_by_user(user_id)
        if game is None:
            return None

        now = utc_now()
        snapshot = capture_clock_snapshot(game, now)
        pause_for_disconnect(game, user_id, snapshot, now, grace_seconds)
        return await self._repo.update(game)

    async def mark_reconnected(self, user_id: UUID) -> Game | None:
        game = await self._repo.get_active_by_user(user_id)
        if game is None or game.disconnected_player_id != user_id:
            return game

        resume_after_reconnect(game, utc_now())
        return await self._repo.update(game)

    async def monitor_active_games(self) -> list[Game]:
        now = utc_now()
        updated_games: list[Game] = []
        for game in await self._repo.list_active():
            if game.disconnect_grace_deadline_at is not None and game.disconnect_grace_deadline_at <= now:
                move_count = len(await self._repo.get_moves(game.id))
                if is_meaningfully_started(move_count):
                    loser_id = game.disconnected_player_id or active_player_id(game)
                    if loser_id is not None:
                        updated_games.append(await self._finalize_timeout(game, loser_id, now, "disconnect_timeout"))
                else:
                    updated_games.append(await self._finalize_abort(game, now, "auto_abort"))
                continue

            if active_remaining_time_ms(game, now) <= 0:
                loser_id = active_player_id(game)
                if loser_id is not None:
                    updated_games.append(await self._finalize_timeout(game, loser_id, now, "clock_timeout"))

        return updated_games

    async def _expire_on_clock_if_needed(self, game: Game, now: datetime) -> Game | None:
        if active_remaining_time_ms(game, now) > 0:
            return None

        loser_id = active_player_id(game)
        if loser_id is None:
            return None
        return await self._finalize_timeout(game, loser_id, now, "clock_timeout")

    async def _finalize_timeout(self, game: Game, losing_player_id: UUID, now: datetime, reason: str) -> Game:
        snapshot = capture_clock_snapshot(game, now)
        timeout_game(game, losing_player_id, snapshot, now, reason)
        return await self._repo.update(game)

    async def _finalize_abort(self, game: Game, now: datetime, reason: str) -> Game:
        snapshot = capture_clock_snapshot(game, now)
        abort_game(game, snapshot, now, reason)
        return await self._repo.update(game)

    async def _require_active_game(self, game_id: UUID) -> Game:
        game = await self._repo.get_by_id(game_id)
        if game is None:
            raise GameNotFound()
        if game.status != GameStatus.ACTIVE:
            raise GameNotActive()
        return game

    @staticmethod
    def _require_participant(game: Game, user_id: UUID) -> None:
        if user_id not in {game.white_id, game.black_id}:
            raise GameAccessDenied()
