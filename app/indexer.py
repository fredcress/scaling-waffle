"""Scans the (read-only) photo folder and prepares thumbnails / previews.

The photo folder is never written to. Everything generated lives in DATA_DIR.
"""
import fnmatch
import hashlib
import os
import re
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import config, db

IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "tif", "tiff", "avif", "bmp"}
VIDEO_EXT = {"mp4", "mov", "m4v", "avi", "mkv", "3gp", "webm", "mts", "m2ts"}
RAW_EXT = {"cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2", "srw", "pef", "raw"}

FP_CHUNK = 256 * 1024
LIVE_MAX_SECONDS = 3.5
PREVIEW_PX = 2000
THUMB_PX = 480

status = {"phase": "idle", "done": 0, "total": 0, "raw_ignored": 0, "error": None, "last_scan": None,
          "found": 0, "problems": [], "uid": f"{os.getuid()}:{os.getgid()}"}
_wake = threading.Event()
_thread: threading.Thread | None = None


# ---------------------------------------------------------------- paths

def cache_path(media_id: int, suffix: str) -> str:
    return os.path.join(config.CACHE_DIR, str(media_id // 1000), f"{media_id}_{suffix}")


def thumb_path(media_id: int) -> str:
    return cache_path(media_id, "t.jpg")


def preview_path(media_id: int) -> str:
    return cache_path(media_id, "p.jpg")


def video_preview_path(media_id: int) -> str:
    return cache_path(media_id, "v.mp4")


def source_path(relpath: str) -> str:
    return os.path.join(config.PHOTOS_DIR, *relpath.split("/"))


# ---------------------------------------------------------------- scan

def fingerprint(path: str, size: int) -> str:
    """Cheap content identity: size + first and last 256 KB."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(FP_CHUNK))
        if size > 2 * FP_CHUNK:
            f.seek(-FP_CHUNK, os.SEEK_END)
            h.update(f.read(FP_CHUNK))
    return h.hexdigest()


def _excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*") for pat in config.EXCLUDE)


def scan() -> dict:
    """Sync the media table with what is on disk. Ratings always survive."""
    c = db.conn()
    root = config.PHOTOS_DIR
    existing = {r["relpath"]: r for r in c.execute("SELECT id, relpath, size, mtime, fp, hidden FROM media")}
    seen: set[str] = set()
    new: list[tuple] = []
    raw = 0
    problems: list[str] = []
    if not os.path.isdir(root):
        problems.append(f"{root} does not exist inside the container")
    elif not os.access(root, os.R_OK | os.X_OK):
        problems.append(f"{root}: permission denied")
    c.execute("BEGIN")
    try:
        for dirpath, dirs, files in os.walk(root, onerror=lambda e: problems.append(f"{e.filename}: {e.strerror}")):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "@")))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if _excluded(rel):
                    continue
                if ext in RAW_EXT:
                    raw += 1
                    continue
                kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
                if kind is None:
                    continue
                try:
                    st = os.stat(full)
                    row = existing.get(rel)
                    if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                        seen.add(rel)
                        if row["hidden"] == "missing":
                            c.execute("UPDATE media SET hidden=NULL WHERE id=?", (row["id"],))
                        continue
                    fp = fingerprint(full, st.st_size)
                except OSError as e:
                    problems.append(f"{rel}: {e.strerror}")
                    continue
                seen.add(rel)
                if row:  # same path, different content
                    c.execute(
                        "UPDATE media SET size=?, mtime=?, fp=?, state='new', error=NULL, vprev=0, hidden=NULL, sharp=NULL, phash=NULL WHERE id=?",
                        (st.st_size, st.st_mtime, fp, row["id"]),
                    )
                else:
                    sender = rel.split("/", 1)[0] if "/" in rel else "Unknown"
                    new.append((rel, sender, kind, st.st_size, st.st_mtime, fp))

        # Files that vanished: either moved (same fingerprint shows up elsewhere) or gone.
        gone: dict[str, list] = {}
        for rel, row in existing.items():
            if rel not in seen:
                gone.setdefault(row["fp"], []).append(row)
        for rel, sender, kind, size, mtime, fp in new:
            moved = gone.get(fp)
            if moved:
                row = moved.pop()
                hidden = None if row["hidden"] == "missing" else row["hidden"]
                c.execute(
                    "UPDATE media SET relpath=?, sender=?, mtime=?, hidden=? WHERE id=?",
                    (rel, sender, mtime, hidden, row["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO media (relpath, sender, kind, size, mtime, fp) VALUES (?,?,?,?,?,?)",
                    (rel, sender, kind, size, mtime, fp),
                )
        for rows in gone.values():
            for row in rows:
                if row["hidden"] != "missing":
                    c.execute("UPDATE media SET hidden='missing' WHERE id=?", (row["id"],))

        # Exact duplicates (same file sent by several guests): keep the first, hide the rest.
        c.execute("UPDATE media SET hidden=NULL, dup_of=NULL WHERE hidden='duplicate'")
        dups = c.execute(
            "SELECT fp, MIN(id) AS keep FROM media WHERE hidden IS NULL GROUP BY fp HAVING COUNT(*) > 1"
        ).fetchall()
        for d in dups:
            c.execute(
                "UPDATE media SET hidden='duplicate', dup_of=? WHERE fp=? AND id<>? AND hidden IS NULL",
                (d["keep"], d["fp"], d["keep"]),
            )
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    status["raw_ignored"] = raw
    status["found"] = len(seen)
    status["problems"] = problems[:20]
    if problems:
        print(f"scan: {len(problems)} unreadable paths (running as uid:gid {status['uid']}), first: {problems[0]}", flush=True)
    print(f"scan: {len(seen)} photos/videos found under {root}, {len(new)} new, {raw} RAW ignored", flush=True)
    status["last_scan"] = datetime.now().isoformat(timespec="seconds")
    return {"new": len(new), "raw_ignored": raw}


def detect_live_photos() -> int:
    """Hide the short video half of an iPhone Live Photo (IMG_1.HEIC + IMG_1.MOV)."""
    c = db.conn()
    images = {
        r["relpath"].rsplit(".", 1)[0].lower()
        for r in c.execute("SELECT relpath FROM media WHERE kind='image' AND hidden IS NULL")
    }
    n = 0
    for r in c.execute(
        "SELECT id, relpath FROM media WHERE kind='video' AND hidden IS NULL AND duration IS NOT NULL AND duration<=?",
        (LIVE_MAX_SECONDS,),
    ).fetchall():
        if r["relpath"].rsplit(".", 1)[0].lower() in images:
            c.execute("UPDATE media SET hidden='live' WHERE id=?", (r["id"],))
            n += 1
    return n


# ---------------------------------------------------------------- previews

_heif_ready = False


def _pil():
    global _heif_ready
    from PIL import Image, ImageOps

    if not _heif_ready:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        _heif_ready = True
    return Image, ImageOps


def _save_jpeg(im, path: str, quality: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    im.save(tmp, format="JPEG", quality=quality, optimize=True)
    os.replace(tmp, path)


def _exif_taken(im) -> str | None:
    try:
        exif = im.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        pass
    return None


def make_image_previews(src: str, media_id: int) -> str | None:
    Image, ImageOps = _pil()
    with Image.open(src) as im:
        taken = _exif_taken(im)
        im.draft("RGB", (PREVIEW_PX, PREVIEW_PX))  # fast JPEG downscale, no-op elsewhere
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((PREVIEW_PX, PREVIEW_PX))
        _save_jpeg(im, preview_path(media_id), 85)
        im.thumbnail((THUMB_PX, THUMB_PX))
        _save_jpeg(im, thumb_path(media_id), 80)
    return taken


def probe_video(src: str) -> dict:
    """Duration / creation time / codec, parsed from `ffmpeg -i` (no ffprobe needed)."""
    p = subprocess.run(
        [config.FFMPEG, "-hide_banner", "-i", src], capture_output=True, text=True, errors="replace", timeout=120
    )
    out = p.stderr
    info: dict = {"duration": None, "taken": None, "h264": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"creation_time\s*:\s*(\S+)", out)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            info["taken"] = dt.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            pass
    m = re.search(r"Stream #.*Video:\s*(\w+)", out)
    if not m:
        raise RuntimeError("no video stream found")
    info["h264"] = m.group(1) == "h264"
    return info


def make_video_poster(src: str, media_id: int, duration: float | None) -> None:
    Image, _ = _pil()
    dst = preview_path(media_id)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp.jpg"
    seeks = [min(1.0, duration / 3)] if duration else []
    seeks.append(0)
    err = ""
    for ss in seeks:
        p = subprocess.run(
            [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{ss:.2f}", "-i", src,
             "-frames:v", "1", "-vf", f"scale={PREVIEW_PX}:{PREVIEW_PX}:force_original_aspect_ratio=decrease",
             "-q:v", "3", tmp],
            capture_output=True, text=True, errors="replace", timeout=300,
        )
        if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            break
        err = p.stderr.strip()[-300:]
    else:
        raise RuntimeError(f"could not extract a frame: {err}")
    with Image.open(dst) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB_PX, THUMB_PX))
        _save_jpeg(im, thumb_path(media_id), 80)


def transcode_video(src: str, media_id: int) -> None:
    """720p H.264 copy so every browser (and a VPN link) can play it."""
    dst = video_preview_path(media_id)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp.mp4"
    p = subprocess.run(
        [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp],
        capture_output=True, text=True, errors="replace", timeout=3 * 3600,
    )
    if p.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(p.stderr.strip()[-300:])
    os.replace(tmp, dst)


# ---------------------------------------------------------------- quality + bursts

def analyse_image(media_id: int) -> dict:
    """Cheap image statistics, computed from the cached preview (never the original)."""
    Image, _ = _pil()
    from PIL import ImageFilter, ImageStat

    with Image.open(preview_path(media_id)) as im:
        im.draft("L", (800, 800))
        g = im.convert("L")
    g.thumbnail((640, 640))
    st = ImageStat.Stat(g)

    # Sharpness = variance of the Laplacian, taken on the sharpest of 16 tiles so that a
    # portrait with a crisp subject and a soft background still counts as sharp.
    lap = g.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128))
    w, h = lap.size
    sharp = 0.0
    for ty in range(4):
        for tx in range(4):
            box = (max(1, tx * w // 4), max(1, ty * h // 4), min(w - 1, (tx + 1) * w // 4), min(h - 1, (ty + 1) * h // 4))
            if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
                sharp = max(sharp, ImageStat.Stat(lap.crop(box)).var[0])

    # dHash: 8x8 "is this pixel brighter than its right neighbour" bits.
    px = g.resize((9, 8), Image.LANCZOS).tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    return {"sharp": sharp, "lum": st.mean[0], "contrast": st.stddev[0], "phash": f"{bits:016x}"}


def quality_flags(sharp, lum, contrast) -> str:
    """b = blurry, d = very dark, o = overexposed, f = flat / no detail."""
    if sharp is None or lum is None or contrast is None:
        return ""
    flags = ""
    if sharp < config.BLUR_BELOW:
        flags += "b"
    if lum < config.DARK_BELOW:
        flags += "d"
    if lum > config.BRIGHT_ABOVE:
        flags += "o"
    if contrast < config.FLAT_BELOW:
        flags += "f"
    return flags


def analyse_one(row) -> None:
    c = db.conn()
    try:
        m = analyse_image(row["id"])
        c.execute("UPDATE media SET sharp=?, lum=?, contrast=?, phash=? WHERE id=?",
                  (m["sharp"], m["lum"], m["contrast"], m["phash"], row["id"]))
    except Exception as e:
        # -1 = "tried, could not tell": not flagged, not retried on every start
        c.execute("UPDATE media SET sharp=-1, error=? WHERE id=?", (f"analysis: {e}"[:400], row["id"]))
    finally:
        status["done"] += 1


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _gap_seconds(a: str | None, b: str | None) -> float:
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def assign_bursts() -> int:
    """Group look-alike shots a guest took within a few seconds of each other."""
    c = db.conn()
    rows = c.execute(
        "SELECT id, sender, taken_at, phash FROM media WHERE kind='image' AND state='ready' "
        "AND hidden IS NULL AND phash IS NOT NULL ORDER BY sender, taken_at, id").fetchall()
    groups: list[list[int]] = []
    cur: list[int] = []
    prev = None
    for r in rows:
        if (prev is not None and r["sender"] == prev["sender"]
                and _gap_seconds(prev["taken_at"], r["taken_at"]) <= config.BURST_SECONDS
                and hamming(prev["phash"], r["phash"]) <= config.BURST_DISTANCE):
            cur.append(r["id"])
        else:
            if len(cur) > 1:
                groups.append(cur)
            cur = [r["id"]]
        prev = r
    if len(cur) > 1:
        groups.append(cur)
    c.execute("BEGIN")
    c.execute("UPDATE media SET burst=NULL WHERE burst IS NOT NULL")
    c.executemany("UPDATE media SET burst=? WHERE id=?", [(min(g), i) for g in groups for i in g])
    c.execute("COMMIT")
    return len(groups)


def _mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def process_one(row) -> None:
    c = db.conn()
    src = source_path(row["relpath"])
    try:
        duration = None
        vprev = 0
        if row["kind"] == "image":
            taken = make_image_previews(src, row["id"])
        else:
            info = probe_video(src)
            duration, taken = info["duration"], info["taken"]
            make_video_poster(src, row["id"], duration)
            # Small H.264 MP4s already play everywhere: no transcode needed.
            if info["h264"] and src.lower().endswith((".mp4", ".m4v")) and row["size"] < 40 * 1024 * 1024:
                vprev = 2
        c.execute(
            "UPDATE media SET state='ready', error=NULL, taken_at=?, duration=?, vprev=? WHERE id=?",
            (taken or _mtime_iso(row["mtime"]), duration, vprev, row["id"]),
        )
    except Exception as e:  # one bad file must never stop the run
        c.execute("UPDATE media SET state='error', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:400], row["id"]))
    finally:
        status["done"] += 1


def run_pipeline() -> None:
    c = db.conn()
    status.update(phase="scanning", done=0, total=0, error=None)
    scan()

    rows = c.execute(
        "SELECT id, relpath, kind, size, mtime FROM media WHERE state='new' AND hidden IS NULL ORDER BY kind, relpath"
    ).fetchall()
    status.update(phase="thumbnails", done=0, total=len(rows))
    if rows:
        with ThreadPoolExecutor(config.WORKERS) as pool:
            list(pool.map(process_one, rows))
    detect_live_photos()

    rows = c.execute(
        "SELECT id FROM media WHERE kind='image' AND state='ready' AND hidden IS NULL AND sharp IS NULL").fetchall()
    status.update(phase="looking for blurry shots and bursts", done=0, total=len(rows))
    if rows:
        with ThreadPoolExecutor(config.WORKERS) as pool:
            list(pool.map(analyse_one, rows))
    assign_bursts()

    if config.VIDEO_PREVIEWS:
        rows = c.execute(
            "SELECT id, relpath FROM media WHERE kind='video' AND state='ready' AND hidden IS NULL AND vprev=0 ORDER BY size"
        ).fetchall()
        status.update(phase="video previews", done=0, total=len(rows))
        for r in rows:
            try:
                transcode_video(source_path(r["relpath"]), r["id"])
                c.execute("UPDATE media SET vprev=1 WHERE id=?", (r["id"],))
            except Exception as e:
                c.execute("UPDATE media SET vprev=3, error=? WHERE id=?", (f"preview: {e}"[:400], r["id"]))
            status["done"] += 1
    status.update(phase="idle", done=0, total=0)


def _loop() -> None:
    while True:
        _wake.wait()
        _wake.clear()
        try:
            run_pipeline()
        except Exception as e:
            traceback.print_exc()
            status.update(phase="idle", error=f"{type(e).__name__}: {e}")


def start() -> None:
    """Kick off (or re-run) the background scan."""
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="indexer", daemon=True)
        _thread.start()
    _wake.set()
