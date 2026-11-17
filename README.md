# claude-voice

Hands-free voice mode for [Claude Code](https://code.claude.com) on macOS. Say a wake word and a
command. Claude does the work and reads a short answer aloud. A push, a sent message and any
other action outside a fixed safe list run only after you say «Клод, да» ("Claude, yes"). This
is a guard against accidents, not a sandbox: see [Safety](#safety). It is for times when your
hands are busy and the screen is out of sight, such as cooking.

> **Russian by default.** The wake word, the stop and yes words, speech recognition
> (`language="ru"`) and every spoken phrase are in Russian. The examples below give English
> translations. Other languages need code changes (see [Limitations](#limitations)).

```text
You:    «Клод, запусти тесты»                  "Claude, run the tests"
Claude: (runs `go test ./...` without asking)
        «Упали два теста в store. Починить?»   "Two tests failed in store. Fix them?"
You:    «Клод, почини и запушь»                "Claude, fix them and push"
Claude: (edits files, commits, then asks before the push)
        «Хочу выполнить команду: git push. Можно?»   "I want to run the command: git push. OK?"
You:    «Клод, да»                             "Claude, yes"
```

This project is independent. It is not related to other projects named "claude-voice" and
contains no code from them.

## How it works

```text
 mic ──► listener (background process, one per machine)
         webrtcvad cuts phrases ─► mlx-whisper (local) ─► ~/.claude-voice-mode/<session>/queue.jsonl
                                                                    │
 Claude Code session                                                ▼
 ├─ UserPromptSubmit  /voice-on, /voice-off: start or stop the listener for this session
 ├─ Stop              read the <speak> part of the answer aloud, then wait for a command
 │                    that starts with the wake word; return it as "decision: block"
 │                    so Claude continues with it as the user's message
 ├─ PreToolUse        safe action → allow; anything else → ask aloud and wait for «Клод, да»
 ├─ Notification      a permission dialog appeared → «Нужно разрешение. Подойди к экрану.»
 │                    ("Permission needed. Come to the screen.")
 ├─ SessionStart      after compact/resume, re-inject the voice-mode instruction
 │                    and restart the listener if it is not running
 └─ SessionEnd        turn voice mode off: stop the listener, delete the queue
```

- **Audio never leaves the machine.** Voice activity detection (webrtcvad) and speech recognition
  (mlx-whisper, `whisper-large-v3-turbo`) run locally. Speech output uses the built-in macOS
  `say` with the Milena voice.
- **While voice mode is on, the listener transcribes all speech it hears**, not only commands, and
  appends it to `queue.jsonl`. That file and its cursor are deleted whenever voice mode turns off
  (`/voice-off`, session end, the 2-hour sleep, a move to another session). If Claude Code crashes
  without a session end, the file stays until one of those happens.
- **The listener is deaf while `say` speaks**, so Claude cannot wake itself.
- **Claude adds a `<speak>…</speak>` block** of 1–3 short sentences to each answer. Only that block
  is read aloud. If the block is missing, the hook reads up to 300 characters of the answer
  with code removed.
- **Waiting costs no tokens.** The Stop hook is an ordinary process that polls a file.

## Requirements

- macOS on Apple Silicon (mlx-whisper needs it).
- Claude Code with plugin support. Tested with the CLI, version 2.1.326.
- [Homebrew](https://brew.sh). `scripts/setup.sh` installs [uv](https://github.com/astral-sh/uv)
  with it if uv is missing.
- About 2.5 GB of disk: about 1.5 GB for the Whisper model and about 1 GB for the listener venv.
- Microphone access for the app that runs Claude Code. macOS asks the first time the listener starts.
- The hooks run with `python3` from `PATH` and use only the standard library. Stock macOS
  `/usr/bin/python3` (3.9) is enough. The listener has its own Python 3.12 venv.

## Install

1. Create the listener venv and download the model:

   ```bash
   git clone https://github.com/valerizabby/claude-voice.git
   cd claude-voice
   ./scripts/setup.sh
   ```

   The script is idempotent. It creates `~/.claude-voice-mode/venv` (Python 3.12), installs
   `listener/requirements.txt` and downloads `mlx-community/whisper-large-v3-turbo`.

2. Install the plugin in Claude Code:

   ```text
   /plugin marketplace add valerizabby/claude-voice
   /plugin install voice-mode@claude-voice-mode
   ```

   You can also add the local clone with `/plugin marketplace add /path/to/claude-voice`.

3. **Recommended:** raise the Stop-hook block cap. By default, Claude Code stops honouring a
   Stop-hook block after 8 consecutive blocks without a tool call. Voice mode would then stop
   listening after 8 voice commands in a row that only need a spoken answer. Add this to
   `~/.claude/settings.json` and restart Claude Code:

   ```json
   {
     "env": {
       "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "1000"
     }
   }
   ```

   If the variable is missing or below 100, `/voice-on` warns you aloud. Setting it through
   `settings.json` → `env` has not been verified yet. Setting it in the environment that starts
   Claude Code has been verified in the CLI.

## Usage

| Do this | Effect |
|---|---|
| `/voice-on` | Turns voice mode on for this session and starts the listener. Claude answers «Слушаю» ("Listening"). `/voice-mode:voice-on` works too. |
| `/voice-off` | Turns it off and stops the listener. |
| «Клод, <command>» ("Claude, …") | Runs the command. Wake words: `Claude`, `Клод`, `Клауд`, `Клоуд`, `Клаут`. The phrase must start with one of them. |
| «Клод.» then the command | A bare wake word waits up to 8 s for the next phrase. |
| «Клод, стоп» ("Claude, stop") | Also `стой`, `хватит`, `отмена`. Cancels commands said before it and refuses every tool call until the turn ends. Commands said after it still run. |
| «Клод, да» ("Claude, yes") | Confirms the action that was just read aloud. Also `отправляй`, `отправь`, `пушь`, `подтверждаю` after the wake word. A bare «Клод.» followed by «да» within 8 s works too. You have 30 s. A plain «да» without the wake word does not confirm: the action is refused and you hear «Чтобы подтвердить, скажи: Клод, да.» ("To confirm, say: Claude, yes."). |

- **Commands said while Claude works** are queued. They are delivered together at the next stop.
- **After about 2 hours without a command**, voice mode turns off and says «Засыпаю. Включи снова
  командой voice-on.» ("Going to sleep. Turn me on again with voice-on."). Run `/voice-on` again.
- **While it waits, the turn stays open** because the Stop hook is running. Interrupt it (Esc)
  to type. Voice mode stays on, and the next stop listens again. (Esc during the wait has not been
  verified end-to-end.)
- **Ending the session turns voice mode off.** On exit, `/clear` or any other session end, the
  SessionEnd hook stops the listener and deletes the queue. The listener also watches the Claude
  Code process that started it and exits within about a second after that process is gone, for
  example after a crash.
- **A dead listener is restarted.** Each stop, every 30 s while a stop waits, and each
  resume/compact check that the listener is running. If it is not, the hook starts it again. If
  it cannot be started (no venv, the start fails, or it dies again right after a restart), voice
  mode turns off and says «Не могу запустить микрофон, голосовой режим выключен.» ("Can't start
  the microphone, voice mode is off.").
- **Only one session listens per machine.** `/voice-on` in another session moves voice mode
  there and says «Голосовой режим перенесён в другую сессию.» ("Voice mode moved to another session.").
- **Microphone:** the listener uses the system default input device. It re-checks the device every
  5 s while nobody speaks, so switching between AirPods and the built-in microphone works.
- **Questions:** `AskUserQuestion` is read aloud with numbered options («первый — …», "first — …").
  Your spoken answer goes back to Claude; it needs no wake word. `ExitPlanMode` asks «План
  готов. Утверждаешь?» ("The plan is ready. Approve?") and needs «Клод, да». The plan itself is
  not read aloud.

## Safety

Voice mode decides in its PreToolUse hook. The rule is enforced by code, not by instructions
to the model. **It stops accidental external actions. It is not a sandbox.** Claude can run
project code without a question, so content Claude reads (a web page, an MCP result, a file)
can lead to network access or a push without any spoken question. See
[What can happen without a question](#what-can-happen-without-a-question).

**Runs without a question** (the hook returns `allow`, so Claude Code shows no permission dialog):

- Always: `Read`, `Grep`, `Glob`, `TodoWrite`, `Agent`, `Task`, `Skill`,
  `ToolSearch`, `WebFetch`, `WebSearch`.
- `Edit`, `Write`, `MultiEdit` and `NotebookEdit`, only inside the session's working directory
  after resolving symlinks. Exceptions: paths under `.git/` or `.claude/`, `.mcp.json`, paths
  with `~`, `$` or `..`, and a working directory that is `/`, your home or one of its parents.
- Bash, when every part of a `&&`, `||` or `|` chain is on the allowlist:
  `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `pwd`, `echo`, `which`, `diff`;
  `git status|diff|log|show|add|commit|switch|branch`; `go test|build|vet`;
  `make test|lint|build`; `npm test`.
  A command still needs «Клод, да» if it contains any of `` ` $ ( ) { } < > \ # ; ! * ? [ ] ``, a newline,
  a single `&` or an environment-variable prefix. Destructive flags also need it
  (`find -delete/-exec`, `git branch -D/-m/-c`, `git switch -f/-C`, `rg --pre`,
  `git log/diff/show --output/--ext-diff/--textconv`). For `go`, only a fixed set of flags is
  allowed, and packages must be `.` or `./…`. `make` and `npm` must have no extra arguments.
- MCP tools whose name contains a read word (`get`, `list`, `search`, …) and no write word
  (`send`, `create`, `delete`, `run`, …).

**Everything else needs a spoken «Клод, да»**: other Bash commands, `git push`, MCP writes,
sending messages, and unknown tools.

- **Every question speaks the full action.** For Bash, that is the whole command and every bool
  and number field (for example `dangerouslyDisableSandbox: да`). For other tools, that is every
  string, bool and number field. Nothing is shortened. If the phrase would exceed 400 characters,
  the hook refuses the action without asking. For Bash, it asks Claude to split the command. For
  anything else, it tells Claude to keep a draft.
- **Text containing `[[` is refused.** macOS `say` treats `[[…]]` as inline speech commands (for
  example, mute), so such text cannot be confirmed by voice.
- **Only an answer given after the question finished counts.** Words said earlier or during the
  question are ignored. Concurrent questions are asked one after another.
- **The yes must carry the wake word.** «Клод, да», or «Клод.» and then «да». A plain «да» is
  refused. The listener cannot tell voices apart: anyone in the room, or a TV, who says «Клод, да»
  within the 30 s window confirms the action.
- **«Клод, стоп» is sticky.** It refuses every tool call until the turn ends, including calls
  that are waiting for their question.
- **A hook error, an unclear answer or no answer means deny.** The hook's own budget is 75 s and
  its timeout in `hooks/hooks.json` is 90 s, so the timeout should never fire. If it did, Claude
  Code would fall back to its normal permission flow, not to a deny. If the plugin fails to
  import, PreToolUse denies while any session has voice mode on. If Python itself fails (for
  example it is missing from `PATH`), the PreToolUse command exits with code 2, which blocks the
  tool, while any session has voice mode on (the `active` file exists).
- **Your settings still apply.** A `deny` rule in Claude Code settings beats the hook's `allow`.
  This was verified for Bash rules in the headless CLI.
- **Voice mode off means no effect.** The hooks return nothing when the session is not listening.

### What can happen without a question

- **The network, directly.** `WebFetch` and `WebSearch` run without a question. They can send
  data out through the URL or the query, and what they return can steer Claude.
- **Any code, indirectly.** Claude can edit files inside the project and then run them through
  allowed commands: `go test`, `go build`, `make test|lint|build` and `npm test` execute
  project code, test files, the Makefile and `package.json` scripts. That code can reach the
  network or run `git push` itself.
- **Repository hooks.** `git commit` runs the repository's git hooks, including hooks set through
  `core.hooksPath` or husky that live in the working tree.
- **Persistent instructions.** Edits to project files such as `CLAUDE.md` need no question and
  apply to later sessions.

**Other known risks:**

- `Read` and `cat` are allowed for any path.
- MCP read/write detection is a heuristic based on the tool name.
- `go build` and `go test` may download modules or a Go toolchain.
- The desktop app has not been verified end-to-end. The terminal CLI has been verified headless.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `VOICE_MODE_HOME` | `~/.claude-voice-mode` | State directory: flags, queues, logs. The venv path stays at `~/.claude-voice-mode/venv`. |
| `VOICE_MODE_SAY` | `say` | Text-to-speech command. It is called as `<cmd> -v Milena -- <text>`. |
| `VOICE_MODEL` | `mlx-community/whisper-large-v3-turbo` | Model that `scripts/setup.sh` downloads. The hook-started listener always uses the default model. |
| `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | unset | Claude Code setting. See [Install](#install), step 3. |

Wake, stop and yes words, the Bash allowlist and the limits are in `voicemode/config.py`.

The hook starts the listener with default flags. To test the microphone and recognition on their
own, run the listener by hand:

```bash
~/.claude-voice-mode/venv/bin/python listener/listen.py --print --device-log
```

| Flag | Meaning |
|---|---|
| `--print` | Print `<unix_ts>\t<text>` for every phrase. Without it, no text is printed, so `listener.log` holds no transcript. |
| `--device-log` | Log the input device name. |
| `--file PATH` | Read a wav/aiff file instead of the microphone. |
| `--model REPO` | Whisper model (default `mlx-community/whisper-large-v3-turbo`). |
| `--prompt TEXT` | Whisper `initial_prompt` (default `Клод, Claude.`, wake words only). |
| `--vad 0..3` | webrtcvad aggressiveness (default 2). |
| `--session ID` | Write to that session's queue; exit when voice mode is off for it. The hook sets this. |
| `--parent-pid PID` | Exit when that process is gone. The hook passes the Claude Code process. |

## Limitations

- **Russian only, out of the box.** Recognition is fixed to Russian, and the phrases and words are
  in `voicemode/config.py`, `voicemode/hooks.py` and `voicemode/speech.py`.
- **macOS on Apple Silicon only.**
- **`/clear` starts a new session id** and ends the old session, which turns voice mode off. Run
  `/voice-on` again after it.
- **"Stop" takes effect at the next tool call or at the end of the turn.** It does not interrupt a
  command that is already running.
- **Desktop app:** not verified end-to-end. Microphone permission for a process started by a hook
  may behave differently there.

## Troubleshooting

- **Hook errors:** `~/.claude-voice-mode/errors.log`.
- **Listener output and errors:** `~/.claude-voice-mode/<session_id>/listener.log`.
- **Nothing is heard:** check System Settings → Privacy & Security → Microphone for the terminal
  or app that runs Claude Code. Then run the listener by hand (see [Configuration](#configuration)).
- **The listener does not start:** run `./scripts/setup.sh` again. The hooks expect
  `~/.claude-voice-mode/venv/bin/python`.
- **It stops listening after a few commands:** set `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` (Install, step 3).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use only the standard library and a stub `say`. No microphone or model is needed.

## License

[MIT](LICENSE)
