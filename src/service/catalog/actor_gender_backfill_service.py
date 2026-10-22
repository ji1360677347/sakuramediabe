from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta

from loguru import logger
from peewee import JOIN, Case, fn

from src.common.runtime_time import utc_now_for_db
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Movie, MovieActor
from src.service.catalog.actor_ownership_gateway import (
    MANUAL_ACTOR_FIELD_OWNER,
    ActorOwnershipGateway,
)


class ActorGenderBackfillService:
    """按 JavDB 影片详情的演员性别，修正存量错误性别。

    当前 JavDB 演员详情和搜索接口实测都不返回性别，只有影片详情的 actors[].gender
    可靠；手动任务一次处理完所有候选，内部分批只是限制单次采样查询量，
    gender_checked_at 逐行持久化，请求失败写 gender_next_check_at 退避，中断后从剩余候选继续。
    """

    TASK_KEY = "actor_gender_backfill"
    BATCH_SIZE = 300
    SAMPLE_MOVIES_PER_ACTOR = 3
    HIGH_VISIBILITY_MOVIE_COUNT = 500
    RECHECK_DAYS = 30
    REQUEST_INTERVAL_SECONDS = 0.5
    MAX_BATCH_ERRORS = 20
    RETRY_AFTER_HOURS = 6
    MANUAL_OWNER = MANUAL_ACTOR_FIELD_OWNER

    def __init__(
        self,
        provider=None,
        *,
        batch_size: int | None = None,
        request_interval: float | None = None,
    ):
        self.provider = provider
        self.batch_size = batch_size or self.BATCH_SIZE
        self.request_interval = (
            self.REQUEST_INTERVAL_SECONDS
            if request_interval is None
            else request_interval
        )

    def run(self, *, reporter=None) -> dict:
        candidates = list(self._pending_initial())
        mode = "initial"
        if not candidates:
            candidates = list(self._pending_recheck())
            mode = "recheck"
        stats = {
            "mode": mode,
            "candidates": len(candidates),
            "corrected": 0,
            "unchanged": 0,
            "unresolved": 0,
            "blocked": 0,
            "errors": 0,
            "aborted": 0,
        }
        if not candidates:
            return stats

        provider = self.provider or build_javdb_provider()
        metadata_provider = getattr(provider, "provider", provider)
        checked_at = utc_now_for_db()
        retry_at = checked_at + timedelta(hours=self.RETRY_AFTER_HOURS)

        try:
            for batch_start in range(0, len(candidates), self.batch_size):
                batch = candidates[batch_start : batch_start + self.batch_size]
                samples = self._sample_movies([actor.id for actor in batch])
                for offset, actor in enumerate(batch, 1):
                    current = batch_start + offset
                    try:
                        detected = self._detect_gender(
                            actor, samples.get(actor.id, ()), metadata_provider
                        )
                    except MetadataRequestError as exc:
                        stats["errors"] += 1
                        Actor.update(gender_next_check_at=retry_at).where(
                            Actor.id == actor.id
                        ).execute()
                        logger.warning(
                            "演员性别回填请求失败 actor_id={} name={} detail={} errors={}/{}",
                            actor.id,
                            actor.name,
                            exc,
                            stats["errors"],
                            self.MAX_BATCH_ERRORS,
                        )
                        if stats["errors"] >= self.MAX_BATCH_ERRORS:
                            stats["aborted"] = 1
                            break
                        self._emit(reporter, current, len(candidates), stats)
                        continue

                    if detected is None:
                        stats["unresolved"] += 1
                    elif detected == actor.gender:
                        stats["unchanged"] += 1
                    elif ActorOwnershipGateway.update_host_source(
                        actor.id, {"gender": detected}
                    ):
                        stats["corrected"] += 1
                    else:
                        stats["blocked"] += 1
                    Actor.update(
                        gender_checked_at=checked_at,
                        gender_next_check_at=None,
                    ).where(Actor.id == actor.id).execute()
                    self._emit(reporter, current, len(candidates), stats)
                    if self.request_interval > 0:
                        time.sleep(self.request_interval)
                if stats["aborted"]:
                    break
        finally:
            if self.provider is None:
                client = getattr(provider, "client", None)
                if client is not None:
                    client.close()

        if stats["aborted"]:
            logger.error("演员性别回填中断 stats={}", stats)
            raise RuntimeError(
                f"演员性别回填累计失败 {stats['errors']} 个演员，本轮已中断"
            )
        logger.info("演员性别回填完成 stats={}", stats)
        return stats

    def _pending_initial(self):
        high_visibility = fn.COUNT(MovieActor.id) >= self.HIGH_VISIBILITY_MOVIE_COUNT
        now = utc_now_for_db()
        return (
            self._base_query()
            .where(
                Actor.gender_checked_at.is_null(),
                (Actor.gender_next_check_at.is_null())
                | (Actor.gender_next_check_at <= now),
            )
            .having((Actor.gender != 1) | Actor.cup.is_null() | high_visibility)
            .order_by(
                # 先处理最可能被误判成女性的 gender=1，再按曝光量降序。
                Case(None, [(Actor.gender == 1, 0)], 1).asc(),
                fn.COUNT(MovieActor.id).desc(),
                Actor.id.asc(),
            )
        )

    def _pending_recheck(self):
        now = utc_now_for_db()
        cutoff = now - timedelta(days=self.RECHECK_DAYS)
        return (
            self._base_query()
            .where(
                Actor.gender == 1,
                Actor.cup.is_null(),
                Actor.gender_checked_at <= cutoff,
                (Actor.gender_next_check_at.is_null())
                | (Actor.gender_next_check_at <= now),
            )
            .order_by(fn.COUNT(MovieActor.id).desc(), Actor.id.asc())
        )

    @classmethod
    def _base_query(cls):
        owner = fn.COALESCE(
            fn.JSONB_EXTRACT_PATH_TEXT(Actor.field_owners, "gender"), ""
        )
        return (
            Actor.select(
                Actor.id,
                Actor.name,
                Actor.javdb_id,
                Actor.gender,
                Actor.cup,
                fn.COUNT(MovieActor.id).alias("movie_count"),
            )
            .join(MovieActor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .join(Movie, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(
                Actor.merged_into.is_null(),
                owner != cls.MANUAL_OWNER,
                Movie.is_collection == False,
                Movie.is_blacklisted == False,
                Movie.javdb_id.is_null(False),
            )
            .group_by(Actor.id, Actor.name, Actor.javdb_id, Actor.gender, Actor.cup)
        )

    @classmethod
    def _sample_movies(cls, actor_ids: list[int]) -> dict[int, list[str]]:
        if not actor_ids:
            return {}
        rows = (
            MovieActor.select(MovieActor.actor, Movie.javdb_id)
            .join(Movie, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(
                MovieActor.actor.in_(actor_ids),
                Movie.is_collection == False,
                Movie.is_blacklisted == False,
                Movie.javdb_id.is_null(False),
            )
            .order_by(Movie.id.desc())
            .tuples()
        )
        samples: dict[int, list[str]] = defaultdict(list)
        for actor_id, javdb_id in rows:
            if len(samples[actor_id]) < cls.SAMPLE_MOVIES_PER_ACTOR:
                samples[actor_id].append(javdb_id)
        return dict(samples)

    @classmethod
    def _detect_gender(
        cls, actor, movie_javdb_ids, provider
    ) -> int | None:
        male_votes = 0
        female_votes = 0
        request_errors = 0
        for javdb_id in movie_javdb_ids[: cls.SAMPLE_MOVIES_PER_ACTOR]:
            try:
                detail = provider.get_movie_by_javdb_id(javdb_id)
            except MetadataNotFoundError:
                continue
            except MetadataRequestError:
                request_errors += 1
                continue
            actors = getattr(detail, "actors", ()) or ()
            match = next(
                (
                    item
                    for item in actors
                    if item.javdb_id == actor.javdb_id
                ),
                None,
            )
            if match is None or match.gender not in (1, 2):
                continue
            if match.gender == 2:
                male_votes += 1
            else:
                female_votes += 1
            if male_votes >= 2:
                return 2
            if female_votes >= 2:
                return 1
        if male_votes + female_votes == 1:
            # 只有一部可用影片时接受单票；多部但平票时不判定。
            return 2 if male_votes else 1
        if request_errors and male_votes + female_votes == 0:
            raise MetadataRequestError("GET", "sample", "all sample requests failed")
        return None

    @staticmethod
    def _emit(reporter, current: int, total: int, stats: dict) -> None:
        if reporter is None:
            return
        reporter.emit(
            current=current,
            total=total,
            text=(
                f"演员性别回填 {stats['mode']} {current}/{total}"
                f" · 修正 {stats['corrected']}"
                f" · 未判定 {stats['unresolved']}"
                f" · 失败 {stats['errors']}"
            ),
            summary_patch=dict(stats),
        )
