from __future__ import annotations

try:  # pragma: no cover - exercised on Windows hosts when pywin32 is available
    import win32cred  # type: ignore
except Exception:  # pragma: no cover
    win32cred = None


class WindowsCredentialStore:
    def __init__(self):
        self.available = win32cred is not None

    def has_credential(self, target: str | None) -> bool:
        if not self.available or not target:
            return False
        try:
            win32cred.CredRead(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target)
            return True
        except Exception:
            return False

    def read_secret(self, target: str | None) -> str:
        if not self.available:
            raise ValueError("Windows Credential Manager integration is not available on this host.")
        if not target:
            raise ValueError("Credential target is required.")
        try:
            record = win32cred.CredRead(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target)
        except Exception as exc:
            raise ValueError(f"Credential target {target} could not be read.") from exc
        secret = record.get("CredentialBlob") or b""
        if isinstance(secret, bytes):
            return secret.decode("utf-16-le", errors="ignore").strip("\x00") or secret.decode("utf-8", errors="ignore")
        return str(secret)

    def write_secret(self, target: str | None, secret: str, username: str | None = None) -> None:
        if not self.available:
            raise ValueError("Windows Credential Manager integration is not available on this host.")
        if not target:
            raise ValueError("Credential target is required.")
        if not secret:
            raise ValueError("Credential secret cannot be empty.")
        blob = secret.encode("utf-16-le")
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": target,
                "CredentialBlob": blob,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": username or target,
            },
            0,
        )

    def delete_secret(self, target: str | None) -> None:
        if not self.available:
            raise ValueError("Windows Credential Manager integration is not available on this host.")
        if not target:
            raise ValueError("Credential target is required.")
        try:
            win32cred.CredDelete(TargetName=target, Type=win32cred.CRED_TYPE_GENERIC, Flags=0)
        except Exception as exc:
            raise ValueError(f"Credential target {target} could not be deleted.") from exc

    def describe(self, target: str | None) -> dict[str, object]:
        return {
            "available": self.available,
            "target": target,
            "configured": self.has_credential(target),
        }
