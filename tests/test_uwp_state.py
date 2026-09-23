"""Офлайн-тесты кэша неудачных UWP-запросов и снимка приложений в шаблон."""

import json
import time
from pathlib import Path

from windriveinstall import builder, uwp
from windriveinstall.builder import DEVICE_META_NAME

NOT_FOUND_STATE = {
    "state": "not_found",
    "ts": int(time.time()) - 3600,
    "msg": "Пакеты не найдены",
}
ERROR_STATE = {"state": "error", "ts": int(time.time()) - 3600, "msg": "403"}


def test_load_query_state_missing_and_corrupt(tmp_path: Path) -> None:
    """Отсутствующий, битый и не-словарный файл кэша дают пустой кэш."""
    assert uwp.load_query_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert uwp.load_query_state(bad) == {}
    lst = tmp_path / "list.json"
    lst.write_text("[]", encoding="utf-8")
    assert uwp.load_query_state(lst) == {}


def test_save_and_load_query_state_roundtrip(tmp_path: Path) -> None:
    """Запись и чтение кэша сохраняют все поля."""
    path = tmp_path / "cache.json"
    state = {"fam_a": NOT_FOUND_STATE, "fam_b": ERROR_STATE}
    uwp.save_query_state(path, state)
    assert uwp.load_query_state(path) == state


def test_failure_is_fresh_honours_ttl() -> None:
    """not_found живёт 7 дней, error — 12 часов; мусорные записи отбрасываются."""
    now = 1_000_000_000.0
    hour = 3600
    day = 24 * 3600
    nf = {"ts": int(now) - hour, "state": "not_found"}
    err = {"ts": int(now) - hour, "state": "error"}
    assert uwp._failure_is_fresh({"a": nf}, "a", now=now) is not None
    assert uwp._failure_is_fresh({"b": err}, "b", now=now) is not None
    nf_old = {"ts": int(now) - 8 * day, "state": "not_found"}
    assert uwp._failure_is_fresh({"c": nf_old}, "c", now=now) is None
    err_old = {"ts": int(now) - 13 * hour, "state": "error"}
    assert uwp._failure_is_fresh({"d": err_old}, "d", now=now) is None
    assert uwp._failure_is_fresh({"e": "junk"}, "e", now=now) is None
    assert uwp._failure_is_fresh({}, "x", now=now) is None


def test_cache_skip_result_message() -> None:
    """Пропуск по кэшу несёт state и пояснение 'кэш до ...'."""
    result = uwp._cache_skip_result("Nitro Sense", NOT_FOUND_STATE)
    assert result.state == "not_found"
    assert result.version == "0"
    assert "кэш до" in (result.message or "")
    assert "Пакеты не найдены" in (result.message or "")


def test_merge_snapshot_apps_dedupe(tmp_path: Path) -> None:
    """Снимок приложений не дублирует известные family и пишет source: system."""
    template = tmp_path / "Template"
    template.mkdir()
    meta = {
        "components": [
            {
                "name": "Old",
                "rel_path": "Apps/Old",
                "kind": "uwp",
                "family_names": ["old.family_a"],
                "hwids": [],
            }
        ]
    }
    (template / DEVICE_META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    apps = [
        ("NanaZip", "NanaZip.NanaZip_8wekyb3d8bbwe"),
        ("Old App", "old.family_a"),
        ("Realtek", "RealtekSemiconductorCorp.RealtekAudioControl_8wekyb3d8bbwe"),
    ]
    added = builder.merge_snapshot_apps(template, apps)

    assert len(added) == 2  # noqa: PLR2004 — из трёх приложений два новых
    saved = builder.read_device_meta(template)
    entries = saved["components"]
    assert len(entries) == 3  # noqa: PLR2004 — один старый + два из снимка
    for entry in entries[1:]:
        assert entry["kind"] == "uwp"
        assert entry["source"] == "system"
    assert {e["family_names"][0].lower() for e in entries} == {
        "old.family_a",
        "nanazip.nanazip_8wekyb3d8bbwe",
        "realteksemiconductorcorp.realtekaudiocontrol_8wekyb3d8bbwe",
    }


def test_uwp_apps_from_meta_system_only(tmp_path: Path) -> None:
    """Из .device.json берутся только source: system, kind: uwp записи."""
    template = tmp_path / "Template"
    template.mkdir()
    meta = {
        "components": [
            {
                "name": "System App",
                "rel_path": "snapshot-apps/System App",
                "kind": "uwp",
                "family_names": ["MicrosoftWindows.Client.Photon_8wekyb3d8bbwe"],
                "hwids": [],
                "source": "system",
            },
            {
                "name": "Classic",
                "rel_path": "snapshot/x",
                "kind": "classic",
                "family_names": [],
                "hwids": ["PCI\\VEN_1111"],
                "source": "system",
            },
            {
                "name": "OEM App",
                "rel_path": "Apps/OEM",
                "kind": "uwp",
                "family_names": ["oem.app_a"],
                "hwids": [],
            },
        ]
    }
    (template / DEVICE_META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    apps = uwp.uwp_apps_from_meta_system(template)
    assert apps == [("System App", "microsoftwindows.client.photon_8wekyb3d8bbwe")]


def test_uwp_apps_from_template_includes_meta(tmp_path: Path) -> None:
    """uwp_apps_from_template дополняется записями снимка из .device.json."""
    template = tmp_path / "Template"
    nitro = template / "Apps" / "Nitro Sense_Acer_3.01.3056_W11x64_A"
    nitro.mkdir(parents=True)
    (nitro / "Install_UWP.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (nitro / "AUMIDs.txt").write_text(
        "AcerIncorporated.NitroSense_wwxyz!App\n", encoding="utf-8"
    )
    (template / ".components").write_text(
        "Apps/Nitro Sense_Acer_3.01.3056_W11x64_A\n", encoding="utf-8"
    )
    meta = {
        "components": [
            {
                "name": "System App",
                "rel_path": "snapshot-apps/System App",
                "kind": "uwp",
                "family_names": ["MicrosoftWindows.Client.Photon_8wekyb3d8bbwe"],
                "hwids": [],
                "source": "system",
            }
        ]
    }
    (template / DEVICE_META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    apps = uwp.uwp_apps_from_template(template)
    assert apps == [
        ("Nitro Sense", "acerincorporated.nitrosense_wwxyz"),
        ("System App", "microsoftwindows.client.photon_8wekyb3d8bbwe"),
    ]
