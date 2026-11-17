"""Hook entry point: python3 run.py <event> < hook-input-json. Stdlib only."""
from __future__ import annotations

import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from voicemode.hooks import main  # noqa: E402
except Exception:  # a broken install must not turn PreToolUse into "run the tool"
    home = os.path.expanduser(os.environ.get("VOICE_MODE_HOME", "~/.claude-voice-mode"))
    if sys.argv[1:2] == ["pre_tool_use"] and os.path.exists(os.path.join(home, "active")):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": "Сбой голосового подтверждения"}},
                         ensure_ascii=True))
    sys.exit(0)

out = main(sys.argv[1] if len(sys.argv) > 1 else "", sys.stdin.read())
if out:
    print(out)
