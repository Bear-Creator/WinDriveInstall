# WinDriveInstall

Гибридный помощник обновления драйверов и сборки OEM-пакетов Acer.

- Ищет и скачивает обновления из каталога Microsoft Update.
- Глубоко вмерживает свежие драйверы в распакованные OEM-компоненты.
- Скачивает свежие UWP-пакеты приложений из Microsoft Store.
- Собирает итоговый пакет с `Install_Factory.bat` для офлайн-установки.

## Требования

- Python ≥ 3.14 (см. `.python-version`), менеджер зависимостей [`uv`](https://docs.astral.sh/uv/).
- Windows — для работы с драйверами/устройствами, rolling-сборки и `--snapshot`.
  На Linux/WSL доступны `build --template-dir …` и `uwp-update` — остальные
  подкоманды завершаются сообщением «только в ОС Windows».

## Быстрый старт

```bash
uv sync
uv run python -m windriveinstall --help
```

Консольные скрипты после установки: `windriveinstall`, `windrive`, `wdi`.

## Типовой цикл сборки OEM

Шаблон собирается **один раз**, дальше сборка повторяется сколько нужно.

```bash
# 1. Один раз: распаковать OEM-архивы в шаблон
#    (пишет манифест .components и меты .device.json)
uv run python scripts/prepare_template.py --oem-dir OEM \
    --template-dir Template/Nitro5

# 2. Свежие драйверы по HWID из шаблона в кэш (только Windows)
uv run python -m windriveinstall driver-update --template-dir Template/Nitro5

# 3. Свежие UWP-приложения (по компонентам шаблона)
uv run python -m windriveinstall uwp-update --template-dir Template/Nitro5

# 4. Итоговый пакет из шаблона (кроссплатформенно)
uv run python -m windriveinstall build --template-dir Template/Nitro5
```

Скрипт `prepare_template.py` отказывается работать с непустым `--template-dir`
(`FileExistsError`) — очистите папку перед повторным запуском.

## Раскладка каталогов

| Путь | Назначение |
| --- | --- |
| `OEM/` | Исходные OEM-архивы (`*.zip`), не изменяются |
| `Drivers/` | Каталог свежих драйверов (структура вендор/устройство/версия) |
| `Template/<Device>/` | Распакованный шаблон устройства + `.components` + `.device.json` |
| `Template/<Device>/Apps/` | OEM-компоненты приложений |
| `Cache/<Device>/Drivers/` | Свежие драйверы, скачанные `driver-update` |
| `Cache/<Device>/Apps/` | Свежие UWP-приложения, скачанные `uwp-update` |
| `Output/<Device>/` | Итоговый пакет с `Install_Factory.bat` |
| `Output/current/` | Результат rolling-сборки без шаблона |

Свежие приложения раскладываются как
`Cache/<Device>/Apps/<Приложение>/{<бандл>, Dependencies/}` и подбираются к
OEM-компонентам по package family name (из `AUMIDs.txt`) или по имени.

## Команды

Только для Windows:

- `run` — основной интерактивный пайплайн обновления;
- `rolling [--out-dir Output/current] [--apps "Имя или ссылка"]`
  — сборка под текущую систему без шаблона (WMI-устройства → каталог);
- `driver-update --template-dir Template/<Device>` — скачать свежие драйверы по
  HWID из шаблона в `Cache/<Device>/Drivers`;
- `check` — показать доступные обновления без скачивания;
- `list-devices` — локальные устройства и их HWID/версии (`--csv`, `--show-all-hwids`);
- `search --hwid ...` — поиск в каталоге Microsoft Update;
- `links --update-id ...` — [диагностика] прямые ссылки через POST;
- `download-file --url ... --out ...` — скачать файл по прямой ссылке;
- `extract --cab ... --out ...` — распаковать `.cab`;
- `auto-download --hwid ... --update-id ... --out-dir ...` — закачка через Selenium;
- `wait-download --dir ...` — ждать новый файл в папке загрузок;
- `semi-auto --query ... --downloads ... --out ...` — ручной фолбэк;
- `install --inf ... --confirm`, `install-bat --bat ... --confirm` — установка.

Доступны и на Linux/WSL:

- `build --template-dir Template/<Device> [--out-dir …] [--drivers-dir …] [--apps-dir …]`
  — сборка из шаблона (все каталоги по умолчанию из раскладки выше);
- `uwp-update [--template-dir Template/<Device>] [--apps "Имя или ссылка"] [--apps-root DIR] [--ring Retail|FVU] [--force]`
  — скачивание свежих UWP-пакетов в `Cache/<Device>/Apps`.

Параметры сборки:

- `build --device Nitro5` — то же, что `--template-dir Template/Nitro5`;
- `build` без шаблона — rolling-сборка под текущую систему (Windows),
  результат в `Output/current/`, кэш `Cache/current/`;
- `--snapshot` (Windows) — дописать WMI-HWID системы в `.device.json`
  (компоненты `source: system`, мержатся только свежие драйверы);
- `--force` в `uwp-update`/`driver-update` — перекачать свежие пакеты, даже если
  в кэше уже актуальная версия.

## Разработка

```bash
uv run pytest                 # все тесты (офлайн, сеть замокана)
uv run pytest tests/test_uwp.py::test_name   # один тест
uv run ruff check             # линтер (ruff, длина строки 88, есть правила docstring)
uv run ty check               # проверка типов
```

Докстринги и пользовательские сообщения — на русском. Windows-only зависимости
(`win32com`, `selenium`) импортируются лениво, чтобы пакет и тесты собирались на
Linux.