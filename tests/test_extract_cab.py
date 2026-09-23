"""Офлайн-тесты extract_cab: валидация файла и понятные ошибки.

Вместо кода -1 от expand.exe повреждённый .cab распознаётся заранее.
"""

import struct
import subprocess
from pathlib import Path

from windriveinstall import downloader


def _long_cab_path(tmp_path: Path) -> Path:
    """Путь длиннее порога MAX_PATH, при котором expand.exe не справляется."""
    long_dir = tmp_path / ("d" * 60) / ("e" * 60) / ("f" * 60) / ("g" * 60)
    long_dir.mkdir(parents=True)
    cab = long_dir / "driver.cab"
    cab.write_bytes(b"MSCF" + b"\x00" * 8)
    assert downloader._path_too_long(cab)
    return cab


def test_extract_cab_missing_file(tmp_path: Path) -> None:
    """Отсутствующий файл ловится сразу и внятно."""
    err = None
    try:
        downloader.extract_cab(tmp_path / "nope.cab", tmp_path / "out")
    except FileNotFoundError as exc:
        err = exc
    assert err is not None
    assert "не найден" in str(err)


def test_extract_cab_empty_file(tmp_path: Path) -> None:
    """Пустой .cab — оборванная загрузка, expand.exe даже не зовём."""
    cab = tmp_path / "x.cab"
    cab.write_bytes(b"")
    err = None
    try:
        downloader.extract_cab(cab, tmp_path / "out")
    except ValueError as exc:
        err = exc
    assert err is not None
    assert "пустой" in str(err)


def test_extract_cab_html_page(tmp_path: Path) -> None:
    """Скачанная HTML-ошибка (страница вместо .cab) распознаётся до распаковки."""
    cab = tmp_path / "x.cab"
    cab.write_text("<!DOCTYPE html><html><body>403 Forbidden</body></html>")
    err = None
    try:
        downloader.extract_cab(cab, tmp_path / "out")
    except ValueError as exc:
        err = exc
    assert err is not None
    assert "HTML" in str(err)


def test_extract_cab_expand_failure(tmp_path: Path, monkeypatch) -> None:
    """При падении expand.exe получаем RuntimeError с кодом, размером и stderr."""
    cab_path = tmp_path / "bad.cab"
    cab_path.write_bytes(b"MSCF" + b"\x00" * 64)

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=-1, stderr="bad cab!")

    monkeypatch.setattr(downloader.subprocess, "run", _fake_run)
    err = None
    try:
        downloader.extract_cab(cab_path, tmp_path / "out")
    except RuntimeError as exc:
        err = exc
    assert err is not None
    text = str(err)
    assert "кодом -1" in text
    assert "68 байт" in text  # len(b"MSCF" + 64 нулей)
    assert "bad cab!" in text


def test_extract_cab_success(tmp_path: Path, monkeypatch) -> None:
    """Успешная распаковка создаёт папку и не чинит файл."""
    cab_path = tmp_path / "good.cab"
    cab_path.write_bytes(b"MSCF" + b"\x00" * 8)
    dest = tmp_path / "out"

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "expand.exe"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "driver.inf").write_text("[Version]")
        return subprocess.CompletedProcess(cmd, returncode=0, stderr="")

    monkeypatch.setattr(downloader.subprocess, "run", _fake_run)
    extracted = downloader.extract_cab(cab_path, dest)
    assert extracted == dest
    assert (dest / "driver.inf").is_file()


def test_extract_cab_long_path_extracts_via_short_tmp(
    tmp_path: Path, monkeypatch
) -> None:
    """Длинный путь не ломает распаковку: каб копируется в короткий temp."""
    cab_path = _long_cab_path(tmp_path)
    dest = tmp_path / "out"
    seen_tmp_dir: list[Path] = []

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "expand.exe"
        out_dir = Path(cmd[3])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "driver.inf").write_text("[Version]")
        seen_tmp_dir.append(out_dir.parent)
        return subprocess.CompletedProcess(cmd, returncode=0, stderr="")

    monkeypatch.setattr(downloader.subprocess, "run", _fake_run)
    extracted = downloader.extract_cab(cab_path, dest)

    assert extracted == dest
    assert seen_tmp_dir, "expand.exe должен был быть вызван"
    work_dir = seen_tmp_dir[0]
    assert work_dir.name.startswith("wdi_cab_")
    assert not work_dir.exists(), "временный каталог должен быть почищен"
    assert (dest / "driver.inf").is_file()


def test_extract_cab_truncated_detected(tmp_path: Path) -> None:
    """Заголовок обещает больше байт, чем есть в файле, — загрузка оборвана."""
    cab = tmp_path / "short.cab"
    header = b"MSCF" + b"\x00" * 4 + struct.pack("<I", 10_000)
    cab.write_bytes(header + b"x" * 32)
    err = None
    try:
        downloader.extract_cab(cab, tmp_path / "out")
    except ValueError as exc:
        err = exc
    assert err is not None
    assert "оборвана" in str(err)


def test_extract_cab_signed_tail_is_ok(tmp_path: Path, monkeypatch) -> None:
    """PKCS#7-подпись после cbCabinet (штатные кабы каталога MS) не мешает."""
    cab_path = tmp_path / "signed.cab"
    # Каб длиной 60 байт (заголовок + payload), cbCabinet = 60.
    cab_body = b"MSCF" + b"\x00" * 4 + struct.pack("<I", 60) + b"\x00" * 24 + b"z" * 24
    cab_path.write_bytes(cab_body + b"\x30\x82\x00\x00signature-tail")
    dest = tmp_path / "out"

    def _fake_run(cmd, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "driver.inf").write_text("[Version]")
        return subprocess.CompletedProcess(cmd, returncode=0, stderr="")

    monkeypatch.setattr(downloader.subprocess, "run", _fake_run)
    extracted = downloader.extract_cab(cab_path, dest)
    assert extracted == dest
    assert (dest / "driver.inf").is_file()
