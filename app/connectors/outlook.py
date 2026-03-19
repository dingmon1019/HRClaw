from __future__ import annotations

from typing import Any

from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError

try:
    import win32com.client  # type: ignore
except Exception:  # pragma: no cover
    win32com = None


class OutlookConnector(BaseConnector):
    name = "outlook"
    description = "Optional Outlook connector powered by pywin32."

    def healthcheck(self) -> dict[str, Any]:
        available = win32com is not None
        return {
            "name": self.name,
            "available": available,
            "description": self.description,
            "message": "Ready" if available else "pywin32 is not installed; Outlook integration is disabled.",
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type != "outlook.send_mail":
            raise ConnectorError(f"Unsupported Outlook action: {action_type}")
        if win32com is None:
            raise ConnectorError("pywin32 is not installed; Outlook integration is unavailable.")
        to = (payload.get("to") or "").strip()
        subject = payload.get("subject") or ""
        body = payload.get("body") or ""
        if not to:
            raise ConnectorError("Outlook send mail requires a recipient address.")
        outlook = win32com.client.Dispatch("Outlook.Application")
        message = outlook.CreateItem(0)
        message.To = to
        message.Subject = subject
        message.Body = body
        message.Send()
        return {"to": to, "subject": subject, "sent": True}

