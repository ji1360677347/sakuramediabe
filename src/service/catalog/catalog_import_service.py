"""目录导入 service。

负责把 JavDB 返回的影片/演员详情转换成本地目录数据。图片下载与图片记录持久化已抽到
``MovieImageService``，本 service 只做元数据编排，图片相关能力统一委托 ``self.image_service``。
阅读入口建议从导入语义的三个方法开始：
``import_movie_if_missing``（纯新建）、``refresh_movie_metadata_strict``（纯覆盖）、
``update_movie_fields``（指定字段更新）。
"""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from peewee import IntegrityError

from src.common import normalize_movie_number
from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import find_movie_by_number
from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import (
    Actor,
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    Tag,
    get_database,
)
from src.model.catalog.actors import merge_actor_alias_name
from src.model.catalog.movies import (
    MOVIE_ACTOR_LIST_FIELD,
    PROTECTED_MOVIE_FIELDS,
)
from src.plugins.extensions.metadata import PluginMovieMetadata
from src.service.catalog.actor_ownership_gateway import (
    JAVDB_ACTOR_FIELD_OWNER,
    ActorOwnershipGateway,
)
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.catalog.movie_image_write_service import (
    apply_thin_cover_resolution,
    delete_plot_image_vectors,
    refresh_movie_thin_cover,
)
from src.service.catalog.movie_ownership_gateway import (
    MANUAL_MOVIE_FIELD_OWNER,
    MovieOwnershipGateway,
)

# 兼容既有导入路径：ImageDownloadError 等类型历史上从本模块导出，且多处 `except ImageDownloadError`
# 依赖同一个类对象，这里显式再导出保证类身份唯一。
__all__ = [
    "CatalogImportService",
    "ImageDownloadError",
    "ImagePersistTask",
    "PreparedImageFile",
    "ThinCoverResolution",
]


class CatalogImportService:
    """承接远端元数据到本地目录模型的 upsert。"""

    JAVDB_CHECK_INTERVAL = timedelta(days=7)

    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
    ):
        # 图片子系统统一交由 MovieImageService，downloader 透传下去保住 media_import 的注入接缝。
        self.image_service = MovieImageService(image_downloader=image_downloader)
        self.persist_lock = persist_lock

    @staticmethod
    def _resolve_movie_series(series_name: str | None) -> MovieSeries | None:
        return Movie.resolve_series(series_name)

    @staticmethod
    def _movie_asset_dir(movie: Movie):
        """影片图片目录（movies/<shard>/<番号>）的相对路径。"""
        return movie_asset_relative_dir(normalize_asset_dir_name(movie.movie_number))

    def _apply_thin_cover_resolution(
        self,
        movie: Movie,
        old_thin_cover_image: Image | None,
        resolution: ThinCoverResolution,
        plot_images_by_index: dict[int, Image],
        *,
        refreshed: bool,
    ) -> set[str]:
        return apply_thin_cover_resolution(
            self.image_service,
            movie,
            old_thin_cover_image,
            resolution,
            plot_images_by_index,
            refreshed=refreshed,
        )

    def import_movie_if_missing(
        self,
        detail: JavdbMovieDetailResource,
        force_subscribed: bool = False,
    ) -> tuple[Movie, bool]:
        """已有 JavDB 影片跳过；同番号插件影片原位接入 JavDB。

        返回 ``(movie, created)``。已有 JavDB 影片手动刷新走
        ``refresh_movie_metadata_strict``；插件影片接入走 ``backfill_plugin_movie``。
        """
        # 快路径：已存在直接返回，零图片 IO。图片下载期间可能被并发导入抢先建好，
        # 事务内会二次确认（图片路径按番号确定性派生，双方下载内容一致，无孤儿文件问题）。
        local_movie = find_movie_by_number(detail.movie_number)
        if local_movie is not None and not local_movie.javdb_id and local_movie.metadata_source:
            return self.backfill_plugin_movie(local_movie, detail), False
        existing_movie = Movie.get_or_none(
            (Movie.movie_number == detail.movie_number)
            | (Movie.javdb_id == detail.javdb_id)
        )
        if existing_movie is not None:
            logger.debug(
                "Catalog import skipped existing movie movie_id={} movie_number={}",
                existing_movie.id,
                existing_movie.movie_number,
            )
            return existing_movie, False

        actors = detail.actors or []
        tags = detail.tags or []
        plot_images = detail.plot_images or []
        logger.info(
            "Catalog import start movie_number={} javdb_id={} actors={} tags={} plot_images={}",
            detail.movie_number,
            detail.javdb_id,
            len(actors),
            len(tags),
            len(plot_images),
        )
        plot_urls = self._unique_preserve_order(plot_images)
        if len(plot_urls) != len(plot_images):
            logger.debug(
                "Catalog import deduplicated plot images movie_number={} original={} deduplicated={}",
                detail.movie_number,
                len(plot_images),
                len(plot_urls),
            )

        # 影片保存进事务前先把全部图片准备好并下载完成，避免事务里做慢速网络 IO。
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self.image_service.build_movie_import_image_tasks(
            detail.movie_number,
            detail.cover_image,
            plot_urls,
            actors,
        )
        self.image_service.download_image_tasks(
            self.image_service.collect_image_tasks(
                cover_task,
                plot_tasks,
                actor_image_tasks_by_javdb_id,
            )
        )
        thin_cover_resolution = self.image_service.resolve_thin_cover_from_downloaded_images(
            detail.movie_number,
            cover_task,
            plot_tasks,
        )

        lock_context = self.persist_lock or nullcontext()
        obsolete_paths: set[str] = set()
        with lock_context, get_database().atomic():
            # 二次确认：图片下载期间并发导入可能已建好该影片，此时跳过写入。
            movie = Movie.get_or_none(
                (Movie.movie_number == detail.movie_number)
                | (Movie.javdb_id == detail.javdb_id)
            )
            if movie is not None:
                logger.debug(
                    "Catalog import concurrent created movie movie_id={} movie_number={}",
                    movie.id,
                    movie.movie_number,
                )
                return movie, False
            movie = Movie(
                movie_number=detail.movie_number,
                javdb_id=detail.javdb_id,
                title=detail.title,
            )
            # 纯新建路径：无旧封面/订阅状态可继承，直接按详情写入。
            target_is_subscribed = True if force_subscribed else detail.is_subscribed

            if cover_task is not None:
                movie.cover_image = self.image_service.persist_prepared_image(cover_task)
            movie.release_date = detail.release_date
            movie.duration_minutes = detail.duration_minutes or 0
            movie.score = detail.score or 0
            movie.score_number = detail.score_number
            movie.watched_count = detail.watched_count
            movie.want_watch_count = detail.want_watch_count
            movie.comment_count = detail.comment_count
            movie.summary = detail.summary
            movie.series = self._resolve_movie_series(detail.series_name)
            # 同步写入影片详情中的厂商和导演名称，保障检索与详情展示一致。
            movie.maker_name = detail.maker_name
            movie.director_name = detail.director_name
            if target_is_subscribed is not None:
                movie.is_subscribed = target_is_subscribed
                if target_is_subscribed:
                    movie.subscribed_at = utc_now_for_db()
                else:
                    movie.subscribed_at = None
            movie.title = detail.title
            movie.javdb_id = detail.javdb_id
            movie.movie_number = detail.movie_number
            movie.save()
            logger.debug(
                "Catalog import movie saved movie_id={} movie_number={}",
                movie.id,
                movie.movie_number,
            )

            # 演员、标签、剧照关系都使用 get_or_create，避免多次导入产生重复关联。
            for actor_resource in actors:
                actor = self.upsert_actor_from_javdb_resource(
                    actor_resource,
                    profile_image_task=actor_image_tasks_by_javdb_id.get(actor_resource.javdb_id),
                    update_gender=True,
                )
                MovieActor.get_or_create(movie=movie, actor=actor)
                logger.debug(
                    "Catalog import actor linked movie_id={} actor_id={} actor_javdb_id={}",
                    movie.id,
                    actor.id,
                    actor.javdb_id,
                )

            for tag_resource in tags:
                tag, _ = Tag.get_or_create(name=tag_resource.name)
                MovieTag.get_or_create(movie=movie, tag=tag)
                logger.debug(
                    "Catalog import tag linked movie_id={} tag_id={} tag_name={}",
                    movie.id,
                    tag.id,
                    tag.name,
                )

            plot_images_by_index: dict[int, Image] = {}
            # 剧照整批一次 upsert，避免逐张 get_or_none + create 的 2N 次往返。
            plot_images_by_path = self.image_service.persist_prepared_images(plot_tasks)
            for plot_task in plot_tasks:
                plot_image = plot_images_by_path.get(plot_task.relative_path)
                if plot_image is not None:
                    if plot_task.plot_index is not None:
                        plot_images_by_index[int(plot_task.plot_index)] = plot_image
                    MoviePlotImage.get_or_create(movie=movie, image=plot_image)
                    logger.debug(
                        "Catalog import plot image linked movie_id={} image_id={} index={}",
                        movie.id,
                        plot_image.id,
                        plot_task.plot_index,
                    )
            obsolete_paths.update(
                self._apply_thin_cover_resolution(
                    movie,
                    None,
                    thin_cover_resolution,
                    plot_images_by_index,
                    refreshed=False,
                )
            )

        self.image_service.delete_obsolete_image_files(obsolete_paths)
        MovieAssetPackService.rebuild_movie_asset_pack(self._movie_asset_dir(movie))
        MovieHeatService.update_single_movie_heat(movie.id)
        logger.info(
            "Catalog import finished movie_id={} movie_number={}",
            movie.id,
            movie.movie_number,
        )
        return movie, True

    def import_plugin_movie(
        self, detail: PluginMovieMetadata, source: dict[str, str | None], provider,
        *, force_subscribed: bool = False,
    ) -> tuple[Movie, bool]:
        from src.service.catalog.metadata_source_service import MetadataSourceService

        existing = find_movie_by_number(detail.movie_number)
        if existing is not None:
            return existing, False
        actors = MetadataSourceService.match_actors(detail.actors, provider, self)
        with self.image_service.prepare_metadata_images(
            detail.movie_number,
            detail.cover_image_path,
            detail.plot_image_paths,
            local=True,
        ) as (cover, plots, _actor_tasks, thin):
            try:
                with self.persist_lock or nullcontext(), get_database().atomic():
                    existing = find_movie_by_number(detail.movie_number)
                    if existing is not None:
                        return existing, False
                    movie = Movie.create(
                        movie_number=detail.movie_number,
                        javdb_id=None,
                        title=detail.title,
                        summary=detail.summary,
                        release_date=detail.release_date,
                        duration_minutes=detail.duration_minutes,
                        maker_name=detail.maker_name,
                        director_name=detail.director_name,
                        series=self._resolve_movie_series(detail.series_name),
                        cover_image=self.image_service.persist_prepared_image(cover),
                        metadata_source=source,
                        javdb_next_check_at=utc_now_for_db() + self.JAVDB_CHECK_INTERVAL,
                        is_subscribed=force_subscribed,
                        subscribed_at=utc_now_for_db() if force_subscribed else None,
                    )
                    for actor in actors:
                        MovieActor.get_or_create(movie=movie, actor=actor)
                    for name in detail.tags:
                        tag, _ = Tag.get_or_create(name=name)
                        MovieTag.get_or_create(movie=movie, tag=tag)
                    images = self.image_service.persist_prepared_images(plots)
                    by_index = {}
                    for task in plots:
                        image = images[task.relative_path]
                        MoviePlotImage.create(movie=movie, image=image)
                        by_index[task.plot_index] = image
                    self._apply_thin_cover_resolution(
                        movie, None, thin, by_index, refreshed=False
                    )
            except IntegrityError:
                existing = find_movie_by_number(detail.movie_number)
                if existing is None:
                    raise
                return existing, False
        MovieAssetPackService.rebuild_movie_asset_pack(self._movie_asset_dir(movie))
        return movie, True


    def _plan_missing_images(
        self,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        actors: list[JavdbMovieActorResource],
    ) -> tuple[str | None, list[str], list[JavdbMovieActorResource]]:
        """只补缺：算出真正需要下载的封面、剧照与缺头像的演员。

        图片 owner 已存在时视为"有人负责"，宿主自动路径不再补；已有剧照批次
        只整批存在或不存，不做逐张补齐。
        """
        owners = movie.field_owners or {}
        cover_url = None
        if movie.cover_image_id is None and owners.get("cover_image") is None:
            cover_url = detail.cover_image
        plot_urls: list[str] = []
        if owners.get("plot_images") is None and not MoviePlotImage.select().where(
            MoviePlotImage.movie == movie.id
        ).exists():
            plot_urls = self._unique_preserve_order(detail.plot_images or [])
        actor_ids = [actor.javdb_id for actor in actors if actor.javdb_id]
        missing_avatar_ids: set[str] = set(actor_ids)
        if actor_ids:
            for actor_row in Actor.select().where(Actor.javdb_id.in_(actor_ids)):
                if (
                    actor_row.profile_image_id is not None
                    or actor_row.profile_image_override_id is not None
                ):
                    missing_avatar_ids.discard(actor_row.javdb_id)
        actors_to_fill = [
            actor for actor in actors if actor.javdb_id in missing_avatar_ids
        ]
        return cover_url, plot_urls, actors_to_fill

    def backfill_plugin_movie(
        self,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        *,
        replace_images: bool = False,
    ) -> Movie:
        """把插件影片接入 JavDB：文案/互动全量覆盖。

        图片默认只补缺；``replace_images=True`` 是人工强制重刷，整批替换封面/剧照
        并重新下载缺头像演员的头像。
        """
        if normalize_movie_number(movie.movie_number) != normalize_movie_number(
            detail.movie_number
        ):
            raise ValueError("JavDB 补录番号不匹配")
        if not detail.javdb_id:
            raise ValueError("JavDB 补录缺少真实 ID")
        conflict = (
            Movie.select()
            .where((Movie.javdb_id == detail.javdb_id) & (Movie.id != movie.id))
            .exists()
        )
        if conflict:
            raise ValueError("JavDB ID 已属于其他本地影片")
        actors = detail.actors if detail.actors_available else []
        if replace_images:
            cover_url = detail.cover_image
            plot_urls = self._unique_preserve_order(detail.plot_images or [])
            actors_for_avatars = actors
        else:
            cover_url, plot_urls, actors_for_avatars = self._plan_missing_images(
                movie, detail, actors
            )
        obsolete_paths: set[str] = set()
        old_plot_ids: list[int] = []
        with (
            self.image_service.prepare_metadata_images(
                movie.movie_number, cover_url, plot_urls, actors_for_avatars
            ) as (cover, plots, actor_tasks, thin),
            self.persist_lock or nullcontext(),
            get_database().atomic(),
        ):
            movie = Movie.select().where(Movie.id == movie.id).for_update().get()
            if movie.javdb_id:
                return movie
            owners = movie.field_owners or {}
            old_images: list[Image] = []
            if replace_images:
                old_images = [movie.cover_image, movie.thin_cover_image]
                if movie.cover_image is not None:
                    movie.cover_image = None
                    movie.save(only=[Movie.cover_image])
                if movie.thin_cover_image is not None:
                    movie.thin_cover_image = None
                    movie.save(only=[Movie.thin_cover_image])
                old_links = list(
                    MoviePlotImage.select(MoviePlotImage, Image)
                    .join(Image)
                    .where(MoviePlotImage.movie == movie)
                    .order_by(MoviePlotImage.id)
                )
                if old_links:
                    MoviePlotImage.delete().where(
                        MoviePlotImage.movie == movie
                    ).execute()
                old_images.extend(link.image for link in old_links)
                old_plot_ids = [link.id for link in old_links]
                if (
                    owners.get("cover_image") is not None
                    or owners.get("plot_images") is not None
                ):
                    MovieOwnershipGateway.release_image_owners(
                        movie.id, ("cover_image", "plot_images")
                    )
            values = {
                "javdb_id": detail.javdb_id,
                "javdb_next_check_at": None,
                "score": detail.score if detail.score is not None else 0,
                "score_number": detail.score_number,
                "watched_count": detail.watched_count,
                "want_watch_count": detail.want_watch_count,
                "comment_count": detail.comment_count,
            }
            if detail.release_date:
                values["release_date"] = detail.release_date
            if detail.duration_minutes > 0:
                values["duration_minutes"] = detail.duration_minutes
            if detail.series_name:
                values["series"] = self._resolve_movie_series(detail.series_name)
            for name, value in values.items():
                setattr(movie, name, value)
            movie.save(only=[Movie._meta.fields[name] for name in values])
            MovieOwnershipGateway.update_host_unowned(
                movie.id,
                {
                    name: getattr(detail, name)
                    for name in ("title", "summary", "maker_name", "director_name")
                    if getattr(detail, name)
                },
            )
            added_images = False
            cover_written = False
            if (
                cover is not None
                and (
                    replace_images
                    or (
                        movie.cover_image_id is None
                        and owners.get("cover_image") is None
                    )
                )
            ):
                movie.cover_image = self.image_service.persist_prepared_image(cover)
                movie.save(only=[Movie.cover_image])
                added_images = True
                cover_written = True
            by_index = {}
            if (
                plots
                and (
                    replace_images
                    or (
                        owners.get("plot_images") is None
                        and not MoviePlotImage.select()
                        .where(MoviePlotImage.movie == movie)
                        .exists()
                    )
                )
            ):
                images = self.image_service.persist_prepared_images(plots)
                for task in plots:
                    image = images[task.relative_path]
                    MoviePlotImage.create(movie=movie, image=image)
                    by_index[task.plot_index] = image
                added_images = True
            # 薄封面若来自封面，只有封面确实写入时才可用：被拒收的下载图不能当薄封面。
            thin_cover_usable = thin.generated_task is None or replace_images or cover_written
            if (
                (replace_images or added_images)
                and movie.thin_cover_image_id is None
                and thin_cover_usable
                and (
                    thin.generated_task is not None
                    or thin.selected_plot_index is not None
                )
            ):
                self._apply_thin_cover_resolution(
                    movie, None, thin, by_index, refreshed=False
                )
            if (
                detail.actors_available
                and owners.get(MOVIE_ACTOR_LIST_FIELD) != MANUAL_MOVIE_FIELD_OWNER
            ):
                MovieActor.delete().where(MovieActor.movie == movie).execute()
                for actor_detail in actors:
                    if replace_images:
                        existing_actor = Actor.get_or_none(
                            Actor.javdb_id == actor_detail.javdb_id
                        )
                        if existing_actor is not None and existing_actor.profile_image is not None:
                            old_images.append(existing_actor.profile_image)
                    actor = self.upsert_actor_from_javdb_resource(
                        actor_detail,
                        profile_image_task=actor_tasks.get(actor_detail.javdb_id),
                        update_gender=True,
                        replace_avatar=replace_images,
                    )
                    MovieActor.get_or_create(movie=movie, actor=actor)
            if detail.tags_available:
                self._replace_movie_tags(movie, detail.tags)
            for image in {
                image.id: image for image in old_images if image is not None
            }.values():
                obsolete_paths.update(
                    self.image_service.delete_image_record_if_unused(image)
                )
            MovieHeatService.update_single_movie_heat(movie.id)
        self.image_service.delete_obsolete_image_files(obsolete_paths)
        delete_plot_image_vectors(old_plot_ids)
        self._backfill_thin_cover_if_missing(movie, replace_images=replace_images)
        MovieAssetPackService.rebuild_movie_asset_pack(self._movie_asset_dir(movie))
        return Movie.get_by_id(movie.id)

    # ③ 允许更新的字段白名单 -> detail 取值器；heat 是推导列不允许直接写，
    # 图片/演员/标签/剧照等关联字段不在本机制内（新建时由 import_movie_if_missing 完整导入）。
    _MOVIE_FIELD_UPDATE_MAP: dict[str, Callable[[JavdbMovieDetailResource], Any]] = {
        "score": lambda detail: detail.score or 0,
        "score_number": lambda detail: detail.score_number,
        "watched_count": lambda detail: detail.watched_count,
        "want_watch_count": lambda detail: detail.want_watch_count,
        "comment_count": lambda detail: detail.comment_count,
        "title": lambda detail: detail.title,
        "summary": lambda detail: detail.summary,
        "maker_name": lambda detail: detail.maker_name,
        "director_name": lambda detail: detail.director_name,
    }

    def update_movie_fields(
        self,
        detail: JavdbMovieDetailResource,
        fields: tuple[str, ...],
    ) -> tuple[Movie, bool, tuple[str, ...]]:
        """指定字段更新：影片不存在先完整导入（import_movie_if_missing），存在则只更新指定字段。

        返回 ``(movie, created, updated_fields)``，updated_fields 是实际发生变化的字段
        （变更检测：值一致的字段跳过不写）；heat 联动由调用方负责（本方法不感知热度公式）。
        """
        if not fields:
            raise ValueError("fields 不能为空")
        normalized_fields = tuple(dict.fromkeys(fields))
        invalid_fields = sorted(set(normalized_fields) - set(self._MOVIE_FIELD_UPDATE_MAP))
        if invalid_fields:
            raise ValueError(f"不支持的字段: {', '.join(invalid_fields)}")
        movie, created = self.import_movie_if_missing(detail)
        # 变更检测：只写值真正变化的字段，避免无意义 UPDATE，也支撑调用方的 updated/unchanged 计数。
        previous_values: dict[str, Any] = {}
        changed_fields: list[str] = []
        for field_name in normalized_fields:
            target_value = self._MOVIE_FIELD_UPDATE_MAP[field_name](detail)
            previous_value = getattr(movie, field_name)
            if previous_value == target_value:
                continue
            previous_values[field_name] = previous_value
            setattr(movie, field_name, target_value)
            changed_fields.append(field_name)
        if changed_fields:
            # 受保护字段（白名单内，如 title/summary）分流到唯一写入网关，宿主侧
            # 只更新未接管字段；其余字段保持窄更新。已持久化对象不能裸 save（护栏）。
            protected_changed = [
                field_name
                for field_name in changed_fields
                if field_name in PROTECTED_MOVIE_FIELDS
            ]
            host_changed = [
                field_name
                for field_name in changed_fields
                if field_name not in PROTECTED_MOVIE_FIELDS
            ]
            if host_changed:
                movie.save(
                    only=[Movie._meta.fields[field_name] for field_name in host_changed]
                )
            if protected_changed:
                MovieOwnershipGateway.update_host_unowned(
                    movie.id,
                    {
                        field_name: getattr(movie, field_name)
                        for field_name in protected_changed
                    },
                )
                # 被插件接管的字段未落库：重读该行并把实际落库变更回流到结果，
                # 保证返回对象与库内真实状态一致、updated 字段不虚报。
                movie = Movie.get_by_id(movie.id)
                changed_fields = [
                    field_name
                    for field_name in changed_fields
                    if getattr(movie, field_name) != previous_values[field_name]
                ]
        return movie, created, tuple(changed_fields)

    def refresh_movie_metadata_strict(
        self,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        *,
        replace_images: bool = False,
    ) -> Movie:
        """按远端详情覆盖已存在影片的元数据：文案/互动/演员/标签全量替换，不触碰订阅与番号字段。

        是已存在影片元数据刷新的唯一入口（手动刷新接口），影片必须已存在。
        图片默认只补缺（缺封面/剧照/头像才下载），``replace_images=True`` 才整批
        替换图片，供人工强制重刷使用。
        """
        actors = detail.actors or []
        tags = detail.tags or []
        actors_for_avatars = actors
        if replace_images:
            cover_url = detail.cover_image
            plot_urls = self._unique_preserve_order(detail.plot_images or [])
        else:
            cover_url, plot_urls, actors_for_avatars = self._plan_missing_images(
                movie, detail, actors
            )
        # 演员表已人工接管或远端解析不完整时，本次不会重建关联，也就不需要下载头像；
        # 事务内仍会以锁后读到的最新 owner 为准，这里只是避免无意义的图片 IO。
        actor_avatars_enabled = (
            detail.actors_available
            and (movie.field_owners or {}).get(MOVIE_ACTOR_LIST_FIELD)
            != MANUAL_MOVIE_FIELD_OWNER
        )
        if not actor_avatars_enabled:
            actors_for_avatars = []
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self.image_service.build_movie_import_image_tasks(
            movie.movie_number,
            cover_url,
            plot_urls,
            actors_for_avatars,
        )
        image_tasks = self.image_service.collect_image_tasks(
            cover_task,
            plot_tasks,
            actor_image_tasks_by_javdb_id,
        )

        # 严格刷新先把新图片全部下载到临时目录，避免中途失败污染正式目录。
        prepared_files = self.image_service.download_image_tasks_to_temporary_files(image_tasks)
        if replace_images or movie.thin_cover_image_id is None:
            thin_cover_resolution = self.image_service.resolve_thin_cover_from_prepared_images(
                movie.movie_number,
                cover_task,
                plot_tasks,
                prepared_files,
            )
        else:
            thin_cover_resolution = ThinCoverResolution()
        if thin_cover_resolution.generated_prepared_file is not None:
            prepared_files.append(thin_cover_resolution.generated_prepared_file)
        original_prepared_files = prepared_files
        finalized = False
        obsolete_paths: set[str] = set()
        old_plot_image_ids: list[int] = []
        try:
            lock_context = self.persist_lock or nullcontext()
            with lock_context, get_database().atomic():
                (
                    persisted_movie,
                    obsolete_paths,
                    old_plot_image_ids,
                    rejected_images,
                    replace_actors,
                ) = self._refresh_movie_metadata_records_strict(
                    movie=movie,
                    detail=detail,
                    actors=actors,
                    tags=tags,
                    thin_cover_resolution=thin_cover_resolution,
                    cover_task=cover_task,
                    plot_tasks=plot_tasks,
                    actor_image_tasks_by_javdb_id=actor_image_tasks_by_javdb_id,
                    replace_images=replace_images,
                )
            if not replace_actors and actor_image_tasks_by_javdb_id:
                # 锁后确认演员表已被接管：本次下载的头像不再落引用，删除临时文件，
                # 避免 finalize 后留下没有数据库引用的孤儿图片。
                actor_task_ids = {
                    id(task) for task in actor_image_tasks_by_javdb_id.values()
                }
                kept_prepared_files = []
                for prepared in prepared_files:
                    if id(prepared.image_task) in actor_task_ids:
                        prepared.temp_path.unlink(missing_ok=True)
                    else:
                        kept_prepared_files.append(prepared)
                prepared_files = kept_prepared_files
                if not prepared_files:
                    self.image_service.cleanup_prepared_image_files(
                        original_prepared_files
                    )
            new_relative_paths = {
                prepared.image_task.relative_path for prepared in prepared_files
            }
            delete_plot_image_vectors(old_plot_image_ids)
            if prepared_files:
                MovieAssetPackService.remove_movie_asset_pack(self._movie_asset_dir(persisted_movie))
            self.image_service.finalize_prepared_image_files(prepared_files)
            self.image_service.delete_obsolete_image_files(obsolete_paths - new_relative_paths)
            rejected_paths: set[str] = set()
            for image in rejected_images:
                rejected_paths.update(self.image_service.delete_image_record_if_unused(image))
            self.image_service.delete_obsolete_image_files(rejected_paths)
            thin_cover_backfilled = self._backfill_thin_cover_if_missing(
                persisted_movie, replace_images=replace_images
            )
            if prepared_files or thin_cover_backfilled:
                MovieAssetPackService.rebuild_movie_asset_pack(self._movie_asset_dir(persisted_movie))
            if thin_cover_backfilled:
                persisted_movie = Movie.get_by_id(persisted_movie.id)
            finalized = True
            logger.info(
                "Catalog strict metadata refresh finished movie_id={} movie_number={}",
                persisted_movie.id,
                persisted_movie.movie_number,
            )
            return persisted_movie
        finally:
            if not finalized:
                self.image_service.cleanup_prepared_image_files(prepared_files)

    def _refresh_movie_metadata_records_strict(
        self,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        actors: list[JavdbMovieActorResource],
        tags: list,
        thin_cover_resolution: ThinCoverResolution,
        cover_task: ImagePersistTask | None,
        plot_tasks: list[ImagePersistTask],
        actor_image_tasks_by_javdb_id: dict[str, ImagePersistTask],
        replace_images: bool,
    ) -> tuple[Movie, set[str], list[int], list[Image], bool]:
        # 事务内先锁行重读（v2-lite 字段主权）：锁定期间当前 owner 状态稳定，
        # 之后受保护字段写入以本次读取为准，杜绝旧快照覆盖插件刚写入的值。
        movie = (
            Movie.select()
            .where(Movie.id == movie.id)
            .for_update()
            .get()
        )
        obsolete_paths: set[str] = set()
        # 下载期间槽位可能已被插件/人工接管：本次下载的图不再落引用，交调用方回收。
        rejected_images: list[Image] = []
        movie_owners = movie.field_owners or {}
        # 用户编辑过演员表后不再整表替换；actors_available=False 表示远端解析不完整，
        # 同样保留现有关系，避免被部分数据清空。
        replace_actors = (
            detail.actors_available
            and movie_owners.get(MOVIE_ACTOR_LIST_FIELD)
            != MANUAL_MOVIE_FIELD_OWNER
        )
        cover_taken = (
            not replace_images
            and cover_task is not None
            and movie_owners.get("cover_image") is not None
        )
        plots_taken = (
            not replace_images
            and bool(plot_tasks)
            and movie_owners.get("plot_images") is not None
        )

        old_cover_image = movie.cover_image
        old_thin_cover_image = movie.thin_cover_image
        old_plot_links = list(
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        old_plot_image_ids: list[int] = []
        if replace_images:
            if old_cover_image is not None:
                movie.cover_image = None
                movie.save(only=[Movie.cover_image])
            if old_thin_cover_image is not None:
                movie.thin_cover_image = None
                movie.save(only=[Movie.thin_cover_image])
            # 整批替换后图片回到宿主自动管理：清掉插件/人工留下的图片 owner。
            if movie_owners.get("cover_image") is not None or movie_owners.get("plot_images") is not None:
                MovieOwnershipGateway.release_image_owners(
                    movie.id, ("cover_image", "plot_images")
                )
            # 先清空旧剧情图关联，再按远端最新顺序重建，保证详情页严格一致。
            if old_plot_links:
                MoviePlotImage.delete().where(MoviePlotImage.movie == movie).execute()
            old_plot_image_ids = [link.id for link in old_plot_links]

        # 演员关系同样按远端列表全量替换，避免残留已下线演员。
        if replace_actors:
            MovieActor.delete().where(MovieActor.movie == movie).execute()

        images_to_cleanup: dict[int, Image] = {}
        if replace_images:
            for image in [old_cover_image, old_thin_cover_image, *[plot_link.image for plot_link in old_plot_links]]:
                if image is None:
                    continue
                images_to_cleanup[int(image.id)] = image
            for image in images_to_cleanup.values():
                obsolete_paths.update(self.image_service.delete_image_record_if_unused(image))

        movie.release_date = detail.release_date
        movie.duration_minutes = detail.duration_minutes or 0
        movie.score = detail.score or 0
        movie.score_number = detail.score_number
        movie.watched_count = detail.watched_count
        movie.want_watch_count = detail.want_watch_count
        movie.comment_count = detail.comment_count
        movie.summary = detail.summary
        movie.series = self._resolve_movie_series(detail.series_name)
        movie.maker_name = detail.maker_name
        movie.director_name = detail.director_name
        movie.javdb_id = detail.javdb_id
        movie.title = detail.title
        if cover_taken:
            rejected_images.append(
                self.image_service.persist_refreshed_image_record(cover_task)
            )
        elif replace_images or cover_task is not None:
            movie.cover_image = self.image_service.persist_refreshed_image_record(cover_task)

        # 受保护字段（白名单内）不允许随宿主窄更新落库，改走唯一写入网关
        # （update_host_unowned 只更新未接管字段）；其余字段显式窄更新。
        host_field_names = (
            "release_date",
            "duration_minutes",
            "score",
            "score_number",
            "watched_count",
            "want_watch_count",
            "comment_count",
            "summary",
            "series",
            "maker_name",
            "director_name",
            "javdb_id",
            "title",
            "cover_image",
        )
        protected_field_names = set(PROTECTED_MOVIE_FIELDS) & set(host_field_names)
        narrow_columns = [
            Movie._meta.fields[name]
            for name in host_field_names
            if name not in protected_field_names
        ]
        movie.save(only=narrow_columns)
        if protected_field_names:
            MovieOwnershipGateway.update_host_unowned(
                movie.id,
                {name: getattr(movie, name) for name in protected_field_names},
            )

        seen_actor_ids: set[str] = set()
        if replace_actors:
            for actor_resource in actors:
                if actor_resource.javdb_id in seen_actor_ids:
                    continue
                seen_actor_ids.add(actor_resource.javdb_id)
                actor, actor_obsolete_paths = self._refresh_actor_from_javdb_resource_strict(
                    actor_resource=actor_resource,
                    profile_image_task=actor_image_tasks_by_javdb_id.get(actor_resource.javdb_id),
                    replace_images=replace_images,
                )
                obsolete_paths.update(actor_obsolete_paths)
                MovieActor.get_or_create(movie=movie, actor=actor)

        self._replace_movie_tags(movie, tags)

        plot_images_by_index: dict[int, Image] = {}
        has_existing_plots = MoviePlotImage.select().where(
            MoviePlotImage.movie == movie
        ).exists()
        if plots_taken:
            rejected_images.extend(
                self.image_service.persist_refreshed_image_records(plot_tasks).values()
            )
        elif replace_images or (plot_tasks and not has_existing_plots):
            # 剧照整批一次 upsert，避免逐张 get_or_none + create 的 2N 次往返。
            plot_images_by_path = self.image_service.persist_refreshed_image_records(plot_tasks)
            for plot_task in plot_tasks:
                plot_image = plot_images_by_path.get(plot_task.relative_path)
                if plot_image is None:
                    continue
                if plot_task.plot_index is not None:
                    plot_images_by_index[int(plot_task.plot_index)] = plot_image
                MoviePlotImage.get_or_create(movie=movie, image=plot_image)
        # 封面被他人接管时不采用本次下载解析出的薄封面（可能取自被拒收的封面图），
        # 交给调用方基于库里真实图片重算。
        if (replace_images or movie.thin_cover_image_id is None) and not cover_taken:
            obsolete_paths.update(
                self._apply_thin_cover_resolution(
                    movie,
                    None,
                    thin_cover_resolution,
                    plot_images_by_index,
                    refreshed=True,
                )
            )

        # 受保护字段可能未全部落库（被插件接管的字段保留插件值）：重读该行，
        # 保证返回对象与库内真实状态一致（内存中的远端值不得外泄给调用方）。
        movie = Movie.get_by_id(movie.id)
        return (
            movie,
            obsolete_paths,
            old_plot_image_ids,
            rejected_images,
            replace_actors,
        )

    @staticmethod
    def _replace_movie_tags(movie: Movie, tags) -> None:
        MovieTag.delete().where(MovieTag.movie == movie).execute()
        for name in dict.fromkeys((tag.name or "").strip() for tag in tags):
            if name:
                tag, _ = Tag.get_or_create(name=name)
                MovieTag.get_or_create(movie=movie, tag=tag)

    def _merge_resource_into_canonical(
        self,
        merged_actor: Actor,
        actor_resource: JavdbMovieActorResource,
        *,
        update_gender: bool = False,
    ) -> Actor:
        """来源 javdb_id 已合并时，只把新名字并入保留记录，不覆盖其身份与头像。"""
        canonical = Actor.resolve_canonical(merged_actor.id)
        if canonical is None:
            raise RuntimeError(f"merged_into 指向的演员缺失 actor_id={merged_actor.id}")
        lock_context = self.persist_lock or nullcontext()
        with lock_context, get_database().atomic():
            canonical = Actor.get_by_id(canonical.id)
            merged_alias = merge_actor_alias_name(
                primary_name=canonical.name,
                alias_names=[actor_resource.name, *actor_resource.alias_names],
                existing_alias_name=canonical.alias_name,
            )
            if merged_alias != canonical.alias_name:
                canonical.alias_name = merged_alias
                canonical.save(only=[Actor.alias_name])
            if update_gender and actor_resource.gender in (1, 2):
                ActorOwnershipGateway.update_host_source(
                    canonical.id,
                    {"gender": actor_resource.gender},
                )
            canonical = Actor.get_by_id(canonical.id)
        return canonical

    def _refresh_actor_from_javdb_resource_strict(
        self,
        *,
        actor_resource: JavdbMovieActorResource,
        profile_image_task: ImagePersistTask | None,
        replace_images: bool,
    ) -> tuple[Actor, set[str]]:
        actor = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id)
        if actor is not None and actor.merged_into_id is not None:
            return self._merge_resource_into_canonical(actor, actor_resource), set()
        if actor is None:
            profile_image = self.image_service.persist_refreshed_image_record(profile_image_task)
            merged_alias_name = merge_actor_alias_name(
                primary_name=actor_resource.name,
                alias_names=actor_resource.alias_names,
                existing_alias_name="",
            )
            return (
                Actor.create(
                    javdb_id=actor_resource.javdb_id,
                    name=actor_resource.name,
                    alias_name=merged_alias_name,
                    profile_image=profile_image,
                    javdb_type=actor_resource.javdb_type,
                ),
                set(),
            )

        obsolete_paths: set[str] = set()
        if replace_images:
            old_profile_image = actor.profile_image
            actor.profile_image = None
            actor.save(only=[Actor.profile_image])
            if old_profile_image is not None:
                obsolete_paths.update(
                    self.image_service.delete_image_record_if_unused(old_profile_image)
                )
            profile_image = self.image_service.persist_refreshed_image_record(
                profile_image_task
            )
        elif actor.profile_image_id is None and actor.profile_image_override_id is None:
            # 只补缺：没有头像（也没有人工/插件覆盖）时才写入。
            profile_image = self.image_service.persist_refreshed_image_record(
                profile_image_task
            )
        else:
            profile_image = actor.profile_image
        actor.name = actor_resource.name
        actor.alias_name = merge_actor_alias_name(
            primary_name=actor_resource.name,
            alias_names=actor_resource.alias_names,
            existing_alias_name=actor.alias_name,
        )
        actor.javdb_type = actor_resource.javdb_type
        actor.profile_image = profile_image
        actor.save(only=[Actor.name, Actor.alias_name, Actor.javdb_type, Actor.profile_image])
        return actor, obsolete_paths

    def backfill_movie_thin_cover(self, movie: Movie) -> bool:
        """基于已落盘的封面和剧情图，为历史影片补算竖封面图。"""
        return refresh_movie_thin_cover(movie.id, self.image_service)

    def _backfill_thin_cover_if_missing(
        self,
        movie: Movie,
        *,
        replace_images: bool,
    ) -> bool:
        """图片未被本次刷新替换时薄封面可能仍缺失：基于已落盘图片重算，保留自愈。

        ``replace_images=True`` 已从新下载的封面/剧照重算过，无需再算。
        返回是否真的重算出了薄封面，供调用方决定资产包重建与重读。
        """
        if replace_images or movie.thin_cover_image_id is not None:
            return False
        return refresh_movie_thin_cover(
            movie.id, self.image_service, rebuild_asset_pack=False
        )

    def upsert_actor_from_javdb_resource(
        self,
        actor_resource: JavdbMovieActorResource,
        profile_image_task: ImagePersistTask | None = None,
        *,
        update_gender: bool = False,
        replace_avatar: bool = False,
    ) -> Actor:
        """把 JavDB 演员资源同步到本地，并在可用时补全头像。

        演员搜索等非影片入库场景只能同步身份和头像信息；只有新影片入库
        显式传入 ``update_gender=True`` 时，才使用影片详情中的性别写入本地。
        头像只补缺：本地已有头像（含人工/插件覆盖）时不替换；
        ``replace_avatar=True`` 是显式强制替换，供人工强制重刷使用。
        """
        existing = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id)
        if existing is not None and existing.merged_into_id is not None:
            return self._merge_resource_into_canonical(
                existing, actor_resource, update_gender=update_gender
            )

        existing_has_avatar = existing is not None and (
            existing.profile_image_id is not None
            or existing.profile_image_override_id is not None
        )
        if existing_has_avatar and not replace_avatar:
            profile_image = None
        elif profile_image_task is None:
            profile_image = self.image_service.persist_image(
                owner_type="actor",
                owner_key=actor_resource.javdb_id,
                image_url=actor_resource.avatar_url,
            )
        else:
            profile_image = self.image_service.persist_prepared_image(profile_image_task)

        lock_context = self.persist_lock or nullcontext()
        with lock_context, get_database().atomic():
            explicit_gender = (
                actor_resource.gender
                if update_gender and actor_resource.gender in (1, 2)
                else None
            )
            merged_alias_name = merge_actor_alias_name(
                primary_name=actor_resource.name,
                alias_names=actor_resource.alias_names,
                existing_alias_name="",
            )
            defaults = {
                "name": actor_resource.name,
                "alias_name": merged_alias_name,
                "profile_image": profile_image,
                "javdb_type": actor_resource.javdb_type,
            }
            if explicit_gender is not None:
                defaults["gender"] = explicit_gender
                defaults["field_owners"] = {"gender": JAVDB_ACTOR_FIELD_OWNER}
            actor, created = Actor.get_or_create(
                javdb_id=actor_resource.javdb_id,
                defaults=defaults,
            )
            if not created:
                actor.name = actor_resource.name
                # 只合并权威来源给出的名字集合，避免把用户输入直接污染到 alias。
                actor.alias_name = merge_actor_alias_name(
                    primary_name=actor_resource.name,
                    alias_names=actor_resource.alias_names,
                    existing_alias_name=actor.alias_name,
                )
                actor.javdb_type = actor_resource.javdb_type
                if profile_image is not None:
                    actor.profile_image = profile_image
                columns = [Actor.name, Actor.alias_name, Actor.javdb_type]
                if profile_image is not None:
                    columns.append(Actor.profile_image)
                actor.save(only=columns)
                if explicit_gender is not None:
                    ActorOwnershipGateway.update_host_source(
                        actor.id,
                        {"gender": explicit_gender},
                    )
                    actor = Actor.get_by_id(actor.id)

        return actor

    def _unique_preserve_order(self, items: list[str]) -> list[str]:
        """在保留 JavDB 原始顺序的前提下去重。"""
        unique_items: list[str] = []
        seen_items = set()
        for item in items:
            if item in seen_items:
                continue
            seen_items.add(item)
            unique_items.append(item)
        return unique_items
