from __future__ import annotations

import os
WAKE_WORDS = ("claude", "клод", "клауд", "клоуд", "клаут")
STOP_WORDS = ("стоп", "стой", "хватит", "отмена")
YES_WORDS = ("да", "отправляй", "отправь", "пушь", "подтверждаю")
WAKE_FOLLOWUP_SEC = 8
# lower-case substrings of typical Whisper hallucinations on silence
HALLUCINATIONS = (
    "продолжение следует",
    "субтитры",
    "спасибо за просмотр",
    "редактор субтитров",
    "подписывайтесь",
    "в следующей серии",
)

# --- action policy (policy.py) ---
SAFE_TOOLS = {"Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite",
              "Agent", "Task", "Skill", "ToolSearch", "WebFetch", "WebSearch"}
BASH_ALLOW = (
    *[(c,) for c in ("ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "echo",
                     "which", "diff")],
    *[("git", s) for s in ("status", "diff", "log", "show", "add", "commit", "switch", "branch")],
    *[("go", s) for s in ("test", "build", "vet")],
    *[("make", s) for s in ("test", "lint", "build")],
    ("npm", "test"),
)
# defence-in-depth: none of these prefixes is in BASH_ALLOW
BASH_DESTRUCTIVE = (
    ("git", "reset", "--hard"), ("git", "clean"), ("git", "checkout", "--"), ("git", "restore"),
)
# flag checks that depend on position-independent tokens: (command prefix, flags that make it destructive)
BASH_DESTRUCTIVE_FLAGS = (
    (("find",), {"-delete", "-exec", "-execdir", "-ok", "-okdir"}),
    (("git", "branch"), {"-D", "-d", "--delete", "-m", "-M", "--move", "-f", "--force", "-C", "-c", "--copy"}),
    (("git", "switch"), {"-f", "--force", "--discard-changes", "-C", "--force-create"}),
    (("rg",), {"--pre", "--hostname-bin"}),
    *[(("git", c), {"--output", "--ext-diff", "--textconv"}) for c in ("log", "diff", "show")],
)
# go test/build/vet: only these flags (- or --, optional =value); anything else -> confirm
GO_FLAGS = {"v", "run", "count", "race", "short", "timeout", "cover", "bench", "benchmem", "tags", "p",
            "json", "failfast"}
GO_VALUE_FLAGS = {"run", "count", "timeout", "bench", "tags", "p"}  # value may be the next token
MCP_READ_WORDS = {"read", "get", "list", "search", "query", "fetch", "find", "show", "describe", "status"}
MCP_WRITE_WORDS = {"send", "create", "update", "delete", "post", "transition", "merge", "add", "edit",
                   "set", "publish", "schedule", "upload", "remove", "resolve", "execute", "run",
                   "write", "apply", "patch", "push", "draft",
                   "certify", "uncertify", "share", "unshare", "archive", "insert", "approve", "assign",
                   "invite", "cancel", "close", "start", "stop", "trigger", "submit", "reply", "comment",
                   "mark", "pin", "respond", "manage", "sync", "ack", "enable", "disable", "grant", "revoke",
                   "destroy", "purge", "replace", "drop", "kill", "wipe", "erase", "move", "rename",
                   "revert", "reset"}

# --- speech (speech.py) ---
VOICE = "Milena"
SAY_CMD = os.environ.get("VOICE_MODE_SAY", "say")  # speak() re-reads the env at call time
FALLBACK_CHARS = 300
CONFIRM_MAX_CHARS = 400  # a longer confirmation phrase is denied unasked: nobody follows it by ear
