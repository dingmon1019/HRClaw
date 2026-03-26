from __future__ import annotations

import base64
from pathlib import Path

from app.config.settings import AppSettings
from app.core.errors import (
    FailClosedStorageRefusalError,
    InsecureSecretStorageRefusalError,
    ProtectedBlobIntegrityError,
)
from app.core.utils import ensure_parent_dir, new_id, random_token, sha256_hex

try:  # pragma: no cover - exercised on Windows hosts
    import win32crypt  # type: ignore
except Exception:  # pragma: no cover
    win32crypt = None


class ProtectedStorageService:
    STRONG_STORAGE_MODE = "dpapi"
    FALLBACK_STORAGE_MODE = "unprotected-local"
    PROTECTED_POSTURE = "protected"
    UNPROTECTED_POSTURE = "unprotected-local"
    REFUSED_POSTURE = "refused"
    FAIL_CLOSED_CLASSES = {"sensitive-local", "privileged-sensitive"}
    SECRET_TEXT_CLASS = "privileged-sensitive"

    def __init__(self, base_settings: AppSettings):
        self.base_settings = base_settings

    def write_secret_text(self, path: Path, value: str, *, purpose: str = "secret-text") -> None:
        self._ensure_secret_storage_allowed(purpose)
        ensure_parent_dir(path)
        path.write_bytes(self._protect(value.encode("utf-8")))

    def read_secret_text(self, path: Path, *, purpose: str = "secret-text") -> str:
        payload = path.read_bytes()
        storage_mode = self._payload_storage_mode(payload)
        self._ensure_secret_payload_allowed(storage_mode, purpose)
        return self._unprotect(payload).decode("utf-8")

    def ensure_secret_text(self, path: Path, *, length: int = 32, purpose: str = "secret-text") -> str:
        if path.exists():
            return self.read_secret_text(path, purpose=purpose).strip()
        secret = random_token(length)
        self.write_secret_text(path, secret, purpose=purpose)
        return secret

    def store_text_blob(self, text: str, *, classification: str, purpose: str) -> dict[str, str]:
        self._ensure_storage_allowed(classification, purpose)
        blob_id = new_id("blob")
        blob_path = self.base_settings.resolved_protected_blob_dir / f"{blob_id}.bin"
        ensure_parent_dir(blob_path)
        blob_path.write_bytes(self._protect(text.encode("utf-8")))
        return {
            "blob_id": blob_id,
            "classification": classification,
            "purpose": purpose,
            "digest": sha256_hex(text),
            "preview": text[:512],
            "storage_mode": self.storage_mode,
        }

    def load_text_blob(self, blob_id: str, *, expected_digest: str | None = None) -> str:
        blob_path = self.base_settings.resolved_protected_blob_dir / f"{blob_id}.bin"
        text = self._unprotect(blob_path.read_bytes()).decode("utf-8")
        if expected_digest and sha256_hex(text) != expected_digest:
            raise ProtectedBlobIntegrityError(f"Protected blob digest mismatch for {blob_id}.")
        return text

    @property
    def storage_mode(self) -> str:
        if self.base_settings.local_protection_mode.lower() == self.STRONG_STORAGE_MODE and win32crypt is not None:
            return self.STRONG_STORAGE_MODE
        return self.FALLBACK_STORAGE_MODE

    @property
    def is_strongly_protected(self) -> bool:
        return self.storage_mode == self.STRONG_STORAGE_MODE

    @property
    def posture_label(self) -> str:
        if self.is_strongly_protected:
            return self.PROTECTED_POSTURE
        if self.base_settings.allow_insecure_local_storage:
            return self.UNPROTECTED_POSTURE
        return self.REFUSED_POSTURE

    @property
    def can_persist_secrets(self) -> bool:
        return self.is_strongly_protected or self.base_settings.allow_insecure_local_storage

    def feature_posture(self) -> dict[str, object]:
        strong_only = self.is_strongly_protected
        posture = self.posture_label
        return {
            "posture": posture,
            "storage_mode": self.storage_mode,
            "strong_protection": strong_only,
            "allows_insecure_override": self.base_settings.allow_insecure_local_storage,
            "secret_persistence_available": self.can_persist_secrets,
            "disabled_features": [] if self.can_persist_secrets else [
                "Generated session secret file",
                "Protected CLI token-file mode",
                "Local secret text persistence",
            ],
            "sensitive_blob_storage": (
                "dpapi-protected"
                if strong_only
                else "insecure-dev-override"
                if self.base_settings.allow_insecure_local_storage
                else "refused"
            ),
        }

    def _ensure_storage_allowed(self, classification: str, purpose: str) -> None:
        if classification not in self.FAIL_CLOSED_CLASSES:
            return
        if self.is_strongly_protected:
            return
        if self.base_settings.allow_insecure_local_storage:
            return
        raise FailClosedStorageRefusalError(
            "Strong local protection is required to store "
            f"{classification} data for {purpose}. Enable DPAPI or explicitly opt into insecure local storage."
        )

    def _ensure_secret_storage_allowed(self, purpose: str) -> None:
        if self.can_persist_secrets:
            return
        raise FailClosedStorageRefusalError(
            "Strong local protection is required to store "
            f"{purpose}. Provide a runtime secret through the environment, enable DPAPI, "
            "or explicitly opt into insecure local storage for development-only use."
        )

    def _ensure_secret_payload_allowed(self, storage_mode: str, purpose: str) -> None:
        if storage_mode == self.STRONG_STORAGE_MODE:
            return
        if self.base_settings.allow_insecure_local_storage:
            return
        raise InsecureSecretStorageRefusalError(
            f"{purpose} is stored with unprotected local fallback. Enable DPAPI or explicitly allow insecure local storage before using it."
        )

    def _protect(self, raw: bytes) -> bytes:
        if self.storage_mode == self.STRONG_STORAGE_MODE:  # pragma: no branch - Windows path
            result = win32crypt.CryptProtectData(raw, None, None, None, None, 0)
            return self._coerce_crypt_result(result, "protect")
        return b"plain:" + base64.b64encode(raw)

    def _unprotect(self, payload: bytes) -> bytes:
        if payload.startswith(b"plain:"):
            return base64.b64decode(payload.split(b":", 1)[1])
        if self.storage_mode == self.STRONG_STORAGE_MODE:  # pragma: no branch - Windows path
            result = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
            return self._coerce_crypt_result(result, "unprotect")
        return payload

    @classmethod
    def _payload_storage_mode(cls, payload: bytes) -> str:
        if payload.startswith(b"plain:"):
            return cls.FALLBACK_STORAGE_MODE
        return cls.STRONG_STORAGE_MODE

    @staticmethod
    def _coerce_crypt_result(result, operation: str) -> bytes:
        if isinstance(result, bytes):
            return result
        if isinstance(result, (bytearray, memoryview)):
            return bytes(result)
        if isinstance(result, tuple):
            for candidate in reversed(result):
                if isinstance(candidate, bytes):
                    return candidate
                if isinstance(candidate, (bytearray, memoryview)):
                    return bytes(candidate)
        raise ValueError(f"Unsupported DPAPI {operation} result type: {type(result)!r}")
