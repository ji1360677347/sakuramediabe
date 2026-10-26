import json
from string import Formatter

from src.i18n.catalog import CATALOG_DIR, _select_value, has_translation, translate


def _load_catalog(filename: str) -> dict:
    return json.loads((CATALOG_DIR / filename).read_text(encoding="utf-8"))


def _placeholders(value) -> set[str]:
    values = value.values() if isinstance(value, dict) else [value]
    result: set[str] = set()
    for text in values:
        if not isinstance(text, str):
            continue
        for _, field_name, _, _ in Formatter().parse(text):
            if field_name:
                result.add(field_name)
    return result


def test_catalogs_keep_key_and_placeholder_parity():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")

    # zh-CN 是任务/通知的源文案；en 可以多出仅错误使用的 errors.*。
    assert set(zh) <= set(en)
    assert all(key.startswith("errors.") for key in set(en) - set(zh))
    for key in set(zh):
        assert _placeholders(zh[key]) == _placeholders(en[key]), key


def test_zh_cn_sample_text_matches_existing_code_strings():
    zh = _load_catalog("zh-CN.json")

    assert zh["notifications.task_failed.title"] == "{task_name}执行失败"
    assert zh["notifications.task_failed.content"] == "后台任务执行失败"
    assert zh["notifications.task_completed.title"] == "{task_name}已完成"
    assert zh["notifications.task_completed.content"] == "后台任务已完成"
    assert zh["tasks.actor_subscription_sync.progress.start"] == "开始同步已订阅演员影片"


def test_plural_selection_uses_count():
    assert _select_value({"one": "one", "other": "other"}, {"count": 1}) == "one"
    assert _select_value({"one": "one", "other": "other"}, {"count": 0}) == "other"
    assert _select_value({"one": "one", "other": "other"}, {"count": 2}) == "other"
    assert _select_value("plain", {}) == "plain"


def test_translate_interpolates_params():
    assert (
        translate("errors.invalid_language", locale="en", language="fr")
        == "Unsupported language: fr"
    )
    assert (
        translate("notifications.task_failed.title", locale="en", task_name="Sync")
        == "Sync failed"
    )


def test_translate_falls_back_to_default_language_for_tasks():
    assert (
        translate(
            "tasks.movie_javdb_backfill.name",
            locale="en",
        )
        == "JavDB movie backfill"
    )


def test_translate_missing_key_returns_default_or_key():
    assert translate("missing.key", locale="en", default="Fallback") == "Fallback"
    assert translate("missing.key", locale="en") == "missing.key"


def test_translate_does_not_fall_back_to_chinese_for_errors():
    # zh-CN 的错误不使用 catalog；错误 handler 会显式走状态兜底。
    assert has_translation("errors.invalid_language", "en") is True
    assert has_translation("errors.invalid_language", "zh-CN") is False
    assert (
        translate(
            "errors.invalid_language",
            locale="zh-CN",
            fallback_to_default=False,
            language="fr",
        )
        == "errors.invalid_language"
    )
