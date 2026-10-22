from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.common.runtime_time import utc_now_for_db
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Movie, MovieActor
from src.service.catalog.actor_gender_backfill_service import ActorGenderBackfillService


def _create_actor(
    name: str,
    *,
    gender: int,
    cup: str | None = None,
    field_owners: dict | None = None,
) -> Actor:
    return Actor.create(
        javdb_id=f"actor-{name}",
        name=name,
        gender=gender,
        cup=cup,
        field_owners=field_owners or {},
    )


def _create_movie(number: str, *, is_collection: bool = False) -> Movie:
    return Movie.create(
        javdb_id=f"movie-{number}",
        movie_number=number,
        title=number,
        is_collection=is_collection,
    )


def _link(movie: Movie, *actors: Actor) -> None:
    for actor in actors:
        MovieActor.create(movie=movie, actor=actor)


class FakeProvider:
    def __init__(
        self,
        genders_by_movie: dict[str, dict[str, int] | Exception],
        *,
        unavailable_movies: frozenset[str] = frozenset(),
    ):
        self.genders_by_movie = genders_by_movie
        self.unavailable_movies = unavailable_movies
        self.calls: list[str] = []

    def get_movie_by_javdb_id(self, javdb_id: str):
        self.calls.append(javdb_id)
        data = self.genders_by_movie.get(javdb_id)
        if data is None:
            raise MetadataNotFoundError("movie", javdb_id)
        if isinstance(data, Exception):
            raise data
        return SimpleNamespace(
            actors_available=javdb_id not in self.unavailable_movies,
            actors=[
                SimpleNamespace(javdb_id=actor_id, gender=gender)
                for actor_id, gender in data.items()
            ],
        )


def test_backfill_corrects_both_directions_and_marks_checked(test_db):
    male = _create_actor("male", gender=1)
    female = _create_actor("female", gender=1)
    female_mislabeled = _create_actor("female-mislabeled", gender=2, cup="C")
    unknown = _create_actor("unknown", gender=0)
    manual = _create_actor(
        "manual", gender=1, field_owners={"gender": "host:manual"}
    )
    complete_low_visibility = _create_actor("complete", gender=1, cup="C")
    no_movie = _create_actor("no-movie", gender=1)

    for index in (1, 2):
        movie = _create_movie(f"SHARED-{index}")
        _link(movie, male, female, female_mislabeled, unknown)

    manual_movie = _create_movie("MANUAL")
    _link(manual_movie, manual)

    complete_movie = _create_movie("COMPLETE")
    _link(complete_movie, complete_low_visibility)

    provider = FakeProvider(
        {
            "movie-SHARED-1": {
                male.javdb_id: 2,
                female.javdb_id: 1,
                female_mislabeled.javdb_id: 1,
                unknown.javdb_id: 2,
            },
            "movie-SHARED-2": {
                male.javdb_id: 2,
                female.javdb_id: 1,
                female_mislabeled.javdb_id: 1,
                unknown.javdb_id: 2,
            },
        }
    )

    stats = ActorGenderBackfillService(
        provider=provider, batch_size=20, request_interval=0
    ).run()

    assert stats["mode"] == "initial"
    assert stats["candidates"] == 4
    assert stats["corrected"] == 3
    assert stats["unchanged"] == 1
    assert stats["unresolved"] == 0

    assert Actor.get_by_id(male.id).gender == 2
    assert Actor.get_by_id(female.id).gender == 1
    assert Actor.get_by_id(female_mislabeled.id).gender == 1
    assert Actor.get_by_id(unknown.id).gender == 2
    for actor in (male, female, female_mislabeled, unknown):
        assert Actor.get_by_id(actor.id).gender_checked_at is not None

    assert Actor.get_by_id(manual.id).gender == 1
    assert Actor.get_by_id(manual.id).gender_checked_at is None
    assert Actor.get_by_id(complete_low_visibility.id).gender_checked_at is None
    assert Actor.get_by_id(no_movie.id).gender_checked_at is None
    assert "movie-MANUAL" not in provider.calls
    assert "movie-COMPLETE" not in provider.calls


def test_backfill_runs_all_batches_in_one_run(test_db):
    actors = [
        _create_actor(f"male-{index}", gender=1) for index in range(3)
    ]
    provider = FakeProvider(
        {
            f"movie-{index}": {actor.javdb_id: 2}
            for index, actor in enumerate(actors, 1)
        }
    )
    for index, actor in enumerate(actors, 1):
        _link(_create_movie(f"{index}"), actor)

    stats = ActorGenderBackfillService(
        provider=provider, batch_size=2, request_interval=0
    ).run()

    assert stats["candidates"] == 3
    assert stats["corrected"] == 3
    assert len(provider.calls) == 3

    second = ActorGenderBackfillService(
        provider=provider, batch_size=2, request_interval=0
    ).run()
    assert second["candidates"] == 0


def test_backfill_marks_unresolved_and_does_not_retry(test_db):
    actor = _create_actor("missing", gender=1)
    _link(_create_movie("MISSING"), actor)
    provider = FakeProvider({})

    first = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()
    assert first["unresolved"] == 1
    assert Actor.get_by_id(actor.id).gender == 1
    assert Actor.get_by_id(actor.id).gender_checked_at is not None

    second = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()
    assert second["candidates"] == 0


def test_backfill_request_error_leaves_unchecked_for_retry(test_db, monkeypatch):
    monkeypatch.setattr(ActorGenderBackfillService, "RETRY_AFTER_HOURS", 0)
    actor = _create_actor("retry", gender=1)
    _link(_create_movie("RETRY"), actor)
    error = MetadataRequestError("GET", "http://test", "boom")

    failed = ActorGenderBackfillService(
        provider=FakeProvider({"movie-RETRY": error}),
        batch_size=10,
        request_interval=0,
    ).run()
    assert failed["errors"] == 1
    failed_actor = Actor.get_by_id(actor.id)
    assert failed_actor.gender_checked_at is None
    assert failed_actor.gender_next_check_at is not None

    fixed = ActorGenderBackfillService(
        provider=FakeProvider({"movie-RETRY": {actor.javdb_id: 2}}),
        batch_size=10,
        request_interval=0,
    ).run()
    assert fixed["corrected"] == 1
    fixed_actor = Actor.get_by_id(actor.id)
    assert fixed_actor.gender == 2
    assert fixed_actor.gender_next_check_at is None


def test_backfill_aborts_after_batch_error_limit(test_db, monkeypatch):
    monkeypatch.setattr(ActorGenderBackfillService, "MAX_BATCH_ERRORS", 3)
    actors = []
    for index in range(4):
        actor = _create_actor(f"error-{index}", gender=1)
        _link(_create_movie(f"ERROR-{index}"), actor)
        actors.append(actor)
    error = MetadataRequestError("GET", "http://test", "boom")
    provider = FakeProvider(
        {f"movie-ERROR-{index}": error for index in range(4)}
    )

    with pytest.raises(RuntimeError, match="本轮已中断"):
        ActorGenderBackfillService(
            provider=provider, batch_size=10, request_interval=0
        ).run()

    assert len(provider.calls) == 3
    for actor in actors[:3]:
        stored = Actor.get_by_id(actor.id)
        assert stored.gender_checked_at is None
        assert stored.gender_next_check_at is not None


def test_backfill_persistent_errors_do_not_starve_queue(test_db):
    error = MetadataRequestError("GET", "http://test", "boom")
    errors_by_movie: dict[str, dict[str, int] | Exception] = {}
    for index in range(3):
        actor = _create_actor(f"stuck-{index}", gender=1)
        for movie_index in range(3):
            movie = _create_movie(f"STUCK-{index}-{movie_index}")
            _link(movie, actor)
            errors_by_movie[f"movie-STUCK-{index}-{movie_index}"] = error

    healthy = _create_actor("healthy", gender=1)
    healthy_movie = _create_movie("HEALTHY")
    _link(healthy_movie, healthy)
    errors_by_movie["movie-HEALTHY"] = {healthy.javdb_id: 2}

    stats = ActorGenderBackfillService(
        provider=FakeProvider(errors_by_movie),
        batch_size=10,
        request_interval=0,
    ).run()

    assert stats["errors"] == 3
    assert stats["aborted"] == 0
    assert stats["corrected"] == 1
    assert Actor.get_by_id(healthy.id).gender == 2

    second = ActorGenderBackfillService(
        provider=FakeProvider(errors_by_movie),
        batch_size=10,
        request_interval=0,
    ).run()
    assert second["candidates"] == 0


def test_backfill_per_sample_error_still_uses_other_votes(test_db):
    actor = _create_actor("partial", gender=1)
    error = MetadataRequestError("GET", "http://test", "boom")
    provider = FakeProvider(
        {
            "movie-PARTIAL-0": error,
            "movie-PARTIAL-1": {actor.javdb_id: 2},
            "movie-PARTIAL-2": {actor.javdb_id: 2},
        }
    )
    for index in range(3):
        _link(_create_movie(f"PARTIAL-{index}"), actor)

    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["errors"] == 0
    assert stats["corrected"] == 1
    assert Actor.get_by_id(actor.id).gender == 2


def test_backfill_uses_actor_even_when_actors_available_is_false(test_db):
    actor = _create_actor("unavailable", gender=1)
    for index in (1, 2):
        _link(_create_movie(f"UNAVAILABLE-{index}"), actor)

    provider = FakeProvider(
        {
            "movie-UNAVAILABLE-1": {actor.javdb_id: 2},
            "movie-UNAVAILABLE-2": {actor.javdb_id: 2},
        },
        unavailable_movies=frozenset(
            {"movie-UNAVAILABLE-1", "movie-UNAVAILABLE-2"}
        ),
    )

    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["corrected"] == 1
    assert Actor.get_by_id(actor.id).gender == 2


def test_backfill_one_to_one_vote_is_unresolved(test_db):
    actor = _create_actor("tie", gender=1)
    for index in (1, 2):
        _link(_create_movie(f"TIE-{index}"), actor)

    provider = FakeProvider(
        {
            "movie-TIE-1": {actor.javdb_id: 2},
            "movie-TIE-2": {actor.javdb_id: 1},
        }
    )
    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["unresolved"] == 1
    assert Actor.get_by_id(actor.id).gender == 1
    assert Actor.get_by_id(actor.id).gender_checked_at is not None


def test_backfill_three_sample_majority(test_db):
    male = _create_actor("majority-male", gender=1)
    female = _create_actor("majority-female", gender=1)
    for index in range(3):
        movie = _create_movie(f"VOTE-{index}")
        _link(movie, male, female)

    provider = FakeProvider(
        {
            "movie-VOTE-0": {male.javdb_id: 2, female.javdb_id: 1},
            "movie-VOTE-1": {male.javdb_id: 1, female.javdb_id: 2},
            "movie-VOTE-2": {male.javdb_id: 2, female.javdb_id: 1},
        }
    )

    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["corrected"] == 1
    assert stats["unchanged"] == 1
    assert Actor.get_by_id(male.id).gender == 2
    assert Actor.get_by_id(female.id).gender == 1


def test_backfill_recheck_after_cooldown(test_db, monkeypatch):
    monkeypatch.setattr(
        ActorGenderBackfillService, "HIGH_VISIBILITY_MOVIE_COUNT", 500
    )
    actor = _create_actor("recheck", gender=1)
    _link(_create_movie("RECHECK"), actor)
    old_checked_at = utc_now_for_db() - timedelta(days=31)
    Actor.update(gender_checked_at=old_checked_at).where(
        Actor.id == actor.id
    ).execute()

    provider = FakeProvider({"movie-RECHECK": {actor.javdb_id: 2}})
    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["mode"] == "recheck"
    assert stats["corrected"] == 1
    assert Actor.get_by_id(actor.id).gender == 2
    assert Actor.get_by_id(actor.id).gender_checked_at > old_checked_at


def test_backfill_checks_high_visibility_actor_with_cup(test_db, monkeypatch):
    monkeypatch.setattr(
        ActorGenderBackfillService, "HIGH_VISIBILITY_MOVIE_COUNT", 2
    )
    actor = _create_actor("high-count", gender=1, cup="C")
    for index in (1, 2):
        _link(_create_movie(f"HIGH-{index}"), actor)

    provider = FakeProvider(
        {
            "movie-HIGH-1": {actor.javdb_id: 2},
            "movie-HIGH-2": {actor.javdb_id: 2},
        }
    )
    stats = ActorGenderBackfillService(
        provider=provider, batch_size=10, request_interval=0
    ).run()

    assert stats["candidates"] == 1
    assert stats["corrected"] == 1
    assert Actor.get_by_id(actor.id).gender == 2
