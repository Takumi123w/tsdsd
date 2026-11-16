"""Tests run against a local HTTP server, so they need no internet access."""

import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

# Keep requests to 127.0.0.1 away from any configured HTTP proxy.
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tsdm.downloader import DownloadTask, plan_segments, probe, unique_filename  # noqa: E402
from tsdm.manager import DownloadManager  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        data = srv.payload
        status, start, end = 200, 0, len(data) - 1
        range_header = self.headers.get("Range")
        if srv.supports_ranges and range_header and range_header.startswith("bytes="):
            first, _, last = range_header[6:].partition("-")
            start = int(first)
            end = min(int(last) if last else len(data) - 1, len(data) - 1)
            status = 206
        body = data[start:end + 1]

        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        if srv.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if srv.disposition:
            self.send_header("Content-Disposition", srv.disposition)
        self.end_headers()

        # Simulate one dropped connection to exercise the resume logic.
        if srv.cut_after and not srv.cut_done and len(body) > srv.cut_after:
            srv.cut_done = True
            self.wfile.write(body[:srv.cut_after])
            return
        for i in range(0, len(body), 16384):
            self.wfile.write(body[i:i + 16384])
            if srv.throttle:
                time.sleep(srv.throttle)


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the probe and simulated drops close sockets early; that is expected


class _Server:
    def __init__(self, payload, *, ranges=True, disposition=None, cut_after=0, throttle=0.0):
        self.httpd = _QuietServer(("127.0.0.1", 0), _Handler)
        self.httpd.payload = payload
        self.httpd.supports_ranges = ranges
        self.httpd.disposition = disposition
        self.httpd.cut_after = cut_after
        self.httpd.cut_done = False
        self.httpd.throttle = throttle
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}/files/sample.bin"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tsdm-test-")
        self.payload = os.urandom(5 * 1024 * 1024 + 123)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, *args, **kwargs):
        server = _Server(*args, **kwargs)
        self.servers.append(server)
        return server

    def read_result(self, task):
        with open(task.path, "rb") as fh:
            return fh.read()

    def test_segmented_download_matches_original(self):
        server = self.serve(self.payload)
        task = DownloadTask(server.url, self.tmp, 4)
        task.start()
        task.join(60)
        self.assertEqual(task.state, "completed", task.error)
        self.assertGreater(len(task.segments), 1)
        self.assertEqual(self.read_result(task), self.payload)

    def test_server_without_range_support(self):
        payload = os.urandom(300 * 1024)
        server = self.serve(payload, ranges=False)
        task = DownloadTask(server.url, self.tmp, 8)
        task.start()
        task.join(60)
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(len(task.segments), 1)
        self.assertEqual(self.read_result(task), payload)

    def test_resumes_after_dropped_connection(self):
        server = self.serve(self.payload, cut_after=1_000_000)
        task = DownloadTask(server.url, self.tmp, 4)
        task.start()
        task.join(60)
        self.assertTrue(server.httpd.cut_done, "the simulated drop never happened")
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(self.read_result(task), self.payload)

    def test_pause_then_resume(self):
        server = self.serve(self.payload, throttle=0.005)
        task = DownloadTask(server.url, self.tmp, 4)
        task.start()
        deadline = time.monotonic() + 30
        while task.downloaded() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        task.pause()
        task.join(30)
        self.assertEqual(task.state, "paused")
        self.assertLess(task.downloaded(), len(self.payload))

        task.start()
        task.join(60)
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(self.read_result(task), self.payload)

    def test_filename_from_content_disposition(self):
        server = self.serve(b"abc", disposition='attachment; filename="my report.pdf"')
        self.assertEqual(probe(server.url).filename, "my report.pdf")

    def test_duplicate_names_are_not_overwritten(self):
        open(os.path.join(self.tmp, "sample.bin"), "wb").close()
        self.assertEqual(unique_filename(self.tmp, "sample.bin"), "sample (1).bin")

    def test_segments_cover_the_whole_file(self):
        for size in (1, 1024 * 1024, 5 * 1024 * 1024 + 7):
            segments = plan_segments(size, True, 8)
            self.assertEqual(segments[0]["start"], 0)
            self.assertEqual(segments[-1]["end"], size - 1)
            covered = sum(s["end"] - s["start"] + 1 for s in segments)
            self.assertEqual(covered, size)

    def test_download_list_survives_restart(self):
        data_file = os.path.join(self.tmp, "downloads.json")
        manager = DownloadManager(data_file)
        task = manager.add("http://127.0.0.1:1/x.bin", self.tmp, 4, start=False)
        reloaded = DownloadManager(data_file)
        self.assertIsNotNone(reloaded.get(task.id))
        self.assertIsNotNone(reloaded.find_resumable("http://127.0.0.1:1/x.bin", self.tmp))


if __name__ == "__main__":
    unittest.main()
