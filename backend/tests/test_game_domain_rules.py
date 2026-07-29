from datetime import datetime, timedelta, timezone
from uuid import uuid4

import chess
import pytest

from domains.game.application.commands import MakeMoveCommand
from domains.game.application.services import GameService
from domains.game.domain.clock import capture_clock_snapshot
from domains.game.domain.entities import Game, Move
from domains.game.domain.outcomes import pause_for_disconnect
from domains.game.domain.value_objects import GameResult, GameStatus, StartingRatings
from shared.time_controls import get_time_control_preset


class InMemoryGameRepository:
    def __init__(self) -> None:
        self.games: dict = {}
        self.moves: dict = {}

    async def create(self, game: Game) -> Game:
        self.games[game.id] = game
        self.moves[game.id] = []
        return game

    async def get_by_id(self, game_id):
        return self.games.get(game_id)

    async def get_active_by_user(self, user_id):
        for game in self.games.values():
            if game.status == GameStatus.ACTIVE and user_id in (game.white_id, game.black_id):
                return game
        return None

    async def list_active(self):
        return [game for game in self.games.values() if game.status == GameStatus.ACTIVE]

    async def update(self, game: Game) -> Game:
        self.games[game.id] = game
        return game

    async def list_by_user(self, user_id, page=1, size=20):
        games = [game for game in self.games.values() if user_id in (game.white_id, game.black_id)]
        return games, len(games)

    async def add_move(self, move: Move) -> Move:
        current_moves = self.moves.setdefault(move.game_id, [])
        move.id = len(current_moves) + 1
        current_moves.append(move)
        return move

    async def get_moves(self, game_id):
        return list(self.moves.get(game_id, []))

    async def get_move_counts(self, game_ids):
        return {game_id: len(self.moves.get(game_id, [])) for game_id in game_ids}

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_capture_clock_snapshot_reports_running_white_clock():
    now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    game = Game(
        white_id=uuid4(),
        black_id=uuid4(),
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=now - timedelta(seconds=3),
        started_at=now - timedelta(seconds=3),
    )

    snapshot = capture_clock_snapshot(game, now)

    assert snapshot.active_color == "white"
    assert snapshot.white_time_ms == 297_000
    assert snapshot.black_time_ms == 300_000
    assert snapshot.is_paused is False


def test_pause_for_disconnect_freezes_clock_and_marks_grace_deadline():
    user_id = uuid4()
    other_id = uuid4()
    now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    game = Game(
        white_id=user_id,
        black_id=other_id,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=now - timedelta(seconds=4),
        started_at=now - timedelta(seconds=4),
    )
    snapshot = capture_clock_snapshot(game, now)

    pause_for_disconnect(game, user_id, snapshot, now, 20)

    assert game.last_clock_started_at is None
    assert game.disconnected_player_id == user_id
    assert game.disconnect_grace_deadline_at == now + timedelta(seconds=20)
    assert game.white_time_ms == 296_000
    assert game.black_time_ms == 300_000


def test_new_game_uses_explicit_time_control_and_starting_ratings():
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    time_control = get_time_control_preset("3+2")

    assert time_control is not None

    game = Game.new(
        white_id=white_id,
        black_id=black_id,
        time_control=time_control,
        starting_ratings=StartingRatings(white=1510, black=1490),
        now=now,
    )

    assert game.time_control_name == "3+2"
    assert game.initial_time_ms == 180_000
    assert game.increment_ms == 2_000
    assert game.white_time_ms == 180_000
    assert game.black_time_ms == 180_000
    assert game.white_rating_before == 1510
    assert game.black_rating_before == 1490
    assert game.last_clock_started_at == now


@pytest.mark.asyncio
async def test_threefold_repetition_ends_game_with_repetition_reason(monkeypatch):
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    repo = InMemoryGameRepository()
    service = GameService(repo)
    game = Game(
        white_id=white_id,
        black_id=black_id,
        last_clock_started_at=now,
        started_at=now,
    )
    await repo.create(game)
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now)

    repeated_knight_shuffle = [
        (white_id, "g1f3"),
        (black_id, "g8f6"),
        (white_id, "f3g1"),
        (black_id, "f6g8"),
        (white_id, "g1f3"),
        (black_id, "g8f6"),
        (white_id, "f3g1"),
        (black_id, "f6g8"),
    ]

    for user_id, uci in repeated_knight_shuffle:
        finished_game, _move = await service.make_move(
            MakeMoveCommand(game_id=game.id, user_id=user_id, uci=uci)
        )

    assert finished_game.status == GameStatus.DRAW
    assert finished_game.result == GameResult.DRAW
    assert finished_game.termination_reason == "repetition"


@pytest.mark.asyncio
async def test_replay_divergence_falls_back_to_authoritative_fen(monkeypatch):
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
    board = chess.Board()
    board.push_uci("e2e4")
    repo = InMemoryGameRepository()
    service = GameService(repo)
    game = Game(
        white_id=white_id,
        black_id=black_id,
        fen=board.fen(),
        last_clock_started_at=now,
        started_at=now,
    )
    await repo.create(game)
    repo.moves[game.id] = [
        Move(game_id=game.id, user_id=white_id, uci="e2e4", fen_after=game.fen, move_number=1),
        Move(game_id=game.id, user_id=white_id, uci="e2e4", fen_after=game.fen, move_number=1),
    ]
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now)

    finished_game, move = await service.make_move(
        MakeMoveCommand(game_id=game.id, user_id=black_id, uci="e7e5")
    )

    board.push_uci("e7e5")
    assert move is not None
    assert move.uci == "e7e5"
    assert finished_game.status == GameStatus.ACTIVE
    assert finished_game.fen == board.fen()