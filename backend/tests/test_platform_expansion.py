from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dependencies import require_admin
from domains.identity.application.services import IdentityService
from domains.game.domain.value_objects import GameResult, GameStatus
from domains.game.application.commands import AcceptDrawCommand, IdentityVerificationFailureCommand, ResignCommand
from domains.game.application.services import GameService
from domains.game.domain.exceptions import GameAccessDenied
from domains.identity.face_verification.provider import LocalStubFaceVerificationProvider
from domains.identity.face_verification.service import (
    FaceVerificationService,
    has_completed_face_verification,
    is_game_participant,
    require_game_face_verification_access,
    should_stop_game_for_verification_session,
)
from domains.identity.domain.exceptions import UserNotFound
from domains.identity.infrastructure.models import UserModel
from domains.payments.service import (
    SUCCESS_SCENARIOS,
    TERMINAL_RELEASE_STATUSES,
    apply_refund_to_registration,
    apply_wallet_charge,
    apply_wallet_refund,
    payment_subject,
    occupies_tournament_slot,
)
from domains.ratings.infrastructure.repository import SqlAlchemyRatingRepository
from domains.profiles.application.head_to_head import HeadToHeadService
from domains.profiles.application.head_to_head import _Stats
from domains.profiles.infrastructure.repository import SqlAlchemyProfileRepository
from domains.scheduled_matches.service import validate_scheduled_match_start, validate_scheduled_match_transition
from domains.scheduled_matches.service import ScheduledMatchService
from domains.scheduled_matches.infrastructure.models import ScheduledMatchModel
from domains.tournaments.application.services import TournamentService
from domains.tournaments.infrastructure.models import TournamentModel, TournamentPairingModel
from domains.tournaments.infrastructure.models import TournamentPlayerModel
from domains.tournaments.domain.entities import TournamentPairing, TournamentPlayer
from domains.tournaments.domain.services import (
    buchholz_scores,
    direct_encounter_score,
    performance_scores,
    plan_swiss_pairings,
)
from domains.tournaments.domain.value_objects import PairingResult, TournamentPlayerStatus
from domains.tournaments.domain.value_objects import TournamentType
from shared.time_controls import RatingSpeed, TimeControl, rating_speed_for_clock, rating_speed_for_time_control_name


def _player(score: float = 0.0, *, status=TournamentPlayerStatus.ACTIVE) -> TournamentPlayer:
    return TournamentPlayer(
        tournament_id=uuid4(),
        user_id=uuid4(),
        seed_rating=1200,
        score=score,
        status=status,
    )


def test_swiss_plan_assigns_bye_and_excludes_withdrawn_players():
    players = [
        _player(2),
        _player(1),
        _player(1),
        _player(0),
        _player(0),
        _player(0, status=TournamentPlayerStatus.WITHDRAWN),
    ]

    plan = plan_swiss_pairings(players, [], round_number=1)

    paired_ids = {pairing.white_id for pairing in plan.pairings}
    paired_ids.update(pairing.black_id for pairing in plan.pairings if pairing.black_id is not None)
    assert players[-1].user_id not in paired_ids
    assert sum(1 for pairing in plan.pairings if pairing.black_id is None) == 1
    assert "bye_assigned" in plan.warnings


def test_otb_tournament_type_is_first_class():
    assert TournamentType.OTB == "otb"


def test_swiss_plan_warns_when_rematch_is_unavoidable():
    players = [_player(1), _player(1)]
    prior = [
        TournamentPairing(
            tournament_id=players[0].tournament_id,
            round_number=1,
            white_id=players[0].user_id,
            black_id=players[1].user_id,
        )
    ]

    plan = plan_swiss_pairings(players, prior, round_number=2)

    assert len(plan.pairings) == 1
    assert "rematch_unavoidable" in plan.warnings


def test_tiebreak_helpers_compute_buchholz_direct_and_performance():
    first = _player(2)
    second = _player(1)
    third = _player(0)
    pairings = [
        TournamentPairing(
            tournament_id=first.tournament_id,
            round_number=1,
            white_id=first.user_id,
            black_id=second.user_id,
            result=PairingResult.WHITE_WINS,
        ),
        TournamentPairing(
            tournament_id=first.tournament_id,
            round_number=2,
            white_id=third.user_id,
            black_id=first.user_id,
            result=PairingResult.BLACK_WINS,
        ),
    ]

    buchholz = buchholz_scores([first, second, third], pairings)

    assert buchholz[first.user_id] == pytest.approx(1.0)
    assert direct_encounter_score(first.user_id, second.user_id, pairings) == pytest.approx(1.0)
    assert performance_scores([first, second, third])[first.user_id] > performance_scores([first, second, third])[third.user_id]


def test_face_verification_stub_statuses_are_explicit():
    provider = LocalStubFaceVerificationProvider()

    assert provider.verify(None).status == "verified"
    assert provider.verify("fail").status == "failed"
    assert provider.verify("uncertain").status == "uncertain"


def test_identity_auth_response_includes_role_for_separate_admin_service():
    service = IdentityService(
        user_repo=SimpleNamespace(),
        hash_password=lambda value: value,
        verify_password=lambda _plain, _hashed: True,
        create_access_token=lambda user_id: f"access-{user_id}",
        create_refresh_token=lambda user_id: f"refresh-{user_id}",
        create_password_reset_token=lambda user_id: f"reset-{user_id}",
        decode_token=lambda token: {"sub": token, "type": "refresh"},
    )
    user_id = uuid4()
    response = service._build_auth_response(
        SimpleNamespace(
            id=user_id,
            username="Admin",
            email="admin@example.com",
            rating=1500,
            role="admin",
            banned_at=None,
            bio=None,
            avatar_path=None,
            created_at=datetime.now(timezone.utc),
        )
    )

    assert response["user"]["role"] == "admin"
    assert response["user"]["banned_at"] is None


def test_payment_emulator_status_mapping_covers_required_scenarios():
    assert SUCCESS_SCENARIOS == {
        "success": "succeeded",
        "pending": "pending",
        "failed": "failed",
        "cancelled": "cancelled",
        "expired": "expired",
        "refunded": "refunded",
        "disputed": "disputed",
    }
    assert TERMINAL_RELEASE_STATUSES == {"failed", "cancelled", "expired"}


def test_payment_slot_occupancy_tracks_pending_expiration_and_success():
    now = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)

    assert occupies_tournament_slot("created", None, now) is False
    assert occupies_tournament_slot("pending", now + timedelta(minutes=5), now) is True
    assert occupies_tournament_slot("pending", now - timedelta(seconds=1), now) is False
    assert occupies_tournament_slot("succeeded", None, now) is True
    assert occupies_tournament_slot("failed", now + timedelta(minutes=5), now) is False
    assert occupies_tournament_slot("cancelled", now + timedelta(minutes=5), now) is False
    assert occupies_tournament_slot("expired", now + timedelta(minutes=5), now) is False
    assert occupies_tournament_slot("refunded", now + timedelta(minutes=5), now) is False
    assert occupies_tournament_slot("disputed", None, now) is True


def test_payment_subject_supports_tournament_and_scheduled_match_contexts():
    tournament_id = uuid4()
    scheduled_match_id = uuid4()

    assert payment_subject(SimpleNamespace(tournament_id=tournament_id, scheduled_match_id=None)) == (
        "tournament",
        tournament_id,
    )
    assert payment_subject(SimpleNamespace(tournament_id=None, scheduled_match_id=scheduled_match_id)) == (
        "scheduled_match",
        scheduled_match_id,
    )

    with pytest.raises(ValueError):
        payment_subject(SimpleNamespace(tournament_id=tournament_id, scheduled_match_id=scheduled_match_id))

    with pytest.raises(ValueError):
        payment_subject(SimpleNamespace(tournament_id=None, scheduled_match_id=None))


def test_wallet_charge_and_refund_use_chessview_coins_once():
    user = SimpleNamespace(coins=750)
    payment = SimpleNamespace(amount_cents=250, metadata_json={})

    apply_wallet_charge(user, payment)
    apply_wallet_charge(user, payment)

    assert user.coins == 500
    assert payment.metadata_json["wallet_debited"] is True
    assert payment.metadata_json["wallet_amount"] == 250

    apply_wallet_refund(user, payment)
    apply_wallet_refund(user, payment)

    assert user.coins == 750
    assert payment.metadata_json["wallet_refunded"] is True


def test_wallet_charge_rejects_insufficient_balance():
    user = SimpleNamespace(coins=100)
    payment = SimpleNamespace(amount_cents=250, metadata_json={})

    with pytest.raises(HTTPException):
        apply_wallet_charge(user, payment)

    assert user.coins == 100


def test_refund_marks_existing_registration_withdrawn_without_deleting_it():
    now = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    player = SimpleNamespace(status="active", withdrawn_at=None)

    apply_refund_to_registration(player, now)

    assert player.status == "withdrawn"
    assert player.withdrawn_at == now


@pytest.mark.asyncio
async def test_duplicate_payment_success_does_not_add_duplicate_registration():
    tournament_id = uuid4()
    user_id = uuid4()
    existing_player = SimpleNamespace(status="active", withdrawn_at=None)
    payment = SimpleNamespace(tournament_id=tournament_id, user_id=user_id)

    class FakeSession:
        added: list[object]

        def __init__(self) -> None:
            self.added = []

        async def get(self, model, key):
            if model is TournamentPlayerModel and key == (tournament_id, user_id):
                return existing_player
            return None

        def add(self, item):
            self.added.append(item)

    from domains.payments.service import PaymentService

    session = FakeSession()
    await PaymentService(session)._confirm_registration(payment)

    assert session.added == []


def test_scheduled_match_lifecycle_rejects_self_accept_and_start_before_acceptance():
    creator_id = uuid4()
    match = SimpleNamespace(
        creator_user_id=creator_id,
        invited_user_id=creator_id,
        white_player_id=creator_id,
        black_player_id=creator_id,
        status="pending_acceptance",
        game_id=None,
    )

    with pytest.raises(HTTPException):
        validate_scheduled_match_transition(match, creator_id, "accepted")

    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, creator_id)


def test_scheduled_match_unrelated_user_cannot_cancel():
    creator_id = uuid4()
    invited_id = uuid4()
    match = SimpleNamespace(
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        white_player_id=creator_id,
        black_player_id=invited_id,
        status="pending_acceptance",
        game_id=None,
    )

    with pytest.raises(HTTPException):
        validate_scheduled_match_transition(match, uuid4(), "cancelled")


def test_scheduled_match_start_is_limited_to_accepted_or_scheduled_states():
    creator_id = uuid4()
    invited_id = uuid4()
    match = SimpleNamespace(
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        white_player_id=creator_id,
        black_player_id=invited_id,
        status="accepted",
        game_id=None,
    )

    validate_scheduled_match_start(match, creator_id)
    validate_scheduled_match_start(match, invited_id)

    match.status = "declined"
    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, invited_id)

    match.status = "cancelled"
    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, creator_id)

    match.status = "live"
    match.game_id = uuid4()
    validate_scheduled_match_start(match, creator_id)


def test_paid_scheduled_match_cannot_start_until_payment_is_confirmed():
    creator_id = uuid4()
    invited_id = uuid4()
    match = SimpleNamespace(
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        white_player_id=creator_id,
        black_player_id=invited_id,
        status="accepted",
        game_id=None,
        starts_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        expires_at=None,
        metadata_json={"match_fee_cents": 250, "payment_status": "pending"},
    )

    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, creator_id)

    match.metadata_json["payment_status"] = "paid"
    validate_scheduled_match_start(match, invited_id)


def test_scheduled_match_start_rejects_future_or_expired_matches():
    creator_id = uuid4()
    invited_id = uuid4()
    match = SimpleNamespace(
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        white_player_id=creator_id,
        black_player_id=invited_id,
        status="accepted",
        game_id=None,
        starts_at=datetime.now(timezone.utc) + timedelta(hours=1),
        expires_at=None,
        metadata_json={},
    )

    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, creator_id)

    match.starts_at = datetime.now(timezone.utc) - timedelta(hours=2)
    match.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with pytest.raises(HTTPException):
        validate_scheduled_match_start(match, invited_id)


@pytest.mark.asyncio
async def test_scheduled_tournament_match_start_creates_game_and_links_pairing():
    creator_id = uuid4()
    white_id = uuid4()
    black_id = uuid4()
    tournament_id = uuid4()
    match_id = uuid4()
    pairing_id = 42
    match = ScheduledMatchModel(
        id=match_id,
        tournament_id=tournament_id,
        pairing_id=pairing_id,
        white_player_id=white_id,
        black_player_id=black_id,
        creator_user_id=creator_id,
        invited_user_id=black_id,
        starts_at=datetime.now(timezone.utc),
        status="accepted",
        metadata_json={},
    )
    pairing = TournamentPairingModel(
        id=pairing_id,
        tournament_id=tournament_id,
        round_number=1,
        white_id=white_id,
        black_id=black_id,
    )
    tournament = TournamentModel(
        id=tournament_id,
        owner_id=creator_id,
        name="Scheduled Swiss",
        time_control_name="3+2",
        initial_time_ms=180_000,
        increment_ms=2_000,
        status="active",
        current_round=1,
        total_rounds=3,
    )
    white = SimpleNamespace(id=white_id, rating=1510)
    black = SimpleNamespace(id=black_id, rating=1490)

    class FakeSession:
        def __init__(self) -> None:
            self.added = []

        async def get(self, model, key):
            if model is ScheduledMatchModel and key == match_id:
                return match
            if model is UserModel and key == white_id:
                return white
            if model is UserModel and key == black_id:
                return black
            if model is TournamentModel and key == tournament_id:
                return tournament
            if model is TournamentPairingModel and key == pairing_id:
                return pairing
            return None

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _item):
            return None

    started = await ScheduledMatchService(FakeSession()).start(match_id, white_id)

    assert started.status == "live"
    assert started.game_id is not None
    assert pairing.game_id == started.game_id


@pytest.mark.asyncio
async def test_live_scheduled_match_cannot_be_rescheduled():
    creator_id = uuid4()
    invited_id = uuid4()
    match_id = uuid4()
    match = ScheduledMatchModel(
        id=match_id,
        creator_user_id=creator_id,
        invited_user_id=invited_id,
        white_player_id=creator_id,
        black_player_id=invited_id,
        starts_at=datetime.now(timezone.utc),
        status="live",
        game_id=uuid4(),
        metadata_json={},
    )

    class FakeSession:
        async def get(self, model, key):
            if model is ScheduledMatchModel and key == match_id:
                return match
            return None

        async def commit(self):
            raise AssertionError("live match reschedule should not commit")

        async def refresh(self, _item):
            raise AssertionError("live match reschedule should not refresh")

    with pytest.raises(HTTPException) as exc_info:
        await ScheduledMatchService(FakeSession()).reschedule(
            match_id,
            creator_id,
            datetime.now(timezone.utc) + timedelta(days=1),
            None,
        )

    assert exc_info.value.status_code == 400
    assert match.status == "live"


def test_custom_tournament_time_control_falls_back_to_stored_clock_values():
    tournament = SimpleNamespace(
        time_control_name="25+10",
        initial_time_ms=1_500_000,
        increment_ms=10_000,
    )

    resolved = ScheduledMatchService._resolve_match_time_control(tournament)

    assert resolved == TimeControl(name="25+10", initial_time_ms=1_500_000, increment_ms=10_000)


def test_custom_tournament_time_control_is_validated_for_create():
    resolved = TournamentService._time_control_for_create("25+10", 1_500_000, 10_000)

    assert resolved == TimeControl(name="25+10", initial_time_ms=1_500_000, increment_ms=10_000)
    assert TournamentService._time_control_for_create("custom", None, 10_000) is None
    assert TournamentService._time_control_for_create("custom", 0, 10_000) is None


def test_online_rating_speed_uses_lichess_estimated_duration_buckets():
    assert rating_speed_for_clock(60_000, 0) == RatingSpeed.BULLET
    assert rating_speed_for_clock(180_000, 0) == RatingSpeed.BLITZ
    assert rating_speed_for_clock(180_000, 2_000) == RatingSpeed.BLITZ
    assert rating_speed_for_clock(300_000, 3_000) == RatingSpeed.BLITZ
    assert rating_speed_for_clock(600_000, 0) == RatingSpeed.RAPID
    assert rating_speed_for_clock(1_500_000, 10_000) == RatingSpeed.CLASSICAL


def test_named_time_controls_map_to_rating_speed_categories():
    assert rating_speed_for_time_control_name("1+0") == RatingSpeed.BULLET
    assert rating_speed_for_time_control_name("3+0") == RatingSpeed.BLITZ
    assert rating_speed_for_time_control_name("3+2") == RatingSpeed.BLITZ
    assert rating_speed_for_time_control_name("5+3") == RatingSpeed.BLITZ
    assert rating_speed_for_time_control_name("10+0") == RatingSpeed.RAPID
    assert rating_speed_for_time_control_name("25+10") == RatingSpeed.CLASSICAL


def test_profile_ratings_are_grouped_by_speed_not_exact_time_control():
    user_id = uuid4()
    older_blitz = SimpleNamespace(
        white_id=user_id,
        black_id=uuid4(),
        rated=True,
        time_control_name="3+0",
        white_rating_after=1410,
        white_rating_before=1400,
        black_rating_after=1390,
        black_rating_before=1400,
        started_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 13, 10, 5, tzinfo=timezone.utc),
    )
    latest_blitz = SimpleNamespace(
        white_id=uuid4(),
        black_id=user_id,
        rated=True,
        time_control_name="5+3",
        white_rating_after=1510,
        white_rating_before=1500,
        black_rating_after=1490,
        black_rating_before=1500,
        started_at=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 13, 11, 8, tzinfo=timezone.utc),
    )
    rapid = SimpleNamespace(
        white_id=user_id,
        black_id=uuid4(),
        rated=True,
        time_control_name="10+0",
        white_rating_after=1605,
        white_rating_before=1600,
        black_rating_after=1595,
        black_rating_before=1600,
        started_at=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 13, 12, 20, tzinfo=timezone.utc),
    )

    ratings = SqlAlchemyProfileRepository._ratings_by_speed(
        user_id,
        {
            RatingSpeed.BULLET: 1200,
            RatingSpeed.BLITZ: 1500,
            RatingSpeed.RAPID: 1600,
            RatingSpeed.CLASSICAL: 1700,
        },
        [older_blitz, latest_blitz, rapid],
    )

    assert ratings == {
        "bullet": 1200,
        "blitz": 1490,
        "rapid": 1605,
    }


@pytest.mark.asyncio
async def test_rating_repository_updates_only_the_matching_speed_rating():
    game_id = uuid4()
    white_id = uuid4()
    black_id = uuid4()
    game = SimpleNamespace(
        id=game_id,
        white_id=white_id,
        black_id=black_id,
        rated=True,
        result=GameResult.WHITE_WINS,
        status=GameStatus.CHECKMATE,
        time_control_name="3+2",
        initial_time_ms=180_000,
        increment_ms=2_000,
        white_rating_before=0,
        black_rating_before=0,
        white_rating_after=None,
        black_rating_after=None,
        rating_applied_at=None,
    )
    white = SimpleNamespace(
        id=white_id,
        rating=1300,
        bullet_rating=1111,
        blitz_rating=1500,
        rapid_rating=1600,
        classical_rating=1700,
    )
    black = SimpleNamespace(
        id=black_id,
        rating=1300,
        bullet_rating=1099,
        blitz_rating=1500,
        rapid_rating=1600,
        classical_rating=1700,
    )

    class ScalarResult:
        def __init__(self, values):
            self._values = values

        def scalar_one_or_none(self):
            return self._values[0] if self._values else None

        def scalars(self):
            return self

        def all(self):
            return self._values

    class FakeSession:
        def __init__(self):
            self.calls = 0
            self.committed = False

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return ScalarResult([game])
            return ScalarResult([white, black])

        async def commit(self):
            self.committed = True

        async def refresh(self, _item):
            return None

    session = FakeSession()
    update = await SqlAlchemyRatingRepository(session).apply_game_rating(game_id)

    assert update is not None
    assert update.white.before == 1500
    assert update.black.before == 1500
    assert white.blitz_rating == update.white.after
    assert black.blitz_rating == update.black.after
    assert white.rating == 1300
    assert black.rating == 1300
    assert white.bullet_rating == 1111
    assert black.rapid_rating == 1600
    assert game.white_rating_before == 1500
    assert game.black_rating_before == 1500
    assert session.committed is True


def test_face_verification_game_participation_is_strict():
    white_id = uuid4()
    black_id = uuid4()
    game = SimpleNamespace(white_id=white_id, black_id=black_id)

    assert is_game_participant(game, white_id) is True
    assert is_game_participant(game, black_id) is True
    assert is_game_participant(game, uuid4()) is False


def test_completed_face_verification_accepts_only_verified_sessions():
    assert has_completed_face_verification(SimpleNamespace(status="verified")) is True
    assert has_completed_face_verification(SimpleNamespace(status="failed")) is False
    assert has_completed_face_verification(SimpleNamespace(status="pending")) is False
    assert has_completed_face_verification(None) is False


def test_failed_game_bound_face_verification_records_status_without_terminal_game_action():
    game_id = uuid4()

    assert should_stop_game_for_verification_session(SimpleNamespace(status="failed", game_id=game_id)) is False
    assert should_stop_game_for_verification_session(SimpleNamespace(status="verified", game_id=game_id)) is False
    assert should_stop_game_for_verification_session(SimpleNamespace(status="uncertain", game_id=game_id)) is False
    assert should_stop_game_for_verification_session(SimpleNamespace(status="failed", game_id=None)) is False
    assert should_stop_game_for_verification_session(None) is False


@pytest.mark.asyncio
async def test_identity_verification_failure_forfeits_active_game_for_opponent():
    white_id = uuid4()
    black_id = uuid4()
    game = SimpleNamespace(
        id=uuid4(),
        white_id=white_id,
        black_id=black_id,
        status=GameStatus.ACTIVE,
        result=None,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        time_control_name="5+0",
        initial_time_ms=300_000,
        increment_ms=0,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=datetime.now(timezone.utc),
        disconnected_player_id=None,
        disconnect_grace_deadline_at=None,
        termination_reason=None,
        ended_at=None,
    )

    class FakeRepo:
        updated = None

        async def get_by_id(self, game_id):
            assert game_id == game.id
            return game

        async def update(self, updated_game):
            self.updated = updated_game
            return updated_game

        async def commit(self):
            return None

        async def rollback(self):
            return None

    repo = FakeRepo()

    stopped = await GameService(repo).stop_for_identity_verification_failure(
        IdentityVerificationFailureCommand(game_id=game.id, user_id=white_id)
    )

    assert stopped.status == GameStatus.RESIGNED
    assert stopped.result == GameResult.BLACK_WINS
    assert stopped.termination_reason == "identity_verification_failed"
    assert stopped.ended_at is not None
    assert repo.updated is stopped


@pytest.mark.asyncio
async def test_identity_verification_failure_cannot_forfeit_when_user_is_not_player():
    game = SimpleNamespace(
        id=uuid4(),
        white_id=uuid4(),
        black_id=uuid4(),
        status=GameStatus.ACTIVE,
        result=None,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        time_control_name="5+0",
        initial_time_ms=300_000,
        increment_ms=0,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=datetime.now(timezone.utc),
        disconnected_player_id=None,
        disconnect_grace_deadline_at=None,
        termination_reason=None,
        ended_at=None,
    )

    class FakeRepo:
        async def get_by_id(self, game_id):
            assert game_id == game.id
            return game

        async def update(self, _updated_game):
            raise AssertionError("non-player identity failure must not update the game")

    with pytest.raises(GameAccessDenied):
        await GameService(FakeRepo()).stop_for_identity_verification_failure(
            IdentityVerificationFailureCommand(game_id=game.id, user_id=uuid4())
        )


@pytest.mark.asyncio
async def test_nonparticipant_cannot_resign_or_accept_draw_for_game():
    game = SimpleNamespace(
        id=uuid4(),
        white_id=uuid4(),
        black_id=uuid4(),
        status=GameStatus.ACTIVE,
        result=None,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        time_control_name="5+0",
        initial_time_ms=300_000,
        increment_ms=0,
        white_time_ms=300_000,
        black_time_ms=300_000,
        last_clock_started_at=datetime.now(timezone.utc),
        disconnected_player_id=None,
        disconnect_grace_deadline_at=None,
        termination_reason=None,
        ended_at=None,
    )

    class FakeRepo:
        async def get_by_id(self, game_id):
            assert game_id == game.id
            return game

        async def update(self, _updated_game):
            raise AssertionError("non-player action must not update the game")

    service = GameService(FakeRepo())
    unrelated_user_id = uuid4()

    with pytest.raises(GameAccessDenied):
        await service.resign(ResignCommand(game_id=game.id, user_id=unrelated_user_id))

    with pytest.raises(GameAccessDenied):
        await service.accept_draw(AcceptDrawCommand(game_id=game.id, user_id=unrelated_user_id))


@pytest.mark.asyncio
async def test_game_face_verification_access_rejects_unrelated_user():
    game_id = uuid4()
    user_id = uuid4()

    class FakeSession:
        async def get(self, model, key):
            if key == game_id:
                return SimpleNamespace(id=game_id, white_id=uuid4(), black_id=uuid4())
            if key == user_id:
                return SimpleNamespace(role="user", banned_at=None)
            return None

    with pytest.raises(HTTPException) as exc_info:
        await require_game_face_verification_access(FakeSession(), game_id, user_id)

    assert exc_info.value.status_code == 403


def test_face_verification_passkey_payload_requires_matching_challenge_and_credential():
    challenge = FaceVerificationService.build_passkey_challenge(str(uuid4()))
    credential_id = "local-device-credential"

    verified = FaceVerificationService.verify_passkey_assertion(
        challenge=challenge,
        assertion={
            "credential_id": credential_id,
            "challenge": challenge["challenge"],
            "client_data_json": "dev-client-data",
            "authenticator_data": "dev-authenticator-data",
            "signature": "dev-signature",
        },
        enrolled_credential_id=credential_id,
    )
    assert verified.status == "verified"
    assert verified.confidence == 1.0

    failed = FaceVerificationService.verify_passkey_assertion(
        challenge=challenge,
        assertion={
            "credential_id": credential_id,
            "challenge": "wrong-challenge",
            "client_data_json": "dev-client-data",
            "authenticator_data": "dev-authenticator-data",
            "signature": "dev-signature",
        },
        enrolled_credential_id=credential_id,
    )
    assert failed.status == "failed"


def test_face_template_enrollment_and_live_video_sample_matching_are_deterministic():
    face_sample = "camera-frame:alice:front-facing"
    template = FaceVerificationService.build_face_template(face_sample)

    assert template["algorithm"] == "local_face_template_v2"
    assert "template_hash" in template
    assert "camera-frame" not in template["template_hash"]

    verified = FaceVerificationService.verify_face_sample(
        stored_template=template,
        live_sample=face_sample,
    )
    assert verified.status == "verified"
    assert verified.confidence == 0.98

    failed = FaceVerificationService.verify_face_sample(
        stored_template=template,
        live_sample="camera-frame:bob:front-facing",
    )
    assert failed.status == "failed"


def test_face_template_requires_the_fixed_enrolled_camera_sample():
    first_frame = "data:image/jpeg;base64," + ("a" * 32_000)
    next_frame = "data:image/jpeg;base64," + ("b" * 32_200)
    different_size_frame = "data:image/jpeg;base64," + ("c" * 96_000)

    template = FaceVerificationService.build_face_template(first_frame)

    assert FaceVerificationService.verify_face_sample(
        stored_template=template,
        live_sample=next_frame,
    ).status == "failed"
    assert FaceVerificationService.verify_face_sample(
        stored_template=template,
        live_sample=different_size_frame,
    ).status == "failed"


def test_head_to_head_perspective_results_cover_colors_and_draws():
    user_id = uuid4()
    opponent_id = uuid4()

    assert HeadToHeadService._perspective_result(
        user_id,
        SimpleNamespace(white_id=user_id, black_id=opponent_id, result=GameResult.WHITE_WINS),
    ) == "win"
    assert HeadToHeadService._perspective_result(
        user_id,
        SimpleNamespace(white_id=opponent_id, black_id=user_id, result=GameResult.BLACK_WINS),
    ) == "win"
    assert HeadToHeadService._perspective_result(
        user_id,
        SimpleNamespace(white_id=user_id, black_id=opponent_id, result=GameResult.BLACK_WINS),
    ) == "loss"
    assert HeadToHeadService._perspective_result(
        user_id,
        SimpleNamespace(white_id=opponent_id, black_id=user_id, result=GameResult.WHITE_WINS),
    ) == "loss"
    assert HeadToHeadService._perspective_result(
        user_id,
        SimpleNamespace(white_id=user_id, black_id=opponent_id, result=GameResult.DRAW),
    ) == "draw"


def test_head_to_head_average_move_count_uses_recorded_move_counts():
    stats = _Stats()

    HeadToHeadService._apply(stats, "win", 12)
    HeadToHeadService._apply(stats, "draw", 20)

    assert stats.games == 2
    assert stats.wins == 1
    assert stats.draws == 1
    assert stats.average_moves == 16.0


@pytest.mark.asyncio
async def test_head_to_head_rejects_missing_players():
    class FakeSession:
        async def get(self, _model, _key):
            return None

    with pytest.raises(UserNotFound):
        await HeadToHeadService(FakeSession()).get(uuid4(), uuid4())


def test_profile_avatar_paths_are_exposed_as_media_urls():
    assert SqlAlchemyProfileRepository._avatar_url(None) is None
    assert SqlAlchemyProfileRepository._avatar_url("/media/avatars/player.png") == "/media/avatars/player.png"
    assert SqlAlchemyProfileRepository._avatar_url("player.png") == "/media/avatars/player.png"


def test_profile_serializer_exposes_wallet_balance():
    from domains.profiles.presentation.router import _serialize_profile

    profile = SimpleNamespace(
        id=str(uuid4()),
        username="WalletTester",
        rating=1500,
        avatar_url=None,
        created_at=datetime.now(timezone.utc),
        games_played=0,
        wins=0,
        losses=0,
        draws=0,
        win_rate=0,
        ratings={},
        global_rank=1,
        coins=4321,
        recent_games=[],
    )

    assert _serialize_profile(profile).coins == 4321


@pytest.mark.asyncio
async def test_require_admin_rejects_normal_and_banned_users():
    user_id = uuid4()

    class FakeSession:
        async def get(self, _model, _user_id):
            return SimpleNamespace(role="user", banned_at=None)

    with pytest.raises(HTTPException):
        await require_admin(str(user_id), FakeSession())

    class BannedAdminSession:
        async def get(self, _model, _user_id):
            return SimpleNamespace(role="admin", banned_at=datetime.now(timezone.utc))

    with pytest.raises(HTTPException):
        await require_admin(str(user_id), BannedAdminSession())


@pytest.mark.asyncio
async def test_require_admin_accepts_unbanned_admin():
    user_id = uuid4()

    class FakeSession:
        async def get(self, _model, _user_id):
            return SimpleNamespace(role="admin", banned_at=None)

    assert await require_admin(str(user_id), FakeSession()) == str(user_id)


def test_swiss_plan_avoids_duplicate_bye_when_possible():
    players = [_player(2), _player(1), _player(1)]
    prior = [
        TournamentPairing(
            tournament_id=players[0].tournament_id,
            round_number=1,
            white_id=players[2].user_id,
            black_id=None,
        )
    ]

    plan = plan_swiss_pairings(players, prior, round_number=2)
    bye = next(pairing for pairing in plan.pairings if pairing.black_id is None)

    assert bye.white_id != players[2].user_id
