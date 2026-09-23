"""Загрузка файлов драйверов через Selenium и распаковка .cab."""

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests
from selenium.common import WebDriverException

from .catalog import CATALOG_SEARCH_URL
from .models import CatalogEntry, DownloadResult, LocalDevice
from .utils import download_file, get_http_session, sanitize_folder_name

try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

log = logging.getLogger(__name__)


def build_device_folder(
    download_root: Path, dev: LocalDevice, entry: CatalogEntry | None = None
) -> Path:
    """Строит и создаёт путь <download_root>/<Вендор>/<Устройство>."""
    folder = (
        Path(download_root)
        / sanitize_folder_name(dev.manufacturer)
        / sanitize_folder_name(dev.name)
    )
    if entry is not None:
        folder = folder / sanitize_folder_name(f"{entry.title}_{entry.version}")

    folder.mkdir(parents=True, exist_ok=True)
    return folder


_LONG_PATH_THRESHOLD = 240
_CAB_MAGIC = b"MSCF"
_CAB_HEAD_READ = 64
_CAB_SIZE_OFFSET = 8
_CAB_MIN_HEADER = 12


def _path_too_long(path: Path) -> bool:
    """Длинный ли путь для легаси expand.exe (лимит MAX_PATH = 260 с нулём)."""
    return len(os.path.abspath(str(path))) >= _LONG_PATH_THRESHOLD


def _cab_validate(cab_path: Path) -> int:
    """Проверяет .cab до вызова expand.exe и возвращает размер файла.

    Ловит отсутствующий/пустой файл, HTML-ошибку вместо каба и оборванную
    загрузку (заголовок обещает больше байт, чем скачано). Подписанные кабы
    каталога MS несут PKCS#7-подпись ПОСЛЕ объявленной длины (cbCabinet), так
    что файл длиннее cbCabinet — это нормально.
    """
    if not cab_path.is_file():
        raise FileNotFoundError(f"Файл .cab не найден: {cab_path}")
    size = cab_path.stat().st_size
    if size == 0:
        raise ValueError(f".cab пустой (оборванная загрузка): {cab_path}")

    with cab_path.open("rb") as fh:
        head = fh.read(_CAB_HEAD_READ)
    if head.lstrip().lower().startswith((b"<!doctype", b"<html", b"<?xml")):
        raise ValueError(
            f"{cab_path.name}: по ссылке пришла не .cab (HTML/ошибка сервера), "
            f"удалите файл и перекачайте"
        )
    if head[:4] == _CAB_MAGIC and len(head) >= _CAB_MIN_HEADER:
        (cb_cabinet,) = struct.unpack_from("<I", head, _CAB_SIZE_OFFSET)
        if size < cb_cabinet:
            raise ValueError(
                f"{cab_path.name}: загрузка оборвана — получено {size} байт "
                f"из {cb_cabinet}, удалите файл и перекачайте заново"
            )
    return size


def extract_cab(cab_path: Path, dest_folder: Path) -> Path:
    """Распаковывает .cab через встроенную утилиту expand.exe.

    Легаси expand.exe не понимает длинные пути (MAX_PATH 260), поэтому при
    длинном входном или целевом пути каб копируется в короткий временный
    каталог, распаковывается там, а результат переносится в целевую папку
    средствами Python (он длинные пути поддерживает).
    """
    dest_folder = Path(dest_folder)
    dest_folder.mkdir(parents=True, exist_ok=True)
    size = _cab_validate(cab_path)

    work_dir: Path | None = None
    try:
        if _path_too_long(cab_path) or _path_too_long(dest_folder):
            work_dir = Path(tempfile.mkdtemp(prefix="wdi_cab_"))
            out_dir = work_dir / "out"
            out_dir.mkdir()
            short_cab = work_dir / cab_path.name
            shutil.copyfile(cab_path, short_cab)
            cmd = ["expand.exe", "-F:*", str(short_cab), str(out_dir)]
        else:
            cmd = ["expand.exe", "-F:*", str(cab_path), str(dest_folder)]

        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"{cab_path.name}: expand.exe завершился с кодом {result.returncode}"
                f" (размер {size} байт) — файл повреждён или не является .cab,"
                f" удалите его и перекачайте заново. {detail}"
            )

        if work_dir is not None:
            for item in out_dir.iterdir():
                target = dest_folder / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))
        return dest_folder
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def collect_inf_files(folders: list[Path]) -> list[Path]:
    """Ищет все .inf во всех переданных папках рекурсивно — для установки."""
    return [inf for folder in folders for inf in Path(folder).rglob("*.inf")]


def _create_webdriver(browser: str, headless: bool):
    """Создаёт Selenium WebDriver.

    Файл мы качаем сами через requests (см. download_via_browser), так что
    браузеру не нужно ничего настраивать под сохранение.
    """
    if not HAS_SELENIUM:
        raise ModuleNotFoundError("Пакет selenium не установлен")

    if browser == "firefox":
        opts = FirefoxOptions()
        if headless:
            opts.add_argument("-headless")
        return webdriver.Firefox(options=opts)

    if browser == "chrome":
        opts = ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        return webdriver.Chrome(options=opts)

    raise ValueError(
        f"Неизвестный браузер: {browser!r} (используйте 'firefox' или 'chrome')"
    )


def selenium_available(browser: str) -> str | None:
    """Быстрая проверка доступности Selenium и браузера. None = всё ок."""
    try:
        _create_webdriver(browser, headless=True).quit()
        return None
    except ModuleNotFoundError:
        return "пакет 'selenium' не установлен (pip install \"selenium>=4.6\")"
    except (WebDriverException, OSError) as e:
        return f"не удалось запустить {browser}: {e}"


def download_via_browser(
    hwid: str,
    update_id: str,
    download_dir: Path,
    browser: str = "firefox",
    headless: bool = True,
) -> Path:
    """Ищет обновление по HWID и скачивает через кнопку Download.

    update_id — это не поисковый запрос, а идентификатор конкретной строки
    результата. Кликает по кнопке Download, забирает href появившейся прямой
    ссылки и cookies браузерной сессии и качает файл через requests.
    """
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    driver = _create_webdriver(browser, headless)

    try:
        btn = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            driver.get(f"{CATALOG_SEARCH_URL}?q={quote(hwid, safe='')}")
            try:
                btn = WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable((By.ID, update_id))
                )
                break
            except TimeoutException:
                if attempt < attempts:
                    log.warning(
                        "Строка update_id=%s не найдена по HWID '%s' "
                        "(попытка %d/%d), повторяю поиск",
                        update_id,
                        hwid,
                        attempt,
                        attempts,
                    )
                    continue
                raise RuntimeError(
                    f"На странице результатов поиска по HWID '{hwid}' не нашлась "
                    f"строка с update_id={update_id} за {attempts * 30} сек."
                ) from None

        # Кликаем через JS, а не нативным Selenium-кликом: нативный клик требует,
        # чтобы элемент был реально проскроллен в видимую область.
        original_handles = set(driver.window_handles)
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", btn
        )

        # Кнопка иногда открывает диалог скачивания в новой вкладке — переключаемся.
        deadline = time.time() + 10
        while time.time() < deadline:
            new_handles = set(driver.window_handles) - original_handles
            if new_handles:
                driver.switch_to.window(new_handles.pop())
                break
            time.sleep(0.3)

        link_selector = "a[href*='.cab'], a[href*='windowsupdate.com'], a[href*='.msu']"
        try:
            link_el = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, link_selector))
            )
        except TimeoutException:
            debug_path = download_dir / "_debug_download_dialog.html"
            debug_path.write_text(driver.page_source, encoding="utf-8")
            raise RuntimeError(
                f"После клика по Download не появилась прямая ссылка за 20 сек. "
                f"HTML страницы сохранён в {debug_path} для разбора."
            ) from None

        href = link_el.get_attribute("href")
        if not href:
            raise RuntimeError("Ссылка на файл найдена в DOM, но её href пуст.")

        # Переносим cookies браузерной сессии в обычную requests-сессию
        session = get_http_session()
        for c in driver.get_cookies():
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        filename = href.rsplit("/", 1)[-1].split("?")[0] or f"{update_id}.cab"
        return download_file(href, download_dir / filename, session=session)
    finally:
        driver.quit()


def wait_for_new_download(
    downloads_dir: Path,
    extensions=(".cab", ".msu", ".exe"),
    timeout_seconds: int = 600,
    poll_interval: float = 1.0,
    stable_checks: int = 3,
) -> Path:
    """Следит за папкой загрузок и возвращает путь к новому файлу.

    Возвращает путь только когда файл ДЕЙСТВИТЕЛЬНО докачался (размер не
    меняется несколько проверок подряд).
    """
    downloads_dir = Path(downloads_dir)
    before = {p.name for p in downloads_dir.iterdir() if p.is_file()}
    temp_suffixes = (".crdownload", ".tmp", ".partial", ".download", ".part")

    deadline = time.time() + timeout_seconds
    tracked_name, stable_count, last_size = None, 0, -1

    while time.time() < deadline:
        new_names = {p.name for p in downloads_dir.iterdir() if p.is_file()} - before
        has_pending_temp = any(n.lower().endswith(temp_suffixes) for n in new_names)
        candidates = [n for n in new_names if Path(n).suffix.lower() in extensions]

        if candidates and not has_pending_temp:
            name = candidates[0]
            if name != tracked_name:
                tracked_name, stable_count, last_size = name, 0, -1
            try:
                size = (downloads_dir / name).stat().st_size
            except FileNotFoundError:
                size = -1
            stable_count = stable_count + 1 if (size > 0 and size == last_size) else 0
            last_size = size
            if stable_count >= stable_checks:
                return downloads_dir / name
        else:
            tracked_name, stable_count, last_size = None, 0, -1

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Файл не появился/не докачался в {downloads_dir} за {timeout_seconds} секунд."
    )


def semi_automatic_download(
    query: str, downloads_dir: Path, dest_folder: Path, timeout_seconds: int = 600
) -> Path:
    """Открывает браузер по умолчанию на поиске и ждет ручного скачивания."""
    print("  Открываю браузер: найдите нужную версию и нажмите Download...")
    webbrowser.open(f"{CATALOG_SEARCH_URL}?q={quote(query, safe='')}")

    downloaded_file = wait_for_new_download(
        downloads_dir, timeout_seconds=timeout_seconds
    )
    dest_folder.mkdir(parents=True, exist_ok=True)
    target_path = dest_folder / downloaded_file.name
    downloaded_file.replace(target_path)

    if target_path.suffix.lower() == ".cab":
        extract_cab(target_path, dest_folder)
        target_path.unlink()

    return dest_folder


def download_candidate(  # noqa: PLR0913, PLR0917
    dev: LocalDevice,
    entry: CatalogEntry,
    matched_hwid: str,
    download_root: Path,
    browser: str,
    headless: bool,
) -> DownloadResult:
    """Выполняет попытку автоматической загрузки драйвера через браузер."""
    try:
        dest_folder = build_device_folder(download_root, dev, entry=entry)
    except OSError as e:
        return DownloadResult(
            dev,
            entry,
            matched_hwid,
            Path(download_root) / "_unknown",
            "failed",
            f"Не удалось создать папку: {e}",
        )

    try:
        cab_path = download_via_browser(
            matched_hwid,
            entry.update_id,
            dest_folder,
            browser=browser,
            headless=headless,
        )
        if cab_path.suffix.lower() == ".cab":
            extract_cab(cab_path, dest_folder)
            cab_path.unlink()
        # Маркер успешной закачки: листья без .inf (например, апдейты корневых
        # сертификатов — только .exe) иначе не считаются закешированными.
        (dest_folder / ".download_ok").touch()
        return DownloadResult(dev, entry, matched_hwid, dest_folder, "ok")
    except (
        RuntimeError,
        ValueError,
        FileNotFoundError,
        TimeoutError,
        requests.RequestException,
        WebDriverException,
    ) as e:
        log.debug(f"Автозакачка не удалась для {dev.name}: {e}")
        return DownloadResult(
            dev, entry, matched_hwid, dest_folder, "manual_needed", str(e)
        )
