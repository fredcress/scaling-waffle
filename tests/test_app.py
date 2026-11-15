"""End-to-end: real images through the pipeline and the HTTP API. Needs the pip requirements."""
import os
import shutil
import subprocess
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    from PIL import Image

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

from app import config, db, indexer
from tests.test_scan import configure

HAVE_FFMPEG = shutil.which(config.FFMPEG) is not None


@unittest.skipUnless(HAVE_DEPS, "fastapi / pillow not installed")
class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        photos = configure(cls.tmp)
        config.USERS = ["Husband", "Wife"]
        config.VIDEO_PREVIEWS = True
        os.makedirs(os.path.join(photos, "Marie"))
        os.makedirs(os.path.join(photos, "Paul"))

        # Landscape pixels flagged "rotate 90°" in EXIF, like a phone held upright.
        im = Image.new("RGB", (800, 400), (200, 80, 40))
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[306] = "2026:09:12 16:30:00"
        im.save(os.path.join(photos, "Marie", "portrait.jpg"), exif=exif)
        Image.new("RGBA", (300, 300), (0, 120, 200, 128)).save(os.path.join(photos, "Paul", "logo.png"))
        # Fine detail vs. the same picture blurred, and a two-shot burst.
        noise = Image.frombytes("RGB", (400, 300), os.urandom(400 * 300 * 3))
        noise.save(os.path.join(photos, "Marie", "sharp.jpg"), quality=95)
        from PIL import ImageFilter

        noise.filter(ImageFilter.GaussianBlur(12)).save(os.path.join(photos, "Marie", "blurry.jpg"), quality=95)
        os.makedirs(os.path.join(photos, "Zoe"))
        scene = Image.linear_gradient("L").rotate(30).convert("RGB")
        scene.paste((255, 255, 255), (40, 60, 120, 200))
        stamp = Image.Exif()
        stamp[306] = "2026:09:12 18:00:00"
        scene.save(os.path.join(photos, "Zoe", "burst1.jpg"), quality=92, exif=stamp)
        scene.save(os.path.join(photos, "Zoe", "burst2.jpg"), quality=70, exif=stamp)
        with open(os.path.join(photos, "Paul", "broken.jpg"), "wb") as f:
            f.write(b"this is not a jpeg")
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            Image.new("RGB", (640, 480), (30, 160, 90)).save(os.path.join(photos, "Paul", "iphone.heic"))
            cls.heic = True
        except Exception:
            cls.heic = False
        cls.video = False
        if HAVE_FFMPEG:
            p = subprocess.run(
                [config.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=15",
                 "-pix_fmt", "yuv420p", os.path.join(photos, "Paul", "clip.mov")], capture_output=True)
            cls.video = p.returncode == 0

        indexer.run_pipeline()
        from app.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def items(self):
        r = self.client.get("/api/media")
        self.assertEqual(r.status_code, 200)
        return {it["n"]: it for it in r.json()["items"]}, r.json()["status"]

    def test_listing_and_previews(self):
        items, status = self.items()
        self.assertIn("portrait.jpg", items)
        self.assertIn("logo.png", items)
        self.assertNotIn("broken.jpg", items)
        self.assertEqual(status["errors"], 1)
        self.assertEqual(status["phase"], "idle")
        it = items["portrait.jpg"]
        self.assertEqual(it["s"], "Marie")
        self.assertEqual(it["t"], "2026-09-12T16:30:00")
        for kind in ("thumb", "preview"):
            r = self.client.get(f"/{kind}/{it['id']}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/jpeg")
        with Image.open(indexer.preview_path(it["id"])) as im:
            self.assertEqual(im.size, (400, 800))  # EXIF rotation applied
        with Image.open(indexer.thumb_path(it["id"])) as im:
            self.assertLessEqual(max(im.size), indexer.THUMB_PX)
        self.assertEqual(self.client.get("/thumb/99999").status_code, 404)

    def test_heic(self):
        if not self.heic:
            self.skipTest("HEIC encoding not available")
        items, _ = self.items()
        self.assertIn("iphone.heic", items)
        self.assertEqual(self.client.get(f"/preview/{items['iphone.heic']['id']}").status_code, 200)

    def test_video(self):
        if not self.video:
            self.skipTest("ffmpeg not available")
        items, _ = self.items()
        it = items["clip.mov"]
        self.assertEqual(it["k"], "v")
        self.assertAlmostEqual(it["d"], 6, delta=0.5)
        self.assertEqual(self.client.get(f"/thumb/{it['id']}").status_code, 200)
        self.assertTrue(os.path.isfile(indexer.video_preview_path(it["id"])))
        r = self.client.get(f"/video/{it['id']}", headers={"Range": "bytes=0-99"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.headers["content-type"], "video/mp4")
        self.assertEqual(len(r.content), 100)

    def test_rating_flow(self):
        items, _ = self.items()
        mid = items["portrait.jpg"]["id"]
        post = lambda **b: self.client.post("/api/rate", json=b)
        self.assertEqual(post(id=mid, user="Wife", score=5, star=True).status_code, 200)
        self.assertEqual(post(id=mid, user="Husband", score=3).status_code, 200)
        items, _ = self.items()
        self.assertEqual(items["portrait.jpg"]["r"], {"Wife": [5, 1], "Husband": [3, 0]})
        self.assertEqual(post(id=mid, user="Husband", score=None).status_code, 200)
        items, _ = self.items()
        self.assertEqual(items["portrait.jpg"]["r"], {"Wife": [5, 1]})
        self.assertEqual(post(id=mid, user="Stranger", score=3).status_code, 400)
        self.assertEqual(post(id=mid, user="Wife", score=6).status_code, 422)
        self.assertEqual(post(id=99999, user="Wife", score=3).status_code, 404)

    def test_quality_and_bursts(self):
        items, _ = self.items()
        sharp, blurry, flat = items["sharp.jpg"], items["blurry.jpg"], items["portrait.jpg"]
        self.assertGreater(sharp["sh"], blurry["sh"] * 5)
        self.assertEqual(sharp["q"], "")
        self.assertIn("b", blurry["q"])
        self.assertIn("f", flat["q"])   # a single flat colour: no detail at all
        b1, b2 = items["burst1.jpg"], items["burst2.jpg"]
        self.assertIsNotNone(b1.get("b"))
        self.assertEqual(b1["b"], b2.get("b"))
        # The sharp picture and its blurred copy are the same scene shot at the same moment:
        # that is a burst too, and the sharpness score tells the two apart.
        self.assertEqual(sharp.get("b"), blurry.get("b"))
        self.assertIsNone(flat.get("b"))

        r = self.client.post("/api/rate-bulk", json={"user": "Husband", "ids": [b1["id"], b2["id"]], "score": 1})
        self.assertEqual(r.json()["rated"], 2)
        r = self.client.post("/api/rate-bulk", json={"user": "Husband", "ids": [b1["id"], b2["id"]], "score": 1})
        self.assertEqual(r.json()["rated"], 0)
        self.assertEqual(self.client.post("/api/rate-bulk", json={"user": "X", "ids": [1], "score": 1}).status_code, 400)

    def test_static_config_and_export_guards(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Who's sorting?", r.text)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        cfg = self.client.get("/api/config").json()
        self.assertEqual(cfg["users"], ["Husband", "Wife"])
        self.assertFalse(cfg["immich"])
        self.assertEqual(self.client.post("/api/export", json={"target": "immich", "ids": [1]}).status_code, 400)
        self.assertEqual(len(self.client.get("/api/errors").json()), 1)

    def test_folder_export(self):
        from app import exporter

        items, _ = self.items()
        mid = items["logo.png"]["id"]
        config.EXPORT_DIR = os.path.join(self.tmp, "export")
        try:
            self.assertEqual(self.client.post("/api/export", json={"target": "folder", "ids": [mid]}).status_code, 200)
            for _ in range(100):
                if not exporter.job["running"]:
                    break
                import time

                time.sleep(0.05)
            self.assertEqual(exporter.job["errors"], [])
            self.assertTrue(os.path.isfile(os.path.join(config.EXPORT_DIR, "Paul", "logo.png")))
            items, _ = self.items()
            self.assertEqual(items["logo.png"]["x"], 1)
        finally:
            config.EXPORT_DIR = ""


if __name__ == "__main__":
    unittest.main()
