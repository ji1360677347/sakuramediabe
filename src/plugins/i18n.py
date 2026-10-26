"""插件可选多语言文本的宿主侧解析。

插件可以在 manifest / JobDefinition / settings JSON Schema 中提供 ``*_i18n``
字典；宿主按当前语言替换展示字段，缺失时保留插件原文。这里不缓存本地化后的
schema，避免污染 Pydantic 可能复用的原始 ``model_json_schema()`` 结果。
"""

from __future__ import annotations

from typing import Any

from src.i18n.locale import DEFAULT_LANGUAGE, current_language, normalize_language


def resolve_text(default: str, translations: dict[str, str] | None) -> str:
    if not translations:
        return default
    locale = normalize_language(current_language()) or DEFAULT_LANGUAGE
    return (
        translations.get(locale)
        or translations.get(DEFAULT_LANGUAGE)
        or default
    )


def localize_json_schema(
    schema: dict[str, Any] | None,
    *,
    locale: str | None = None,
) -> dict[str, Any] | None:
    """递归把 title_i18n/description_i18n 解析成 title/description。

    返回新 dict/list，不修改传入的原始 schema。
    """
    if schema is None:
        return None
    return _localize_node(schema, normalize_language(locale) or current_language())


def _localize_node(node: Any, locale: str) -> Any:
    if isinstance(node, list):
        return [_localize_node(item, locale) for item in node]
    if not isinstance(node, dict):
        return node

    localized = {
        key: _localize_node(value, locale)
        for key, value in node.items()
        if key not in {"title_i18n", "description_i18n"}
    }
    for field in ("title", "description"):
        translations = node.get(f"{field}_i18n")
        if isinstance(translations, dict):
            localized[field] = (
                translations.get(locale)
                or translations.get(DEFAULT_LANGUAGE)
                or node.get(field, "")
            )
    return localized
