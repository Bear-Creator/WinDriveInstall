"""Модели данных приложения WinDriveInstall."""

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LocalDevice:
    """Локальное устройство из WMI с версией и идентификаторами оборудования."""

    name: str
    hwid: str
    driver_version: str
    manufacturer: str
    device_class: str
    driver_date: dt.date
    hwid_candidates: list[str] = field(default_factory=list)

    def __post_init__(self):
        """Гарантирует наличие хотя бы основного HWID в списке кандидатов."""
        if not self.hwid_candidates:
            self.hwid_candidates = [self.hwid]


@dataclass
class CatalogEntry:
    """Запись об обновлении из каталога Microsoft Update Catalog."""

    update_id: str
    title: str
    products: str
    classification: str
    last_updated: dt.date
    version: str
    size: str


@dataclass
class DownloadResult:
    """Результат загрузки и распаковки драйвера для устройства."""

    device: LocalDevice
    entry: CatalogEntry
    matched_hwid: str
    dest_folder: Path
    status: str = "pending"  # pending | ok | manual_needed | failed
    error: str | None = None


@dataclass
class OEMComponent:
    """Один OEM-компонент (архив .zip или извлечённая папка) и результат его сборки."""

    name: str
    rel_path: Path
    kind: str  # "classic" | "uwp"
    source: Path
    out_dir: Path
    blacklisted: bool = False
    family_names: list[str] = field(default_factory=list)
    hwids: list[str] = field(default_factory=list)
    matched_sources: list[Path] = field(default_factory=list)
    files_overwritten: int = 0
    files_added: int = 0
    packages_removed: int = 0
    snapshot: bool = False
    error: str | None = None


@dataclass
class MergeReport:
    """Итоговый отчёт о гибридной сборке: шаблон OEM + свежие драйверы."""

    template_dir: Path
    drivers_dir: Path
    out_dir: Path
    apps_dir: Path | None = None
    components: list[OEMComponent] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def files_overwritten(self) -> int:
        """Суммарное число перезаписанных OEM-файлов по всем компонентам."""
        return sum(c.files_overwritten for c in self.components)

    @property
    def files_added(self) -> int:
        """Суммарное число добавленных свежих файлов по всем компонентам."""
        return sum(c.files_added for c in self.components)

    @property
    def packages_replaced(self) -> int:
        """Суммарное число удалённых старых UWP-пакетов по всем компонентам."""
        return sum(c.packages_removed for c in self.components)


@dataclass
class UwpDownloadResult:
    """Результат скачивания свежего UWP-пакета приложения из Microsoft Store."""

    app: str
    family: str | None
    version: str
    state: str = "done"  # done | up_to_date | not_found | no_update | error
    message: str | None = None
