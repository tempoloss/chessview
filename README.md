# ChessView

ChessView is a full-stack educational chess web application. It demonstrates authenticated play, server-authoritative game state, WebSocket events, PostgreSQL persistence, local browser analysis, puzzles, tournaments, scheduled matches, and an admin surface.

Repository: <https://github.com/thefueki/chessview>
Current baseline: `v1.0.1`

## Status

ChessView is suitable for local development, diploma demonstration, and single-instance Docker Compose deployment. Redis now backs shared ephemeral realtime state for matchmaking, WebSocket presence/rooms, cross-instance fanout, and game-monitor coordination. It is not described as a hardened industrial deployment: uploaded media is still stored locally, and production load-balancer/object-storage setup is outside the current baseline.

## Features

Implemented core:

- JWT registration, login, refresh, current-user loading, and guarded frontend routes
- player profiles, ratings, leaderboard, history, and head-to-head comparison
- live 1v1 chess with server-side move validation through `python-chess`
- time controls, clock snapshots, resign/draw flows, reconnect handling, timeout, and early auto-abort policy
- WebSocket endpoint at `/ws?token=<access_token>` with typed event envelopes
- Redis-backed matchmaking, presence, room membership, pub/sub fanout, and game-monitor locking
- game chat and WebRTC signaling relay for live games
- replay/review and local Stockfish analysis in the browser
- analysis workspace with FEN/PGN workflows and board editing
- puzzle catalog and per-user attempt tracking
- tournaments with Swiss helpers, standings, round progression, entry-payment emulator hooks, and OTB tournament type
- clubs with persisted ownership, visibility, searchable listings, and membership lifecycle
- scheduled matches with invite/accept/decline/cancel/start lifecycle
- admin API and separate admin frontend for user, audit, payment, and verification views

Extended modules:

- `ClubsPage` is backed by the `/api/v1/clubs` API and PostgreSQL club/member tables.
- `ShopPage` is a marketplace/wallet surface backed by the shop API and profile coin balance.
- payments are handled by an emulator for internal scenarios such as scheduled match or tournament entry payments.
- face verification and passkey flows provide a local architectural foundation, not a certified identity verification service.

Known limitations:

- default Compose runs a single backend instance
- Redis is required for ephemeral realtime coordination
- media storage is local filesystem storage under `backend/storage`
- multi-instance production deployment still needs a load balancer and shared object storage
- WebSocket authentication uses a query token, which is practical for this local app but should be reviewed for hardened deployments
- browser E2E coverage is intentionally small and desktop-focused; load testing is not part of the current baseline

## Tech Stack

- Frontend: React, TypeScript, Vite, TanStack Query, Zustand, react-router, react-chessboard, chess.js, Stockfish, Tailwind CSS
- Backend: FastAPI, SQLAlchemy async, Alembic, PostgreSQL, Redis, Uvicorn, PyJWT, python-chess
- Tooling: Docker Compose, Yarn Classic, uv, pytest, ESLint, TypeScript build

## Repository Layout

```text
backend/          FastAPI app, domains, migrations, tests
frontend/         React/Vite user frontend
admin-frontend/   React/Vite admin frontend
docs/             architecture, domain, event, scaling, diploma notes
docker-compose.yml
justfile
```

Useful docs:

- [Architecture](docs/architecture.md)
- [Domain map](docs/domain-map.md)
- [Event contract](docs/event-contract.md)
- [Scaling notes](docs/scaling.md)
- [Backend README](backend/README.md)
- [Frontend README](frontend/README.md)

## Environment

Create a local env file:

```powershell
Copy-Item .env.example .env
```

The example file contains local development defaults. Replace `JWT_SECRET` and database credentials for any shared environment.

Important variables:

- `DATABASE_URL`: backend connection string for local split development
- `DOCKER_DATABASE_URL`: backend container connection string for Docker Compose
- `REDIS_URL`: backend Redis connection string for local split development
- `DOCKER_REDIS_URL`: backend container Redis connection string for Docker Compose
- `STORAGE_DIR`: local media directory for backend commands
- `DOCKER_STORAGE_DIR`: media directory inside the backend container
- `VITE_SERVER_URL`: browser-facing backend URL
- `VITE_API_PROXY_TARGET`, `VITE_WS_PROXY_TARGET`: Vite proxy targets

## Quickstart: Docker Compose

```powershell
docker compose up --build
```

Endpoints:

- frontend: <http://localhost:5173>
- admin frontend: <http://localhost:5174>
- backend health: <http://localhost:8000/health>
- backend API base: <http://localhost:8000/api/v1>

Check the resolved Compose configuration:

```powershell
docker compose config
```

## Quickstart: Split Development

Start PostgreSQL and Redis:

```powershell
docker compose up -d postgres redis
```

Start backend:

```powershell
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host localhost --port 8000
```

Start frontend:

```powershell
cd frontend
yarn install --frozen-lockfile
yarn dev
```

Start admin frontend:

```powershell
cd admin-frontend
yarn install --frozen-lockfile
yarn dev
```

## Verification Commands

Backend:

```powershell
cd backend
uv run python -m compileall app domains infrastructure shared tests
uv run alembic upgrade head
uv run alembic check
uv run python -c "import app.main"
uv run pytest tests
```

Frontend:

```powershell
cd frontend
yarn install --frozen-lockfile
yarn lint
yarn test:i18n
yarn test:ui-consistency
yarn test:unit
yarn build
```

Admin frontend:

```powershell
cd admin-frontend
yarn install --frozen-lockfile
yarn typecheck
yarn lint
yarn test:unit
yarn build
```

Desktop browser E2E:

```powershell
yarn install --frozen-lockfile
yarn test:e2e
```

Full CI-equivalent local check:

```powershell
just check-ci
```

Shared deterministic test data lives in `tools/fixtures/chessview.fixture.v1.json`. Use it for frontend mocks, admin mocks, and future database seed/reset scripts; it contains local-only credentials and no production secrets.

Smoke checks after backend startup:

```powershell
Invoke-RestMethod http://localhost:8000/health
try {
  Invoke-RestMethod http://localhost:8000/api/v1/puzzles
} catch {
  $_.Exception.Response.StatusCode.value__
}
```

The second command is expected to return `401` without an access token.

## Published images

The `Publish Images` workflow publishes production backend and static frontend images to GitHub Container Registry:

- `ghcr.io/tempoloss/chessview-backend`
- `ghcr.io/tempoloss/chessview-frontend`
- `ghcr.io/tempoloss/chessview-admin-frontend`

## Troubleshooting

- `uv` is missing: install it from <https://docs.astral.sh/uv/> or run backend checks through the existing virtual environment if present.
- Docker cannot reach PostgreSQL: run `docker compose config`, then verify `.env` has `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, and `DOCKER_DATABASE_URL`.
- frontend API calls fail in dev: check `VITE_API_PROXY_TARGET` and `VITE_WS_PROXY_TARGET`.
- protected API returns `401`: log in first and send `Authorization: Bearer <access_token>`.
- avatar upload fails: verify `STORAGE_DIR` exists and the file is PNG, JPEG, or WebP within backend limits.

## CI

Pull request CI runs backend compile/migrations/tests, frontend lint/UI consistency/unit/build, and admin frontend typecheck/lint/unit/build. Keep local commands aligned with `.github/workflows/pr-ci.yml`.
