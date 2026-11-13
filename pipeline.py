"""Isma3 - the processing pipeline.

Audio -> 16 kHz WAV (ffmpeg) -> transcript (mlx-whisper) -> speaker labels (sherpa-onnx)
-> notes (Ollama) -> search index (Ollama embeddings) -> Obsidian note.
Everything runs on this Mac. Nothing is sent to the internet.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
REC_DIR = DATA_DIR / "recordings"
MODELS_DIR = APP_DIR / "models"
SEG_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = MODELS_DIR / "nemo_en_titanet_small.onnx"

DEFAULT_CONFIG = {
    "name": "",
    "obsidian_vault": "",
    "obsidian_folder": "Isma3",
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "language": None,
    "llm_model": "qwen3:14b",
    "embed_model": "nomic-embed-text",
    "ollama_url": "http://127.0.0.1:11434",
    "speaker_threshold": 0.9,
    "port": 8765,
}

STAGES = {
    "queued": "Waiting",
    "converting": "Preparing audio",
    "transcribing": "Transcribing",
    "speakers": "Identifying speakers",
    "notes": "Writing summary",
    "indexing": "Indexing for Ask",
    "exporting": "Saving to Obsidian",
    "done": "Done",
    "error": "Failed",
}


# ---------------------------------------------------------------- config & storage

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    for path in (APP_DIR / "config.example.json", APP_DIR / "config.json"):  # your config.json wins
        if path.exists():
            cfg.update(json.loads(path.read_text()))
    return cfg


def rec_path(rec_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", rec_id):
        raise ValueError("bad recording id")
    return REC_DIR / rec_id


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


_meta_lock = threading.Lock()


def get_meta(rec_id: str) -> dict:
    return read_json(rec_path(rec_id) / "meta.json", {})


def update_meta(rec_id: str, **changes) -> dict:
    with _meta_lock:
        meta = get_meta(rec_id)
        meta.update(changes)
        write_json(rec_path(rec_id) / "meta.json", meta)
        return meta


def list_recordings() -> list[dict]:
    REC_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for d in REC_DIR.iterdir():
        m = read_json(d / "meta.json")
        if m:
            out.append(m)
    return sorted(out, key=lambda m: m.get("created", ""), reverse=True)


def create_recording(src_bytes: bytes, filename: str, title: str | None, num_speakers: int | None,
                     source: str) -> dict:
    rec_id = uuid.uuid4().hex[:12]
    d = REC_DIR / rec_id
    d.mkdir(parents=True)
    ext = (Path(filename).suffix or ".webm").lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".webm"
    (d / f"original{ext}").write_bytes(src_bytes)
    now = datetime.now()
    meta = {
        "id": rec_id,
        "title": (title or "").strip() or f"Recording {now:%d %b %Y, %H:%M}",
        "title_locked": bool((title or "").strip()),
        "created": now.isoformat(timespec="seconds"),
        "original": f"original{ext}",
        "source": source,
        "num_speakers": num_speakers,
        "duration": None,
        "speakers": {},
        "stage": "queued",
        "error": None,
        "export_path": None,
        "export_error": None,
    }
    write_json(d / "meta.json", meta)
    return meta


def delete_recording(rec_id: str) -> None:
    shutil.rmtree(rec_path(rec_id), ignore_errors=True)


# ---------------------------------------------------------------- helpers

def fmt_ts(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def speaker_name(meta: dict, key: str) -> str:
    return meta.get("speakers", {}).get(key) or key.replace("S", "Speaker ")


def transcript_text(meta: dict, utterances: list[dict]) -> str:
    return "\n".join(
        f"[{fmt_ts(u['start'])}] {speaker_name(meta, u['speaker'])}: {u['text']}" for u in utterances
    )


# ---------------------------------------------------------------- stage 1: audio

def convert_audio(d: Path, original: str) -> tuple[Path, float]:
    wav = d / "audio16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(d / original),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    with wave.open(str(wav)) as w:
        duration = w.getnframes() / w.getframerate()
    return wav, duration


def load_wav(wav: Path) -> np.ndarray:
    with wave.open(str(wav)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- stage 2: transcription

def transcribe(wav: Path, cfg: dict) -> list[dict]:
    """Returns whisper segments: [{start, end, text, words:[{word,start,end}]}]."""
    import mlx_whisper  # Apple Silicon only

    result = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=cfg["whisper_model"],
        language=cfg.get("language"),
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    segs = []
    for s in result.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        segs.append({
            "start": float(s["start"]),
            "end": float(s["end"]),
            "text": text,
            "words": [
                {"word": w["word"], "start": float(w["start"]), "end": float(w["end"])}
                for w in s.get("words", [])
            ],
        })
    return segs


# ---------------------------------------------------------------- stage 3: speakers

def diarize(samples: np.ndarray, cfg: dict, num_speakers: int | None) -> list[tuple[float, float, int]]:
    if not (SEG_MODEL.exists() and EMB_MODEL.exists()):
        return []
    import sherpa_onnx as so

    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL)),
        clustering=so.FastClusteringConfig(
            num_clusters=int(num_speakers) if num_speakers else -1,
            threshold=float(cfg.get("speaker_threshold", 0.9)),
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = so.OfflineSpeakerDiarization(config)
    result = sd.process(samples).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in result]


def _speaker_at(t0: float, t1: float, turns) -> int | None:
    best, best_overlap = None, 0.0
    for s, e, spk in turns:
        overlap = min(t1, e) - max(t0, s)
        if overlap > best_overlap:
            best, best_overlap = spk, overlap
    if best is not None:
        return best
    mid = (t0 + t1) / 2  # no overlap: take the nearest turn
    nearest = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])), default=None)
    return nearest[2] if nearest else None


def build_utterances(segments: list[dict], turns) -> list[dict]:
    """Assign each word to a speaker, then group runs of words into utterances."""
    units = []  # (start, end, text, raw_speaker, segment_index)
    for i, seg in enumerate(segments):
        words = seg["words"] or [{"word": seg["text"], "start": seg["start"], "end": seg["end"]}]
        for w in words:
            spk = _speaker_at(w["start"], w["end"], turns) if turns else 0
            units.append((w["start"], w["end"], w["word"], spk if spk is not None else 0, i))

    order: dict[int, str] = {}  # raw diarisation id -> S1, S2... in order of first appearance
    utterances: list[dict] = []
    for start, end, word, raw, seg_i in units:
        key = order.setdefault(raw, f"S{len(order) + 1}")
        last = utterances[-1] if utterances else None
        long_pause = last is not None and start - last["end"] > 2.0
        too_long = last is not None and len(last["text"]) > 600 and seg_i != last["_seg"]
        if last and last["speaker"] == key and not long_pause and not too_long:
            last["text"] += word
            last["end"] = end
            last["_seg"] = seg_i
        else:
            utterances.append({"start": start, "end": end, "speaker": key, "text": word, "_seg": seg_i})
    for u in utterances:
        u.pop("_seg")
        u["text"] = re.sub(r"\s+", " ", u["text"]).strip()
    return [u for u in utterances if u["text"]]


# ---------------------------------------------------------------- stage 4: notes (Ollama)

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_takeaways": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["task", "owner", "due"],
            },
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "points"],
            },
        },
    },
    "required": ["title", "summary", "key_takeaways", "action_items", "topics"],
}

NOTES_SYSTEM = (
    "You are Isma3, a private note-taker. You turn conversation transcripts into clear notes "
    "in British English. Only use what is in the transcript; never invent names, dates or numbers. "
    "Refer to people by the speaker names used in the transcript."
)

NOTES_INSTRUCTIONS = """Write notes for this recording.
- title: a short, specific title (max 8 words).
- summary: 2-5 sentences covering what was discussed and decided.
- key_takeaways: 3-8 short bullet points.
- action_items: every concrete to-do. owner = the person responsible, or "" if unclear. due = the deadline as said in the conversation, or "" if none.
- topics: 2-6 main topics, each with 2-5 short points. These become the mind map.

Transcript:
"""


def ollama_chat(cfg: dict, messages: list[dict], schema: dict | None = None, num_ctx: int = 16384) -> str:
    body = {
        "model": cfg["llm_model"],
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    if schema:
        body["format"] = schema
    r = requests.post(f"{cfg['ollama_url']}/api/chat", json=body, timeout=1800)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def ollama_embed(cfg: dict, texts: list[str]) -> np.ndarray:
    vecs = []
    for i in range(0, len(texts), 32):
        r = requests.post(
            f"{cfg['ollama_url']}/api/embed",
            json={"model": cfg["embed_model"], "input": texts[i:i + 32], "keep_alive": "5m"},
            timeout=600,
        )
        r.raise_for_status()
        vecs.extend(r.json()["embeddings"])
    arr = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.maximum(norms, 1e-8)


CHUNK_CHARS = 36000  # ~9k tokens; longer transcripts are summarised in parts first


def make_notes(cfg: dict, meta: dict, utterances: list[dict]) -> dict:
    text = transcript_text(meta, utterances)
    if not text.strip():
        return {"title": meta["title"], "summary": "No speech was detected in this recording.",
                "key_takeaways": [], "action_items": [], "topics": []}

    if len(text) > CHUNK_CHARS:
        lines, parts, cur = text.split("\n"), [], ""
        for line in lines:
            if len(cur) + len(line) > CHUNK_CHARS and cur:
                parts.append(cur)
                cur = ""
            cur += line + "\n"
        if cur:
            parts.append(cur)
        partial = []
        for n, part in enumerate(parts, 1):
            partial.append(f"Part {n} of {len(parts)}:\n" + ollama_chat(cfg, [
                {"role": "system", "content": NOTES_SYSTEM},
                {"role": "user", "content": "Write detailed bullet-point notes of this part of a longer "
                 "conversation. Keep every decision, number, name, deadline and to-do.\n\n" + part},
            ]))
        source = "Notes from each part of a long recording (in order):\n\n" + "\n\n".join(partial)
    else:
        source = text

    raw = ollama_chat(cfg, [
        {"role": "system", "content": NOTES_SYSTEM},
        {"role": "user", "content": NOTES_INSTRUCTIONS + source},
    ], schema=NOTES_SCHEMA)
    notes = json.loads(raw)
    for key in ("key_takeaways", "action_items", "topics"):
        notes.setdefault(key, [])
    return notes


# ---------------------------------------------------------------- stage 5: search index

def build_chunks(meta: dict, utterances: list[dict], max_chars: int = 900) -> list[dict]:
    chunks, cur, start = [], [], None
    for u in utterances:
        line = f"{speaker_name(meta, u['speaker'])}: {u['text']}"
        if cur and sum(len(x) for x in cur) + len(line) > max_chars:
            chunks.append({"start": start, "text": "\n".join(cur)})
            cur, start = [], None
        if start is None:
            start = u["start"]
        cur.append(line)
    if cur:
        chunks.append({"start": start, "text": "\n".join(cur)})
    return chunks


def index_recording(cfg: dict, rec_id: str, meta: dict, utterances: list[dict], notes: dict) -> None:
    d = rec_path(rec_id)
    chunks = build_chunks(meta, utterances)
    summary_text = f"Summary of '{notes.get('title', meta['title'])}': {notes.get('summary', '')}"
    chunks.insert(0, {"start": 0.0, "text": summary_text, "kind": "summary"})
    vecs = ollama_embed(cfg, [f"search_document: {c['text']}" for c in chunks])
    write_json(d / "chunks.json", chunks)
    np.save(d / "embeddings.npy", vecs)


# ---------------------------------------------------------------- stage 6: Obsidian

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", name).strip()
    return name[:80] or "Recording"


def obsidian_markdown(meta: dict, utterances: list[dict], notes: dict) -> str:
    created = datetime.fromisoformat(meta["created"])
    speakers = sorted({speaker_name(meta, u["speaker"]) for u in utterances})
    out = [
        "---",
        "type: isma3-recording",
        f"date: {created:%Y-%m-%d}",
        f"time: \"{created:%H:%M}\"",
        f"duration: \"{fmt_ts(meta.get('duration') or 0)}\"",
        "speakers: [" + ", ".join(json.dumps(s) for s in speakers) + "]",
        "tags: [isma3]",
        "---",
        "",
        f"# {meta['title']}",
        "",
        "## Summary",
        notes.get("summary", ""),
        "",
    ]
    if notes.get("key_takeaways"):
        out += ["## Key takeaways"] + [f"- {t}" for t in notes["key_takeaways"]] + [""]
    if notes.get("action_items"):
        out.append("## Action items")
        for a in notes["action_items"]:
            extra = " — ".join(x for x in (a.get("owner"), f"due {a['due']}" if a.get("due") else "") if x)
            out.append(f"- [ ] {a['task']}" + (f" ({extra})" if extra else ""))
        out.append("")
    if notes.get("topics"):
        out += ["## Mind map", f"- **{meta['title']}**"]
        for t in notes["topics"]:
            out.append(f"\t- {t['topic']}")
            out += [f"\t\t- {p}" for p in t.get("points", [])]
        out.append("")
    out.append("## Transcript")
    for u in utterances:
        out.append(f"**{speaker_name(meta, u['speaker'])}** `{fmt_ts(u['start'])}` {u['text']}")
        out.append("")
    return "\n".join(out)


def export_obsidian(cfg: dict, rec_id: str) -> None:
    d = rec_path(rec_id)
    meta = get_meta(rec_id)
    utterances = read_json(d / "transcript.json", [])
    notes = read_json(d / "notes.json", {})
    if not (cfg.get("obsidian_vault") or "").strip():
        return  # Obsidian export is optional
    vault = Path(cfg["obsidian_vault"]).expanduser()
    if not vault.is_dir():
        update_meta(rec_id, export_error=f"Obsidian vault not found at {vault}. Set obsidian_vault in config.json.")
        return
    if meta.get("export_path") and Path(meta["export_path"]).parent.exists():
        target = Path(meta["export_path"])
    else:
        folder = vault / cfg.get("obsidian_folder", "Isma3")
        folder.mkdir(parents=True, exist_ok=True)
        created = datetime.fromisoformat(meta["created"])
        target = folder / f"{created:%Y-%m-%d %H%M} {_safe_filename(meta['title'])}.md"
    target.write_text(obsidian_markdown(meta, utterances, notes), encoding="utf-8")
    update_meta(rec_id, export_path=str(target), export_error=None)


# ---------------------------------------------------------------- speaker edits

def merge_speakers(rec_id: str) -> bool:
    """If two speakers share a name, fold them into one. Returns True if anything merged."""
    d = rec_path(rec_id)
    meta = get_meta(rec_id)
    speakers = dict(meta.get("speakers") or {})
    first_key_for: dict[str, str] = {}
    remap: dict[str, str] = {}
    for key in sorted(speakers, key=lambda k: int(k[1:]) if k[1:].isdigit() else 99):
        name = speakers[key].strip().lower()
        if name in first_key_for:
            remap[key] = first_key_for[name]
        else:
            first_key_for[name] = key
    if not remap:
        return False
    merged: list[dict] = []
    for u in read_json(d / "transcript.json", []):
        u = dict(u, speaker=remap.get(u["speaker"], u["speaker"]))
        last = merged[-1] if merged else None
        if last and last["speaker"] == u["speaker"] and u["start"] - last["end"] <= 2.0:
            last["text"] = f"{last['text']} {u['text']}"
            last["end"] = u["end"]
        else:
            merged.append(u)
    write_json(d / "transcript.json", merged)
    for k in remap:
        speakers.pop(k, None)
    update_meta(rec_id, speakers=speakers)
    return True


# ---------------------------------------------------------------- the worker

def process(rec_id: str, mode: str = "full") -> None:
    """mode: "full" (audio -> everything), "speakers" (re-label speakers, then notes), "notes" (notes only)."""
    cfg = load_config()
    d = rec_path(rec_id)
    meta = get_meta(rec_id)
    try:
        if mode == "speakers" and not ((d / "segments.json").exists() and (d / "audio16k.wav").exists()):
            mode = "full"  # older recordings: no saved segments, so transcribe again
        if mode in ("full", "speakers"):
            if mode == "full":
                update_meta(rec_id, stage="converting", error=None)
                wav, duration = convert_audio(d, meta["original"])
                update_meta(rec_id, duration=round(duration, 1))
                update_meta(rec_id, stage="transcribing")
                segments = transcribe(wav, cfg)
                write_json(d / "segments.json", segments)
            else:
                update_meta(rec_id, error=None)
                wav = d / "audio16k.wav"
                segments = read_json(d / "segments.json", [])

            update_meta(rec_id, stage="speakers")
            meta = get_meta(rec_id)
            turns = diarize(load_wav(wav), cfg, meta.get("num_speakers")) if segments else []
            utterances = build_utterances(segments, turns)
            old = dict(meta.get("speakers") or {}) if mode == "full" else {}
            speakers = {u["speaker"]: old.get(u["speaker"], u["speaker"].replace("S", "Speaker ")) for u in utterances}
            meta = update_meta(rec_id, speakers=speakers)
            write_json(d / "transcript.json", utterances)
        else:
            update_meta(rec_id, error=None)
            utterances = read_json(d / "transcript.json", [])

        update_meta(rec_id, stage="notes")
        notes = make_notes(cfg, meta, utterances)
        write_json(d / "notes.json", notes)
        if not meta.get("title_locked") and notes.get("title"):
            meta = update_meta(rec_id, title=notes["title"].strip())

        update_meta(rec_id, stage="indexing")
        if utterances:
            index_recording(cfg, rec_id, meta, utterances, notes)

        update_meta(rec_id, stage="exporting")
        try:
            export_obsidian(cfg, rec_id)
        except Exception as e:  # export problems shouldn't lose the notes
            update_meta(rec_id, export_error=str(e))

        update_meta(rec_id, stage="done", finished=datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if isinstance(e, requests.ConnectionError):
            msg = "Couldn't reach Ollama. Open the Ollama app, then press Retry."
        elif isinstance(e, ModuleNotFoundError) and "mlx_whisper" in msg:
            msg = "mlx-whisper isn't installed. Re-run the install step."
        update_meta(rec_id, stage="error", error=msg)


class Worker:
    """Processes one recording at a time so the models never compete for memory."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, rec_id: str, mode: str = "full") -> None:
        update_meta(rec_id, stage="queued", error=None)
        self.q.put((rec_id, mode))

    def _run(self):
        while True:
            rec_id, mode = self.q.get()
            try:
                if rec_path(rec_id).exists():
                    process(rec_id, mode)
            except Exception:
                traceback.print_exc()
            finally:
                self.q.task_done()


def recover_interrupted() -> None:
    """Anything left mid-way when the app last closed is marked as failed so it can be retried."""
    for m in list_recordings():
        if m.get("stage") not in ("done", "error"):
            update_meta(m["id"], stage="error", error="The app was closed while this was processing. Press Retry.")


# ---------------------------------------------------------------- Ask Isma3

ASK_SYSTEM = (
    "You are Isma3, a private assistant that answers questions about the user's own recorded "
    "conversations. Answer in British English, briefly and directly, using only the excerpts given. "
    "Cite where each fact came from like [1] or [2], matching the excerpt numbers. If the excerpts "
    "don't contain the answer, say so plainly."
)


def ask(question: str, rec_id: str | None = None, top_k: int = 8) -> dict:
    cfg = load_config()
    metas = [get_meta(rec_id)] if rec_id else [m for m in list_recordings() if m.get("stage") == "done"]

    excerpts = []
    if rec_id and metas[0]:
        meta = metas[0]
        text = transcript_text(meta, read_json(rec_path(rec_id) / "transcript.json", []))
        if len(text) < CHUNK_CHARS:  # short enough: give the model the whole thing
            excerpts = [{"rec": meta, "start": 0.0, "text": text}]

    if not excerpts:
        pool = []
        for meta in metas:
            d = rec_path(meta["id"])
            if not (d / "embeddings.npy").exists():
                continue
            chunks = read_json(d / "chunks.json", [])
            vecs = np.load(d / "embeddings.npy")
            pool += [(meta, c, v) for c, v in zip(chunks, vecs)]
        if not pool:
            return {"answer": "There are no processed recordings to search yet.", "sources": []}
        q = ollama_embed(cfg, [f"search_query: {question}"])[0]
        scores = np.array([float(v @ q) for _, _, v in pool])
        best = scores.argsort()[::-1][:top_k]
        excerpts = [{"rec": pool[i][0], "start": pool[i][1]["start"], "text": pool[i][1]["text"]} for i in best]

    context = "\n\n".join(
        f"[{n}] From \"{e['rec']['title']}\" ({e['rec']['created'][:10]}) at {fmt_ts(e['start'])}:\n{e['text']}"
        for n, e in enumerate(excerpts, 1)
    )
    answer = ollama_chat(cfg, [
        {"role": "system", "content": ASK_SYSTEM},
        {"role": "user", "content": f"Excerpts:\n\n{context}\n\nQuestion: {question}"},
    ], num_ctx=16384)
    sources = [
        {"n": n, "id": e["rec"]["id"], "title": e["rec"]["title"], "start": e["start"],
         "date": e["rec"]["created"][:10]}
        for n, e in enumerate(excerpts, 1)
    ]
    return {"answer": answer, "sources": sources}


# ---------------------------------------------------------------- health check

def health() -> dict:
    cfg = load_config()
    checks = {}
    checks["ffmpeg"] = {"ok": shutil.which("ffmpeg") is not None, "fix": "brew install ffmpeg"}
    try:
        import importlib.util
        ok = importlib.util.find_spec("mlx_whisper") is not None
    except Exception:
        ok = False
    checks["whisper"] = {"ok": ok, "fix": "pip install -r requirements.txt (inside the app's .venv)"}
    checks["speaker models"] = {"ok": SEG_MODEL.exists() and EMB_MODEL.exists(), "fix": "./download_models.sh"}
    try:
        tags = requests.get(f"{cfg['ollama_url']}/api/tags", timeout=3).json()
        names = {m["name"] for m in tags.get("models", [])}

        def has(model):
            return model in names or f"{model}:latest" in names

        checks["ollama"] = {"ok": True, "fix": ""}
        checks["summary model"] = {"ok": has(cfg["llm_model"]), "fix": f"ollama pull {cfg['llm_model']}"}
        checks["search model"] = {"ok": has(cfg["embed_model"]), "fix": f"ollama pull {cfg['embed_model']}"}
    except Exception:
        checks["ollama"] = {"ok": False, "fix": "Open the Ollama app"}
    vault_set = bool((cfg.get("obsidian_vault") or "").strip())
    vault = Path(cfg["obsidian_vault"]).expanduser() if vault_set else None
    if vault_set:
        checks["obsidian vault"] = {"ok": vault.is_dir(), "fix": f"Check obsidian_vault in config.json (now: {vault})"}
    return {"checks": checks, "vault": str(vault) if vault else "", "folder": cfg.get("obsidian_folder", "Isma3"),
            "name": (cfg.get("name") or "").strip()}
