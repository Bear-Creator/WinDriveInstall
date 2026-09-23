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
    python driver_updater.py run --template-dir Template/Nitro5 --no-install
    python driver_updater.py run --template-dir Template/Nitro5
        --no-drivers --no-apps   // пересборка из существующего кэша
    python driver_updater.py run   // rolling-сборка под текущую систему

run обновляет драйверы -> UWP-приложения -> собирает пакет (шаблон или
rolling) и предлагает установку.

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
    python driver_updater.py build --template-dir Template/Nitro5 --snapshot
    python driver_updater.py build  (rolling: драйверы под текущую систему)
    python driver_updater.py driver-update --template-dir Template/Nitro5
    python driver_updater.py uwp-update --template-dir Template/Nitro5 --force

Флаг --verbose (до имени команды) включает подробный лог для отладки.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import NoReturn

import requests

from .builder import (
    FACTORY_BAT_NAME,
    build_oem_package,
    merge_snapshot_apps,
    read_device_meta,
)
from .catalog import find_updates, search_catalog_with_fallback
from .installer import install_driver, run_install_bat
from .models import MergeReport
from .rolling import (
    DownloadOptions,
    compose_app_seeds,
    run_rolling,
    update_template_drivers,
)
from .ui import print_update_summary
from .utils import get_http_session
from .uwp import (
    download_uwp_updates,
    product_id_from,
    resolve_store_product,
    uwp_apps_from_template,
)

try:
    from .devices import (
        get_installed_appx,
        get_installed_appx_versions,
        get_local_devices,
        save_devices_to_csv,
    )
    from .downloader import (
        download_file,
        download_via_browser,
        extract_cab,
        selenium_available,
        semi_automatic_download,
        wait_for_new_download,
    )
except ModuleNotFoundError:

    def _windows_only(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Эта команда доступна только в ОС Windows")

    (
        get_installed_appx,
        get_installed_appx_versions,
        get_local_devices,
        save_devices_to_csv,
        download_file,
        download_via_browser,
        extract_cab,
        selenium_available,
        semi_automatic_download,
        wait_for_new_download,
    ) = (_windows_only,) * 10

log = logging.getLogger(__name__)


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


def _offer_install(bat_path: Path, ask: bool, prompt: str | None = None) -> None:
    """Предлагает запустить установочный скрипт (UAC), когда он найден."""
    if not bat_path.is_file():
        print(f"Установочный скрипт не найден: {bat_path}")
        return
    print(f"\nУстановочный скрипт: {bat_path}")
    if not ask:
        print("Установка пропущена (--no-install): запустите скрипт от администратора.")
        return
    confirm_msg = (
        prompt
        if prompt is not None
        else "\nЗапустить установку сейчас? Потребуются права администратора. [y/N]: "
    )
    if input(confirm_msg).strip().lower() == "y":
        run_install_bat(bat_path, confirm=True)
    else:
        print(f"Запустите от администратора самостоятельно:\n  {bat_path}")


def _target_arch(args: argparse.Namespace, template_dir: Path | None) -> str:
    """Целевая архитектура пакетов UWP: --arch, затем из шаблона, затем x64."""
    arch = getattr(args, "arch", None)
    if arch:
        return str(arch).lower()
    if template_dir is not None:
        return str(read_device_meta(template_dir).get("arch", "x64")).lower()
    return "x64"


def _run_uwp_download(
    args: argparse.Namespace, template_dir: Path, apps_root: Path
) -> None:
    """Скачивает свежие UWP-пакеты шаблона в apps_root (шаг run-пайплайна)."""
    seeds = uwp_apps_from_template(template_dir)
    if not seeds:
        print("В шаблоне нет UWP-приложений — обновление пропущено.")
        return
    print(f"UWP-приложений в шаблоне: {len(seeds)}")
    results = download_uwp_updates(
        seeds,
        apps_root,
        args.ring,
        force=args.force,
        arch=_target_arch(args, template_dir),
    )
    status_names = {
        "done": "скачано",
        "up_to_date": "актуально",
        "not_found": "не найдено",
        "error": "ошибка",
    }
    for result in results:
        status = status_names.get(result.state, result.state)
        message = (
            f" | {result.message}" if result.message and result.state != "done" else ""
        )
        print(f"  - {result.app:40s} | {status:10s} | v{result.version}{message}")
    print(f"Готово. Пакеты: {apps_root}")


def _build_from_args(
    args: argparse.Namespace,
    template_dir: Path,
    out_dir: Path,
    drivers_dir: Path,
    apps_dir: Path,
) -> MergeReport:
    """Собирает пакет из шаблона: снимки системы + финальный build."""
    snapshot_hwids: list[str] | None = None
    if getattr(args, "snapshot", False):
        try:
            local_devices = get_local_devices()
        except RuntimeError as exc:
            print(f"Снимок недоступен: {exc}")
        else:
            snapshot_hwids = (
                [dev.hwid for dev in local_devices if dev.hwid]
                if local_devices is not None
                else None
            )
    if getattr(args, "snapshot_apps", False):
        try:
            system_apps = get_installed_appx()
        except RuntimeError as exc:
            print(f"Снимок приложений недоступен: {exc}")
        else:
            added = (
                merge_snapshot_apps(template_dir, system_apps)
                if system_apps is not None
                else []
            )
            print(f"Снимок UWP-приложений: добавлено в шаблон: {len(added)}")
    return build_oem_package(
        template_dir,
        out_dir=out_dir,
        drivers_dir=drivers_dir,
        apps_dir=apps_dir,
        snapshot_hwids=snapshot_hwids,
        arch=_target_arch(args, template_dir),
    )


def _run_template_umbrella(
    args: argparse.Namespace, template_dir: Path
) -> None:
    """Run --template-dir: driver-update → uwp-update → build → установка."""
    device = args.device or template_dir.name
    out_dir = Path(args.out_dir) if args.out_dir else Path("Output") / device
    drivers_dir = (
        Path(args.drivers_dir)
        if args.drivers_dir
        else Path("Cache") / device / "Drivers"
    )
    apps_dir = Path(args.apps_dir) if args.apps_dir else Path("Cache") / device / "Apps"

    if args.no_drivers:
        print(
            f"\n[1/3] Скачивание драйверов пропущено (--no-drivers): "
            f"кэш {drivers_dir}"
        )
    else:
        print("\n[1/3] Обновление драйверов (driver-update)...")
        update_template_drivers(
            template_dir,
            drivers_dir,
            options=DownloadOptions(
                browser=args.browser,
                headless=args.headless,
                force=args.force,
            ),
        )

    if args.no_apps:
        print(f"\n[2/3] Обновление UWP пропущено (--no-apps): кэш {apps_dir}")
    else:
        print("\n[2/3] Обновление UWP-приложений (uwp-update)...")
        _run_uwp_download(args, template_dir, apps_dir)

    print("\n[3/3] Сборка пакета (build)...")
    report = _build_from_args(args, template_dir, out_dir, drivers_dir, apps_dir)
    _print_build_report(report)
    print(f"\nИтоговый пакет: {report.out_dir}")
    _offer_install(report.out_dir / FACTORY_BAT_NAME, ask=not args.no_install)


def _run_rolling_umbrella(args: argparse.Namespace) -> None:
    """Run без шаблона: rolling-сборка под текущую систему + установка."""
    seeds = _rolling_app_seeds(args)
    if seeds:
        print(f"UWP-приложений: {len(seeds)}")
    out_dir = Path(args.out_dir) if args.out_dir else Path("Output") / "current"
    new_updates_count = run_rolling(
        out_dir,
        Path("Cache") / "current",
        apps_seeds=seeds,
        options=DownloadOptions(
            browser=args.browser,
            headless=args.headless,
            ring=args.ring,
            force=args.force,
        ),
    )
    drivers_bat = out_dir / "install_drivers.bat"
    apps_bat = out_dir / "install_apps.bat"
    has_drivers = drivers_bat.is_file()
    has_apps = apps_bat.is_file()

    if not has_drivers and not has_apps:
        print("\nФайлов для установки не найдено.")
        return

    if new_updates_count == 0 and not getattr(args, "force", False):
        scripts = [f"  {p}" for p in (drivers_bat, apps_bat) if p.is_file()]
        print(
            "\nВсе драйверы и приложения уже актуальны. "
            "Скрипты установки сохранены на случай ручной переустановки:\n"
            + "\n".join(scripts)
        )
        return

    if has_drivers:
        _offer_install(
            drivers_bat,
            ask=not args.no_install,
            prompt=(
                "\nЗапустить установку драйверов сейчас? "
                "Потребуются права администратора. [y/N]: "
            ),
        )
    if has_apps:
        _offer_install(
            apps_bat,
            ask=not args.no_install,
            prompt=(
                "\nЗапустить установку приложений сейчас? "
                "Потребуются права администратора. [y/N]: "
            ),
        )


def _handle_run(args: argparse.Namespace) -> None:
    """Единый пайплайн обновления: драйверы + UWP + сборка (шаблон или rolling)."""
    template_dir = _resolve_template(args)
    if template_dir is None:
        _run_rolling_umbrella(args)
        return
    _run_template_umbrella(args, template_dir.resolve())


# =============================================================================
# CLI
# =============================================================================


def _add_browser_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--browser", choices=["firefox", "chrome"], default="firefox")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)


def _add_run_args(p: argparse.ArgumentParser) -> None:
    """Аргументы команды run: пайплайн драйверы → UWP → сборка → установка."""
    p.add_argument(
        "--template-dir", default=None, help="Шаблон устройства: Template/<Устройство>"
    )
    p.add_argument(
        "--device",
        default=None,
        help="Имя устройства (папка шаблона); Template/<device> при --template-dir",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Куда класть пакет (по умолчанию Output/<device>)",
    )
    p.add_argument(
        "--drivers-dir",
        default=None,
        help="Кэш драйверов (по умолчанию Cache/<device>/Drivers)",
    )
    p.add_argument(
        "--apps-dir",
        default=None,
        help="Кэш UWP-приложений (по умолчанию Cache/<device>/Apps)",
    )
    p.add_argument(
        "--snapshot",
        action="store_true",
        help="Дополнить .device.json устройствами из WMI (только Windows)",
    )
    p.add_argument(
        "--snapshot-apps",
        action="store_true",
        help="Дополнить .device.json установленными UWP-приложениями (только Windows)",
    )
    p.add_argument("--apps", action="append", help="Имена/ссылки приложений (rolling)")
    p.add_argument("--ring", default="Retail", choices=("Retail", "FVU"))
    p.add_argument("--force", action="store_true", help="Качать заново, игнорируя кэш")
    p.add_argument(
        "--no-drivers",
        action="store_true",
        help="Не качать драйверы: сборка из существующего кэша",
    )
    p.add_argument(
        "--no-apps",
        action="store_true",
        help="Не обновлять UWP-приложения: сборка из существующего кэша",
    )
    p.add_argument(
        "--no-install",
        action="store_true",
        help="Не предлагать установку после сборки",
    )
    p.add_argument(
        "--arch",
        default=None,
        choices=("x64", "x86", "arm", "arm64"),
        help="Целевая архитектура пакетов UWP (по умолчанию из шаблона)",
    )
    _add_browser_args(p)


def build_arg_parser() -> argparse.ArgumentParser:
    """Создает парсер аргументов командной строки для CLI."""
    parser = argparse.ArgumentParser(description="Driver Update Helper")
    parser.add_argument(
        "--verbose", action="store_true", help="Подробный лог для отладки"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "run",
        help=(
            "Единый пайплайн: обновление драйверов → UWP → сборка пакета "
            "(--template-dir) или rolling под текущую систему + установка"
        ),
    )
    _add_run_args(p)

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

    _add_package_subparsers(sub)

    return parser


def _add_package_subparsers(sub: argparse._SubParsersAction) -> None:
    """Регистрирует команды сборки пакета: build/rolling/driver-update/uwp-update."""
    p = sub.add_parser(
        "build",
        help=(
            "Сборка пакета: из шаблона (--template-dir, кроссплатформенно) "
            "или rolling под текущую систему (без шаблона, Windows)"
        ),
    )
    p.add_argument(
        "--template-dir", default=None, help="Шаблон устройства: Template/<Устройство>"
    )
    p.add_argument(
        "--device",
        default=None,
        help="Имя устройства (папка шаблона); Template/<device> при --template-dir",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Куда класть пакет (по умолчанию Output/<device>)",
    )
    p.add_argument(
        "--drivers-dir",
        default=None,
        help="Кэш драйверов (по умолчанию Cache/<device>/Drivers)",
    )
    p.add_argument(
        "--apps-dir",
        default=None,
        help="Кэш UWP-приложений (по умолчанию Cache/<device>/Apps)",
    )
    p.add_argument(
        "--snapshot",
        action="store_true",
        help="Дополнить .device.json устройствами из WMI (только Windows)",
    )
    p.add_argument(
        "--snapshot-apps",
        action="store_true",
        help="Дополнить .device.json установленными UWP-приложениями (только Windows)",
    )
    p.add_argument("--apps", action="append", help="Имена/ссылки приложений (rolling)")
    p.add_argument("--ring", default="Retail", choices=("Retail", "FVU"))
    p.add_argument("--force", action="store_true", help="Качать заново, игнорируя кэш")
    p.add_argument(
        "--arch",
        default=None,
        choices=("x64", "x86", "arm", "arm64"),
        help="Целевая архитектура пакетов UWP (по умолчанию из шаблона/x64)",
    )
    p.add_argument(
        "--no-system-apps",
        action="store_true",
        help="Не подставлять установленные UWP-приложения (rolling)",
    )
    _add_browser_args(p)

    p = sub.add_parser(
        "rolling",
        help="Rolling-сборка под текущую систему (без шаблона, только Windows)",
    )
    p.add_argument(
        "--out-dir", default=str(Path("Output") / "current"), help="Куда класть пакет"
    )
    p.add_argument(
        "--cache-dir",
        default=str(Path("Cache") / "current"),
        help="Каталог кэша",
    )
    p.add_argument(
        "--apps", action="append", help="Имена/ссылки приложений (можно несколько раз)"
    )
    p.add_argument("--ring", default="Retail", choices=("Retail", "FVU"))
    p.add_argument("--force", action="store_true", help="Качать заново, игнорируя кэш")
    p.add_argument(
        "--no-system-apps",
        action="store_true",
        help="Не подставлять установленные UWP-приложения",
    )
    _add_browser_args(p)

    p = sub.add_parser(
        "driver-update",
        help="Скачать свежие драйверы по HWID из шаблона в кэш (Windows)",
    )
    p.add_argument(
        "--template-dir",
        required=True,
        help="Шаблон устройства: Template/<Устройство>",
    )
    p.add_argument(
        "--drivers-dir",
        default=None,
        help="Кэш драйверов (по умолчанию Cache/<device>/Drivers)",
    )
    p.add_argument("--force", action="store_true", help="Качать заново, игнорируя кэш")
    _add_browser_args(p)

    p = sub.add_parser(
        "uwp-update",
        help=(
            "Скачать свежие UWP-пакеты приложений из MS Store в кэш "
            "(по умолчанию Cache/<device>/Apps)"
        ),
    )
    p.add_argument(
        "--template-dir",
        default=None,
        help="Шаблон: UWP-компоненты берутся из него автоматически",
    )
    p.add_argument(
        "--apps", action="append", help="Имена/ссылки приложений (можно несколько раз)"
    )
    p.add_argument(
        "--apps-root",
        default=None,
        help="Куда складывать пакеты (по умолчанию Cache/<device>/Apps)",
    )
    p.add_argument("--ring", default="Retail", choices=("Retail", "FVU"))
    p.add_argument("--force", action="store_true", help="Качать заново, игнорируя кэш")
    p.add_argument(
        "--arch",
        default=None,
        choices=("x64", "x86", "arm", "arm64"),
        help="Целевая архитектура пакетов UWP (по умолчанию из шаблона/x64)",
    )
    p.add_argument(
        "--snapshot-apps",
        action="store_true",
        help=(
            "Записать установленные UWP-приложения системы в шаблон "
            "(source: system) и обновить их (только Windows)"
        ),
    )
    p.add_argument(
        "--add-app",
        action="append",
        default=None,
        help=(
            "Store ID или ссылка приложения: зарегистрировать его в шаблоне "
            "(source: system) и обновлять при каждом запуске (нужен "
            "--template-dir), например 9NF8H0H7WMLT"
        ),
    )


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


def _resolve_template(args: argparse.Namespace) -> Path | None:
    """Определяет папку шаблона по --template-dir/--device или None (rolling)."""
    if args.template_dir:
        return Path(args.template_dir)
    if getattr(args, "device", None):
        return Path("Template") / args.device
    return None


def _print_build_report(report) -> None:
    print(f"Компонентов обработано: {len(report.components)}")
    print(f"  - перезаписано файлов: {report.files_overwritten}")
    print(f"  - добавлено файлов:    {report.files_added}")
    print(f"  - заменено UWP-пакетов: {report.packages_replaced}")
    if report.ignored:
        print(f"Пропущено (blacklist): {len(report.ignored)}")
        for name in report.ignored:
            print(f"  - {name}")
    if report.unmatched:
        print(f"Без сопоставления ({len(report.unmatched)}):")
        for item in report.unmatched:
            print(f"  - {item}")
    for error in report.errors:
        print(f"ОШИБКА: {error}")


def _handle_build(args: argparse.Namespace) -> None:
    """Собирает пакет: из шаблона или rolling под текущую систему."""
    template_dir = _resolve_template(args)
    if template_dir is None:
        seeds = _rolling_app_seeds(args)
        run_rolling(
            Path(args.out_dir) if args.out_dir else Path("Output") / "current",
            Path("Cache") / "current",
            apps_seeds=seeds,
            options=DownloadOptions(
                browser=args.browser,
                headless=args.headless,
                ring=args.ring,
                force=args.force,
            ),
        )
        return

    template_dir = template_dir.resolve()
    device = args.device or template_dir.name
    out_dir = Path(args.out_dir) if args.out_dir else Path("Output") / device
    drivers_dir = (
        Path(args.drivers_dir)
        if args.drivers_dir
        else Path("Cache") / device / "Drivers"
    )
    apps_dir = Path(args.apps_dir) if args.apps_dir else Path("Cache") / device / "Apps"

    report = _build_from_args(args, template_dir, out_dir, drivers_dir, apps_dir)
    _print_build_report(report)
    print(f"\nИтоговый пакет: {report.out_dir}")


def _handle_rolling(args: argparse.Namespace) -> None:
    """Rolling-сборка под текущую систему (без шаблона, только Windows)."""
    run_rolling(
        Path(args.out_dir),
        Path(args.cache_dir),
        apps_seeds=_rolling_app_seeds(args),
        options=DownloadOptions(
            browser=args.browser,
            headless=args.headless,
            ring=args.ring,
            force=args.force,
        ),
    )


def _handle_driver_update(args: argparse.Namespace) -> None:
    """Качает свежие драйверы по HWID из шаблона в Cache/<device>/Drivers."""
    template_dir = Path(args.template_dir).resolve()
    device = template_dir.name
    drivers_dir = Path(args.drivers_dir) if args.drivers_dir else (
        Path("Cache") / device / "Drivers"
    )
    update_template_drivers(
        template_dir,
        drivers_dir,
        options=DownloadOptions(
            browser=args.browser,
            headless=args.headless,
            force=args.force,
        ),
    )


def _apps_seeds(args: argparse.Namespace) -> list[tuple[str, str]]:
    seeds: list[tuple[str, str]] = []
    for raw in getattr(args, "apps", None) or []:
        for item in raw.split(","):
            part = item.strip()
            if part:
                seeds.append((part, part))
    return seeds


def _rolling_app_seeds(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Seeds приложений для rolling-сборки: --apps + авто из системы."""
    system_apps = (
        None if getattr(args, "no_system_apps", False) else get_installed_appx()
    )
    seeds = compose_app_seeds(system_apps, _apps_seeds(args))
    if seeds:
        print(f"UWP-приложений: {len(seeds)}")
    return seeds


def _register_store_app(
    session: requests.Session, template_dir: Path, value: str
) -> None:
    """Резолвит Store ID и добавляет приложение в .device.json шаблона."""
    pid = product_id_from(value)
    if pid is None:
        print(f"--add-app {value}: не похоже на Store ID или ссылку store")
        return
    try:
        resolved = resolve_store_product(pid, session)
    except requests.RequestException as exc:
        print(f"--add-app {value}: Store недоступен: {exc}")
        return
    if resolved is None:
        print(f"--add-app {value}: приложение не найдено в Store")
        return
    title, family = resolved
    added = merge_snapshot_apps(template_dir, [(title, family)])
    if added:
        print(f"Добавлено в шаблон: {title} ({family})")
    else:
        print(f"Уже в шаблоне: {title} ({family})")


def _handle_uwp_add_apps(args: argparse.Namespace) -> None:
    """Обрабатывает --add-app: регистрирует Store-приложения в шаблоне."""
    if not args.add_app:
        return
    if not args.template_dir:
        print("--add-app требует --template-dir.")
        return
    session = get_http_session()
    template_dir = Path(args.template_dir)
    for raw in args.add_app:
        for value in (item.strip() for item in str(raw).split(",") if item.strip()):
            _register_store_app(session, template_dir, value)


def _handle_uwp_update(args: argparse.Namespace) -> None:
    """Скачивает свежие UWP-пакеты: из шаблона и/или по списку --apps."""
    _handle_uwp_add_apps(args)

    if args.snapshot_apps:
        if not args.template_dir:
            print("--snapshot-apps требует --template-dir.")
            return
        try:
            system_apps = get_installed_appx()
        except RuntimeError as exc:
            print(f"Снимок приложений недоступен: {exc}")
            return
        added = merge_snapshot_apps(Path(args.template_dir), system_apps)
        print(f"Снимок UWP-приложений: добавлено в шаблон: {len(added)}")

    seeds = _apps_seeds(args)
    if args.template_dir:
        seeds.extend(uwp_apps_from_template(Path(args.template_dir)))
    elif not seeds:
        print("Нет приложений: укажите --apps или --template-dir.")
        return

    if args.template_dir:
        device = Path(args.template_dir).name
    else:
        device = seeds[0][0] if seeds else "current"
    apps_root = (
        Path(args.apps_root) if args.apps_root else Path("Cache") / device / "Apps"
    )

    results = download_uwp_updates(
        seeds,
        apps_root,
        args.ring,
        force=args.force,
        arch=_target_arch(args, Path(args.template_dir) if args.template_dir else None),
    )
    status_names = {
        "done": "скачано",
        "up_to_date": "актуально",
        "not_found": "не найдено",
        "error": "ошибка",
    }
    for result in results:
        status = status_names.get(result.state, result.state)
        if result.message and result.state != "done":
            message = f" | {result.message}"
        else:
            message = ""
        print(f"  - {result.app:40s} | {status:10s} | v{result.version}{message}")
    print(f"\nГотово. Пакеты: {apps_root}")


def _execute_command(args: argparse.Namespace) -> None:
    """Диспатчер подкоманд CLI."""
    session_lazy = None

    def _get_session():
        nonlocal session_lazy
        if session_lazy is None:
            session_lazy = get_http_session()
        return session_lazy

    handlers = {
        "run": lambda: _handle_run(args),
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
        "build": lambda: _handle_build(args),
        "rolling": lambda: _handle_rolling(args),
        "driver-update": lambda: _handle_driver_update(args),
        "uwp-update": lambda: _handle_uwp_update(args),
    }

    handler = handlers.get(args.command)
    if handler:
        handler()


def main() -> None:
    """Главная точка входа в приложение."""
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
        "build",
        "rolling",
        "driver-update",
        "uwp-update",
        "-h",
        "--help",
    }

    if not any(arg in known_commands for arg in args_list):
        args_list.append("run")

    args = build_arg_parser().parse_args(args_list)

    if sys.platform != "win32":
        if args.command == "run":
            template_dir = _resolve_template(args)
            portable = template_dir is not None and getattr(
                args, "no_drivers", False
            )
        elif args.command == "build":
            portable = _resolve_template(args) is not None
        elif args.command in ("rolling", "driver-update"):
            portable = False
        else:
            portable = args.command in ("build", "uwp-update")
        if not portable:
            print("Ошибка: Скрипт может обновлять драйверы только в ОС Windows!")
            sys.exit(1)

    setup_logging(verbose=args.verbose)
    log.info("Запуск приложения...")
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _execute_command(args)


if __name__ == "__main__":
    main()
