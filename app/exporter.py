"""Send the chosen photos to Immich (API upload) or copy them to a folder."""
import os
import shutil
import threading
import traceback
from datetime import datetime, timezone

from . import config, db
from .indexer import source_path

job = {"running": False, "target": None, "done": 0, "total": 0, "uploaded": 0, "skipped": 0,
       "errors": [], "message": ""}
_lock = threading.Lock()


def _selected(ids: list[int]) -> list[dict]:
    c = db.conn()
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        rows = c.execute(
            f"SELECT id, relpath, sender, fp, mtime, taken_at FROM media WHERE id IN ({q}) AND hidden IS NULL", chunk
        ).fetchall()
        for r in rows:
            rs = c.execute("SELECT score, star FROM ratings WHERE media_id=?", (r["id"],)).fetchall()
            scores = [x["score"] for x in rs if x["score"]]
            out.append({
                **dict(r),
                "rating": round(sum(scores) / len(scores)) if scores else None,
                "star": any(x["star"] for x in rs),
            })
    return out


def _done(media_id: int, target: str) -> str | None:
    r = db.conn().execute("SELECT ref FROM exports WHERE media_id=? AND target=?", (media_id, target)).fetchone()
    return r["ref"] if r else None


def _mark(media_id: int, target: str, ref: str) -> None:
    db.conn().execute("INSERT OR REPLACE INTO exports (media_id, target, ref) VALUES (?,?,?)", (media_id, target, ref))


# ---------------------------------------------------------------- Immich

def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _immich_client():
    import httpx

    base = config.IMMICH_URL
    if not base.endswith("/api"):
        base += "/api"
    return httpx.Client(base_url=base, headers={"x-api-key": config.IMMICH_API_KEY, "Accept": "application/json"},
                        timeout=httpx.Timeout(30, read=1800, write=1800))


def immich_check() -> dict:
    """Used by the UI to verify URL + key before a long upload."""
    with _immich_client() as cl:
        r = cl.get("/albums")
        r.raise_for_status()
        return {"ok": True, "albums": len(r.json())}


def _upload(cl, item: dict, state: dict) -> str:
    path = source_path(item["relpath"])
    modified = _iso_utc(item["mtime"])
    created = f"{item['taken_at']}.000Z" if item["taken_at"] else modified
    last = None
    # Older Immich servers require deviceAssetId/deviceId, newer ones dropped them:
    # try the form that worked last time first, fall back to the other on HTTP 400.
    for legacy in (state["legacy"], not state["legacy"]):
        data = {"fileCreatedAt": created, "fileModifiedAt": modified}
        if legacy:
            data.update(deviceAssetId=f"wedding-triage-{item['fp']}", deviceId="wedding-triage")
        with open(path, "rb") as f:
            last = cl.post("/assets", data=data,
                           files={"assetData": (os.path.basename(path), f, "application/octet-stream")})
        if last.status_code in (200, 201):
            state["legacy"] = legacy
            return last.json()["id"]
        if last.status_code != 400:
            break
    raise RuntimeError(f"upload failed: HTTP {last.status_code} {last.text[:200]}")


def _annotate(cl, asset_id: str, item: dict) -> None:
    body = {"ids": [asset_id], "isFavorite": bool(item["star"])}
    if item["rating"]:
        body["rating"] = item["rating"]
    r = cl.put("/assets", json={**body, "description": f"Sent by {item['sender']}"})
    if r.status_code == 400:  # server without bulk description support
        r = cl.put("/assets", json=body)
    r.raise_for_status()


def _album_id(cl) -> str:
    r = cl.get("/albums")
    r.raise_for_status()
    for a in r.json():
        if a.get("albumName") == config.IMMICH_ALBUM:
            return a["id"]
    r = cl.post("/albums", json={"albumName": config.IMMICH_ALBUM})
    r.raise_for_status()
    return r.json()["id"]


def _run_immich(items: list[dict]) -> None:
    state = {"legacy": True}
    asset_ids = []
    with _immich_client() as cl:
        album = _album_id(cl)
        for it in items:
            try:
                ref = _done(it["id"], "immich")
                if ref:
                    job["skipped"] += 1
                else:
                    ref = _upload(cl, it, state)
                    _mark(it["id"], "immich", ref)
                    job["uploaded"] += 1
                _annotate(cl, ref, it)  # ratings may have changed since the last export
                asset_ids.append(ref)
            except Exception as e:
                job["errors"].append(f"{it['relpath']}: {e}"[:300])
            job["done"] += 1
        for i in range(0, len(asset_ids), 200):
            cl.put(f"/albums/{album}/assets", json={"ids": asset_ids[i:i + 200]}).raise_for_status()
    job["message"] = f"Album “{config.IMMICH_ALBUM}” updated."


# ---------------------------------------------------------------- folder

def _run_folder(items: list[dict]) -> None:
    for it in items:
        try:
            src = source_path(it["relpath"])
            dst = os.path.join(config.EXPORT_DIR, *it["relpath"].split("/"))
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                job["skipped"] += 1
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst + ".part")
                os.replace(dst + ".part", dst)
                job["uploaded"] += 1
            _mark(it["id"], "folder", dst)
        except Exception as e:
            job["errors"].append(f"{it['relpath']}: {e}"[:300])
        job["done"] += 1
    job["message"] = f"Copied to {config.EXPORT_DIR}."


def _run(target: str, ids: list[int]) -> None:
    try:
        items = _selected(ids)
        job["total"] = len(items)
        (_run_immich if target == "immich" else _run_folder)(items)
    except Exception as e:
        traceback.print_exc()
        job["errors"].append(f"{type(e).__name__}: {e}"[:300])
        job["message"] = "Export stopped early."
    finally:
        job["running"] = False


def start(target: str, ids: list[int]) -> bool:
    with _lock:
        if job["running"]:
            return False
        job.update(running=True, target=target, done=0, total=len(ids), uploaded=0, skipped=0, errors=[], message="")
    threading.Thread(target=_run, args=(target, ids), name="export", daemon=True).start()
    return True
