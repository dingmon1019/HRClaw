from __future__ import annotations


def test_audit_verification_detects_tampering(container):
    container.audit_service.emit("test.event", {"step": 1})
    container.audit_service.emit("test.event", {"step": 2})

    healthy = container.audit_service.verify_integrity()
    assert healthy["ok"] is True
    assert healthy["entry_count"] == 2

    container.database.execute(
        "UPDATE audit_entries SET payload_json = ? WHERE created_at = (SELECT MIN(created_at) FROM audit_entries)",
        ('{"step":999}',),
    )

    tampered = container.audit_service.verify_integrity()
    assert tampered["ok"] is False
    assert tampered["broken_at"] == 1
