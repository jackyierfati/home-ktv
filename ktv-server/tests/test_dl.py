import os, shutil, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server import igeba_dl, igeba_source
from tests.helpers import temp_dirs

BLOB = bytes(range(256)) * 4096          # 1 MiB


class RangeServer:
    """按 Range 返回 BLOB；stall 里的起点第一次请求时只给一点数据就卡住。"""

    def __init__(self, stall=(), exclusive=False):
        self.stall, self.stalled, self.exclusive = set(stall), set(), exclusive
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                start, end = map(int, self.headers["Range"][6:].split("-"))
                end = min(end - 1 if fake.exclusive else end, len(BLOB) - 1)   # 像 i歌霸那样不包含右端点
                body = BLOB[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(BLOB)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if start in fake.stall and start not in fake.stalled:
                    fake.stalled.add(start)
                    self.wfile.write(body[:100]); self.wfile.flush()
                    time.sleep(30)                      # 卡住的连接
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/song.mkv"

    def close(self):
        self.server.shutdown(); self.server.server_close()


class FakeSource:
    """地址固定是假 CDN 的；分段下载走不读代理的 opener。"""

    def __init__(self, url):
        self.url, self.opener = url, urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def song_url(self, number, **kw):
        return self.url, self.opener


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.root, _, self.media = temp_dirs()
        self.old_chunk = igeba_dl.CHUNK
        igeba_dl.CHUNK = 64 * 1024

    def tearDown(self):
        igeba_dl.CHUNK = self.old_chunk
        shutil.rmtree(self.root, ignore_errors=True)

    def download(self, server, **kw):
        return igeba_dl.download_song("1", "歌", "歌手", "1.mkv", self.media, 8, source=FakeSource(server.url), **kw)

    def test_a_stalled_chunk_is_raced_instead_of_waiting_for_the_timeout(self):
        server = RangeServer(stall={5 * 64 * 1024})
        progress = []
        try:
            began = time.time()
            self.assertTrue(self.download(server, on_progress=lambda d, t, s: progress.append(d)))
            took = time.time() - began
        finally:
            server.close()
        with open(os.path.join(self.media, "1.mkv"), "rb") as f: self.assertEqual(BLOB, f.read())
        self.assertLess(took, 15)                       # 不用等 20 秒超时
        self.assertTrue(progress and max(progress) <= len(BLOB))
        self.assertFalse(os.path.exists(os.path.join(self.media, "1.mkv.part")))

    def test_cancel_removes_the_partial_file(self):
        server = RangeServer(stall={3 * 64 * 1024})
        stop = threading.Event()
        threading.Timer(1, stop.set).start()
        try:
            began = time.time()
            with self.assertRaises(igeba_dl.Cancelled):
                self.download(server, should_stop=stop.is_set)
            self.assertLess(time.time() - began, 5)         # 不等卡住的连接超时
        finally:
            server.close()
        self.assertFalse(os.path.exists(os.path.join(self.media, "1.mkv.part")))
        self.assertFalse(os.path.exists(os.path.join(self.media, "1.mkv")))


class ExclusiveRangeTest(DownloadTest):
    """i歌霸把 Range 右端点当成不包含：要 10 字节只给 9 字节。"""

    def test_download_is_complete_even_when_the_server_drops_the_last_byte(self):
        server = RangeServer(exclusive=True)
        try:
            self.assertTrue(self.download(server))
        finally:
            server.close()
        with open(os.path.join(self.media, "1.mkv"), "rb") as f: self.assertEqual(BLOB, f.read())

    def test_repair_restores_the_bytes_old_downloads_lost(self):
        from server import repair
        broken = bytearray(BLOB)
        for offset in repair.boundary_offsets(len(BLOB)): broken[offset] = 0
        path = os.path.join(self.media, "1.mkv")
        with open(path, "wb") as f: f.write(broken)
        server = RangeServer(exclusive=True)
        try:
            checked, changed = repair.repair_file(path, "1", FakeSource(server.url), threads=4)
        finally:
            server.close()
        self.assertEqual(len(repair.boundary_offsets(len(BLOB))), checked)
        self.assertGreater(changed, 0)
        with open(path, "rb") as f: self.assertEqual(BLOB, f.read())

    test_a_stalled_chunk_is_raced_instead_of_waiting_for_the_timeout = None
    test_cancel_removes_the_partial_file = None


class RaceTest(unittest.TestCase):
    """地址接口偶尔几十秒不回：隔一会儿再并发发一个，谁先回来用谁。"""

    def test_a_hanging_address_request_is_raced(self):
        calls = []

        def once():
            calls.append(1)
            if len(calls) == 1: time.sleep(5); return "http://slow"
            return "http://fast"

        began = time.time()
        self.assertEqual("http://fast", igeba_source.race(once, stagger=0.2, timeout=5))
        self.assertLess(time.time() - began, 2)

    def test_gives_up_after_every_attempt_fails(self):
        def once(): raise RuntimeError("歌源拒绝")

        with self.assertRaisesRegex(igeba_source.SourceError, "4 次.*歌源拒绝"):
            igeba_source.race(once, stagger=0.05, timeout=1)

    def test_403_stops_right_away(self):
        def once(): raise urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)

        began = time.time()
        with self.assertRaises(igeba_source.Forbidden):
            igeba_source.race(once, stagger=1, timeout=5)
        self.assertLess(time.time() - began, 0.5)


if __name__ == "__main__":
    unittest.main()
