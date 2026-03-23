from __future__ import annotations

import json
import secrets
from hashlib import sha256
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def sha256_hex(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_directory_digest(path: Path) -> str:
    entries: list[str] = []
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            entries.append(f"symlink:{relative}")
            continue
        if child.is_dir():
            entries.append(f"dir:{relative}")
            continue
        size = child.stat().st_size
        entries.append(f"file:{relative}:{size}:{sha256_file(child)}")
    return sha256_hex("\n".join(entries))
