# 1. One transaction per move, owned by the service

Status: accepted

Decided in `55ba893` and `8a4b8e8` (2026-07-29). Written down as an ADR on
2026-07-30, when the decisions that until then lived only in commit messages were
filed here.

## Context

Making a move writes two rows: the move itself, and the game (new `fen`, new clock
values, possibly a result). The repository used to commit inside each write, so a
move was **two** transactions:

```
add_move(...)  -> INSERT ... COMMIT
update(game)   -> UPDATE ... COMMIT
```

A failure between them -- a lost connection, a constraint error, a cancelled request
-- left the first commit standing. The move row was visible to every reader while
the game still held the previous position and the previous clocks. The board and its
own move list disagreed, and nothing in the system would ever reconcile them,
because from the game's point of view the move never happened.

Worse, it is the kind of bug that never appears in a happy-path test: both writes
succeed in every test that does not inject a failure.

## Decision

The transaction boundary belongs to the use case, not to the repository.

* The repository `flush`es so generated ids are available, and no longer commits on
  its own.
* `commit` and `rollback` are exposed on the repository interface
  (`domains/game/domain/repository.py`) and implemented over the session.
* `make_move` wraps both writes in one `try`, and on any exception rolls back and
  re-raises.

The WebSocket broadcast already ran after `make_move` returned, so it now
necessarily lands after the commit: nobody is told about a move that was rolled
back.

## Alternatives rejected

**Keep committing in the repository and compensate on failure.** The compensation
is another write that can fail for the same reasons as the first, and it has to run
in a code path that is already handling an exception. A transaction is the database
doing this correctly for free.

**Move both writes into one repository method.** It hides the boundary instead of
placing it: the next use case that needs two writes has the same problem, and the
repository grows a method per combination.

## Consequences

The service layer now owns durability, which means every test double standing in for
a session or repository has to implement `flush` and `rollback` -- and one of them
did not, which surfaced as `AttributeError: 'FakeSession' object has no attribute
'rollback'` masking the real assertion. Three further doubles were given a
`rollback` that states why it should never be reached, so the next such gap fails
with a sentence instead of an attribute error.

Covered by `test_make_move_rolls_back_move_when_game_update_fails`, which fails the
game update and asserts `persisted_moves == []` from a fresh repository. Against the
previous implementation it reported `assert [Move(id=1, ...)] == []`, which is the
whole point: the test was seen red before it was believed green.
