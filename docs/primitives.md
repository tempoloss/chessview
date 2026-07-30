# ChessView primitives

This document explains the runtime primitives ChessView rests on, from the mechanism upward to the code that depends on it. Every entry below is anchored to code or tests in this repository; if an entry says nothing catches it yet, that is intentional.

The line-by-line annotations behind these entries live in [`primitives.code.json`](primitives.code.json): the cited ranges, the note lines, and a fingerprint of the code each one was written against. `python3 scripts/check_primitives_anchors.py` fails when an anchor in this file or in that one no longer points at the code it describes. The Primitives workflow runs it on every push, re-anchors a pure line shift by matching content instead of line numbers, and pushes that repair back, so only a substantive code change needs a person.

## WebSockets

### WebSocket connection

A WebSocket starts as an HTTP upgrade and then stays open so either side can send more messages without opening a new request. A plain HTTP request asks once and gets one response; a chess board needs a WebSocket because moves, clock state, draw offers, chat, and RTC signals can arrive while the user is staring at the board.

**Where:** `backend/app/main.py:141` registers `/ws`, `backend/app/ws_entry.py:86` handles that socket, and `frontend/src/shared/api/ws.ts:105` opens `WS_BASE_URL?token=<token>`.

**What breaks if it is wrong:** 1) White moves. 2) Black's browser has no open channel. 3) Black sees the old board until polling catches up or the page reloads. 4) The clock and legal-turn UI drift from the server.

**Caught by:** `test_ws_endpoint_authenticates_dispatches_and_maps_handler_errors` in `backend/tests/test_ws_workflows.py:95`, and `connects with the bearer token and publishes connection state changes` in `frontend/src/shared/api/__tests__/ws.test.ts:52`.

### Typed event envelope

A socket is just bytes unless both sides agree on a shape. ChessView wraps every frame in an envelope with a `type` routing key, a `payload`, an optional `game_id`, and a timestamp.

**Where:** `backend/shared/events.py:14` lists event names and `backend/shared/events.py:41` defines `WSEnvelope`; the server dispatcher looks up handlers from that type at `backend/app/ws_entry.py:131`.

**What breaks if it is wrong:** 1) The client sends `move` but the server expects another key. 2) The dispatcher cannot find the handler. 3) The move is rejected as `INVALID_EVENT` even though the board UI looked valid. 4) If the wrong `game_id` is accepted, an event can leak into the wrong room.

**Caught by:** `test_ws_endpoint_authenticates_dispatches_and_maps_handler_errors` in `backend/tests/test_ws_workflows.py:95` and `sends typed envelopes only when the socket is open` in `frontend/src/shared/api/__tests__/ws.test.ts:65`.

### Socket drop state

A socket dropping does not mean the game vanished. The connection manager removes the socket, while the game service freezes the active clock and records which player is inside the reconnect grace window.

**Where:** `backend/app/ws_entry.py:149` catches `WebSocketDisconnect`, `backend/app/ws_entry.py:164` calls `mark_disconnected`, and `backend/domains/game/domain/outcomes.py:80` freezes time and sets the disconnect deadline.

**What breaks if it is wrong:** 1) A mobile client loses network. 2) The server either keeps charging the clock as if the player were connected, or forgets the player was in a game. 3) Reconnect can resume the wrong state, or the opponent gets a timeout that was never earned.

**Caught by:** `test_pause_for_disconnect_freezes_clock_and_marks_grace_deadline` in `backend/tests/test_game_domain_rules.py:78`, and `test_current_connection_disconnect_cleans_membership` in `backend/tests/test_ws_manager.py:94`.

### Server-owned clock

The server, not the browser, decides how much time remains. That is a correctness rule, not a style choice: a client-owned chess clock is a client that can cheat by reporting a larger remaining time after thinking.

**Where:** `backend/domains/game/presentation/ws_handler.py:115` reads only the submitted UCI move, `backend/domains/game/application/services.py:61` takes server time, and `backend/domains/game/domain/clock.py:55` computes the clock snapshot from stored game state.

**What breaks if it is wrong:** 1) A player thinks for 20 seconds. 2) Their browser submits the move with an invented remaining time. 3) The server trusts it. 4) The player never flags, even though the opponent played against a real clock.

**Caught by:** `test_clock_timeout_triggers_when_active_player_flags` in `backend/tests/test_phase2_game_clocks.py:120`; `test_game_ws_move_validates_game_id_and_broadcasts_state` in `backend/tests/test_ws_workflows.py:233` verifies the WS handler passes the move command through, but nothing yet fuzzes extra client-supplied clock fields.

## Redis

### Redis pub/sub fanout

A WebSocket object exists only inside the backend process that accepted it. Redis pub/sub lets one backend process publish a message to another process so a user connected elsewhere still receives the event.

**Where:** `backend/shared/ws_manager.py:148` gathers room recipients, `backend/shared/ws_manager.py:163` groups remote users by instance, `backend/shared/ws_manager.py:259` publishes to `ws:instance:<id>`, and `backend/shared/ws_manager.py:182` listens on that instance channel.

**What breaks if it is wrong:** 1) The app runs two backend workers. 2) White is connected to worker A and Black to worker B. 3) White moves on A. 4) Without pub/sub, B never hears about the `game_state`, so Black's board stays stale.

**Caught by:** `test_broadcast_to_room_delivers_local_and_groups_remote_users_by_instance` in `backend/tests/test_ws_manager.py:149`.

### Redis presence and room state

Presence is a short-lived record saying which process currently owns a user's socket. Room membership says which users should receive a game-scoped event, and it is ephemeral because sockets and rooms are runtime facts, not historical game facts.

**Where:** `backend/shared/ws_manager.py:4` states that Redis stores ephemeral presence, rooms, and routing data; `backend/shared/ws_manager.py:35` sets presence and room TTLs; `backend/shared/ws_manager.py:103` writes room membership; `backend/shared/ws_manager.py:232` writes presence.

**What breaks if it is wrong:** 1) A user closes the browser. 2) PostgreSQL or Redis still claims the user is present forever. 3) Future broadcasts target a dead socket, or a later process routes to an instance that no longer owns the user. 4) Chat, RTC, and game updates become noisy or missing.

**Caught by:** `test_connect_join_room_and_disconnect_update_redis_presence_and_rooms` in `backend/tests/test_ws_manager.py:110`.

### Redis sorted set matchmaking queue

A Redis sorted set is a set where each member also has a numeric score. Here the member is the user id and the score is the join time, so the service can scan queued users in deterministic order while comparing rating and time-control boundaries.

**Where:** `backend/domains/matchmaking/application/services.py:70` stores the user's queue metadata, `backend/domains/matchmaking/application/services.py:81` adds the user to `mm:queue:<time_control>`, and `backend/domains/matchmaking/application/services.py:135` reads candidates back with `zrange`.

**What breaks if it is wrong:** 1) Three players enter the same pool. 2) The queue loses join order or metadata. 3) The service pairs across the wrong time control or chooses a worse rating match. 4) One player remains in Redis after a match and can be matched twice.

**Caught by:** `test_redis_matchmaking_pairs_same_time_control_with_best_rating_diff` in `backend/tests/test_redis_matchmaking.py:99`, and `test_redis_matchmaking_keeps_rating_and_time_control_boundaries` in `backend/tests/test_redis_matchmaking.py:128`.

### Owner-token distributed lock

`SET key token NX EX seconds` means "write this lock only if it does not already exist, and let Redis expire it." The token matters: release must delete the lock only when Redis still stores the same token, usually with a Lua compare-and-delete script that runs atomically inside Redis.

**Where:** `backend/domains/game/presentation/runtime.py:24` names the game-monitor lock, `backend/domains/game/presentation/runtime.py:33` acquires it with `SET ... NX ... EX`, `backend/domains/game/presentation/runtime.py:38` releases it through Lua, and matchmaking uses the same owner-token pattern at `backend/domains/matchmaking/application/services.py:101` and `backend/domains/matchmaking/application/services.py:182`.

**What breaks if it is wrong:** 1) Worker A acquires a lock and stalls until its TTL expires. 2) Worker B acquires the same lock with a new token. 3) Worker A finally runs `DEL lock` unconditionally. 4) B's valid lock is deleted, so two workers can monitor games or match players at the same time. ChessView implements the correct token-plus-Lua release, not that naive release.

**Caught by:** `test_game_monitor_lock_is_exclusive_and_token_released` in `backend/tests/test_game_monitor_lock.py:24`.

## Time

### Monotonic time versus wall-clock time

Wall-clock time is the calendar time humans see, and it can jump if NTP or the host changes it. Monotonic time only moves forward and is the right primitive for measuring elapsed thinking time; this code currently stores UTC wall-clock instants so game state survives process restart, then subtracts those instants to make snapshots.

**Where:** `backend/domains/game/application/services.py:31` defines `utc_now()` with `datetime.now(timezone.utc)`, and `backend/domains/game/domain/clock.py:61` subtracts `last_clock_started_at` from `now` to compute elapsed milliseconds.

**What breaks if it is wrong:** 1) The server clock jumps backward during a game. 2) `now - last_clock_started_at` shrinks or goes negative. 3) The active player gains time they did not earn. 4) If it jumps forward, the player can lose on time without thinking that long.

**Caught by:** `test_capture_clock_snapshot_reports_running_white_clock` in `backend/tests/test_game_domain_rules.py:59` covers fixed elapsed-time math; nothing yet simulates wall-clock jumps.

### Clock snapshot

A clock snapshot is the server's answer to "if we stop time at this instant, how much time does each player have and is the clock paused?" It turns stored game fields into a payload the frontend can render between server events.

**Where:** `backend/domains/game/domain/clock.py:13` defines `ClockSnapshot`, `backend/domains/game/domain/clock.py:55` captures one, `backend/domains/game/presentation/ws_handler.py:58` includes it in `game_state`, and `frontend/src/shared/hooks/useLiveClock.ts:9` locally counts forward from `last_updated_at` for display.

**What breaks if it is wrong:** 1) The server broadcasts `white_time_ms` without `last_updated_at`. 2) The frontend cannot know how much time has passed since the message. 3) The displayed clock freezes or double-counts. 4) A later timeout looks arbitrary to the player.

**Caught by:** `test_capture_clock_snapshot_reports_running_white_clock` in `backend/tests/test_game_domain_rules.py:59`, and `test_game_clock_moves_and_lifecycle_edges` in `backend/tests/test_service_and_serializer_coverage.py:444`.

### Disconnect grace period

A grace period is a short server-owned deadline after a disconnect. During it the active chess clock is paused, and when it expires the monitor either aborts an unstarted game or times out a meaningfully started one.

**Where:** `backend/domains/game/domain/policies.py:5` sets the default to 20 seconds, `backend/domains/game/application/services.py:120` marks the disconnect, and `backend/domains/game/application/services.py:142` resolves expired grace windows in the runtime monitor path.

**What breaks if it is wrong:** 1) A player disconnects before either side has really played. 2) The server treats it like a full rated timeout instead of an abort. 3) Or, after two moves, it aborts a real game instead of awarding the opponent a disconnect timeout. 4) Ratings and tournament standings then describe the wrong result.

**Caught by:** `test_disconnect_grace_auto_aborts_game_before_meaningful_start` in `backend/tests/test_phase2_game_clocks.py:55`, and `test_disconnect_grace_times_out_meaningfully_started_game` in `backend/tests/test_phase2_game_clocks.py:85`.

## PostgreSQL

### Move transaction boundary

A transaction is the database boundary where related writes become visible together or not at all. A chess move wants the move row and the resulting game row to agree: the move number, `fen_after`, clocks, status, and result are one logical update.

**Where:** `backend/domains/game/application/services.py:66` loads existing moves, `backend/domains/game/application/services.py:76` stages the move row, `backend/domains/game/application/services.py:77` stages the game row, `backend/domains/game/application/services.py:78` commits the shared unit of work, and `backend/domains/game/infrastructure/repository.py:203` flushes without committing inside individual repository methods.

**What breaks if it is wrong:** 1) The move row commits. 2) The game update fails before `fen` or clocks are committed. 3) History says `e2e4` happened, but the authoritative game row still shows the old position. 4) Reconnect and REST history can disagree.

**Caught by:** `test_make_move_rolls_back_move_when_game_update_fails` in `backend/tests/test_game_repository_transactions.py:121` asserts that a failure after staging the move leaves both the move list and authoritative game row unchanged.

### Authoritative board row

The board on screen is a projection. The authoritative position is the `fen` stored on the game, with move rows as history; if replayed move history diverges, the domain code falls back to the stored `fen` rather than trusting a broken replay.

**Where:** `backend/domains/game/infrastructure/models.py:60` stores `GameModel.fen`, `backend/domains/game/infrastructure/models.py:83` stores `MoveModel.fen_after`, and `backend/domains/game/domain/moves.py:60` rebuilds from move history but returns to the stored `game.fen` on divergence.

**What breaks if it is wrong:** 1) A process keeps the board only in memory. 2) That process restarts or another process handles the reconnect. 3) The new process has no authoritative board. 4) A player can be shown a legal-move set for the wrong position.

**Caught by:** `test_replay_divergence_falls_back_to_authoritative_fen` in `backend/tests/test_game_domain_rules.py:165`.

### Games-by-player indexes

An index is a smaller lookup structure PostgreSQL can scan instead of reading the whole table. A query that lists games for one player by `white_id OR black_id` and orders by `started_at` is exactly the kind of query that needs index support as the games table grows.

**Where:** `backend/domains/game/infrastructure/repository.py:61` implements `list_by_user` with `white_id OR black_id`, `backend/domains/game/infrastructure/repository.py:71` orders by newest games, and `backend/alembic/versions/0011_game_player_history_indexes.py:19` creates `ix_games_white_id_started_at_desc` plus `ix_games_black_id_started_at_desc` — one per side of the `OR`, because a single index cannot serve both branches.

Measured on PostgreSQL 16 with 60,000 games and 200 users, for a player with 600 of them: with the indexes the planner uses `BitmapOr` over both `Bitmap Index Scan`s and touches 600 rows; with them dropped it falls back to `Seq Scan` and reports `Rows Removed by Filter: 59400`. What the indexes do NOT do is remove the sort — `BitmapOr` does not preserve index order, so the plan still runs a `top-N heapsort`, just over 600 rows instead of 60,000. The `started_at DESC` column is there for the single-side variant of this query, not for this one.

A migration is not the only place the schema is written down; the ORM model is the other, and `alembic check` compares them. Created by the migration but missing from `GameModel`, these two indexes read to autogenerate as indexes nobody asked for, and the check failed with a `remove_index` for each — the CI job that runs it stayed red until the model declared them. `backend/domains/game/infrastructure/models.py:34` declares them, spelled the way the migration spells them.

**What breaks if it is wrong:** 1) A long-time player opens history. 2) PostgreSQL scans many game rows to find that player. 3) The request gets slower as the site grows. 4) Enough users do it at once and ordinary gameplay endpoints compete for database time.

**Caught by:** `test_games_by_player_indexes_are_declared_in_migrations` in `backend/tests/test_database_bootstrap.py:120` verifies the Alembic upgrade path declares both games-by-player indexes with the filter column before `started_at DESC`.

### Alembic migration

A migration is a versioned schema change: create this table, add this column, create this index. Alembic exists so every environment reaches the same schema through recorded steps, rather than by someone editing a database by hand and hoping production matches development.

**Where:** `backend/infrastructure/database_migrations.py:78` runs migrations, `backend/infrastructure/database_migrations.py:109` upgrades to `head`, `backend/alembic/env.py:28` registers models for migration metadata, and `backend/alembic/versions/0001_baseline.py:16` creates the baseline schema.

**What breaks if it is wrong:** 1) A developer adds a model field but no migration. 2) Their local database happens to have the column. 3) CI or production starts from migrations and does not. 4) The app imports or writes a missing column at runtime.

**Caught by:** `test_initialize_database_runs_migrations_before_seeding` in `backend/tests/test_database_bootstrap.py:12`, `test_to_migration_database_url_rewrites_asyncpg_for_alembic` in `backend/tests/test_database_bootstrap.py:46`, and CI's `uv run alembic check` step in `.github/workflows/pr-ci.yml:91`.

## HTTP and auth

### JWT access token

A JWT is a signed claim bundle: here it carries `sub`, expiry, and token type. The server can verify the signature with its secret and recover the user id without storing a session row, but verification is not optional because unsigned or wrongly signed claims are just user-controlled text.

**Where:** `backend/infrastructure/security.py:32` creates access tokens, `backend/infrastructure/security.py:53` decodes and verifies them, `backend/app/dependencies.py:30` protects HTTP routes with a Bearer token, and `backend/app/ws_entry.py:88` requires the token on WebSocket connect.

**What breaks if it is wrong:** 1) A client edits a token payload to change `sub`. 2) The server decodes without verifying the signature. 3) The request runs as another user. 4) Game, profile, admin, and WebSocket actions no longer belong to the authenticated player.

**Caught by:** `test_security_tokens_and_current_user_dependency` in `backend/tests/test_service_and_serializer_coverage.py:416`, and `test_ws_endpoint_authenticates_dispatches_and_maps_handler_errors` in `backend/tests/test_ws_workflows.py:95`.

## Testing

### Real-browser end-to-end test

A unit test can prove a function returns the right object, but it cannot prove a real browser, localStorage auth, HTTP, routing, rendered text, Docker networking, PostgreSQL, and Redis all work together. A Playwright test drives Chromium through those seams.

**Where:** `e2e/full-stack-real.spec.ts:69` defines the real Docker stack workflow, `playwright.config.ts:7` points Playwright at `./e2e`, and `.github/workflows/pr-ci.yml:175` runs the Desktop E2E job.

**What breaks if it is wrong:** 1) Backend tests pass with fake clients. 2) Frontend unit tests pass with mocked fetches. 3) The real browser cannot log in, proxy to the backend, or render a page from live API data. 4) Users see the broken seam first.

**Caught by:** `runs core user, scheduling, tournament, puzzle, and admin flows` in `e2e/full-stack-real.spec.ts:70`, `loads the public player app and navigates to auth` in `e2e/desktop-smoke.spec.ts:6`, and the CI `Run desktop E2E tests` step in `.github/workflows/pr-ci.yml:232`.
