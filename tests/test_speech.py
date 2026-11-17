import os, stat, tempfile, unittest
from pathlib import Path

os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()
from voicemode import speech, state  # noqa: E402
from voicemode.speech import *  # noqa: E402,F403

FIX = str(Path(__file__).parent / "fixtures" / "transcript.jsonl")
LONG = str(Path(__file__).parent / "fixtures" / "transcript_long.jsonl")


class SpeechTest(unittest.TestCase):
    def setUp(self):
        os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()

    def test_extract(self):
        # last line is tool_use-only and one is malformed: both skipped
        self.assertEqual(extract_speak(FIX), "Тесты зелёные.")
        self.assertEqual(extract_speak("/nonexistent"), "")

    def test_fallback_truncates_at_sentence(self):
        out = extract_speak(LONG)
        self.assertTrue(0 < len(out) <= 300)
        self.assertTrue(out.endswith("."))

    def test_truncate_word_boundary(self):
        out = speech._truncate("слово " * 100, 300)
        self.assertLessEqual(len(out), 300)
        self.assertTrue(out.endswith("слово"))

    def test_prepare(self):
        self.assertEqual(prepare_for_tts("Смотри `foo.go` и ```\ncode\n``` [док](http://x)"), "Смотри foo.go и док")
        self.assertEqual(prepare_for_tts("# Заголовок\n- **жирный** пункт см. https://a.b/c"), "Заголовок жирный пункт см.")

    def test_confirm(self):
        self.assertEqual(confirm_phrase("Bash", {"command": "git push"}), "Хочу выполнить команду: git push. Можно?")
        self.assertEqual(confirm_phrase("mcp__claude_ai_Slack__slack_send_message",
                                        {"channel_id": "C1", "message": "привет", "n": 2, "users": ["U1", "U2"],
                                         "blocks": [{"text": "hi", "meta": {"to": "x@y"}}]}),
                         "Хочу вызвать slack_send_message: channel_id: C1; message: привет; n: 2; users: U1; users: U2; "
                         "text: hi; to: x@y. Можно?")
        self.assertEqual(confirm_phrase("Bash", {"command": "x" * 500}), f"Хочу выполнить команду: {'x' * 500}. Можно?")
        self.assertEqual(confirm_phrase("Edit", {"file_path": "/a", "old_string": "1", "new_string": "2"}),
                         "Хочу вызвать Edit: file_path: /a; old_string: 1; new_string: 2. Можно?")

    def _stub(self, body):
        d = tempfile.mkdtemp()
        p = Path(d) / "say"
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        os.environ["VOICE_MODE_SAY"] = str(p)
        self.addCleanup(os.environ.pop, "VOICE_MODE_SAY", None)
        return Path(d) / "argv"

    def test_speak(self):
        out = self._stub('printf "%s\\n" "$@" > "$(dirname "$0")/argv"\n')
        self.assertTrue(speak("Привет мир"))
        self.assertEqual(out.read_text().split("\n")[:4], ["-v", "Milena", "--", "Привет мир"])
        self.assertFalse(state.speaking_recently(grace=0))

    def test_speak_strips_say_commands(self):
        out = self._stub('printf "%s\\n" "$@" > "$(dirname "$0")/argv"\n')
        self.assertTrue(speak("раз [[volm 0]] два [[[slnc 4000]]] три"))
        spoken = out.read_text().split("\n")[3]
        self.assertNotIn("[[", spoken)
        self.assertNotIn("]]", spoken)
        self.assertEqual(spoken, "раз  volm 0  два  [slnc 4000 ] три")

    def test_speak_swallows_errors(self):
        self._stub("exit 3\n")
        self.assertFalse(speak("x"))
        os.environ["VOICE_MODE_SAY"] = "/nonexistent/say"
        self.assertFalse(speak("x"))
        self.assertFalse(state.speaking_recently(grace=0))

    def test_speak_unwritable_home_and_dash_text(self):
        out = self._stub('printf "%s\\n" "$@" > "$(dirname "$0")/argv"; echo noise\n')
        f = Path(tempfile.mkdtemp()) / "file"
        f.write_text("x")
        os.environ["VOICE_MODE_HOME"] = str(f / "sub")  # under a file: mkdir fails
        speak("-тест")
        os.environ["VOICE_MODE_HOME"] = tempfile.mkdtemp()
        speak("-тест")
        self.assertEqual(out.read_text().split("\n")[3], "-тест")

    def test_snake_case_kept(self):
        self.assertEqual(prepare_for_tts("my_var и other_var"), "my_var и other_var")

    def test_truncate_never_exceeds(self):
        for n in (298, 299, 300, 301):
            self.assertLessEqual(len(speech._truncate("а" * (n - 1) + ". Дальше текст.", 300)), 300)
        self.assertEqual(len(speech._truncate("а" * 299 + ". Дальше", 300)), 300)

    def test_confirm_no_args(self):
        self.assertEqual(confirm_phrase("mcp__s__do_it", {}), "Хочу вызвать do_it. Можно?")
        self.assertEqual(confirm_phrase("mcp__s__do_it", {"n": 1, "dry": False, "x": None}),
                         "Хочу вызвать do_it: n: 1; dry: нет. Можно?")
        self.assertEqual(confirm_phrase("Bash", {"command": "make", "description": "build", "timeout": 600000,
                                                 "run_in_background": True, "dangerouslyDisableSandbox": True}),
                         "Хочу выполнить команду: make; timeout: 600000; run_in_background: да; "
                         "dangerouslyDisableSandbox: да. Можно?")

    def test_speak_empty(self):
        self._stub("exit 0\n")
        self.assertFalse(speak("  "))
        self.assertFalse((state.home() / "speaking").exists())

    def test_instruction(self):
        self.assertIn("<speak>…</speak>", VOICE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
