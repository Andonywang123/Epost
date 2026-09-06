"""Only test local control-layer mapping; no browser, translation or API calls."""
import json
from pathlib import Path
import tempfile
import unittest
from youtube_browser import prepare


class BrowserBridgeTest(unittest.TestCase):
    def test_reuses_original_prepared_fields_without_calling_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            video = root / "fake.mp4"
            video.write_bytes(b"local fake fixture only")
            manifest = root / "post.json"
            data = {"job_id": "fake", "account_profile": "main", "mode": "publish",
                    "video": str(video), "title": "Film memories", "description": "Original text",
                    "tags": ["film", "memories"], "privacy": "private", "publish_at": None,
                    "made_for_kids": False, "contains_synthetic_media": False,
                    "notify_subscribers": False, "auto_tags": True, "tag_region": "US"}
            manifest.write_text(json.dumps(data))
            result = prepare(manifest)
            self.assertEqual(result["title"], data["title"])
            self.assertEqual(result["description"], data["description"])
            self.assertEqual(result["assets"]["video"]["path"], str(video))
            self.assertEqual(result["tags"], data["tags"])
            self.assertTrue(result["auto_tags"])
            self.assertEqual(result["tag_region"], "US")


if __name__ == "__main__":
    unittest.main()
