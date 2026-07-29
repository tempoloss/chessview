import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/chessview")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("STORAGE_DIR", "storage")
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "true")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("BACKEND_URL", "http://localhost:8000")
os.environ.setdefault("FRONTEND_URL", "http://localhost:5173")

from domains.game.application.commands import MakeMoveCommand
from domains.game.application.services import GameService
from domains.game.domain.policies import DEFAULT_GAME_START_FEN
from domains.game.domain.value_objects import GameStatus
from domains.game.infrastructure.models import GameModel, MoveModel
from domains.game.infrastructure.repository import SqlAlchemyGameRepository


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return list(self._values)


class _Result:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalar_one_or_none(self) -> object | None:
        return self._values[0] if self._values else None

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._values)


class _CommittedStore:
    def __init__(self) -> None:
        self.games: dict[object, GameModel] = {}
        self.moves: list[MoveModel] = []
        self.next_move_id = 1


class _TransactionalSession:
    def __init__(self, store: _CommittedStore) -> None:
        self._store = store
        self._load_committed_state()
        self._pending: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, model: object) -> None:
        self._pending.append(model)

    async def execute(self, statement: object) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        if entity is GameModel:
            return _Result(list(self._games.values()))
        if entity is MoveModel:
            moves = sorted(self._moves, key=lambda move: move.move_number)
            return _Result(moves)
        raise AssertionError(f"Unexpected select entity: {entity!r}")

    async def flush(self) -> None:
        self._stage_pending_models()

    async def commit(self) -> None:
        self._stage_pending_models()
        self._store.games = {game_id: _clone_model(model) for game_id, model in self._games.items()}
        self._store.moves = [_clone_model(model) for model in self._moves]
        self.commits += 1

    async def rollback(self) -> None:
        self._load_committed_state()
        self._pending = []
        self.rollbacks += 1

    async def refresh(self, model: object) -> None:
        # Defaults are assigned by flush/commit in this test double.
        assert getattr(model, "id", None) is not None

    def _load_committed_state(self) -> None:
        self._games = {game_id: _clone_model(model) for game_id, model in self._store.games.items()}
        self._moves = [_clone_model(model) for model in self._store.moves]

    def _stage_pending_models(self) -> None:
        for model in self._pending:
            if isinstance(model, MoveModel):
                if model.id is None:
                    model.id = self._store.next_move_id
                    self._store.next_move_id += 1
                if model.created_at is None:
                    model.created_at = datetime.now(timezone.utc)
                if not any(existing.id == model.id for existing in self._moves):
                    self._moves.append(model)
            elif isinstance(model, GameModel):
                self._games[model.id] = model
            else:
                raise AssertionError(f"Unexpected pending model: {model!r}")
        self._pending = []


class _GameUpdateFailsRepository(SqlAlchemyGameRepository):
    async def update(self, game):
        raise RuntimeError("game update failed")


def _clone_model(model):
    values = {column.name: getattr(model, column.name) for column in model.__table__.columns}
    return type(model)(**values)


@pytest.mark.asyncio
async def test_make_move_rolls_back_move_when_game_update_fails(monkeypatch):
    store = _CommittedStore()
    game_id = uuid4()
    white_id = uuid4()
    black_id = uuid4()
    started_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    store.games[game_id] = GameModel(
        id=game_id,
        white_id=white_id,
        black_id=black_id,
        time_control_name="5+0",
        initial_time_ms=300_000,
        increment_ms=0,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=started_at,
        disconnected_player_id=None,
        disconnect_grace_deadline_at=None,
        rated=True,
        white_rating_before=1500,
        black_rating_before=1500,
        white_rating_after=None,
        black_rating_after=None,
        status=GameStatus.ACTIVE,
        result=None,
        fen=DEFAULT_GAME_START_FEN,
        pgn=None,
        started_at=started_at,
        ended_at=None,
        termination_reason=None,
        rating_applied_at=None,
    )

    service = GameService(_GameUpdateFailsRepository(_TransactionalSession(store)))
    monkeypatch.setattr("domains.game.application.services.utc_now", lambda: started_at)

    with pytest.raises(RuntimeError, match="game update failed"):
        await service.make_move(MakeMoveCommand(game_id=game_id, user_id=white_id, uci="e2e4"))

    fresh_repo = SqlAlchemyGameRepository(_TransactionalSession(store))
    persisted_moves = await fresh_repo.get_moves(game_id)
    persisted_game = await fresh_repo.get_by_id(game_id)

    assert persisted_moves == []
    assert persisted_game is not None
    assert persisted_game.fen == DEFAULT_GAME_START_FEN
    assert persisted_game.white_time_ms == 300_000
    assert persisted_game.black_time_ms == 300_000
    assert persisted_game.status == GameStatus.ACTIVE
