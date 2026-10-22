from src.common.runtime_time import utc_now_for_db
from src.model import Actor
from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway


def test_update_host_source_gender_clears_backfill_state(test_db):
    checked_at = utc_now_for_db()
    actor = Actor.create(
        javdb_id="gateway-gender",
        name="gateway",
        gender=1,
        gender_checked_at=checked_at,
        gender_next_check_at=checked_at,
    )

    assert ActorOwnershipGateway.update_host_source(actor.id, {"gender": 2})

    stored = Actor.get_by_id(actor.id)
    assert stored.gender == 2
    assert stored.gender_checked_at is None
    assert stored.gender_next_check_at is None


def test_patch_plugin_gender_clears_backfill_state(test_db):
    checked_at = utc_now_for_db()
    actor = Actor.create(
        javdb_id="gateway-plugin-gender",
        name="gateway-plugin",
        gender=1,
        gender_checked_at=checked_at,
        gender_next_check_at=checked_at,
    )

    assert ActorOwnershipGateway.patch_plugin(
        actor.id, "demo", {"gender": 2}, actor.mutation_revision
    )

    stored = Actor.get_by_id(actor.id)
    assert stored.gender == 2
    assert stored.gender_checked_at is None
    assert stored.gender_next_check_at is None
