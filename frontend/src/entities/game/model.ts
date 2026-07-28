/**
 * Game entity - types and Zustand store for active game state.
 *
 * The server is the source of truth. This store is updated
 * from server game_state / game_over WS events, with a small
 * UI-only layer for local board interactions such as selection
 * and premoves.
 *
 * FSD layer: entities/game
 * May import: shared
 */

import { create } from "zustand";
import type {
  ClockState,
  ErrorPayload,
  GameDetailResponse,
  GameOverPayload,
  GameResult,
  GameStatePayload,
  GameStatus,
  PlayerColor,
  PlayerSummary,
  TerminationReason,
} from "@/shared/types";

export interface GamePlayer {
  avatar_url?: string;
  id: string;
  username: string;
  rating: number;
}

export interface GameMove {
  move_number: number;
  uci: string;
  username: string;
  user_id: string | null;
  fen_after: string | null;
  created_at: string | null;
}

export interface GameSummary {
  id: string;
  white: GamePlayer;
  black: GamePlayer;
  result: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
}

export interface QueuedPremove {
  from: string;
  to: string;
  uci: string;
}

interface GameState {
  gameId: string | null;
  fen: string;
  turn: PlayerColor;
  status: GameStatus;
  terminationReason: TerminationReason | null;
  timeControlName: string;
  white: GamePlayer | null;
  black: GamePlayer | null;
  clock: ClockState | null;
  myColor: PlayerColor | null;
  moveHistory: string[];
  moves: GameMove[];
  lastMove: { uci: string; move_number: number } | null;
  selectedSquare: string | null;
  legalTargets: string[];
  premove: QueuedPremove | null;
  result: GameResult | null;
  gameOverReason: GameOverPayload["reason"] | null;
  winnerId: string | null;
  error: ErrorPayload | null;
  isLoading: boolean;

  setGame: (gameId: string, color: PlayerColor | null) => void;
  hydrateGame: (payload: GameDetailResponse, currentUserId: string | null) => void;
  updateState: (payload: GameStatePayload) => void;
  setGameOver: (payload: GameOverPayload) => void;
  setSelection: (selectedSquare: string | null, legalTargets: string[]) => void;
  clearSelection: () => void;
  setPremove: (premove: QueuedPremove | null) => void;
  clearPremove: () => void;
  setError: (payload: ErrorPayload | null) => void;
  setLoading: (isLoading: boolean) => void;
  reset: () => void;
}

const initialState = {
  gameId: null as string | null,
  fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  turn: "white" as const,
  status: "active" as GameStatus,
  terminationReason: null as TerminationReason | null,
  timeControlName: "5+0",
  white: null as GamePlayer | null,
  black: null as GamePlayer | null,
  clock: null as ClockState | null,
  myColor: null as PlayerColor | null,
  moveHistory: [] as string[],
  moves: [] as GameMove[],
  lastMove: null as { uci: string; move_number: number } | null,
  selectedSquare: null as string | null,
  legalTargets: [] as string[],
  premove: null as QueuedPremove | null,
  result: null as GameResult | null,
  gameOverReason: null as GameOverPayload["reason"] | null,
  winnerId: null as string | null,
  error: null as ErrorPayload | null,
  isLoading: false,
};

function toGamePlayer(player: PlayerSummary | null): GamePlayer | null {
  if (!player) {
    return null;
  }

  return {
    id: player.id,
    username: player.username,
    rating: player.rating,
    avatar_url: player.avatar_url ?? "",
  };
}

function statusFromGameOver(reason: GameOverPayload["reason"]): GameStatus {
  switch (reason) {
    case "resignation":
    case "identity_verification_failed":
      return "resigned";
    case "draw_agreement":
    case "draw":
    case "repetition":
      return "draw";
    case "clock_timeout":
    case "disconnect_timeout":
      return "timeout";
    case "auto_abort":
      return "aborted";
    default:
      return reason;
  }
}

function reasonFromStatus(status: Exclude<GameStatus, "active">): GameOverPayload["reason"] {
  switch (status) {
    case "checkmate":
      return "checkmate";
    case "stalemate":
      return "stalemate";
    case "resigned":
      return "resignation";
    case "draw":
      return "draw_agreement";
    case "timeout":
      return "clock_timeout";
    case "aborted":
      return "auto_abort";
    default:
      return "draw";
  }
}

function buildMoveFallbacks(
  moveHistory: string[],
  white: GamePlayer | null,
  black: GamePlayer | null,
  existingMoves: GameMove[],
): GameMove[] {
  const existingByNumber = new Map(existingMoves.map((move) => [move.move_number, move]));

  return moveHistory.map((uci, index) => {
    const moveNumber = index + 1;
    const existing = existingByNumber.get(moveNumber);

    if (existing && existing.uci === uci) {
      return existing;
    }

    const player = moveNumber % 2 === 1 ? white : black;

    return {
      move_number: moveNumber,
      uci,
      username: player?.username ?? (moveNumber % 2 === 1 ? "White" : "Black"),
      user_id: player?.id ?? null,
      fen_after: existing?.fen_after ?? null,
      created_at: existing?.created_at ?? null,
    };
  });
}

export const useGameStore = create<GameState>((set) => ({
  ...initialState,

  setGame: (gameId, color) =>
    set({
      ...initialState,
      gameId,
      myColor: color,
    }),

  hydrateGame: (payload, currentUserId) => {
    const white = toGamePlayer(payload.white);
    const black = toGamePlayer(payload.black);
    const myColor =
      currentUserId === payload.white.id ? "white" : currentUserId === payload.black.id ? "black" : null;

    set((state) => ({
      ...state,
      gameId: payload.id,
      fen: payload.fen,
      turn: payload.fen.includes(" w ") ? "white" : "black",
      status: payload.status,
      terminationReason: payload.termination_reason,
      timeControlName: payload.time_control_name,
      white,
      black,
      clock: payload.clock,
      myColor: state.myColor ?? myColor,
      moveHistory: payload.moves.map((move) => move.uci),
      moves: payload.moves.map((move) => ({
        move_number: move.move_number,
        uci: move.uci,
        username: move.username,
        user_id: move.user_id,
        fen_after: move.fen_after,
        created_at: move.created_at,
      })),
      lastMove:
        payload.moves.length > 0
          ? {
              uci: payload.moves[payload.moves.length - 1].uci,
              move_number: payload.moves[payload.moves.length - 1].move_number,
            }
          : null,
      selectedSquare: null,
      legalTargets: [],
      premove: null,
      result: payload.result,
      gameOverReason:
        payload.termination_reason ?? (payload.status === "active" ? null : reasonFromStatus(payload.status)),
      winnerId:
        payload.result === "1-0"
          ? payload.white.id
          : payload.result === "0-1"
            ? payload.black.id
            : null,
      error: null,
      isLoading: false,
    }));
  },

  updateState: (payload) =>
    set((state) => {
      const white = toGamePlayer(payload.white);
      const black = toGamePlayer(payload.black);

      return {
        fen: payload.fen,
        turn: payload.turn,
        status: payload.status,
        terminationReason: payload.termination_reason,
        white,
        black,
        clock: payload.clock,
        moveHistory: payload.move_history,
        moves: buildMoveFallbacks(payload.move_history, white, black, state.moves),
        lastMove: payload.last_move,
        selectedSquare: null,
        legalTargets: [],
        error: null,
      };
    }),

  setGameOver: (payload) =>
    set((state) => ({
      result: payload.result,
      gameOverReason: payload.reason,
      winnerId: payload.winner_id,
      status: payload.status ?? statusFromGameOver(payload.reason),
      terminationReason: payload.reason,
      clock: payload.clock,
      selectedSquare: null,
      legalTargets: [],
      premove: null,
      white:
        payload.rating_update && state.white
          ? { ...state.white, rating: payload.rating_update.white.after }
          : state.white,
      black:
        payload.rating_update && state.black
          ? { ...state.black, rating: payload.rating_update.black.after }
          : state.black,
      error: null,
    })),

  setSelection: (selectedSquare, legalTargets) =>
    set({
      selectedSquare,
      legalTargets,
    }),

  clearSelection: () =>
    set({
      selectedSquare: null,
      legalTargets: [],
    }),

  setPremove: (premove) =>
    set({
      premove,
      selectedSquare: null,
      legalTargets: [],
    }),

  clearPremove: () =>
    set({
      premove: null,
    }),

  setError: (payload) => set({ error: payload }),

  setLoading: (isLoading) => set({ isLoading }),

  reset: () => set(initialState),
}));
