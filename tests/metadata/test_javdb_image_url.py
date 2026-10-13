from src.metadata._providers.javdb import JavdbProvider


def test_normalize_image_url_keeps_app_cdn_url():
    """app CDN 是干净原图，不能再改写成带水印的网页 CDN。"""
    for url in (
        "https://tp.spfcas.com/rhe951l4q/covers/6d/6d4Qn9.jpg",
        "https://tp.spfcas.com/rhe951l4q/samples/6d/6d4Qn9_l_0.jpg",
        "https://tp.spfcas.com/rhe951l4q/avatars/g9/G955.jpg",
    ):
        assert JavdbProvider._normalize_image_url(url) == url


def test_normalize_image_url_keeps_foreign_and_empty_values():
    assert JavdbProvider._normalize_image_url(None) is None
    assert JavdbProvider._normalize_image_url("") is None
    assert JavdbProvider._normalize_image_url("https://example.test/a.jpg") == (
        "https://example.test/a.jpg"
    )
