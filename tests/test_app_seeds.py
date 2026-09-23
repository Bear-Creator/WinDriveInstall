"""Офлайн-тесты app-seed'ов rolling-сборки: парсинг AppX и композиция списков."""

import json

from windriveinstall.rolling import compose_app_seeds
from windriveinstall.utils import (
    is_app_version_up_to_date,
    parse_appx_json,
    parse_appx_versions,
)


def _appx_row(name: str, family: str, framework: bool = False) -> dict:
    """Строка вывода Get-AppxPackage (ConvertTo-Json) для теста."""
    row = {"Name": name, "PackageFamilyName": family}
    if framework:
        row["IsFramework"] = True
    return row


def test_parse_appx_json_single_object() -> None:
    """Единичный объект ConvertTo-Json разбирается в один seed."""
    row = _appx_row("QuickAccess", "AcerIncorporated.QuickAccess_48frkmn4z8aw4")
    apps = parse_appx_json(json.dumps(row))
    assert apps == [("QuickAccess", "AcerIncorporated.QuickAccess_48frkmn4z8aw4")]


def test_parse_appx_json_filters_system_and_unrelated() -> None:
    """Системные приложения Windows, сторонний софт и GUID отбрасываются."""
    realtek_family = (
        "RealtekSemiconductorCorp.RealtekAudioControl_dt26b99r8h8gj"
    )
    avc_family = "Microsoft.AVCEncoderVideoExtension_8wekyb3d8bbwe"
    rows = [
        _appx_row("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe"),
        _appx_row("BioEnrollment", "Microsoft.BioEnrollment_cw5n1h2txyewy"),
        _appx_row("NanaZip", "40174MouriNaruto.NanaZipPreview_gnj4mf6z9tkrc"),
        _appx_row("1527c705-839a-4832-9118-54d4bd6a0c89", "Unknown_12345"),
        _appx_row("Realtek Audio Control", realtek_family),
        _appx_row("AVCEncoderVideoExtension", avc_family),
    ]
    apps = parse_appx_json(json.dumps(rows))
    assert apps == [
        ("Realtek Audio Control", realtek_family),
        ("AVCEncoderVideoExtension", avc_family),
    ]


def test_parse_appx_json_array_filters_frameworks() -> None:
    """Массив: фреймворки и пакеты-зависимости отбрасываются."""
    rows = [
        _appx_row("Nitro Sense", "AcerIncorporated.NitroSense_6q7k6jn2v4h9c"),
        _appx_row("VCLibs", "Microsoft.VCLibs.140.00_8wekyb3d8bbwe", framework=True),
        _appx_row(
            "WindowsAppRuntime", "Microsoft.WindowsAppRuntime.1.7_8wekyb3d8bbwe"
        ),
    ]
    apps = parse_appx_json(json.dumps(rows))
    assert apps == [("Nitro Sense", "AcerIncorporated.NitroSense_6q7k6jn2v4h9c")]


def test_parse_appx_json_invalid_and_empty() -> None:
    """Мусор, пустой ввод и null не роняют парсер."""
    assert parse_appx_json("not json") == []
    assert parse_appx_json("") == []
    assert parse_appx_json("null") == []
    assert parse_appx_json("[]") == []


def test_compose_app_seeds_explicit_first_and_dedupe() -> None:
    """--apps идут первыми, системные — после, дубликаты по target убираются."""
    system = [
        ("Nitro Sense", "AcerIncorporated.NitroSense_6q7k6jn2v4h9c"),
        ("DTS Ultra", "DTSInc.DTSXUltra_t5j2fzbtdg37r"),
    ]
    cli = [
        ("DTS Ultra", "DTSInc.DTSXUltra_t5j2fzbtdg37r"),
        ("Extra App", "ExtraCorp.ExtraApp_xp9xyzp8bbwe"),
    ]
    seeds = compose_app_seeds(system, cli)
    assert seeds == [
        ("DTS Ultra", "DTSInc.DTSXUltra_t5j2fzbtdg37r"),
        ("Extra App", "ExtraCorp.ExtraApp_xp9xyzp8bbwe"),
        ("Nitro Sense", "AcerIncorporated.NitroSense_6q7k6jn2v4h9c"),
    ]


def test_compose_app_seeds_none_system() -> None:
    """Без системы (--no-system-apps) возвращаются только явные seed'ы."""
    assert compose_app_seeds(None, [("A", "fam_a")]) == [("A", "fam_a")]
    assert compose_app_seeds([], [("A", "fam_a")]) == [("A", "fam_a")]
    assert compose_app_seeds([], []) == []


def test_parse_appx_versions() -> None:
    """Парсинг версий установленных приложений возвращает словарь по family и name."""
    rows = [
        {
            "Name": "Microsoft.WindowsTerminal",
            "PackageFamilyName": "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
            "Version": "1.24.11911.0",
        },
        {
            "Name": "VCLibs",
            "PackageFamilyName": "Microsoft.VCLibs_8wekyb3d8bbwe",
            "Version": "14.0.0.0",
            "IsFramework": True,
        },
    ]
    versions = parse_appx_versions(json.dumps(rows))
    assert versions["microsoft.windowsterminal_8wekyb3d8bbwe"] == "1.24.11911.0"
    assert versions["microsoft.windowsterminal"] == "1.24.11911.0"
    assert "vclibs" not in versions


def test_is_app_version_up_to_date() -> None:
    """Сравнение версий с учетом стандартного semver и major-смещения Store."""
    assert is_app_version_up_to_date("1.24.11911.0", "3001.24.11911.0")
    assert not is_app_version_up_to_date("1.24.11911.0", "3001.25.1.0")
    assert is_app_version_up_to_date("3.0.3001.0", "3.0.3001.0")
    assert is_app_version_up_to_date("4.0.3001.0", "3.0.3001.0")
    assert not is_app_version_up_to_date("3.0.3001.0", "3.0.3002.0")
    assert not is_app_version_up_to_date("2026.3.26.0", "2026.918.244.0")
    assert not is_app_version_up_to_date(None, "1.0.0.0")
    assert not is_app_version_up_to_date("1.0.0.0", "0")
