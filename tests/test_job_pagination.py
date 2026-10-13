import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask

from transcription.db import JobStore
from transcription.routes import web
from transcription.settings import Settings
from transcription.urls import url_for


class JobPaginationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name) / "jobs.sqlite")
        self.store.initialize()
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__, template_folder=str(root / "transcription/templates"))
        app.config.update(TESTING=True, SECRET_KEY="test", SETTINGS=Settings.load(root / "config.yml"))
        app.extensions["job_store"] = self.store
        app.register_blueprint(web)
        app.jinja_env.globals["url_for"] = url_for
        self.client = app.test_client()

    def seed_jobs(self):
        # Insert out of chronological order, including equal timestamps.
        for index in reversed(range(23)):
            with patch("transcription.db.utcnow", return_value=f"2026-10-{index // 2 + 1:02d}T00:00:00+00:00"):
                self.store.create(f"job-{index:02d}", f"Meeting {index}", "text", [], {})

    def test_pages_are_descending_without_overlap(self):
        self.seed_jobs()
        ids = []
        for page, count in ((1, 10), (2, 10), (3, 3)):
            response = self.client.get(f"/api/jobs?page={page}")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual((payload["page"], payload["pages"], payload["total"]), (page, 3, 23))
            self.assertEqual(len(payload["jobs"]), count)
            ids.extend(job["id"] for job in payload["jobs"])
        self.assertEqual(ids, [f"job-{index:02d}" for index in reversed(range(23))])
        self.assertEqual(len(self.client.get("/api/jobs").get_json()), 23)

    def test_empty_invalid_and_out_of_range_pages(self):
        self.assertEqual(self.client.get("/api/jobs?page=8").get_json(),
                         {"jobs": [], "total": 0, "page": 1, "pages": 1})
        self.assertIn("Page 1 of 1", self.client.get("/").text)
        self.seed_jobs()
        for value, expected in (("0", 1), ("-2", 1), ("bad", 1), ("999999999999999999999", 3)):
            with self.subTest(value=value):
                self.assertEqual(self.client.get(f"/api/jobs?page={value}").get_json()["page"], expected)
        for job in self.store.page(3)["jobs"]:
            self.store.delete(job["id"])
        self.assertEqual(self.client.get("/api/jobs?page=3").get_json()["page"], 2)

    def test_html_page_contains_only_requested_jobs_and_navigation(self):
        self.seed_jobs()
        response = self.client.get("/?page=2")
        self.assertEqual(response.status_code, 200)
        ids = re.findall(r'class="job-row" href="\./jobs/(job-\d+)"', response.text)
        self.assertEqual(ids, [f"job-{index:02d}" for index in range(12, 2, -1)])
        self.assertIn("23 total", response.text)
        self.assertIn("Page 2 of 3", response.text)
        self.assertIn('href="./?page=1"', response.text)
        self.assertIn('href="./?page=3"', response.text)


if __name__ == "__main__":
    unittest.main()
