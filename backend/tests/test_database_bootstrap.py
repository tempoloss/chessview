import pytest

from domains.admin.infrastructure import seed as admin_seed
from infrastructure.database_bootstrap import initialize_database
from infrastructure.database_migrations import ALEMBIC_SCRIPT_PATH, HEAD_REVISION, to_migration_database_url


MAX_ALEMBIC_VERSION_LENGTH = 32


@pytest.mark.asyncio
async def test_initialize_database_runs_migrations_before_seeding(monkeypatch):
    calls: list[object] = []
    engine = object()

    def fake_register_models() -> None:
        calls.append("register")

    async def fake_run_database_migrations() -> None:
        calls.append("migrate")

    async def fake_seed_starter_puzzles(received_engine) -> None:
        calls.append(("seed", received_engine))

    async def fake_seed_first_admin(received_engine) -> None:
        calls.append(("admin", received_engine))

    async def fake_seed_demo_tournaments(received_engine) -> None:
        calls.append(("demo", received_engine))

    async def fake_seed_default_shop_items(received_engine) -> None:
        calls.append(("shop", received_engine))

    monkeypatch.setattr("infrastructure.database_bootstrap.register_models", fake_register_models)
    monkeypatch.setattr("infrastructure.database_bootstrap.run_database_migrations", fake_run_database_migrations)
    monkeypatch.setattr("infrastructure.database_bootstrap.seed_starter_puzzles", fake_seed_starter_puzzles)
    monkeypatch.setattr("infrastructure.database_bootstrap.seed_default_shop_items", fake_seed_default_shop_items)
    monkeypatch.setattr("infrastructure.database_bootstrap.seed_demo_tournaments", fake_seed_demo_tournaments)
    monkeypatch.setattr("infrastructure.database_bootstrap.seed_first_admin", fake_seed_first_admin)

    await initialize_database(engine)

    assert calls == ["register", "migrate", ("seed", engine), ("shop", engine), ("demo", engine), ("admin", engine)]


def test_to_migration_database_url_rewrites_asyncpg_for_alembic():
    assert (
        to_migration_database_url("postgresql+asyncpg://user:password@db.example.invalid:5432/app")
        == "postgresql+psycopg://user:password@db.example.invalid:5432/app"
    )


def test_alembic_revision_ids_fit_version_table():
    assert len(HEAD_REVISION) <= MAX_ALEMBIC_VERSION_LENGTH

    for migration_file in (ALEMBIC_SCRIPT_PATH / "versions").glob("*.py"):
        namespace: dict[str, object] = {}
        exec(migration_file.read_text(encoding="utf-8"), namespace)
        revision = namespace.get("revision")
        assert isinstance(revision, str)
        assert len(revision) <= MAX_ALEMBIC_VERSION_LENGTH, migration_file.name


@pytest.mark.asyncio
async def test_seed_first_admin_creates_predictable_local_admin(monkeypatch):
    added_users = []

    class FakeResult:
        def scalar_one_or_none(self):
            return None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

        def add(self, user):
            added_users.append(user)

        async def commit(self):
            pass

    def fake_sessionmaker(_engine, expire_on_commit=False):
        assert expire_on_commit is False
        return lambda: FakeSession()

    monkeypatch.setattr(admin_seed, "async_sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(admin_seed.settings, "SEED_ADMIN_EMAIL", "admin@chessview.app")
    monkeypatch.setattr(admin_seed.settings, "SEED_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(admin_seed.settings, "SEED_ADMIN_PASSWORD", "admin123")
    monkeypatch.setattr(admin_seed, "hash_password", lambda password: f"hashed:{password}")

    await admin_seed.seed_first_admin(object())

    assert len(added_users) == 1
    assert added_users[0].email == "admin@chessview.app"
    assert added_users[0].username == "admin"
    assert added_users[0].password == "hashed:admin123"
    assert added_users[0].role == "admin"


class _AlembicOpRecorder:
    def __init__(self) -> None:
        self.indexes: dict[tuple[str, str], tuple[str, ...]] = {}

    def create_index(self, name, table_name, columns, **_kwargs):
        self.indexes[(table_name, name)] = tuple(str(column) for column in columns)

    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            return None

        return _noop


def test_games_by_player_indexes_are_declared_in_migrations():
    recorder = _AlembicOpRecorder()

    for migration_file in sorted((ALEMBIC_SCRIPT_PATH / "versions").glob("*.py")):
        namespace: dict[str, object] = {}
        exec(migration_file.read_text(encoding="utf-8"), namespace)
        namespace["op"] = recorder
        namespace["upgrade"]()

    assert recorder.indexes[("games", "ix_games_white_id_started_at_desc")] == (
        "white_id",
        "started_at DESC",
    )
    assert recorder.indexes[("games", "ix_games_black_id_started_at_desc")] == (
        "black_id",
        "started_at DESC",
    )
