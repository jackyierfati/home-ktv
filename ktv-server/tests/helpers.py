import json, os, sqlite3, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server.catalog import SCHEMA

SONGS = [
    # number, name, singer, language, filename, sort_no
    ("20019055", "一半", "薛之谦", "国语", "220019055.mkv", 1),
    ("20034621", "一人一半", "旺福", "闽南语", "220034621.mkv", 2),
    ("10006055", "江南", "林俊杰", "国语", "210006055.mkv", 3),
    ("20012901", "山丘", "李宗盛", "国语", "220012901.mkv", 4),
    ("30000461", "天命最高", "古天乐", "粤语", "230000461.mkv", 5),
    ("20000001", "合唱", "薛之谦/林俊杰", "国语", "220000001.mkv", 6),
]
SINGERS = [("885", "薛之谦", "大陆", "男", "XZQ"), ("521", "林俊杰", "港台", "男", "LJJ"), ("1", "李宗盛", "港台", "男", "LZS")]


def make_catalog(path):
    db = sqlite3.connect(path)
    for sql in SCHEMA: db.execute(sql)
    pinyin = {"一半": "YB", "一人一半": "YRYB", "江南": "JN", "山丘": "SQ", "天命最高": "TMZG", "合唱": "HC"}
    for number, name, singer, language, filename, order in SONGS:
        db.execute("insert into source_songs values(?,?,?,?,?,'',240,0,?,?,?,?,'')",
                   (number, name, singer, language, filename, pinyin[name], pinyin[name], len(name), order))
    for i, (number, name, region, kind, py) in enumerate(SINGERS):
        db.execute("insert into source_singers values(?,?,?,?,?,?,?,?)", (number, name, region, kind, py, py, "", i))
    for singer, song in (("885", "20019055"), ("521", "10006055"), ("1", "20012901"), ("885", "20000001"), ("521", "20000001")):
        db.execute("insert into source_singer_songs values(?,?)", (singer, song))
    db.execute("insert into source_playlists values('5432',4,'热歌榜（2026第43期）','','',0,2)")
    db.execute("insert into source_playlists values('51890',3,'抖音热门歌曲','','',1,1)")
    for order, song in enumerate(("20012901", "20019055")):
        db.execute("insert into source_playlist_songs values('5432',4,?,?)", (song, order))
    db.execute("insert into source_playlist_songs values('51890',3,'10006055',0)")
    db.execute("insert into meta values('provider','igeba')")
    db.execute("insert into meta values('remote_version','30000461:6')")
    db.commit(); db.close()


class Cancelled(Exception):
    pass


class FakeDownloader:
    """按编号决定结果：默认成功并写出文件；fail 里的编号返回 False；gate 未放行前阻塞。"""

    def __init__(self):
        self.calls, self.fail, self.gate = [], set(), threading.Event()
        self.gate.set()

    def __call__(self, number, name, singer, filename, outdir, on_progress, should_stop, replace=False):
        self.calls.append((number, "replace") if replace else number)
        on_progress(50, 100, 1.0)
        self.gate.wait(5)
        if should_stop(): raise Cancelled()
        if number in self.fail: return False
        with open(os.path.join(outdir, filename), "wb") as f: f.write(b"new mkv" if replace else b"mkv")
        return True


class FakeMaidong:
    """模拟麦动的 /api/v1 局域网接口。"""

    def __init__(self):
        self.queue, self.requests, self.state = [], [], {"playing": True, "vocalMode": "accompany",
                                                         "musicVolume": 60, "micVolume": 50}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def reply(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                if self.path == "/api/v1/state":
                    current = fake.queue[0] if fake.queue else None
                    return self.reply(200, {**fake.state, "current": current, "next": None, "ok": True})
                if self.path == "/api/v1/queue": return self.reply(200, {"queue": fake.queue, "ok": True})
                self.reply(404, {"ok": False, "error": "接口不存在"})

            def do_POST(self):
                data = self.body(); fake.requests.append(("POST", self.path, data))
                if self.path == "/api/v1/queue":
                    if any(s["id"] == data["songId"] for s in fake.queue):
                        return self.reply(409, {"ok": False, "error": "歌曲已在已点列表中"})
                    number = data["songId"].rsplit(":", 1)[-1]
                    fake.queue.append({"id": data["songId"], "number": number, "title": number, "singer": "歌手",
                                       "local": False, "cloud": False})
                    return self.reply(201, {"ok": True})
                self.reply(200, {"ok": True})

            def do_PATCH(self):
                fake.requests.append(("PATCH", self.path, self.body())); self.reply(200, {"ok": True})

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path, None)); self.reply(200, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown(); self.server.server_close()


def temp_dirs():
    root = tempfile.mkdtemp(prefix="ktv-test-")
    data, media = os.path.join(root, "data"), os.path.join(root, "media")
    os.makedirs(data); os.makedirs(media)
    return root, data, media
