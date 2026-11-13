"""Isma3 - web server. Run with ./start.command, then open http://localhost:8765"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import pipeline as P

app = FastAPI(title="Isma3")
P.REC_DIR.mkdir(parents=True, exist_ok=True)
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
    data = await file.read()
    if len(data) < 1000:
        raise HTTPException(400, "That recording is empty.")
    n = int(num_speakers) if num_speakers.strip().isdigit() and int(num_speakers) > 0 else None
    meta = P.create_recording(data, file.filename or "recording.webm", title, n, source)
    worker.submit(meta["id"])
    return P.get_meta(meta["id"])


@app.get("/api/recordings/{rec_id}")
def recording(rec_id: str):
    meta = _meta_or_404(rec_id)
    d = P.rec_path(rec_id)
    return {
        "meta": meta,
        "transcript": P.read_json(d / "transcript.json", []),
        "notes": P.read_json(d / "notes.json", None),
    }


@app.get("/api/recordings/{rec_id}/audio")
def audio(rec_id: str):
    meta = _meta_or_404(rec_id)
    return FileResponse(P.rec_path(rec_id) / meta["original"])


class Patch(BaseModel):
    title: str | None = None
    speakers: dict[str, str] | None = None


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
    meta = P.update_meta(rec_id, **changes)
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
    worker.submit(rec_id, notes_only=True)
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
