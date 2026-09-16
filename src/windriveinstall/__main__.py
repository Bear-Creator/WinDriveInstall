r"""Driver Update Helper.

Собирает HWID и версии драйверов установленных устройств (Windows), ищет
обновления в Microsoft Update Catalog, автоматически скачивает их через
настоящий браузер (Selenium) и раскладывает по структуре:

    <download_root>/<Вендор>/<Устройство>/*.inf, *.sys, ...

В конце предлагает установить все найденные драйверы (генерирует .bat с
pnputil) — но только после явного подтверждения пользователя.

-----------------------------------------------------------------------------
ЗАВИСИМОСТИ
-----------------------------------------------------------------------------
    pip install wmi pywin32 requests beautifulsoup4 "selenium>=4.6"

Selenium 4.6+ сам подтягивает нужный geckodriver/chromedriver — в PATH ничего
класть не надо, достаточно самого браузера (Firefox или Chrome).

-----------------------------------------------------------------------------
ОСНОВНОЙ СЦЕНАРИЙ
-----------------------------------------------------------------------------
    python driver_updater.py run
    python driver_updater.py run --download-root D:\Drivers --browser chrome
    --no-headless

Сканирует устройства -> ищет обновления -> печатает таблицу -> спрашивает,
что скачивать -> скачивает через браузер (с ручным фолбэком, если автоматика
не справилась) -> печатает итоговый отчёт -> предлагает установить всё разом.

-----------------------------------------------------------------------------
ОТДЕЛЬНЫЕ КОМАНДЫ (для точечного тестирования)
-----------------------------------------------------------------------------
    python driver_updater.py list-devices --csv devices.csv --show-all-hwids
    python driver_updater.py search --hwid "PCI\VEN_10EC&DEV_2600&REV_21"
    --hwid "PCI\VEN_10EC&DEV_2600"
    python driver_updater.py check
    python driver_updater.py auto-download --hwid "PCI\VEN_10EC&DEV_2600"
    --update-id <GUID> --out-dir tmp --no-headless
    python driver_updater.py semi-auto --query "PCI\VEN_10EC&DEV_2600"
    --downloads "C:\Users\bear\Downloads" --out tmp
    python driver_updater.py extract --cab file.cab --out tmp
    python driver_updater.py install --inf tmp\driver.inf --confirm
    python driver_updater.py install-bat --bat Drivers\install_drivers.bat --confirm

Флаг --verbose (до имени команды) включает подробный лог для отладки.
"""

import argparse
import logging
import re
import sys
from pathlib import Path

from .catalog import find_updates, search_catalog_with_fallback
from .devices import get_local_devices, save_devices_to_csv
from .downloader import (
    build_device_folder,
    collect_inf_files,
    download_candidate,
    download_file,
    download_via_browser,
    extract_cab,
    selenium_available,
    semi_automatic_download,
    wait_for_new_download,
)
from .installer import (
    generate_install_bats,
    install_driver,
    run_install_bat,
)
from .models import DownloadResult
from .ui import print_final_report, print_update_summary
from .utils import get_http_session

log = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_ROOT = Path("Drivers")


def setup_logging(verbose: bool = False) -> None:
    """Настройка единого формата и уровня логов для всего приложения."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            # logging.StreamHandler(sys.stdout),  # Вывод в консоль
            logging.FileHandler("app.log", encoding="utf-8")  # Запись в файл
        ],
    )


def _parse_selection(choice: str, candidates: list) -> list:
    choice = choice.strip().lower()
    if choice == "all":
        return candidates
    indices = {int(x) - 1 for x in re.findall(r"\d+", choice)}
    return [c for i, c in enumerate(candidates) if i in indices]


def _process_downloads(
    selected: list, download_root: Path, browser: str, headless: bool
) -> list[DownloadResult]:
    """Скачивает выбранные кандидаты через браузер или переходит в ручной режим."""
    selenium_error = selenium_available(browser)
    results = []
    if selenium_error:
        print(f"\nАвтоматическая закачка через {browser} недоступна: {selenium_error}")
        print("Сразу переходим к ручному режиму для всех выбранных устройств.\n")
        for dev, entry, matched_hwid in selected:
            dest_folder = build_device_folder(download_root, dev)
            results.append(
                DownloadResult(
                    dev,
                    entry,
                    matched_hwid,
                    dest_folder,
                    "manual_needed",
                    selenium_error,
                )
            )
    else:
        print(f"\nСкачиваю {len(selected)} драйвер(ов) через браузер...\n")
        for dev, entry, matched_hwid in selected:
            print(f"-> {dev.name} ({dev.driver_version} -> {entry.version})")
            results.append(
                download_candidate(
                    dev, entry, matched_hwid, download_root, browser, headless
                )
            )

    manual_queue = [r for r in results if r.status != "ok"]
    if manual_queue and (
        input(
            f"\n{len(manual_queue)} драйвер(ов) не скачано. "
            "Скачать вручную через браузер? [Y/n]: "
        )
        .strip()
        .lower()
        != "n"
    ):
        downloads_dir = Path.home() / "Downloads"
        for r in manual_queue:
            print(f"\n-> Ручное скачивание: {r.device.name}")
            try:
                semi_automatic_download(r.matched_hwid, downloads_dir, r.dest_folder)
                r.status, r.error = "ok", None
            except (TimeoutError, RuntimeError, OSError) as e:
                r.status, r.error = "failed", str(e)

    return results


def run_pipeline(download_root: Path, browser: str, headless: bool) -> None:
    """Запускает основной интерактивный пайплайн поиска и установки обновлений."""
    print("Сканирую установленные устройства...")
    devices = get_local_devices()
    print(f"Найдено устройств: {len(devices)}")

    print("Ищу обновления в Microsoft Update Catalog...")
    session = get_http_session()
    candidates = find_updates(devices, session)

    if not candidates:
        print("\nВсе драйверы актуальны — обновлений не найдено.")
        return

    print(f"\nНайдено устройств с обновлениями: {len(candidates)}")
    print_update_summary(candidates)

    choice = input("Что скачать? [all / none / номера через запятую, напр. 1,3]: ")
    if choice.strip().lower() in ("", "none", "n"):
        print("Ничего не скачиваем.")
        return

    selected = _parse_selection(choice, candidates)
    if not selected:
        print("Ничего не выбрано.")
        return

    results = _process_downloads(selected, download_root, browser, headless)
    print_final_report(results)

    inf_files = collect_inf_files([r.dest_folder for r in results if r.status == "ok"])
    if not inf_files:
        print("Нет распакованных .inf для установки.")
        return

    bat_path = generate_install_bats(inf_files, download_root)[0]
    print(f"Сгенерирован установочный скрипт: {bat_path}")
    print(f"Найдено .inf файлов: {len(inf_files)}")

    confirm_msg = (
        "\nУстановить все найденные драйверы сейчас? "
        "Потребуются права администратора. [y/N]: "
    )
    if input(confirm_msg).strip().lower() == "y":
        run_install_bat(bat_path, confirm=True)
    else:
        print(f"Когда будете готовы — запустите от администратора:\n  {bat_path}")


# =============================================================================
# CLI
# =============================================================================


def _add_browser_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--browser", choices=["firefox", "chrome"], default="firefox")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)


def build_arg_parser() -> argparse.ArgumentParser:
    """Создает парсер аргументов командной строки для CLI."""
    parser = argparse.ArgumentParser(description="Driver Update Helper")
    parser.add_argument(
        "--verbose", action="store_true", help="Подробный лог для отладки"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Основной интерактивный пайплайн обновления")
    p.add_argument("--download-root", default=str(DEFAULT_DOWNLOAD_ROOT))
    _add_browser_args(p)

    sub.add_parser(
        "check", help="Показать таблицу доступных обновлений, без скачивания"
    )

    p = sub.add_parser(
        "list-devices", help="Показать локальные устройства и их HWID/версии"
    )
    p.add_argument("--csv", default=None)
    p.add_argument("--show-all-hwids", action="store_true")

    p = sub.add_parser(
        "search", help="Поиск в каталоге по HWID (можно указать несколько)"
    )
    p.add_argument("--hwid", required=True, action="append")

    p = sub.add_parser(
        "links", help="[диагностика] Прямые ссылки через POST (может не работать)"
    )
    p.add_argument("--update-id", required=True)
    p.add_argument("--debug-dump", default=None)

    p = sub.add_parser("download-file", help="Скачать файл по прямой ссылке")
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("extract", help="Распаковать .cab в указанную папку")
    p.add_argument("--cab", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser(
        "auto-download", help="Автоматическая закачка через Selenium (одно обновление)"
    )
    p.add_argument("--hwid", required=True, help="HWID для поиска (не GUID!)")
    p.add_argument(
        "--update-id", required=True, help="GUID строки результата (id кнопки Download)"
    )
    p.add_argument("--out-dir", required=True)
    _add_browser_args(p)

    p = sub.add_parser("wait-download", help="Ждать новый файл в папке загрузок")
    p.add_argument("--dir", required=True)
    p.add_argument("--timeout", type=int, default=600)

    p = sub.add_parser(
        "semi-auto", help="Ручной фолбэк: браузер по умолчанию + ожидание файла"
    )
    p.add_argument("--query", required=True)
    p.add_argument("--downloads", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=600)

    p = sub.add_parser(
        "install", help="Установить один драйвер из .inf (требует --confirm)"
    )
    p.add_argument("--inf", required=True)
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser(
        "install-bat",
        help="Запустить сгенерированный install_drivers.bat (требует --confirm)",
    )
    p.add_argument("--bat", required=True)
    p.add_argument("--confirm", action="store_true")

    return parser


def _handle_list_devices(args: argparse.Namespace) -> None:
    devices = get_local_devices()
    for d in devices:
        print(f"{d.name:50s} | {d.hwid:40s} | v{d.driver_version}")
        if args.show_all_hwids:
            for h in d.hwid_candidates:
                print(f"      -> {h}")
    if args.csv:
        save_devices_to_csv(devices, args.csv)


def _handle_search(args: argparse.Namespace) -> None:
    matched_hwid, entries = search_catalog_with_fallback(args.hwid)
    if matched_hwid:
        print(f"# Совпадение по HWID: {matched_hwid}")
    for e in entries:
        print(f"[{e.update_id}] {e.title} | {e.version} | {e.last_updated}")


def _execute_command(args: argparse.Namespace) -> None:
    """Диспатчер подкоманд CLI."""
    session_lazy = None

    def _get_session():
        nonlocal session_lazy
        if session_lazy is None:
            session_lazy = get_http_session()
        return session_lazy

    handlers = {
        "run": lambda: run_pipeline(
            Path(args.download_root), args.browser, args.headless
        ),
        "check": lambda: (
            candidates := find_updates(get_local_devices(), _get_session()),
            print_update_summary(candidates)
            if candidates
            else print("Все драйверы актуальны — обновлений не найдено."),
        ),
        "list-devices": lambda: _handle_list_devices(args),
        "search": lambda: _handle_search(args),
        "download-file": lambda: download_file(args.url, Path(args.out)),
        "extract": lambda: (
            extract_cab(Path(args.cab), Path(args.out)),
            print(f"Распаковано в: {args.out}"),
        ),
        "auto-download": lambda: print(
            f"Скачано: {
                download_via_browser(
                    args.hwid,
                    args.update_id,
                    Path(args.out_dir),
                    browser=args.browser,
                    headless=args.headless,
                )
            }"
        ),
        "wait-download": lambda: print(
            wait_for_new_download(Path(args.dir), timeout_seconds=args.timeout)
        ),
        "semi-auto": lambda: print(
            f"Готово: {
                semi_automatic_download(
                    args.query,
                    Path(args.downloads),
                    Path(args.out),
                    timeout_seconds=args.timeout,
                )
            }"
        ),
        "install": lambda: install_driver(Path(args.inf), confirm=args.confirm),
        "install-bat": lambda: run_install_bat(Path(args.bat), confirm=args.confirm),
    }

    handler = handlers.get(args.command)
    if handler:
        handler()


def main() -> None:
    """Главная точка входа в приложение."""
    if sys.platform != "win32":
        print("Ошибка: Скрипт может обновлять драйверы только в ОС Windows!")
        sys.exit(1)

    args_list = sys.argv[1:]
    known_commands = {
        "run",
        "check",
        "list-devices",
        "search",
        "download-file",
        "extract",
        "auto-download",
        "wait-download",
        "semi-auto",
        "install",
        "install-bat",
        "-h",
        "--help",
    }

    if not any(arg in known_commands for arg in args_list):
        args_list.append("run")

    args = build_arg_parser().parse_args(args_list)

    setup_logging(verbose=args.verbose)
    log.info("Запуск приложения...")
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _execute_command(args)


if __name__ == "__main__":
    main()
