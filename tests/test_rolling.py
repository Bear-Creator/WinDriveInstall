"""Офлайн-тесты rolling-сборки: bat-скрипт, выбор кандидатов, HWID из меты."""

import datetime as dt
import json
from pathlib import Path

from windriveinstall import rolling
from windriveinstall.models import CatalogEntry, LocalDevice


def _entry(version: str) -> CatalogEntry:
    """Запись каталога MS для теста."""
    return CatalogEntry(
        update_id="9f6a1c4d-0000-0000-0000-000000000000",
        title="Driver",
        products="Windows 11",
        classification="Drivers",
        last_updated=dt.date(2026, 1, 1),
        version=version,
        size="1 MB",
    )


def _device(hwid: str) -> LocalDevice:
    """Устройство с одним кандидатом HWID."""
    return LocalDevice(
        name="Widget",
        hwid=hwid,
        driver_version="1.0.0.0",
        manufacturer="Acme",
        device_class="Net",
        driver_date=dt.date(2025, 1, 1),
        hwid_candidates=[hwid],
    )


def test_generate_package_install_bat_portable(tmp_path: Path) -> None:
    """Bat использует %~dp0-относительные пути — пакет переносим."""
    inf = tmp_path / "Drivers" / "Acme" / "Widget" / "widget.inf"
    inf.parent.mkdir(parents=True)
    inf.touch()

    bat = rolling.generate_package_install_bat([inf], tmp_path)

    text = bat.read_text(encoding="utf-8")
    assert "pnputil /add-driver" in text
    assert "%~dp0Drivers/Acme/Widget/widget.inf" in text
    assert "ADMINISTRATOR" in text


def test_best_catalog_entries_picks_best_version(monkeypatch) -> None:
    """Выбирается запись с максимальной версией (без порога is_newer)."""
    hwid = "PCI\\VEN_1234&DEV_5678"
    entries = [_entry("10.1.0.0"), _entry("10.5.0.0")]

    def fake_fallback(candidates, session=None):
        return candidates[0], entries

    monkeypatch.setattr(rolling, "search_catalog_with_fallback", fake_fallback)

    best = rolling._best_catalog_entries([_device(hwid)], session=None)

    assert best[0][1].version == "10.5.0.0"
    assert best[0][2] == hwid


def test_meta_hwids_to_devices(tmp_path: Path) -> None:
    """HWID из .device.json превращаются в устройства для поиска в каталоге."""
    hwid = "USB\\VID_0E8D&PID_7961&MI_00"
    meta_dir = tmp_path / "Template"
    meta_dir.mkdir()
    (meta_dir / ".device.json").write_text(
        json.dumps(
            {
                "name": "Template",
                "components": [
                    {
                        "rel_path": "Bluetooth_MTK_1.3.15.141_W11x64_A",
                        "name": "Bluetooth_MTK_1.3.15.141_W11x64_A",
                        "kind": "classic",
                        "hwids": [hwid],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    devices = rolling._meta_hwids_to_devices(meta_dir)

    assert len(devices) == 1
    assert devices[0].hwid == hwid
    assert devices[0].hwid_candidates == [hwid]


def test_run_rolling_offline(tmp_path: Path, monkeypatch) -> None:
    """Rolling собирает Drivers/ + install_drivers.bat без сети и Windows."""
    out = tmp_path / "Output" / "current"
    cache = tmp_path / "Cache" / "current"
    dev = _device("USB\\VID_0001&PID_0002")

    monkeypatch.setattr(rolling, "get_local_devices", lambda: [dev])
    monkeypatch.setattr(
        rolling,
        "_best_catalog_entries",
        lambda devices, session: [(dev, _entry("10.5.0.0"), dev.hwid)],
    )

    def fake_dl(candidates, root, options):
        for dev, entry, _hwid in candidates:
            dest = root / dev.manufacturer / dev.name / f"Driver_{entry.version}"
            dest.mkdir(parents=True)
            (dest / "widget.inf").write_text("NEW INF\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(rolling, "_download_candidates", fake_dl)

    rolling.run_rolling(out, cache, options=rolling.DownloadOptions())

    leaf_inf = out / "Drivers" / "Acme" / "Widget" / "Driver_10.5.0.0" / "widget.inf"
    assert leaf_inf.exists()
    assert (out / "install_drivers.bat").is_file()


def test_download_options_defaults() -> None:
    """Значения по умолчанию: firefox, headless, Retail, без форсирования."""
    options = rolling.DownloadOptions()
    assert options.browser == "firefox"
    assert options.headless is True
    assert options.ring == "Retail"
    assert options.force is False


def test_update_template_drivers_runs_prune(tmp_path: Path, monkeypatch) -> None:
    """driver-update после скачивания листьев вызывает apply_second_run."""
    hwid = "PCI\\VEN_1234&DEV_5678"
    meta_dir = tmp_path / "Template"
    meta_dir.mkdir()
    (meta_dir / ".device.json").write_text(
        json.dumps(
            {
                "name": "Template",
                "arch": "x64",
                "components": [
                    {
                        "rel_path": "Drivers/Airplane Mode_Acer_1.0.0.10_W11x64_A",
                        "name": "Airplane Mode",
                        "kind": "classic",
                        "blacklisted": False,
                        "version": "1.0.0.0",
                        "family_names": [],
                        "hwids": [hwid],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cache = tmp_path / "Cache" / "Drivers"
    cache.mkdir(parents=True)

    monkeypatch.setattr(rolling, "get_http_session", lambda: None)
    monkeypatch.setattr(
        rolling,
        "_best_catalog_entries",
        lambda devices, session: [(d, _entry("1.0.0.0"), d.hwid) for d in devices],
    )
    monkeypatch.setattr(rolling, "_download_candidates", lambda *a, **k: 1)

    calls: list[tuple[Path, Path]] = []

    def fake_second(template_dir: Path, drivers_dir: Path) -> None:
        calls.append((template_dir, drivers_dir))

    monkeypatch.setattr(rolling, "apply_second_run", fake_second)

    rolling.update_template_drivers(meta_dir, cache, options=rolling.DownloadOptions())

    assert calls == [(meta_dir, cache)]


def test_generate_apps_install_bat_none_when_empty(tmp_path: Path) -> None:
    """Возвращает None, если папки Apps нет или в ней нет приложений."""
    assert rolling.generate_apps_install_bat(tmp_path) is None

    (tmp_path / "Apps").mkdir()
    assert rolling.generate_apps_install_bat(tmp_path) is None


def test_generate_apps_install_bat_portable_dism(tmp_path: Path) -> None:
    """Генерирует install_apps.bat с DISM, зависимостями и %~dp0 путями."""
    apps_dir = tmp_path / "Apps"
    terminal_dir = apps_dir / "Microsoft.WindowsTerminal"
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "terminal.msixbundle").write_bytes(b"MSIXBUNDLE")

    realtek_dir = apps_dir / "RealtekSemiconductorCorp.RealtekAudioControl"
    realtek_dir.mkdir(parents=True)
    (realtek_dir / "audio.appxbundle").write_bytes(b"APPXBUNDLE")

    deps_dir = apps_dir / "Dependencies"
    deps_dir.mkdir(parents=True)
    vclibs = deps_dir / "Microsoft.VCLibs.appx"
    vclibs.write_bytes(b"VCLIBS")

    cache_apps = tmp_path / "Cache" / "Apps"
    cache_realtek = cache_apps / "RealtekSemiconductorCorp.RealtekAudioControl"
    cache_realtek.mkdir(parents=True)
    (cache_realtek / ".deps.json").write_text(
        json.dumps(["Microsoft.VCLibs.appx"]), encoding="utf-8"
    )

    bat = rolling.generate_apps_install_bat(tmp_path, apps_cache=cache_apps)
    assert bat is not None
    assert bat.is_file()

    text = bat.read_text(encoding="utf-8")
    assert "ADMINISTRATOR" in text
    assert "dism /Online /Add-ProvisionedAppxPackage" in text
    assert "/StubPackageOption:InstallFull" in text
    assert "-ForceApplicationShutdown" in text
    assert "Add-AppxPackage" in text
    expected_term = (
        r'/PackagePath:"%~dp0Apps\Microsoft.WindowsTerminal\terminal.msixbundle"'
        r" /StubPackageOption:InstallFull /SkipLicense"
    )
    assert expected_term in text
    assert (
        r'/PackagePath:"%~dp0Apps\RealtekSemiconductorCorp.RealtekAudioControl\audio.appxbundle"'
        in text
    )
    assert (
        r'/DependencyPackagePath:"%~dp0Apps\Dependencies\Microsoft.VCLibs.appx"'
        in text
    )


def test_generate_apps_install_bat_with_license(tmp_path: Path) -> None:
    """Если у приложения есть файл лицензии XML, используется /LicensePath."""
    app_dir = tmp_path / "Apps" / "CustomApp"
    app_dir.mkdir(parents=True)
    (app_dir / "custom.appx").write_bytes(b"APPX")
    (app_dir / "Custom_License1.xml").write_text("<license/>", encoding="utf-8")

    bat = rolling.generate_apps_install_bat(tmp_path)
    assert bat is not None

    text = bat.read_text(encoding="utf-8")
    assert (
        r'/LicensePath:"%~dp0Apps\CustomApp\Custom_License1.xml"'
        in text
    )
    assert "/SkipLicense" not in text


def test_generate_apps_install_bat_skips_installed_current(tmp_path: Path) -> None:
    """Приложения, версия которых в Windows актуальна, не попадают в батник."""
    app_dir = tmp_path / "Apps" / "QuickAccess"
    app_dir.mkdir(parents=True)
    pkg_name = (
        "AcerIncorporated.QuickAccess_3.0.3001.0_neutral_~_48frkmn4z8aw4.appxbundle"
    )
    (app_dir / pkg_name).write_bytes(b"APPX")

    installed = {
        "acerincorporated.quickaccess_48frkmn4z8aw4": "3.0.3001.0",
    }
    bat = rolling.generate_apps_install_bat(tmp_path, installed_versions=installed)
    assert bat is None

