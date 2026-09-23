"""Скачивание свежих UWP-пакетов приложений из Microsoft Store.

Источник ссылок — публичный сервис store.rg-adguard.net: по product-ссылке,
ProductId или PackageFamilyName он отдаёт прямые CDN-ссылки на пакеты
(.appx/.appxbundle/.msixbundle) и зависимости. Скачивание идёт привычным
requests-путём (download_file) в структуру, которую уже понимает сборка:
Cache/<Устройство>/Apps/<Приложение>/<бандл> + Dependencies/.
"""

import json
import logging
import re
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .builder import (
    DEPS_DIR_NAME,
    DEPS_META_NAME,
    UWP_BUNDLE_EXTS,
    _make_component,
    _read_template_manifest,
    get_bundle_inner_version,
    parse_package_family,
    read_device_meta,
    scan_oem_components,
)
from .models import UwpDownloadResult
from .utils import (
    download_file,
    get_http_session,
    is_app_version_up_to_date,
    sanitize_folder_name,
    version_tuple,
)

log = logging.getLogger(__name__)

STORE_SEARCH_API = "https://storeedgefd.dsx.mp.microsoft.com/v9.0/search"
_STORE_EDGE_HEADERS = {
    "User-Agent": "WindowsStore/22106.1401.2.0",
    "OSIsGenuine": "True",
    "OSIsSMode": "False",
}
ADGUARD_API_URL = "https://store.rg-adguard.net/api/GetFiles"
CDN_HOSTS = frozenset(
    {
        "tlu.dl.delivery.mp.microsoft.com",
        "download.microsoft.com",
        "download.windowsupdate.com",
        "storeedgefd.dsx.mp.microsoft.com",
    }
)
# Браузерные заголовки для обхода Cloudflare-блокировки rg-adguard: POST без
# Origin/Referer/X-Requested-With сервис отвечает 403 "Just a moment...".
_STORE_COMMON_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://store.rg-adguard.net/",
    "Origin": "https://store.rg-adguard.net",
    "X-Requested-With": "XMLHttpRequest",
}
_STORE_POST_HEADERS = {
    **_STORE_COMMON_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
_STORE_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_STORE_RETRIES = 3
QUERY_STATE_NAME = ".apps-query-state.json"
_FAIL_TTL_ERROR_SECONDS = 12 * 3600
_FAIL_TTL_NOT_FOUND_SECONDS = 7 * 24 * 3600
DEPENDENCY_PREFIXES = (
    "microsoft.vclibs",
    "microsoft.net.native",
    "microsoft.ui.xaml",
    "microsoft.web",
    "microsoft.cardinal",
    "microsoft.windowsappruntime",
)
_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,4}")
_FAMILY_RE = re.compile(r"^[a-z0-9._-]+_[a-z0-9]+$", re.IGNORECASE)
_PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9]{12}$")
_ARCH_TOKENS = frozenset({"neutral", "x64", "x86", "arm", "arm64"})
NEUTRAL_ARCH = "neutral"


def _package_version(filename: str) -> str:
    """Достаёт версию из имени пакета (первый токен-версия после названия)."""
    for token in filename.split("_"):
        if _VERSION_RE.fullmatch(token):
            return token
    return "0"


def package_arch(filename: str) -> str:
    """Определяет архитектуру пакета по токену имени (по умолчанию neutral)."""
    for token in filename.split("_"):
        if token in _ARCH_TOKENS:
            return token
    return NEUTRAL_ARCH


def _arch_rank(filename: str, arch: str = "x64") -> int:
    """Ранг архитектуры пакета: neutral, затем целевая, затем всё прочее."""
    pkg = package_arch(filename)
    if pkg == NEUTRAL_ARCH:
        return 0
    if pkg == arch.lower():
        return 1
    return 2


def _ext_rank(filename: str) -> int:
    order = {".msixbundle": 0, ".appxbundle": 1, ".msix": 2, ".appx": 3}
    suffix = Path(filename).suffix.lower()
    return order.get(suffix, 99)


def parse_adguard_links(html: str) -> list[tuple[str, str]]:
    """Собирает (имя, url) прямых пакетов из HTML-ответа store.rg-adguard.net."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        if urlparse(href).netloc not in CDN_HOSTS:
            continue
        name = anchor.get_text(strip=True) or Path(urlparse(href).path).name
        if ".encrypted" in name.lower():
            continue
        if Path(name).suffix.lower() not in UWP_BUNDLE_EXTS:
            continue
        links.append((name, href))
    return links


def _is_dependency_family(family: str) -> bool:
    return any(family.startswith(prefix) for prefix in DEPENDENCY_PREFIXES)


def pick_uwp_packages(
    links: list[tuple[str, str]],
    known_family: str | None = None,
    arch: str = "x64",
) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    """Выбирает главный бандл приложения и список зависимостей.

    Главный — пакет семейства known_family (или выведенного семейства) с
    максимальной версией; предпочтение бандлам и neutral-архитектуре, затем
    целевой (arch). Зависимости — все пакеты из других семейств, но только
    neutral-архитектуры и целевой arch (строгий x64: x86/arm/arm64 отбрасываются).
    """
    target = arch.lower()
    by_family: dict[str, list[tuple[str, str]]] = {}
    for item in links:
        family = parse_package_family(item[0])
        if family is None:
            continue
        by_family.setdefault(family, []).append(item)

    if not by_family:
        return None, []

    if known_family and known_family.lower() in by_family:
        main_family = known_family.lower()
    else:
        best_by_family = {
            fam: _best_bundle(items, arch=target) for fam, items in by_family.items()
        }
        main_family = max(
            best_by_family,
            key=lambda fam: (
                not _is_dependency_family(fam),
                version_tuple(_package_version(best_by_family[fam][0])),
                -_ext_rank(best_by_family[fam][0]),
            ),
        )
    main = _best_bundle(by_family[main_family], arch=target)
    deps = [
        item
        for fam, items in by_family.items()
        for item in items
        if fam != main_family and package_arch(item[0]) in (NEUTRAL_ARCH, target)
    ]
    return main, deps


def _best_bundle(
    items: list[tuple[str, str]], arch: str = "x64"
) -> tuple[str, str]:
    """Возвращает лучший пакет семейства: новее, бандл, neutral/целевая arch."""
    target = arch.lower()
    return max(
        items,
        key=lambda item: (
            version_tuple(_package_version(item[0])),
            Path(item[0]).suffix.lower() in (".msixbundle", ".appxbundle"),
            -_arch_rank(item[0], target),
            -_ext_rank(item[0]),
        ),
    )


def fetch_store_files(
    target: str,
    ring: str = "Retail",
    session: requests.Session | None = None,
) -> list[tuple[str, str]] | None:
    """Получает прямые ссылки на пакеты приложения из rg-adguard.

    target — продукт-ссылка, ProductId или PackageFamilyName. None, если пакеты
    не получены (приложение отсутствует или изменился формат сервиса).
    """
    session = session or get_http_session()
    pid = product_id_from(target)
    if pid is not None:
        query_type = "ProductId"
        query_val = pid
    elif "://" in target:
        query_type = "ProductUrl"
        query_val = target
    else:
        query_type = "PackageFamilyName"
        query_val = target

    session.get(
        "https://store.rg-adguard.net/",
        headers=_STORE_COMMON_HEADERS,
        timeout=30,
    )
    payload = {"type": query_type, "url": query_val, "ring": ring, "lang": "en-US"}
    for attempt in range(_STORE_RETRIES + 1):
        try:
            resp = session.post(
                ADGUARD_API_URL,
                data=payload,
                headers=_STORE_POST_HEADERS,
                timeout=60,
            )
        except requests.RequestException:
            if attempt >= _STORE_RETRIES:
                raise
            log.warning(
                "rg-adguard: сетевой сбой (попытка %d из %d), повтор через %d с",
                attempt + 1,
                _STORE_RETRIES + 1,
                2 << attempt,
            )
            time.sleep(2 << attempt)
            continue
        if resp.status_code not in _STORE_RETRY_STATUSES:
            break
        log.warning(
            "rg-adguard: HTTP %s (попытка %d из %d), повтор через %d с",
            resp.status_code,
            attempt + 1,
            _STORE_RETRIES + 1,
            2 << attempt,
        )
        time.sleep(2 << attempt)
    resp.raise_for_status()
    links = parse_adguard_links(resp.text)
    if not links:
        log.debug("rg-adguard не вернул пакеты для: %s", target)
        return None
    return links


def resolve_product_url(
    name: str, session: requests.Session | None = None
) -> str | None:
    """Ищет приложение через StoreEdgeFD API (как в Raven) и возвращает ссылку."""
    session = session or get_http_session()
    params = {
        "query": name,
        "market": "US",
        "locale": "en-US",
        "deviceFamily": "Windows.Desktop",
    }
    try:
        resp = session.get(
            STORE_SEARCH_API,
            params=params,
            headers=_STORE_EDGE_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    try:
        data = resp.json()
        if isinstance(data, dict):
            results = (data.get("Payload") or {}).get("SearchResults") or []
            if results and isinstance(results, list):
                pid = results[0].get("ProductId")
                if pid:
                    return f"https://apps.microsoft.com/detail/{pid}"
    except Exception:
        pass

    match = re.search(r"/detail/[^/?#]+/([A-Za-z0-9]{12})", getattr(resp, "text", ""))
    if match is not None:
        return (
            f"https://apps.microsoft.com/detail/"
            f"{match.group(0).split('/')[2]}/{match.group(1)}"
        )
    return None


_STORE_EDGE_API = "https://storeedgefd.dsx.mp.microsoft.com/v9.0/products/{}"
_STORE_EDGE_PARAMS = {
    "market": "US",
    "locale": "en-US",
    "deviceFamily": "Windows.Desktop",
}


def resolve_store_product(
    product_id: str, session: requests.Session | None = None
) -> tuple[str, str] | None:
    """Резолвит Store ID в (название, PackageFamilyName) через Storefront API.

    В отличие от страницы apps.microsoft.com, storeedgefd отвечает JSON без JS.
    None, если продукт не найден или у него нет PackageFamily.
    """
    session = session or get_http_session()
    resp = session.get(
        _STORE_EDGE_API.format(product_id),
        params=_STORE_EDGE_PARAMS,
        timeout=30,
    )
    resp.raise_for_status()
    payload = (resp.json() or {}).get("Payload") or {}
    families = [str(f) for f in payload.get("PackageFamilyNames", []) if str(f)]
    title = str(payload.get("Title") or "").strip()
    if not families or not title:
        return None
    return title, families[0]


def product_id_from(value: str) -> str | None:
    """Достаёт Store ID приложения из голого ID или product-ссылки store."""
    tail = value.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = _PRODUCT_ID_RE.fullmatch(Path(tail).name)
    return match.group(0) if match else None


def _seed_target(name: str, session: requests.Session) -> str:
    """Превращает имя/ссылку в query-цель для rg-adguard."""
    if "://" in name:
        return name
    if _FAMILY_RE.fullmatch(name):
        return name.lower()
    url = resolve_product_url(name, session)
    return url or name


def _human_app_name(component_name: str) -> str:
    """Убирает хвост вроде _ACER_4.00.3060_W11x64_A, оставляя имя приложения."""
    return re.sub(r"_[^_]+_[vV]?[\d.]+_W11x64_[AB]$", "", component_name)


def uwp_apps_from_template(template_dir: Path) -> list[tuple[str, str]]:
    """Возвращает [(имя для папки, family-цель)] для UWP-компонентов шаблона.

    Включает статические компоненты (.components/скан) и записи снимка
    системы (source: system, kind: uwp) из .device.json.
    """
    template_dir = Path(template_dir).resolve()
    manifest = _read_template_manifest(template_dir)
    if manifest is not None:
        sources = manifest
    else:
        sources = [comp.source for comp in scan_oem_components(template_dir)]
    apps: list[tuple[str, str]] = []
    for source in sources:
        component = _make_component(template_dir, source)
        if component.kind != "uwp" or component.blacklisted:
            continue
        family = component.family_names[0] if component.family_names else ""
        target = family or _human_app_name(component.name)
        apps.append((_human_app_name(component.name), target))
    apps.extend(uwp_apps_from_meta_system(template_dir))
    return apps


def uwp_apps_from_meta_system(template_dir: Path) -> list[tuple[str, str]]:
    """UWP-записи снимка (source: system, kind: uwp) из .device.json шаблона."""
    meta = read_device_meta(template_dir)
    apps: list[tuple[str, str]] = []
    for comp in meta.get("components", []):
        if comp.get("source") != "system" or comp.get("kind") != "uwp":
            continue
        families = [
            str(f).strip() for f in comp.get("family_names", []) if str(f).strip()
        ]
        name = str(comp.get("name") or "").strip()
        if not families or not name:
            continue
        apps.append((_human_app_name(name), families[0].lower()))
    return apps


def _existing_main_version(app_dir: Path, family: str) -> str:
    """Версия главного пакета, уже лежащего в папке приложения (или '0')."""
    for path in sorted(app_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in UWP_BUNDLE_EXTS:
            continue
        if parse_package_family(path.name) == family:
            return _package_version(path.name)
    return "0"


def _remove_stale_main(app_dir: Path, family: str, keep_name: str) -> None:
    """Удаляет старые главные бандлы семейства, оставляет только keep_name."""
    for path in sorted(app_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in UWP_BUNDLE_EXTS:
            continue
        if path.name == keep_name:
            continue
        if parse_package_family(path.name) == family:
            path.unlink()


def load_query_state(path: Path | None) -> dict:
    """Читает кэш неудачных запросов (.apps-query-state.json); битый — пустой."""
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_query_state(path: Path, state: dict) -> None:
    """Атомарно пишет кэш неудачных запросов (tmp + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _failure_is_fresh(state: dict, key: str, now: float | None = None) -> dict | None:
    """Запись кэша для ключа, если она ещё не истекла (иначе None)."""
    entry = state.get(key)
    if not isinstance(entry, dict):
        return None
    when = entry.get("ts")
    if not isinstance(when, (int, float)):
        return None
    st = entry.get("state")
    ttl = (
        _FAIL_TTL_NOT_FOUND_SECONDS
        if st == "not_found"
        else _FAIL_TTL_ERROR_SECONDS
    )
    now = time.time() if now is None else now
    if now - when >= ttl:
        return None
    if "apps.microsoft.com/search" in str(entry.get("msg", "")):
        return None
    return entry


def _cache_skip_result(app: str, entry: dict) -> UwpDownloadResult:
    """Результат пропуска приложения по кэшу неудачного запроса."""
    st = entry.get("state", "error")
    ttl = (
        _FAIL_TTL_NOT_FOUND_SECONDS
        if st == "not_found"
        else _FAIL_TTL_ERROR_SECONDS
    )
    when = entry.get("ts")
    when = when if isinstance(when, (int, float)) else time.time()
    until = time.strftime("%d.%m.%Y", time.localtime(when + ttl))
    msg = str(entry.get("msg") or "").strip()
    text = f"кэш до {until}" + (f": {msg}" if msg else "")
    return UwpDownloadResult(
        app=app,
        family=None,
        version="0",
        state=st,
        message=text,
    )


def download_uwp_app(  # noqa: PLR0913, PLR0917
    app: str,
    target: str,
    apps_root: Path,
    ring: str = "Retail",
    session: requests.Session | None = None,
    force: bool = False,
    arch: str = "x64",
    installed_versions: Mapping[str, str] | None = None,
) -> UwpDownloadResult:
    """Скачивает свежий пакет приложения в apps_root/<Приложение>.

    force=True — качать заново, даже если в кэше уже есть актуальная версия.
    arch — целевая архитектура: в пул зависимостей попадают только neutral и
    arch пакеты (строгий x64: x86/arm/arm64 отбрасываются).
    Сетевые/сервисные ошибки (в т.ч. 403 Cloudflare от rg-adguard) не роняют
    сборку: возвращается state="error", остальные приложения качаются дальше.
    """
    try:
        return _download_uwp_app(
            app,
            target,
            apps_root,
            ring,
            session,
            force,
            arch,
            installed_versions=installed_versions,
        )
    except requests.RequestException as e:
        log.warning("UWP '%s' (%s): ошибка Store/сети: %s", app, target, e)
        return UwpDownloadResult(
            app=app,
            family=None,
            version="0",
            state="error",
            message=f"Store/сеть: {e}",
        )


def _write_deps_meta(app_dir: Path, dep_names: list[str]) -> None:
    """Пишет .deps.json — список зависимостей приложения из общего пула.

    Пакеты лежат плоско в Cache/<Dev>/Apps/Dependencies; по этому списку
    сборка знает, какие файлы пула материализовать в Output/<Приложение>/.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    app_dir.joinpath(DEPS_META_NAME).write_text(
        json.dumps(dep_names, ensure_ascii=False), encoding="utf-8"
    )


def _download_uwp_app(  # noqa: PLR0912, PLR0913, PLR0917
    app: str,
    target: str,
    apps_root: Path,
    ring: str = "Retail",
    session: requests.Session | None = None,
    force: bool = False,
    arch: str = "x64",
    installed_versions: Mapping[str, str] | None = None,
) -> UwpDownloadResult:
    """Реализация скачивания без перехвата сетевых ошибок (см. download_uwp_app)."""
    session = session or get_http_session()
    query = _seed_target(target, session)
    links = fetch_store_files(query, ring=ring, session=session)
    if links is None:
        return UwpDownloadResult(
            app=app,
            family=None,
            version="0",
            state="not_found",
            message="Пакеты не найдены в Microsoft Store (rg-adguard)",
        )

    known_family = query.lower() if _FAMILY_RE.fullmatch(query) else None
    main, deps = pick_uwp_packages(links, known_family=known_family, arch=arch)
    if main is None:
        return UwpDownloadResult(
            app=app,
            family=known_family,
            version="0",
            state="not_found",
            message="Не удалось выбрать главный пакет приложения",
        )

    main_name, main_url = main
    main_family = parse_package_family(main_name)
    main_version = _package_version(main_name)

    app_dir = apps_root / sanitize_folder_name(app)
    app_dir.mkdir(parents=True, exist_ok=True)
    installed_ver = None
    if not force:
        if installed_versions and main_family:
            installed_ver = installed_versions.get(main_family.lower())
        if not installed_ver and installed_versions:
            installed_ver = installed_versions.get(app.lower())

        cached_pkg = app_dir / main_name
        effective_cand_ver = main_version
        if cached_pkg.is_file():
            inner = get_bundle_inner_version(cached_pkg, arch=arch)
            if inner:
                effective_cand_ver = inner

        if installed_ver and is_app_version_up_to_date(
            installed_ver, effective_cand_ver
        ):
            return UwpDownloadResult(
                app=app,
                family=main_family,
                version=installed_ver,
                state="up_to_date",
                message="уже актуально в системе",
            )

        if main_family is not None:
            existing = _existing_main_version(app_dir, main_family)
            if (
                version_tuple(existing) >= version_tuple(main_version)
                and existing != "0"
            ):
                return UwpDownloadResult(
                    app=app,
                    family=main_family,
                    version=existing,
                    state="up_to_date",
                )

    download_file(main_url, app_dir / main_name, session=session)
    if main_family is not None:
        _remove_stale_main(app_dir, main_family, main_name)

    inner_ver = get_bundle_inner_version(app_dir / main_name, arch=arch)
    if (
        inner_ver
        and installed_ver
        and is_app_version_up_to_date(installed_ver, inner_ver)
    ):
        return UwpDownloadResult(
            app=app,
            family=main_family,
            version=installed_ver,
            state="up_to_date",
            message="уже актуально в системе",
        )

    total = 1
    dep_names = [name for name, _ in deps]
    pool = apps_root / DEPS_DIR_NAME
    for dep_name, dep_url in deps:
        dep_path = pool / dep_name
        if dep_path.exists():
            continue
        download_file(dep_url, dep_path, session=session)
        total += 1
    if dep_names:
        _write_deps_meta(app_dir, dep_names)

    return UwpDownloadResult(
        app=app,
        family=main_family,
        version=main_version,
        state="done",
        message=f"скачано файлов: {total}",
    )


def download_uwp_updates(  # noqa: PLR0913, PLR0917
    seeds: list[tuple[str, str]],
    apps_root: Path,
    ring: str = "Retail",
    session: requests.Session | None = None,
    force: bool = False,
    arch: str = "x64",
    installed_versions: Mapping[str, str] | None = None,
) -> list[UwpDownloadResult]:
    """Скачивает обновления всех приложений из списка (имя, query-цель).

    Неудачные запросы (not_found/error) пишутся в кэш .apps-query-state.json
    рядом с apps_root; при следующих запусках они пропускаются до истечения
    TTL (not_found — 7 дней, error — 12 часов) или при force=True. Между
    реальными запросами — пауза, чтобы не словить троттлинг Cloudflare.
    arch — целевая архитектура (только neutral + arch пакеты).
    """
    apps_root = Path(apps_root)
    state_path = Path(apps_root).parent / QUERY_STATE_NAME
    state = load_query_state(state_path)
    results: list[UwpDownloadResult] = []
    queried_any = False
    for app, target in seeds:
        key = (target or app).strip().lower()
        cached = None if force else _failure_is_fresh(state, key)
        if cached is not None:
            results.append(_cache_skip_result(app, cached))
            continue
        if queried_any:
            time.sleep(1.0)
        result = download_uwp_app(
            app,
            target,
            apps_root,
            ring=ring,
            session=session,
            force=force,
            arch=arch,
            installed_versions=installed_versions,
        )
        queried_any = True
        if result.state in ("not_found", "error"):
            state[key] = {
                "state": result.state,
                "ts": int(time.time()),
                "msg": result.message or "",
            }
        results.append(result)
        log.info(
            "%s: %s (%s) %s",
            app,
            result.state,
            result.version,
            result.message or "",
        )
    if state:
        save_query_state(state_path, state)
    return results
