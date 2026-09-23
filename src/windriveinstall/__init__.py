"""Гибридный помощник обновления драйверов и сборки OEM-пакетов."""

__version__ = "0.1.0"

from .models import CatalogEntry, DownloadResult, LocalDevice

__all__ = [
    "CatalogEntry",
    "DownloadResult",
    "LocalDevice",
    "find_updates",
    "get_local_devices",
]


def find_updates(*args, **kwargs):
    """Ленивый вход в .catalog: тяжёлые зависимости тянем только по необходимости."""
    from .catalog import find_updates  # noqa: PLC0415

    return find_updates(*args, **kwargs)


def get_local_devices():
    """Ленивый вход в .devices (win32com доступен только в ОС Windows)."""
    from .devices import get_local_devices  # noqa: PLC0415

    return get_local_devices()
