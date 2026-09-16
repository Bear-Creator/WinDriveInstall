"""Библиотека для установки драверов."""

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def generate_install_bats(
    inf_files: list[Path], download_root: Path
) -> tuple[Path, Path]:
    """Генерирует два .bat скрипта.

    1. install_all.bat — устанавливает абсолютно все найденные .inf
    2. install_updates.bat — устанавливает только обновления
    """
    download_root = Path(download_root)

    # Шапка для bat-файлов с проверкой прав Администратора
    header_lines = [
        "@echo off",
        "net session >nul 2>&1",
        "if %errorLevel% neq 0 (",
        "    echo [!] Ошибка: Запустите скрипт от имени АДМИНИСТРАТОРА!",
        "    pause",
        "    exit /b",
        ")",
        "",
        "echo Installing drivers via pnputil...",
        "",
    ]
    footer_lines = ["", "echo Done.", "pause"]

    all_bat_path = download_root / "install_all.bat"
    all_lines = (
        header_lines
        + [f'pnputil /add-driver "{inf}" /install' for inf in inf_files]
        + footer_lines
    )
    all_bat_path.write_text("\r\n".join(all_lines), encoding="utf-8")

    upd_bat_path = download_root / "install_updates.bat"
    upd_lines = (
        header_lines
        + [f'pnputil /add-driver "{inf}" /install' for inf in inf_files]
        + footer_lines
    )
    upd_bat_path.write_text("\r\n".join(upd_lines), encoding="utf-8")

    return upd_bat_path, all_bat_path


def _require_windows_and_confirm(confirm: bool, target: Path) -> None:
    if not confirm:
        raise PermissionError(
            "Установка НЕ выполнена: требуется явное подтверждение (--confirm)."
        )
    if not target.exists():
        raise FileNotFoundError(f"Не найден: {target}")


def run_install_bat(bat_path: Path, confirm: bool = False) -> bool:
    """Запускает сгенерированный .bat с правами администратора (через UAC-запрос)."""
    try:
        _require_windows_and_confirm(confirm, bat_path)
    except PermissionError as e:
        log.warning(str(e))
        return False
    subprocess.run(
        [
            "powershell",
            "-Command",
            f"Start-Process -FilePath '{bat_path}' -Verb RunAs -Wait",
        ],
        check=False,
    )
    return True


def install_driver(inf_path: Path, confirm: bool = False) -> bool:
    """Устанавливает один драйвер из .inf через pnputil."""
    try:
        _require_windows_and_confirm(confirm, inf_path)
    except PermissionError as e:
        log.warning(str(e))
        return False

    result = subprocess.run(
        ["pnputil", "/add-driver", str(inf_path), "/install"],
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return False
    return True
