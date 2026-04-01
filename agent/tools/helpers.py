"""Shared helpers used by multiple tool modules."""

import unicodedata
from pathlib import Path

import httpx

from ..config import Config, load_config

_SELF_DIR = Path(__file__).parent.parent.parent


def _music_dir() -> Path:
    return load_config().library.music_path


def _roots(cfg: Config) -> list[Path]:
    return [_SELF_DIR.resolve(), cfg.library.music_path.expanduser().resolve()]


def _is_under_allowed_roots(cfg: Config, path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in _roots(cfg):
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _resolve_tool_path(cfg: Config, file_path: str) -> Path | None:
    raw = Path(file_path).expanduser()
    if not raw.is_absolute():
        path = (_SELF_DIR / raw).resolve()
    else:
        path = raw.resolve()
    if not _is_under_allowed_roots(cfg, path):
        return None
    return path


def _normalize_for_search(s: str) -> str:
    """Normalize unicode for fuzzy matching -- strip emoji, normalize dashes/special chars."""
    s = ''.join(c for c in s if unicodedata.category(c) not in ('So', 'Sk', 'Sm'))
    s = s.replace('\u2013', '-').replace('\u2014', '-').replace('\uff5c', '|').replace('\uff1a', ':')
    s = unicodedata.normalize('NFKC', s)
    return s.lower().strip()


def _mixxx_failed(d: dict) -> str | None:
    if d.get("_request_failed"):
        return d.get("_detail", "Mixxx request failed")
    return None


def _mixxx_get(path: str) -> dict:
    cfg = load_config()
    try:
        r = httpx.get(f"{cfg.mixxx.url}{path}", timeout=cfg.mixxx.timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_request_failed": True, "_detail": str(e)}


def _mixxx_post(path: str, data: dict | None = None) -> dict:
    cfg = load_config()
    try:
        r = httpx.post(f"{cfg.mixxx.url}{path}", json=data or {}, timeout=cfg.mixxx.timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_request_failed": True, "_detail": str(e)}


def _dj_get(path: str) -> dict:
    r = _mixxx_get(path)
    if err := _mixxx_failed(r):
        return {"error": err}
    return r


def _dj_post(path: str, data: dict | None = None) -> dict:
    r = _mixxx_post(path, data)
    if err := _mixxx_failed(r):
        return {"error": err}
    return r
