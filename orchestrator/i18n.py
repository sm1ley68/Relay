"""Tiny inline i18n. English is the default; Russian is available.

Usage: ``t("Tasks: {n}", "Задач: {n}").format(n=5)`` — both strings live at the
call site, so there is no separate catalog to keep in sync. Language comes from
``RELAY_LANG`` / config / the ``/lang`` command; default is English.
"""
from __future__ import annotations

import os

_LANG = "en"


def set_lang(lang: str | None) -> None:
    global _LANG
    _LANG = (lang or "en").strip().lower()[:2] or "en"


# resolve an initial default from the environment (config can override later)
set_lang(os.environ.get("RELAY_LANG", "en"))


def current_lang() -> str:
    return _LANG


def t(en: str, ru: str | None = None) -> str:
    """Return the string for the active language (falls back to English)."""
    if _LANG == "ru" and ru is not None:
        return ru
    return en
