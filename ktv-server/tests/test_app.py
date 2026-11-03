import json, os, shutil, threading, time, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

from server.app import Ktv, make_handler
from tests.helpers import Cancelled, FakeDownloader, FakeMaidong, make_catalog, temp_dirs

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AppBase(unittest.TestCase):
    """启动服务端、假的下载器和假的麦动。"""

    def setUp(self):
        self.root, self.data, self.media = temp_dirs()
        make_catalog(os.path.join(self.data, "igeba_catalog.db"))
        self.maidong = FakeMaidong()
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump({"player_url": self.maidong.url, "player_mode": "maidong"}, f)
        self.download = FakeDownloader()
        self.download.gate.clear()                   # 下载先卡住，方便看“下载中”
        self.ktv = Ktv(self.data, self.media, parallel=1, download=self.download, cancelled=Cancelled, retry_delay=0)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.ktv))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.download.gate.set()
        self.server.shutdown(); self.server.server_close()
        self.maidong.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=5) as r: return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait_for(self, check, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if check(): return
            time.sleep(0.02)
        self.fail("等待超时")


class AppTest(AppBase):
    """第一阶段：点歌转给麦动。"""

    def test_order_downloads_first_and_forwards_to_the_tv(self):
        code, body = self.call("POST", "/api/room/queue", {"number": "20019055"})
        self.assertEqual(201, code)
        self.assertIn(("POST", "/api/v1/queue", {"songId": "igeba:song:20019055"}), self.maidong.requests)
        job = self.ktv.jobs.progress_of("20019055")
        self.assertTrue(job["priority"])
        self.wait_for(lambda: self.ktv.jobs.progress_of("20019055")["state"] == "downloading")
        code, room = self.call("GET", "/api/room")
        self.assertTrue(room["player"]["connected"])
        item = room["queue"][0]
        self.assertEqual(("20019055", "downloading"), (item["number"], item["status"]))
        self.assertTrue(item["current"])
        self.assertEqual(["20019055"], [d["number"] for d in room["downloads"]])
        self.download.gate.set()
        self.wait_for(lambda: self.call("GET", "/api/room")[1]["queue"][0]["status"] == "ready")

    def test_ordering_twice_reports_already_queued(self):
        self.call("POST", "/api/room/queue", {"number": "20019055"})
        code, body = self.call("POST", "/api/room/queue", {"number": "20019055"})
        self.assertEqual(409, code)
        self.assertIn("已点", body["error"])

    def test_tv_offline_still_starts_the_download(self):
        self.maidong.close()
        code, body = self.call("POST", "/api/room/queue", {"number": "10006055"})
        self.assertEqual(503, code)
        self.assertIn("NAS 下载", body["error"])
        self.assertIsNotNone(self.ktv.jobs.progress_of("10006055"))
        code, room = self.call("GET", "/api/room")
        self.assertFalse(room["player"]["connected"])

    def test_browse_marks_songs_that_are_ready(self):
        with open(os.path.join(self.media, "220019055.mkv"), "wb") as f: f.write(b"x")
        code, body = self.call("GET", "/api/songs?q=%E4%B8%80%E5%8D%8A")
        self.assertEqual(200, code)
        self.assertEqual([("一半", True), ("一人一半", False)], [(s["name"], s["ready"]) for s in body["songs"]])
        code, body = self.call("GET", "/api/singers/885/songs")
        self.assertEqual("薛之谦", body["singer"]["name"])
        self.assertEqual(2, body["total"])
        code, body = self.call("GET", "/api/languages/%E7%B2%A4%E8%AF%AD/songs")
        self.assertEqual(["天命最高"], [s["name"] for s in body["songs"]])
        code, body = self.call("GET", "/api/charts")
        self.assertEqual(["热歌榜（2026第43期）"], [c["name"] for c in body["charts"]])

    def test_remote_controls_are_forwarded(self):
        self.assertEqual(200, self.call("POST", "/api/room/player", {"action": "next"})[0])
        self.assertEqual(200, self.call("PATCH", "/api/room/audio", {"vocalMode": "original"})[0])
        self.assertEqual(200, self.call("DELETE", "/api/room/queue/igeba%3Asong%3A1")[0])
        self.assertEqual(400, self.call("POST", "/api/room/player", {"action": "explode"})[0])
        self.assertIn(("POST", "/api/v1/player/actions", {"action": "next"}), self.maidong.requests)
        self.assertIn(("PATCH", "/api/v1/player/audio", {"vocalMode": "original"}), self.maidong.requests)
        self.assertIn(("DELETE", "/api/v1/queue/igeba%3Asong%3A1", None), self.maidong.requests)

    def test_maidong_compatible_download_api(self):
        code, job = self.call("POST", "/download", {"number": "20012901"})
        self.assertEqual(202, code)
        self.assertTrue(job["priority"])
        code, status = self.call("GET", "/status/20012901")
        self.assertIn(status["state"], ("queued", "downloading"))
        self.download.gate.set()
        self.wait_for(lambda: self.call("GET", "/status/20012901")[1]["state"] == "done")

    def test_pages_and_settings(self):
        with _opener.open(self.base + "/", timeout=5) as r: self.assertIn("家庭KTV".encode(), r.read())
        with _opener.open(self.base + "/admin", timeout=5) as r: self.assertIn("管理".encode(), r.read())
        self.assertEqual(404, self.call("GET", "/static/../app.py")[0])
        code, s = self.call("PUT", "/admin/settings", {"player_url": "http://192.168.1.50:8765/"})
        self.assertEqual("http://192.168.1.50:8765", s["player_url"])
        self.assertEqual(400, self.call("PUT", "/admin/settings", {"player_url": "javascript:alert(1)"})[0])
        self.assertEqual("家庭KTV", self.call("GET", "/api/room")[1]["name"])                 # 名字默认是“家庭KTV”
        self.assertEqual("Jack 家庭KTV", self.call("PUT", "/admin/settings", {"room_name": " Jack 家庭KTV "})[1]["room_name"])
        self.assertEqual("Jack 家庭KTV", self.call("GET", "/api/room")[1]["name"])
        self.assertEqual("Jack 家庭KTV", self.call("GET", "/api/tv/state")[1]["name"])
        self.assertEqual(400, self.call("PUT", "/admin/settings", {"room_name": ""})[0])


class TvModeTest(AppBase):
    """第二阶段：服务端管已点，电视播放器取状态、拿歌曲文件、汇报播放完。"""

    def setUp(self):
        super().setUp()
        self.ktv.settings.update({"player_mode": "tv"})

    def test_order_download_play_and_move_on(self):
        self.assertEqual(201, self.call("POST", "/api/room/queue", {"number": "20019055"})[0])
        self.assertEqual(409, self.call("POST", "/api/room/queue", {"number": "20019055"})[0])
        self.assertFalse(any(r[0] == "POST" for r in self.maidong.requests))      # 不再转给麦动
        code, room = self.call("GET", "/api/room")
        self.assertFalse(room["player"]["connected"])
        self.assertEqual("downloading", room["queue"][0]["status"])
        code, state = self.call("GET", "/api/player/state")
        self.assertIsNone(state["current"])
        self.assertEqual({"catalog": 6, "downloaded": 0}, state["stats"])
        self.download.gate.set()                                                   # 下完自动开始放
        self.wait_for(lambda: self.call("GET", "/api/player/state")[1]["current"] is not None)
        code, state = self.call("GET", "/api/player/state")
        self.assertEqual("/media/20019055", state["current"]["media"])
        code, room = self.call("GET", "/api/room")
        self.assertTrue(room["player"]["connected"])
        self.assertEqual((True, "ready"), (room["queue"][0]["current"], room["queue"][0]["status"]))
        self.assertIsNone(room["player"]["micVolume"])
        self.call("POST", "/api/player/report", {"entryId": state["current"]["id"], "state": "ended"})
        self.assertIsNone(self.call("GET", "/api/player/state")[1]["current"])

    def test_remote_controls_change_the_player_state(self):
        with open(os.path.join(self.media, "220019055.mkv"), "wb") as f: f.write(b"x")
        self.call("POST", "/api/room/queue", {"number": "20019055"})
        version = self.call("GET", "/api/player/state")[1]["version"]
        self.assertEqual(200, self.call("POST", "/api/room/player", {"action": "pause"})[0])
        self.assertEqual(200, self.call("PATCH", "/api/room/audio", {"vocalMode": "original", "musicVolume": 40})[0])
        code, state = self.call("GET", f"/api/player/state?since={version}&wait=2")
        self.assertEqual((False, "original", 40), (state["playing"], state["vocal"], state["volume"]))
        self.assertEqual(200, self.call("POST", "/api/player/action", {"action": "next"})[0])
        self.assertIsNone(self.call("GET", "/api/player/state")[1]["current"])

    def test_tv_page_state_has_everything_the_home_screen_shows(self):
        self.ktv.settings.update({"wifi_ssid": 'Jack;家"', "wifi_password": "p:w,1", "wifi_security": "WPA"})
        self.assertEqual(201, self.call("POST", "/api/room/queue", {"number": "20019055", "by": "小王"})[0])
        code, s = self.call("GET", "/api/tv/state")
        self.assertEqual(200, code)
        self.assertEqual("WIFI:T:WPA;S:Jack\\;家\\\";P:p\\:w\\,1;;", s["wifi"]["qr"])
        self.assertEqual(("一半", "小王", "downloading"), (s["queue"][0]["name"], s["queue"][0]["by"], s["queue"][0]["status"]))
        self.assertEqual("小王 点了《一半》", s["feed"][-1]["text"])
        self.assertEqual({"requested": 1, "sung": 0}, s["users"]["小王"])
        self.assertTrue(s["nas"]["online"])
        self.assertEqual(6, s["stats"]["catalog"])
        self.assertIsInstance(s["ui"], int)
        code, room = self.call("GET", "/api/room")
        self.assertEqual("小王", room["queue"][0]["by"])
        version = s["version"]
        began = time.time()
        self.call("GET", f"/api/tv/state?since={version}&wait=1")
        self.assertGreaterEqual(time.time() - began, 0.9)                      # 没变化就等
        with _opener.open(self.base + "/tv", timeout=5) as r: self.assertIn("家庭KTV".encode(), r.read())
        self.assertEqual(400, self.call("PUT", "/admin/settings", {"wifi_security": "WPA9"})[0])
        self.ktv.settings.update({"wifi_ssid": ""})
        self.assertIsNone(self.call("GET", "/api/tv/state")[1]["wifi"])

    def test_order_tells_the_phone_its_place_in_line(self):
        with open(os.path.join(self.media, "220019055.mkv"), "wb") as f: f.write(b"x")
        code, first = self.call("POST", "/api/room/queue", {"number": "20019055", "by": "小王"})
        self.assertEqual((201, True, 0, "ready"), (code, first["now"], first["ahead"], first["status"]))
        code, second = self.call("POST", "/api/room/queue", {"number": "10006055"})
        self.assertEqual((False, 1), (second["now"], second["ahead"]))             # 前面只有正在唱的那首
        self.assertEqual(2, self.call("POST", "/api/room/queue", {"number": "20012901"})[1]["ahead"])
        code, room = self.call("GET", "/api/room")
        self.assertEqual([first["id"], second["id"]], [i["id"] for i in room["queue"][:2]])
        self.assertEqual([240, 240, 240], [i["duration"] for i in room["queue"]])  # 手机用来估等多久
        code, hits = self.call("GET", "/api/singers?q=xzq&exact=1")
        self.assertEqual(["薛之谦"], [s["name"] for s in hits["singers"]])
        self.assertEqual([], self.call("GET", "/api/singers?q=x&exact=1")[1]["singers"])

    def test_redownload_waits_for_the_new_file_and_never_swaps_the_song_on_tv(self):
        for fn in ("220019055.mkv", "210006055.mkv"):
            with open(os.path.join(self.media, fn), "wb") as f: f.write(b"old")
        self.call("POST", "/api/room/queue", {"number": "20019055"})                     # 马上开始放
        self.assertEqual(409, self.call("POST", "/admin/redownload", {"number": "20019055"})[0])
        self.assertEqual(202, self.call("POST", "/admin/redownload", {"number": "10006055"})[0])
        self.call("POST", "/api/room/queue", {"number": "10006055"})
        self.call("POST", "/api/player/action", {"action": "next"})
        code, state = self.call("GET", "/api/player/state")
        self.assertIsNone(state["current"])                                               # 不拿旧文件放
        self.assertEqual("downloading", self.call("GET", "/api/room")[1]["queue"][0]["status"])
        self.download.gate.set()                                                          # 新文件下好就放
        self.wait_for(lambda: (self.call("GET", "/api/player/state")[1]["current"] or {}).get("number") == "10006055")
        with open(os.path.join(self.media, "210006055.mkv"), "rb") as f: self.assertEqual(b"new mkv", f.read())

    def test_media_supports_ranges(self):
        with open(os.path.join(self.media, "220019055.mkv"), "wb") as f: f.write(b"0123456789")
        req = urllib.request.Request(self.base + "/media/20019055", headers={"Range": "bytes=2-5"})
        with _opener.open(req, timeout=5) as r:
            self.assertEqual((206, b"2345", "bytes 2-5/10"), (r.status, r.read(), r.headers["Content-Range"]))
        req = urllib.request.Request(self.base + "/media/20019055", headers={"Range": "bytes=-3"})
        with _opener.open(req, timeout=5) as r: self.assertEqual(b"789", r.read())
        with _opener.open(self.base + "/media/20019055", timeout=5) as r: self.assertEqual((200, 10), (r.status, len(r.read())))
        with self.assertRaises(urllib.error.HTTPError) as e:
            _opener.open(urllib.request.Request(self.base + "/media/20019055", headers={"Range": "bytes=20-"}), timeout=5)
        self.assertEqual(416, e.exception.code)
        self.assertEqual(404, self.call("GET", "/media/10006055")[0])


if __name__ == "__main__":
    unittest.main()
