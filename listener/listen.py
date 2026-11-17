#!/usr/bin/env python3
"""Mic -> VAD -> local mlx-whisper -> one line per utterance: '<unix_ts>\\t<text>'."""
import argparse, atexit, os, queue, signal, subprocess, sys, tempfile, time, wave

import numpy as np
import sounddevice as sd
import webrtcvad

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from voicemode import state, wake  # noqa: E402

RATE, FRAME_MS = 16000, 30
FRAME = RATE * FRAME_MS // 1000          # samples per frame
SILENCE_FRAMES = 700 // FRAME_MS         # end of phrase
MIN_FRAMES = 300 // FRAME_MS             # shorter phrases are dropped
MAX_FRAMES = 15000 // FRAME_MS           # hard cap per phrase
PREROLL = 3                              # frames kept before speech starts
PROMPT = "Клод, Claude."  # wake words only: never put commands here (echo on noise would run them)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Segmenter:
    """Feed 30 ms int16 frames; feed() returns float32 audio when a phrase ends, else None."""

    def __init__(self, aggressiveness=2):
        self.vad, self.buf, self.silent, self.pre = webrtcvad.Vad(aggressiveness), [], 0, []

    def reset(self):
        self.buf, self.silent, self.pre = [], 0, []

    @property
    def idle(self):  # no phrase in progress
        return not self.buf

    def feed(self, f):
        if self.vad.is_speech(f, RATE):
            if not self.buf:
                self.buf = self.pre[:]  # pre-roll: don't clip the first phoneme
            self.buf.append(f); self.silent = 0
        elif self.buf:
            self.buf.append(f); self.silent += 1
        else:
            self.pre = (self.pre + [f])[-PREROLL:]
        if self.buf and (self.silent >= SILENCE_FRAMES or len(self.buf) >= MAX_FRAMES):
            speech = len(self.buf) - self.silent
            pcm, self.buf, self.silent, self.pre = b"".join(self.buf), [], 0, []
            if speech >= MIN_FRAMES:
                return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768


def file_frames(path):
    with tempfile.TemporaryDirectory() as d:
        wav = f"{d}/a.wav"  # afconvert handles wav/aiff, any rate -> 16 kHz mono
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", path, wav], check=True)
        with wave.open(wav) as w:
            pcm = w.readframes(w.getnframes())
    pcm += b"\0" * (FRAME * 2 * (SILENCE_FRAMES * 2))  # trailing silence closes the last phrase
    step = FRAME * 2
    for i in range(0, len(pcm) - step + 1, step):
        yield pcm[i:i + step]


def mic_frames(device_log, idle):
    """Frames from the default input. Every 5 s, while VAD is idle, the stream is closed
    first and only then PortAudio is re-initialised to see a changed default device."""
    while True:
        try:
            name = sd.query_devices(kind="input")["name"]
            if device_log:
                log("input device:", name)
            q = queue.Queue()

            def cb(data, n, t, status):
                if status:
                    log("stream status:", status)
                q.put(bytes(data))

            changed = False
            with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=cb):
                last = time.time()
                while not changed:
                    yield q.get(timeout=5)
                    if time.time() - last > 5 and idle():
                        last = time.time()
                        changed = True  # leave the context (closes the stream), then re-check below
            sd._terminate(); sd._initialize()
        except Exception as e:  # noqa: BLE001 - any stream failure -> reopen
            log("stream error:", repr(e)); time.sleep(2)
            try:
                sd._terminate(); sd._initialize()
            except Exception as e2:  # noqa: BLE001
                log("portaudio reinit error:", repr(e2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="to_stdout")
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--device-log", action="store_true")
    ap.add_argument("--file", help="wav/aiff instead of the mic (self-check)")
    ap.add_argument("--session", help="write utterances to this session's queue; exit when it is turned off")
    ap.add_argument("--parent-pid", type=int, help="exit when this process (Claude Code) is gone")
    ap.add_argument("--prompt", default=PROMPT, help="whisper initial_prompt")
    ap.add_argument("--vad", type=int, default=2, choices=range(4), help="webrtcvad aggressiveness")
    a = ap.parse_args()
    import mlx_whisper

    def stt(audio):
        return mlx_whisper.transcribe(audio, path_or_hf_repo=a.model, language="ru", initial_prompt=a.prompt or None,
                                      condition_on_previous_text=False)["text"].strip()

    stt(np.zeros(RATE, np.float32))  # warm-up: first phrase must not pay for model load
    seg = Segmenter(a.vad)
    frames = file_frames(a.file) if a.file else mic_frames(a.device_log, lambda: seg.idle)
    sid, last_check = a.session, time.time()
    if sid:
        state.save_listener_pid(sid, os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # run atexit on kill

        def rm_pid():  # only if the file is still ours (a newer listener may own it)
            if state.listener_pid(sid) == os.getpid():
                try:
                    os.remove(state.home() / sid / "listener.pid")
                except OSError:
                    pass
        atexit.register(rm_pid)
    for f in frames:
        if sid:
            if time.time() - last_check >= 1:
                last_check = time.time()
                if not state.is_on(sid) or (a.parent_pid and not alive(a.parent_pid)):
                    return
            if state.speaking_recently():  # don't hear our own TTS
                seg.reset()  # no half-phrase glued across TTS
                continue
        audio = seg.feed(f)
        if audio is None:
            continue
        t0 = time.time()  # end of the speech segment: the utterance ts (STT latency must not make it look newer)
        try:
            text = stt(audio)
        except Exception as e:  # noqa: BLE001 - skip this phrase, keep listening
            log("transcribe error:", repr(e)); continue
        log(f"latency {time.time() - t0:.2f}s")
        if text and sid and not wake.is_hallucination(text) and state.is_on(sid):  # not after turn_off
            state.append_utterance(sid, t0, text)
        if text and a.to_stdout:  # the hook never passes --print: listener.log must not hold a transcript
            print(f"{int(time.time())}\t{text}", flush=True)


if __name__ == "__main__":
    main()
