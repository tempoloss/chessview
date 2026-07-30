"""
SQLAlchemy implementation of the game repository.

Converts between ORM models and domain entities.
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.game.domain.entities import Game, Move
from domains.game.domain.repository import AbstractGameRepository
from domains.game.domain.value_objects import GameResult, GameStatus
from domains.game.infrastructure.models import GameModel, MoveModel


class SqlAlchemyGameRepository(AbstractGameRepository):
    """Concrete game repository backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, game: Game) -> Game:
        model = self._new_game_model(game)
        self._session.add(model)
        await self._persist(model)
        return self._to_game_entity(model)

    async def get_by_id(self, game_id: UUID) -> Game | None:
        model = await self._get_game_model(game_id)
        return self._to_game_entity(model) if model else None

    async def get_active_by_user(self, user_id: UUID) -> Game | None:
        stmt = (
            select(GameModel)
            .where(
                or_(GameModel.white_id == user_id, GameModel.black_id == user_id),
                GameModel.status == GameStatus.ACTIVE,
            )
            .order_by(GameModel.started_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_game_entity(model) if model else None

    async def list_active(self) -> list[Game]:
        stmt = select(GameModel).where(GameModel.status == GameStatus.ACTIVE).order_by(GameModel.started_at)
        result = await self._session.execute(stmt)
        return [self._to_game_entity(model) for model in result.scalars().all()]

    async def update(self, game: Game) -> Game:
        model = await self._get_game_model(game.id)
        if model is None:
            raise ValueError(f"Game {game.id} not found for update")
        self._apply_game_state(model, game)
        await self._persist(model)
        return self._to_game_entity(model)

    async def list_by_user(self, user_id: UUID, page: int = 1, size: int = 20) -> tuple[list[Game], int]:
        condition = or_(GameModel.white_id == user_id, GameModel.black_id == user_id)

        count_stmt = select(func.count()).select_from(GameModel).where(condition)
        count_result = await self._session.execute(count_stmt)
        total = count_result.scalar_one()

        stmt = (
            select(GameModel)
            .where(condition)
            .order_by(GameModel.started_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()

        return [self._to_game_entity(model) for model in models], total

    async def add_move(self, move: Move) -> Move:
        model = self._new_move_model(move)
        self._session.add(model)
        await self._persist(model)
        return self._to_move_entity(model)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def get_moves(self, game_id: UUID) -> list[Move]:
        stmt = (
            select(MoveModel)
            .where(MoveModel.game_id == game_id)
            .order_by(MoveModel.move_number)
        )
        result = await self._session.execute(stmt)
        return [self._to_move_entity(model) for model in result.scalars().all()]

    async def get_move_counts(self, game_ids: list[UUID]) -> dict[UUID, int]:
        if not game_ids:
            return {}

        stmt = (
            select(MoveModel.game_id, func.count(MoveModel.id))
            .where(MoveModel.game_id.in_(game_ids))
            .group_by(MoveModel.game_id)
        )
        result = await self._session.execute(stmt)
        return {game_id: count for game_id, count in result.all()}

    @staticmethod
    def _new_game_model(game: Game) -> GameModel:
        model = GameModel(
            id=game.id,
            white_id=game.white_id,
            black_id=game.black_id,
        )
        SqlAlchemyGameRepository._apply_game_state(model, game)
        return model

    @staticmethod
    def _apply_game_state(model: GameModel, game: Game) -> None:
        model.time_control_name = game.time_control_name
        model.initial_time_ms = game.initial_time_ms
        model.increment_ms = game.increment_ms
        model.white_time_ms = game.white_time_ms
        model.black_time_ms = game.black_time_ms
        model.last_clock_started_at = game.last_clock_started_at
        model.disconnected_player_id = game.disconnected_player_id
        model.disconnect_grace_deadline_at = game.disconnect_grace_deadline_at
        model.rated = game.rated
        model.white_rating_before = game.white_rating_before
        model.black_rating_before = game.black_rating_before
        model.white_rating_after = game.white_rating_after
        model.black_rating_after = game.black_rating_after
        model.status = game.status
        model.result = game.result
        model.fen = game.fen
        model.pgn = game.pgn
        model.started_at = game.started_at
        model.ended_at = game.ended_at
        model.termination_reason = game.termination_reason
        model.rating_applied_at = game.rating_applied_at

    @staticmethod
    def _new_move_model(move: Move) -> MoveModel:
        return MoveModel(
            game_id=move.game_id,
            user_id=move.user_id,
            uci=move.uci,
            fen_after=move.fen_after,
            move_number=move.move_number,
        )

    @staticmethod
    def _to_game_entity(model: GameModel) -> Game:
        return Game(
            id=model.id,
            white_id=model.white_id,
            black_id=model.black_id,
            time_control_name=model.time_control_name,
            initial_time_ms=model.initial_time_ms,
            increment_ms=model.increment_ms,
            white_time_ms=model.white_time_ms,
            black_time_ms=model.black_time_ms,
            last_clock_started_at=model.last_clock_started_at,
            disconnected_player_id=model.disconnected_player_id,
            disconnect_grace_deadline_at=model.disconnect_grace_deadline_at,
            rated=model.rated,
            white_rating_before=model.white_rating_before,
            black_rating_before=model.black_rating_before,
            white_rating_after=model.white_rating_after,
            black_rating_after=model.black_rating_after,
            status=GameStatus(model.status),
            result=GameResult(model.result) if model.result is not None else None,
            fen=model.fen,
            pgn=model.pgn,
            started_at=model.started_at,
            ended_at=model.ended_at,
            termination_reason=model.termination_reason,
            rating_applied_at=model.rating_applied_at,
        )

    @staticmethod
    def _to_move_entity(model: MoveModel) -> Move:
        return Move(
            id=model.id,
            game_id=model.game_id,
            user_id=model.user_id,
            uci=model.uci,
            fen_after=model.fen_after,
            move_number=model.move_number,
            created_at=model.created_at,
        )

    async def _get_game_model(self, game_id: UUID) -> GameModel | None:
        result = await self._session.execute(select(GameModel).where(GameModel.id == game_id))
        return result.scalar_one_or_none()

    async def _persist(self, model: GameModel | MoveModel) -> None:
        await self._session.flush()
        await self._session.refresh(model)
