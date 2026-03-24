from __future__ import annotations

import json
import sys

from app.runtime.execution_boundary import run_bundle_in_child
from app.schemas.actions import ExecutionBundle


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"ok": False, "error": "Execution boundary received no bundle payload."}))
        return
    try:
        bundle = ExecutionBundle(**json.loads(raw))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Execution boundary could not parse bundle: {exc}"}))
        return
    print(json.dumps(run_bundle_in_child(bundle)))


if __name__ == "__main__":
    main()
