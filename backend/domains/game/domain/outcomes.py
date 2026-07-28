"""Outcome and lifecycle transitions for games."""

from datetime import datetime, timedelta
from uuid import UUID

import chess

from domains.game.domain.clock import ClockSnapshot
from domains.game.domain.entities import Game
from domains.game.domain.value_objects import GameResult, GameStatus


def apply_board_outcome(game: Game, board: chess.Board, now: datetime) -> None:
    if board.is_checkmate():
        _finish_game(
            game,
            status=GameStatus.CHECKMATE,
            result=GameResult.WHITE_WINS if board.turn == chess.BLACK else GameResult.BLACK_WINS,
            reason="checkmate",
            now=now,
        )
        return

    if board.is_stalemate():
        _finish_game(game, status=GameStatus.STALEMATE, result=GameResult.DRAW, reason="stalemate", now=now)
        return

    if board.is_insufficient_material() or board.is_fifty_moves():
        _finish_game(game, status=GameStatus.DRAW, result=GameResult.DRAW, reason="draw", now=now)
        return

    if board.is_repetition():
        _finish_game(game, status=GameStatus.DRAW, result=GameResult.DRAW, reason="repetition", now=now)
        return

    game.last_clock_started_at = now


def resign_game(game: Game, user_id: UUID, snapshot: ClockSnapshot, now: datetime) -> None:
    result = GameResult.BLACK_WINS if user_id == game.white_id else GameResult.WHITE_WINS
    game.white_time_ms = snapshot.white_time_ms
    game.black_time_ms = snapshot.black_time_ms
    _finish_game(game, status=GameStatus.RESIGNED, result=result, reason="resignation", now=now)


def forfeit_game_for_identity_failure(game: Game, user_id: UUID, snapshot: ClockSnapshot, now: datetime) -> None:
    result = GameResult.BLACK_WINS if user_id == game.white_id else GameResult.WHITE_WINS
    game.white_time_ms = snapshot.white_time_ms
    game.black_time_ms = snapshot.black_time_ms
    _finish_game(game, status=GameStatus.RESIGNED, result=result, reason="identity_verification_failed", now=now)


def accept_draw(game: Game, snapshot: ClockSnapshot, now: datetime) -> None:
    game.white_time_ms = snapshot.white_time_ms
    game.black_time_ms = snapshot.black_time_ms
    _finish_game(game, status=GameStatus.DRAW, result=GameResult.DRAW, reason="draw_agreement", now=now)


def timeout_game(game: Game, losing_player_id: UUID, snapshot: ClockSnapshot, now: datetime, reason: str) -> None:
    if losing_player_id == game.white_id:
        white_time_ms = 0
        black_time_ms = snapshot.black_time_ms
        result = GameResult.BLACK_WINS
    else:
        white_time_ms = snapshot.white_time_ms
        black_time_ms = 0
        result = GameResult.WHITE_WINS

    game.white_time_ms = white_time_ms
    game.black_time_ms = black_time_ms
    _finish_game(game, status=GameStatus.TIMEOUT, result=result, reason=reason, now=now)


def abort_game(game: Game, snapshot: ClockSnapshot, now: datetime, reason: str) -> None:
    game.white_time_ms = snapshot.white_time_ms
    game.black_time_ms = snapshot.black_time_ms
    _finish_game(game, status=GameStatus.ABORTED, result=None, reason=reason, now=now)


def pause_for_disconnect(game: Game, user_id: UUID, snapshot: ClockSnapshot, now: datetime, grace_seconds: int) -> None:
    game.white_time_ms = snapshot.white_time_ms
    game.black_time_ms = snapshot.black_time_ms
    game.last_clock_started_at = None
    game.disconnected_player_id = user_id
    game.disconnect_grace_deadline_at = now + timedelta(seconds=grace_seconds)


def resume_after_reconnect(game: Game, now: datetime) -> None:
    game.disconnected_player_id = None
    game.disconnect_grace_deadline_at = None
    game.last_clock_started_at = now


def clear_disconnect_state(game: Game) -> None:
    game.disconnected_player_id = None
    game.disconnect_grace_deadline_at = None


def _finish_game(
    game: Game,
    *,
    status: GameStatus,
    result: GameResult | None,
    reason: str,
    now: datetime,
) -> None:
    game.status = status
    game.result = result
    game.termination_reason = reason
    game.ended_at = now
    game.last_clock_started_at = None
    clear_disconnect_state(game)
