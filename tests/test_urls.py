import unittest
from urllib.parse import urljoin

from flask import Flask

from transcription.urls import url_for


class RelativeURLTests(unittest.TestCase):
    def test_urls_resolve_from_nested_pages_and_post_endpoints(self):
        app = Flask(__name__)
        app.add_url_rule("/", endpoint="index")
        app.add_url_rule("/jobs/<job_id>", endpoint="job")
        app.add_url_rule("/realtime/stream", endpoint="stream", websocket=True)
        for path in ("/", "/jobs/one", "/jobs/one/retry", "/nested/"):
            for mount in ("", "/transcription", "/tools/transcription"):
                for script_root in ("", mount):
                    with self.subTest(path=path, mount=mount, script_root=script_root):
                        with app.test_request_context(path, environ_overrides={"SCRIPT_NAME": script_root}):
                            base = "http://localhost" + mount + path
                            self.assertEqual(urljoin(base, url_for("index")), "http://localhost" + mount + "/")
                            self.assertEqual(urljoin(base, url_for("job", job_id="two", tab="logs")),
                                             "http://localhost" + mount + "/jobs/two?tab=logs")
                            self.assertEqual(urljoin(base, url_for("static", filename="app.css")),
                                             "http://localhost" + mount + "/static/app.css")
                            self.assertEqual(urljoin(base, url_for("stream")),
                                             "http://localhost" + mount + "/realtime/stream")
                            self.assertEqual(url_for("job", job_id="two", _external=True),
                                             "http://localhost" + script_root + "/jobs/two")
