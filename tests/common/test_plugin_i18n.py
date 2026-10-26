import json
from contextlib import contextmanager

from src.config.config import settings
from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.plugins.i18n import localize_json_schema, resolve_text
from src.plugins.manager import PluginManager
from src.plugins.manifest import load_manifest_from_dict


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def test_resolve_text_uses_locale_then_default_then_original():
    with _language("en"):
        assert resolve_text("中文", {"en": "English", "zh-CN": "中文"}) == "English"
        assert resolve_text("中文", {"zh-CN": "中文"}) == "中文"
        assert resolve_text("中文", {}) == "中文"


def test_localize_json_schema_does_not_mutate_source():
    schema = {
        "type": "object",
        "title_i18n": {"en": "Settings", "zh-CN": "设置"},
        "properties": {
            "retry_days": {
                "type": "integer",
                "title": "重试间隔",
                "title_i18n": {"en": "Retry interval"},
                "description": "天数",
                "description_i18n": {"en": "Days"},
            }
        },
    }

    with _language("en"):
        localized = localize_json_schema(schema)

    assert localized["title"] == "Settings"
    assert localized["properties"]["retry_days"]["title"] == "Retry interval"
    assert localized["properties"]["retry_days"]["description"] == "Days"
    assert "title_i18n" not in localized
    assert "title_i18n" not in localized["properties"]["retry_days"]
    # 原始 schema 保持不变，避免污染 Pydantic 缓存。
    assert schema["title_i18n"] == {"en": "Settings", "zh-CN": "设置"}
    assert schema["properties"]["retry_days"]["title"] == "重试间隔"


def test_manifest_accepts_optional_display_name_i18n():
    manifest = load_manifest_from_dict(
        {
            "plugin_id": "demo",
            "display_name": "演示插件",
            "display_name_i18n": {"en": "Demo plugin"},
            "version": "1.0.0",
            "host_api_version": 11,
        }
    )

    assert manifest.display_name_i18n == {"en": "Demo plugin"}


def test_plugin_manager_list_plugins_localizes_display_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    settings.plugins.enabled = []
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo",
                "display_name": "演示插件",
                "display_name_i18n": {"en": "Demo plugin"},
                "version": "1.0.0",
                "host_api_version": 11,
            }
        ),
        encoding="utf-8",
    )

    with _language("en"):
        plugins = PluginManager(root_dir=tmp_path).list_plugins()
    assert plugins[0]["display_name"] == "Demo plugin"

    with _language("zh-CN"):
        plugins = PluginManager(root_dir=tmp_path).list_plugins()
    assert plugins[0]["display_name"] == "演示插件"
