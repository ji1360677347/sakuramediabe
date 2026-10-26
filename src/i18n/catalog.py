"""JSON 语言包加载与翻译。

语言包是纯数据文件，key 使用点分命名空间（如 ``errors.job_not_found``、
``tasks.actor_subscription_sync.name``）。这里只做查表、复数选择和插值，
不引入 gettext/Babel 的编译流程。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from loguru import logger

from src.i18n.locale import DEFAULT_LANGUAGE, current_language, normalize_language

CATALOG_DIR = Path(__file__).with_name("catalogs")


@cache
def _load_catalog(language: str) -> dict[str, Any]:
    path = CATALOG_DIR / f"{language}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("i18n catalog missing: {}", path)
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("i18n catalog invalid: {}", path)
        return {}
    if not isinstance(data, dict):
        logger.error("i18n catalog root must be an object: {}", path)
        return {}
    return data


def _select_value(value: Any, params: dict[str, Any]) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if params.get("count") == 1:
            selected = value.get("one", value.get("other"))
        else:
            selected = value.get("other", value.get("one"))
        return selected if isinstance(selected, str) else None
    return None


def has_translation(key: str, locale: str | None = None) -> bool:
    resolved = normalize_language(locale) or current_language()
    return key in _load_catalog(resolved)


def translate_from_catalogs(
    catalogs: Mapping[str, Mapping[str, Any]],
    key: str,
    *,
    locale: str | None = None,
    default: str | None = None,
    fallback_to_default: bool = True,
    **params: Any,
) -> str:
    """从一组已加载的 catalog 渲染 key。

    供宿主全局 catalog 与插件自带 catalog 共用；catalog 的 key 是平铺点分
    字符串，值可以是普通模板或 ``{"one": ..., "other": ...}``。
    """
    resolved = normalize_language(locale) or current_language()
    value = catalogs.get(resolved, {}).get(key)
    if value is None and fallback_to_default and resolved != DEFAULT_LANGUAGE:
        value = catalogs.get(DEFAULT_LANGUAGE, {}).get(key)
    selected = _select_value(value, params)
    if selected is None:
        return default if default is not None else key
    try:
        return selected.format(**params)
    except (KeyError, IndexError, ValueError):
        logger.warning(
            "i18n interpolation failed key={} language={}", key, resolved
        )
        return default if default is not None else selected


def translate(
    key: str,
    *,
    locale: str | None = None,
    default: str | None = None,
    fallback_to_default: bool = True,
    **params: Any,
) -> str:
    """按当前/指定语言渲染宿主 catalog 的 key。

    ``locale`` 是目标语言参数，刻意不叫 ``language``，避免和消息里的
    ``{language}`` 占位符冲突。
    - 缺失译文时默认回退到 zh-CN；错误文案会传 ``fallback_to_default=False``，
      由调用方决定英文通用兜底，避免把中文透给英文界面。
    - 插值失败不抛异常，返回 default 或原始模板，保证错误 handler 不会二次 500。
    """
    resolved = normalize_language(locale) or current_language()
    catalogs: dict[str, Mapping[str, Any]] = {resolved: _load_catalog(resolved)}
    if fallback_to_default and resolved != DEFAULT_LANGUAGE:
        catalogs[DEFAULT_LANGUAGE] = _load_catalog(DEFAULT_LANGUAGE)
    return translate_from_catalogs(
        catalogs,
        key,
        locale=resolved,
        default=default,
        fallback_to_default=fallback_to_default,
        **params,
    )
