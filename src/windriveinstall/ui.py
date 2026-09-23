"""Форматированный вывод отчетов и таблиц в консоль."""

from .models import CatalogEntry, DownloadResult, LocalDevice
from .utils import truncate


def print_update_summary(
    candidates: list[tuple[LocalDevice, CatalogEntry, str]],
) -> None:
    """Выводит таблицу найденных обновлений драйверов для устройств."""
    w = (3, 42, 20, 20, 12, 12)

    print(
        f"\n{'#':<{w[0]}} "
        f"{truncate('Device', w[1])} "
        f"{'Local ver':<{w[2]}} "
        f"{'Latest ver':<{w[3]}} "
        f"{'Local date':<{w[4]}} "
        f"{'Latest date':<{w[5]}}"
    )
    print("-" * (sum(w) + len(w) - 1))

    for i, (dev, entry, _hwid) in enumerate(candidates, 1):
        print(
            f"{i:<{w[0]}} {truncate(dev.name, w[1])} "
            f"{dev.driver_version:<{w[2]}} "
            f"{entry.version:<{w[3]}} "
            f"{dev.driver_date!s:<{w[4]}} "
            f"{entry.last_updated!s:<{w[5]}}"
        )
    print()


def print_final_report(results: list[DownloadResult]) -> None:
    """Печатает сводный отчет по статусам загрузки драйверов."""
    labels = {"ok": "OK", "manual_needed": "ПРОПУЩЕН", "failed": "ОШИБКА"}
    print("\n" + "=" * 76)
    print("ИТОГ ЗАГРУЗКИ")
    print("=" * 76)
    for r in results:
        print(
            f"[{labels.get(r.status, r.status.upper()):9}] "
            f"{truncate(r.device.name, 40):<40} -> {r.dest_folder}"
        )
        if r.error:
            print(f"              причина: {r.error}")
    ok_count = sum(1 for r in results if r.status == "ok")
    print("-" * 76)
    print(f"Успешно: {ok_count}/{len(results)}\n")
