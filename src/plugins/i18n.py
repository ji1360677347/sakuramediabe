"""插件可选多语言文本的宿主侧解析。

插件可以在 manifest / JobDefinition / settings JSON Schema / ranking /
provider config 中提供 ``*_i18n`` 字典；宿主按当前语言替换展示字段，缺失时
保留插件原文。运行时文本走插件自带的 ``i18n/{locale}.json`` 和
``context.i18n.t()``。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from functools import cache
from pathlib import Path
from typing import Any

from loguru import logger

from src.i18n.catalog import translate_from_catalogs
from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    current_language,
    normalize_language,
)
from src.i18n.text import resolve_text

__all__ = [
    "PluginI18n",
    "localize_config_field",
    "localize_json_schema",
    "localize_provider_bundle",
    "resolve_text",
]


@cache
def _load_plugin_catalog(path: str) -> dict[str, Any]:
    """加载插件语言包；文件不存在是合法的（插件未提供翻译）。"""
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("plugin i18n catalog invalid: {}", file_path)
        return {}
    if not isinstance(data, dict):
        logger.error("plugin i18n catalog root must be an object: {}", file_path)
        return {}
    return data


class PluginI18n:
    """插件运行时文本翻译器；语言由宿主当前请求/任务上下文决定。"""

    def __init__(self, plugin_id: str, root_dir: Path | None = None):
        self.plugin_id = plugin_id
        self.root_dir = Path(root_dir) if root_dir is not None else None

    @property
    def locale(self) -> str:
        return current_language()

    def t(
        self,
        key: str,
        default: str | None = None,
        **params: Any,
    ) -> str:
        return translate_from_catalogs(
            self._catalogs(),
            key,
            default=default,
            **params,
        )

    def _catalogs(self) -> dict[str, dict[str, Any]]:
        if self.root_dir is None:
            return {}
        return {
            locale: _load_plugin_catalog(
                str(self.root_dir / "i18n" / f"{locale}.json")
            )
            for locale in SUPPORTED_LANGUAGES
        }


def localize_provider_bundle(bundle: Any) -> str:
    """解析媒体 Provider 展示名；旧插件没有 display_name_i18n 时回退原文。"""
    return resolve_text(
        getattr(bundle, "display_name", ""),
        getattr(bundle, "display_name_i18n", None),
    )


def localize_config_field(field: Any) -> dict[str, Any]:
    """把 ConfigField 的 *_i18n 解析成响应字段，并移除内部 i18n 键。"""
    data = asdict(field)
    for name in ("label", "description", "hint"):
        translations = data.pop(f"{name}_i18n", None)
        if translations is not None and not isinstance(translations, dict):
            logger.warning(
                "config field i18n must be a dict: field={} name={}",
                data.get("key"),
                name,
            )
            translations = None
        value = data.get(name)
        if value is not None:
            data[name] = resolve_text(value, translations)
        elif translations:
            localized = resolve_text("", translations)
            if localized:
                data[name] = localized
    return data


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
