import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LocalDevice:
    name: str
    hwid: str
    driver_version: str
    manufacturer: str
    device_class: str
    driver_date: dt.date
    hwid_candidates: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.hwid_candidates:
            self.hwid_candidates = [self.hwid]


@dataclass
class CatalogEntry:
    update_id: str
    title: str
    products: str
    classification: str
    last_updated: dt.date
    version: str
    size: str


@dataclass
class DownloadResult:
    device: LocalDevice
    entry: CatalogEntry
    matched_hwid: str
    dest_folder: Path
    status: str = "pending"  # pending | ok | manual_needed | failed
    error: str | None = None
