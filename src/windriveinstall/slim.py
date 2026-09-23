r"""Ужимает Template/<Устройство> до манифеста потребностей + фолбэков.

Две стадии (обе идемпотентны, можно повторять):

Первая — ``apply_first_run``: вызывается одноразово при подготовке шаблона
(scripts/prepare_template.py после распаковки OEM-архивов):
  * чинит HWID в .device.json из .inf шаблона (ридер UTF-16-совместим);
  * удаляет VGA-классику (папки VGA_AMD_* / VGA_NVIDIA_* в Drivers/);
  * переносит VGA Utility (AMD Radeon Software, NVIDIA Control Panel)
    из Drivers/ в Apps/ — это UWP-приложения, им место в списке приложений;
  * конвертирует Audio_Realtek в pnputil-компонент: режет InstallShield-
    обёртку (Setup.exe, data*.cab, setup.inx/isn, RtlUpd*, ChCfg.exe,
    0x*.ini), оставляет Win64 (база + APO + DTS-тун) и пишет
    Setup_Driver.cmd + InfFiles.txt со списком всех .inf;

Вторая — ``apply_second_run``: вызывается командой обновления драйверов
(driver-update) после скачивания свежих листьев в Cache/<Устройство>/Drivers:
  * pnputil-классика (Airplane Mode, LAN_Killer, Bluetooth, Wireless LAN,
    TouchPad): есть свежий лист по HWID — вырезается старый payload
    (inf/sys/cat/dll/dat/bin/exe/cer) и остаётся каркас; нет листа —
    компонент удаляется целиком (папка + записи в .components/.device.json).

Обе стадии сохраняют сторонние верхнеуровневые ключи .device.json
(например, "arch"), пересобирая только name/components.
"""

import json
import re
import shutil
from pathlib import Path

from .builder import (
    DEVICE_META_NAME,
    DRIVER_EXTENSIONS,
    MANIFEST_NAME,
    _extract_hwids,
    collect_fresh_leaves,
    read_device_meta,
)

VGA_CLASSIC_PREFIXES = ("VGA_AMD_", "VGA_NVIDIA_")
VGA_UTILITY_PREFIX = "VGA Utility_"
REALTEK_PREFIX = "Audio_Realtek"
PNPUTIL_CLASSIC_PREFIXES = (
    "Airplane Mode",
    "LAN_Killer",
    "Bluetooth",
    "Wireless LAN",
    "TouchPad",
)
REALTEK_WRAPPER_NAMES = frozenset(
    {
        "Setup.exe",
        "data1.cab",
        "data1.hdr",
        "data2.cab",
        "ISSetup.dll",
        "layout.bin",
        "RtlExUpd.dll",
        "RtlUpd64.exe",
        "RtlUpd.exe",
        "ChCfg.exe",
        "setup.ini",
        "setup.inx",
        "setup.isn",
        "setup.iss",
        "USetup.iss",
    }
)
_EXEC_RE = re.compile(r'Exec="([^"]+)"')


def _meta_by_rel(meta: dict) -> dict[str, dict]:
    """Индексирует записи .device.json по rel_path (нормализованный posix)."""
    by_rel: dict[str, dict] = {}
    for entry in meta.get("components", []):
        rel = str(entry.get("rel_path", "")).replace("\\", "/")
        if rel:
            by_rel[rel] = entry
    return by_rel


def _write_manifest(template_dir: Path, manifest: list[str]) -> None:
    """Пишет обновлённый .components (сортированный список rel_path)."""
    path = template_dir / MANIFEST_NAME
    path.write_text("\n".join(sorted(manifest)) + "\n", encoding="utf-8")


def _write_meta(template_dir: Path, meta: dict, by_rel: dict[str, dict]) -> None:
    """Пишет .device.json: актуальные компоненты + сохранение прочих ключей."""
    merged = {
        key: value for key, value in meta.items() if key not in ("name", "components")
    }
    merged.setdefault("arch", "x64")
    merged["name"] = template_dir.name
    merged["components"] = list(by_rel.values())
    path = template_dir / DEVICE_META_NAME
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _manifest_lines(template_dir: Path) -> list[str]:
    """Читает .components как список rel_path или бросает FileNotFoundError."""
    path = template_dir / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"Манифест не найден: {path}")
    return [
        line.strip() for line in path.read_text("utf-8").splitlines() if line.strip()
    ]


def _fix_meta_hwids(template_dir: Path, by_rel: dict[str, dict]) -> None:
    """Восстанавливает HWID в .device.json из .inf шаблона (UTF-16-совместимо)."""
    fixed = 0
    for rel, entry in by_rel.items():
        if entry.get("kind") != "classic":
            continue
        folder = template_dir / rel
        if not folder.is_dir():
            continue
        hwids = _extract_hwids(folder)
        if not hwids:
            continue
        if sorted({str(h).upper() for h in entry.get("hwids", [])}) != hwids:
            entry["hwids"] = hwids
            fixed += 1
    if fixed:
        print(f"HWID пересчитаны в {fixed} классических компонентах")


def _remove_vga_classics(
    template_dir: Path, manifest: list[str], by_rel: dict[str, dict]
) -> None:
    """Удаляет классические VGA-драйверы (AMD/NVIDIA) из шаблона."""
    for rel in list(manifest):
        name = rel.rsplit("/", 1)[-1]
        if not name.startswith(VGA_CLASSIC_PREFIXES):
            continue
        folder = template_dir / rel
        if folder.exists():
            shutil.rmtree(folder)
            print(f"VGA-классик удалён: {rel}")
        manifest.remove(rel)
        by_rel.pop(rel, None)


def _move_vga_utility(
    template_dir: Path, manifest: list[str], by_rel: dict[str, dict]
) -> None:
    """Переносит UWP-приложения VGA Utility из Drivers/ в Apps/."""
    apps_dir = template_dir / "Apps"
    for index, rel in enumerate(list(manifest)):
        name = rel.rsplit("/", 1)[-1]
        if not name.startswith(VGA_UTILITY_PREFIX):
            continue
        new_rel = f"Apps/{name}"
        source = template_dir / rel
        dest = template_dir / new_rel
        if source.exists() and not dest.exists():
            apps_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            print(f"VGA Utility перенесено: {rel} -> {new_rel}")
        manifest[index] = new_rel
        entry = by_rel.pop(rel, None)
        if entry is not None:
            entry["rel_path"] = new_rel
            entry["blacklisted"] = False
            by_rel[new_rel] = entry


def _strip_realtek_wrapper(folder: Path) -> None:
    """Вырезает InstallShield-обёртку Realtek на верхнем уровне компонента."""
    removed = 0
    for path in folder.iterdir():
        if not path.is_file():
            continue
        is_locale = path.name.startswith("0x") and path.suffix.lower() == ".ini"
        if is_locale or path.name in REALTEK_WRAPPER_NAMES:
            path.unlink()
            removed += 1
    print(f"  Realtek: удалено файлов обёртки: {removed}")


def _write_realtek_scripts(folder: Path) -> None:
    """Пишет InfFiles.txt и Setup_Driver.cmd для pnputil-установки Realtek."""
    infs = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.inf"))
    if not infs:
        raise SystemExit(f"В Realtek-компоненте нет .inf: {folder}")
    (folder / "InfFiles.txt").write_text(
        "\n".join(rel.replace("/", "\\") for rel in infs) + "\n", encoding="utf-8"
    )
    cmd = (
        "@echo off\r\n"
        "title Installing Realtek Audio\r\n"
        "if not exist C:\\OEM\\AcerLogs md C:\\OEM\\AcerLogs\r\n"
        "SET LogPath=C:\\OEM\\AcerLogs\\DriverInstallation.log\r\n"
        'pushd "%~dp0"\r\n'
        "ECHO %DATE% %TIME%[START] %~dpnx0 >> %LogPath%\r\n"
        'for /f "tokens=*" %%v in (InfFiles.txt) do (\r\n'
        '    pnputil /add-driver "%%v" /install >> %LogPath%\r\n'
        '    pnputil -i -a "%%v" >> %LogPath%\r\n'
        ")\r\n"
        "timeout /t 5 >NUL 2>&1\r\n"
        "ECHO Installation process completed.\r\n"
        "ECHO Press any key to exit\r\n"
        "PAUSE >NUL\r\n"
        "ECHO %DATE% %TIME%[Leave] %~dpnx0 >> %LogPath%\r\n"
        "popd\r\n"
    )
    (folder / "Setup_Driver.cmd").write_text(cmd, encoding="ascii")
    print(f"  Realtek: InfFiles.txt со списком .inf: {len(infs)}")


def _rewrite_prepackage_exec(xml_path: Path) -> None:
    """Переписывает Exec в Prepackage.xml на Setup_Driver.cmd."""
    if not xml_path.is_file():
        return
    text = xml_path.read_text("utf-8", errors="replace")
    new_text = _EXEC_RE.sub(
        lambda _match: 'Exec="%RELATIVE_PATH%\\Setup_Driver.cmd"', text
    )
    if new_text != text:
        xml_path.write_text(new_text, encoding="utf-8")
        print("  Realtek: Prepackage.xml Exec -> Setup_Driver.cmd")


def _convert_realtek(
    template_dir: Path, manifest: list[str], by_rel: dict[str, dict]
) -> None:
    """Конвертирует Audio_Realtek из Setup.exe-чёрного ящика в pnputil."""
    for rel in manifest:
        name = rel.rsplit("/", 1)[-1]
        if not name.startswith(REALTEK_PREFIX):
            continue
        folder = template_dir / rel
        if not folder.is_dir():
            continue
        _strip_realtek_wrapper(folder)
        _write_realtek_scripts(folder)
        _rewrite_prepackage_exec(folder / "Prepackage.xml")
        print(f"Audio_Realtek сконвертирован в pnputil: {rel}")
        return


def _fresh_leaf_hwids(drivers_dir: Path) -> dict[Path, set[str]]:
    """Индексирует HWID свежих листьев каталога драйверов."""
    return {
        leaf: {h.upper() for h in _extract_hwids(leaf)}
        for leaf in collect_fresh_leaves(drivers_dir)
    }


def _strip_classic_payload(folder: Path) -> int:
    """Вырезает старый драйверный payload pnputil-компонента, оставляя каркас."""
    removed = 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in DRIVER_EXTENSIONS or suffix == ".cer":
            path.unlink()
            removed += 1
    return removed


def _prune_pnputil_classics(
    template_dir: Path,
    manifest: list[str],
    by_rel: dict[str, dict],
    drivers_dir: Path,
) -> None:
    """Заменяет или удаляет pnputil-классику по наличию свежего листа."""
    fresh_hwids = _fresh_leaf_hwids(drivers_dir)
    for rel in list(manifest):
        name = rel.rsplit("/", 1)[-1]
        if not name.startswith(PNPUTIL_CLASSIC_PREFIXES):
            continue
        entry = by_rel.get(rel, {})
        hwids = {str(h).upper() for h in entry.get("hwids", [])}
        matched = any(hwids & leaf for leaf in fresh_hwids.values())
        folder = template_dir / rel
        if matched:
            removed = _strip_classic_payload(folder)
            print(
                f"pnputil-классика остаётся (лист найден): {rel} "
                f"(вырезано payload: {removed})"
            )
        else:
            if folder.exists():
                shutil.rmtree(folder)
            manifest.remove(rel)
            by_rel.pop(rel, None)
            print(f"pnputil-классика удалена (листа нет): {rel}")


def apply_first_run(template_dir: Path) -> None:
    """Одноразовая ужимка шаблона: HWID, удаление VGA, перенос VGA Utility, Realtek."""
    manifest = _manifest_lines(template_dir)
    meta = read_device_meta(template_dir)
    by_rel = _meta_by_rel(meta)

    _fix_meta_hwids(template_dir, by_rel)
    _remove_vga_classics(template_dir, manifest, by_rel)
    _move_vga_utility(template_dir, manifest, by_rel)
    _convert_realtek(template_dir, manifest, by_rel)

    _write_manifest(template_dir, manifest)
    _write_meta(template_dir, meta, by_rel)
    print("Прогон 1 готов: .components и .device.json перезаписаны")


def apply_second_run(template_dir: Path, drivers_dir: Path | None = None) -> None:
    """Повторная ужимка после driver-update: pnputil-классика по свежим листьям."""
    manifest = _manifest_lines(template_dir)
    meta = read_device_meta(template_dir)
    by_rel = _meta_by_rel(meta)

    if drivers_dir is None:
        drivers_dir = Path("Cache") / template_dir.name / "Drivers"
    if drivers_dir.is_dir() and any(drivers_dir.rglob("*.inf")):
        _prune_pnputil_classics(template_dir, manifest, by_rel, drivers_dir)
    else:
        print(
            f"Кэш драйверов не найден ({drivers_dir}) — ужимка "
            "pnputil-классики пропущена"
        )

    _write_manifest(template_dir, manifest)
    _write_meta(template_dir, meta, by_rel)
    print("Прогон 2 готов: .components и .device.json перезаписаны")
