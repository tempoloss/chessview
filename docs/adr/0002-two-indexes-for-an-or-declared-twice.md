# 2. Two indexes for an OR, declared in the migration and on the model

Status: accepted

Decided in `8a4b8e8` (2026-07-29) and completed in `d69b84e` (2026-07-30). Written
down as an ADR on 2026-07-30, when the decisions that until then lived only in
commit messages were filed here.

## Context

A player's history is `list_by_user`: every game where `white_id = :me OR black_id
= :me`, newest first. Through migration 0010 no index covered either column, so
PostgreSQL read the whole `games` table for every history request.

Measured on PostgreSQL 16 with 60,000 games and 200 users, for a player with 600 of
them: `Seq Scan` with `Rows Removed by Filter: 59400`.

## Decision

One index per side of the `OR`, each with the sort column second:

```
ix_games_white_id_started_at_desc  (white_id, started_at DESC)
ix_games_black_id_started_at_desc  (black_id, started_at DESC)
```

Created by migration `0011_game_player_history_indexes` **and** declared in
`GameModel.__table_args__`. With both indexes the same query plans as `BitmapOr` over
two `Bitmap Index Scan`s and touches 600 rows.

## Alternatives rejected

**One composite index on `(white_id, black_id, started_at)`.** Serves neither branch
of the `OR`: a lookup on `black_id` cannot use an index whose leading column is
`white_id`.

**Rewriting the query as two queries with `UNION ALL`.** It would let each branch
use its own index and preserve order per branch, but it rewrites the repository and
the pagination for a plan the planner already produces from the `OR`.

**Declaring the indexes only in the migration.** This is what shipped first, and CI
rejected it. `alembic check` compares the models against the migrated database, so
indexes that exist in the database and not in the metadata read to autogenerate as
indexes nobody asked for: it proposed a `remove_index` for each and the step exited
255, in two jobs, on every push for a day. The schema is written down in two places
and both have to agree.

## Consequences

What these indexes do **not** do is remove the sort. `BitmapOr` does not preserve
index order, so the plan still runs a `top-N heapsort` -- over 600 rows instead of
60,000. The `started_at DESC` component earns its place in the single-sided variant
of the query, not in this one, and the primitives entry says so rather than implying
the index made the sort free.

Covered by `test_games_by_player_indexes_are_declared_in_migrations`, which asserts
the Alembic upgrade path declares both indexes with the filter column before
`started_at DESC`, and by `alembic check` in CI for the model side. The migration is
reversible: `downgrade` drops both, `upgrade` re-creates them.
