"""What to say aloud and how. Stdlib only (hooks import it)."""
from __future__ import annotations

import json, os, re, subprocess

from . import config, state

VOICE_INSTRUCTION = (
    "Голосовой режим включён. Пользователь не смотрит на экран и говорит с тобой голосом; "
    "реплики, пришедшие от Stop-хука как „Голосовая команда пользователя: …“, — это её слова, "
    "отвечай на них как на обычные сообщения. Полный ответ пиши как обычно, но в конце КАЖДОГО "
    "ответа добавляй блок <speak>…</speak>: 1–3 коротких предложения по-русски, которые будут "
    "зачитаны вслух, без кода, путей, ссылок и markdown. Если действие отклонено, потому что "
    "пользователь не подтвердил голосом, — не повторяй его, оставь результат черновиком и скажи "
    "об этом в <speak>."
)

_SPEAK = re.compile(r"<speak>(.*?)</speak>", re.S)


def _last_assistant_text(path):
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") != "assistant":
                        continue
                    blocks = msg["message"]["content"]
                    text = "\n".join(b["text"] for b in blocks
                                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if text:
                    last = text
    except OSError:
        return ""
    return last


def prepare_for_tts(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"</?speak>", " ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?m)^\s*(#+|[-*+>]|\d+\.)\s+", "", text)
    text = re.sub(r"(?<!\w)([*_~]{1,3})(\S(?:.*?\S)?)\1(?!\w)", r"\2", text)  # snake_case stays
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", head) if m.end() <= limit]
    if ends:
        return head[:ends[-1]].strip()
    cut = head.rfind(" ")
    return head[:cut].strip() if cut > 0 else text[:limit]


def extract_speak(transcript_path):
    return speak_part(_last_assistant_text(transcript_path))


def speak_part(text):
    if not text:
        return ""
    tags = [t.strip() for t in _SPEAK.findall(text) if t.strip()]
    if tags:
        return prepare_for_tts(tags[-1])
    return _truncate(prepare_for_tts(text), config.FALLBACK_CHARS)


def _leaves(v, key=""):
    """Every str/bool/number leaf as (key, spoken value); lists keep their key, nested dicts give the inner key."""
    if isinstance(v, bool):
        yield key, "да" if v else "нет"
    elif isinstance(v, (str, int, float)):
        yield key, str(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _leaves(x, str(k))
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _leaves(x, key)


def confirm_phrase(tool_name, tool_input):
    # never cut: a yes approves exactly what was heard (the hook denies phrases over CONFIRM_MAX_CHARS)
    tool_input = tool_input or {}
    if tool_name == "Bash":  # flags (sandbox off, background) after the command; the description is the model's
        flags = [f"{k}: {v}" for k, v in _leaves({k: x for k, x in tool_input.items() if not isinstance(x, str)})]
        return f"Хочу выполнить команду: {'; '.join([str(tool_input.get('command', ''))] + flags)}. Можно?"
    name = tool_name.rsplit("__", 1)[-1]
    vals = "; ".join(f"{k}: {v}" if k else v for k, v in _leaves(tool_input))
    return f"Хочу вызвать {name}{': ' + vals if vals else ''}. Можно?"


def speak(text, timeout=120):
    """True only if `say` exited 0."""
    if not text or not text.strip():
        return False
    try:
        cmd = os.environ.get("VOICE_MODE_SAY") or config.SAY_CMD  # env read at call time
        text = text.replace("[[", " ").replace("]]", " ")  # `say` obeys [[volm 0]] etc. (the only inline syntax)
        state.set_speaking(True)
        # DEVNULL: the child must not write into the hook's stdout (JSON); "--": text may start with "-"
        return subprocess.run([cmd, "-v", config.VOICE, "--", text], timeout=min(timeout, 120),  # < STALE_SPEAKING
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:  # a hook calling speak must never crash
        return False
    finally:
        try:
            state.set_speaking(False)
        except Exception:
            pass
