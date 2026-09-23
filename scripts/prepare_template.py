r"""Одноразовый помощник: собирает шаблонную директорию из OEM-архивов.

Запуск (Linux/WSL, без win32com):
    uv run python scripts/prepare_template.py --oem-dir OEM \\
        --template-dir Template/Nitro5

Шаблон собирается один раз вручную: каждый OEM-.zip распаковывается в папку
компонента, записываются манифест .components и меты .device.json. Свежие
драйверы и приложения в шаблон не входят — они живут в Cache/<Устройство>
(driver-update / uwp-update). Необязательный --copy-drivers
копирует каталог загрузок в Template/<Устройство>/Drivers сразу при создании.
"""

import argparse
import shutil
from pathlib import Path

from windriveinstall.builder import prepare_oem_template
from windriveinstall.slim import apply_first_run


def _copy_drivers(src_dir: Path, template_dir: Path) -> None:
    """Копирует каталог загрузок драйверов в Template/<Устройство>/Drivers."""
    dest = template_dir / "Drivers"
    if dest.exists():
        if any(dest.iterdir()):
            raise FileExistsError(f"Папка уже не пуста: {dest}")
        dest.rmdir()
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Каталог драйверов не найден: {src_dir}")
    shutil.copytree(src_dir, dest)
    print(f"Драйверы скопированы: {src_dir} -> {dest}")


def main() -> None:
    """Точка входа одноразового скрипта подготовки шаблона."""
    parser = argparse.ArgumentParser(
        description="Сборка шаблонной директории из OEM-архивов (один раз)"
    )
    parser.add_argument("--oem-dir", default=str(Path("OEM")), help="Папка OEM-архивов")
    parser.add_argument(
        "--template-dir", default=str(Path("Template")), help="Папка шаблона"
    )
    parser.add_argument(
        "--copy-drivers",
        metavar="DIR",
        default=None,
        help="Необязательно: сразу скопировать каталог загрузок в Template/Drivers",
    )
    args = parser.parse_args()

    template_dir = Path(args.template_dir)
    try:
        components = prepare_oem_template(Path(args.oem_dir), template_dir)
    except FileExistsError as exc:
        print(f"Ошибка: {exc}")
        return
    for comp in components:
        status = "ошибка: " + comp.error if comp.error else comp.kind
        print(f"  - {comp.name}  [{status}]")
    print(f"Подготовлено компонентов: {len(components)}")

    if args.copy_drivers is not None:
        _copy_drivers(Path(args.copy_drivers), template_dir)

    try:
        apply_first_run(template_dir)
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}")
        return

    print(f"Шаблон готов: {template_dir}")
    print(
        "Свежие драйверы качайте в Cache/<Устройство>/Drivers (driver-update), "
        "UWP-приложения — в Cache/<Устройство>/Apps (uwp-update), затем собирайте: "
        "windriveinstall build --template-dir <путь к шаблону>.\n"
        "Единый пайплайн: windriveinstall run --template-dir <путь к шаблону> "
        "— скачает драйверы и приложения, ужмёт шаблон и соберёт пакет."
    )


if __name__ == "__main__":
    main()
