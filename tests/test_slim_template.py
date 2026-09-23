"""Тесты windriveinstall.slim: ужимка шаблона до потребностей + фолбэков.

apply_first_run — одноразовая подготовка (prepare_template.py),
apply_second_run — повторная (driver-update, после скачивания свежих листьев).
"""

import json
from pathlib import Path

from windriveinstall.slim import apply_first_run, apply_second_run


def _write(path: Path, content: str | bytes) -> None:
    """Пишет файл (родительские папки создаются автоматически)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _make_template(root: Path) -> tuple[Path, Path]:
    """Создаёт мини-шаблон Nitro5 и кэш драйверов; возвращает (template, cache)."""
    template = root / "Template"
    cache = root / "Cache" / "Template" / "Drivers"

    _stage_pnputil(template)
    _stage_realtek(template)
    _stage_vga(template)
    _stage_apps(template)
    _write_manifest_and_meta(template)
    _stage_fresh_leaves(cache)
    return template, cache


def _stage_pnputil(template: Path) -> None:
    """Раскладывает pnputil-классику (Airplane/LAN/Bluetooth/WiFi/TouchPad)."""

    def write(rel: str, content: str | bytes) -> None:
        _write(template / rel, content)

    airplane = "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A"
    write(f"{airplane}/Detail.txt", "Projects=X\n")
    write(f"{airplane}/Prepackage.xml", '<P Exec="%RELATIVE_PATH%\\Install.cmd"/>\n')
    write(f"{airplane}/Setup_Driver.cmd", "@echo off\r\n")
    write(f"{airplane}/InfFiles.txt", "AcerAirplaneModeController.inf\r\n")
    write(f"{airplane}/Success.tag", "OK")
    write(f"{airplane}/ACIP_Deploy.ini", "[INFORMATION]\n")
    write(f"{airplane}/AcerAirplaneModeController.inf", "ACPI\\VEN_1025&DEV_1229\n")
    write(f"{airplane}/AcerAirplaneModeController.sys", b"SYS")
    write(f"{airplane}/AcerAirplaneModeController.cat", b"CAT")
    write(f"{airplane}/AcerAirplaneModeController.cer", b"CER")

    lan = "Drivers/LAN_Killer_10.043.0723.2020_W11x64_A"
    lan_inf = "PCI\\VEN_10EC&DEV_2502&SUBSYS_151E1025&REV_21"
    write(f"{lan}/Detail.txt", "Projects=X\n")
    write(f"{lan}/Setup_Driver.cmd", "@echo off\r\n")
    write(f"{lan}/InfFiles.txt", "e2kw10x64.inf\r\n")
    write(f"{lan}/e2kw10x64.inf", b"\xff\xfe" + lan_inf.encode("utf-16-le"))
    write(f"{lan}/e2kw10x64.sys", b"SYS")
    write(f"{lan}/e2kw10x64.cat", b"CAT")

    bt = "Drivers/Bluetooth_MTK_1.3.15.141_W11x64_A"
    write(f"{bt}/Detail.txt", "Projects=X\n")
    write(f"{bt}/Install.cmd", "@echo off\r\n")
    write(f"{bt}/Setup_Driver.cmd", "@echo off\r\n")
    write(f"{bt}/InfFiles.txt", "mtkbtfilter.inf\r\n")
    write(f"{bt}/mtkbtfilter.inf", "USB\\VID_04CA&PID_3802\n")
    write(f"{bt}/mtkbtfilterx.sys", b"SYS")
    write(f"{bt}/mtkbt0.dat", b"DAT")

    wifi = "Drivers/Wireless LAN_MTK_3.0.1.1300_W11x64_A"
    wifi_inf = "PCI\\VEN_14C3&DEV_7961"
    write(f"{wifi}/Detail.txt", "Projects=X\n")
    write(f"{wifi}/Setup_Driver.cmd", "@echo off\r\n")
    write(f"{wifi}/InfFiles.txt", "mtkwl6ex.inf\r\n")
    write(f"{wifi}/mtkwl6ex.inf", b"\xff\xfe" + wifi_inf.encode("utf-16-le"))
    write(f"{wifi}/mtkwl6ex.sys", b"SYS")

    touchpad = "Drivers/TouchPad_ELANTECH_13.6.18.1_W11x64_A"
    write(f"{touchpad}/Detail.txt", "Projects=X\n")
    write(f"{touchpad}/Setup_Driver.cmd", "@echo off\r\n")
    write(f"{touchpad}/Setup.exe", b"MZ")
    write(f"{touchpad}/InfFiles.txt", "X64\\ETDI2C.inf\r\n")
    write(f"{touchpad}/X64/ETDI2C.inf", "HID\\VEN_ELAN&DEV_050A\n")
    write(f"{touchpad}/X64/ETD_Smbus.dll", b"DLL")


def _stage_realtek(template: Path) -> None:
    """Раскладывает Realtek-компонент (обёртка Setup.exe + Win64)."""
    realtek = "Drivers/Audio_Realtek_6.0.9126.1_W11x64_A"

    def write(rel: str, content: str | bytes) -> None:
        _write(template / rel, content)

    write(f"{realtek}/Detail.txt", "Projects=X\n")
    write(f"{realtek}/Prepackage.xml", '<P Exec="%RELATIVE_PATH%\\Setup.exe"/>\n')
    write(f"{realtek}/Setup.exe", b"MZ")
    write(f"{realtek}/data1.cab", b"CAB")
    write(f"{realtek}/ISSetup.dll", b"DLL")
    write(f"{realtek}/layout.bin", b"BIN")
    write(f"{realtek}/RtlUpd.exe", b"MZ")
    write(f"{realtek}/ChCfg.exe", b"MZ")
    write(f"{realtek}/0x0409.ini", "[LANGUAGE]\n")
    write(f"{realtek}/Win64/HDXACPAcer.inf", "HDAUDIO\\FUNC_01&VEN_10EC&DEV_0295\n")
    write(f"{realtek}/Win64/RTKVHD64.sys", b"SYS")
    write(f"{realtek}/Win64/Thirdparty/DTS.inf", "DTS\\APO4x\n")


def _stage_vga(template: Path) -> None:
    """Раскладывает VGA-классику и UWP-приложения VGA Utility."""
    for vga in (
        "Drivers/VGA_AMD_27.20.14032.15001_W11x64_A",
        "Drivers/VGA_NVIDIA_30.0.15.1274_W11x64_A",
    ):
        _write(template / f"{vga}/Detail.txt", "Projects=X\n")
        _write(template / f"{vga}/gpu.inf", "PCI\\VEN_1002&DEV_1636\n")
    for vu, family in (
        ("Drivers/VGA Utility_AMD_10.20.40028.0_W11x64_A", "amd-fam!app"),
        ("Drivers/VGA Utility_NVIDIA_8.1.958.0_W11x64_A", "nv-fam!app"),
    ):
        _write(template / f"{vu}/Detail.txt", "Projects=X\n")
        _write(template / f"{vu}/AUMIDs.txt", f"{family}\n")
        _write(template / f"{vu}/app.appx", b"APPX")


def _stage_apps(template: Path) -> None:
    """Раскладывает прочие компоненты: Chipset, Apps/, BIOS/."""
    chipset = "Drivers/Chipset_AMD_4.03.02.2056_W11x64_A"
    _write(template / f"{chipset}/Detail.txt", "Projects=X\n")
    _write(template / f"{chipset}/AMD_Chipset_Software.exe", b"MZ")
    _write(template / f"{chipset}/ACIP_Deploy.ini", "[INFORMATION]\n")
    for app in (
        "Acer Care Center_Acer_4.00.3060_W11x64_A",
        "Nitro Sense_Acer_3.01.3056_W11x64_A",
        "Quick Access_Acer_3.00.3044_W11x64_A",
        "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A",
    ):
        _write(template / f"Apps/{app}/AUMIDs.txt", "family!app\n")
        _write(template / f"Apps/{app}/app.appx", b"APPX")
    _write(template / "BIOS/BIOS_Acer_1.13_A_A/GH51Z113.exe", b"MZ")


def _write_manifest_and_meta(template: Path) -> None:
    """Пишет .components и .device.json мини-шаблона."""
    manifest = (
        "Apps/Acer Care Center_Acer_4.00.3060_W11x64_A\n"
        "Apps/Nitro Sense_Acer_3.01.3056_W11x64_A\n"
        "Apps/Quick Access_Acer_3.00.3044_W11x64_A\n"
        "Apps/XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A\n"
        "BIOS/BIOS_Acer_1.13_A_A\n"
        "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A\n"
        "Drivers/Audio_Realtek_6.0.9126.1_W11x64_A\n"
        "Drivers/Bluetooth_MTK_1.3.15.141_W11x64_A\n"
        "Drivers/Chipset_AMD_4.03.02.2056_W11x64_A\n"
        "Drivers/LAN_Killer_10.043.0723.2020_W11x64_A\n"
        "Drivers/TouchPad_ELANTECH_13.6.18.1_W11x64_A\n"
        "Drivers/VGA Utility_AMD_10.20.40028.0_W11x64_A\n"
        "Drivers/VGA Utility_NVIDIA_8.1.958.0_W11x64_A\n"
        "Drivers/VGA_AMD_27.20.14032.15001_W11x64_A\n"
        "Drivers/VGA_NVIDIA_30.0.15.1274_W11x64_A\n"
        "Drivers/Wireless LAN_MTK_3.0.1.1300_W11x64_A\n"
    )
    (template / ".components").write_text(manifest, encoding="utf-8")

    components = [
        {
            "rel_path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "kind": "uwp" if "Utility" in rel or rel.startswith("Apps/") else "classic",
            "blacklisted": "VGA Utility" in rel,
            "version": "1.0.0.0",
            "family_names": ["family"],
            "hwids": ["ACPI\\VEN_1025&DEV_1229"]
            if rel == "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A"
            else [],
        }
        for rel in manifest.splitlines()
    ]
    components.append(
        {
            "rel_path": "snapshot/PCI_VEN_1002&DEV_1636",
            "name": "System device PCI\\VEN_1002&DEV_1636",
            "kind": "classic",
            "blacklisted": False,
            "version": "",
            "family_names": [],
            "hwids": ["PCI\\VEN_1002&DEV_1636"],
            "source": "system",
        }
    )
    (template / ".device.json").write_text(
        json.dumps(
            {"name": "Template", "arch": "x64", "components": components}, indent=2
        ),
        encoding="utf-8",
    )


def _stage_fresh_leaves(cache: Path) -> None:
    """Раскладывает свежие листья под HWID существующих pnputil-компонентов."""
    for leaf_name, hwid in (
        ("Airplane_1.0.0.10", "ACPI\\VEN_1025&DEV_1229"),
        ("KillerLAN_10.045", "PCI\\VEN_10EC&DEV_2502&SUBSYS_151E1025&REV_21"),
        ("MT7961_WIFI_3.02", "PCI\\VEN_14C3&DEV_7961"),
        ("ETD_13.6.20.2", "HID\\VEN_ELAN&DEV_050A"),
    ):
        leaf = cache / "Vendor" / leaf_name
        _write(leaf / "fresh.inf", f"{hwid}\n")
        _write(leaf / "fresh.cat", b"CAT")
        _write(leaf / "fresh.sys", b"SYS")


def _meta_by_rel(template: Path) -> dict[str, dict]:
    """Читает .device.json как индекс по rel_path."""
    meta = json.loads((template / ".device.json").read_text("utf-8"))
    return {entry["rel_path"]: entry for entry in meta["components"]}


def test_first_run_passthrough_missing_manifest(tmp_path: Path) -> None:
    """Без .components apply_first_run бросает FileNotFoundError."""
    (tmp_path / "T").mkdir()
    try:
        apply_first_run(tmp_path / "T")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Ожидался FileNotFoundError без манифеста")


def test_slim_pass1_only(tmp_path: Path) -> None:
    """Прогон 1: HWID из UTF-16, удаление VGA, перенос VGA Utility, Realtek pnputil."""
    template, _ = _make_template(tmp_path)
    apply_first_run(template)

    meta = _meta_by_rel(template)
    assert meta["Drivers/LAN_Killer_10.043.0723.2020_W11x64_A"]["hwids"] == [
        "PCI\\VEN_10EC&DEV_2502&SUBSYS_151E1025&REV_21"
    ]
    assert meta["Drivers/Wireless LAN_MTK_3.0.1.1300_W11x64_A"]["hwids"] == [
        "PCI\\VEN_14C3&DEV_7961"
    ]

    assert not (template / "Drivers/VGA_AMD_27.20.14032.15001_W11x64_A").exists()
    assert not (template / "Drivers/VGA_NVIDIA_30.0.15.1274_W11x64_A").exists()
    manifest_text = (template / ".components").read_text()
    assert "Drivers/VGA_AMD_27.20.14032.15001_W11x64_A" not in manifest_text

    for vu in (
        "VGA Utility_AMD_10.20.40028.0_W11x64_A",
        "VGA Utility_NVIDIA_8.1.958.0_W11x64_A",
    ):
        assert (template / f"Apps/{vu}" / "app.appx").is_file()
        assert not (template / f"Drivers/{vu}").exists()
        entry = meta[f"Apps/{vu}"]
        assert entry["blacklisted"] is False

    realtek = template / "Drivers/Audio_Realtek_6.0.9126.1_W11x64_A"
    for wrapper in (
        "Setup.exe", "data1.cab", "ISSetup.dll", "layout.bin", "RtlUpd.exe",
        "ChCfg.exe", "0x0409.ini",
    ):
        assert not (realtek / wrapper).exists()
    assert (realtek / "Setup_Driver.cmd").is_file()
    infs = (realtek / "InfFiles.txt").read_text().splitlines()
    assert "Win64\\HDXACPAcer.inf" in infs
    assert "Win64\\Thirdparty\\DTS.inf" in infs
    prepackage = (realtek / "Prepackage.xml").read_text("utf-8")
    assert 'Exec="%RELATIVE_PATH%\\Setup_Driver.cmd"' in prepackage

    assert "snapshot/PCI_VEN_1002&DEV_1636" in meta


def test_slim_preserves_extra_meta_keys(tmp_path: Path) -> None:
    """Сторонние ключи .device.json (arch) сохраняются обоими прогонами."""
    template, cache = _make_template(tmp_path)
    apply_first_run(template)
    apply_second_run(template, cache)

    meta = json.loads((template / ".device.json").read_text("utf-8"))
    assert meta["arch"] == "x64"


def test_slim_pass2_replaces_or_deletes(tmp_path: Path) -> None:
    """Прогон 2: есть лист — payload вырезан; нет листа — компонент удалён."""
    template, cache = _make_template(tmp_path)
    apply_first_run(template)
    apply_second_run(template, cache)

    manifest = (template / ".components").read_text().splitlines()
    meta = _meta_by_rel(template)

    for kept in (
        "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A",
        "Drivers/LAN_Killer_10.043.0723.2020_W11x64_A",
        "Drivers/Wireless LAN_MTK_3.0.1.1300_W11x64_A",
        "Drivers/TouchPad_ELANTECH_13.6.18.1_W11x64_A",
    ):
        assert kept in manifest
        folder = template / kept
        assert list(folder.rglob("*.inf")) == []
        assert list(folder.rglob("*.sys")) == []
        assert list(folder.rglob("*.cat")) == []
        assert (folder / "Setup_Driver.cmd").is_file()
        assert (folder / "Detail.txt").is_file()

    bt = "Drivers/Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert bt not in manifest
    assert bt not in meta
    assert not (template / bt).exists()

    airplane = template / "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A"
    assert (airplane / "ACIP_Deploy.ini").is_file()
    assert (airplane / "Success.tag").is_file()
    assert set(airplane.iterdir()) == {
        airplane / "Detail.txt",
        airplane / "Prepackage.xml",
        airplane / "Setup_Driver.cmd",
        airplane / "InfFiles.txt",
        airplane / "Success.tag",
        airplane / "ACIP_Deploy.ini",
    }


def test_first_run_defaults_arch(tmp_path: Path) -> None:
    """Шаблон без ключа arch получает x64 после первого прогона."""
    template, _ = _make_template(tmp_path)
    meta_path = template / ".device.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    del meta["arch"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    apply_first_run(template)

    meta = json.loads(meta_path.read_text("utf-8"))
    assert meta["arch"] == "x64"


def test_slim_idempotent(tmp_path: Path) -> None:
    """Повторный запуск со свежим кэшем не меняет результат дальше."""
    template, cache = _make_template(tmp_path)
    apply_first_run(template)
    apply_second_run(template, cache)

    first = {
        "manifest": (template / ".components").read_text(),
        "meta": (template / ".device.json").read_bytes(),
    }
    apply_second_run(template, cache)
    second = {
        "manifest": (template / ".components").read_text(),
        "meta": (template / ".device.json").read_bytes(),
    }
    assert first == second
