from uuid import uuid4

import pytest

from domains.game.domain.entities import Game
from domains.game.domain.value_objects import GameResult, GameStatus
from domains.tournaments.application.services import TournamentService
from domains.tournaments.domain.value_objects import TournamentStatus


class InMemoryTournamentRepository:
    def __init__(self) -> None:
        self.tournaments = {}
        self.players = {}
        self.rounds = {}
        self.pairings = {}
        self.next_round_id = 1
        self.next_pairing_id = 1

    async def create_tournament(self, tournament):
        self.tournaments[tournament.id] = tournament
        self.players[tournament.id] = {}
        self.rounds[tournament.id] = []
        self.pairings[tournament.id] = []
        return tournament

    async def get_tournament(self, tournament_id):
        return self.tournaments.get(tournament_id)

    async def list_tournaments(self):
        return list(self.tournaments.values())

    async def update_tournament(self, tournament):
        self.tournaments[tournament.id] = tournament
        return tournament

    async def add_player(self, player):
        self.players[player.tournament_id][player.user_id] = player
        return player

    async def get_player(self, tournament_id, user_id):
        return self.players.get(tournament_id, {}).get(user_id)

    async def list_players(self, tournament_id):
        return list(self.players.get(tournament_id, {}).values())

    async def remove_player(self, tournament_id, user_id):
        self.players.get(tournament_id, {}).pop(user_id, None)

    async def update_players(self, players):
        for player in players:
            self.players[player.tournament_id][player.user_id] = player
        return players

    async def create_round(self, tournament_round):
        tournament_round.id = self.next_round_id
        self.next_round_id += 1
        self.rounds[tournament_round.tournament_id].append(tournament_round)
        return tournament_round

    async def list_rounds(self, tournament_id):
        return list(self.rounds.get(tournament_id, []))

    async def add_pairing(self, pairing):
        pairing.id = self.next_pairing_id
        self.next_pairing_id += 1
        self.pairings[pairing.tournament_id].append(pairing)
        return pairing

    async def list_pairings(self, tournament_id, round_number=None):
        pairings = list(self.pairings.get(tournament_id, []))
        if round_number is not None:
            pairings = [pairing for pairing in pairings if pairing.round_number == round_number]
        return pairings

    async def get_pairing_by_game_id(self, game_id):
        for pairings in self.pairings.values():
            for pairing in pairings:
                if pairing.game_id == game_id:
                    return pairing
        return None

    async def update_pairing(self, pairing):
        return pairing


class StubUser:
    def __init__(self, user_id, username, rating):
        self.id = user_id
        self.username = username
        self.email = f"{username}@example.test"
        self.password_hash = "hash"
        self.rating = rating
        self.bio = None
        self.avatar_path = None
        self.role = "user"
        self.banned_at = None
        self.created_at = None


class InMemoryUserRepository:
    def __init__(self, users):
        self.users = {user.id: user for user in users}

    async def create(self, user):
        self.users[user.id] = user
        return user

    async def get_by_id(self, user_id):
        return self.users.get(user_id)

    async def get_by_email(self, email):
        return next((user for user in self.users.values() if user.email == email), None)

    async def get_by_username(self, username):
        return next((user for user in self.users.values() if user.username == username), None)

    async def get_by_ids(self, user_ids):
        return {user_id: self.users[user_id] for user_id in user_ids if user_id in self.users}

    async def update(self, user):
        self.users[user.id] = user
        return user

    async def update_many(self, users):
        for user in users:
            self.users[user.id] = user
        return users


class InMemoryGameRepository:
    def __init__(self):
        self.games = {}

    async def create(self, game):
        self.games[game.id] = game
        return game

    async def get_by_id(self, game_id):
        return self.games.get(game_id)

    async def get_active_by_user(self, user_id):
        return None

    async def list_active(self):
        return []

    async def update(self, game):
        self.games[game.id] = game
        return game

    async def list_by_user(self, user_id, page=1, size=20):
        return [], 0

    async def add_move(self, move):
        return move

    async def get_moves(self, game_id):
        return []

    async def get_move_counts(self, game_ids):
        return {}

    async def commit(self):
        return None

    async def rollback(self):
        return None


class StubGameService:
    def __init__(self, game_repo):
        self.game_repo = game_repo

    async def create_game(self, cmd):
        game = Game(
            white_id=cmd.white_id,
            black_id=cmd.black_id,
            time_control_name=cmd.time_control.name,
            initial_time_ms=cmd.time_control.initial_time_ms,
            increment_ms=cmd.time_control.increment_ms,
            white_rating_before=cmd.starting_ratings.white,
            black_rating_before=cmd.starting_ratings.black,
            rated=cmd.rated,
        )
        await self.game_repo.create(game)
        return game


@pytest.mark.asyncio
async def test_start_tournament_creates_round_one_pairings_and_bye():
    owner = StubUser(uuid4(), "owner", 1500)
    guest_a = StubUser(uuid4(), "guest-a", 1480)
    guest_b = StubUser(uuid4(), "guest-b", 1460)
    tournament_repo = InMemoryTournamentRepository()
    user_repo = InMemoryUserRepository([owner, guest_a, guest_b])
    game_repo = InMemoryGameRepository()
    game_service = StubGameService(game_repo)
    service = TournamentService(tournament_repo, user_repo, game_repo, game_service)

    tournament = await service.create_tournament(owner.id, "Rapid Swiss", "5+0")
    await service.join_tournament(tournament.id, guest_a.id)
    await service.join_tournament(tournament.id, guest_b.id)

    started = await service.start_tournament(tournament.id, owner.id)
    round_one_pairings = await tournament_repo.list_pairings(tournament.id, 1)
    players = await tournament_repo.list_players(tournament.id)

    assert started.status == "active"
    assert started.current_round == 1
    assert len(round_one_pairings) == 2
    assert sum(1 for pairing in round_one_pairings if pairing.black_id is None) == 1
    assert all(pairing.game_id is not None for pairing in round_one_pairings if pairing.black_id is not None)
    assert len(game_repo.games) == 1
    assert any(player.score == 1.0 for player in players)


@pytest.mark.asyncio
async def test_owner_can_add_otb_player_without_self_registration():
    owner = StubUser(uuid4(), "owner", 1500)
    tournament_repo = InMemoryTournamentRepository()
    user_repo = InMemoryUserRepository([owner])
    game_repo = InMemoryGameRepository()
    game_service = StubGameService(game_repo)
    service = TournamentService(tournament_repo, user_repo, game_repo, game_service)

    tournament = await service.create_tournament(owner.id, "City OTB Swiss", "15+10", tournament_type="otb")

    created_player = await service.add_otb_player(
        tournament.id,
        owner.id,
        display_name="Nadia Petrova",
        seed_rating=1725,
    )
    players = await tournament_repo.list_players(tournament.id)
    created_user = await user_repo.get_by_id(created_player.user_id)

    assert created_user is not None
    assert created_user.username == "NadiaPetrova"
    assert created_user.email.endswith("@otb.chessview.local")
    assert created_user.rating == 1725
    assert created_player in players


@pytest.mark.asyncio
async def test_start_tournament_allows_closed_registration_state():
    owner = StubUser(uuid4(), "owner", 1500)
    guest = StubUser(uuid4(), "guest", 1480)
    tournament_repo = InMemoryTournamentRepository()
    user_repo = InMemoryUserRepository([owner, guest])
    game_repo = InMemoryGameRepository()
    game_service = StubGameService(game_repo)
    service = TournamentService(tournament_repo, user_repo, game_repo, game_service)

    tournament = await service.create_tournament(owner.id, "Closed Swiss", "5+0")
    await service.join_tournament(tournament.id, guest.id)
    tournament.status = TournamentStatus.REGISTRATION_CLOSED
    await tournament_repo.update_tournament(tournament)

    started = await service.start_tournament(tournament.id, owner.id)

    assert started.status == TournamentStatus.ACTIVE


@pytest.mark.asyncio
async def test_sync_game_result_advances_round_without_immediate_rematch():
    players = [
        StubUser(uuid4(), "owner", 1600),
        StubUser(uuid4(), "guest-a", 1550),
        StubUser(uuid4(), "guest-b", 1500),
        StubUser(uuid4(), "guest-c", 1450),
    ]
    owner = players[0]
    tournament_repo = InMemoryTournamentRepository()
    user_repo = InMemoryUserRepository(players)
    game_repo = InMemoryGameRepository()
    game_service = StubGameService(game_repo)
    service = TournamentService(tournament_repo, user_repo, game_repo, game_service)

    tournament = await service.create_tournament(owner.id, "Swiss Night", "3+2")
    for player in players[1:]:
        await service.join_tournament(tournament.id, player.id)

    await service.start_tournament(tournament.id, owner.id)
    round_one_pairings = await tournament_repo.list_pairings(tournament.id, 1)

    for index, pairing in enumerate(round_one_pairings):
        if pairing.black_id is None:
            continue
        game = Game(
            white_id=pairing.white_id,
            black_id=pairing.black_id,
            time_control_name="3+2",
            initial_time_ms=180_000,
            increment_ms=2_000,
            white_rating_before=1500,
            black_rating_before=1500,
        )
        await game_repo.create(game)
        pairing.game_id = game.id
        await tournament_repo.update_pairing(pairing)
        game.status = GameStatus.CHECKMATE
        game.result = GameResult.WHITE_WINS if index == 0 else GameResult.BLACK_WINS
        await game_repo.update(game)
        await service.sync_game_result(pairing.game_id)

    updated_tournament = await tournament_repo.get_tournament(tournament.id)
    round_two_pairings = await tournament_repo.list_pairings(tournament.id, 2)
    round_one_matchups = {
        frozenset({pairing.white_id, pairing.black_id})
        for pairing in round_one_pairings
        if pairing.black_id is not None
    }
    round_two_matchups = {
        frozenset({pairing.white_id, pairing.black_id})
        for pairing in round_two_pairings
        if pairing.black_id is not None
    }

    assert updated_tournament is not None
    assert updated_tournament.current_round == 2
    assert round_two_matchups.isdisjoint(round_one_matchups)
