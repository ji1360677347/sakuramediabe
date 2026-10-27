import ast
import json
from pathlib import Path
from string import Formatter

from src.i18n.catalog import CATALOG_DIR, _select_value, translate
from src.i18n.locale import SUPPORTED_LANGUAGES
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY


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


def _api_error_codes() -> set[str]:
    codes: set[str] = set()
    for path in Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name != "ApiError" or len(node.args) < 2:
                continue
            code = node.args[1]
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                codes.add(code.value)
    return codes


def test_every_api_error_code_has_english_translation():
    en = _load_catalog("en.json")
    missing = sorted(
        code for code in _api_error_codes() if f"errors.{code}" not in en
    )
    assert not missing, missing


def test_catalogs_keep_key_and_placeholder_parity():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")

    # zh-CN 是任务/通知的源文案；en 可以多出仅错误使用的 errors.*。
    assert set(zh) <= set(en)
    assert all(key.startswith("errors.") for key in set(en) - set(zh))
    for key in set(zh):
        assert _placeholders(zh[key]) == _placeholders(en[key]), key


def test_every_registered_task_name_has_both_catalogs():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")

    for task_key in TASK_NAME_REGISTRY:
        key = f"tasks.{task_key}.name"
        assert key in zh, task_key
        assert key in en, task_key


def test_host_notification_keys_exist_in_both_catalogs():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")
    required = {
        "notifications.task_failed.title",
        "notifications.task_failed.content",
        "notifications.task_completed.title",
        "notifications.task_completed.content",
        "notifications.new_media.title",
        "notifications.new_media.content",
    }

    assert required <= set(zh)
    assert required <= set(en)


def test_new_media_notification_plural():
    assert (
        translate("notifications.new_media.content", locale="en", count=1)
        == "1 new movie added"
    )
    assert (
        translate("notifications.new_media.content", locale="en", count=3)
        == "3 new movies added"
    )
    assert (
        translate("notifications.new_media.content", locale="zh-CN", count=3)
        == "新增了 3 个影片"
    )


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
    assert "errors.invalid_language" in _load_catalog("en.json")
    assert "errors.invalid_language" not in _load_catalog("zh-CN.json")
    assert (
        translate(
            "errors.invalid_language",
            locale="zh-CN",
            fallback_to_default=False,
            language="fr",
        )
        == "errors.invalid_language"
    )


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_catalog_keys() -> set[str]:
    keys: set[str] = set()
    for path in Path("src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) in {"translate", "translate_for_display"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
            for keyword in node.keywords:
                if keyword.arg in {"text_key", "message_key"}:
                    value = keyword.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        keys.add(value.value)
    return keys


def test_every_literal_catalog_key_exists():
    zh = _load_catalog("zh-CN.json")
    en = _load_catalog("en.json")
    keys = _literal_catalog_keys()
    assert keys
    assert sorted(key for key in keys if key not in en) == []
    # zh-CN 的 errors.* 只覆盖 handler 兜底用的少量 key，其余错误继续用源码文本。
    assert sorted(
        key for key in keys if key not in zh and not key.startswith("errors.")
    ) == []


def test_every_supported_language_has_catalog():
    missing = [
        language
        for language in SUPPORTED_LANGUAGES
        if not (CATALOG_DIR / f"{language}.json").is_file()
    ]
    assert missing == []
