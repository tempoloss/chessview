"""Move application rules for authoritative games."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import chess

from domains.game.domain.clock import capture_clock_snapshot
from domains.game.domain.entities import Game, Move
from domains.game.domain.exceptions import IllegalMove, NotYourTurn
from domains.game.domain.policies import DEFAULT_GAME_START_FEN
from domains.game.domain.outcomes import apply_board_outcome, clear_disconnect_state


def apply_player_move(
    game: Game,
    *,
    user_id: UUID,
    uci: str,
    move_number: int,
    now: datetime,
    previous_moves: Sequence[Move],
) -> Move:
    board = _board_with_history(game, previous_moves)
    expected_player = game.white_id if board.turn == chess.WHITE else game.black_id
    if user_id != expected_player:
        raise NotYourTurn()

    try:
        parsed_move = board.parse_uci(uci)
    except ValueError as exc:
        raise IllegalMove() from exc

    if parsed_move not in board.legal_moves:
        raise IllegalMove()

    snapshot = capture_clock_snapshot(game, now)
    if board.turn == chess.WHITE:
        game.white_time_ms = snapshot.white_time_ms + game.increment_ms
        game.black_time_ms = snapshot.black_time_ms
    else:
        game.white_time_ms = snapshot.white_time_ms
        game.black_time_ms = snapshot.black_time_ms + game.increment_ms

    board.push(parsed_move)
    game.fen = board.fen()
    clear_disconnect_state(game)
    apply_board_outcome(game, board, now)

    return Move(
        game_id=game.id,
        user_id=user_id,
        uci=uci,
        fen_after=game.fen,
        move_number=move_number,
    )


def _board_with_history(game: Game, previous_moves: Sequence[Move]) -> chess.Board:
    if not previous_moves:
        return chess.Board(game.fen)

    try:
        board = chess.Board(DEFAULT_GAME_START_FEN)
        for previous_move in previous_moves:
            board.push_uci(previous_move.uci)
    except ValueError:
        return chess.Board(game.fen)

    if board.fen() != game.fen:
        return chess.Board(game.fen)
    return board
