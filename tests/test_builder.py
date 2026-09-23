"""Тесты модуля builder: подготовка шаблона и гибридная сборка из шаблона."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from windriveinstall.builder import (
    _copy_driver_files,
    _extract_hwids,
    _find_fresh_uwp_app,
    _load_template_components,
    _meta_snapshot_components,
    _package_arch,
    _rewrite_inf_files_list,
    _soften_dism_failure,
    _strip_script_pauses,
    _uwp_deps_mode,
    build_oem_package,
    collect_fresh_leaves,
    copy_fresh_apps,
    generate_factory_install_bat,
    is_blacklisted,
    merge_snapshot_apps,
    parse_package_family,
    prepare_oem_template,
    read_device_meta,
    update_uwp_scripts,
    write_device_meta,
)
from windriveinstall.models import OEMComponent

DTS_BUNDLE = "DTSInc.DTSXUltra_1.14.2.0_neutral_~_t5j2fzbtdg37r.appxbundle"
OLD_BUNDLE = "bc3197149dc74db3a18593d1c62fc7b3.appxbundle"
BT_OVERWRITTEN = 2
BT_ADDED = 3
NCP_FAMILY = "NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj"
NCP_BUNDLE = "NVIDIACorp.NVIDIAControlPanel_8.1.969.0_x64__56jybvy8sckqj.appx"
FRAMEWORK_PKG = (
    "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
)
VCLIB_PKG = "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx"
DISM_CMD = (
    "@echo off\r\n"
    'pushd "%~dp0"\r\n'
    'dir /s /b "%TargetVCLib%*_x64*.appx" >>%LogPath% 2>&1\r\n'
    'dir /s /b "%TargetVCLib%*_x86*.appx" >>%LogPath% 2>&1\r\n'
    "dir /s /b Microsoft*_x86*.appx >> %LogPath% 2>&1\r\n"
    "dir /s /b Microsoft*_x64*.appx >> %LogPath% 2>&1\r\n"
    'dism /online /add-provisionedappxpackage '
    '/packagepath:"!packagepath!" !DependencyPackage_X64!'
    '/region=all\r\n'
)


def _write_zip(archive: Path, entries: dict[str, bytes | str]) -> None:
    """Пишет тестовый zip-архив из словаря {имя_члена: содержимое}."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zf:
        for name, raw in entries.items():
            content = raw.encode("utf-8") if isinstance(raw, str) else raw
            zf.writestr(name, content)


def _make_fixtures(root: Path) -> tuple[Path, Path, Path, Path]:
    oem = root / "OEM"
    drivers = root / "Drivers"
    apps = root / "Apps"
    out = root / "OutPackage"

    _write_zip(
        oem / "Bluetooth_MTK_1.3.15.141_W11x64_A.zip",
        {
            "MTK_MT7921_BT_V1.3.15.141/Detail.txt": "Projects=Kamiq_CAS\n",
            "MTK_MT7921_BT_V1.3.15.141/Prepackage.xml": (
                '<Prepackage><Process Exec="%RELATIVE_PATH%'
                '\\Install.cmd"/></Prepackage>'
            ),
            "MTK_MT7921_BT_V1.3.15.141/Setup_Driver.cmd": "@echo off\r\n",
            "MTK_MT7921_BT_V1.3.15.141/Install.cmd": "@echo off\r\n",
            "MTK_MT7921_BT_V1.3.15.141/mtkbtfilter.inf": "OEM INF\n",
            "MTK_MT7921_BT_V1.3.15.141/mtkbtfilterx.cat": "OEM CAT\n",
            "MTK_MT7921_BT_V1.3.15.141/mtkbt0.dat": "OEM DAT\n",
            "MTK_MT7921_BT_V1.3.15.141/BT_RAM_CODE.bin": "OEM BIN\n",
        },
    )

    _write_zip(
        oem / "Chipset_AMD_4.03.02.2056_W11x64_A.zip",
        {
            "Chipset_AMD_4.03.02.2056_W11x64/Detail.txt": "Projects=Kamiq_CAS\n",
            "Chipset_AMD_4.03.02.2056_W11x64/Prepackage.xml": (
                '<Prepackage><Process Exec="%RELATIVE_PATH%'
                '\\AMD_Chipset_Software.exe"/></Prepackage>'
            ),
            "Chipset_AMD_4.03.02.2056_W11x64/AMD_Chipset_Software.exe": b"MZ-EXE",
            "Chipset_AMD_4.03.02.2056_W11x64/ACIP_Deploy.ini": "[INFORMATION]\n",
            "Chipset_AMD_4.03.02.2056_W11x64/amd_chipset_software.inf": "OEM INF\n",
        },
    )

    _write_zip(
        oem / "VGA Utility_AMD_10.20.40028.0_W11x64_A.zip",
        {
            "VGA Utility_AMD_10.20.40028.0_W11x64/Detail.txt": "Projects=Kamiq_CAS\n",
            "VGA Utility_AMD_10.20.40028.0_W11x64/"
            "e6dda89df233434ea960aad82fac9046.appx": b"APPX",
        },
    )

    _write_zip(
        oem / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A.zip",
        {
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/AUMIDs.txt": (
                "DTSInc.DTSXUltra_t5j2fzbtdg37r!App\n"
            ),
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/Install_UWP.cmd": (
                f"set PACKAGE={OLD_BUNDLE}\r\n"
            ),
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/"
            f"{OLD_BUNDLE}": b"OLD",
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/"
            f"{OLD_BUNDLE.rsplit('.', 1)[0]}_License1.xml": "<License/>",
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/"
            f"MPAP_{OLD_BUNDLE.rsplit('.', 1)[0]}_001.provxml": "<provxml/>",
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_(DTS Console)/"
            "Microsoft.VCLibs.140.00_14.0.29231.0_x64__8wekyb3d8bbwe.appx": (
                b"VCLIB"
            ),
        },
    )

    leaf = drivers / "Mediatek Inc" / "MediaTek Bluetooth MT7921" / "BT_1.1147.0.616"
    leaf.mkdir(parents=True)
    (leaf / "mtkbtfilter.inf").write_text("NEW INF\n", encoding="utf-8")
    (leaf / "mtkbtfilterx.cat").write_text("NEW CAT\n", encoding="utf-8")
    (leaf / "mtkbtfilterx.sys").write_bytes(b"NEW SYS")
    (leaf / "mtkbtsvc.exe").write_bytes(b"MZ")
    (leaf / "mtkbt_v2.dat").write_text("NEW DAT\n", encoding="utf-8")

    orphan = (
        drivers
        / "Microsoft"
        / "Generic software component"
        / "AudioProcessingObject_10.0.26100"
    )
    orphan.mkdir(parents=True)
    (orphan / "voiceclarity_audio_component.inf").write_text(
        "NEW INF\n", encoding="utf-8"
    )

    app_dir = apps / "DTS_X Ultra"
    app_dir.mkdir(parents=True)
    (app_dir / DTS_BUNDLE).write_bytes(b"NEW BUNDLE")
    (app_dir / ".deps.json").write_text(
        json.dumps(
            [
                "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64"
                "__8wekyb3d8bbwe.appx",
                "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx",
            ]
        ),
        encoding="utf-8",
    )
    pool = apps / "Dependencies"
    pool.mkdir()
    (
        pool
        / "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
    ).write_bytes(b"FRAMEWORK")
    (
        pool / "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx"
    ).write_bytes(b"VCLIB NEW")

    return oem, drivers, apps, out


def _stage_fresh(template: Path, drivers: Path, apps: Path) -> None:
    """Раскладывает свежие драйверы и UWP-приложения по папкам шаблона."""
    shutil.copytree(drivers, template / "Drivers")
    shutil.copytree(apps, template / "FreshApps")


def test_parse_package_family() -> None:
    """Парсит реальные имена пакетов MS Store/Catalog в package family name."""
    assert (
        parse_package_family(
            "DTSInc.DTSSoundUnbound_2026.716.209.0_neutral_~"
            "_t5j2fzbtdg37r.appxbundle"
        )
        == "dtsinc.dtssoundunbound_t5j2fzbtdg37r"
    )
    assert (
        parse_package_family(DTS_BUNDLE)
        == "dtsinc.dtsxultra_t5j2fzbtdg37r"
    )
    assert (
        parse_package_family(
            "NVIDIACorp.NVIDIAControlPanel_8.1.969.0_x64__56jybvy8sckqj.appx"
        )
        == "nvidiacorp.nvidiacontrolpanel_56jybvy8sckqj"
    )
    assert (
        parse_package_family(
            "RealtekSemiconductorCorp.RealtekAudioControl_2.54.397.0_neutral_~_dt26b99r8h8gj.msixbundle"
        )
        == "realteksemiconductorcorp.realtekaudiocontrol_dt26b99r8h8gj"
    )
    assert parse_package_family("bc3197149dc74db3a18593d1c62fc7b3.appxbundle") is None


def test_is_blacklisted() -> None:
    """VGA Utility и NVIDIA Control Panel больше не в чёрном списке (UWP в пакете)."""
    assert not is_blacklisted("VGA Utility_AMD_10.20.40028.0_W11x64_A")
    assert not is_blacklisted("NVIDIA Control Panel App")
    assert not is_blacklisted("VGA_AMD_27.20.14032.15001_W11x64_A")


def _uwp_comp(
    tmp_path: Path, name: str, family: str | None = None
) -> OEMComponent:
    """Минимальный UWP-компонент для проверки подбора свежего пакета."""
    return OEMComponent(
        name=name,
        rel_path=Path("Apps") / name,
        kind="uwp",
        source=Path(),
        out_dir=tmp_path / "Out" / name,
        family_names=[family.lower()] if family else [],
    )


def test_find_fresh_uwp_app_assigns_multi_pkg_dir(tmp_path: Path) -> None:
    """Папка приложения с несколькими главными пакетами раздаётся по family."""
    amd_appx = (
        "AdvancedMicroDevicesInc-2.AMDRadeonSoftware_10.21.30024.0_"
        "x64__0a9344xs7nr4m.appx"
    )
    vga = tmp_path / "Apps" / "VGA Utility"
    vga.mkdir(parents=True)
    (vga / amd_appx).write_bytes(b"A")
    (vga / NCP_BUNDLE).write_bytes(b"N")

    amd = _find_fresh_uwp_app(
        tmp_path / "Apps",
        _uwp_comp(
            tmp_path,
            "VGA Utility_AMD_10.20.40028.0_W11x64_A",
            "advancedmicrodevicesinc-2.amdradeonsofware_0a9344xs7nr4m",
        ),
    )
    ncp = _find_fresh_uwp_app(
        tmp_path / "Apps",
        _uwp_comp(
            tmp_path,
            "VGA Utility_NVIDIA_8.1.958.0_W11x64_A",
            NCP_FAMILY.lower(),
        ),
    )
    assert amd is not None and amd[1].name == amd_appx
    assert ncp is not None and ncp[1].name == NCP_BUNDLE


def test_find_fresh_uwp_app_no_shared_token_cross_match(tmp_path: Path) -> None:
    """Общий токен имени не раздаёт чужие пакеты (VGA/XPERI/Quick Access)."""
    apps = tmp_path / "Apps"
    contents = {
        "Acer Care Center": (
            "AcerIncorporated.AcerCareCenterS_4.0.3001.0_x64__8wekyb3d8bbwe.appx",
        ),
        "Quick Access": (
            "AcerIncorporated.QuickAccess_3.0.3001.0_neutral_~_48frkmn4z8aw4.appxbundle",
        ),
        "VGA Utility": (
            "AdvancedMicroDevicesInc-2.AMDRadeonSoftware_10.21.30024.0_"
            "x64__0a9344xs7nr4m.appx",
            NCP_BUNDLE,
        ),
        "XPERI DTS Utility": (
            "DTSInc.DTSSoundUnbound_2026.918.244.0_neutral_~_t5j2fzbtdg37r.appxbundle",
            DTS_BUNDLE,
        ),
    }
    for folder, files in contents.items():
        d = apps / folder
        d.mkdir(parents=True)
        for fname in files:
            (d / fname).write_bytes(b"X")

    fast_access = _find_fresh_uwp_app(
        apps, _uwp_comp(tmp_path, "Quick Access_Acer_3.00.3044_W11x64_A")
    )
    assert fast_access is not None
    assert fast_access[1].name == (
        "AcerIncorporated.QuickAccess_3.0.3001.0_neutral_~_48frkmn4z8aw4.appxbundle"
    )

    x_ultra = _find_fresh_uwp_app(
        apps,
        _uwp_comp(
            tmp_path,
            "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A",
            "DTSInc.DTSXUltra_t5j2fzbtdg37r",
        ),
    )
    assert x_ultra is not None and x_ultra[1].name == DTS_BUNDLE

    sound_unbound = _find_fresh_uwp_app(
        apps,
        _uwp_comp(
            tmp_path,
            "XPERI DTS Utility_XPERI_2020.4.45.0_W11x64_A",
            "DTSInc.DTSSoundUnbound_t5j2fzbtdg37r",
        ),
    )
    assert sound_unbound is not None and sound_unbound[1].name == (
        "DTSInc.DTSSoundUnbound_2026.918.244.0_neutral_~_t5j2fzbtdg37r.appxbundle"
    )

    care_center = _find_fresh_uwp_app(
        apps, _uwp_comp(tmp_path, "Acer Care Center_Acer_4.00.3060_W11x64_A")
    )
    assert care_center is not None and care_center[1].name == (
        "AcerIncorporated.AcerCareCenterS_4.0.3001.0_x64__8wekyb3d8bbwe.appx"
    )


def test_find_fresh_uwp_app_unmatched_keeps_none(tmp_path: Path) -> None:
    """Компонент без family и без имени-тёзки не забирает чужой пакет."""
    apps = tmp_path / "Apps"
    vga = apps / "VGA Utility"
    vga.mkdir(parents=True)
    (vga / NCP_BUNDLE).write_bytes(b"N")
    comp = _uwp_comp(tmp_path, "Nitro Sense_Acer_3.01.3056_W11x64_A")
    assert _find_fresh_uwp_app(apps, comp) is None


def test_soften_dism_failure_replaces_reboot_branch() -> None:
    """Ветка :Reboot при ошибке DISM заменяется на мягкий пропуск."""
    sample = (
        "SET DISMErrCode=%errorlevel%\r\n"
        "if %DISMErrCode% neq 0 (\r\n"
        "\tECHO %DATE% %TIME%[Log TRACE]  Return code is [%DISMErrCode%], "
        "pop up error message to remind user. >>%LogPath%\r\n"
        "\tcall :Reboot\r\n"
        ") else (\r\n"
        "\tECHO %DATE% %TIME%[Log TRACE]  Return code is [%DISMErrCode%], "
        "leave. >>%LogPath%\r\n"
        ")\r\n"
    )
    out_text = _soften_dism_failure(sample)
    assert "call :Reboot" not in out_text
    assert "goto :END" in out_text
    assert "not installed" in out_text
    assert ") else (" in out_text
    assert _soften_dism_failure("nothing here\n") == "nothing here\n"


def test_copy_driver_files_copies_inf_referenced_files(tmp_path: Path) -> None:
    """_copy_driver_files копирует файлы, на которые ссылается .inf (.avi, .ini)."""
    src = tmp_path / "leaf"
    dest = tmp_path / "component"
    src.mkdir()
    dest.mkdir()
    inf_text = (
        "; comment with unreferenced.avi\n"
        "[SourceDisksFiles]\n"
        "Animation.avi = 1 ; video\n"
        "Config.ini = 1\n"
        ";Ignored.exe = 1\n"
    )
    (src / "driver.inf").write_text(inf_text, encoding="utf-8")
    (src / "driver.sys").write_bytes(b"sys")
    (src / "Animation.avi").write_bytes(b"avi")
    (src / "Config.ini").write_bytes(b"ini")
    (src / "Ignored.exe").write_bytes(b"exe")
    (src / "unreferenced.avi").write_bytes(b"no")

    comp = OEMComponent(
        source=src,
        rel_path=Path("Drivers/Test"),
        name="Test",
        kind="classic",
        out_dir=dest,
    )
    used: set[str] = set()
    _copy_driver_files(src, dest, comp, used)

    assert (dest / "driver.inf").is_file()
    assert (dest / "driver.sys").is_file()
    assert (dest / "Animation.avi").is_file()
    assert (dest / "Config.ini").is_file()
    assert (dest / "Ignored.exe").is_file()  # .exe is in DRIVER_EXTENSIONS
    assert not (dest / "unreferenced.avi").exists()


def test_uwp_dism_rewrite_handles_drivers_subfolder(tmp_path: Path) -> None:
    r"""Для компонентов в Drivers/ префикс указывает на ..\..\Apps\Dependencies."""
    out_root = tmp_path / "Output"
    comp_dir = out_root / "Drivers" / "Audio_Console"
    comp_dir.mkdir(parents=True)
    script = comp_dir / "Install_UWP.cmd"
    script.write_text(DISM_CMD, encoding="utf-8")

    comp = OEMComponent(
        source=comp_dir,
        rel_path=Path("Drivers/Audio_Console"),
        name="Audio_Console",
        kind="uwp",
        out_dir=comp_dir,
    )
    mode = _uwp_deps_mode(comp)
    assert mode == "shared"
    new_cmd = script.read_text(encoding="utf-8")
    assert "%~dp0..\\..\\Apps\\Dependencies\\" in new_cmd


def test_strip_script_pauses(tmp_path: Path) -> None:
    """_strip_script_pauses заменяет PAUSE на REM без нарушения структуры скобок."""
    script = tmp_path / "test.cmd"
    content = (
        "@echo off\r\n"
        "echo Step 1\r\n"
        "PAUSE >NUL\r\n"
        "if %errorlevel% neq 0 (\r\n"
        "    echo Failed\r\n"
        "    pause\r\n"
        ") else (\r\n"
        "    echo OK\r\n"
        ")\r\n"
    )
    script.write_bytes(content.encode("utf-8"))
    _strip_script_pauses(script)
    result = script.read_text(encoding="utf-8")
    assert "PAUSE >NUL" not in result
    assert "\n    pause\r\n" not in result
    assert "REM pause suppressed for batch install" in result
    assert ") else (" in result


def test_factory_bat_orders_drivers_before_uwp(tmp_path: Path) -> None:
    """Установщики драйверов в bat идут раньше UWP-провижининга."""
    out = tmp_path / "Out"
    (out / "Apps" / "DTS" / "Install_UWP.cmd").parent.mkdir(parents=True)
    (out / "Drivers" / "Realtek" / "Setup_Driver.cmd").parent.mkdir(parents=True)
    (out / "Apps" / "DTS" / "Install_UWP.cmd").write_text(
        "@echo off\r\n", encoding="utf-8"
    )
    (out / "Drivers" / "Realtek" / "Setup_Driver.cmd").write_text(
        "@echo off\r\n", encoding="utf-8"
    )
    xml = '<Prepackage><Process Exec="%RELATIVE_PATH%\\{0}"/></Prepackage>'
    (out / "Apps" / "DTS" / "Prepackage.xml").write_text(
        xml.format(r"Install_UWP.cmd"), encoding="utf-8"
    )
    (out / "Drivers" / "Realtek" / "Prepackage.xml").write_text(
        xml.format(r"Setup_Driver.cmd"), encoding="utf-8"
    )
    bat_text = generate_factory_install_bat(out).read_text(encoding="utf-8")
    assert bat_text.index("Setup_Driver.cmd") < bat_text.index("Install_UWP.cmd")


def _component(report, name: str):
    """Возвращает компонент отчёта по имени."""
    return next(c for c in report.components if c.name == name)


def test_build_oem_package(tmp_path: Path) -> None:
    """Собирает пакет: подготовка шаблона из OEM-zip, затем сборка из шаблона."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors
    assert not report.ignored
    assert {c.name for c in report.components} == {
        "Bluetooth_MTK_1.3.15.141_W11x64_A",
        "Chipset_AMD_4.03.02.2056_W11x64_A",
        "VGA Utility_AMD_10.20.40028.0_W11x64_A",
        "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A",
    }

    bt = _component(report, "Bluetooth_MTK_1.3.15.141_W11x64_A")
    assert bt.kind == "classic"
    assert bt.files_overwritten == BT_OVERWRITTEN
    assert bt.files_added == BT_ADDED
    assert "MediaTek Bluetooth MT7921" in str(bt.matched_sources[0])
    assert (bt.out_dir / "mtkbtfilter.inf").read_text() == "NEW INF\n"
    assert (bt.out_dir / "InfFiles.txt").read_text().splitlines() == ["mtkbtfilter.inf"]

    bt_dir = out / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert (bt_dir / "mtkbtfilter.inf").read_text() == "NEW INF\n"
    assert (bt_dir / "mtkbtfilterx.sys").read_bytes() == b"NEW SYS"
    assert (bt_dir / "mtkbtsvc.exe").exists()
    assert (bt_dir / "mtkbt_v2.dat").exists()
    assert (bt_dir / "Setup_Driver.cmd").exists()
    assert (bt_dir / "Detail.txt").exists()
    assert (bt_dir / "BT_RAM_CODE.bin").exists()

    uwp = _component(report, "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A")
    assert uwp.kind == "uwp"
    # Старый бандл и плоская заводская зависимость VCLib удалены (2 пакета).
    vclib_old = "Microsoft.VCLibs.140.00_14.0.29231.0_x64__8wekyb3d8bbwe.appx"
    assert uwp.packages_removed == len([OLD_BUNDLE, vclib_old])

    uwp_dir = out / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    assert not (uwp_dir / OLD_BUNDLE).exists()
    assert (uwp_dir / DTS_BUNDLE).read_bytes() == b"NEW BUNDLE"
    assert (uwp_dir / "AUMIDs.txt").exists()
    assert (uwp_dir / "MPAP_bc3197149dc74db3a18593d1c62fc7b3_001.provxml").exists()
    assert (uwp_dir / f"{OLD_BUNDLE[:-len('.appxbundle')]}_License1.xml").exists()
    # Плоская заводская зависимость удалена — её заменили свежие из пула,
    # иначе в папке было бы две версии VCLib.
    assert not (uwp_dir / vclib_old).exists()
    assert (
        uwp_dir / "Dependencies"
        / "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
    ).read_bytes() == b"FRAMEWORK"
    assert (
        uwp_dir / "Dependencies" / "Microsoft.VCLibs.140.00_14.0.33519.0"
        "_x64__8wekyb3d8bbwe.appx"
    ).read_bytes() == b"VCLIB NEW"
    assert not (uwp_dir / ".deps.json").exists()
    script_text = (uwp_dir / "Install_UWP.cmd").read_text()
    assert DTS_BUNDLE in script_text
    assert OLD_BUNDLE not in script_text

    assert (
        out
        / "VGA Utility_AMD_10.20.40028.0_W11x64_A"
        / "e6dda89df233434ea960aad82fac9046.appx"
    ).is_file()

    assert any("Generic software component" in u for u in report.unmatched)
    assert "Chipset_AMD_4.03.02.2056_W11x64_A" in report.unmatched

    bat = out / "Install_Factory.bat"
    bat_text = bat.read_text(encoding="utf-8")
    assert (
        'call "%~dp0Bluetooth_MTK_1.3.15.141_W11x64_A\\Install.cmd"'
        in bat_text
    )
    assert (
        'start "" /wait "%~dp0Chipset_AMD_4.03.02.2056_W11x64_A'
        '\\AMD_Chipset_Software.exe"'
        in bat_text
    )
    assert "VGA Utility_AMD" not in bat_text
    assert "net session >nul 2>&1" in bat_text


def test_collect_fresh_leaves_skips_nested_inf_folders(tmp_path: Path) -> None:
    """Вложенные .inf-каталоги (HSA-подпакеты Realtek) листом не считаются."""
    drivers = tmp_path / "Drivers"
    pkg = (
        drivers
        / "classic"
        / "RT"
        / "Realtek SoftwareComponent Driver Update (1.0.1007.0)_1.0.1007.0"
    )
    pkg.mkdir(parents=True)
    (pkg / "RealtekService.inf").write_text("INF\n", encoding="utf-8")
    (pkg / "RealtekService.cat").write_bytes(b"CAT")
    (pkg / "AcerPurifiedVoiceHSA_1").mkdir()
    (pkg / "AcerPurifiedVoiceHSA_1" / "AcerPurifiedVoiceHSA.inf").write_text(
        "INF\n", encoding="utf-8"
    )
    single = drivers / "vendor" / "LAN" / "Killer"
    single.mkdir(parents=True)
    (single / "net.inf").write_text("INF\n", encoding="utf-8")

    assert collect_fresh_leaves(drivers) == sorted([pkg, single])


def test_build_nested_inf_package_not_reported_unmatched(tmp_path: Path) -> None:
    """Пакет со вложенными .inf копируется целиком и не даёт фантомов в отчёте."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    bt_leaf = drivers / "Mediatek Inc" / "MediaTek Bluetooth MT7921" / "BT_1.1147.0.616"
    hsa = bt_leaf / "PurifiedVoiceHSA_1"
    hsa.mkdir(parents=True)
    (hsa / "PurifiedVoiceHSA.inf").write_text("NESTED INF\n", encoding="utf-8")
    (hsa / "purified.dll").write_bytes(b"DLL")

    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )

    assert not report.errors
    assert not any("PurifiedVoiceHSA" in u for u in report.unmatched)
    bt_dir = out / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert (bt_dir / "PurifiedVoiceHSA_1" / "PurifiedVoiceHSA.inf").is_file()
    assert (bt_dir / "PurifiedVoiceHSA_1" / "purified.dll").is_file()


def test_prepare_oem_template(tmp_path: Path) -> None:
    """Распаковывает OEM-.zip в шаблон: срез корня, иерархия, единичный файл."""
    oem = tmp_path / "OEM"
    template = tmp_path / "Template"
    _write_zip(
        oem / "Bluetooth_MTK_1.3.15.141_W11x64_A.zip",
        {
            "MTK_MT7921_BT_V1.3.15.141/Install.cmd": "@echo off\r\n",
            "MTK_MT7921_BT_V1.3.15.141/mtkbtfilter.inf": "OEM INF\n",
            "MTK_MT7921_BT_V1.3.15.141/sub/mtkbt0.dat": "DAT\n",
        },
    )
    (oem / "BIOS").mkdir(parents=True)
    _write_zip(
        oem / "BIOS" / "BIOS_Acer_1.13_A_A.zip",
        {"GH51Z113.exe": b"MZ"},
    )
    (oem / "BIOS" / "readme.txt").write_text("plain file", encoding="utf-8")

    components = prepare_oem_template(oem, template)
    assert {c.name for c in components} == {
        "Bluetooth_MTK_1.3.15.141_W11x64_A",
        "BIOS_Acer_1.13_A_A",
    }
    assert all(c.error is None for c in components)

    manifest = (template / ".components").read_text().splitlines()
    assert manifest == [
        "BIOS/BIOS_Acer_1.13_A_A",
        "Bluetooth_MTK_1.3.15.141_W11x64_A",
    ]

    bt = template / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert (bt / "Install.cmd").exists()
    assert (bt / "mtkbtfilter.inf").exists()
    assert (bt / "sub" / "mtkbt0.dat").exists()
    assert not (bt / "MTK_MT7921_BT_V1.3.15.141").exists()

    bios = template / "BIOS"
    assert (bios / "BIOS_Acer_1.13_A_A" / "GH51Z113.exe").read_bytes() == b"MZ"
    assert (bios / "readme.txt").read_text() == "plain file"


def test_prepare_oem_template_refuses_non_empty(tmp_path: Path) -> None:
    """Повторная подготовка в непустую папку шаблона запрещена."""
    oem = tmp_path / "OEM"
    template = tmp_path / "Template"
    _write_zip(
        oem / "Bluetooth_MTK_1.3.15.141_W11x64_A.zip",
        {"MTK_MT7921_BT_V1.3.15.141/Install.cmd": "@echo off\r\n"},
    )
    prepare_oem_template(oem, template)
    with pytest.raises(FileExistsError, match="не пуста"):
        prepare_oem_template(oem, template)


def test_build_oem_package_rejects_zips(tmp_path: Path) -> None:
    """Сборка из шаблона с .zip в составе запрещена с подсказкой prepare-oem."""
    _, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    _write_zip(
        template / "Bluetooth_MTK_1.3.15.141_W11x64_A.zip",
        {"MTK_MT7921_BT_V1.3.15.141/Install.cmd": "@echo off\r\n"},
    )
    with pytest.raises(ValueError, match=r"prepare-oem|prepare_oem"):
        build_oem_package(template, out)


def test_build_oem_package_without_drivers(tmp_path: Path) -> None:
    """Без Template/Drivers сборка оставляет OEM-компоненты как есть."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    report = build_oem_package(template, out)

    assert not report.errors
    assert report.files_overwritten == 0
    assert report.files_added == 0
    bt = out / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert (bt / "mtkbtfilter.inf").read_text() == "OEM INF\n"


def test_build_oem_package_scan_ignores_drivers_folder(tmp_path: Path) -> None:
    """Скан без манифеста не считает Template/Drivers OEM-компонентом."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    (template / ".components").unlink()
    _stage_fresh(template, drivers, apps)
    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )

    assert not report.errors
    assert {c.name for c in report.components} == {
        "Bluetooth_MTK_1.3.15.141_W11x64_A",
        "Chipset_AMD_4.03.02.2056_W11x64_A",
        "VGA Utility_AMD_10.20.40028.0_W11x64_A",
        "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A",
    }
    bt_dir = out / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    assert (bt_dir / "mtkbtfilter.inf").read_text() == "NEW INF\n"


def test_update_uwp_scripts_renames_hardcoded(tmp_path: Path) -> None:
    """Заменяет захардкоженное имя старого бандла в Install_UWP.cmd."""
    comp_dir = tmp_path / "Component"
    comp_dir.mkdir()
    cmd = comp_dir / "Install_UWP.cmd"
    cmd.write_text(
        f"set PACKAGE={OLD_BUNDLE}\r\necho installing\r\n",
        encoding="utf-8",
    )
    (comp_dir / "Readme.txt").write_text(f"uses {OLD_BUNDLE}", encoding="utf-8")

    comp = OEMComponent(
        name="XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A",
        rel_path=Path("Component"),
        kind="uwp",
        source=tmp_path,
        out_dir=comp_dir,
        family_names=["dtsinc.dtsxultra_t5j2fzbtdg37r"],
    )
    replaced = update_uwp_scripts(comp, [OLD_BUNDLE], DTS_BUNDLE)
    assert replaced == 1
    text = cmd.read_text(encoding="utf-8")
    assert f"set PACKAGE={DTS_BUNDLE}" in text
    assert OLD_BUNDLE not in text


def test_extract_hwids(tmp_path: Path) -> None:
    """Достаёт и нормализует HWID из .inf классического компонента."""
    comp = tmp_path / "BT"
    comp.mkdir()
    (comp / "a.inf").write_text(
        'USB\\VID_0E8D&PID_7961&MI_00\\6&3b  "OEM"\n'
        'PCI\\VEN_10EC&DEV_8168&SUBSYS_816810EC&REV_15\n',
        encoding="utf-8",
    )
    (comp / "b.inf").write_text("PCI\\VEN_10EC&DEV_8168\n", encoding="utf-8")
    (comp / "c.inf").write_text("SW\\PROP\nNOT_A_HWID\n", encoding="utf-8")

    hwids = _extract_hwids(comp)
    assert "USB\\VID_0E8D&PID_7961&MI_00" in hwids
    assert "PCI\\VEN_10EC&DEV_8168" in hwids
    assert hwids == sorted(set(hwids))


def test_extract_hwids_utf16(tmp_path: Path) -> None:
    """Читает HWID из .inf в UTF-16LE (Killer LAN, MTK WiFi)."""
    comp = tmp_path / "LAN"
    comp.mkdir()
    text = (
        '[Version]\r\nSignature="$Windows NT$"\r\n[Manufacturer]\r\n'
        "Killer = OEM, NTamd64.10.0\r\n"
        "PCI\\VEN_10EC&DEV_2600&SUBSYS_151E1025&REV_21\r\n"
    )
    (comp / "e2kw10x64.inf").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))

    hwids = _extract_hwids(comp)
    assert "PCI\\VEN_10EC&DEV_2600&SUBSYS_151E1025&REV_21" in hwids
    assert len(hwids) == 1


def test_fill_meta_hwids(tmp_path: Path) -> None:
    """HWID классического компонента берутся из .device.json, если .inf нет."""
    template = tmp_path / "Template"
    comp = template / "Airplane Mode_Acer_1.0.0.10_W11x64_A"
    comp.mkdir(parents=True)
    (comp / "Detail.txt").write_text("Projects=Kamiq_CAS\n", encoding="utf-8")
    (comp / "Setup_Driver.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (template / ".components").write_text(
        "Airplane Mode_Acer_1.0.0.10_W11x64_A\n", encoding="utf-8"
    )
    (template / ".device.json").write_text(
        json.dumps(
            {
                "name": "Template",
                "components": [
                    {
                        "rel_path": "Airplane Mode_Acer_1.0.0.10_W11x64_A",
                        "name": "Airplane Mode_Acer_1.0.0.10_W11x64_A",
                        "kind": "classic",
                        "blacklisted": False,
                        "version": "1.0.0.10",
                        "family_names": [],
                        "hwids": ["ACPI\\VEN_1025&DEV_1229"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    components = _load_template_components(
        template, tmp_path / "no-drivers", tmp_path / "no-apps"
    )
    assert components[0].kind == "classic"
    assert components[0].hwids == ["ACPI\\VEN_1025&DEV_1229"]


def test_rewrite_inf_files_list(tmp_path: Path) -> None:
    """InfFiles.txt пересобирается из фактических .inf в собранном компоненте."""
    out = tmp_path / "Out"
    (out / "X64").mkdir(parents=True)
    (out / "X64" / "ETDI2C.inf").write_text("INF\n", encoding="utf-8")
    (out / "Setup_Driver.cmd").write_text("@echo off\r\n", encoding="utf-8")

    component = OEMComponent(
        name="TouchPad_ELANTECH_13.6.18.1_W11x64_A",
        rel_path=Path("TouchPad_ELANTECH_13.6.18.1_W11x64_A"),
        kind="classic",
        source=tmp_path / "src",
        out_dir=out,
    )
    _rewrite_inf_files_list(component)
    assert (out / "InfFiles.txt").read_text().splitlines() == ["X64\\ETDI2C.inf"]


def test_write_device_meta_roundtrip(tmp_path: Path) -> None:
    """Метаданные шаблона пишутся и читаются обратно."""
    comp = tmp_path / "Chipset"
    comp.mkdir()
    (comp / "chipset.inf").write_text("PCI\\VEN_1002&DEV_1636\n", encoding="utf-8")
    (comp / "Detail.txt").write_text("Projects=Kamiq_CAS\n", encoding="utf-8")
    template = tmp_path / "Template"
    template.mkdir()
    oem_comp = OEMComponent(
        name="Chipset_AMD_4.03.02.2056_W11x64_A",
        rel_path=Path("Chipset"),
        kind="classic",
        source=comp,
        out_dir=comp,
    )

    write_device_meta(template, [oem_comp])
    meta = read_device_meta(template)

    assert meta["name"] == "Template"
    assert meta["components"][0]["hwids"] == ["PCI\\VEN_1002&DEV_1636"]
    assert meta["components"][0]["version"] == "4.03.02.2056"
    assert meta["components"][0]["kind"] == "classic"


def test_build_resets_out_dir(tmp_path: Path) -> None:
    """Каждая сборка полностью пересоздаёт выходную папку."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    build_oem_package(template, out)
    (out / "stale.txt").write_text("мусор", encoding="utf-8")

    report = build_oem_package(template, out)

    assert not report.errors
    assert not (out / "stale.txt").exists()
    assert (out / "Bluetooth_MTK_1.3.15.141_W11x64_A").is_dir()


def test_snapshot_component_merge(tmp_path: Path) -> None:
    """Снапшот-устройства дописываются в .device.json и собираются по HWID."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)

    hwid = "USB\\VID_0E8D&PID_7961&MI_00"
    cache = tmp_path / "Cache" / "Template" / "Drivers"
    leaf = cache / "Widget" / "Widget v1"
    leaf.mkdir(parents=True)
    (leaf / "widget.inf").write_text(
        'USB\\VID_0E8D&PID_7961&MI_00  "Fresh widget"' "\n", encoding="utf-8"
    )
    (leaf / "widget.sys").write_bytes(b"SYS")

    report = build_oem_package(template, out, drivers_dir=cache, snapshot_hwids=[hwid])

    assert not report.errors
    snap = next(c for c in report.components if c.snapshot)
    assert snap.files_added == len(list(leaf.glob("*")))
    assert (out / snap.rel_path / "widget.inf").read_text(encoding="utf-8").startswith(
        "USB\\"
    )

    meta = read_device_meta(template)
    system = [c for c in meta["components"] if c.get("source") == "system"]
    assert len(system) == 1
    assert system[0]["hwids"] == [hwid]


def test_build_copies_unmatched_fresh_apps(tmp_path: Path) -> None:
    """Свежие приложения без компонента (в т.ч. NVIDIA Control Panel) в Output/Apps."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)

    app_dir = template / "FreshApps" / "NVIDIA Control Panel"
    app_dir.mkdir()
    (app_dir / NCP_BUNDLE).write_bytes(b"FRESH CP")
    # У незаявленного приложения тоже есть зависимости из общего пула.
    vclib_new = "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx"
    (app_dir / ".deps.json").write_text(json.dumps([vclib_new]), encoding="utf-8")

    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors
    dest = out / "Apps" / "NVIDIA Control Panel"
    assert (dest / NCP_BUNDLE).read_bytes() == b"FRESH CP"
    # У незаявленного приложения нет своего инсталлятора, поэтому его
    # зависимости материализуются в общий пул Output/Apps/Dependencies.
    assert (out / "Apps" / "Dependencies" / vclib_new).read_bytes() == b"VCLIB NEW"
    assert not (dest / "Dependencies").exists()
    # Служебный .deps.json в папку приложения не попадает.
    assert not (dest / ".deps.json").exists()


def test_uwp_merge_legacy_deps(tmp_path: Path) -> None:
    """Старый кэш (зависимости в <App>/Dependencies, без .deps.json) читается."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)

    app_dir = template / "FreshApps" / "DTS_X Ultra"
    app_dir.mkdir(parents=True)
    (app_dir / DTS_BUNDLE).write_bytes(b"NEW BUNDLE")
    legacy = app_dir / "Dependencies"
    legacy.mkdir()
    (
        legacy
        / "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
    ).write_bytes(b"FRAMEWORK")

    report = build_oem_package(template, out, apps_dir=template / "FreshApps")
    assert not report.errors

    uwp_dir = out / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    assert (
        uwp_dir / "Dependencies"
        / "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
    ).read_bytes() == b"FRAMEWORK"
    # Плоская заводская зависимость заменена legacy-набором из кэша.
    assert not (
        uwp_dir / "Microsoft.VCLibs.140.00_14.0.29231.0_x64__8wekyb3d8bbwe.appx"
    ).exists()


def test_build_dism_component_uses_shared_deps(tmp_path: Path) -> None:
    """DISM-компонент: зависимости в общий пул, скрипты переписаны на него."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    dts = template / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    (dts / "Install_UWP.cmd").write_text(DISM_CMD, encoding="utf-8")

    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors

    uwp_dir = out / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    pool = out / "Apps" / "Dependencies"
    # В папке компонента зависимостей нет — они в общем пуле один раз.
    assert not (uwp_dir / "Dependencies").exists()
    assert (pool / FRAMEWORK_PKG).read_bytes() == b"FRAMEWORK"
    assert (pool / VCLIB_PKG).read_bytes() == b"VCLIB NEW"
    # Скрипт переписан на ..\Dependencies, оригинал сохранён рядом.
    script_text = (uwp_dir / "Install_UWP.cmd").read_text(encoding="utf-8")
    assert r"%~dp0..\Dependencies\Microsoft*_x86*.appx" in script_text
    assert r"%~dp0..\Dependencies\Microsoft*_x64*.appx" in script_text
    assert r"dir /s /b Microsoft*_x64*.appx" not in script_text
    assert r"%~dp0..\Dependencies\%TargetVCLib%*_x86*.appx" in script_text
    orig = uwp_dir / "Install_UWP.cmd.orig"
    assert orig.is_file()
    assert "add-provisionedappxpackage" in orig.read_text(encoding="utf-8")
    # Главный бандл остаётся в папке компонента.
    assert (uwp_dir / DTS_BUNDLE).read_bytes() == b"NEW BUNDLE"


def test_build_dism_component_rewrite_failure_falls_back_to_per_app(
    tmp_path: Path,
) -> None:
    """DISM-скрипт неизвестного вида: зависимости остаются в папке компонента."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    dts = template / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    unknown_cmd = (
        "@echo off\r\n"
        'dism /online /add-provisionedappxpackage /packagepath:"!packagepath!"\r\n'
    )
    (dts / "Install_UWP.cmd").write_text(unknown_cmd, encoding="utf-8")

    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors

    uwp_dir = out / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    assert (uwp_dir / "Dependencies" / FRAMEWORK_PKG).read_bytes() == b"FRAMEWORK"
    assert not (uwp_dir / "Install_UWP.cmd.orig").exists()


def test_build_setup_exe_component_keeps_per_app_deps(tmp_path: Path) -> None:
    """Setup.exe-компонент: зависимости остаются в папке (механизм неизвестен)."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    dts = template / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    (dts / "Install_UWP.cmd").unlink()
    (dts / "Setup.exe").write_bytes(b"MZ")

    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors

    uwp_dir = out / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    assert (uwp_dir / "Dependencies" / FRAMEWORK_PKG).read_bytes() == b"FRAMEWORK"
    assert (uwp_dir / "Setup.exe").read_bytes() == b"MZ"
    # Нет ни одного приложения, направленного в общий пул.
    assert not (out / "Apps" / "Dependencies").exists()


def test_build_shared_deps_dedup_across_components(tmp_path: Path) -> None:
    """Одинаковые зависимости двух DISM-компонентов попадают в пул один раз."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)
    _stage_fresh(template, drivers, apps)
    dts = template / "Apps" / "XPERI DTS Utility_XPERI_1.10.1.0_W11x64_A"
    (dts / "Install_UWP.cmd").write_text(DISM_CMD, encoding="utf-8")

    qa_dir = template / "Apps" / "Quick Access_Acer_3.00.3044_W11x64_A"
    qa_dir.mkdir()
    (qa_dir / "AUMIDs.txt").write_text(
        "QACorp.QuickAccess_abc123!App\n", encoding="utf-8"
    )
    (qa_dir / "Install_UWP.cmd").write_text(DISM_CMD, encoding="utf-8")
    (qa_dir / OLD_BUNDLE).write_bytes(b"OLD QA")
    manifest = template / ".components"
    manifest.write_text(
        manifest.read_text("utf-8")
        + "Apps/Quick Access_Acer_3.00.3044_W11x64_A\n",
        encoding="utf-8",
    )

    qa_app = template / "FreshApps" / "QuickAccess"
    qa_app.mkdir()
    qa_bundle = "QACorp.QuickAccess_1.0.0.0_neutral_~_abc123.appxbundle"
    (qa_app / qa_bundle).write_bytes(b"QA BUNDLE")
    (qa_app / ".deps.json").write_text(json.dumps([VCLIB_PKG]), encoding="utf-8")

    report = build_oem_package(
        template, out, drivers_dir=template / "Drivers", apps_dir=template / "FreshApps"
    )
    assert not report.errors

    pool = out / "Apps" / "Dependencies"
    assert (pool / FRAMEWORK_PKG).read_bytes() == b"FRAMEWORK"
    assert (pool / VCLIB_PKG).read_bytes() == b"VCLIB NEW"
    assert sorted(p.name for p in pool.iterdir()) == sorted(
        [FRAMEWORK_PKG, VCLIB_PKG]
    )
    # Дедупликация: общий VCLib учитывается один раз, а не в каждом компоненте.
    uwp_merged = [
        c for c in report.components if c.kind == "uwp" and c.matched_sources
    ]
    uwp_added = sum(c.files_added for c in uwp_merged)
    assert uwp_added == len(uwp_merged) + len({FRAMEWORK_PKG, VCLIB_PKG})


def test_build_unmatched_legacy_app_folds_deps_into_shared_pool(
    tmp_path: Path,
) -> None:
    """Legacy зависимости незаявленного приложения переезжают в общий пул."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)

    app_dir = template / "FreshApps" / "Legacy App"
    app_dir.mkdir(parents=True)
    (app_dir / NCP_BUNDLE).write_bytes(b"FRESH CP")
    legacy = app_dir / "Dependencies"
    legacy.mkdir()
    (legacy / VCLIB_PKG).write_bytes(b"VCLIB LEGACY")

    report = build_oem_package(template, out, apps_dir=template / "FreshApps")
    assert not report.errors

    # Папка приложения без внутренней legacy-папки зависимостей.
    dest = out / "Apps" / "Legacy App"
    assert (dest / NCP_BUNDLE).read_bytes() == b"FRESH CP"
    assert not (dest / "Dependencies").exists()
    # Файлы legacy-папки уехали в общий пул пакета.
    assert (out / "Apps" / "Dependencies" / VCLIB_PKG).read_bytes() == b"VCLIB LEGACY"


def test_snapshot_uwp_app_merge(tmp_path: Path) -> None:
    """Приложение из снимка (source: system, kind: uwp) попадает в Output/Apps."""
    oem, _, _, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)

    merge_snapshot_apps(template, [("NVIDIA Control Panel", NCP_FAMILY)])
    apps_dir = tmp_path / "Cache" / "Template" / "Apps"
    app_dir = apps_dir / "NVIDIA Control Panel"
    app_dir.mkdir(parents=True)
    (app_dir / NCP_BUNDLE).write_bytes(b"CP")

    report = build_oem_package(template, out, apps_dir=apps_dir)
    assert not report.errors
    dest = out / "Apps" / "NVIDIA Control Panel"
    assert (dest / NCP_BUNDLE).read_bytes() == b"CP"


def test_meta_snapshot_components_kind(tmp_path: Path) -> None:
    """Записи снимка kind: uwp становятся UWP-компонентами с family_names."""
    meta = {
        "components": [
            {
                "name": "System App",
                "rel_path": "Apps/System App",
                "kind": "uwp",
                "family_names": ["microsoftwindows.client.photon_8wekyb3d8bbwe"],
                "hwids": [],
                "source": "system",
            },
            {
                "name": "Snap Device",
                "rel_path": "snapshot/x",
                "kind": "classic",
                "family_names": [],
                "hwids": ["PCI\\VEN_1111"],
                "source": "system",
            },
        ]
    }
    comps = _meta_snapshot_components(tmp_path, meta)
    by_rel = {c.rel_path.as_posix(): c for c in comps}
    uwp = by_rel["Apps/System App"]
    assert uwp.kind == "uwp"
    assert uwp.family_names == ["microsoftwindows.client.photon_8wekyb3d8bbwe"]
    assert uwp.snapshot
    classic = by_rel["snapshot/x"]
    assert classic.kind == "classic"
    assert classic.hwids == ["PCI\\VEN_1111"]


def test_package_arch_helper() -> None:
    """_package_arch находит архитектуру по токену имени пакета."""
    assert _package_arch("App_1.0.0.0_neutral_~_fam.appx") == "neutral"
    assert _package_arch("Microsoft.VCLibs.140.00_14.0.0.0_x64__8wekyb.appx") == "x64"
    assert _package_arch("App_1.0.0.0_x86__fam.appx") == "x86"
    assert _package_arch("plain.bundle") == "neutral"


def test_copy_fresh_apps_drops_non_target_arch(tmp_path: Path) -> None:
    """В общий пул Dependencies копируются только neutral и x64 пакеты."""
    apps = tmp_path / "Apps"
    app_dir = apps / "Nitro Sense"
    app_dir.mkdir(parents=True)
    (app_dir / "app.bundle").write_bytes(b"BUNDLE")
    vclib_name = "Microsoft.VCLibs.140.00_14.0.33519.0_{arch}__8wekyb3d8bbwe.appx"
    deps = [vclib_name.format(arch=a) for a in ("x64", "x86", "arm64")]
    (app_dir / ".deps.json").write_text(json.dumps(deps), encoding="utf-8")
    pool = apps / "Dependencies"
    pool.mkdir()
    for name in deps:
        (pool / name).write_bytes(b"PKG")

    dest = tmp_path / "Out" / "Apps"
    copy_fresh_apps(apps, dest, arch="x64")

    dest_pool = dest / "Dependencies"
    assert {p.name for p in dest_pool.iterdir()} == {deps[0]}
    assert (dest / "Nitro Sense" / "app.bundle").is_file()
    assert not (dest / "Nitro Sense" / ".deps.json").exists()


def test_build_drops_non_target_arch_deps(tmp_path: Path) -> None:
    """В выходной пакет не попадают зависимости x86/arm/arm64 (строгий x64)."""
    oem, drivers, apps, out = _make_fixtures(tmp_path)
    template = tmp_path / "Template"
    prepare_oem_template(oem, template)

    dts = apps / "DTS_X Ultra"
    vclib_x64 = "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx"
    extra = [
        f"Microsoft.VCLibs.140.00_14.0.33519.0_{arch}__8wekyb3d8bbwe.appx"
        for arch in ("x86", "arm", "arm64")
    ]
    meta = json.loads((dts / ".deps.json").read_text("utf-8"))
    (dts / ".deps.json").write_text(json.dumps(meta + extra), encoding="utf-8")
    pool = apps / "Dependencies"
    for name in extra:
        (pool / name).write_bytes(b"PKG")

    _stage_fresh(template, drivers, apps)
    report = build_oem_package(
        template,
        out,
        drivers_dir=template / "Drivers",
        apps_dir=template / "FreshApps",
    )
    assert not report.errors

    pkgs = {p.name for p in out.rglob("*.appx*")}
    assert vclib_x64 in pkgs
    assert not any("_x86_" in n or "_arm_" in n or "_arm64_" in n for n in pkgs)
