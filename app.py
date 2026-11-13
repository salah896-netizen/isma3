"""Isma3 - web server. Run with ./start.command, then open http://localhost:8765"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import pipeline as P

app = FastAPI(title="Isma3")
P.REC_DIR.mkdir(parents=True, exist_ok=True)
(P.DATA_DIR / "server.pid").write_text(str(__import__("os").getpid()))  # lets the launcher restart us after updates
P.recover_interrupted()
worker = P.Worker()


def _meta_or_404(rec_id: str) -> dict:
    try:
        meta = P.get_meta(rec_id)
    except ValueError:
        meta = None
    if not meta:
        raise HTTPException(404, "Recording not found")
    return meta


@app.get("/")
def index():
    return FileResponse(P.APP_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    return P.health()


@app.get("/api/recordings")
def recordings():
    return P.list_recordings()


@app.post("/api/recordings")
async def upload(file: UploadFile = File(...), title: str = Form(""), num_speakers: str = Form(""),
                 source: str = Form("upload")):
    n = int(num_speakers) if num_speakers.strip().isdigit() and int(num_speakers) > 0 else None
    try:
        meta = P.create_recording(file.file, file.filename or "recording.webm", title, n, source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    worker.submit(meta["id"])
    return P.get_meta(meta["id"])


@app.get("/api/recordings/{rec_id}")
def recording(rec_id: str):
    meta = _meta_or_404(rec_id)
    d = P.rec_path(rec_id)
    transcript = P.read_json(d / "transcript.json", [])
    if meta.get("tc_start"):
        for u in transcript:
            u["tc"] = P.tc_at(meta, u["start"])
    return {
        "meta": meta,
        "fps_label": P.fps_label(meta.get("fps")),
        "transcript": transcript,
        "notes": P.read_json(d / "notes.json", None),
    }


@app.get("/api/recordings/{rec_id}/audio")
def audio(rec_id: str):
    meta = _meta_or_404(rec_id)
    d = P.rec_path(rec_id)
    if (d / "playback.m4a").exists():
        return FileResponse(d / "playback.m4a", media_type="audio/mp4")
    if meta["original"].endswith(".webm") and (d / "audio16k.wav").exists():
        return FileResponse(d / "audio16k.wav", media_type="audio/wav")  # older recordings: seekable fallback
    return FileResponse(d / meta["original"])


@app.get("/api/recordings/{rec_id}/original")
def original(rec_id: str):
    meta = _meta_or_404(rec_id)
    return FileResponse(P.rec_path(rec_id) / meta["original"], filename=f"{meta['title'][:60]}{Path(meta['original']).suffix}")


class Patch(BaseModel):
    title: str | None = None
    speakers: dict[str, str] | None = None
    tc_start: str | None = None   # "HH:MM:SS:FF" (or ";" before frames for drop-frame)
    fps: str | None = None        # one of P.FPS_CHOICES keys, e.g. "25" or "29.97"
    tc_clear: bool = False


@app.patch("/api/recordings/{rec_id}")
def patch(rec_id: str, body: Patch):
    meta = _meta_or_404(rec_id)
    changes = {}
    if body.title is not None and body.title.strip():
        changes.update(title=body.title.strip()[:120], title_locked=True)
    if body.speakers:
        speakers = dict(meta.get("speakers") or {})
        for k, v in body.speakers.items():
            if k in speakers and v.strip():
                speakers[k] = v.strip()[:40]
        changes["speakers"] = speakers
    if body.tc_clear:
        changes.update(tc_start=None, tc_from=None)
    elif body.tc_start is not None:
        fps = P.FPS_CHOICES.get(body.fps or "", body.fps or meta.get("fps") or "")
        tc = body.tc_start.strip().replace(".", ":")
        if not fps or not P.valid_tc(tc, fps):
            raise HTTPException(400, "Enter timecode as HH:MM:SS:FF, with frames below the frame rate.")
        if ";" in tc and round(P._fps_value(fps)) not in (30, 60):
            raise HTTPException(400, "Drop-frame (;) only applies to 29.97 or 59.94 fps.")
        changes.update(tc_start=tc, fps=fps, tc_from="set manually")
    meta = P.update_meta(rec_id, **changes)
    if body.speakers:
        P.merge_speakers(rec_id)  # two speakers given the same name become one
    if meta.get("stage") == "done":  # keep the Obsidian note in step with renames
        try:
            P.export_obsidian(P.load_config(), rec_id)
        except Exception as e:
            P.update_meta(rec_id, export_error=str(e))
    return P.get_meta(rec_id)


@app.post("/api/recordings/{rec_id}/retry")
def retry(rec_id: str):
    meta = _meta_or_404(rec_id)
    if meta.get("stage") not in ("done", "error"):
        raise HTTPException(409, "Still processing")
    worker.submit(rec_id)
    return P.get_meta(rec_id)


@app.post("/api/recordings/{rec_id}/regenerate")
def regenerate(rec_id: str):
    """Rewrite the summary/actions/mind map (e.g. after renaming speakers), keeping the transcript."""
    meta = _meta_or_404(rec_id)
    if meta.get("stage") not in ("done", "error"):
        raise HTTPException(409, "Still processing")
    if not (P.rec_path(rec_id) / "transcript.json").exists():
        raise HTTPException(400, "No transcript yet. Use Retry instead.")
    worker.submit(rec_id, "notes")
    return P.get_meta(rec_id)


@app.get("/api/recordings/{rec_id}/export/{fmt}")
def export(rec_id: str, fmt: str):
    from fastapi.responses import Response
    meta = _meta_or_404(rec_id)
    if not (P.rec_path(rec_id) / "transcript.json").exists():
        raise HTTPException(400, "No transcript yet.")
    makers = {"srt": (P.export_srt, "application/x-subrip"), "txt": (P.export_txt, "text/plain; charset=utf-8"),
              "csv": (P.export_csv, "text/csv; charset=utf-8")}
    if fmt not in makers:
        raise HTTPException(404, "Unknown export")
    make, media = makers[fmt]
    name = (P._safe_filename(meta.get("source_name") and Path(meta["source_name"]).stem or meta["title"]) or "isma3")
    if fmt == "csv":
        name += " select list"
    from urllib.parse import quote
    return Response(make(rec_id), media_type=media,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.{fmt}"})


class SpeakerCount(BaseModel):
    num_speakers: int | None = None


@app.post("/api/recordings/{rec_id}/speakers")
def redo_speakers(rec_id: str, body: SpeakerCount):
    """Re-label who said what with a given number of speakers (keeps the transcript text)."""
    meta = _meta_or_404(rec_id)
    if meta.get("stage") not in ("done", "error"):
        raise HTTPException(409, "Still processing")
    n = body.num_speakers if body.num_speakers and 0 < body.num_speakers <= 12 else None
    P.update_meta(rec_id, num_speakers=n)
    worker.submit(rec_id, "speakers")
    return P.get_meta(rec_id)


@app.delete("/api/recordings/{rec_id}")
def delete(rec_id: str):
    meta = _meta_or_404(rec_id)
    if meta.get("stage") not in ("done", "error"):
        raise HTTPException(409, "Wait until it has finished processing.")
    P.delete_recording(rec_id)
    return {"ok": True}


class Ask(BaseModel):
    question: str
    recording_id: str | None = None


@app.post("/api/ask")
def ask(body: Ask):
    if not body.question.strip():
        raise HTTPException(400, "Ask a question first.")
    if body.recording_id:
        _meta_or_404(body.recording_id)
    try:
        return P.ask(body.question.strip(), body.recording_id)
    except P.requests.ConnectionError:
        raise HTTPException(503, "Couldn't reach Ollama. Open the Ollama app and try again.")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(P.load_config().get("port", 8765)))
