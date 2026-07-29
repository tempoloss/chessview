# Scaling Notes and Limitations

ChessView uses Redis for named ephemeral realtime coordination points while keeping PostgreSQL as the durable source of truth. The current Docker Compose stack still runs one backend container by default; the Redis-backed mechanisms below are implemented, but multi-instance operation has not been load-tested or documented with a production load balancer.

## Redis-Backed Realtime State

- Matchmaking queue state is stored in Redis sorted sets and user hashes.
  - See `backend/domains/matchmaking/application/services.py`.
- WebSocket presence, active game membership, and room membership are stored in Redis with TTL refresh.
  - See `backend/shared/ws_manager.py`.
- Cross-instance WebSocket delivery uses Redis pub/sub channels named `ws:instance:{instance_id}`.
  - Local sockets remain in the owning backend process.
  - Remote events are published to the instance that owns the target user's presence record.
- Background game monitoring uses a Redis lock named `lock:game-monitor`.
  - Only the lock owner processes timeout/auto-abort work on each poll.

PostgreSQL remains authoritative for users, games, moves, chat messages, ratings, tournaments, scheduled matches, and payment emulator data. Redis data can expire or be rebuilt from PostgreSQL-backed state during reconnect flows; see `backend/domains/game/application/services.py` and `backend/app/ws_entry.py` for reconnect state loading.

## Current Remaining Single-Deployment Assumptions / Not Yet Proven

- The default Compose stack runs one backend container.
- Uploaded media is stored on the local filesystem under `backend/storage/`.
- WebSocket authentication uses a query token for the SPA connection flow.
- Payment flows use an internal emulator, not a real provider.
- Load testing and production load-balancer configuration are not part of the current verification baseline.

## Recommended Next Infra Move

The next deployment-hardening step is:

1. Keep PostgreSQL as the system of record.
2. Run multiple stateless backend instances behind one load balancer.
3. Move media from local disk to shared object storage before multi-instance production use.
4. Add load tests for matchmaking, room fanout, reconnect, and timeout flows.

This remains a scaling mechanism change, not a rewrite. FastAPI, the current domain structure, and browser-local Stockfish analysis can stay as they are.

## What Does Not Need To Change

- No Redis-backed move engine
- No rewrite away from FastAPI
- No speculative microservice split
- No change to browser-local Stockfish analysis ownership
