from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from domains.game.application.commands import AcceptDrawCommand, MakeMoveCommand
from domains.game.application.services import DEFAULT_DISCONNECT_GRACE_SECONDS, GameService
from domains.game.domain.entities import Game, Move


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
            if game.status == "active" and user_id in (game.white_id, game.black_id):
                return game
        return None

    async def list_active(self):
        return [game for game in self.games.values() if game.status == "active"]

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


@pytest.mark.asyncio
async def test_disconnect_grace_auto_aborts_game_before_meaningful_start(monkeypatch):
    repo = InMemoryGameRepository()
    service = GameService(repo)
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

    game = Game(
        white_id=white_id,
        black_id=black_id,
        last_clock_started_at=now,
        started_at=now,
    )
    await repo.create(game)

    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now)
    disconnected_game = await service.mark_disconnected(white_id)
    assert disconnected_game is not None

    deadline = now + timedelta(seconds=DEFAULT_DISCONNECT_GRACE_SECONDS + 1)
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: deadline)
    finished_games = await service.monitor_active_games()

    assert len(finished_games) == 1
    assert finished_games[0].status == "aborted"
    assert finished_games[0].result is None
    assert finished_games[0].termination_reason == "auto_abort"


@pytest.mark.asyncio
async def test_disconnect_grace_times_out_meaningfully_started_game(monkeypatch):
    repo = InMemoryGameRepository()
    service = GameService(repo)
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

    game = Game(
        white_id=white_id,
        black_id=black_id,
        last_clock_started_at=now,
        started_at=now,
    )
    await repo.create(game)

    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now)
    await service.make_move(MakeMoveCommand(game_id=game.id, user_id=white_id, uci="e2e4"))
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now + timedelta(seconds=1))
    await service.make_move(MakeMoveCommand(game_id=game.id, user_id=black_id, uci="e7e5"))

    disconnect_time = now + timedelta(seconds=2)
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: disconnect_time)
    await service.mark_disconnected(white_id)

    deadline = disconnect_time + timedelta(seconds=DEFAULT_DISCONNECT_GRACE_SECONDS + 1)
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: deadline)
    finished_games = await service.monitor_active_games()

    assert len(finished_games) == 1
    assert finished_games[0].status == "timeout"
    assert finished_games[0].result == "0-1"
    assert finished_games[0].termination_reason == "disconnect_timeout"


@pytest.mark.asyncio
async def test_clock_timeout_triggers_when_active_player_flags(monkeypatch):
    repo = InMemoryGameRepository()
    service = GameService(repo)
    white_id = uuid4()
    black_id = uuid4()
    start = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

    game = Game(
        white_id=white_id,
        black_id=black_id,
        initial_time_ms=1_000,
        white_time_ms=1_000,
        black_time_ms=1_000,
        last_clock_started_at=start,
        started_at=start,
    )
    await repo.create(game)

    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: start + timedelta(seconds=2))
    finished_games = await service.monitor_active_games()

    assert len(finished_games) == 1
    assert finished_games[0].status == "timeout"
    assert finished_games[0].result == "0-1"
    assert finished_games[0].termination_reason == "clock_timeout"


@pytest.mark.asyncio
async def test_accept_draw_finalizes_game_through_application_service(monkeypatch):
    repo = InMemoryGameRepository()
    service = GameService(repo)
    white_id = uuid4()
    black_id = uuid4()
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

    game = Game(
        white_id=white_id,
        black_id=black_id,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=now - timedelta(seconds=3),
        started_at=now - timedelta(seconds=3),
    )
    await repo.create(game)

    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: now)
    finished_game = await service.accept_draw(AcceptDrawCommand(game_id=game.id, user_id=black_id))

    assert finished_game.status == "draw"
    assert finished_game.result == "1/2-1/2"
    assert finished_game.white_time_ms == 297_000
    assert finished_game.black_time_ms == 300_000
    assert finished_game.termination_reason == "draw_agreement"
    assert finished_game.ended_at == now
    assert finished_game.last_clock_started_at is None
