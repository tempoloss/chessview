from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import chess
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.dependencies import get_current_user_id
from domains.communication.application.services import ChatService, MAX_MESSAGE_LENGTH
from domains.communication.domain.exceptions import MessageTooLong
from domains.game.domain.clock import ClockSnapshot, active_player_id, active_remaining_time_ms
from domains.game.domain.entities import Game, Move
from domains.game.domain.exceptions import IllegalMove, NotYourTurn
from domains.game.domain.moves import apply_player_move
from domains.game.domain.outcomes import (
    abort_game,
    accept_draw,
    apply_board_outcome,
    clear_disconnect_state,
    pause_for_disconnect,
    resign_game,
    resume_after_reconnect,
    timeout_game,
)
from domains.game.domain.value_objects import Color, GameResult, GameStatus
from domains.game.presentation.serializers import (
    PlayerDirectory,
    player_directory_from_users,
    rating_delta_for_user,
    rating_summary,
    to_game_detail_response,
    to_game_list_item,
)
from domains.identity.application.commands import (
    LoginUserCommand,
    OAuthUserCommand,
    CompletePasswordResetCommand,
    RefreshTokenCommand,
    RegisterUserCommand,
    RequestPasswordResetCommand,
    UpdateProfileCommand,
)
from domains.identity.application.services import IdentityService
from domains.identity.domain.entities import User
from domains.identity.domain.exceptions import (
    DuplicateEmail,
    DuplicateUsername,
    InvalidCredentials,
    UserNotFound,
)
from domains.identity.face_verification.models import (
    FaceVerificationChallengeModel,
    FaceVerificationEventModel,
    FaceVerificationProfileModel,
    FaceVerificationSessionModel,
)
from domains.identity.face_verification.service import (
    FACE_TEMPLATE_PROVIDER,
    PASSKEY_PROVIDER,
    FaceVerificationService,
)
from domains.identity.infrastructure.models import UserModel
from domains.identity.presentation.mailer import send_password_reset_email
from domains.matchmaking.application.services import MatchmakingService
from domains.matchmaking.domain.exceptions import AlreadyInQueue, NotInQueue
from domains.payments.infrastructure.models import PaymentEventModel, PaymentIntentModel
from domains.payments.service import PaymentService
from domains.profiles.application.services import ProfileService
from domains.puzzles.application.services import PuzzleService
from domains.puzzles.domain.entities import Puzzle
from domains.puzzles.domain.exceptions import PuzzleNotFound
from domains.puzzles.domain.value_objects import PuzzleAttemptResult
from domains.rtc.application.services import SignalingService
from domains.scheduled_matches.infrastructure.models import ScheduledMatchModel
from domains.scheduled_matches.service import ScheduledMatchService
from domains.shop.application import ShopService
from domains.shop.infrastructure.models import ShopItemModel, UserShopItemModel
from domains.tournaments.domain.entities import TournamentPairing
from domains.tournaments.infrastructure.models import TournamentModel, TournamentPlayerModel
from domains.tournaments.presentation.serializers import (
    TournamentPlayerDirectory,
    count_games_played,
    player_directory_from_users as tournament_player_directory_from_users,
    to_tournament_detail_response,
    to_tournament_round_responses,
    to_tournament_standing_responses,
    to_tournament_summary_response,
)
from infrastructure.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from shared.ws_manager import ConnectionManager


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({field: str(value) for field, value in mapping.items()})

    async def expire(self, _key, _seconds):
        return None

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrank(self, key, member):
        ordered = self._ordered(key)
        return ordered.index(member) if member in ordered else None

    async def zrange(self, key, start, end):
        ordered = self._ordered(key)
        return ordered[start:] if end == -1 else ordered[start : end + 1]

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zrem(self, key, *members):
        for member in members:
            self.zsets.setdefault(key, {}).pop(member, None)

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        for member in members:
            self.sets.setdefault(key, set()).discard(member)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def get(self, key):
        return self.strings.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)
            self.zsets.pop(key, None)
            self.sets.pop(key, None)
            self.strings.pop(key, None)

    async def publish(self, channel, message):
        self.published.append((channel, message))

    async def eval(self, _script, _numkeys, key, expected):
        if self.strings.get(key) == expected or self.hashes.get(key, {}).get("connection_id") == expected:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            return 1
        return 0

    def _ordered(self, key):
        return [member for member, _score in sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])]


class InMemoryUserRepo:
    def __init__(self, users: list[User] | None = None) -> None:
        self.users = {user.id: user for user in users or []}

    async def create(self, user: User) -> User:
        self.users[user.id] = user
        return user

    async def get_by_email(self, email: str) -> User | None:
        return next((user for user in self.users.values() if user.email == email), None)

    async def get_by_username(self, username: str) -> User | None:
        return next((user for user in self.users.values() if user.username == username), None)

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def update(self, user: User) -> User:
        self.users[user.id] = user
        return user


def identity_service(repo: InMemoryUserRepo) -> IdentityService:
    return IdentityService(
        user_repo=repo,
        hash_password=lambda value: f"hashed:{value}",
        verify_password=lambda plain, hashed: hashed == f"hashed:{plain}",
        create_access_token=lambda user_id: f"access:{user_id}",
        create_refresh_token=lambda user_id: f"refresh:{user_id}",
        create_password_reset_token=lambda user_id: f"token:{user_id}",
        decode_token=lambda token: {"type": "refresh", "sub": token.removeprefix("refresh:")},
    )


@pytest.mark.asyncio
async def test_identity_service_register_login_refresh_oauth_and_profile_updates():
    repo = InMemoryUserRepo()
    service = identity_service(repo)

    registered = await service.register(
        RegisterUserCommand(username="alice", email="alice@example.com", password="secret")
    )
    user_id = UUID(registered["user"]["id"])

    assert registered["access_token"] == f"access:{user_id}"
    assert registered["refresh_token"] == f"refresh:{user_id}"
    assert repo.users[user_id].password_hash == "hashed:secret"

    with pytest.raises(DuplicateEmail):
        await service.register(RegisterUserCommand(username="other", email="alice@example.com", password="secret"))

    with pytest.raises(DuplicateUsername):
        await service.register(RegisterUserCommand(username="alice", email="other@example.com", password="secret"))

    logged_in = await service.login(LoginUserCommand(email="alice@example.com", password="secret"))
    assert logged_in["user"]["username"] == "alice"

    with pytest.raises(InvalidCredentials):
        await service.login(LoginUserCommand(email="alice@example.com", password="wrong"))

    assert await service.refresh(RefreshTokenCommand(refresh_token=f"refresh:{user_id}")) == {
        "access_token": f"access:{user_id}",
        "refresh_token": f"refresh:{user_id}",
        "token_type": "bearer",
    }

    service._decode_token = lambda _token: {"type": "access", "sub": str(user_id)}
    with pytest.raises(InvalidCredentials):
        await service.refresh(RefreshTokenCommand(refresh_token="not-refresh"))

    service._decode_token = lambda _token: (_ for _ in ()).throw(RuntimeError("bad token"))
    with pytest.raises(InvalidCredentials):
        await service.refresh(RefreshTokenCommand(refresh_token="broken"))

    service._decode_token = lambda _token: {"type": "refresh"}
    with pytest.raises(InvalidCredentials):
        await service.refresh(RefreshTokenCommand(refresh_token="missing-subject"))

    reset_ticket = await service.request_password_reset(
        RequestPasswordResetCommand(email="alice@example.com", frontend_url="http://localhost:5173")
    )
    assert reset_ticket is not None
    assert reset_ticket.email == "alice@example.com"
    assert reset_ticket.reset_url.startswith("http://localhost:5173/reset-password?token=reset:")

    assert await service.request_password_reset(
        RequestPasswordResetCommand(email="missing@example.com", frontend_url="http://localhost:5173")
    ) is None

    service._decode_token = lambda token: {"type": "password_reset", "sub": str(user_id)} if token == "token:" + str(user_id) else {}
    await service.complete_password_reset(CompletePasswordResetCommand(token=reset_ticket.token, password="new-secret"))
    assert repo.users[user_id].password_hash == "hashed:new-secret"

    with pytest.raises(InvalidCredentials):
        await service.login(LoginUserCommand(email="alice@example.com", password="secret"))
    assert (await service.login(LoginUserCommand(email="alice@example.com", password="new-secret")))["user"]["username"] == "alice"

    updated = await service.update_profile(UpdateProfileCommand(user_id=user_id, username="alice2", bio="hi"))
    assert updated.username == "alice2"
    assert updated.bio == "hi"

    avatar = await service.update_avatar(user_id, "/media/avatars/a.png")
    assert avatar.avatar_path == "/media/avatars/a.png"

    oauth = await service.oauth_flow(OAuthUserCommand(email="oauth@example.com", username="alice2"))
    assert oauth["user"]["username"].startswith("alice2_")

    with pytest.raises(UserNotFound):
        await service.get_profile(uuid4())


@pytest.mark.asyncio
async def test_chat_service_persists_and_rejects_oversized_messages():
    class Repo:
        def __init__(self) -> None:
            self.messages = []

        async def create(self, message):
            self.messages.append(message)
            return message

        async def list_by_game(self, game_id):
            return [message for message in self.messages if message.game_id == game_id]

    repo = Repo()
    service = ChatService(repo)
    game_id = uuid4()
    user_id = uuid4()

    message = await service.send_message(game_id, user_id, "good move")

    assert message.content == "good move"
    assert await service.get_messages(game_id) == [message]

    with pytest.raises(MessageTooLong):
        await service.send_message(game_id, user_id, "x" * (MAX_MESSAGE_LENGTH + 1))


@pytest.mark.asyncio
async def test_matchmaking_service_queue_lifecycle_and_pairing(monkeypatch):
    service = MatchmakingService(redis_client=FakeRedis(), clock_ms=lambda: 1000)
    first = uuid4()
    second = uuid4()
    third = uuid4()

    assert await service.join_queue(first, 1500, "5+0", 300_000, 0) == 1

    with pytest.raises(AlreadyInQueue):
        await service.join_queue(first, 1500, "5+0", 300_000, 0)

    assert await service.try_match(first) is None

    await service.join_queue(third, 1500, "3+0", 180_000, 0)
    assert await service.try_match(first) is None

    monkeypatch.setattr("domains.matchmaking.application.services.random.random", lambda: 0.75)
    await service.join_queue(second, 1520, "5+0", 300_000, 0)
    pair = await service.try_match(first)

    assert pair is not None
    assert pair.white_id == second
    assert pair.black_id == first
    assert pair.time_control_name == "5+0"

    with pytest.raises(NotInQueue):
        await service.leave_queue(first)

    await service.leave_queue(third)


@pytest.mark.asyncio
async def test_signaling_service_relays_or_reports_missing_opponent():
    class Manager:
        def __init__(self) -> None:
            self.sent = []
            self.errors = []
            self.opponent = "black"

        async def get_opponent_id(self, game_id, sender_id):
            return self.opponent

        async def send_error(self, user_id, code, message):
            self.errors.append((user_id, code, message))

        async def send_to_user(self, user_id, event_type, payload, game_id=None):
            self.sent.append((user_id, event_type, payload, game_id))

    manager = Manager()
    service = SignalingService(manager)

    await service.relay("game", "white", "rtc.offer", {"sdp": "offer"})
    assert manager.sent == [("black", "rtc.offer", {"sdp": "offer"}, "game")]

    manager.opponent = None
    await service.relay("game", "white", "rtc.answer", {})
    assert manager.errors == [("white", "NOT_IN_GAME", "Opponent not found in room")]


@pytest.mark.asyncio
async def test_connection_manager_sends_disconnects_and_tracks_rooms():
    class WebSocket:
        def __init__(self, *, fail_send: bool = False) -> None:
            self.sent = []
            self.closed = []
            self.fail_send = fail_send

        async def send_text(self, message):
            if self.fail_send:
                raise RuntimeError("send failed")
            self.sent.append(message)

        async def close(self, code):
            self.closed.append(code)

    manager = ConnectionManager(redis_client=FakeRedis(), instance_id="instance-a")
    old = WebSocket()
    new = WebSocket()

    await manager.connect("white", old)
    await manager.connect("white", new)
    assert old.closed == [4000]
    assert manager.is_current_connection("white", new) is True

    await manager.join_room("game", "white")
    await manager.join_room("game", "black")
    assert await manager.get_opponent_id("game", "white") == "black"

    await manager.send_to_user("white", "move", {"uci": "e2e4"}, game_id="game")
    assert '"type": "move"' in new.sent[0]

    broken = WebSocket(fail_send=True)
    await manager.connect("black", broken)
    await manager.send_error("black", "BAD", "Bad event")
    assert "black" not in manager.active_connections

    await manager.leave_room("game", "white")
    assert "game" not in manager.game_rooms


@pytest.mark.asyncio
async def test_security_tokens_and_current_user_dependency(monkeypatch):
    user_id = str(uuid4())
    hashed = hash_password("secret")

    assert verify_password("secret", hashed) is True
    assert verify_password("wrong", hashed) is False

    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    assert decode_token(access)["type"] == "access"
    assert decode_token(refresh)["type"] == "refresh"

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=access)
    assert await get_current_user_id(credentials) == user_id

    monkeypatch.setattr("app.dependencies.decode_token", lambda _token: {"type": "refresh", "sub": user_id})
    with pytest.raises(HTTPException):
        await get_current_user_id(credentials)

    monkeypatch.setattr("app.dependencies.decode_token", lambda _token: {"type": "access"})
    with pytest.raises(HTTPException):
        await get_current_user_id(credentials)

    monkeypatch.setattr("app.dependencies.decode_token", lambda _token: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(HTTPException):
        await get_current_user_id(credentials)


def test_game_clock_moves_and_lifecycle_edges():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    white_id = uuid4()
    black_id = uuid4()
    game = Game(
        white_id=white_id,
        black_id=black_id,
        initial_time_ms=60_000,
        increment_ms=1_000,
        white_time_ms=60_000,
        black_time_ms=60_000,
        last_clock_started_at=now - timedelta(seconds=2),
    )

    assert active_player_id(game) == white_id

    with pytest.raises(NotYourTurn):
        apply_player_move(
            game, user_id=black_id, uci="e2e4", move_number=1, now=now, previous_moves=()
        )

    with pytest.raises(IllegalMove):
        apply_player_move(
            game, user_id=white_id, uci="not-a-move", move_number=1, now=now, previous_moves=()
        )

    move = apply_player_move(
        game, user_id=white_id, uci="e2e4", move_number=1, now=now, previous_moves=()
    )
    assert move.fen_after == game.fen
    assert game.white_time_ms == 59_000
    assert game.black_time_ms == 60_000

    snapshot = ClockSnapshot(
        time_control_name="1+1",
        initial_time_ms=60_000,
        increment_ms=1_000,
        white_time_ms=50_000,
        black_time_ms=45_000,
        active_color=Color.BLACK,
        is_paused=False,
        pause_reason=None,
        disconnected_player_id=None,
        grace_deadline_at=None,
        last_updated_at=now,
    )

    pause_for_disconnect(game, black_id, snapshot, now, 15)
    assert game.disconnected_player_id == black_id
    assert active_remaining_time_ms(game, now) == 1

    resume_after_reconnect(game, now)
    assert game.disconnected_player_id is None
    assert game.last_clock_started_at == now

    timeout_game(game, black_id, snapshot, now, "clock")
    assert game.status == GameStatus.TIMEOUT
    assert game.result == GameResult.WHITE_WINS
    assert game.black_time_ms == 0

    game.status = GameStatus.ACTIVE
    resign_game(game, white_id, snapshot, now)
    assert game.status == GameStatus.RESIGNED
    assert game.result == GameResult.BLACK_WINS

    game.status = GameStatus.ACTIVE
    accept_draw(game, snapshot, now)
    assert game.status == GameStatus.DRAW
    assert game.result == GameResult.DRAW

    game.status = GameStatus.ACTIVE
    abort_game(game, snapshot, now, "too early")
    assert game.status == GameStatus.ABORTED
    assert game.termination_reason == "too early"

    game.disconnected_player_id = black_id
    game.disconnect_grace_deadline_at = now
    clear_disconnect_state(game)
    assert game.disconnect_grace_deadline_at is None


def test_board_outcome_branches():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    game = Game()

    board = chess.Board("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
    apply_board_outcome(game, board, now)
    assert game.status == GameStatus.CHECKMATE
    assert game.result == GameResult.WHITE_WINS

    game = Game()
    board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    apply_board_outcome(game, board, now)
    assert game.status == GameStatus.STALEMATE
    assert game.result == GameResult.DRAW

    game = Game()
    board = chess.Board("8/8/8/8/8/8/6k1/6K1 w - - 0 1")
    apply_board_outcome(game, board, now)
    assert game.status == GameStatus.DRAW


def test_game_serializers_cover_fallbacks_and_rating_summaries():
    white_id = uuid4()
    black_id = uuid4()
    game = Game(
        white_id=white_id,
        black_id=black_id,
        rated=True,
        white_rating_before=1400,
        black_rating_before=1500,
        white_rating_after=1410,
        black_rating_after=1490,
    )
    users = {
        white_id: SimpleNamespace(username="White", rating=1410, avatar_path="/w.png"),
    }
    players = player_directory_from_users(users)
    move = Move(game_id=game.id, user_id=white_id, uci="e2e4", fen_after="fen", move_number=1)

    assert players.detail(black_id).username == "?"
    assert players.brief(white_id).username == "White"
    assert rating_delta_for_user(game, white_id) == 10
    assert rating_delta_for_user(Game(rated=False), white_id) is None
    assert rating_summary(1400, None) is None
    assert rating_summary(1400, 1415).delta == 15

    list_item = to_game_list_item(game, white_id, players, move_count=1)
    assert list_item.opponent.username == "?"
    assert list_item.my_color == "white"

    detail = to_game_detail_response(
        game,
        [move],
        players,
        {
            "time_control_name": "10+0",
            "initial_time_ms": 600_000,
            "increment_ms": 0,
            "white_time_ms": 600_000,
            "black_time_ms": 600_000,
            "active_color": "white",
            "is_paused": False,
            "pause_reason": None,
            "disconnected_player_id": None,
            "grace_deadline_at": None,
            "last_updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert detail.moves[0].username == "White"
    assert detail.black.username == "?"


def test_tournament_serializers_cover_byes_standings_and_details():
    owner_id = uuid4()
    player_id = uuid4()
    missing_id = uuid4()
    tournament_id = uuid4()
    users = {
        owner_id: SimpleNamespace(username="Owner", rating=1800),
        player_id: SimpleNamespace(username="Player", rating=1500),
    }
    directory = tournament_player_directory_from_users(users)
    owner = directory.get(owner_id)
    tournament = SimpleNamespace(
        id=tournament_id,
        name="Open",
        time_control_name="5+0",
        tournament_type="swiss",
        entry_fee_cents=0,
        status="active",
        current_round=1,
        total_rounds=3,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        finished_at=None,
    )
    summary = to_tournament_summary_response(
        tournament,
        owner,
        player_count=2,
        viewer_is_member=True,
        viewer_is_owner=False,
    )

    assert summary.owner.username == "Owner"
    assert TournamentPlayerDirectory({}).get(missing_id, 1600).rating == 1600

    bye_pairing = SimpleNamespace(
        id=1,
        round_number=1,
        white_id=player_id,
        black_id=None,
        game_id=None,
        result="bye",
    )
    game_id = uuid4()
    played_pairing = TournamentPairing(
        id=2,
        tournament_id=tournament_id,
        round_number=1,
        white_id=owner_id,
        black_id=player_id,
        game_id=game_id,
        result="1-0",
    )
    assert count_games_played([bye_pairing, played_pairing]) == {player_id: 2, owner_id: 1}

    rounds = to_tournament_round_responses(
        [SimpleNamespace(round_number=1)],
        [bye_pairing, played_pairing],
        directory,
        {game_id: "active"},
    )
    assert rounds[0].pairings[0].black is None
    assert rounds[0].pairings[1].game_status == "active"

    standings = to_tournament_standing_responses(
        [SimpleNamespace(user_id=player_id, seed_rating=1500, score=1.0)],
        directory,
        {player_id: 2},
    )
    detail = to_tournament_detail_response(summary, standings=standings, rounds=rounds)

    assert detail.standings[0].games_played == 2
    assert detail.rounds == rounds


class ScalarList:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values

    def first(self):
        return self._values[0] if self._values else None


class InMemoryFaceSession:
    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        self.user = UserModel(
            id=user_id,
            username="Face User",
            email="face@example.com",
            password="hash",
            rating=1200,
        )
        self.objects: dict[type, list[object]] = {
            FaceVerificationProfileModel: [],
            FaceVerificationSessionModel: [],
            FaceVerificationChallengeModel: [],
            FaceVerificationEventModel: [],
        }
        self.commits = 0
        self.flushes = 0

    def add(self, item):
        model = type(item)
        if hasattr(item, "id") and getattr(item, "id", None) is None:
            item.id = uuid4()
        now = datetime.now(timezone.utc)
        if hasattr(item, "created_at") and getattr(item, "created_at", None) is None:
            item.created_at = now
        if hasattr(item, "updated_at") and getattr(item, "updated_at", None) is None:
            item.updated_at = None
        self.objects.setdefault(model, []).append(item)

    async def get(self, model, key):
        if model is UserModel and key == self.user_id:
            return self.user
        for item in self.objects.get(model, []):
            if getattr(item, "id", None) == key:
                return item
        return None

    async def execute(self, statement):
        model = statement.column_descriptions[0]["entity"]
        values = list(self.objects.get(model, []))
        if model is FaceVerificationProfileModel:
            values = [item for item in values if item.user_id == self.user_id]
            text = str(statement)
            if PASSKEY_PROVIDER in text:
                values = [item for item in values if item.provider == PASSKEY_PROVIDER and item.status == "enrolled"]
            if FACE_TEMPLATE_PROVIDER in text:
                values = [item for item in values if item.provider == FACE_TEMPLATE_PROVIDER and item.status == "enrolled"]
            priority = {FACE_TEMPLATE_PROVIDER: 1, PASSKEY_PROVIDER: 1}
            values.sort(key=lambda item: (priority.get(item.provider, 0), item.created_at), reverse=True)
        return ScalarList(values)

    async def flush(self):
        self.flushes += 1
        for values in self.objects.values():
            for item in values:
                if hasattr(item, "id") and getattr(item, "id", None) is None:
                    item.id = uuid4()

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        if hasattr(item, "id") and getattr(item, "id", None) is None:
            item.id = uuid4()
        now = datetime.now(timezone.utc)
        if hasattr(item, "created_at") and getattr(item, "created_at", None) is None:
            item.created_at = now


@pytest.mark.asyncio
async def test_face_verification_service_enrolls_profiles_sessions_and_passkeys():
    user_id = uuid4()
    session = InMemoryFaceSession(user_id)
    service = FaceVerificationService(session)

    with pytest.raises(HTTPException):
        await service.enroll(user_id, "camera", consent=False)

    profile = await service.enroll(user_id, "camera", consent=True)
    assert service.profile_response(profile).device_label == "camera"

    started = await service.start_session(user_id=user_id, game_id=uuid4(), tournament_id=None, scheduled_match_id=None)
    assert started.status == "pending"
    assert any(event.event_type == "session.started" for event in session.objects[FaceVerificationEventModel])

    submitted = await service.submit(started.id, user_id, "uncertain")
    assert submitted.status == "uncertain"
    assert service.session_response(submitted).reason is not None

    with pytest.raises(HTTPException):
        await service.submit(uuid4(), user_id, "success")

    with pytest.raises(HTTPException):
        await service.submit(started.id, uuid4(), "success")

    with pytest.raises(HTTPException):
        await service.start_passkey_enrollment(user_id=uuid4(), authenticator_attachment=None, device_label=None)

    enrollment_challenge = await service.start_passkey_enrollment(
        user_id=user_id,
        authenticator_attachment=None,
        device_label="laptop",
    )
    assert enrollment_challenge.payload["public_key"]["user"]["name"] == "face@example.com"

    with pytest.raises(HTTPException):
        await service.complete_passkey_enrollment(user_id=user_id, challenge_id=enrollment_challenge.id, credential={})

    passkey_profile = await service.complete_passkey_enrollment(
        user_id=user_id,
        challenge_id=enrollment_challenge.id,
        credential={"id": "cred-1", "raw_id": "raw-1"},
    )
    assert passkey_profile.provider == PASSKEY_PROVIDER
    assert passkey_profile.credential_id == "cred-1"

    with pytest.raises(HTTPException):
        await service.complete_passkey_enrollment(
            user_id=user_id,
            challenge_id=enrollment_challenge.id,
            credential={"id": "cred-2"},
        )

    verification_challenge, verification = await service.start_passkey_verification(
        user_id=user_id,
        game_id=None,
        tournament_id=None,
        scheduled_match_id=None,
    )
    assert verification.provider == PASSKEY_PROVIDER
    assert verification_challenge.payload["public_key"]["allowCredentials"][0]["id"] == "raw-1"

    completed = await service.complete_passkey_verification(
        user_id=user_id,
        challenge_id=verification_challenge.id,
        credential={
            "id": "cred-1",
            "response": {
                "client_data_json": "client",
                "authenticator_data": "auth",
                "signature": "sig",
            },
        },
    )
    assert completed.status == "verified"
    assert completed.confidence == 1.0

    result = FaceVerificationService.verify_passkey_assertion(
        challenge={"challenge": "c"},
        assertion={"credential_id": "cred-1"},
        enrolled_credential_id="cred-1",
    )
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_face_verification_service_face_templates_and_failure_paths():
    user_id = uuid4()
    session = InMemoryFaceSession(user_id)
    service = FaceVerificationService(session)

    with pytest.raises(HTTPException):
        await service.enroll_face_template(user_id=user_id, device_label=None, consent=False, face_sample=" face ")

    with pytest.raises(HTTPException):
        await service.verify_live_face_sample(user_id=user_id, face_sample="face")

    profile = await service.enroll_face_template(
        user_id=user_id,
        device_label=None,
        consent=True,
        face_sample="  same   face  ",
    )
    assert profile.device_label == "Primary camera"
    assert FaceVerificationService._normalize_face_sample("  same   face  ") == "same face"

    with pytest.raises(HTTPException):
        await service.enroll_face_template(
            user_id=user_id,
            device_label="replacement camera",
            consent=True,
            face_sample="same face",
        )

    verified = await service.verify_live_face_sample(
        user_id=user_id,
        face_sample="same face",
        game_id=None,
        tournament_id=uuid4(),
        scheduled_match_id=None,
    )
    assert verified.status == "verified"

    failed = await service.verify_live_face_sample(
        user_id=user_id,
        face_sample="different face",
        game_id=uuid4(),
        tournament_id=None,
        scheduled_match_id=None,
    )
    assert failed.status == "failed"

    assert await service.stop_game_after_failed_verification(verified) is None
    assert await service.stop_game_after_failed_verification(failed) is None


class GenericMemorySession:
    def __init__(self) -> None:
        self.store: dict[object, object] = {}
        self.added: list[object] = []
        self.commits = 0

    def add(self, item):
        if hasattr(item, "id") and getattr(item, "id", None) is None:
            item.id = uuid4()
        now = datetime.now(timezone.utc)
        if hasattr(item, "created_at") and getattr(item, "created_at", None) is None:
            item.created_at = now
        if hasattr(item, "updated_at") and getattr(item, "updated_at", None) is None:
            item.updated_at = None
        self.added.append(item)
        if isinstance(item, TournamentPlayerModel):
            self.store[(TournamentPlayerModel, (item.tournament_id, item.user_id))] = item
        if isinstance(item, UserShopItemModel):
            self.store[(UserShopItemModel, (item.user_id, item.item_id))] = item
        key = getattr(item, "id", None)
        if key is not None:
            self.store[(type(item), key)] = item

    async def get(self, model, key):
        return self.store.get((model, key))

    async def execute(self, _statement):
        entities = [description.get("entity") for description in getattr(_statement, "column_descriptions", [])]
        if entities == [UserShopItemModel, ShopItemModel]:
            rows = []
            for item in self.store.values():
                if isinstance(item, UserShopItemModel):
                    shop_item = self.store.get((ShopItemModel, item.item_id))
                    if shop_item is not None:
                        rows.append((item, shop_item))
            return ScalarList(rows)
        if entities == [UserShopItemModel]:
            return ScalarList([item for item in self.store.values() if isinstance(item, UserShopItemModel)])
        if entities == [ShopItemModel]:
            return ScalarList([item for item in self.store.values() if isinstance(item, ShopItemModel)])
        return ScalarList([item for item in self.store.values() if isinstance(item, ScheduledMatchModel)])

    async def flush(self):
        for item in self.added:
            if hasattr(item, "id") and getattr(item, "id", None) is None:
                item.id = uuid4()
                self.store[(type(item), item.id)] = item

    async def commit(self):
        self.commits += 1

    async def refresh(self, item):
        if hasattr(item, "id") and getattr(item, "id", None) is None:
            item.id = uuid4()
        now = datetime.now(timezone.utc)
        if hasattr(item, "created_at") and getattr(item, "created_at", None) is None:
            item.created_at = now


@pytest.mark.asyncio
async def test_payment_service_creates_simulates_confirms_and_refunds():
    session = GenericMemorySession()
    service = PaymentService(session)
    user_id = uuid4()
    tournament_id = uuid4()
    match_id = uuid4()
    now = datetime.now(timezone.utc)
    tournament = TournamentModel(
        id=tournament_id,
        owner_id=uuid4(),
        name="Paid Open",
        time_control_name="5+0",
        entry_fee_cents=250,
        status="registration",
        current_round=0,
        total_rounds=3,
    )
    user = UserModel(id=user_id, username="payer", email="payer@example.com", password="hash", rating=1500, coins=1000)
    match = ScheduledMatchModel(
        id=match_id,
        creator_user_id=user_id,
        invited_user_id=uuid4(),
        white_player_id=user_id,
        black_player_id=uuid4(),
        starts_at=now,
        status="accepted",
        metadata_json={"match_fee_cents": 100},
    )
    session.store[(TournamentModel, tournament_id)] = tournament
    session.store[(UserModel, user_id)] = user
    session.store[(ScheduledMatchModel, match_id)] = match

    with pytest.raises(HTTPException):
        await service.create_entry_payment(uuid4(), user_id)

    payment = await service.create_entry_payment(tournament_id, user_id)
    assert payment.amount_cents == 250
    assert any(isinstance(item, PaymentEventModel) and item.type == "payment.created" for item in session.added)

    with pytest.raises(HTTPException):
        await service.simulate(uuid4(), "success")
    with pytest.raises(HTTPException):
        await service.simulate(payment.id, "unknown")

    succeeded = await service.simulate(payment.id, "success")
    assert succeeded.status == "succeeded"
    assert user.coins == 750
    assert session.store[(TournamentPlayerModel, (tournament_id, user_id))].status == "active"

    response = PaymentService.to_response(succeeded)
    assert response.subject_type == "tournament"

    refunded = await service.simulate(payment.id, "refunded")
    assert refunded.status == "refunded"
    assert user.coins == 1000
    assert session.store[(TournamentPlayerModel, (tournament_id, user_id))].status == "withdrawn"

    scheduled_payment = await service.create_scheduled_match_payment(match_id, user_id)
    assert PaymentService.to_response(scheduled_payment).subject_type == "scheduled_match"
    await service.simulate(scheduled_payment.id, "success")
    assert match.metadata_json["payment_status"] == "paid"

    with pytest.raises(HTTPException):
        await service.create_scheduled_match_payment(match_id, uuid4())


@pytest.mark.asyncio
async def test_shop_service_purchases_and_equips_backend_inventory():
    session = GenericMemorySession()
    user_id = uuid4()
    user = UserModel(id=user_id, username="shopper", email="shopper@example.com", password="hash", rating=1500, coins=1000)
    board = ShopItemModel(
        id=1,
        sku="board-test",
        name="Test Board",
        price=300,
        type="board",
        rarity="rare",
        description="Test board",
        asset_key="test",
        metadata_json={"light": "#fff", "dark": "#000"},
        consumable=False,
        is_active=True,
    )
    session.store[(UserModel, user_id)] = user
    session.store[(ShopItemModel, board.id)] = board

    service = ShopService(session)
    updated_user, purchased = await service.purchase(user_id, board.id)

    assert updated_user.coins == 700
    assert purchased.owned is True
    assert session.store[(UserShopItemModel, (user_id, board.id))].quantity == 1

    with pytest.raises(HTTPException):
        await service.purchase(user_id, board.id)

    equipped_user, equipped = await service.equip(user_id, board.id)
    assert equipped_user.equipped_board_sku == "board-test"
    assert equipped.equipped is True


def test_password_reset_mailer_sends_configured_smtp(monkeypatch):
    sent_messages = []
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def starttls(self):
            calls.append(("starttls",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr("domains.identity.presentation.mailer.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_PORT", 587)
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_USERNAME", "user@gmail.com")
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_PASSWORD", "app-password")
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_FROM_EMAIL", "user@gmail.com")
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_USE_TLS", True)
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_USE_SSL", False)
    monkeypatch.setattr("domains.identity.presentation.mailer.settings.SMTP_TIMEOUT_SECONDS", 7)

    assert send_password_reset_email("player@example.com", "https://example.test/reset") is True
    assert calls == [
        ("connect", "smtp.gmail.com", 587, 7),
        ("starttls",),
        ("login", "user@gmail.com", "app-password"),
    ]
    assert sent_messages[0]["To"] == "player@example.com"


@pytest.mark.asyncio
async def test_scheduled_match_service_lists_creates_transitions_and_serializes():
    session = GenericMemorySession()
    service = ScheduledMatchService(session)
    creator_id = uuid4()
    invited_id = uuid4()
    starts_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    with pytest.raises(HTTPException):
        await service.create_invitation(
            creator_user_id=creator_id,
            invited_user_id=creator_id,
            starts_at=starts_at,
            expires_at=None,
            metadata={},
        )

    match = await service.create_invitation(
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        starts_at=starts_at,
        expires_at=None,
        metadata={"note": "arena"},
    )
    assert match.status == "pending_acceptance"

    listed = await service.list_for_user(creator_id)
    assert match in listed

    accepted = await service.transition(match.id, invited_id, "accepted")
    assert accepted.status == "accepted"

    with pytest.raises(HTTPException):
        await service.transition(uuid4(), invited_id, "accepted")

    rescheduled = await service.reschedule(match.id, creator_id, starts_at + timedelta(minutes=5), None)
    assert rescheduled.status == "rescheduled"

    with pytest.raises(HTTPException):
        await service.reschedule(match.id, uuid4(), starts_at, None)

    match.status = "live"
    match.game_id = uuid4()
    started = await service.start(match.id, creator_id)
    assert started.status == "live"

    payload = ScheduledMatchService.to_response(started)
    assert payload.metadata["note"] == "arena"

    with pytest.raises(HTTPException):
            ScheduledMatchService._resolve_match_time_control(
                SimpleNamespace(time_control_name="custom", initial_time_ms=0, increment_ms=0)
            )


@pytest.mark.asyncio
async def test_puzzle_and_profile_services_cover_empty_and_not_found_paths():
    user_id = uuid4()
    puzzle = Puzzle(
        fen="8/8/8/8/8/8/8/K6k w - - 0 1",
        solution_moves=["a1a2"],
        rating=800,
        themes=["endgame"],
    )

    class PuzzleRepo:
        def __init__(self) -> None:
            self.attempts = {}

        async def list_puzzles(self, page, size):
            return [puzzle], 1

        async def list_attempts(self, _user_id, puzzle_ids):
            return {puzzle_id: self.attempts[puzzle_id] for puzzle_id in puzzle_ids if puzzle_id in self.attempts}

        async def get_puzzle(self, puzzle_id):
            return puzzle if puzzle_id == puzzle.id else None

        async def get_attempt(self, _user_id, puzzle_id):
            return self.attempts.get(puzzle_id)

        async def get_random_puzzle(self, exclude_id=None):
            return None if exclude_id == puzzle.id else puzzle

        async def save_attempt(self, attempt):
            self.attempts[attempt.puzzle_id] = attempt
            return attempt

    puzzle_repo = PuzzleRepo()
    puzzle_service = PuzzleService(puzzle_repo)

    overview, total = await puzzle_service.list_puzzles(user_id)
    assert total == 1
    assert overview[0].attempt is None

    random_overview = await puzzle_service.get_random_puzzle(user_id)
    assert random_overview.puzzle == puzzle

    with pytest.raises(PuzzleNotFound):
        await puzzle_service.get_puzzle(user_id, uuid4())

    with pytest.raises(PuzzleNotFound):
        await puzzle_service.get_random_puzzle(user_id, exclude_id=puzzle.id)

    attempt = await puzzle_service.record_attempt(user_id, puzzle.id, PuzzleAttemptResult.FAILED)
    assert attempt.attempts_count == 1
    solved = await puzzle_service.record_attempt(user_id, puzzle.id, PuzzleAttemptResult.SOLVED)
    assert solved.solved is True

    class ProfileRepo:
        async def get_profile_summary(self, profile_id, recent_game_limit):
            return SimpleNamespace(id=profile_id, recent_game_limit=recent_game_limit) if profile_id == user_id else None

        async def get_top_profiles(self, limit, category):
            return [SimpleNamespace(limit=limit, category=category)]

        async def search_players(self, query, limit):
            return [SimpleNamespace(query=query, limit=limit)]

    profile_service = ProfileService(ProfileRepo())
    assert (await profile_service.get_profile(user_id, recent_game_limit=3)).recent_game_limit == 3
    with pytest.raises(UserNotFound):
        await profile_service.get_profile(uuid4())
    assert await profile_service.search_players("   ") == []
    assert (await profile_service.search_players(" alice ", limit=1))[0].query == "alice"
    assert (await profile_service.get_top_players(limit=2))[0].limit == 2
