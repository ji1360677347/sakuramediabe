import io
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as PillowImage

from src.common.image_store import read_image_bytes
from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.config.config import settings
from src.model import Image, Movie, MoviePlotImage
from src.service.catalog.movie_cover_refresh_service import (
    MovieCoverRefreshLedger,
    MovieCoverRefreshParams,
    MovieCoverRefreshService,
)
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    MovieImageService,
)

TENANT = "rhe951l4q"


class _FakeProvider:
    """只提供租户段：封面地址由服务自己推导，不依赖详情返回。"""

    def __init__(self, tenant: str = TENANT):
        self.tenant = tenant

    def get_movie_by_number(self, movie_number: str):
        return SimpleNamespace(
            cover_image=f"https://tp.spfcas.com/{self.tenant}/covers/ab/{movie_number}.jpg"
        )


class _Reporter:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, **kwargs):
        self.events.append(kwargs)


def _image_bytes(
    image_format: str, size: tuple[int, int] = (840, 472), color=(90, 120, 200)
) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", size, color).save(buffer, format=image_format)
    return buffer.getvalue()


def _downloader(payload: bytes):
    def download(_url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    return download


def _prepare_movie(
    tmp_path,
    monkeypatch,
    *,
    number: str = "REFRESH-001",
    javdb_id: str = "abRefresh",
    suffix: str = ".webp",
    heat: int = 0,
    is_subscribed: bool = False,
    subscribed_at: datetime | None = None,
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    movie_dir = image_root / movie_asset_relative_dir(normalize_asset_dir_name(number))
    movie_dir.mkdir(parents=True)
    cover_origin = (movie_dir / f"cover{suffix}").relative_to(image_root).as_posix()
    (image_root / cover_origin).write_bytes(_image_bytes("PNG"))
    cover = Image.create(origin=cover_origin)
    movie = Movie.create(
        movie_number=number,
        title="封面刷新用例",
        javdb_id=javdb_id,
        cover_image=cover,
        heat=heat,
        is_subscribed=is_subscribed,
        subscribed_at=subscribed_at,
    )
    return movie, cover, image_root, movie_dir


def _service(tmp_path, payload: bytes, *, provider=None, ledger=None):
    return MovieCoverRefreshService(
        provider=provider or _FakeProvider(),
        image_service=MovieImageService(image_downloader=_downloader(payload)),
        ledger=ledger or MovieCoverRefreshLedger(root=tmp_path / "ledger"),
    )


def test_app_cover_url_uses_lowercase_shard_prefix():
    assert (
        MovieCoverRefreshService.app_cover_url("A2vym", TENANT)
        == f"https://tp.spfcas.com/{TENANT}/covers/a2/A2vym.jpg"
    )


def test_targets_orders_subscribed_first_then_heat(test_db, monkeypatch, tmp_path):
    _, _, _, _ = _prepare_movie(
        tmp_path,
        monkeypatch,
        number="ORDER-HOT",
        javdb_id="abHot",
        heat=900,
    )
    early = _prepare_movie(
        tmp_path,
        monkeypatch,
        number="ORDER-SUB-EARLY",
        javdb_id="abSubEarly",
        heat=10,
        is_subscribed=True,
        subscribed_at=datetime(2026, 1, 1, 0, 0, 0),
    )[0]
    late = _prepare_movie(
        tmp_path,
        monkeypatch,
        number="ORDER-SUB-LATE",
        javdb_id="abSubLate",
        heat=10,
        is_subscribed=True,
        subscribed_at=datetime(2026, 5, 1, 0, 0, 0),
    )[0]

    ordered = [movie.id for movie in MovieCoverRefreshService.targets()]

    assert ordered == [late.id, early.id, Movie.get(Movie.movie_number == "ORDER-HOT").id]


def test_run_replaces_cover_keeps_origin_suffix_and_rebuilds_pack(
    test_db, monkeypatch, tmp_path
):
    movie, cover, image_root, movie_dir = _prepare_movie(tmp_path, monkeypatch)
    # app CDN 对 FC2 这类返回 PNG，而存量封面是 webp：必须按目标后缀重编码。
    service = _service(tmp_path, _image_bytes("PNG"))

    stats = service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert stats["refreshed_movies"] == 1
    assert stats["failed_movies"] == 0
    refreshed = Movie.get_by_id(movie.id)
    assert refreshed.cover_image.origin == cover.origin
    stored = read_image_bytes(cover.origin)
    assert stored[:4] == b"RIFF" and stored[8:12] == b"WEBP"
    pack_path = movie_dir / "assets.zip"
    assert pack_path.is_file()
    with zipfile.ZipFile(pack_path) as archive:
        assert "cover.webp" in archive.namelist()
    assert not (image_root / cover.origin).exists()
    assert movie.id in service.ledger.load_done()


def test_run_keeps_downloaded_bytes_when_format_matches_suffix(
    test_db, monkeypatch, tmp_path
):
    movie, cover, _, _ = _prepare_movie(
        tmp_path, monkeypatch, suffix=".jpg", javdb_id="abJpeg"
    )
    payload = _image_bytes("JPEG")
    service = _service(tmp_path, payload)

    service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert Movie.get_by_id(movie.id).cover_image.origin == cover.origin
    assert read_image_bytes(cover.origin) == payload


def test_run_skips_movies_already_recorded(test_db, monkeypatch, tmp_path):
    movie, _, _, _ = _prepare_movie(tmp_path, monkeypatch)
    ledger = MovieCoverRefreshLedger(root=tmp_path / "ledger")
    ledger.mark_done(movie.id)
    downloaded: list[str] = []

    def downloader(url: str, target: Path) -> None:
        downloaded.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_image_bytes("JPEG"))

    service = MovieCoverRefreshService(
        provider=_FakeProvider(),
        image_service=MovieImageService(image_downloader=downloader),
        ledger=ledger,
    )

    stats = service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert downloaded == []
    assert stats["skipped_movies"] == 1
    assert stats["refreshed_movies"] == 0


def test_run_records_failure_and_keeps_existing_cover(test_db, monkeypatch, tmp_path):
    movie, cover, _, _ = _prepare_movie(tmp_path, monkeypatch)

    def downloader(_url: str, _target: Path) -> None:
        raise ImageDownloadError("unexpected_status_code:500")

    ledger = MovieCoverRefreshLedger(root=tmp_path / "ledger")
    service = MovieCoverRefreshService(
        provider=_FakeProvider(),
        image_service=MovieImageService(image_downloader=downloader),
        ledger=ledger,
    )

    stats = service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert stats["failed_movies"] == 1
    assert stats["refreshed_movies"] == 0
    assert Movie.get_by_id(movie.id).cover_image.id == cover.id
    assert ledger.load_done() == set()
    assert "unexpected_status_code:500" in ledger.failed_path.read_text(encoding="utf-8")


def test_run_retries_with_re_resolved_tenant_when_url_missing(
    test_db, monkeypatch, tmp_path
):
    movie, cover, _, _ = _prepare_movie(tmp_path, monkeypatch)
    provider = _FakeProvider(tenant="oldtenant")
    calls: list[str] = []

    def downloader(url: str, target: Path) -> None:
        calls.append(url)
        if len(calls) == 1:
            provider.tenant = "newtenant"
            raise ImageDownloadError("unexpected_status_code:404")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_image_bytes("JPEG"))

    service = MovieCoverRefreshService(
        provider=provider,
        image_service=MovieImageService(image_downloader=downloader),
        ledger=MovieCoverRefreshLedger(root=tmp_path / "ledger"),
    )

    stats = service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert stats["refreshed_movies"] == 1
    assert len(calls) == 2
    assert "oldtenant" in calls[0] and "newtenant" in calls[1]
    assert Movie.get_by_id(movie.id).cover_image.origin == cover.origin


def test_dry_run_validates_without_writing(test_db, monkeypatch, tmp_path):
    movie, cover, _, movie_dir = _prepare_movie(tmp_path, monkeypatch)
    service = _service(tmp_path, _image_bytes("PNG"))

    stats = service.run(
        reporter=_Reporter(), params=MovieCoverRefreshParams(dry_run=True)
    )

    assert stats["refreshed_movies"] == 1
    assert Movie.get_by_id(movie.id).cover_image.id == cover.id
    assert not (movie_dir / "assets.zip").exists()
    assert service.ledger.load_done() == set()


def test_run_falls_back_to_portrait_plot_for_thin_cover(
    test_db, monkeypatch, tmp_path
):
    movie, _, image_root, movie_dir = _prepare_movie(tmp_path, monkeypatch)
    plot_origin = (movie_dir / "plot-0.webp").relative_to(image_root).as_posix()
    (image_root / plot_origin).write_bytes(_image_bytes("PNG", size=(300, 500)))
    plot_image = Image.create(origin=plot_origin)
    MoviePlotImage.create(movie=movie, image=plot_image)
    # 纯色封面切不出书脊，薄封面必须回退到竖版剧情图而不是被清空。
    service = _service(tmp_path, _image_bytes("PNG"))

    service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert Movie.get_by_id(movie.id).thin_cover_image.id == plot_image.id


def test_run_keeps_existing_thin_cover_when_nothing_resolves(
    test_db, monkeypatch, tmp_path
):
    movie, _, image_root, movie_dir = _prepare_movie(tmp_path, monkeypatch)
    thin_origin = (movie_dir / "thin-cover.webp").relative_to(image_root).as_posix()
    (image_root / thin_origin).write_bytes(_image_bytes("PNG", size=(200, 300)))
    thin_cover = Image.create(origin=thin_origin)
    movie.thin_cover_image = thin_cover
    movie.save(only=[Movie.thin_cover_image])
    service = _service(tmp_path, _image_bytes("PNG"))

    service.run(reporter=_Reporter(), params=MovieCoverRefreshParams())

    assert Movie.get_by_id(movie.id).thin_cover_image.id == thin_cover.id
