"""Гибридная сборка единого OEM-пакета драйверов и UWP-приложений.

Два этапа:

1. prepare_oem_template(OEM, Template) - из заводских OEM-архивов (.zip)
   строится шаблонная директория: каждый компонент становится извлечённой
   папкой с нормализованной структурой (единый внутренний корень срезается).
   Вместе с манифестом .components пишет метаданные .device.json (HWID из
   .inf, package family из AUMIDs, версии) — по ним ищутся свежие обновления.

2. build_oem_package(Template, Out, Drivers, Apps) - финальный пакет из шаблона:
   поверх шаблона накладываются свежие драйверы (Cache/<Dev>/Drivers) и свежие
   UWP-приложения (Cache/<Dev>/Apps), генерируется Install_Factory.bat,
   запускающий заводские скрипты. Папка выхода пересоздаётся целиком.

Режимы:
    --template <путь до Template/<Dev>>  reproducible-сборка из шаблона.
    без шаблона                        rolling-сборка под текущую систему
                                        (см. rolling.py, только Windows).

Исходники:
    OEM/         - заводские .zip (скрипты, конфиги, лицензии).
    Template/<Dev>/ - распакованный шаблон из шага 1 (база, статична).
    Cache/<Dev>/ - свежие обновления: Drivers/ (каталог MS), Apps/ (UWP).

Результат (--out-dir, по умолчанию Output/<Dev>):
    зеркало структуры Template/ с извлечёнными компонентами, куда поверх
    наложены свежие файлы драйверов, заменённые UWP-бандлы и объединённые
    зависимости, плюс Install_Factory.bat, запускающий заводские скрипты.
"""

import json
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import MergeReport, OEMComponent
from .utils import sanitize_folder_name

log = logging.getLogger(__name__)

DRIVER_EXTENSIONS = frozenset({".inf", ".sys", ".cat", ".dll", ".dat", ".bin", ".exe"})
UWP_BUNDLE_EXTS = frozenset({".appx", ".appxbundle", ".msix", ".msixbundle"})
DEPENDENCY_PREFIX = "Microsoft."
BLACKLIST_KEYWORDS: tuple[str, ...] = ()
ENTRY_SCRIPT_NAMES = ("Install_UWP.cmd", "Setup_Driver.cmd", "Install.cmd")
PREPACKAGE_XML = "Prepackage.xml"
RELATIVE_PATH_TOKEN = "%RELATIVE_PATH%"
FACTORY_BAT_NAME = "Install_Factory.bat"
INF_LIST_NAME = "InfFiles.txt"

_ARCH_TOKENS = frozenset({"neutral", "x64", "x86", "arm", "arm64"})
_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,4}")
_EXEC_RE = re.compile(r'Exec="([^"]+)"')
MAX_SHOWN_ZIPS = 3
MANIFEST_NAME = ".components"
DEVICE_META_NAME = ".device.json"
DRIVERS_DIR_NAME = "Drivers"
APPS_DIR_NAME = "Apps"
DEPS_DIR_NAME = "Dependencies"
DEPS_META_NAME = ".deps.json"
NEUTRAL_ARCH = "neutral"


def _package_arch(filename: str) -> str:
    """Определяет архитектуру пакета по токену имени (по умолчанию neutral)."""
    for token in Path(filename).name.split("_"):
        if token in _ARCH_TOKENS:
            return token
    return NEUTRAL_ARCH

_HWID_HEAD_RE = (
    r"(?:PCI\\|USB\\|ACPI\\|HID\\|HTREE\\|SW\\|SCSI\\|StdProv\\|OEM\\|BTH\\|"
    r"STORAGE\\|PNP\\|ROOT\\|MDNX\\|SD\\|DTS\\|VEN_)"
)
_HWID_RE = re.compile(_HWID_HEAD_RE + r"[A-Za-z0-9_&^;.]{4,}")
MIN_HWID_LEN = 6


def is_blacklisted(name: str) -> bool:
    """Возвращает True, если имя компонента попадает в чёрный список.

    Список ключевых слов сейчас пуст: VGA Utility и NVIDIA Control Panel —
    это UWP-приложения, которые нужны в пакете, поэтому исключение убрано.
    """
    lowered = name.lower()
    return any(keyword in lowered for keyword in BLACKLIST_KEYWORDS)


def _ext_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _unsafe_member(name: str) -> bool:
    parts = name.split("/")
    return not parts or parts[0] == "" or any(p in ("..", "") for p in parts)


def _iter_files(source: Path):
    """Итерирует (rel_posix, filename) по всем файлам компонента (zip/папка)."""
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if _unsafe_member(name):
                    continue
                yield name, name.rsplit("/", 1)[-1]
        return
    for path in source.rglob("*"):
        if path.is_file():
            yield path.relative_to(source).as_posix(), path.name


def _read_named_text(source: Path, filename: str) -> str | None:
    """Читает текстовый файл из zip/папки по имени (без учёта регистра)."""
    target = None
    for rel, fname in _iter_files(source):
        if fname.lower() == filename.lower():
            target = rel
            break
    if target is None:
        return None
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            try:
                return zf.read(target).decode("utf-8", errors="replace")
            except KeyError:
                return None
    try:
        return (source / target).read_text("utf-8", errors="replace")
    except OSError:
        return None


def _driver_stems(source: Path, extensions: frozenset[str]) -> set[str]:
    """Собирает нижний регистр стеблей файлов драйверов (без расширения)."""
    stems: set[str] = set()
    for _rel, fname in _iter_files(source):
        if _ext_of(fname) in extensions and not fname.startswith("~"):
            stems.add(fname.rsplit(".", 1)[0].lower())
    return stems


def _component_has_uwp(source: Path) -> bool:
    for _rel, fname in _iter_files(source):
        if fname.lower() == "install_uwp.cmd":
            return True
        is_app = _ext_of(fname) in UWP_BUNDLE_EXTS
        if is_app and not fname.startswith(DEPENDENCY_PREFIX):
            return True
    return False


def _component_family_names(source: Path) -> list[str]:
    """Парсит package family names из AUMIDs.txt (если он есть в компоненте)."""
    text = _read_named_text(source, "AUMIDs.txt")
    if text is None:
        return []
    families: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "!" not in line:
            continue
        family = line.split("!", 1)[0].strip().lower()
        if family:
            families.add(family)
    return sorted(families)


def _read_inf_text(inf: Path) -> str:
    """Читает .inf с учётом кодировки: UTF-16 по BOM, иначе UTF-8.

    Некоторые OEM-пакеты (например, Killer LAN или MTK WiFi) хранят .inf
    в UTF-16LE — без BOM-детекта HWID из такого файла не прочитаются.
    """
    try:
        data = inf.read_bytes()
    except OSError:
        return ""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    return data.decode("utf-8", errors="replace")


def _extract_hwids(component_dir: Path) -> list[str]:
    r"""Извлекает hardware ID из .inf-файлов компонента (для поиска в каталоге).

    Примеры: ``USB\VID_0E8D&PID_7961&MI_00``, ``PCI\VEN_10EC&DEV_8168``.
    Возвращает нормализованные уникальные HWID (без *-подстановок).
    """
    hwids: set[str] = set()
    for inf in sorted(component_dir.rglob("*.inf")):
        text = _read_inf_text(inf)
        for raw in _HWID_RE.findall(text):
            hwid = raw.rstrip("*&^;").strip()
            if len(hwid) >= MIN_HWID_LEN:
                hwids.add(hwid.upper())
    return sorted(hwids)


def _component_version(name: str) -> str:
    """Достаёт версию из имени компонента (лучшая попытка, пустая строка — ок)."""
    match = _VERSION_RE.search(name)
    return match.group(0) if match else ""


def _device_meta_components(
    template_dir: Path, components: list[OEMComponent]
) -> list[dict]:
    """Собирает плоское описание компонентов для .device.json."""
    metas: list[dict] = []
    for comp in components:
        entry = {
            "rel_path": comp.rel_path.as_posix(),
            "name": comp.name,
            "kind": comp.kind,
            "blacklisted": comp.blacklisted,
            "version": _component_version(comp.name),
            "family_names": comp.family_names,
            "hwids": _extract_hwids(comp.out_dir) if comp.kind == "classic" else [],
        }
        metas.append(entry)
    return metas


def write_device_meta(template_dir: Path, components: list[OEMComponent]) -> Path:
    """Пишет .device.json: метаданные шаблона, по которым ищутся обновления."""
    meta = {
        "name": template_dir.name,
        "components": _device_meta_components(template_dir, components),
    }
    path = template_dir / DEVICE_META_NAME
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_device_meta(template_dir: Path) -> dict:
    """Читает .device.json шаблона (пустой словарь, если файла нет)."""
    path = template_dir / DEVICE_META_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def merge_snapshot_devices(template_dir: Path, hwids: list[str]) -> list[dict]:
    """Дополняет .device.json устройствами из снимка системы (WMI, source: system).

    Возвращает список добавленных записей. Снимок не трогает существующие
    HWID: добавляются только те, которых ещё нет в метаданных шаблона.
    """
    meta = read_device_meta(template_dir)
    known: set[str] = set()
    for comp in meta.get("components", []):
        known.update(str(h).upper() for h in comp.get("hwids", []))
    added: list[dict] = []
    for hwid in sorted({str(h).upper() for h in hwids}):
        if hwid in known or len(hwid) < MIN_HWID_LEN:
            continue
        name = f"System device {hwid}"
        entry = {
            "rel_path": f"snapshot/{hwid.replace('\\\\', '_')}",
            "name": name,
            "kind": "classic",
            "blacklisted": False,
            "version": "",
            "family_names": [],
            "hwids": [hwid],
            "source": "system",
        }
        added.append(entry)
    if added:
        meta.setdefault("components", []).extend(added)
        path = template_dir / DEVICE_META_NAME
        path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return added


def merge_snapshot_apps(template_dir: Path, apps: list[tuple[str, str]]) -> list[dict]:
    """Дополняет .device.json приложениями из снимка системы (source: system).

    Записи помечаются kind="uwp" и несут family_names, чтобы uwp-update
    и сборка подхватывали их как приложения. Слияние по семейству пакетов:
    уже известные family не перезаписываются и не дублируются.
    """
    meta = read_device_meta(template_dir)
    known: set[str] = set()
    for comp in meta.get("components", []):
        for fam in comp.get("family_names", []):
            known.add(str(fam).lower())
    added: list[dict] = []
    for name, family in apps:
        if not family or family.lower() in known:
            continue
        entry = {
            "rel_path": f"Apps/{sanitize_folder_name(name)}",
            "name": name,
            "kind": "uwp",
            "blacklisted": False,
            "version": "",
            "family_names": [family.lower()],
            "hwids": [],
            "source": "system",
        }
        added.append(entry)
        known.add(family.lower())
    if added:
        meta.setdefault("components", []).extend(added)
        path = template_dir / DEVICE_META_NAME
        path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return added


def _is_component_dir(path: Path) -> bool:
    """Определяет, является ли папка извлечённым компонентом (не контейнером)."""
    markers = {
        "detail.txt",
        "prepackage.xml",
        "install_uwp.cmd",
        "install.cmd",
        "aumids.txt",
    }
    for child in path.iterdir():
        if child.name.lower() in markers:
            return True
        if child.is_file() and child.suffix.lower() == ".inf":
            return True
    return False


def _make_component(oem_dir: Path, source: Path) -> OEMComponent:
    if source.is_file() and source.suffix.lower() == ".zip":
        rel_path = source.relative_to(oem_dir).with_suffix("")
        name = source.stem
    else:
        rel_path = source.relative_to(oem_dir)
        name = source.name
    kind = "uwp" if _component_has_uwp(source) else "classic"
    return OEMComponent(
        name=name,
        rel_path=rel_path,
        kind=kind,
        source=source,
        out_dir=Path(),
        blacklisted=is_blacklisted(name),
        family_names=_component_family_names(source),
        hwids=_extract_hwids(source) if kind == "classic" else [],
    )


def scan_oem_components(oem_dir: Path) -> list[OEMComponent]:
    """Рекурсивно находит все OEM-компоненты (zip-архивы и распакованные папки)."""
    components: list[OEMComponent] = []

    def walk(current: Path) -> None:
        for path in sorted(current.iterdir()):
            if path.is_file() and path.suffix.lower() == ".zip":
                components.append(_make_component(oem_dir, path))
            elif path.is_dir() and _is_component_dir(path):
                components.append(_make_component(oem_dir, path))
            elif path.is_dir():
                walk(path)

    for path in sorted(oem_dir.iterdir()):
        if path.is_file() and path.suffix.lower() == ".zip":
            components.append(_make_component(oem_dir, path))
        elif path.is_dir() and _is_component_dir(path):
            components.append(_make_component(oem_dir, path))
        elif path.is_dir():
            walk(path)
    return components


def prepare_oem_template(oem_dir: Path, template_dir: Path) -> list[OEMComponent]:
    """Строит шаблонную директорию: каждый OEM-.zip распаковывается в папку.

    Шаблон повторяет структуру OEM/, но вместо .zip создаётся папка с именем
    архива, в которую распаковывается содержимое (единый верхний корень внутри
    архива срезается). Уже распакованные папки компонентов и обычные файлы
    копируются как есть. Папка шаблона должна быть пустой или отсутствовать.
    """
    oem_dir, template_dir = Path(oem_dir), Path(template_dir)
    if template_dir.exists() and any(template_dir.iterdir()):
        raise FileExistsError(
            f"Папка шаблона не пуста: {template_dir}. Очистите её перед запуском."
        )
    template_dir.mkdir(parents=True, exist_ok=True)
    components: list[OEMComponent] = []

    def walk(current: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(current.iterdir()):
            if path.is_file() and path.suffix.lower() == ".zip":
                dest = target / path.stem
                dest.mkdir()
                component = _make_component(oem_dir, path)
                component.out_dir = dest
                try:
                    _extract_zip(path, dest, _zip_common_root(path))
                except (OSError, zipfile.BadZipFile) as exc:
                    component.error = str(exc)
                components.append(component)
            elif path.is_file():
                shutil.copy2(path, target / path.name)
            elif _is_component_dir(path):
                dest = target / path.name
                component = _make_component(oem_dir, path)
                component.out_dir = dest
                try:
                    shutil.copytree(path, dest)
                except (OSError, zipfile.BadZipFile) as exc:
                    component.error = str(exc)
                components.append(component)
            else:
                walk(path, target / path.name)

    walk(oem_dir, template_dir)

    manifest_lines = sorted(str(c.rel_path.as_posix()) for c in components)
    (template_dir / MANIFEST_NAME).write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )
    write_device_meta(template_dir, components)
    return components


def _read_template_manifest(template_dir: Path) -> list[Path] | None:
    """Читает .components-манифест шаблона или возвращает None, если его нет."""
    manifest = template_dir / MANIFEST_NAME
    if not manifest.is_file():
        return None
    root = template_dir.resolve()
    folders: list[Path] = []
    for raw in manifest.read_text("utf-8").splitlines():
        rel = raw.strip()
        if not rel:
            continue
        target = (template_dir / rel).resolve()
        if target.is_dir() and target.is_relative_to(root):
            folders.append(target)
    return folders


def parse_package_family(filename: str) -> str | None:
    """Достаёт package family name (в нижнем регистре) из имени UWP-пакета.

    Примеры: 'DTSInc.DTSXUltra_1.14.2.0_neutral_~_t5j2fzbtdg37r.appxbundle'
             -> 'dtsinc.dtsxultra_t5j2fzbtdg37r'.
    """
    name = Path(filename).name
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("_")
    version_index = next(
        (i for i, token in enumerate(parts) if _VERSION_RE.fullmatch(token)), None
    )
    if version_index is None:
        return None
    app_name = "_".join(parts[:version_index])
    publisher = next(
        (
            token
            for token in parts[version_index + 1 :]
            if token not in _ARCH_TOKENS and token not in ("", "~")
        ),
        None,
    )
    if publisher is None:
        return None
    return f"{app_name}_{publisher}".lower()


def get_bundle_inner_version(bundle_path: Path, arch: str = "x64") -> str | None:
    """Извлекает версию приложения из AppxBundleManifest.xml внутри бандла."""
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file() or _ext_of(bundle_path.name) not in (
        ".appxbundle",
        ".msixbundle",
    ):
        return None
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            if "AppxMetadata/AppxBundleManifest.xml" in zf.namelist():
                xml_data = zf.read("AppxMetadata/AppxBundleManifest.xml")
                root = ET.fromstring(xml_data)
                target = arch.lower()
                for pkg in root.findall(
                    ".//{http://schemas.microsoft.com/appx/2013/bundle}Package"
                ):
                    if pkg.attrib.get("Type") == "application":
                        pkg_arch = pkg.attrib.get("Architecture", "").lower()
                        if pkg_arch in (target, NEUTRAL_ARCH):
                            ver = pkg.attrib.get("Version")
                            if ver:
                                return ver
    except Exception:
        pass
    return None


def _zip_common_root(zip_path: Path) -> str | None:
    """Возвращает единый верхний каталог внутри zip или None, если его нет."""
    tops: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/").rstrip("/")
            if not name:
                continue
            if not (info.is_dir() or "/" in name):
                continue
            top = name.split("/", 1)[0]
            if not top or top == "":
                return None
            tops.add(top)
    return tops.pop() if len(tops) == 1 else None


def _extract_zip(zip_path: Path, dest: Path, strip_root: str | None = None) -> None:
    """Распаковывает zip в dest, защищаясь от path traversal."""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if _unsafe_member(name):
                log.warning("Пропуск небезопасного пути в архиве: %s", name)
                continue
            if strip_root:
                prefix = strip_root + "/"
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                elif name == strip_root:
                    continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _prepare_component(component: OEMComponent, out_root: Path) -> None:
    """Извлекает архив или копирует папку компонента в итоговый каталог."""
    component.out_dir = out_root / component.rel_path
    if component.blacklisted:
        component.out_dir.mkdir(parents=True, exist_ok=True)
        return
    if component.out_dir.exists():
        shutil.rmtree(component.out_dir)
    if component.source.is_file() and component.source.suffix.lower() == ".zip":
        component.out_dir.mkdir(parents=True, exist_ok=True)
        _extract_zip(
            component.source, component.out_dir, _zip_common_root(component.source)
        )
    else:
        shutil.copytree(component.source, component.out_dir)


def collect_fresh_leaves(drivers_dir: Path) -> list[Path]:
    """Собирает верхние каталоги-листья свежих классических драйверов с .inf.

    Вложенная папка с .inf внутри листа (например, HSA-подпакеты Realtek
    SoftwareComponent) листом не считается: она входит в свой пакет и
    копируется рекурсивно вместе с родителем. Иначе отчёт засоряется
    «несопоставленными» дубликатами одного и того же пакета.
    """
    inf_parents = {inf.parent for inf in drivers_dir.rglob("*.inf")}
    leaves = [
        folder
        for folder in sorted(inf_parents)
        if not any(ancestor in inf_parents for ancestor in folder.parents)
    ]
    return [
        leaf
        for leaf in leaves
        if not is_blacklisted(str(leaf.relative_to(drivers_dir)))
    ]


def _file_count(folder: Path) -> int:
    return sum(1 for p in folder.rglob("*") if p.is_file())


def _extract_inf_referenced_files(folder: Path) -> set[str]:
    """Извлекает имена файлов, на которые активно ссылаются .inf в каталоге."""
    referenced: set[str] = set()
    if not folder.is_dir():
        return referenced
    for inf in sorted(folder.rglob("*.inf")):
        text = _read_inf_text(inf)
        for raw_line in text.splitlines():
            cleaned = raw_line.strip()
            if not cleaned or cleaned.startswith(";"):
                continue
            body = cleaned.split(";", 1)[0]
            if body.startswith("["):
                continue
            for token in re.findall(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+", body):
                referenced.add(token.lower())
    return referenced


def _copy_driver_files(
    src_dir: Path, dest_dir: Path, component: OEMComponent, used: set[str]
) -> None:
    """Копирует файлы драйверов (inf/sys/cat/...) из свежего листа в компонент."""
    referenced = _extract_inf_referenced_files(
        src_dir
    ) | _extract_inf_referenced_files(dest_dir)
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in DRIVER_EXTENSIONS and path.name.lower() not in referenced:
            continue
        rel = path.relative_to(src_dir).as_posix()
        if rel in used:
            continue
        used.add(rel)
        target = dest_dir / rel
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if existed:
            component.files_overwritten += 1
        else:
            component.files_added += 1


def _merge_classic(
    component: OEMComponent,
    fresh_leaves: list[Path],
    claimed: set[Path],
    report: MergeReport,
) -> None:
    """Объединяет классический драйверный компонент со свежими листами.

    Лист считается кандидатом при пересечении по HWID (точное совпадение из
    .inf) или по общим стеблям файлов (fuzzy-запас, когда .inf без HWID).
    """
    oem_stems = _driver_stems(component.source, DRIVER_EXTENSIONS)
    component_hwids = {h.upper() for h in component.hwids}
    hwid_cache: dict[Path, set[str]] = {}
    scored: list[tuple[int, int, Path]] = []
    for leaf in fresh_leaves:
        if leaf in claimed:
            continue
        shared = oem_stems & _driver_stems(leaf, DRIVER_EXTENSIONS)
        leaf_hwids = hwid_cache.setdefault(
            leaf, {h.upper() for h in _extract_hwids(leaf)}
        )
        hits = len(component_hwids & leaf_hwids)
        if hits or shared:
            scored.append((hits, len(shared), leaf))
    scored.sort(key=lambda item: (-item[0], -item[1], _file_count(item[2])))
    used: set[str] = set()
    for hits, shared, leaf in scored:
        if hits == 0 and shared == 0:
            break
        claimed.add(leaf)
        component.matched_sources.append(leaf)
        _copy_driver_files(leaf, component.out_dir, component, used)
        log.info(
            "Компонент '%s': объединено с %s (HWID=%d, общих файлов=%d)",
            component.name,
            leaf.name,
            hits,
            shared,
        )
    if component.matched_sources:
        if _has_pnputil_installer(component.out_dir):
            _rewrite_inf_files_list(component)
    else:
        report.unmatched.append(component.name)


def _has_pnputil_installer(out_dir: Path) -> bool:
    """Есть ли в компоненте pnputil-инсталлятор (Setup_Driver.cmd/Install.cmd)."""
    return (out_dir / "Setup_Driver.cmd").is_file() or (
        out_dir / "Install.cmd"
    ).is_file()


def _rewrite_inf_files_list(component: OEMComponent) -> None:
    """Переписывает InfFiles.txt списком фактических .inf в собранном компоненте.

    Setup_Driver.cmd/Install.cmd ставят драйверы pnputil-ом построчно по
    InfFiles.txt. После мержа свежих листьев старые имена из списка могли
    исчезнуть (slim вырезал старый payload) или быть перезаписаны — список
    пересобирается из реальных .inf в out_dir (относительные пути), чтобы
    инсталлятор ставил именно свежие пакеты.
    """
    out = component.out_dir
    infs = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.inf"))
    if not infs:
        return
    lines = [rel.replace("/", "\\") for rel in infs]
    (out / INF_LIST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _app_main_packages(app_dir: Path) -> list[Path]:
    """Возвращает все главные пакеты (не-зависимости) папки приложения.

    В одной папке приложения может быть несколько главных пакетов — например,
    'VGA Utility' с AMD Radeon Software и NVIDIA Control Panel. Каждый пакет
    участвует в подборе под компонент отдельно, иначе один компонент забирал
    чужой пакет, а свой терял.
    """
    order = {".appxbundle": 0, ".msixbundle": 1, ".appx": 2, ".msix": 3}
    return sorted(
        (
            p
            for p in app_dir.iterdir()
            if p.is_file() and _ext_of(p.name) in UWP_BUNDLE_EXTS
        ),
        key=lambda p: (order.get(_ext_of(p.name), 99), p.name.lower()),
    )


def _name_prefix_match(component_name: str, app_dir_name: str) -> bool:
    """Имя компонента начинается с имени папки приложения (без учёта регистра).

    Строгое правило исключает случайные пересечения по одному слову: ранее
    общий токен 'Utility' отдавал VGA Utility XPERI-компоненту, а 'Acer' —
    Acer Care Center компоненту Quick Access.
    """
    left = re.sub(r"[^a-z0-9]+", " ", component_name.lower()).strip().split()
    right = re.sub(r"[^a-z0-9]+", " ", app_dir_name.lower()).strip().split()
    return bool(right) and left[: len(right)] == right


def _find_fresh_uwp_app(
    apps_dir: Path, component: OEMComponent
) -> tuple[Path, Path, str] | None:
    """Подбирает свежий UWP-пакет под компонент по family name / имени.

    Точное совпадение по family предпочтительно; при отсутствии — совпадение
    по префиксу имени (имя-тёзка из каталога приложений). В папке приложения
    может быть несколько главных пакетов — каждый проверяется отдельно.
    """
    if not apps_dir.is_dir():
        return None
    candidates: list[tuple[Path, Path, str]] = []
    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir() or app_dir.name == DEPS_DIR_NAME:
            continue
        for pkg in _app_main_packages(app_dir):
            family = parse_package_family(pkg.name)
            if family is None:
                continue
            candidates.append((app_dir, pkg, family))
    for app_dir, pkg, family in candidates:
        if component.family_names and family in component.family_names:
            return app_dir, pkg, family
    for app_dir, pkg, family in candidates:
        if is_blacklisted(app_dir.name):
            continue
        if _name_prefix_match(component.name, app_dir.name):
            return app_dir, pkg, family
    return None


def _remove_old_packages(
    component: OEMComponent, matched_family: str, drop_flat_deps: bool = False
) -> list[str]:
    """Удаляет старые главные UWP-пакеты компонента.

    Плоские зависимости (Microsoft.*) убираются только когда их заменяют
    свежие (drop_flat_deps=True) — иначе в папке окажутся две версии одного
    фреймворка, и рекурсивный discovery подхватит обе.
    """
    removed: list[str] = []
    for path in sorted(component.out_dir.rglob("*")):
        if not path.is_file() or _ext_of(path.name) not in UWP_BUNDLE_EXTS:
            continue
        rel = path.relative_to(component.out_dir).as_posix()
        if rel.lower().startswith("dependencies/"):
            continue
        if path.name.startswith(DEPENDENCY_PREFIX):
            if drop_flat_deps:
                path.unlink()
                removed.append(path.name)
                component.packages_removed += 1
            continue
        family = parse_package_family(path.name)
        if family is not None and family != matched_family:
            continue
        path.unlink()
        removed.append(path.name)
        component.packages_removed += 1
    return removed


def update_uwp_scripts(
    component: OEMComponent, old_names: list[str], new_name: str
) -> int:
    """Заменяет захардкоженные имена старых бандлов на новое внутри .cmd/.bat."""
    replaced = 0
    for path in sorted(component.out_dir.rglob("*")):
        if not path.is_file() or _ext_of(path.name) not in (".cmd", ".bat"):
            continue
        text = path.read_text("utf-8", errors="replace")
        new_text = text
        for old in old_names:
            new_text = re.sub(rf"\b{re.escape(old)}\b", new_name, new_text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            replaced += 1
    return replaced


def _uwp_script_files(component: OEMComponent) -> list[Path]:
    """Возвращает .cmd/.bat-скрипты компонента в итоговой папке."""
    return [
        p
        for p in sorted(component.out_dir.rglob("*"))
        if p.is_file() and _ext_of(p.name) in (".cmd", ".bat")
    ]


def _is_dism_script(text: str) -> bool:
    """Живой DISM-скрипт: add-provisionedappxpackage + перебор Microsoft.*.

    Перебор зависимостей опознаётся по маркерным строкам dir /s /b
    Microsoft*_xNN*.appx, которые переписываются на общий пул.
    """
    return (
        "add-provisionedappxpackage" in text
        and (
            "dir /s /b Microsoft*_x86*.appx" in text
            or "dir /s /b Microsoft*_x64*.appx" in text
        )
    )


def _rewrite_dism_script_text(
    text: str, prefix: str = f"%~dp0..\\{DEPS_DIR_NAME}\\"
) -> str | None:
    r"""Переписывает поиск зависимостей DISM-скрипта на общий пул.

    Точечно дописывает префикс к перебору Microsoft.*-зависимостей (главный
    бандл, лицензии и кастомные файлы остаются локальными, т.к. лежат в папке
    компонента). По умолчанию префикс — %~dp0..\Dependencies\, но для
    компонентов в Drivers/ передаётся %~dp0..\..\Apps\Dependencies\.
    Возвращает новый текст или None, если ни один ожидаемый паттерн не найден.
    """
    patterns = (
        'dir /s /b "%TargetVCLib%*_x64*.appx"',
        'dir /s /b "%TargetVCLib%*_x86*.appx"',
        "dir /s /b Microsoft*_x86*.appx",
        "dir /s /b Microsoft*_x64*.appx",
    )
    out = text
    for pattern in patterns:
        if pattern.startswith('dir /s /b "'):
            replacement = f"dir /s /b \"{prefix}{pattern.partition('dir /s /b \"')[2]}"
        else:
            replacement = (
                f"dir /s /b \"{prefix}{pattern.partition('dir /s /b ')[2]}\""
            )
        out = out.replace(pattern, replacement)
    return out if out != text else None


def _apply_dism_rewrite(script: Path, new_text: str) -> None:
    """Пишет переписанный DISM-скрипт, сохраняя оригинал в <имя>.orig."""
    orig = script.with_name(script.name + ".orig")
    if not orig.is_file():
        shutil.copy2(script, orig)
    script.write_text(new_text, encoding="utf-8")


def _soften_dism_failure(text: str) -> str:
    """Заменяет ветку :Reboot на мягкое предупреждение и продолжение.

    При ошибке DISM (обычно 0x80073CFD — отсутствует парный драйвер) OEM-скрипт
    показывал MsgBox и timeout/pause. Для пользовательской установки это меняется
    на понятное сообщение и переход к завершению: приложение поставится повторным
    запуском после появления драйвера. Если ветки нет — текст не меняется.
    """
    soft_lines = (
        "\tif exist UWPFAIL_rebooted.tag del /f /q UWPFAIL_rebooted.tag >NUL 2>&1\r\n"
        "\tECHO %DATE% %TIME%[Log TRACE]  UWP skipped: paired driver "
        "required, code %DISMErrCode%.>>%LogPath%\r\n"
        "\tECHO [WARNING] App [%~n0] not installed, code %DISMErrCode%. "
        "Reason: paired driver missing (VGA/Realtek/DTS). "
        "Install driver and run Install_Factory.bat again.\r\n"
        "\tgoto :END\r\n"
    )
    return re.sub(
        r"^[ \t]*call :Reboot[ \t]*\r?$",
        soft_lines,
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _has_setup_exe(folder: Path) -> bool:
    """Есть ли в компоненте чёрно-ящичный Setup.exe (механизм неизвестен)."""
    return any(
        p.is_file() and p.name.lower() == "setup.exe" for p in folder.rglob("*")
    )


def _strip_script_pauses(script_path: Path) -> None:
    """Убирает интерактивные pause из скриптов компонентов для пакетной установки."""
    try:
        raw = script_path.read_bytes()
    except OSError:
        return
    new_lines: list[bytes] = []
    changed = False
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if re.match(rb"^pause\b", stripped, re.IGNORECASE):
            indent = line[: len(line) - len(line.lstrip())]
            ending = (
                b"\r\n"
                if line.endswith(b"\r\n")
                else (b"\n" if line.endswith(b"\n") else b"")
            )
            new_lines.append(
                indent + b"REM pause suppressed for batch install" + ending
            )
            changed = True
        else:
            new_lines.append(line)
    if changed:
        script_path.write_bytes(b"".join(new_lines))


def _uwp_deps_mode(component: OEMComponent) -> str:
    """Решает, куда положить зависимости компонента: 'shared' или 'per_app'.

    'shared' — зависимости в общий пул Output/<Dev>/Apps/Dependencies,
    DISM-скрипты переписываются на него (процедурный провижининг это
    переживает). 'per_app' — зависимости остаются в папке компонента
    (Setup.exe и скрипты неизвестной механики), либо DISM-скрипт не
    удалось переписать по паттерну.
    """
    scripts = _uwp_script_files(component)
    dism_scripts: list[Path] = []
    for script in scripts:
        text = script.read_text("utf-8", errors="replace")
        if _is_dism_script(text):
            dism_scripts.append(script)
    if not dism_scripts:
        if scripts or _has_setup_exe(component.out_dir):
            return "per_app"
        return "shared"
    prepared: list[tuple[Path, str]] = []
    min_depth = 2
    depth = len(component.rel_path.parts)
    if depth >= min_depth:
        device_root = component.out_dir.parents[depth - 1]
        pool_dir = device_root / "Apps" / DEPS_DIR_NAME
    else:
        pool_dir = component.out_dir.parent / DEPS_DIR_NAME
    for script in dism_scripts:
        text = script.read_text("utf-8", errors="replace")
        rel = os.path.relpath(pool_dir, script.parent).replace("/", "\\")
        prefix = f"%~dp0{rel}\\"
        new_text = _rewrite_dism_script_text(text, prefix=prefix)
        if new_text is None:
            log.warning(
                "Компонент '%s': DISM-скрипт %s не переписывается — "
                "зависимости останутся в папке",
                component.name,
                script.name,
            )
            return "per_app"
        prepared.append((script, _soften_dism_failure(new_text)))
    for script, new_text in prepared:
        _apply_dism_rewrite(script, new_text)
    return "shared"


def _merge_uwp(component: OEMComponent, ctx: MergeContext) -> None:
    """Заменяет UWP-бандл компонента свежим из Cache/<Dev>/Apps и правит скрипты.

    Зависимости раскладываются либо в общий пул Output/Apps/Dependencies
    (DISM-компоненты с переписанными скриптами и приложения без скриптов),
    либо в папку компонента (Setup.exe-компоненты и фолбэк).
    """
    apps_dir = ctx.apps_dir
    report = ctx.report
    match = _find_fresh_uwp_app(apps_dir, component)
    if match is None:
        report.unmatched.append(component.name)
        log.info("Компонент '%s': свежий UWP-пакет не найден", component.name)
        return
    app_dir, pkg, family = match
    ctx.claimed_apps.add(app_dir)
    component.matched_sources.append(pkg)
    deps = _app_dep_files(app_dir, apps_dir / DEPS_DIR_NAME)
    old_names = _remove_old_packages(component, family, drop_flat_deps=bool(deps))

    shared = bool(
        deps
        and ctx.shared_deps_dir is not None
        and _uwp_deps_mode(component) == "shared"
    )
    deps_dir = ctx.shared_deps_dir if shared else component.out_dir / DEPS_DIR_NAME

    new_dest = component.out_dir / pkg.name
    existed = new_dest.exists()
    new_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pkg, new_dest)
    if existed:
        component.files_overwritten += 1
    else:
        component.files_added += 1

    for src in deps:
        if not src.is_file():
            continue
        if _package_arch(src.name) not in (NEUTRAL_ARCH, ctx.target_arch):
            log.info(
                "Зависимость '%s' не под целевую arch=%s — пропущена",
                src.name,
                ctx.target_arch,
            )
            continue
        target = deps_dir / src.name
        existed = target.exists()
        if existed and shared:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        if existed:
            component.files_overwritten += 1
        else:
            component.files_added += 1

    update_uwp_scripts(component, old_names, pkg.name)
    log.info(
        "Компонент '%s': UWP-пакет заменён {%s} -> %s",
        component.name,
        ", ".join(old_names) or "нет старого",
        pkg.name,
    )


def _prepackage_execs(xml_text: str) -> list[str]:
    r"""Вытаскивает %RELATIVE_PATH%\<скрипт> из ProcessList/ReinstallProcessList."""
    targets: list[str] = []
    for raw in _EXEC_RE.findall(xml_text):
        cleaned = raw.strip().replace("/", "\\")
        if cleaned.upper().startswith(RELATIVE_PATH_TOKEN):
            targets.append(cleaned[len(RELATIVE_PATH_TOKEN) :].lstrip("\\"))
    return targets


def _script_is_uwp_installer(script: Path) -> bool:
    """Скрипт UWP-провижининга (DISM), а не установщик классических драйверов."""
    return script.name.lower() in ("install_uwp.cmd", "setup_app.cmd")


def generate_factory_install_bat(out_dir: Path) -> Path:
    """Генерирует Install_Factory.bat, запускающий все заводские скрипты.

    Классические установщики драйверов (Setup_Driver.cmd/Install.cmd/*.exe)
    вызываются раньше UWP-провижининга (Install_UWP.cmd/Setup_APP.cmd):
    UWP-утилиты часто требуют парный драйвер (0x80073CFD), поэтому драйвер
    должен быть установлен до DISM-провижининга приложения.
    """
    out_dir = Path(out_dir)
    lines = [
        "@echo off",
        "net session >nul 2>&1",
        "if %errorLevel% neq 0 (",
        "    echo [ERROR] Please run this script as ADMINISTRATOR!",
        "    pause",
        "    exit /b",
        ")",
        "echo Starting factory installation scripts...",
        "",
    ]
    seen: set[Path] = set()
    handled_dirs: set[Path] = set()
    driver_entries: list[tuple[Path, str]] = []
    uwp_entries: list[tuple[Path, str]] = []

    def _collect(script: Path) -> None:
        if script in seen:
            return
        seen.add(script)
        win_rel = script.relative_to(out_dir.resolve()).as_posix()
        win_rel = win_rel.replace("/", "\\")
        if _script_is_uwp_installer(script):
            uwp_entries.append((script, win_rel))
        else:
            driver_entries.append((script, win_rel))

    for xml in sorted(out_dir.rglob(PREPACKAGE_XML)):
        script_dirs: list[Path] = []
        for rel_target in _prepackage_execs(xml.read_text("utf-8", errors="replace")):
            target_path = rel_target.replace("\\", "/")
            script = (xml.parent / target_path).resolve()
            if not script.is_file():
                log.warning("Заводской скрипт из %s не найден: %s", xml, rel_target)
                continue
            script_dirs.append(script)
        if script_dirs:
            handled_dirs.add(xml.parent.resolve())
        for script in script_dirs:
            _collect(script)

    for detail in sorted(out_dir.rglob("Detail.txt")):
        parent = detail.parent.resolve()
        if parent in handled_dirs:
            continue
        for name in ENTRY_SCRIPT_NAMES:
            script = parent / name
            if not script.is_file():
                continue
            handled_dirs.add(parent)
            _collect(script)
            break

    for script, win_rel in driver_entries + uwp_entries:
        lines.append(f"echo -> {win_rel}")
        if script.suffix.lower() in (".cmd", ".bat"):
            lines.append(f'call "%~dp0{win_rel}"')
        else:
            lines.append(f'start "" /wait "%~dp0{win_rel}"')
        lines.append("")

    lines += ["echo.", "echo Done!", "pause"]
    bat = out_dir / FACTORY_BAT_NAME
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return bat


def _meta_snapshot_components(
    template_dir: Path, meta: dict
) -> list[OEMComponent]:
    """Компоненты-снимки (source: system) из .device.json шаблона.

    kind записи определяет тип компонента: kind="uwp" собирается как
    приложение (подтягивает свежий пакет по family), остальные — как
    классические драйверы по HWID.
    """
    components: list[OEMComponent] = []
    for entry in meta.get("components", []):
        if entry.get("source") != "system":
            continue
        rel = Path(entry.get("rel_path", ""))
        if not rel.parts:
            continue
        components.append(
            OEMComponent(
                name=entry.get("name", rel.as_posix()),
                rel_path=rel,
                kind=str(entry.get("kind") or "classic"),
                source=template_dir,
                out_dir=Path(),
                blacklisted=False,
                family_names=[str(f) for f in entry.get("family_names", [])],
                hwids=[str(h) for h in entry.get("hwids", [])],
                snapshot=True,
            )
        )
    return components


def _fill_meta_hwids(template_dir: Path, components: list[OEMComponent]) -> None:
    """Дополняет классические компоненты HWID из .device.json шаблона.

    После slim-чистки в шаблоне может не остаться .inf (payload вырезан,
    остался только каркас с Setup_Driver.cmd). HWID тогда берутся из
    метаданных шаблона — иначе _merge_classic не сматчит свежие листья.
    """
    meta = read_device_meta(template_dir)
    by_rel: dict[str, list[str]] = {}
    for entry in meta.get("components", []):
        rel = str(entry.get("rel_path", "")).replace("\\", "/")
        if rel:
            by_rel[rel] = [str(h) for h in entry.get("hwids", [])]
    for component in components:
        if component.kind != "classic" or component.hwids or component.snapshot:
            continue
        meta_hwids = by_rel.get(component.rel_path.as_posix(), [])
        if meta_hwids:
            component.hwids = sorted({h.upper() for h in meta_hwids})


def _load_template_components(
    template_dir: Path, drivers_dir: Path, apps_dir: Path
) -> list[OEMComponent]:
    """Компоненты шаблона: из манифеста .components или эвристическим скан-ом.

    Без манифеста: если в шаблоне остались .zip — поднимает ValueError
    с подсказкой снять шаблон заново (prepare_template). Кэш драйверов
    и приложений из скана исключается. HWID классических компонентов
    дополняются из .device.json (после slim-чистки .inf может не быть).
    """
    manifest = _read_template_manifest(template_dir)
    if manifest is not None:
        components = [_make_component(template_dir, folder) for folder in manifest]
        _fill_meta_hwids(template_dir, components)
        return components

    scanned = scan_oem_components(template_dir)
    zip_sources = [
        str(comp.source.relative_to(template_dir))
        for comp in scanned
        if comp.source.is_file() and comp.source.suffix.lower() == ".zip"
    ]
    if zip_sources:
        shown = ", ".join(zip_sources[:MAX_SHOWN_ZIPS]) + (
            "..." if len(zip_sources) > MAX_SHOWN_ZIPS else ""
        )
        raise ValueError(
            "В шаблоне найдены файлы .zip — сначала выполните prepare-oem: "
            f"{shown}"
        )
    components = [
        comp
        for comp in scanned
        if comp.source != drivers_dir
        and drivers_dir not in comp.source.parents
        and comp.source != apps_dir
        and apps_dir not in comp.source.parents
    ]
    _fill_meta_hwids(template_dir, components)
    return components


def _extend_snapshot_components(
    template_dir: Path, components: list[OEMComponent], snapshot_hwids: list[str] | None
) -> None:
    """Дополняет список компонентов снимком системы (source: system)."""
    if snapshot_hwids:
        added = merge_snapshot_devices(template_dir, snapshot_hwids)
        if added:
            log.info("Снимок системы: добавлено устройств в шаблон: %d", len(added))
    snapshot_comps = _meta_snapshot_components(
        template_dir, read_device_meta(template_dir)
    )
    components.extend(snapshot_comps)


@dataclass
class MergeContext:
    """Контекст мержа: каталоги кэша и аккумуляторы отчёта сборки."""

    apps_dir: Path
    fresh_leaves: list[Path]
    report: MergeReport
    claimed: set[Path] = field(default_factory=set)
    claimed_apps: set[Path] = field(default_factory=set)
    shared_deps_dir: Path | None = None
    target_arch: str = "x64"


def _build_component(
    component: OEMComponent, out_dir: Path, ctx: MergeContext
) -> None:
    """Готовит компонент в выходе и выполняет мерж свежих файлов."""
    try:
        if component.snapshot:
            component.out_dir = out_dir / component.rel_path
            component.out_dir.mkdir(parents=True, exist_ok=True)
        else:
            _prepare_component(component, out_dir)
    except (OSError, zipfile.BadZipFile) as exc:
        component.error = str(exc)
        ctx.report.errors.append(f"{component.name}: {exc}")
        return
    if component.blacklisted:
        ctx.report.ignored.append(component.name)
        return
    if component.kind == "uwp":
        _merge_uwp(component, ctx)
    else:
        _merge_classic(component, ctx.fresh_leaves, ctx.claimed, ctx.report)


def _app_dep_files(app_dir: Path, pool: Path) -> list[Path]:
    """Возвращает файлы зависимостей приложения из пула или legacy-папки.

    Свежие загрузки (uwp.py) пишут .deps.json со списком имён пакетов из
    общего пула Cache/<Dev>/Apps/Dependencies. Старые кэши хранили
    зависимости в <Приложение>/Dependencies — такие читаются как есть,
    чтобы сборка ничего не потеряла.
    """
    meta = app_dir / DEPS_META_NAME
    if meta.is_file():
        try:
            raw = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            log.warning("Не читается %s — зависимости не скопируются", meta)
            return []
        return [pool / Path(str(name)).name for name in raw if str(name)]
    legacy = app_dir / DEPS_DIR_NAME
    if legacy.is_dir():
        return [p for p in sorted(legacy.rglob("*")) if p.is_file()]
    return []


def copy_fresh_apps(
    apps_dir: Path,
    dest_apps: Path,
    claimed_apps: set[Path] | None = None,
    arch: str = "x64",
) -> None:
    """Копирует свежие приложения из кэша в пакет, материализуя зависимости.

    Зависимости берутся из общего пула Dependencies (по .deps.json) или из
    legacy-папки приложения и раскладываются в общий пул пакета
    <пакет>/Apps/Dependencies/ — у незаявленных приложений в пакете нет
    собственного инсталлятора, поэтому общая папка безопасна и не дублирует
    одинаковые фреймворки. В пул копируются только neutral и arch пакеты
    (строгий x64: x86/arm/arm64 в legacy-кэшах отбрасываются). Служебный
    .deps.json, legacy-папка Dependencies и сам пул в папку приложения не
    попадают; существующие каталоги назначения не перезаписываются.
    """
    if not apps_dir.is_dir():
        return
    target_arch = arch.lower()
    for app_dir in sorted(p for p in apps_dir.iterdir() if p.is_dir()):
        if app_dir.name == DEPS_DIR_NAME:
            continue
        if claimed_apps and app_dir in claimed_apps:
            continue
        dest = dest_apps / app_dir.name
        if dest.exists():
            continue
        shutil.copytree(
            app_dir,
            dest,
            ignore=shutil.ignore_patterns(DEPS_META_NAME, DEPS_DIR_NAME),
        )
        pool_dest = dest_apps / DEPS_DIR_NAME
        for src in _app_dep_files(app_dir, apps_dir / DEPS_DIR_NAME):
            if not src.is_file() or _package_arch(src.name) not in (
                NEUTRAL_ARCH,
                target_arch,
            ):
                continue
            target = pool_dest / src.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        log.info("Свежее приложение '%s' скопировано в %s", app_dir.name, dest)


def build_oem_package(  # noqa: PLR0913, PLR0917
    template_dir: Path,
    out_dir: Path | None = None,
    drivers_dir: Path | None = None,
    apps_dir: Path | None = None,
    snapshot_hwids: list[str] | None = None,
    arch: str | None = None,
) -> MergeReport:
    """Собирает финальный пакет из шаблона: OEM-компоненты + свежие драйверы/UWP.

    База — Template/<Устройство> (манифест .components + мета .device.json).
    Свежие драйверы читаются из drivers_dir (по умолчанию Cache/<Dev>/Drivers),
    свежие UWP-приложения — из apps_dir (по умолчанию Cache/<Dev>/Apps).
    Выход out_dir (по умолчанию Output/<Dev>) пересоздаётся целиком.

    snapshot_hwids — список HWID из WMI текущей системы: дополняют .device.json
    записями source=system (снимок системы) и участвуют в сборке. Если папок
    кэша нет — мерж не выполняется, OEM-компоненты переносятся как есть.

    arch — целевая архитектура пакетов UWP (по умолчанию из ключа "arch"
    шаблонного .device.json, иначе "x64"). В пул Dependencies попадают только
    neutral и arch пакеты.
    """
    template_dir = Path(template_dir).resolve()
    name = template_dir.name
    meta_arch = str(read_device_meta(template_dir).get("arch", "x64"))
    target_arch = (arch or meta_arch).lower()
    resolve_out = Path(out_dir) if out_dir is not None else Path("Output") / name
    dflt_drivers = Path("Cache") / name / DRIVERS_DIR_NAME
    resolve_drivers = Path(drivers_dir) if drivers_dir is not None else dflt_drivers
    dflt_apps = Path("Cache") / name / APPS_DIR_NAME
    resolve_apps = Path(apps_dir) if apps_dir is not None else dflt_apps
    out_dir = resolve_out.resolve()
    drivers_dir = resolve_drivers
    apps_dir = resolve_apps

    report = MergeReport(
        template_dir=template_dir,
        drivers_dir=drivers_dir,
        apps_dir=apps_dir,
        out_dir=out_dir,
    )
    if not template_dir.is_dir():
        report.errors.append(f"Каталог шаблона не найден: {template_dir}")
        return report
    if out_dir == template_dir:
        report.errors.append("Выходной каталог совпадает с шаблоном: невозможно")
        return report

    components = _load_template_components(template_dir, drivers_dir, apps_dir)
    _extend_snapshot_components(template_dir, components, snapshot_hwids)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    fresh_leaves = collect_fresh_leaves(drivers_dir)
    ctx = MergeContext(
        apps_dir=apps_dir,
        fresh_leaves=fresh_leaves,
        report=report,
        shared_deps_dir=out_dir / APPS_DIR_NAME / DEPS_DIR_NAME,
        target_arch=target_arch,
    )

    for component in components:
        _build_component(component, out_dir, ctx)

    copy_fresh_apps(apps_dir, out_dir / APPS_DIR_NAME, ctx.claimed_apps, target_arch)

    for leaf in ctx.fresh_leaves:
        if leaf not in ctx.claimed:
            report.unmatched.append(str(leaf.relative_to(drivers_dir)))

    for script_path in out_dir.rglob("*"):
        if script_path.is_file() and script_path.suffix.lower() in (".cmd", ".bat"):
            _strip_script_pauses(script_path)

    try:
        generate_factory_install_bat(out_dir)
    except OSError as exc:
        report.errors.append(f"Не удалось сгенерировать {FACTORY_BAT_NAME}: {exc}")

    report.components = components
    return report
