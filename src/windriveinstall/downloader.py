import logging
import subprocess
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests
from selenium.common import WebDriverException

from .catalog import CATALOG_SEARCH_URL
from .models import CatalogEntry, DownloadResult, LocalDevice
from .utils import get_http_session, sanitize_folder_name

try:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
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

    # if entry.version.strip().lower() not in ("n/a", "none", ""):
    #     try:
    #         return version_tuple(remote_version) > version_tuple(local_version)
    #     except ValueError, TypeError:
    #         pass

    folder.mkdir(parents=True, exist_ok=True)
    return folder


def extract_cab(cab_path: Path, dest_folder: Path) -> Path:
    """Распаковывает .cab через встроенную утилиту expand.exe."""
    dest_folder.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["expand.exe", "-F:*", str(cab_path), str(dest_folder)],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"expand.exe завершился с ошибкой: {result.stderr}")
    return dest_folder


def collect_inf_files(folders: list[Path]) -> list[Path]:
    """Ищет все .inf во всех переданных папках рекурсивно — для установки."""
    return [inf for folder in folders for inf in Path(folder).rglob("*.inf")]


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
                    print(
                        f"\r  Скачивание {dest_path.name}: {downloaded / total * 100:5.1f}%",
                        end="",
                    )
        print()
    return dest_path


def _create_webdriver(browser: str, headless: bool):
    """
    Создаёт Selenium WebDriver. Файл мы качаем сами через requests (см.
    download_via_browser), так что браузеру не нужно ничего настраивать под
    сохранение — только рендерить страницу и кликать.
    """
    from selenium import webdriver

    if browser == "firefox":
        from selenium.webdriver.firefox.options import Options as FirefoxOptions

        opts = FirefoxOptions()
        if headless:
            opts.add_argument("-headless")
        return webdriver.Firefox(options=opts)

    if browser == "chrome":
        from selenium.webdriver.chrome.options import Options as ChromeOptions

        opts = ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        return webdriver.Chrome(options=opts)

    raise ValueError(
        f"Неизвестный браузер: {browser!r} (используйте 'firefox' или 'chrome')"
    )


def selenium_available(browser: str) -> str | None:
    """Быстрая проверка, что Selenium установлен и браузер реально запускается. None = всё ок."""
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
    """
    Ищет обновление по HWID (update_id — это не поисковый запрос, а
    идентификатор конкретной строки результата), кликает по кнопке "Download",
    забирает href появившейся прямой ссылки и cookies браузерной сессии —
    и качает файл сам через requests (см. download_file). Так надёжнее и
    быстрее, чем ждать, пока браузер сам сохранит файл на диск.

    Если разметка сайта изменится настолько, что что-то из этого не найдётся,
    здесь вылетит понятная ошибка, а пайплайн (run) предложит ручной фолбэк
    (semi_automatic_download).
    """

    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    driver = _create_webdriver(browser, headless)

    try:
        driver.get(f"{CATALOG_SEARCH_URL}?q={quote(hwid, safe='')}")

        try:
            btn = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.ID, update_id))
            )
        except TimeoutException:
            raise RuntimeError(
                f"На странице результатов поиска по HWID '{hwid}' не нашлась строка "
                f"с update_id={update_id} за 30 сек — либо этот HWID больше не даёт "
                f"такой результат, либо изменилась разметка сайта."
            ) from None

        # Кликаем через JS, а не нативным Selenium-кликом: нативный клик требует,
        # чтобы элемент был реально проскроллен в видимую область, а строки
        # каталога иногда не поддаются автоскроллу в headless-режиме.
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

        # Переносим cookies браузерной сессии в обычную requests-сессию —
        # на случай, если сервер проверяет, что запрос идёт "из браузера".
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
    """
    Следит за папкой загрузок и возвращает путь к новому файлу — но только
    когда он ДЕЙСТВИТЕЛЬНО докачался (размер не меняется несколько проверок
    подряд; универсальный признак, не зависящий от того, как конкретный
    браузер называет свой временный файл).
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
    """Открывает браузер по умолчанию на поиске, ждёт, пока пользователь сам нажмёт Download, распаковывает."""
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


def download_candidate(
    dev: LocalDevice,
    entry: CatalogEntry,
    matched_hwid: str,
    download_root: Path,
    browser: str,
    headless: bool,
) -> DownloadResult:
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
        return DownloadResult(dev, entry, matched_hwid, dest_folder, "ok")
    except (
        RuntimeError,
        TimeoutError,
        requests.RequestException,
        WebDriverException,
    ) as e:
        log.debug(f"Автозакачка не удалась для {dev.name}: {e}")
        return DownloadResult(
            dev, entry, matched_hwid, dest_folder, "manual_needed", str(e)
        )
