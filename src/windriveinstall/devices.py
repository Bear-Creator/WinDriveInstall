"""Получение информации об установленных устройствах и AppX-пакетах (WMI)."""

import csv
import datetime as dt
import functools
import logging
import subprocess

import win32com.client

from .models import LocalDevice
from .utils import parse_appx_json, parse_appx_versions

log = logging.getLogger(__name__)

APPX_PS_COMMAND = (
    "Get-AppxPackage | "
    "Where-Object { -not $_.IsFramework } | "
    "Select-Object Name, PackageFamilyName, Version | "
    "ConvertTo-Json -Compress"
)


def parse_wmi_date(wmi_date) -> dt.date | None:
    """Парсит дату драйвера из формата WMI (YYYYMMDD...)."""
    if not wmi_date:
        return None
    try:
        return dt.date.strptime(str(wmi_date).strip()[:8], "%Y%m%d")
    except (ValueError, TypeError):
        return None


def get_local_devices() -> list[LocalDevice]:
    """Собирает PnP-устройства через WMI.

    Собирает все варианты HWID (от специфичного к общему — нужно для поиска в
    каталоге), версию драйвера, вендора.
    """
    wmi_service: win32com.client.CDispatch = win32com.client.GetObject(
        "winmgmts:\\\\.\\root\\cimv2"
    )
    drivers = list(wmi_service.ExecQuery("SELECT * FROM Win32_PnPSignedDriver"))

    devices = []
    for d in drivers:
        raw_ids = d.HardWareID
        if not raw_ids:
            continue
        candidates = list(raw_ids) if isinstance(raw_ids, (list, tuple)) else [raw_ids]
        try:
            compat = d.CompatibleID
            if compat:
                candidates += (
                    list(compat) if isinstance(compat, (list, tuple)) else [compat]
                )
        except (AttributeError, TypeError):
            pass
        if not candidates:
            continue
        devices.append(
            LocalDevice(
                name=getattr(d, "DeviceName", None) or "Unknown device",
                hwid=candidates[0],
                driver_version=getattr(d, "DriverVersion", None) or "[IP]",
                manufacturer=getattr(d, "Manufacturer", None) or "Unknown",
                device_class=getattr(d, "DeviceClass", None) or "Unknown",
                driver_date=parse_wmi_date(getattr(d, "DriverDate", None))
                or dt.date.min,
                hwid_candidates=candidates,
            )
        )

    log.debug(f"Найдено устройств: {len(devices)}")
    return devices


@functools.cache
def _get_installed_appx_raw() -> str:
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                APPX_PS_COMMAND,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Не удалось получить AppX-приложения: %s", e)
        return ""
    if result.returncode != 0:
        log.warning(
            "Get-AppxPackage завершился с кодом %s: %s",
            result.returncode,
            result.stderr.strip(),
        )
        return ""
    return result.stdout


def get_installed_appx() -> list[tuple[str, str]]:
    """Список установленных UWP-приложений текущего пользователя.

    Только Windows: вызывает powershell Get-AppxPackage (без -AllUsers, т.е.
    пакеты текущего пользователя). Возвращает [(имя, PackageFamilyName)];
    при ошибке PowerShell — пустой список, чтобы сборка не падала.
    """
    raw = _get_installed_appx_raw()
    return parse_appx_json(raw) if raw else []


def get_installed_appx_versions() -> dict[str, str]:
    """Словарь версий установленных UWP-приложений: {family_or_name: version}."""
    raw = _get_installed_appx_raw()
    return parse_appx_versions(raw) if raw else {}


def save_devices_to_csv(devices: list[LocalDevice], path: str) -> None:
    """Сохраняет список устройств в CSV — удобно для ручной проверки."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        fields = ["name", "hwid", "driver_version", "manufacturer", "device_class"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for d in devices:
            writer.writerow({k: getattr(d, k) for k in fields})
    print(f"Список устройств сохранён: {path}")
