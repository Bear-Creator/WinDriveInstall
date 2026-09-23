"""Вспомогательные утилиты: сеть, даты, версии и парсинг AppX."""

import datetime as dt
import json
import re
from pathlib import Path

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def get_http_session() -> requests.Session:
    """Возвращает преднастроенную сессию requests с User-Agent."""
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return session


def download_file(
    url: str, dest_path: Path, session: requests.Session | None = None
) -> Path:
    """Скачивает файл по прямой ссылке с прогрессом."""
    session = session or get_http_session()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(
                        f"\r  Скачивание {dest_path.name}: {pct:5.1f}%",
                        end="",
                    )
        print()
    return dest_path


def parse_catalog_date(date_str: str) -> dt.date:
    """Парсит дату обновления из таблицы каталога (формат MM/DD/YYYY)."""
    if not date_str or date_str.lower() in ("n/a"):
        return dt.date.min
    return dt.date.strptime(date_str.strip(), "%m/%d/%Y")


def sanitize_folder_name(name: str) -> str:
    """Убирает недопустимые для Windows символы из имени папки."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    return cleaned[:150] or "unknown"


def version_tuple(v: str) -> tuple[int, ...]:
    """Разбивает строку версии на числовой кортеж для сравнения."""
    if not v:
        return (0,)
    numbers = [
        int(m.group()) for p in re.split(r"[.\-,]", v) if (m := re.match(r"\d+", p))
    ]
    return tuple(numbers) if numbers else (0,)


def is_newer(
    remote_version: str,
    local_version: str,
    remote_date: dt.date = dt.date.min,
    local_date: dt.date = dt.date.min,
) -> bool:
    """Проверяет, новее ли удаленная версия драйвера локальной."""
    r_ver = remote_version.strip()
    l_ver = local_version.strip()
    if (
        r_ver
        and r_ver.lower() not in ("n/a", "none", "")
        and l_ver
        and l_ver.lower() not in ("n/a", "none", "")
    ):
        try:
            r_tup = version_tuple(r_ver)
            l_tup = version_tuple(l_ver)
            if r_tup != (0,) and l_tup != (0,):
                return r_tup > l_tup
        except (ValueError, TypeError):
            pass

    if dt.date.min not in (remote_date, local_date):
        return remote_date > local_date

    return False


def truncate(text: str, max_len: int) -> str:
    """Обрезает строку до max_len символов с многоточием при превышении."""
    truncated = text if len(text) <= max_len else text[: max_len - 3] + "..."
    return f"{truncated:<{max_len}}"


APPX_SKIP_FAMILY_PREFIXES = (
    # Фреймворки и зависимости (скачиваются автоматически бандлами)
    "microsoft.vclibs",
    "microsoft.net.native",
    "microsoft.ui.xaml",
    "microsoft.web",
    "microsoft.cardinal",
    "microsoft.windowsappruntime",
    # Внутренние компоненты и стандартные системные утилиты Windows
    "microsoft.aad.",
    "microsoft.accountscontrol",
    "microsoft.asynctextservice",
    "microsoft.bioenrollment",
    "microsoft.creddialoghost",
    "microsoft.win32webviewhost",
    "microsoft.xboxgamecallableui",
    "microsoft.sechealthui",
    "microsoft.applicationcompatibilityenhancements",
    "microsoft.windowscalculator",
    "microsoft.storepurchaseapp",
    "microsoft.ecapp",
    "microsoft.lockapp",
    "microsoft.winget.",
    "microsoft.desktopappinstaller",
    "microsoft.windowsstore",
    "microsoftcorporationii.windowssubsystemforlinux",
    # Сторонний софт общего назначения и архиваторы
    "notepadplusplus",
    "40174mourinaruto.nanazip",
)

INTERNAL_OS_PUBLISHERS = (
    # Внутренний сертификат сборки компонентов Windows (не публикуются в Store)
    "_cw5n1h2txyewy",
)


def _is_ignored_appx(name: str, family: str) -> bool:
    """Отсекает системные компоненты, фреймворки и нерелевантный софт."""
    f_lower = family.lower()
    n_lower = name.lower()
    if f_lower.startswith(APPX_SKIP_FAMILY_PREFIXES):
        return True
    if n_lower.startswith(APPX_SKIP_FAMILY_PREFIXES):
        return True
    if any(f_lower.endswith(pub) for pub in INTERNAL_OS_PUBLISHERS):
        return True
    # Пропуск GUID-имен вроде 1527c705-839a-4832-9118-54d4bd6a0c89
    guid_re = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    if re.fullmatch(guid_re, n_lower):
        return True
    if re.fullmatch(guid_re, f_lower.split("_")[0]):
        return True
    return False


def parse_appx_json(raw: str) -> list[tuple[str, str]]:
    """Разбирает вывод Get-AppxPackage (ConvertTo-Json) в [(имя, family)].

    Принимает и единичный объект, и массив. Фреймворки, системные компоненты
    Windows и нерелевантный софт отбрасываются.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    apps: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("IsFramework"):
            continue
        name = str(item.get("Name") or "").strip()
        family = str(item.get("PackageFamilyName") or "").strip()
        if not name or not family:
            continue
        if _is_ignored_appx(name, family):
            continue
        apps.append((name, family))
    return apps


def parse_appx_versions(raw: str) -> dict[str, str]:
    """Разбирает вывод Get-AppxPackage в словарь {family_or_name: version}.

    Фреймворки отбрасываются; имена приводятся к нижнему регистру для
    регистронезависимого поиска.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    items = data if isinstance(data, list) else [data]
    versions: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("IsFramework"):
            continue
        name = str(item.get("Name") or "").strip().lower()
        family = str(item.get("PackageFamilyName") or "").strip().lower()
        version = str(item.get("Version") or "").strip()
        if not version:
            continue
        if family:
            versions[family] = version
        if name:
            versions[name] = version
    return versions


_STORE_BUNDLE_MAJOR_OFFSET = 1000
_SEMVER_PARTS_MIN = 4


def is_app_version_up_to_date(
    installed_ver: str | None, candidate_ver: str | None
) -> bool:
    """Проверяет, установлена ли в системе версия не старее версии-кандидата.

    Учитывает стандартное сравнение версий, а также соглашение Store по
    смещению major-версии в бандлах (например, 3001.24.11911.0 vs 1.24.11911.0).
    """
    if not installed_ver or not candidate_ver or candidate_ver == "0":
        return False
    inst = version_tuple(installed_ver)
    cand = version_tuple(candidate_ver)
    if inst >= cand:
        return True
    if (
        cand
        and cand[0] >= _STORE_BUNDLE_MAJOR_OFFSET
        and inst
        and inst[0] < _STORE_BUNDLE_MAJOR_OFFSET
        and len(cand) >= _SEMVER_PARTS_MIN
        and len(inst) >= _SEMVER_PARTS_MIN
    ):
        norm_cand = (cand[0] % _STORE_BUNDLE_MAJOR_OFFSET, *cand[1:])
        if inst >= norm_cand:
            return True
    return False
