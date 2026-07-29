# ChessView Architecture

## Overview

ChessView is a browser-based educational chess platform with three core responsibilities:

- live competitive play
- post-game review and local analysis
- study and organization tooling such as puzzles, tournaments, and scheduled matches

The stack is split between:

- a server-authoritative backend for game state, ratings, profiles, clubs, tournaments, payments emulator data, and persistence
- a frontend that owns interaction, presentation, route guards, and browser-local Stockfish analysis
- PostgreSQL as the relational store, with schema history managed by Alembic

## System Shape

```text
Browser
  -> React/Vite user frontend
  -> REST requests to /api/v1/*
  -> WebSocket connection to /ws?token=<access_token>
  -> local Stockfish worker for analysis/review
  -> optional WebRTC peer connection after WS signaling

Admin browser
  -> React/Vite admin frontend
  -> admin REST requests to /api/v1/admin/*

FastAPI backend
  -> domain/application/infrastructure/presentation modules
  -> PostgreSQL persistence through SQLAlchemy async
  -> Alembic migrations
  -> Redis-backed matchmaking, WebSocket presence/rooms, pub/sub fanout, and monitor locks
  -> local media storage under backend/storage
```

## Backend Architecture

The backend is organized by domain under `backend/domains/`.

Common domain shape:

- `domain/`: entities, value objects, policies, pure rules, repository interfaces
- `application/`: commands, services, orchestration
- `infrastructure/`: SQLAlchemy models and repository implementations
- `presentation/`: REST routers, WebSocket handlers, schemas, serializers

Key domains:

- `identity`: auth, current user, avatars, face/passkey flows
- `game`: live games, clocks, reconnect, timeouts, history, replay data
- `matchmaking`: Redis-backed queueing and game creation
- `profiles`: profile read models, leaderboard, search, head-to-head
- `ratings`: Elo updates and rating snapshots
- `communication`: game chat
- `clubs`: persisted club ownership, discovery, visibility, and membership
- `tournaments`: lifecycle, pairings, standings, Swiss helpers
- `scheduled_matches`: planned match invitations and start flow
- `puzzles`: puzzle catalog and attempt tracking
- `payments`: payment intent emulator and coin debit/refund helpers
- `rtc`: WebRTC signaling relay
- `admin`: admin-only user, audit, payment, and verification views

Shared infrastructure lives under `backend/infrastructure/` and `backend/shared/`.

## Frontend Architecture

The frontend lives in `frontend/src/` and follows a feature-sliced structure:

- `app/`: providers, router, app-wide route guards
- `pages/`: route-level surfaces
- `widgets/`: composite UI blocks
- `features/`: focused interactive capabilities
- `entities/`: domain-facing frontend state and types
- `shared/`: UI primitives, utilities, API clients, chess helpers

Important frontend choices:

- auth is bootstrapped once and guarded at the router layer
- live game flow stays separate from replay, analysis, and puzzles
- browser-local Stockfish is used for review and study, not for backend move authority
- shop, clubs, scheduled matches, and OTB manager are backend-backed product surfaces; payment and verification providers remain local/emulated

## Live Game Ownership

The server is the source of truth for gameplay.

Flow:

1. Client sends a `move` WebSocket event with a UCI move and game id.
2. Backend loads the active game and checks the user, side to move, UCI parsing, legal moves, and clock state.
3. Backend persists the move and updated FEN.
4. Backend broadcasts `game_state` or `game_over` to the room.
5. Clients render from server state.

This keeps clocks, results, reconnect behavior, and move legality authoritative.

## Analysis Ownership

Replay and study analysis are local to the browser.

- replay uses finished game positions
- analysis workspace uses the displayed sandbox/editor position
- puzzle mode validates attempts against stored solution lines
- Stockfish worker output is tied to the active FEN so stale results are ignored

This preserves backend simplicity and avoids mixing engine output into live game authority.

## Deployment Model

Supported workflows:

- Docker Compose for the full local stack
- local split development for faster iteration

Development topology:

- frontend on `localhost:5173`
- admin frontend on `localhost:5174`
- backend on `localhost:8000`
- PostgreSQL on `localhost:5432`
- Redis on `localhost:6379`

The frontend dev server proxies `/api` and `/ws` to the backend.

## Current Limitations / Not Yet Proven

The default Compose deployment still runs a single backend instance. Redis backs these specific realtime coordination mechanisms:

- matchmaking queue state is stored in Redis; see `backend/domains/matchmaking/application/services.py`
- WebSocket presence and room membership are stored in Redis, while socket objects remain local to each backend process; see `backend/shared/ws_manager.py`
- background game monitoring uses a Redis lock so one instance owns each monitor tick; see `backend/domains/game/presentation/runtime.py`
- avatar/media storage is local filesystem storage
- payment workflows are emulator-based

Scaling to multiple backend instances still requires load-balancer setup, shared object storage for uploaded media, and load testing.
