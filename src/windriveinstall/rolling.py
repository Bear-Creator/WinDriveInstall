"""Rolling-сборка: драйверы и UWP-приложения под текущую систему без шаблона.

Только Windows: используются WMI (win32com) и Selenium. Windows-зависимые
импорты закрыты try/except на верхнем уровне (как в __main__) — модуль
остаётся импортируемым на Linux, а на Windows получает рабочие функции.

Маршруты данных:
    Cache/<Dev>/Drivers — кэш скачанных классических драйверов (каталог MS),
    Cache/<Dev>/Apps    — кэш UWP-пакетов,
    Output/<Dev>        — итоговый пакет (Drivers/, Apps/, install_drivers.bat).

driver-update --template … — то же скачивание, но HWID берутся из шаблона
(.device.json), а не из текущего состояния WMI: «уже обновлено» на системе
не блокирует загрузку.
"""

import datetime as dt
import json
import logging
import platform
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .builder import (
    DEPS_DIR_NAME,
    DEPS_META_NAME,
    copy_fresh_apps,
    get_bundle_inner_version,
    parse_package_family,
    read_device_meta,
)
from .catalog import search_catalog_with_fallback
from .models import CatalogEntry, LocalDevice
from .slim import apply_second_run
from .utils import (
    get_http_session,
    is_app_version_up_to_date,
    is_newer,
    sanitize_folder_name,
    version_tuple,
)
from .uwp import _package_version, download_uwp_updates

try:
    from .devices import get_installed_appx_versions, get_local_devices
    from .downloader import build_device_folder, download_candidate
except ModuleNotFoundError:

    def _windows_only(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Эта команда доступна только в ОС Windows")

    (
        get_installed_appx_versions,
        get_local_devices,
        build_device_folder,
        download_candidate,
    ) = (_windows_only,) * 4

log = logging.getLogger(__name__)


@dataclass
class DownloadOptions:
    """Параметры закачки: браузер, кольцо обновлений и поведение с кэшем."""

    browser: str = "firefox"
    headless: bool = True
    ring: str = "Retail"
    force: bool = False


def _best_catalog_entries(
    devices: list[LocalDevice], session
) -> list[tuple]:
    """Лучшие записи каталога для HWID устройств (без порога «актуально на ПК»)."""
    results: list[tuple] = []
    for dev in devices:
        matched_hwid, entries = search_catalog_with_fallback(
            dev.hwid_candidates, session=session
        )
        if not entries:
            continue
        best = max(entries, key=lambda e: (version_tuple(e.version), e.last_updated))
        results.append((dev, best, matched_hwid))
    return results


def _is_candidate_newer(dev: LocalDevice, entry: CatalogEntry) -> bool:
    """Проверяет, новее ли драйвер из каталога, чем установленный на устройстве."""
    best_ver = entry.version
    if best_ver.strip().lower() in ("n/a", "none", ""):
        m = re.findall(r"\b\d+\.\d+(?:\.\d+)+\b", entry.title)
        if m:
            best_ver = m[-1]
    return is_newer(
        best_ver,
        dev.driver_version,
        remote_date=entry.last_updated,
        local_date=dev.driver_date,
    )


def _leaf_has_inf(folder: Path) -> bool:
    """Есть ли в папке кэша .inf или маркер успешной закачки (.download_ok)."""
    return folder.is_dir() and (
        any(folder.rglob("*.inf")) or (folder / ".download_ok").is_file()
    )


def _portable_bat_paths(inf_files: list[Path], root: Path) -> list[str]:
    """Относительные пути .inf от корня пакета для bat (pnputil, %~dp0)."""
    lines: list[str] = []
    for inf in inf_files:
        rel = inf.relative_to(root)
        lines.append(f'pnputil /add-driver "%~dp0{rel.as_posix()}" /install')
    return lines


PACKAGE_EXTS = (".msixbundle", ".appxbundle", ".msix", ".appx")
BUNDLE_EXTS = (".msixbundle", ".appxbundle")


def generate_package_install_bat(inf_files: list[Path], root: Path) -> Path:
    """Пишет install_drivers.bat с %~dp0-относительными путями .inf."""
    root = Path(root)
    header = [
        "@echo off",
        "net session >nul 2>&1",
        "if %errorLevel% neq 0 (",
        "    echo [ERROR] Please run this script as ADMINISTRATOR!",
        "    pause",
        "    exit /b",
        ")",
        "echo Installing drivers via pnputil...",
    ]
    lines = header + _portable_bat_paths(inf_files, root) + ["", "echo Done.", "pause"]
    bat_path = root / "install_drivers.bat"
    bat_path.write_text("\r\n".join(lines), encoding="utf-8")
    return bat_path


def _app_package_files(app_dir: Path) -> list[Path]:
    """Возвращает главные пакеты приложения (бандлы предпочтительнее)."""
    all_pkgs = [
        p
        for p in app_dir.iterdir()
        if p.is_file() and p.suffix.lower() in PACKAGE_EXTS
    ]
    bundles = [p for p in all_pkgs if p.suffix.lower() in BUNDLE_EXTS]
    return bundles if bundles else all_pkgs


def _resolve_app_dependencies(
    app_dir: Path, apps_dir: Path, apps_cache: Path | None
) -> list[Path]:
    """Определяет файлы зависимостей приложения из общего пула или локально."""
    dep_names: list[str] = []
    meta_candidates: list[Path] = []
    if apps_cache is not None:
        meta_candidates.append(apps_cache / app_dir.name / DEPS_META_NAME)
    meta_candidates.append(app_dir / DEPS_META_NAME)
    for meta_path in meta_candidates:
        if meta_path.is_file():
            try:
                raw = json.loads(meta_path.read_text("utf-8"))
                if isinstance(raw, list):
                    dep_names = [str(x) for x in raw if x]
                    break
            except (OSError, ValueError):
                pass

    dep_paths: list[Path] = []
    if dep_names:
        for name in dep_names:
            shared_dep = apps_dir / DEPS_DIR_NAME / name
            local_dep = app_dir / DEPS_DIR_NAME / name
            if shared_dep.is_file():
                dep_paths.append(shared_dep)
            elif local_dep.is_file():
                dep_paths.append(local_dep)
    elif (app_dir / DEPS_DIR_NAME).is_dir():
        dep_paths.extend(
            p
            for p in sorted((app_dir / DEPS_DIR_NAME).iterdir())
            if p.is_file() and p.suffix.lower() in PACKAGE_EXTS
        )
    return list(dict.fromkeys(dep_paths))


def _resolve_app_license(app_dir: Path, root: Path) -> str:
    """Возвращает параметр лицензии для DISM (/LicensePath или /SkipLicense)."""
    license_files = [
        p
        for p in app_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".xml" and "license" in p.name.lower()
    ]
    if license_files:
        rel = license_files[0].relative_to(root).as_posix().replace("/", "\\")
        return f'/LicensePath:"%~dp0{rel}"'
    return "/SkipLicense"


def _win_rel_path(target: Path, root: Path) -> str:
    """Возвращает Windows-путь с обратными слэшами относительно root."""
    return target.relative_to(root).as_posix().replace("/", "\\")


def _app_install_commands(
    app_name: str,
    pkg: Path,
    dep_paths: list[Path],
    lic_flag: str,
    root: Path,
) -> list[str]:
    """Формирует команды установки UWP (Add-AppxPackage + DISM provisioning)."""
    pkg_rel = _win_rel_path(pkg, root)
    lines = [
        "",
        f"echo Installing: {app_name}...",
    ]
    for dep in dep_paths:
        dep_rel = _win_rel_path(dep, root)
        lines.append(
            f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-Command "Add-AppxPackage -Path \'%~dp0{dep_rel}\' '
            f'-ForceApplicationShutdown -ForceUpdateFromAnyVersion" >nul 2>&1'
        )

    ps_cmd = (
        f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass '
        f'-Command "Add-AppxPackage -Path \'%~dp0{pkg_rel}\' '
        f'-ForceApplicationShutdown -ForceUpdateFromAnyVersion"'
    )
    lines.extend([
        ps_cmd,
        "if %errorlevel% neq 0 (",
        f"    echo [WARNING] Add-AppxPackage failed for {app_name}. "
        "Trying DISM provisioning...",
        ")",
    ])

    dep_parts = [
        f'/DependencyPackagePath:"%~dp0{_win_rel_path(dep, root)}"'
        for dep in dep_paths
    ]
    dep_str = (" " + " ".join(dep_parts)) if dep_parts else ""
    dism_cmd = (
        f"dism /Online /Add-ProvisionedAppxPackage "
        f'/PackagePath:"%~dp0{pkg_rel}"{dep_str} '
        f"/StubPackageOption:InstallFull {lic_flag} >nul 2>&1"
    )
    lines.append(dism_cmd)
    return lines


def generate_apps_install_bat(
    root: Path,
    apps_cache: Path | None = None,
    installed_versions: Mapping[str, str] | None = None,
    force: bool = False,
) -> Path | None:
    """Пишет install_apps.bat для системной установки UWP-приложений через DISM.

    Находит пакеты приложений в root/Apps/<App>/, сопоставляет зависимости
    (через .deps.json из apps_cache или Apps/<App>) с файлами в
    Apps/Dependencies, и генерирует команды dism /Online /Add-ProvisionedAppxPackage.
    Если приложение уже актуально в системе и force=False, оно пропускается.
    """
    root = Path(root)
    apps_dir = root / "Apps"
    if not apps_dir.is_dir():
        return None

    app_dirs = sorted(
        d for d in apps_dir.iterdir() if d.is_dir() and d.name != DEPS_DIR_NAME
    )
    if not app_dirs:
        return None

    lines: list[str] = [
        "@echo off",
        "net session >nul 2>&1",
        "if %errorLevel% neq 0 (",
        "    echo [ERROR] Please run this script as ADMINISTRATOR!",
        "    pause",
        "    exit /b 1",
        ")",
        "echo Installing applications via DISM...",
    ]

    installed_any = False
    for app_dir in app_dirs:
        pkgs = _app_package_files(app_dir)
        if not pkgs:
            continue

        dep_paths = _resolve_app_dependencies(app_dir, apps_dir, apps_cache)
        lic_flag = _resolve_app_license(app_dir, root)

        for pkg in pkgs:
            if not force and installed_versions:
                pkg_family = parse_package_family(pkg.name)
                pkg_ver = get_bundle_inner_version(pkg) or _package_version(pkg.name)
                inst_ver = (
                    installed_versions.get(pkg_family.lower())
                    if pkg_family
                    else None
                )
                if not inst_ver:
                    inst_ver = installed_versions.get(app_dir.name.lower())
                if inst_ver and is_app_version_up_to_date(inst_ver, pkg_ver):
                    continue

            lines.extend(
                _app_install_commands(app_dir.name, pkg, dep_paths, lic_flag, root)
            )
            installed_any = True

    if not installed_any:
        return None

    lines.extend(
        ["", "echo.", "echo All application installation tasks completed.", "pause"]
    )
    bat_path = root / "install_apps.bat"
    bat_path.write_text("\r\n".join(lines), encoding="utf-8")
    return bat_path


def compose_app_seeds(
    system_apps: list[tuple[str, str]] | None,
    cli_seeds: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Объединяет явные --apps и системные приложения, дедуплицируя по цели.

    Явные seed'ы идут первыми, системные добавляются после. Ключ дедупликации —
    target (семейство/ссылка), порядок первого появления сохраняется.
    """
    seeds = list(cli_seeds)
    seen = {target.lower() for _name, target in seeds}
    for name, target in system_apps or []:
        if target.lower() in seen:
            continue
        seeds.append((name, target))
        seen.add(target.lower())
    return seeds


def _download_candidates(
    candidates: list[tuple], download_root: Path, options: DownloadOptions
) -> int:
    """Качает кандидатов в download_root, пропуская уже имеющиеся в кэше."""
    downloaded = 0
    for dev, entry, matched_hwid in candidates:
        dest = build_device_folder(download_root, dev, entry=entry)
        if not options.force and _leaf_has_inf(dest):
            print(f"- {dev.name}: уже в кэше (v{entry.version}), пропуск")
            continue
        result = download_candidate(
            dev,
            entry,
            matched_hwid,
            download_root,
            options.browser,
            options.headless,
        )
        if result is None:
            continue
        if result.status == "ok":
            downloaded += 1
            print(f"- {dev.name}: v{entry.version} -> {result.dest_folder}")
        else:
            print(f"- {dev.name}: надо вручную ({result.status}): {result.error or ''}")
    return downloaded


def _copy_driver_leaves(cache_drivers: Path, out_root: Path) -> list[Path]:
    """Копирует скачанные листья драйверов из кэша в пакет, возвращает .inf-ы."""
    out_drivers = out_root / "Drivers"
    inf_files: list[Path] = []
    for leaf in sorted(p for p in cache_drivers.rglob("*.inf")):
        src = leaf.parent
        rel = src.relative_to(cache_drivers)
        dest = out_drivers / rel
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        marker = dest / ".download_ok"
        if marker.exists():
            marker.unlink()
        inf_files.extend(dest.rglob("*.inf"))
    return inf_files


def _local_arch() -> str:
    """Архитектура текущей системы в терминах пакетов UWP (amd64 -> x64)."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "x64"


def run_rolling(
    out_dir: Path,
    cache_dir: Path,
    apps_seeds: list[tuple[str, str]] | None = None,
    options: DownloadOptions | None = None,
) -> int:
    """Собирает пакет под текущую систему: драйверы по WMI-устройствам + UWP.

    Результат: out_dir (по умолчанию Output/current) с Drivers/, Apps/ (если
    заданы apps_seeds) и install_drivers.bat на pnputil.
    Возвращает количество новых (скачанных в этом сеансе) обновлений.
    """
    options = options or DownloadOptions()
    session = get_http_session()
    drivers_cache = cache_dir / "Drivers"
    apps_cache = cache_dir / "Apps"

    print("Сканирую устройства (WMI)...")
    devices = get_local_devices()
    print(f"Устройств: {len(devices)}")

    downloaded_drivers = 0
    candidates = _best_catalog_entries(devices, session)
    if not options.force:
        candidates = [c for c in candidates if _is_candidate_newer(c[0], c[1])]
    if not candidates:
        print("Обновления драйверов не найдены.")
    else:
        print(f"Найдено обновлений: {len(candidates)}")
        downloaded_drivers = _download_candidates(candidates, drivers_cache, options)

    downloaded_apps = 0
    try:
        installed_versions = get_installed_appx_versions()
    except RuntimeError:
        installed_versions = {}
    if apps_seeds:
        results = download_uwp_updates(
            apps_seeds,
            apps_cache,
            ring=options.ring,
            force=options.force,
            arch=_local_arch(),
            installed_versions=installed_versions,
        )
        for r in results:
            if r.state == "done":
                downloaded_apps += 1
            tail = f" — {r.message}" if r.message and r.state != "done" else ""
            print(f"- {r.app}: {r.state} v{r.version}{tail}")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    inf_files = (
        _copy_driver_leaves(drivers_cache, out_dir) if drivers_cache.is_dir() else []
    )
    if inf_files:
        bat = generate_package_install_bat(inf_files, out_dir)
        print(f"Установочный скрипт (драйверы): {bat}")
    if apps_cache.is_dir() and any(apps_cache.iterdir()):
        copy_fresh_apps(apps_cache, out_dir / "Apps", arch=_local_arch())
    apps_bat = generate_apps_install_bat(
        out_dir,
        apps_cache=apps_cache,
        installed_versions=installed_versions,
        force=options.force,
    )
    if apps_bat:
        print(f"Установочный скрипт (приложения): {apps_bat}")

    print(f"\nГотово. Пакет: {out_dir}")
    return downloaded_drivers + downloaded_apps


def _meta_hwids_to_devices(template_dir: Path) -> list[LocalDevice]:
    """Превращает HWID из .device.json шаблона в устройства для поиска в каталоге."""
    meta = read_device_meta(template_dir)
    devices: list[LocalDevice] = []
    for comp in meta.get("components", []):
        for hwid in comp.get("hwids", []):
            devices.append(
                LocalDevice(
                    name=sanitize_folder_name(str(comp.get("rel_path", hwid))),
                    hwid=hwid,
                    driver_version="0.0.0.0",
                    manufacturer=sanitize_folder_name(str(comp.get("kind", "Driver"))),
                    device_class="Unknown",
                    driver_date=dt.date.min,
                    hwid_candidates=[hwid],
                )
            )
    return devices


def update_template_drivers(
    template_dir: Path,
    cache_drivers: Path,
    options: DownloadOptions | None = None,
) -> None:
    """Качает свежие драйверы по HWID из шаблона в Cache/<Dev>/Drivers.

    После скачивания листьев повторно ужимает шаблон (apply_second_run):
    у pnputil-классики, для которой нашёлся свежий лист, вырезается старый
    payload, а без листа — компонент удаляется.
    """
    options = options or DownloadOptions()
    session = get_http_session()
    devices = _meta_hwids_to_devices(template_dir)
    if not devices:
        print(f"Нет HWID в шаблоне (нет .device.json): {template_dir}")
        return
    print(f"HWID из шаблона: {len(devices)}")
    candidates = _best_catalog_entries(devices, session)
    print(f"Найдено обновлений в каталоге: {len(candidates)}")
    _download_candidates(candidates, Path(cache_drivers), options)
    apply_second_run(template_dir, Path(cache_drivers))
