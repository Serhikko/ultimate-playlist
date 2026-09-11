"""Provider registry. First provider whose `matches(url)` is True owns the link."""

from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING

from .base import (
    DownloadCancelled,
    Provider,
    ProviderError,
    ProviderNotAvailable,
)

if TYPE_CHECKING:
    from ..config import Settings

__all__ = [
    "PROVIDERS",
    "DownloadCancelled",
    "Provider",
    "ProviderError",
    "ProviderNotAvailable",
    "configure",
    "doctor_all",
    "get_provider",
    "normalize_url",
    "provider_by_name",
    "register",
    "run_doctor",
    "unregister",
]

log = logging.getLogger(__name__)
_lock = threading.RLock()

PROVIDERS: list[Provider] = []


def _load_builtin() -> None:
    """Import built-in providers lazily so a broken optional provider never breaks the registry."""
    builtin: list[tuple[str, str]] = [
        ("ultimate_playlist.providers.youtube", "YouTubeProvider"),
        ("ultimate_playlist.providers.spotify", "SpotifyProvider"),
    ]
    import importlib

    for module_name, class_name in builtin:
        try:
            module = importlib.import_module(module_name)
            provider = getattr(module, class_name)()
        except Exception as exc:  # noqa: BLE001
            log.warning("Provider %s unavailable: %s", module_name, exc)
            continue
        if provider_by_name(provider.name) is None:
            PROVIDERS.append(provider)


def register(provider: Provider) -> None:
    """Insert at the front so it wins over built-ins (used by tests and plugins)."""
    with _lock:
        unregister(provider.name)
        PROVIDERS.insert(0, provider)


def unregister(name: str) -> None:
    with _lock:
        PROVIDERS[:] = [p for p in PROVIDERS if p.name != name]


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and "://" not in url and not url.startswith("spotify:"):
        url = "https://" + url
    return url


def get_provider(url: str) -> Provider | None:
    url = normalize_url(url)
    if not url:
        return None
    with _lock:
        for provider in PROVIDERS:
            try:
                if provider.matches(url):
                    return provider
            except Exception as exc:  # noqa: BLE001
                log.warning("%s.matches() failed: %s", provider.name, exc)
    return None


def provider_by_name(name: str) -> Provider | None:
    with _lock:
        for provider in PROVIDERS:
            if provider.name == name:
                return provider
    return None


def configure(settings: Settings) -> None:
    """Hand the live settings to every provider that wants them (``configure(settings)``).

    Providers are constructed without settings by `_load_builtin`; the app and the CLI call this
    once so `resolve()`/`doctor()` honour config.json (ffmpeg_path, js_runtimes, ...).
    """
    with _lock:
        for provider in PROVIDERS:
            hook = getattr(provider, "configure", None)
            if not callable(hook):
                continue
            try:
                hook(settings)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s.configure() failed: %s", provider.name, exc)


def _accepts_argument(func: object) -> bool:
    try:
        params = inspect.signature(func).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    )
    return any(p.kind in positional for p in params)


def run_doctor(provider: Provider, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
    """`provider.doctor()`, passing `settings` when the provider accepts one. Never raises."""
    try:
        if settings is not None and _accepts_argument(provider.doctor):
            return list(provider.doctor(settings))  # type: ignore[call-arg]
        return list(provider.doctor())
    except Exception as exc:  # noqa: BLE001
        display = getattr(provider, "display_name", provider.name)
        return [(False, display, f"doctor() failed: {exc}")]


def doctor_all(settings: Settings | None = None) -> dict[str, list[tuple[bool, str, str]]]:
    result: dict[str, list[tuple[bool, str, str]]] = {}
    with _lock:
        for provider in PROVIDERS:
            result[provider.name] = run_doctor(provider, settings)
    return result


_load_builtin()
