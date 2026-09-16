import datetime as dt
import re

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def get_http_session() -> requests.Session:
    """Возвращает преднастроенную сессию requests с User-Agent."""
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return session


def parse_catalog_date(date_str: str) -> dt.date:
    if not date_str or date_str.lower() in ("n/a"):
        return dt.date.min
    return dt.date.strptime(date_str.strip(), "%m/%d/%Y")


def sanitize_folder_name(name: str) -> str:
    """Убирает недопустимые для Windows символы из имени папки."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    return cleaned[:150] or "unknown"


def version_tuple(v: str) -> tuple[int, ...]:
    if not v:
        return (0,)
    numbers = [
        int(m.group()) for p in re.split(r"[.\-,]", v) if (m := re.match(r"\d+", p))
    ]
    return tuple(numbers) if numbers else (0,)


def is_newer(
    remote_version: str,
    local_version: str,
    remote_date: dt.date = dt.date.min,
    local_date: dt.date = dt.date.min,
) -> bool:
    if remote_version.strip().lower() not in ("n/a"):
        try:
            return version_tuple(remote_version) > version_tuple(local_version)
        except ValueError, TypeError:
            pass

    return remote_date > local_date


def truncate(text: str, max_len: int) -> str:
    truncated = text if len(text) <= max_len else text[: max_len - 3] + "..."
    return f"{truncated:<{max_len}}"
