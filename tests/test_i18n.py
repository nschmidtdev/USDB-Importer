"""Regression tests for the browser-side internationalization contract."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INDEX = STATIC / "index.html"
RUNTIME = STATIC / "i18n.js"
LOCALES = ("en", "de", "es", "ru")
ATTRIBUTE_PATTERNS = (
    r'data-i18n="([^"]+)"',
    r'data-i18n-html="([^"]+)"',
    r'data-i18n-placeholder="([^"]+)"',
    r'data-i18n-title="([^"]+)"',
)


def _catalog(locale: str) -> dict[str, str]:
    path = STATIC / "i18n" / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_locale_catalogs_exist_and_have_identical_keys():
    catalogs = {locale: _catalog(locale) for locale in LOCALES}
    expected = set(catalogs["en"])

    assert len(expected) >= 100
    assert all(catalogs[locale] for locale in LOCALES)
    for locale, catalog in catalogs.items():
        assert set(catalog) == expected, locale
        assert all(isinstance(value, str) and value.strip() for value in catalog.values())


def test_markup_translation_keys_exist_in_every_catalog():
    html = INDEX.read_text(encoding="utf-8")
    referenced = {
        match
        for pattern in ATTRIBUTE_PATTERNS
        for match in re.findall(pattern, html)
    }
    assert len(referenced) >= 60

    for locale in LOCALES:
        missing = referenced - set(_catalog(locale))
        assert not missing, (locale, sorted(missing))


def test_runtime_contract_and_language_picker_are_present():
    html = INDEX.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")

    assert '<script src="/static/i18n.js"></script>' in html
    assert 'id="languageSelect"' in html
    assert 'value="auto"' in html
    assert 'data-i18n="settings.language"' in html
    assert "usi_language" in runtime
    assert "document.documentElement.lang" in runtime
    assert "navigator.languages" in runtime
    assert "localStorage" in runtime
    assert "data-i18n-placeholder" in runtime
    assert "data-i18n-title" in runtime


def test_dynamic_translation_calls_reference_known_keys():
    html = INDEX.read_text(encoding="utf-8")
    called = set(re.findall(r"\bt\(['\"]([^'\"]+)['\"]", html))
    assert len(called) >= 40

    missing = called - set(_catalog("en"))
    assert not missing, sorted(missing)


def test_catalogs_preserve_native_utf8_scripts():
    assert "Español" in _catalog("es").values()
    assert any(re.search(r"[А-Яа-яЁё]", value) for value in _catalog("ru").values())
    assert any(re.search(r"[äöüÄÖÜß]", value) for value in _catalog("de").values())


def test_dynamic_placeholders_match_across_locales():
    catalogs = {locale: _catalog(locale) for locale in LOCALES}
    for key, english in catalogs["en"].items():
        expected = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", english))
        for locale in LOCALES[1:]:
            actual = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", catalogs[locale][key]))
            assert actual == expected, (locale, key, expected, actual)
