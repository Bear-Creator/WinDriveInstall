import csv
import datetime as dt
import logging

import win32com.client

from .models import LocalDevice

log = logging.getLogger(__name__)


def parse_wmi_date(wmi_date) -> dt.date | None:
    if not wmi_date:
        return None
    try:
        return dt.date.strptime(str(wmi_date).strip()[:8], "%Y%m%d")
    except ValueError, TypeError:
        return None


def get_local_devices() -> list[LocalDevice]:
    """
    Собирает PnP-устройства через WMI: все варианты HWID (от специфичного к
    общему — нужно для поиска в каталоге), версию драйвера, вендора.
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
        except AttributeError, TypeError:
            pass
        if not candidates:
            continue
        devices.append(
            LocalDevice(
                name=getattr(d, "DeviceName", None) or "Unknown device",
                hwid=candidates[0],
                driver_version=getattr(d, "DriverVersion", None) or "0.0.0.0",
                manufacturer=getattr(d, "Manufacturer", None) or "Unknown",
                device_class=getattr(d, "DeviceClass", None) or "Unknown",
                driver_date=parse_wmi_date(getattr(d, "DriverDate", None))
                or dt.date.min,
                hwid_candidates=candidates,
            )
        )

    log.debug(f"Найдено устройств: {len(devices)}")
    return devices


def save_devices_to_csv(devices: list[LocalDevice], path: str) -> None:
    """Сохраняет список устройств в CSV — удобно для ручной проверки."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        fields = ["name", "hwid", "driver_version", "manufacturer", "device_class"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for d in devices:
            writer.writerow({k: getattr(d, k) for k in fields})
    print(f"Список устройств сохранён: {path}")
