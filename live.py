"""Isma3 live notes: a rough transcript and rolling notes while you record.

The browser streams 16 kHz mono audio here every few seconds. A background thread transcribes it in
short slices (cut at quiet moments so words aren't chopped) and, every minute or so, asks a smaller,
faster model to update the notes. When you stop, the normal full pass takes over and produces the
proper transcript, speaker names and final notes.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid

import numpy as np
import requests

import pipeline as P

RATE = 16000
SESSIONS: dict[str, "LiveSession"] = {}
_sessions_lock = threading.Lock()

LIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_now": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "object", "properties": {
            "task": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task", "owner"]}},
    },
    "required": ["topic_now", "key_points", "decisions", "action_items"],
}

LIVE_PROMPT = """These are live, rough notes of a conversation that is still going on (the transcript may have small errors).
Update the notes so far, in British English, using only what is in the transcript.
- topic_now: what they're talking about right now (a few words).
- key_points: up to 8 short bullets covering the whole conversation so far.
- decisions: anything agreed or decided.
- action_items: to-dos with an owner if one was named ("" if not).

Previous notes (update and improve these, don't repeat them blindly):
{previous}

Transcript so far:
{transcript}"""


def _live_model(cfg: dict) -> str:
    want = cfg.get("live_llm_model") or cfg["llm_model"]
    try:
        names = {m["name"] for m in requests.get(f"{cfg['ollama_url']}/api/tags", timeout=3).json().get("models", [])}
        if want in names or f"{want}:latest" in names:
            return want
    except Exception:
        pass
    return cfg["llm_model"]


class LiveSession:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = P.load_config()
        self.model = _live_model(self.cfg)
        self.audio = np.zeros(0, dtype=np.float32)
        self.done_until = 0          # samples already transcribed
        self.lines: list[dict] = []  # {start, end, text}
        self.notes: dict = {}
        self.notes_chars = 0         # transcript length when the notes were last updated
        self.notes_at = 0.0
        self.stopped = False
        self.finished = False
        self.error = None
        self.touched = time.time()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    # -- audio in
    def add(self, pcm16: bytes):
        x = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        with self.lock:
            self.audio = np.concatenate([self.audio, x])
            self.touched = time.time()

    # -- processing loop
    def _cut_point(self, end: int) -> int:
        """Cut at the quietest 100 ms in the last 1.5 s before `end`, so words aren't split."""
        a = self.audio
        lo = max(self.done_until + RATE, end - int(1.5 * RATE))
        best, best_e = end, None
        for i in range(lo, end - RATE // 10, RATE // 20):
            e = float(np.mean(np.square(a[i:i + RATE // 10])))
            if best_e is None or e < best_e:
                best, best_e = i + RATE // 20, e
        return best

    def _transcribe(self, start: int, end: int):
        x = self.audio[start:end]
        if len(x) < RATE // 2 or float(np.sqrt(np.mean(np.square(x)))) < 0.004:
            return  # silence: Whisper tends to invent text here, so skip it
        import mlx_whisper
        with P.WHISPER_LOCK:
            result = mlx_whisper.transcribe(x, path_or_hf_repo=self.cfg["whisper_model"], language=self.cfg.get("language"),
                                            condition_on_previous_text=False, word_timestamps=False)
        off = start / RATE
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if text and seg.get("no_speech_prob", 0) < 0.6:
                self.lines.append({"start": round(off + seg["start"], 2), "end": round(off + seg["end"], 2), "text": text})

    def _update_notes(self):
        text = "\n".join(f"[{P.fmt_ts(l['start'])}] {l['text']}" for l in self.lines)[-20000:]
        prompt = LIVE_PROMPT.format(previous=json.dumps(self.notes) if self.notes else "(none yet)", transcript=text)
        cfg = dict(self.cfg, llm_model=self.model)
        raw = P.ollama_chat(cfg, [{"role": "system", "content": P.NOTES_SYSTEM}, {"role": "user", "content": prompt}],
                            schema=LIVE_SCHEMA, num_ctx=8192)
        self.notes = json.loads(raw)
        self.notes_chars = sum(len(l["text"]) for l in self.lines)
        self.notes_at = time.time()

    def _run(self):
        try:
            while True:
                with self.lock:
                    total = len(self.audio)
                    stopped = self.stopped
                pending = total - self.done_until
                if pending >= 6 * RATE or (stopped and pending > 0):
                    end = total if stopped else self._cut_point(min(total, self.done_until + 20 * RATE))
                    self._transcribe(self.done_until, end)
                    self.done_until = end
                    continue
                chars = sum(len(l["text"]) for l in self.lines)
                first = not self.notes and chars >= 150      # first notes come quickly
                due = first or (chars - self.notes_chars >= 300 and time.time() - self.notes_at >= 45)
                if (due or (stopped and chars > self.notes_chars)) and self.lines:
                    try:
                        self._update_notes()
                    except Exception as e:
                        self.error = "Live notes paused: " + (
                            "couldn't reach Ollama." if isinstance(e, requests.ConnectionError) else str(e))
                        self.notes_chars = chars
                    continue
                if stopped:
                    break
                if time.time() - self.touched > 120:  # browser went away
                    break
                time.sleep(0.5)
        except Exception as e:
            traceback.print_exc()
            self.error = "Live transcript stopped: " + str(e)
        finally:
            self.finished = True

    def state(self) -> dict:
        lag = max(0.0, (len(self.audio) - self.done_until) / RATE)
        return {"id": self.id, "lines": self.lines, "notes": self.notes, "lag": round(lag, 1),
                "stopped": self.stopped, "finished": self.finished, "error": self.error, "model": self.model}


def start() -> LiveSession:
    s = LiveSession()
    with _sessions_lock:
        for k in [k for k, v in SESSIONS.items() if v.finished and time.time() - v.touched > 600]:
            del SESSIONS[k]
        SESSIONS[s.id] = s
    return s


def get(sid: str) -> LiveSession | None:
    return SESSIONS.get(sid)
