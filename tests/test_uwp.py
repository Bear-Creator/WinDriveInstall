"""Тесты модуля uwp: парсинг ссылок rg-adguard и скачивание в apps_root/."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from windriveinstall import uwp
from windriveinstall.models import UwpDownloadResult

ADGUARD_URL = "https://store.rg-adguard.net/api/GetFiles"
CDN = "https://tlu.dl.delivery.mp.microsoft.com/dl/pr/12345/"
FAMILY = "acerincorporated.nitrosense_wwxyz"
NEW_BUNDLE = "AcerIncorporated.NitroSense_3.1.100.0_neutral_~_wwxyz.appxbundle"
OLD_BUNDLE = "AcerIncorporated.NitroSense_3.1.99.0_neutral_~_wwxyz.appxbundle"
NEWEST_BUNDLE = "AcerIncorporated.NitroSense_3.2.0.0_neutral_~_wwxyz.appxbundle"
X64_BUNDLE = "AcerIncorporated.NitroSense_3.1.100.0_x64__wwxyz.appx"
VCLIB = "Microsoft.VCLibs.140.00_14.0.33519.0_x64__8wekyb3d8bbwe.appx"
DOTNET = "Microsoft.NET.Native.Framework.1.7_1.7.27413.0_x64__8wekyb3d8bbwe.appx"
VCLIB_X86 = "Microsoft.VCLibs.140.00_14.0.33519.0_x86__8wekyb3d8bbwe.appx"
VCLIB_ARM64 = "Microsoft.VCLibs.140.00_14.0.33519.0_arm64__8wekyb3d8bbwe.appx"
APP_X64 = "AcerIncorporated.NitroSense_3.1.100.0_x64__wwxyz.appx"
APP_X86 = "AcerIncorporated.NitroSense_3.1.100.0_x86__wwxyz.appx"


def _adguard_html(rows: list[tuple[str, str]]) -> str:
    anchors = "".join(
        f'<a id="g0" rel="noreferrer" target="_blank" href="{url}">{name}</a>'
        for name, url in rows
    )
    return f"<html><body>{anchors}</body></html>"


def _fake_session(response_text: str):
    """Сессия, отвечающая заданным текстом на любые запросы."""

    def _get(*_args, **_kwargs):
        return SimpleNamespace(text=response_text, raise_for_status=lambda: None)

    def _post(*_args, **_kwargs):
        return SimpleNamespace(text=response_text, raise_for_status=lambda: None)

    return SimpleNamespace(get=_get, post=_post)


def test_parse_adguard_links() -> None:
    """Собирает пакеты, пропуская .encrypted и ссылки не с CDN."""
    html = _adguard_html(
        [
            (NEW_BUNDLE, CDN + NEW_BUNDLE),
            (NEW_BUNDLE + ".encrypted", CDN + NEW_BUNDLE + ".encrypted"),
            ("driver.cab", "https://other.example/x.cab"),
            ("readme.txt", "https://download.microsoft.com/readme.txt"),
        ]
    )
    links = uwp.parse_adguard_links(html)
    assert links == [(NEW_BUNDLE, CDN + NEW_BUNDLE)]


def test_pick_uwp_packages_chooses_newest_bundle() -> None:
    """Главный — новейший neutral-бандл семейства, остальное — зависимости."""
    links = [
        (OLD_BUNDLE, CDN + OLD_BUNDLE),
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (X64_BUNDLE, CDN + X64_BUNDLE),
        (VCLIB, CDN + VCLIB),
        (DOTNET, CDN + DOTNET),
    ]
    main, deps = uwp.pick_uwp_packages(links, known_family=FAMILY)
    assert main == (NEW_BUNDLE, CDN + NEW_BUNDLE)
    assert {name for name, _ in deps} == {VCLIB, DOTNET}


def test_pick_uwp_packages_prefers_newer_msixbundle_over_older_appxbundle() -> None:
    """Новый .msixbundle выбирается взамен более старого .appxbundle."""
    older_appxbundle = (
        "Microsoft.DesktopAppInstaller_2021.1207.203.0_neutral_~_8wekyb3d8bbwe.appxbundle"
    )
    newer_msixbundle = (
        "Microsoft.DesktopAppInstaller_2026.917.151.0_neutral_~_8wekyb3d8bbwe.msixbundle"
    )
    links = [
        (older_appxbundle, CDN + older_appxbundle),
        (newer_msixbundle, CDN + newer_msixbundle),
    ]
    main, _deps = uwp.pick_uwp_packages(links)
    assert main is not None
    assert main[0] == newer_msixbundle


def test_pick_uwp_packages_infers_family() -> None:
    """Без known_family семейство выводится как не-зависимость с бандлом."""
    links = [
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB, CDN + VCLIB),
    ]
    main, deps = uwp.pick_uwp_packages(links)
    assert main == (NEW_BUNDLE, CDN + NEW_BUNDLE)
    assert deps == [(VCLIB, CDN + VCLIB)]


def test_package_arch() -> None:
    """package_arch находит токен архитектуры в имени пакета."""
    assert uwp.package_arch(NEW_BUNDLE) == "neutral"
    assert uwp.package_arch(VCLIB) == "x64"
    assert uwp.package_arch(VCLIB_X86) == "x86"
    assert uwp.package_arch(VCLIB_ARM64) == "arm64"
    assert uwp.package_arch("NoArchFile.appx") == "neutral"


def test_pick_uwp_packages_filters_deps_by_arch() -> None:
    """Строгий x64: в зависимости идут только neutral и x64 пакеты."""
    links = [
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB, CDN + VCLIB),
        (VCLIB_X86, CDN + VCLIB_X86),
        (VCLIB_ARM64, CDN + VCLIB_ARM64),
        (DOTNET, CDN + DOTNET),
    ]
    main, deps = uwp.pick_uwp_packages(links, known_family=FAMILY, arch="x64")
    assert main == (NEW_BUNDLE, CDN + NEW_BUNDLE)
    assert {name for name, _ in deps} == {VCLIB, DOTNET}


def test_pick_uwp_packages_prefers_target_arch_main() -> None:
    """Главный пакет: neutral, затем целевая arch (x64), затем прочее."""
    links = [
        (APP_X64, CDN + APP_X64),
        (APP_X86, CDN + APP_X86),
    ]
    main, deps = uwp.pick_uwp_packages(links, known_family=FAMILY, arch="x64")
    assert main == (APP_X64, CDN + APP_X64)
    assert deps == []


def test_pick_uwp_packages_drops_arm64_for_x64() -> None:
    """Для x64-устройства arm64 — не целевая арка: в deps не попадает."""
    links = [
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB_ARM64, CDN + VCLIB_ARM64),
    ]
    main, deps = uwp.pick_uwp_packages(links, known_family=FAMILY, arch="x64")
    assert main == (NEW_BUNDLE, CDN + NEW_BUNDLE)
    assert deps == []


def test_download_uwp_app_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Скачивает в общий пул Dependencies и пишет .deps.json; старый удаляется."""
    links = [
        (OLD_BUNDLE, CDN + OLD_BUNDLE),
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB, CDN + VCLIB),
        (DOTNET, CDN + DOTNET),
    ]
    fake_session = _fake_session(_adguard_html([(OLD_BUNDLE, CDN + OLD_BUNDLE)]))
    monkeypatch.setattr(uwp, "fetch_store_files", lambda *a, **k: links)

    def _fake_download(url, dest, session=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(uwp, "download_file", _fake_download)

    apps_root = tmp_path / "Template" / "FreshApps"
    result = uwp.download_uwp_app(
        "Nitro Sense", FAMILY, apps_root, session=fake_session
    )

    assert result.state == "done"
    assert result.version == "3.1.100.0"
    app_dir = apps_root / "Nitro Sense"
    assert (app_dir / NEW_BUNDLE).exists()
    assert not (app_dir / OLD_BUNDLE).exists()
    assert not (app_dir / "Dependencies").exists()
    assert (apps_root / "Dependencies" / VCLIB).exists()
    assert (apps_root / "Dependencies" / DOTNET).exists()
    assert json.loads((app_dir / ".deps.json").read_text("utf-8")) == [
        VCLIB,
        DOTNET,
    ]


def test_download_uwp_app_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если свежее уже лежит в папке — скачивание пропускается."""
    links = [
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB, CDN + VCLIB),
    ]
    fake_session = _fake_session(_adguard_html([(OLD_BUNDLE, CDN + OLD_BUNDLE)]))
    monkeypatch.setattr(uwp, "fetch_store_files", lambda *a, **k: links)
    downloads: list[str] = []
    monkeypatch.setattr(
        uwp,
        "download_file",
        lambda url, dest, session=None: downloads.append(dest.name) and dest,
    )

    app_dir = tmp_path / "Template" / "FreshApps" / "Nitro Sense"
    app_dir.mkdir(parents=True)
    (app_dir / NEWEST_BUNDLE).write_bytes(b"x")

    result = uwp.download_uwp_app(
        "Nitro Sense", FAMILY, app_dir.parent, session=fake_session
    )

    assert result.state == "up_to_date"
    assert downloads == []


def test_download_uwp_app_skips_when_system_version_current(
    tmp_path: Path, monkeypatch
) -> None:
    """Если на ПК уже стоит актуальная версия из Store, скачивание пропускается."""
    links = [
        (NEW_BUNDLE, CDN + NEW_BUNDLE),
        (VCLIB, CDN + VCLIB),
    ]
    monkeypatch.setattr(uwp, "fetch_store_files", lambda *a, **k: links)
    downloads: list[str] = []
    monkeypatch.setattr(
        uwp,
        "download_file",
        lambda url, dest, session=None: downloads.append(dest.name) and dest,
    )

    apps_root = tmp_path / "Apps"
    installed_versions: dict[str, str] = {
        FAMILY.lower(): uwp._package_version(NEW_BUNDLE),
    }
    result = uwp.download_uwp_app(
        "Nitro Sense",
        FAMILY,
        apps_root,
        installed_versions=installed_versions,
    )
    assert result.state == "up_to_date"
    assert "уже актуально в системе" in (result.message or "")
    assert downloads == []
    # Папка приложения не должна содержать скачанный бандл
    app_dir = apps_root / "Nitro Sense"
    assert not (app_dir / NEW_BUNDLE).exists()


def test_download_uwp_app_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет ссылок от rg-adguard — результат not_found."""
    monkeypatch.setattr(uwp, "fetch_store_files", lambda *a, **k: None)
    result = uwp.download_uwp_app(
        "Ghost App", "bogus.app_zzz", tmp_path, session=_fake_session("")
    )
    assert isinstance(result, UwpDownloadResult)
    assert result.state == "not_found"


def test_uwp_apps_from_template(tmp_path: Path) -> None:
    """Достаёт только UWP-компоненты шаблона и их package family names."""
    template = tmp_path / "Template"
    nitro = template / "Apps" / "Nitro Sense_Acer_3.01.3056_W11x64_A"
    nitro.mkdir(parents=True)
    (nitro / "Install_UWP.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (nitro / "AUMIDs.txt").write_text("AcerIncorporated.NitroSense_wwxyz!App\n")

    classic = template / "Bluetooth_MTK_1.3.15.141_W11x64_A"
    classic.mkdir()
    (classic / "mtkbtfilter.inf").write_text("OEM INF\n", encoding="utf-8")

    (template / ".components").write_text(
        "Apps/Nitro Sense_Acer_3.01.3056_W11x64_A\n"
        "Bluetooth_MTK_1.3.15.141_W11x64_A\n",
        encoding="utf-8",
    )

    apps = uwp.uwp_apps_from_template(template)
    assert apps == [("Nitro Sense", FAMILY)]


def test_resolve_product_url() -> None:
    """Достаёт product-ссылку из страницы поиска apps.microsoft.com."""
    html = '<a href="/detail/nitro-sense/9NBLGGH4GQ2W">Nitro Sense</a>'
    url = uwp.resolve_product_url("Nitro Sense", session=_fake_session(html))
    assert url == "https://apps.microsoft.com/detail/nitro-sense/9NBLGGH4GQ2W"


def test_resolve_product_url_storeedgefd_json() -> None:
    """StoreEdgeFD JSON-ответ разбирается в ссылку на продукт (как в Raven)."""
    payload = {
        "Payload": {
            "SearchResults": [
                {"ProductId": "9NBLGGH4GQ2W", "Title": "Nitro Sense"}
            ]
        }
    }
    session: Any = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(
            json=lambda: payload,
            text=json.dumps(payload),
            raise_for_status=lambda: None,
        )
    )
    url = uwp.resolve_product_url("Nitro Sense", session=session)
    assert url == "https://apps.microsoft.com/detail/9NBLGGH4GQ2W"


def test_seed_target_preserves_package_family_name_case_insensitive() -> None:
    """PackageFamilyName с заглавными буквами сразу уходит в rg-adguard."""
    family = "RealtekSemiconductorCorp.RealtekAudioControl_dt26b99r8h8gj"

    def _fail_get(*_args, **_kwargs):
        raise AssertionError("must not call store")

    session: Any = SimpleNamespace(get=_fail_get)
    target = uwp._seed_target(family, session=session)
    assert target == family.lower()


def test_seed_target_preserves_family_with_hyphen_and_no_dot() -> None:
    """Семейства с дефисом и без точек (напр. AMD) сразу уходят в rg-adguard."""
    family = "AdvancedMicroDevicesInc-RSXCM_v2es6h43hjn86"

    def _fail_get(*_args, **_kwargs):
        raise AssertionError("must not call store")

    session: Any = SimpleNamespace(get=_fail_get)
    target = uwp._seed_target(family, session=session)
    assert target == family.lower()


def test_fetch_store_files_posts_form() -> None:
    """POST в /api/GetFiles уходит с типом по типу цели."""
    calls: dict[str, Any] = {}

    def _post(url, data, headers=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        html = _adguard_html([(NEW_BUNDLE, CDN + NEW_BUNDLE)])
        return SimpleNamespace(
            text=html, status_code=200, raise_for_status=lambda: None
        )

    def _get(*_args, **_kwargs):
        return None

    session: Any = SimpleNamespace(get=_get, post=_post)
    links = uwp.fetch_store_files(FAMILY, ring="Retail", session=session)
    assert links == [(NEW_BUNDLE, CDN + NEW_BUNDLE)]
    assert calls["url"] == ADGUARD_URL
    assert calls["data"]["type"] == "PackageFamilyName"
    assert calls["data"]["ring"] == "Retail"


def test_fetch_store_files_sends_browser_headers() -> None:
    """POST в rg-adguard уходит с браузерными заголовками (обход Cloudflare)."""
    calls: dict[str, Any] = {}

    def _post(url, data, headers=None, timeout=None):
        calls["headers"] = headers or {}
        html = _adguard_html([(NEW_BUNDLE, CDN + NEW_BUNDLE)])
        return SimpleNamespace(
            text=html, status_code=200, raise_for_status=lambda: None
        )

    def _get(*_args, **_kwargs):
        return None

    session: Any = SimpleNamespace(get=_get, post=_post)
    links = uwp.fetch_store_files(FAMILY, session=session)
    assert links == [(NEW_BUNDLE, CDN + NEW_BUNDLE)]
    headers = calls["headers"]
    assert headers.get("Referer") == "https://store.rg-adguard.net/"
    assert headers.get("Origin") == "https://store.rg-adguard.net"
    assert headers.get("X-Requested-With") == "XMLHttpRequest"
    assert "application/x-www-form-urlencoded" in headers.get("Content-Type", "")
    assert headers.get("Accept-Language")


def test_fetch_store_files_retries_on_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient 403 от Cloudflare переживается повторным POST без сна в тесте."""

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(uwp.time, "sleep", _no_sleep)
    posts = 0

    def _raise_403() -> None:
        raise requests.HTTPError("403 Client Error: Forbidden")

    def _post(url, data, headers=None, timeout=None):
        nonlocal posts
        posts += 1
        if posts == 1:
            return SimpleNamespace(
                text="Just a moment...",
                status_code=403,
                raise_for_status=_raise_403,
            )
        html = _adguard_html([(NEW_BUNDLE, CDN + NEW_BUNDLE)])
        return SimpleNamespace(
            text=html, status_code=200, raise_for_status=lambda: None
        )

    def _get(*_args, **_kwargs):
        return None

    session: Any = SimpleNamespace(get=_get, post=_post)
    links = uwp.fetch_store_files(FAMILY, session=session)
    assert links == [(NEW_BUNDLE, CDN + NEW_BUNDLE)]
    assert posts == 2  # noqa: PLR2004 — первая попытка + один повтор


def test_download_uwp_app_error_on_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сетевая ошибка Store (403) даёт state='error', а не роняет сборку."""

    def _boom(*_args, **_kwargs):
        raise requests.HTTPError("403 Client Error: Forbidden")

    monkeypatch.setattr(uwp, "fetch_store_files", _boom)
    result = uwp.download_uwp_app(
        "Nitro Sense", FAMILY, tmp_path, session=_fake_session("")
    )
    assert isinstance(result, UwpDownloadResult)
    assert result.state == "error"
    assert "403" in (result.message or "")


def test_fetch_store_files_retries_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сетевой сбой (ConnectionError) переживается повторным POST."""

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(uwp.time, "sleep", _no_sleep)
    posts = 0

    def _post(url, data, headers=None, timeout=None):
        nonlocal posts
        posts += 1
        if posts == 1:
            raise requests.ConnectionError("connection reset by peer")
        html = _adguard_html([(NEW_BUNDLE, CDN + NEW_BUNDLE)])
        return SimpleNamespace(
            text=html, status_code=200, raise_for_status=lambda: None
        )

    def _get(*_args, **_kwargs):
        return None

    session: Any = SimpleNamespace(get=_get, post=_post)
    links = uwp.fetch_store_files(FAMILY, session=session)
    assert links == [(NEW_BUNDLE, CDN + NEW_BUNDLE)]
    assert posts == 2  # noqa: PLR2004 — первая попытка + один повтор


def test_download_uwp_updates_skips_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кэш неудачных запросов пропускает повторные; новые исходы записываются."""
    apps_root = tmp_path / "Apps"
    apps_root.mkdir()
    state_path = apps_root.parent / uwp.QUERY_STATE_NAME
    cached_family = "acerincorporated.nitrosense_wwxyz"
    uwp.save_query_state(
        state_path,
        {
            cached_family: {
                "state": "not_found",
                "ts": int(time.time()) - 3600,
                "msg": "Пакеты не найдены в Microsoft Store",
            }
        },
    )

    calls: list[tuple[str, str]] = []

    def _fake_download_uwp_app(
        *_args: object, **_kwargs: object
    ) -> UwpDownloadResult:
        calls.append((str(_args[0]), str(_args[1])))
        return UwpDownloadResult(
            app=str(_args[0]),
            family=None,
            version="0",
            state="error",
            message="403 Client Error",
        )

    monkeypatch.setattr(uwp, "download_uwp_app", _fake_download_uwp_app)

    realtek = "RealtekSemiconductorCorp.RealtekAudioControl_8wekyb3d8bbwe"
    results = uwp.download_uwp_updates(
        [("Nitro Sense", cached_family), ("Realtek Audio", realtek)],
        apps_root,
    )

    assert len(results) == 2  # noqa: PLR2004 — один пропуск по кэшу + запрос
    assert results[0].state == "not_found"
    assert "кэш до" in (results[0].message or "")
    assert results[1].state == "error"
    assert calls == [("Realtek Audio", realtek)]

    saved = uwp.load_query_state(state_path)
    assert saved[cached_family]["state"] == "not_found"
    assert realtek.lower() in saved
    assert saved[realtek.lower()]["state"] == "error"


def test_resolve_store_product() -> None:
    """Резолвит Store ID в (название, PackageFamilyName) из Storefront API."""

    def _get(url, params=None, timeout=None):
        return SimpleNamespace(
            json=lambda: {
                "Payload": {
                    "Title": "NVIDIA Control Panel",
                    "PackageFamilyNames": [
                        "NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj"
                    ],
                }
            },
            raise_for_status=lambda: None,
        )

    session: Any = SimpleNamespace(get=_get)
    result = uwp.resolve_store_product("9NF8H0H7WMLT", session=session)
    assert result == (
        "NVIDIA Control Panel",
        "NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj",
    )


def test_resolve_store_product_no_family() -> None:
    """Продукт без PackageFamilyNames -> None."""

    def _get(url, params=None, timeout=None):
        return SimpleNamespace(
            json=lambda: {"Payload": {"Title": "X", "PackageFamilyNames": []}},
            raise_for_status=lambda: None,
        )

    session: Any = SimpleNamespace(get=_get)
    assert uwp.resolve_store_product("9NF8H0H7WMLT", session=session) is None


def test_resolve_store_product_no_payload() -> None:
    """Ответ без Payload -> None."""

    def _get(url, params=None, timeout=None):
        return SimpleNamespace(
            json=lambda: {"Error": "not found"}, raise_for_status=lambda: None
        )

    session: Any = SimpleNamespace(get=_get)
    assert uwp.resolve_store_product("9NF8H0H7WMLT", session=session) is None


def test_product_id_from() -> None:
    """Достаёт Store ID из голого ID и ссылок apps.microsoft.com."""
    assert uwp.product_id_from("9NF8H0H7WMLT") == "9NF8H0H7WMLT"
    assert (
        uwp.product_id_from("https://apps.microsoft.com/detail/9NF8H0H7WMLT")
        == "9NF8H0H7WMLT"
    )
    assert (
        uwp.product_id_from("https://apps.microsoft.com/detail/xid/9NF8H0H7WMLT")
        == "9NF8H0H7WMLT"
    )
    assert uwp.product_id_from("не приложение") is None
