"""Wedding photo triage: rate guest photos 1-5, super-star the best, export to Immich."""
import gzip
import json
import mimetypes
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, exporter, indexer

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MEDIA_CACHE = {"Cache-Control": "private, max-age=604800"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    indexer.start()
    yield


app = FastAPI(title="Wedding photo triage", lifespan=lifespan)


class RateBody(BaseModel):
    id: int
    user: str
    score: int | None = Field(default=None, ge=1, le=5)
    star: bool = False


class ExportBody(BaseModel):
    target: str
    ids: list[int]


def _status() -> dict:
    c = db.conn()
    counts = {r["k"]: r["n"] for r in c.execute(
        "SELECT COALESCE(hidden, state) AS k, COUNT(*) AS n FROM media GROUP BY 1")}
    return {
        **indexer.status,
        "ready": counts.get("ready", 0),
        "pending": counts.get("new", 0),
        "errors": counts.get("error", 0),
        "duplicates": counts.get("duplicate", 0),
        "live": counts.get("live", 0),
        "missing": counts.get("missing", 0),
    }


@app.get("/api/config")
def get_config():
    return {
        "users": config.USERS,
        "immich": bool(config.IMMICH_URL and config.IMMICH_API_KEY),
        "immich_album": config.IMMICH_ALBUM,
        "export_dir": bool(config.EXPORT_DIR),
    }


@app.get("/api/status")
def get_status():
    return _status()


@app.get("/api/media")
def get_media(request: Request):
    c = db.conn()
    ratings: dict[int, dict] = {}
    for r in c.execute("SELECT media_id, user, score, star FROM ratings"):
        ratings.setdefault(r["media_id"], {})[r["user"]] = [r["score"], r["star"]]
    exported = {r["media_id"] for r in c.execute("SELECT DISTINCT media_id FROM exports")}
    items = [
        {
            "id": r["id"],
            "s": r["sender"],
            "k": "v" if r["kind"] == "video" else "i",
            "n": r["relpath"].rsplit("/", 1)[-1],
            "t": r["taken_at"],
            "d": r["duration"],
            "r": ratings.get(r["id"], {}),
            "x": 1 if r["id"] in exported else 0,
        }
        for r in c.execute(
            "SELECT id, sender, kind, relpath, taken_at, duration FROM media "
            "WHERE state='ready' AND hidden IS NULL ORDER BY sender COLLATE NOCASE, taken_at, relpath")
    ]
    body = json.dumps({"items": items, "status": _status()}, separators=(",", ":")).encode()
    headers = {"Cache-Control": "no-store"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, 5)
        headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(body, media_type="application/json", headers=headers)


@app.post("/api/rate")
def rate(body: RateBody):
    if body.user not in config.USERS:
        raise HTTPException(400, "unknown user")
    if not db.conn().execute("SELECT 1 FROM media WHERE id=?", (body.id,)).fetchone():
        raise HTTPException(404, "unknown photo")
    db.set_rating(body.id, body.user, body.score, body.star)
    return {"ok": True}


@app.post("/api/rescan")
def rescan():
    db.conn().execute("UPDATE media SET state='new', error=NULL WHERE state='error'")
    db.conn().execute("UPDATE media SET vprev=0 WHERE vprev=3")
    indexer.start()
    return {"ok": True}


@app.get("/api/errors")
def errors():
    rows = db.conn().execute(
        "SELECT relpath, error FROM media WHERE error IS NOT NULL AND hidden IS NULL ORDER BY relpath LIMIT 500")
    return [dict(r) for r in rows]


@app.get("/api/immich/check")
def immich_check():
    if not (config.IMMICH_URL and config.IMMICH_API_KEY):
        raise HTTPException(400, "IMMICH_URL / IMMICH_API_KEY are not set")
    try:
        return exporter.immich_check()
    except Exception as e:
        raise HTTPException(502, f"Immich not reachable: {e}"[:300])


@app.post("/api/export")
def export(body: ExportBody):
    if body.target == "immich":
        if not (config.IMMICH_URL and config.IMMICH_API_KEY):
            raise HTTPException(400, "IMMICH_URL / IMMICH_API_KEY are not set")
    elif body.target == "folder":
        if not config.EXPORT_DIR:
            raise HTTPException(400, "EXPORT_DIR is not set")
    else:
        raise HTTPException(400, "unknown target")
    if not body.ids:
        raise HTTPException(400, "nothing selected")
    if not exporter.start(body.target, body.ids):
        raise HTTPException(409, "an export is already running")
    return {"ok": True}


@app.get("/api/export/status")
def export_status():
    return exporter.job


def _row(media_id: int):
    r = db.conn().execute("SELECT id, relpath, kind, vprev FROM media WHERE id=?", (media_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return r


def _file(path: str, media_type: str | None = None, **kw):
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type=media_type, headers=MEDIA_CACHE, **kw)


@app.get("/thumb/{media_id}")
def thumb(media_id: int):
    return _file(indexer.thumb_path(media_id), "image/jpeg")


@app.get("/preview/{media_id}")
def preview(media_id: int):
    return _file(indexer.preview_path(media_id), "image/jpeg")


@app.get("/video/{media_id}")
def video(media_id: int):
    r = _row(media_id)
    if r["kind"] != "video":
        raise HTTPException(404)
    transcoded = indexer.video_preview_path(media_id)
    if r["vprev"] == 1 and os.path.isfile(transcoded):
        return _file(transcoded, "video/mp4")
    src = indexer.source_path(r["relpath"])
    return _file(src, mimetypes.guess_type(src)[0] or "video/mp4")


@app.get("/original/{media_id}")
def original(media_id: int):
    r = _row(media_id)
    src = indexer.source_path(r["relpath"])
    return _file(src, mimetypes.guess_type(src)[0] or "application/octet-stream", filename=os.path.basename(src))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
