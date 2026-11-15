"""Folder scanning: senders, duplicates, RAW, moved and deleted files. Standard library only."""
import os
import shutil
import tempfile
import unittest

from app import config, db, indexer


def configure(tmp: str) -> str:
    photos = os.path.join(tmp, "photos")
    os.makedirs(photos, exist_ok=True)
    config.PHOTOS_DIR = photos
    config.DATA_DIR = os.path.join(tmp, "data")
    config.DB_PATH = os.path.join(config.DATA_DIR, "triage.sqlite3")
    config.CACHE_DIR = os.path.join(config.DATA_DIR, "cache")
    config.EXCLUDE = []
    db.init()
    return photos


def put(root: str, rel: str, content: bytes) -> None:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def rows() -> dict:
    return {r["relpath"]: dict(r) for r in db.conn().execute("SELECT * FROM media")}


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.photos = configure(self.tmp)

    def test_senders_kinds_and_ignored_files(self):
        put(self.photos, "Aunt Marie/a.JPG", b"a" * 100)
        put(self.photos, "Aunt Marie/sub/clip.MOV", b"b" * 100)
        put(self.photos, "loose.heic", b"c" * 100)
        put(self.photos, "Uncle/shot.CR3", b"d" * 100)
        put(self.photos, "Uncle/notes.txt", b"e" * 100)
        put(self.photos, "Uncle/.hidden.jpg", b"f" * 100)
        put(self.photos, "@eaDir/thumb.jpg", b"g" * 100)
        indexer.scan()
        r = rows()
        self.assertEqual(set(r), {"Aunt Marie/a.JPG", "Aunt Marie/sub/clip.MOV", "loose.heic"})
        self.assertEqual(r["Aunt Marie/a.JPG"]["sender"], "Aunt Marie")
        self.assertEqual(r["Aunt Marie/sub/clip.MOV"]["sender"], "Aunt Marie")
        self.assertEqual(r["Aunt Marie/sub/clip.MOV"]["kind"], "video")
        self.assertEqual(r["loose.heic"]["sender"], "Unknown")
        self.assertEqual(indexer.status["raw_ignored"], 1)

    def test_exclude(self):
        config.EXCLUDE = ["Uncle"]
        put(self.photos, "Uncle/a.jpg", b"a" * 10)
        put(self.photos, "Marie/b.jpg", b"b" * 10)
        indexer.scan()
        self.assertEqual(set(rows()), {"Marie/b.jpg"})

    def test_exact_duplicates_are_hidden_once(self):
        put(self.photos, "A/x.jpg", b"same" * 50)
        put(self.photos, "B/x-copy.jpg", b"same" * 50)
        put(self.photos, "B/other.jpg", b"diff" * 50)
        indexer.scan()
        indexer.scan()  # stable on a second pass
        r = rows()
        self.assertIsNone(r["A/x.jpg"]["hidden"])
        self.assertEqual(r["B/x-copy.jpg"]["hidden"], "duplicate")
        self.assertEqual(r["B/x-copy.jpg"]["dup_of"], r["A/x.jpg"]["id"])
        self.assertIsNone(r["B/other.jpg"]["hidden"])

    def test_moved_file_keeps_its_rating(self):
        put(self.photos, "A/x.jpg", b"x" * 500)
        indexer.scan()
        mid = rows()["A/x.jpg"]["id"]
        db.set_rating(mid, "Wife", 5, True)
        os.makedirs(os.path.join(self.photos, "Marie"))
        os.rename(os.path.join(self.photos, "A", "x.jpg"), os.path.join(self.photos, "Marie", "x.jpg"))
        indexer.scan()
        r = rows()
        self.assertEqual(set(r), {"Marie/x.jpg"})
        self.assertEqual(r["Marie/x.jpg"]["id"], mid)
        self.assertEqual(r["Marie/x.jpg"]["sender"], "Marie")

    def test_deleted_file_is_hidden_and_comes_back(self):
        put(self.photos, "A/x.jpg", b"x" * 500)
        indexer.scan()
        mid = rows()["A/x.jpg"]["id"]
        db.set_rating(mid, "Wife", 4, False)
        os.remove(os.path.join(self.photos, "A", "x.jpg"))
        indexer.scan()
        self.assertEqual(rows()["A/x.jpg"]["hidden"], "missing")
        put(self.photos, "A/x.jpg", b"x" * 500)
        indexer.scan()
        self.assertIsNone(rows()["A/x.jpg"]["hidden"])
        n = db.conn().execute("SELECT score FROM ratings WHERE media_id=?", (mid,)).fetchone()
        self.assertEqual(n["score"], 4)

    def test_ratings_upsert_and_clear(self):
        put(self.photos, "A/x.jpg", b"x" * 500)
        indexer.scan()
        mid = rows()["A/x.jpg"]["id"]
        db.set_rating(mid, "Wife", 3, False)
        db.set_rating(mid, "Wife", 5, True)
        db.set_rating(mid, "Husband", 2, False)
        got = {r["user"]: (r["score"], r["star"]) for r in db.conn().execute("SELECT * FROM ratings")}
        self.assertEqual(got, {"Wife": (5, 1), "Husband": (2, 0)})
        db.set_rating(mid, "Wife", None, False)
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM ratings").fetchone()[0], 1)

    def test_live_photo_clip_is_hidden(self):
        put(self.photos, "A/IMG_1.HEIC", b"i" * 100)
        put(self.photos, "A/IMG_1.MOV", b"v" * 100)
        put(self.photos, "A/IMG_2.MOV", b"w" * 100)
        indexer.scan()
        db.conn().execute("UPDATE media SET duration=2.8 WHERE kind='video'")
        self.assertEqual(indexer.detect_live_photos(), 1)
        r = rows()
        self.assertEqual(r["A/IMG_1.MOV"]["hidden"], "live")
        self.assertIsNone(r["A/IMG_2.MOV"]["hidden"])

    def _ready_image(self, rel, taken, phash):
        put(self.photos, rel, rel.encode() * 20)
        indexer.scan()
        db.conn().execute("UPDATE media SET state='ready', taken_at=?, phash=? WHERE relpath=?", (taken, phash, rel))

    def test_bursts_group_lookalikes_taken_together(self):
        self._ready_image("A/1.jpg", "2026-09-12T16:00:00", "ffff0000ffff0000")
        self._ready_image("A/2.jpg", "2026-09-12T16:00:02", "ffff0000ffff0001")   # 1 bit off, 2 s later
        self._ready_image("A/3.jpg", "2026-09-12T16:00:05", "ffff0000ffff0003")   # chains on from 2
        self._ready_image("A/4.jpg", "2026-09-12T16:00:06", "0000ffff0000ffff")   # same moment, different picture
        self._ready_image("A/5.jpg", "2026-09-12T17:00:00", "ffff0000ffff0000")   # same picture, an hour later
        self._ready_image("B/6.jpg", "2026-09-12T16:00:01", "ffff0000ffff0000")   # same picture, another guest
        self.assertEqual(indexer.assign_bursts(), 1)
        r = rows()
        first = r["A/1.jpg"]["id"]
        self.assertEqual([r[f"A/{n}.jpg"]["burst"] for n in (1, 2, 3)], [first] * 3)
        for rel in ("A/4.jpg", "A/5.jpg", "B/6.jpg"):
            self.assertIsNone(r[rel]["burst"])
        db.conn().execute("UPDATE media SET hidden='missing' WHERE relpath IN ('A/2.jpg', 'A/3.jpg')")
        self.assertEqual(indexer.assign_bursts(), 0)   # regrouped from scratch every time
        self.assertIsNone(rows()["A/1.jpg"]["burst"])

    def test_quality_flags(self):
        self.assertEqual(indexer.quality_flags(500, 120, 60), "")
        self.assertEqual(indexer.quality_flags(5, 120, 60), "b")
        self.assertEqual(indexer.quality_flags(5, 10, 3), "bdf")
        self.assertEqual(indexer.quality_flags(500, 250, 60), "o")
        self.assertEqual(indexer.quality_flags(None, None, None), "")
        self.assertEqual(indexer.hamming("ffff0000ffff0000", "ffff0000ffff0003"), 2)

    def test_bulk_rating_never_overwrites(self):
        for n in (1, 2, 3):
            put(self.photos, f"A/{n}.jpg", bytes([n]) * 300)
        indexer.scan()
        ids = [r["id"] for r in rows().values()]
        db.set_rating(ids[0], "Wife", 4, True)
        self.assertEqual(db.bulk_rate_unrated(ids, "Wife", 1), 2)
        got = {r["media_id"]: (r["score"], r["star"]) for r in db.conn().execute("SELECT * FROM ratings")}
        self.assertEqual(got, {ids[0]: (4, 1), ids[1]: (1, 0), ids[2]: (1, 0)})


if __name__ == "__main__":
    unittest.main()
