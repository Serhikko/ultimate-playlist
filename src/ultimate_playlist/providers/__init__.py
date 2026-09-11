"""Provider registry. First provider whose `matches(url)` is True owns the link."""

from __future__ import annotations

import logging
import threading

from .base import (
    DownloadCancelled,
    Provider,
    ProviderError,
    ProviderNotAvailable,
)

__all__ = [
    "PROVIDERS",
    "DownloadCancelled",
    "Provider",
    "ProviderError",
    "ProviderNotAvailable",
    "doctor_all",
    "get_provider",
    "normalize_url",
    "provider_by_name",
    "register",
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


def doctor_all() -> dict[str, list[tuple[bool, str, str]]]:
    result: dict[str, list[tuple[bool, str, str]]] = {}
    with _lock:
        for provider in PROVIDERS:
            try:
                result[provider.name] = list(provider.doctor())
            except Exception as exc:  # noqa: BLE001
                result[provider.name] = [(False, provider.display_name, f"doctor() failed: {exc}")]
    return result


_load_builtin()
