import logging
import re

import requests
from bs4 import BeautifulSoup

from .models import CatalogEntry, LocalDevice
from .utils import get_http_session, is_newer, parse_catalog_date, version_tuple

log = logging.getLogger(__name__)


CATALOG_SEARCH_URL = "https://www.catalog.update.microsoft.com/Search.aspx"
CATALOG_DOWNLOAD_DIALOG_URL = (
    "https://www.catalog.update.microsoft.com/DownloadDialog.aspx"
)
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def search_catalog(
    query: str, session: requests.Session | None = None
) -> list[CatalogEntry]:
    """Ищет обновления в каталоге по строке запроса (обычно HWID)."""
    session = session or get_http_session()

    resp = session.get(CATALOG_SEARCH_URL, params={"q": query}, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", {"id": "ctl00_catalogBody_updateMatches"})
    entries: list[CatalogEntry] = []

    if table is None:
        log.debug(f"Таблица результатов не найдена для запроса '{query}'.")
        return entries

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        try:
            # id строки иногда имеет вид "{GUID}_R6" (когда одно обновление
            # совпало с несколькими результатами поиска), а кнопка "Download"
            # внутри строки всегда имеет id = чистый GUID без суффикса —
            # нормализуем сразу, иначе клик по нему попадёт в саму строку,
            # а не в кнопку.
            raw_id = str(row.get("id", "")).strip()
            match = GUID_RE.match(raw_id)
            update_id = match.group(0) if match else raw_id

            entries.append(
                CatalogEntry(
                    update_id=update_id,
                    title=cells[1].get_text(strip=True),
                    products=cells[2].get_text(strip=True),
                    classification=cells[3].get_text(strip=True),
                    last_updated=parse_catalog_date(cells[4].get_text(strip=True)),
                    version=cells[5].get_text(strip=True),
                    size=cells[6].get_text(strip=True),
                )
            )
        except (IndexError, AttributeError, ValueError) as e:
            log.debug(f"Не удалось разобрать строку таблицы: {e}")

    log.debug(f"По запросу '{query}' найдено записей: {len(entries)}")
    return entries


def search_catalog_with_fallback(
    hwid_candidates: list[str], session: requests.Session | None = None
) -> tuple[str, list[CatalogEntry]]:
    """Пробует HWID от специфичного к общему, возвращает первый непустой результат."""
    session = session or get_http_session()
    for hwid in hwid_candidates:
        entries = search_catalog(hwid, session=session)
        if entries:
            log.debug(f"Совпадение найдено по HWID: {hwid}")
            return hwid, entries
    return "", []


def find_updates(
    devices: list[LocalDevice], session: requests.Session | None = None
) -> list[tuple[LocalDevice, CatalogEntry, str]]:
    """Ищет обновления для всех устройств. Возвращает [(device, лучшая_запись, matched_hwid)]."""
    session = session or get_http_session()
    candidates = []
    for dev in devices:
        matched_hwid, entries = search_catalog_with_fallback(
            dev.hwid_candidates, session=session
        )
        if not entries:
            continue

        best = max(
            entries,
            key=lambda e: (version_tuple(e.version), e.last_updated),
        )
        if is_newer(
            best.version,
            dev.driver_version,
            remote_date=best.last_updated,
            local_date=dev.driver_date,
        ):
            candidates.append((dev, best, matched_hwid))
    return candidates
