from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import Actor, Movie, MovieActor
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_actor_service import MovieActorService


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _build_detail(
    *,
    movie_number: str = "API-CAST-1",
    javdb_id: str = "javdb-API-CAST-1",
    actors: list[JavdbMovieActorResource] | None = None,
) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title="远端标题",
        summary="",
        duration_minutes=120,
        actors=actors or [],
        tags=[],
    )


def test_local_actor_create_and_local_only_identity_edit(client, account_user):
    headers = _auth_headers(client, account_user)

    created = client.post(
        "/actors",
        headers=headers,
        json={"name": " 本地女优 ", "alias_name": "别名A / 别名B"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["javdb_id"] is None
    assert payload["name"] == "本地女优"
    assert payload["alias_name"] == "本地女优 / 别名A / 别名B"
    assert payload["display_name"] == "本地女优"
    assert payload["gender"] == 1
    assert payload["manual_fields"] == ["gender"]

    # 同名本地女优允许并存：多个 javdb_id NULL 不触发唯一约束。
    duplicate = client.post("/actors", headers=headers, json={"name": "本地女优"})
    assert duplicate.status_code == 201
    assert duplicate.json()["javdb_id"] is None

    renamed = client.patch(
        f"/actors/{payload['id']}",
        headers=headers,
        json={"name": "新名字"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新名字"
    assert renamed.json()["alias_name"] == "新名字 / 本地女优 / 别名A / 别名B"

    aliased = client.patch(
        f"/actors/{payload['id']}",
        headers=headers,
        json={"alias_name": "新别名"},
    )
    assert aliased.status_code == 200
    assert aliased.json()["alias_name"] == "新名字 / 新别名"


def test_remote_actor_identity_is_not_editable(client, account_user):
    actor = Actor.create(javdb_id="remote-identity", name="远端演员")
    headers = _auth_headers(client, account_user)

    response = client.patch(
        f"/actors/{actor.id}", headers=headers, json={"name": "本地改名"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_actor_update"
    assert Actor.get_by_id(actor.id).name == "远端演员"


def test_movie_actor_add_remove_and_manual_ownership(client, account_user):
    movie = Movie.create(
        movie_number="API-CAST-1",
        javdb_id="javdb-API-CAST-1",
        title="影片",
    )
    local_actor = Actor.create(javdb_id=None, name="本地女优")
    remote_actor = Actor.create(javdb_id="remote-cast", name="远端女优")
    headers = _auth_headers(client, account_user)

    assert (
        client.put(
            f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
        ).status_code
        == 204
    )
    assert (
        client.put(
            f"/movies/{movie.movie_number}/actors/{remote_actor.id}", headers=headers
        ).status_code
        == 204
    )

    detail = client.get(f"/movies/{movie.movie_number}", headers=headers)
    assert detail.status_code == 200
    assert {item["id"] for item in detail.json()["actors"]} == {
        local_actor.id,
        remote_actor.id,
    }
    local_payload = next(
        item for item in detail.json()["actors"] if item["id"] == local_actor.id
    )
    assert local_payload["javdb_id"] is None
    assert detail.json()["is_actors_manually_managed"] is True

    assert (
        client.delete(
            f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
        ).status_code
        == 204
    )
    # 幂等移除：关联不存在也成功，且不会解除人工接管。
    assert (
        client.delete(
            f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
        ).status_code
        == 204
    )
    detail = client.get(f"/movies/{movie.movie_number}", headers=headers)
    assert [item["id"] for item in detail.json()["actors"]] == [remote_actor.id]
    assert detail.json()["is_actors_manually_managed"] is True

    reset = client.post(
        f"/movies/{movie.movie_number}/actors/reset", headers=headers
    )
    assert reset.status_code == 204
    detail = client.get(f"/movies/{movie.movie_number}", headers=headers)
    assert detail.json()["is_actors_manually_managed"] is False


def test_strict_refresh_respects_manual_actor_list(client, account_user, test_db):
    movie = Movie.create(
        movie_number="API-CAST-1",
        javdb_id="javdb-API-CAST-1",
        title="影片",
    )
    local_actor = Actor.create(javdb_id=None, name="本地女优")
    old_remote = Actor.create(javdb_id="remote-old", name="旧远端女优")
    MovieActor.create(movie=movie, actor=old_remote)
    headers = _auth_headers(client, account_user)

    client.put(
        f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
    )

    detail = _build_detail(
        actors=[JavdbMovieActorResource(javdb_id="remote-new", name="新远端女优")]
    )
    CatalogImportService().refresh_movie_metadata_strict(movie, detail)

    links = {
        link.actor_id
        for link in MovieActor.select(MovieActor.actor).where(
            MovieActor.movie == movie
        )
    }
    assert links == {local_actor.id, old_remote.id}
    assert (
        Movie.get_by_id(movie.id).field_owners["actors"] == "host:manual"
    )
    assert not Actor.select().where(Actor.javdb_id == "remote-new").exists()

    client.post(f"/movies/{movie.movie_number}/actors/reset", headers=headers)
    CatalogImportService().refresh_movie_metadata_strict(movie, detail)

    new_remote = Actor.get(Actor.javdb_id == "remote-new")
    links = {
        link.actor_id
        for link in MovieActor.select(MovieActor.actor).where(
            MovieActor.movie == movie
        )
    }
    assert links == {new_remote.id}
    assert "actors" not in (Movie.get_by_id(movie.id).field_owners or {})


def test_backfill_respects_manual_actor_list(test_db):
    movie = Movie.create(
        movie_number="API-PLUGIN-CAST",
        javdb_id=None,
        metadata_source={"plugin_id": "test"},
        title="插件影片",
    )
    local_actor = Actor.create(javdb_id=None, name="本地女优")
    MovieActorService.add_actor(movie.movie_number, local_actor.id)

    detail = _build_detail(
        movie_number=movie.movie_number,
        javdb_id="javdb-API-PLUGIN-CAST",
        actors=[JavdbMovieActorResource(javdb_id="remote-backfill", name="远端女优")],
    )
    CatalogImportService().backfill_plugin_movie(movie, detail)

    links = {
        link.actor_id
        for link in MovieActor.select(MovieActor.actor).where(
            MovieActor.movie == movie
        )
    }
    assert links == {local_actor.id}
    assert not Actor.select().where(Actor.javdb_id == "remote-backfill").exists()


def test_strict_refresh_keeps_actor_list_when_remote_actors_unavailable(test_db):
    movie = Movie.create(
        movie_number="API-CAST-1",
        javdb_id="javdb-API-CAST-1",
        title="影片",
    )
    remote_actor = Actor.create(javdb_id="remote-keep", name="保留女优")
    MovieActor.create(movie=movie, actor=remote_actor)
    detail = _build_detail(actors=[])
    detail.actors_available = False

    CatalogImportService().refresh_movie_metadata_strict(movie, detail)

    links = {
        link.actor_id
        for link in MovieActor.select(MovieActor.actor).where(
            MovieActor.movie == movie
        )
    }
    assert links == {remote_actor.id}


def test_local_actor_delete_requires_no_references(client, account_user):
    movie = Movie.create(
        movie_number="API-CAST-1",
        javdb_id="javdb-API-CAST-1",
        title="影片",
    )
    local_actor = Actor.create(javdb_id=None, name="本地女优")
    remote_actor = Actor.create(javdb_id="remote-delete", name="远端女优")
    headers = _auth_headers(client, account_user)

    client.put(
        f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
    )

    in_use = client.delete(f"/actors/{local_actor.id}", headers=headers)
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "actor_in_use"

    remote_delete = client.delete(f"/actors/{remote_actor.id}", headers=headers)
    assert remote_delete.status_code == 422
    assert remote_delete.json()["error"]["code"] == "actor_not_local"

    client.delete(
        f"/movies/{movie.movie_number}/actors/{local_actor.id}", headers=headers
    )
    deleted = client.delete(f"/actors/{local_actor.id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/actors/{local_actor.id}", headers=headers).status_code == 404


def test_local_actor_can_be_subscribed_as_favorite(client, account_user):
    local_actor = Actor.create(javdb_id=None, name="本地女优")
    headers = _auth_headers(client, account_user)

    subscribed = client.put(
        f"/actors/{local_actor.id}/subscription", headers=headers
    )
    assert subscribed.status_code == 204
    fresh = Actor.get_by_id(local_actor.id)
    assert fresh.is_subscribed is True
    assert fresh.subscribed_at is not None

    unsubscribed = client.delete(
        f"/actors/{local_actor.id}/subscription", headers=headers
    )
    assert unsubscribed.status_code == 204
    fresh = Actor.get_by_id(local_actor.id)
    assert fresh.is_subscribed is False
    assert fresh.subscribed_at is None


def test_local_actor_merge_direction_guards_and_subscription_carry_over(
    client, account_user
):
    local_actor = Actor.create(javdb_id=None, name="本地女优", is_subscribed=True)
    remote_actor = Actor.create(javdb_id="remote-guard", name="远端女优")
    headers = _auth_headers(client, account_user)

    merged = client.post(
        f"/actors/{local_actor.id}/merge",
        headers=headers,
        json={"source_actor_ids": [remote_actor.id]},
    )
    assert merged.status_code == 422
    assert merged.json()["error"]["code"] == "invalid_actor_merge"

    reverse = client.post(
        f"/actors/{remote_actor.id}/merge",
        headers=headers,
        json={"source_actor_ids": [local_actor.id]},
    )
    assert reverse.status_code == 200
    assert reverse.json()["id"] == remote_actor.id
    # 本地女优作为来源合并进远端女优时，收藏态随合并保留。
    target = Actor.get_by_id(remote_actor.id)
    assert target.is_subscribed is True
    assert target.subscribed_at is not None
