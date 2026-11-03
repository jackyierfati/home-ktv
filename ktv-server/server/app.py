"""Home KTV 服务端：点歌网页（/）、管理页（/admin）、NAS 下载，以及兼容麦动的下载接口。

启动：python -m server.app --data /data --media /media [--port 8787]
"""
import argparse, json, os, re, sys, threading, time, traceback, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import catalog as catalog_mod, igeba_api, igeba_dl
from .catalog import Catalog
from .jobs import Jobs, NUM_RE
from .player import MaidongBridge, PlayerError, song_number
from .room import Room, RoomError

VERSION = "3"
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"index.html": "text/html", "app.css": "text/css", "app.js": "application/javascript",
          "admin.html": "text/html", "admin.js": "application/javascript",
          "tv.html": "text/html", "tv.css": "text/css", "tv.js": "application/javascript"}
TV_FILES = ("tv.html", "tv.css", "tv.js")
GUEST_PAGE = 30


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Settings:
    # room_name：电视主页和点歌页上的名字，比如“Jack 家庭KTV”
    # player_mode：tv = 服务端自己管已点，电视装 Home KTV 播放器；maidong = 点歌转给电视上的麦动（player_url）
    # wifi_*：电视主页上“连 WiFi”二维码用的网络名和密码
    DEFAULTS = {"room_name": os.environ.get("KTV_NAME", "家庭KTV"),
                "player_url": os.environ.get("KTV_PLAYER_URL", ""),
                "player_mode": os.environ.get("KTV_PLAYER_MODE", "tv"),
                "wifi_ssid": "", "wifi_password": "", "wifi_security": "WPA"}

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.values = dict(self.DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f: self.values.update(json.load(f))
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.values.get(key)

    def update(self, values):
        with self.lock:
            for key in self.DEFAULTS:
                if key in values: self.values[key] = str(values[key]).strip()
            with open(self.path + ".tmp", "w", encoding="utf-8") as f: json.dump(self.values, f, ensure_ascii=False)
            os.replace(self.path + ".tmp", self.path)
        return dict(self.values)


class Updater:
    """后台重建曲库；同一时间只跑一次。"""

    def __init__(self, ktv, api=igeba_api):
        self.ktv, self.api, self.lock = ktv, api, threading.Lock()
        self.state = {"running": False, "progress": 0, "error": None, "finished": None, "remote": None}

    def start(self):
        with self.lock:
            if self.state["running"]: return False
            self.state.update(running=True, progress=0, error=None)
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            catalog_mod.rebuild(self.ktv.catalog.path, self.api, lambda p: self.state.update(progress=p))
            self.ktv.catalog.reopen()
            self.state.update(finished=time.time())
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"[:300]
            print("曲库更新失败:", self.state["error"])
        finally:
            self.state["running"] = False

    def status(self):
        meta = self.ktv.catalog.meta()
        return {**self.state, "version": meta.get("remote_version"), "songs": self.ktv.catalog.song_count()}

    def check_remote(self):
        try: self.state["remote"] = catalog_mod.remote_version(self.api)
        except Exception as e: self.state["error"] = f"查询远端版本失败: {e}"[:300]
        return self.status()


class Ktv:
    def __init__(self, data_dir, media_dir, threads=64, parallel=2, download=None, cancelled=None,
                 retry_delay=30, api=igeba_api):
        os.makedirs(data_dir, exist_ok=True)
        self.catalog = Catalog(os.path.join(data_dir, "igeba_catalog.db"))
        self.settings = Settings(os.path.join(data_dir, "config.json"))
        self.player = MaidongBridge(lambda: self.settings.get("player_url"))
        if download is None:
            def download(number, name, singer, fn, outdir, on_progress, should_stop, replace=False):
                return igeba_dl.download_song(number, name, singer, fn, outdir, threads,
                                              on_progress=on_progress, should_stop=should_stop, replace=replace)
        self.media_dir = media_dir
        self.tv = Room(os.path.join(data_dir, "room.json"), self.catalog.lookup, self.is_ready)
        self.jobs = Jobs(self.catalog.lookup, media_dir, os.path.join(data_dir, "agent_state.json"),
                         download, cancelled or igeba_dl.Cancelled, parallel, retry_delay, on_done=self.tv.song_ready)
        self.updater = Updater(self, api)

    def tv_mode(self):
        return self.settings.get("player_mode") == "tv"

    def stats(self):
        """电视主页上显示的曲库和 NAS 歌曲数，每分钟统计一次。"""
        now = time.time()
        if now - getattr(self, "_stats_at", 0) > 60:
            self._stats = {"catalog": self.catalog.song_count(),
                           "downloaded": sum(1 for n in self.jobs.downloaded_names() if n.endswith(".mkv"))}
            self._stats_at = now
        return self._stats

    def singer_image(self, singer):
        """歌手头像地址；合唱取第一个歌手。"""
        cache = self.__dict__.setdefault("_images", {})
        name = re.split(r"[/、&]+", singer or "")[0].strip()
        if name not in cache:
            row = self.catalog.one("select image_url from source_singers where name=?", (name,))
            cache[name] = (row[0] or None) if row else None
        return cache[name]

    def nas_status(self):
        """电视主页底部的实时状态：正在下载、速度、最近下好的一首。"""
        with self.jobs.lock:
            jobs = list(self.jobs.jobs.values())
        downloading = [j for j in jobs if j["state"] == "downloading"]
        done = [j for j in jobs if j["state"] == "done" and j.get("finished")]
        latest = max(done, key=lambda j: j["finished"], default=None)
        return {"online": True, "speed": round(sum(j.get("speed_mib") or 0 for j in downloading), 2),
                "downloading": [{"name": j["name"], "singer": j["singer"], "progress": j["progress"]} for j in downloading],
                "latest": latest and {"name": latest["name"], "singer": latest["singer"], "t": latest["finished"]}}

    def wifi(self):
        """WiFi 二维码内容（标准 WIFI:T:WPA;S:名字;P:密码;; 格式），没设置时为 None。"""
        ssid = self.settings.get("wifi_ssid") or ""
        if not ssid: return None
        esc = lambda s: re.sub(r'([\\;,:"])', r"\\\1", s)
        security = self.settings.get("wifi_security") or "WPA"
        password = self.settings.get("wifi_password") or ""
        qr = f"WIFI:T:{security};S:{esc(ssid)};P:{esc(password)};;" if security != "nopass" else f"WIFI:T:nopass;S:{esc(ssid)};;"
        return {"ssid": ssid, "qr": qr}

    def ui_version(self):
        """电视主页文件的修改时间：变了电视就自己刷新页面，改主页不用重装 App。"""
        return int(max(os.path.getmtime(os.path.join(WEB, n)) for n in TV_FILES if os.path.exists(os.path.join(WEB, n))))

    def tv_state(self):
        snap, names = self.tv.snapshot(), self.jobs.downloaded_names()
        song = lambda e: {"id": e["id"], "name": e["name"], "singer": e["singer"], "by": e.get("by", ""),
                          "image": self.singer_image(e["singer"])}
        current = snap["current"]
        return {"version": snap["version"], "connected": self.tv.connected(),
                "current": current and song(current), "playing": snap["playing"], "vocal": snap["vocal"],
                "queue": [{**song(e), **self.download_state(e["number"], {}, names)} for e in snap["queue"]],
                "feed": snap["feed"], "users": snap["users"], "nas": self.nas_status(), "stats": self.stats(),
                "wifi": self.wifi(), "ui": self.ui_version(), "name": self.settings.get("room_name")}

    def media_path(self, number):
        row = self.catalog.lookup(number)
        return os.path.join(self.media_dir, row[3]) if row else None

    def is_ready(self, number):
        """歌曲文件在 NAS 上可以放；正在重新下载的不算（旧文件是坏的，新的还没换上）。"""
        path = self.media_path(number)
        return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0 and not self.jobs.replacing(number)

    # ---- 点歌网页用的歌曲格式
    def songs_json(self, rows):
        names = self.jobs.downloaded_names()
        with self.jobs.lock:
            return [{"number": r[0], "name": r[1], "singer": r[2], "language": r[3], "duration": r[5],
                     "ready": r[4] in names, "state": self.jobs.song_state(r[0], r[4], names)} for r in rows]

    def page(self, fetch, page, size=GUEST_PAGE, **extra):
        rows = fetch((page - 1) * size, size + 1)
        return {"songs": self.songs_json(rows[:size]), "page": page, "hasMore": len(rows) > size, **extra}

    # ---- 已点和下载状态
    def download_state(self, number, item, names):
        if item.get("local") or item.get("cloud") or not number: return {"status": "ready"}
        row = self.catalog.lookup(number)
        if row and row[3] in names and not self.jobs.replacing(number): return {"status": "ready"}
        j = self.jobs.progress_of(number)
        if not j: return {"status": "waiting"}
        if j["state"] == "done": return {"status": "ready"}
        if j["state"] == "downloading":
            return {"status": "downloading", "progress": j["progress"], "speed": j["speed_mib"]}
        if j["state"] == "queued": return {"status": "queued"}
        return {"status": "failed", "error": j.get("error")}

    def room(self):
        return self.room_tv() if self.tv_mode() else self.room_maidong()

    def room_tv(self):
        snap, names = self.tv.snapshot(), self.jobs.downloaded_names()
        connected = self.tv.connected()
        current = snap["current"]
        player = {"connected": connected, "error": None if connected else "电视播放器没有连接：请在电视上打开「家庭KTV」",
                  "current": current and {"id": current["id"], "name": current["name"], "singer": current["singer"]},
                  "next": snap["queue"][0] if snap["queue"] else None, "playing": snap["playing"] and bool(current),
                  "vocalMode": snap["vocal"], "musicVolume": snap["volume"], "micVolume": None}
        items = [{**current, "current": True, "status": "ready"}] if current else []
        items += [{**e, "current": False, **self.download_state(e["number"], {}, names)} for e in snap["queue"]]
        # 歌长：手机上“我点的”用它估计还要等多久（正在放的那首还带着播到第几秒 position）
        durations = {r[0]: r[5] for r in self.catalog.songs_by_numbers([i["number"] for i in items])}
        for item in items:
            item.pop("media", None)
            item["duration"] = durations.get(item["number"], 0)
        return {"version": snap["version"], "player": player, "queue": items, "downloads": []}

    def room_maidong(self):
        player = {"connected": False, "error": None}
        queue, current_id = [], None
        try:
            state = self.player.state()
            queue = self.player.queue()
            current = state.get("current") or None
            current_id = current and current.get("id")
            brief = lambda s: s and {"id": s.get("id"), "name": s.get("title"), "singer": s.get("singer")}
            player = {"connected": True, "error": None, "current": brief(current), "next": brief(state.get("next")),
                      "playing": bool(state.get("playing")), "vocalMode": state.get("vocalMode"),
                      "musicVolume": state.get("musicVolume"), "micVolume": state.get("micVolume")}
        except PlayerError as e:
            player["error"] = str(e)
        names = self.jobs.downloaded_names()
        items = []
        for item in queue:
            number = song_number(item)
            items.append({"id": item.get("id"), "number": number, "name": item.get("title"),
                          "singer": item.get("singer"), "current": item.get("id") == current_id,
                          **self.download_state(number, item, names)})
        downloads = [{"number": j["number"], "name": j["name"], "singer": j["singer"], "state": j["state"],
                      "progress": j["progress"], "speed": j["speed_mib"]} for j in self.jobs.active()]
        return {"player": player, "queue": items, "downloads": downloads}

    def order(self, number, by=""):
        row = self.catalog.lookup(number)
        if not row: raise ApiError(404, "曲库里没有这首歌")
        if self.tv_mode():
            entry = self.tv.add(number, by)               # 已点过会抛 409
            # 前面还有几首（正在放的也算一首）；手机记下 id，“我点的”就靠它认出自己点的歌
            snap = self.tv.snapshot()
            now = bool(snap["current"]) and snap["current"]["id"] == entry["id"]
            ids = [e["id"] for e in snap["queue"]]
            ahead = 0 if now else (ids.index(entry["id"]) if entry["id"] in ids else len(ids)) + (1 if snap["current"] else 0)
            mine = {"ok": True, "id": entry["id"], "ahead": ahead, "now": now}
            if self.is_ready(number): return {**mine, "status": "ready"}
            _, job = self.jobs.submit(number, filename=row[3], priority=True)
            return {**mine, "status": job.get("state")}
        _, job = self.jobs.submit(number, filename=row[3], priority=True)
        try:
            self.player.order(number)
        except PlayerError as e:
            if e.status == 409: raise ApiError(409, str(e))
            raise ApiError(503, f"{e}。歌已经开始在 NAS 下载，电视连上后再点一次就行")
        return {"ok": True, "status": "ready" if job.get("state") == "done" else job.get("state")}


# ---------------- 路由 ----------------
ROUTES = []


def route(method, pattern):
    def wrap(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn)); return fn
    return wrap


def arg(qs, key, default=""):
    return (qs.get(key) or [default])[0].strip()


def page_arg(qs):
    try: return max(1, int(arg(qs, "page", "1") or 1))
    except ValueError: return 1


def numbers_arg(data):
    numbers = data.get("numbers") or []
    if not isinstance(numbers, list): raise ApiError(400, "numbers 要是数组")
    return [str(n) for n in numbers]


# ---- 点歌网页
@route("GET", "/api/songs")
def api_songs(k, m, qs, data):
    q = arg(qs, "q")
    if not q: raise ApiError(400, "请输入歌名或歌手")
    fetch = k.catalog.search_by_singer if arg(qs, "mode") == "singer" else k.catalog.search_songs
    return k.page(lambda off, lim: fetch(q, off, lim, cached=k.jobs.downloaded_names()), page_arg(qs))


@route("GET", "/api/hot")
def api_hot(k, m, qs, data):
    return k.page(k.catalog.hot, page_arg(qs))


@route("GET", "/api/singers")
def api_singers(k, m, qs, data):
    page, size = page_arg(qs), 48
    if arg(qs, "exact"):                                   # 按歌名搜时，名字或拼音完全对上的歌手
        rows = k.catalog.singers_named(arg(qs, "q")) if arg(qs, "q") else []
        total = len(rows)
    else:
        rows, total = k.catalog.singers(arg(qs, "q"), arg(qs, "region"), arg(qs, "type"), (page - 1) * size, size + 1)
    return {"singers": [{"number": r[0], "name": r[1], "region": r[2], "type": r[3], "image": r[4] or None}
                        for r in rows[:size]], "total": total, "page": page, "hasMore": len(rows) > size}


@route("GET", r"/api/singers/(\d{1,16})/songs")
def api_singer_songs(k, m, qs, data):
    singer = k.catalog.singer(m[1])
    if not singer: raise ApiError(404, "没有这个歌手")
    info = {"number": singer[0], "name": singer[1], "region": singer[2], "type": singer[3], "image": singer[4] or None}
    return k.page(lambda off, lim: k.catalog.singer_songs(m[1], off, lim, cached=k.jobs.downloaded_names()), page_arg(qs),
                  singer=info, total=k.catalog.singer_song_count(m[1]))


@route("GET", "/api/charts")
def api_charts(k, m, qs, data):
    lists = [{"number": r[0], "type": r[1], "name": r[2], "count": r[3]} for r in k.catalog.playlists()]
    return {"charts": [x for x in lists if x["type"] == 4], "playlists": [x for x in lists if x["type"] == 3]}


@route("GET", r"/api/charts/(\d{1,16})/songs")
def api_chart_songs(k, m, qs, data):
    kind = 3 if arg(qs, "type") == "3" else 4
    return k.page(lambda off, lim: k.catalog.playlist_songs(m[1], kind, off, lim), page_arg(qs))


@route("GET", "/api/languages")
def api_languages(k, m, qs, data):
    return {"languages": [{"name": r[0], "count": r[1]} for r in k.catalog.languages()]}


@route("GET", r"/api/languages/([^/]+)/songs")
def api_language_songs(k, m, qs, data):
    return k.page(lambda off, lim: k.catalog.language_songs(m[1], off, lim, cached=k.jobs.downloaded_names()), page_arg(qs))


def since_wait(qs):
    """长轮询参数：since=上次拿到的 version，wait=状态没变时最多等几秒。"""
    try: since = int(arg(qs, "since")) if arg(qs, "since") else None
    except ValueError: since = None
    try: wait = max(0.0, min(25.0, float(arg(qs, "wait", "0") or 0)))
    except ValueError: wait = 0.0
    return since, wait


@route("GET", "/api/room")
def api_room(k, m, qs, data):
    if k.tv_mode(): k.tv.wait(*since_wait(qs))
    return {**k.room(), "name": k.settings.get("room_name")}


@route("POST", "/api/room/queue")
def api_order(k, m, qs, data):
    number = str(data.get("number", "")).strip()
    if not NUM_RE.match(number): raise ApiError(400, "歌曲编号格式不对")
    return 201, k.order(number, str(data.get("by") or ""))


@route("POST", "/api/room/retry")
def api_retry(k, m, qs, data):
    number = str(data.get("number", "")).strip()
    code, job = k.jobs.submit(number, priority=True)
    if code >= 400: raise ApiError(code, job.get("error"))
    return {"ok": True, "status": job.get("state")}


@route("PATCH", r"/api/room/queue/([^/]+)")
def api_move(k, m, qs, data):
    (k.tv if k.tv_mode() else k.player).move_to_next(m[1]); return {"ok": True}


@route("DELETE", r"/api/room/queue/([^/]+)")
def api_remove(k, m, qs, data):
    (k.tv if k.tv_mode() else k.player).remove(m[1]); return {"ok": True}


@route("POST", "/api/room/player")
def api_player(k, m, qs, data):
    if data.get("action") not in ("pause", "resume", "next", "replay"): raise ApiError(400, "不支持的操作")
    (k.tv if k.tv_mode() else k.player).action(data["action"]); return {"ok": True}


@route("PATCH", "/api/room/audio")
def api_audio(k, m, qs, data):
    values = {key: data[key] for key in ("vocalMode", "musicVolume", "micVolume") if key in data}
    if not values: raise ApiError(400, "缺少音频参数")
    if k.tv_mode(): k.tv.audio(values.get("vocalMode"), values.get("musicVolume"))
    else: k.player.audio(values)
    return {"ok": True}


# ---- 电视播放器（第二阶段）
@route("GET", "/api/tv/state")
def tv_page_state(k, m, qs, data):
    k.tv.wait(*since_wait(qs))
    return k.tv_state()


@route("GET", "/api/player/state")
def player_state(k, m, qs, data):
    snap, names = k.tv.poll(*since_wait(qs)), k.jobs.downloaded_names()
    # 电视上要显示已点歌曲的下载进度，主页上显示曲库和 NAS 歌曲数
    snap["queue"] = [{**e, **k.download_state(e["number"], {}, names)} for e in snap["queue"]]
    snap["stats"] = k.stats()
    return snap


@route("POST", "/api/player/report")
def player_report(k, m, qs, data):
    k.tv.report(data); return {"ok": True}


@route("POST", "/api/player/action")
def player_action(k, m, qs, data):
    k.tv.action(str(data.get("action", ""))); return {"ok": True}


# ---- 管理页
@route("GET", "/admin/catalog")
def admin_catalog(k, m, qs, data):
    return k.updater.check_remote() if arg(qs, "remote") == "1" else k.updater.status()


@route("POST", "/admin/catalog/update")
def admin_catalog_update(k, m, qs, data):
    if not k.updater.start(): raise ApiError(409, "曲库正在更新")
    return 202, k.updater.status()


@route("GET", "/admin/settings")
def admin_settings(k, m, qs, data):
    return dict(k.settings.values)


@route("PUT", "/admin/settings")
def admin_settings_put(k, m, qs, data):
    values = {}
    if "room_name" in data:
        name = str(data.get("room_name") or "").strip()
        if not name or len(name) > 20: raise ApiError(400, "名字要 1 到 20 个字")
        values["room_name"] = name
    if "player_url" in data:
        url = str(data.get("player_url", "")).strip().rstrip("/")
        if url and not re.match(r"^https?://[^/\s]+$", url): raise ApiError(400, "播放器地址格式不对")
        values["player_url"] = url
    if "player_mode" in data:
        if data["player_mode"] not in ("maidong", "tv"): raise ApiError(400, "播放模式不对")
        values["player_mode"] = data["player_mode"]
    for key, limit in (("wifi_ssid", 32), ("wifi_password", 63)):
        if key in data: values[key] = str(data[key] or "")[:limit]
    if "wifi_security" in data:
        if data["wifi_security"] not in ("WPA", "WEP", "nopass"): raise ApiError(400, "WiFi 加密方式不对")
        values["wifi_security"] = data["wifi_security"]
    return k.settings.update(values)


# ---- 兼容麦动和旧管理页的下载接口（v2）
@route("GET", "/health")
def health(k, m, qs, data):
    return {"ok": True, "version": VERSION}


@route("GET", "/jobs")
def jobs_list(k, m, qs, data):
    try: limit = int(arg(qs, "limit", "100") or 100)
    except ValueError: limit = 100
    return k.jobs.all(limit)


@route("GET", "/queue")
def queue_summary(k, m, qs, data):
    return k.jobs.summary()


@route("GET", r"/status/(\d{1,16})")
def status(k, m, qs, data):
    return k.jobs.status(m[1])


@route("POST", "/download")
def download(k, m, qs, data):
    # 麦动只在点歌或保存时请求下载，按客人点歌处理（优先）
    return k.jobs.submit(str(data.get("number", "")).strip(), data.get("filename"), priority=True)


@route("DELETE", r"/download/(\d{1,16})")
def cancel(k, m, qs, data):
    return k.jobs.cancel(m[1])


@route("POST", "/admin/redownload")
def redownload(k, m, qs, data):
    """管理页：放起来卡的歌重新下一遍。正在放的不行，换文件会把电视上那首放坏。"""
    number = str(data.get("number", "")).strip()
    current = k.tv.snapshot()["current"]
    if current and current["number"] == number: raise ApiError(409, "电视正在放这首，切掉或放完再重新下载")
    return k.jobs.redownload(number)


@route("GET", "/exists")
def exists_get(k, m, qs, data):
    return k.jobs.exists([n for n in arg(qs, "numbers").split(",") if n])


@route("POST", "/exists")
def exists_post(k, m, qs, data):
    return k.jobs.exists(numbers_arg(data)[:1000])


def admin_rows(k, rows):
    names = k.jobs.downloaded_names()
    with k.jobs.lock:
        return [{"number": r[0], "name": r[1], "singer": r[2], "language": r[3], "filename": r[4], "duration": r[5],
                 "state": k.jobs.song_state(r[0], r[4], names), "downloaded": r[4] in names} for r in rows]


@route("GET", "/search")
def admin_search(k, m, qs, data):
    q, singer = arg(qs, "q"), arg(qs, "singer")
    if not q and not singer: raise ApiError(400, "缺少 q")
    rows = k.catalog.search_by_singer(singer, 0, 200) if singer and not q else k.catalog.search_songs(q, 0, 200)
    if singer and q: rows = [r for r in rows if singer in r[2]]
    return admin_rows(k, rows[:50])


@route("GET", "/singers")
def admin_singers(k, m, qs, data):
    try: limit = max(1, min(200, int(arg(qs, "limit", "60") or 60)))
    except ValueError: limit = 60
    rows, _ = k.catalog.singers(arg(qs, "q"), "", "", 0, limit)
    names = k.jobs.downloaded_names()
    out = []
    for number, name, region, kind, _image in rows:
        files = [r[4] for r in k.catalog.singer_songs(number, 0, 0)]
        out.append({"number": number, "name": name, "region": region, "type": kind,
                    "songs": len(files), "downloaded": sum(1 for f in files if f in names)})
    return out


@route("GET", r"/singer/(\d{1,16})/songs")
def admin_singer_songs(k, m, qs, data):
    return admin_rows(k, k.catalog.singer_songs(m[1], 0, 0))


@route("GET", "/languages")
def admin_languages(k, m, qs, data):
    return [{"language": r[0], "count": r[1]} for r in k.catalog.languages()]


@route("GET", "/playlists")
def admin_playlists(k, m, qs, data):
    return [{"number": r[0], "type": "chart" if r[1] == 4 else "playlist", "name": r[2], "count": r[3]}
            for r in k.catalog.playlists()]


@route("GET", r"/playlist/(\d{1,16})")
def admin_playlist(k, m, qs, data):
    kind = int(arg(qs, "type", "0") or 0) or None
    return admin_rows(k, k.catalog.playlist_songs(m[1], kind))


@route("POST", "/download_many")
def download_many(k, m, qs, data):
    return 202, k.jobs.submit_many(numbers_arg(data)[:5000])


@route("POST", "/download_playlist")
def download_playlist(k, m, qs, data):
    number, limit = str(data.get("number", "")).strip(), int(data.get("limit") or 0)
    kind = int(data.get("type") or 0) or None
    nums = list(dict.fromkeys(r[0] for r in k.catalog.playlist_songs(number, kind, 0, limit)))
    return 202, {"playlist": number, **k.jobs.submit_many(nums)}


@route("POST", "/download_singer")
def download_singer(k, m, qs, data):
    singer = str(data.get("number", "")).strip()
    if not NUM_RE.match(singer): raise ApiError(400, "歌手编号格式不对")
    rows = k.catalog.singer_songs(singer, 0, int(data.get("limit") or 0), str(data.get("language") or "").strip())
    return 202, {"singer": singer, **k.jobs.submit_many([r[0] for r in rows])}


@route("POST", "/queue/pause")
def queue_pause(k, m, qs, data):
    return k.jobs.set_paused(True)


@route("POST", "/queue/resume")
def queue_resume(k, m, qs, data):
    return k.jobs.set_paused(False)


@route("POST", "/queue/clear")
def queue_clear(k, m, qs, data):
    return k.jobs.clear_queue()


@route("POST", "/queue/retry")
def queue_retry(k, m, qs, data):
    return k.jobs.retry_failed()


# ---------------- HTTP ----------------
def make_handler(ktv, token=""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "home-ktv/" + VERSION

        def log_message(self, fmt, *args):
            line = args[0] if args and isinstance(args[0], str) else ""
            if any(p in line for p in ("GET /api/room ", "GET /api/room?", "GET /queue ", " /api/player/state", " /api/tv/state")): return  # 轮询太频繁，不记日志
            sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args)); sys.stdout.flush()

        def send_body(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD": self.wfile.write(body)

        def send_json(self, code, obj):
            self.send_body(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Agent-Token")
            self.end_headers()

        def media(self, number):
            """歌曲文件给电视播放器，支持 Range（ExoPlayer 读 MKV 索引和拖动进度都要用）。"""
            path = ktv.media_path(number)
            if not path or not os.path.exists(path): return self.send_json(404, {"error": "这首歌还没下载好"})
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            spec = (self.headers.get("Range") or "").strip()
            if spec:
                m = re.match(r"^bytes=(\d*)-(\d*)$", spec)
                if m and m[1]: start, end = int(m[1]), int(m[2]) if m[2] else size - 1
                elif m and m[2]: start = max(0, size - int(m[2]))
                end = min(end, size - 1)
                if not m or start > end:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD": return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk: break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                    # 播放器拖动进度时会主动断开

        def static(self, name):
            try:
                with open(os.path.join(WEB, name), "rb") as f: body = f.read()
            except OSError:
                return self.send_json(404, {"error": "not found"})
            self.send_body(200, body, STATIC[name] + "; charset=utf-8")

        def handle_request(self):
            # 请求行按 latin-1 解码，裸 UTF-8 的中文要转回来（浏览器会百分号编码，curl 直接写中文不会）
            raw = self.path
            try: raw = raw.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError): pass
            u = urllib.parse.urlsplit(raw)
            path, qs = urllib.parse.unquote(u.path), urllib.parse.parse_qs(u.query)
            if self.command in ("GET", "HEAD"):
                media = re.match(r"^/media/(\d{1,16})$", path)
                if media: return self.media(media[1])
            if self.command == "GET":
                if path in ("/", "/index.html"): return self.static("index.html")
                if path in ("/admin", "/admin/"): return self.static("admin.html")
                if path in ("/tv", "/tv/"): return self.static("tv.html")
                if path.startswith("/static/") and path[8:] in STATIC: return self.static(path[8:])
            if self.command in ("POST", "PUT", "PATCH", "DELETE") and token and self.headers.get("X-Agent-Token") != token \
                    and not path.startswith("/api/"):
                return self.send_json(401, {"error": "token 不对"})
            data = {}
            if self.command in ("POST", "PUT", "PATCH"):
                n = int(self.headers.get("Content-Length") or 0)
                try: data = json.loads((self.rfile.read(n) if n else b"").decode("utf-8") or "{}")
                except Exception: return self.send_json(400, {"error": "JSON 解析失败"})
                if not isinstance(data, dict): return self.send_json(400, {"error": "JSON 要是对象"})
            for method, pattern, fn in ROUTES:
                m = pattern.match(path)
                if m and method == self.command:
                    try:
                        result = fn(ktv, m, qs, data)
                    except (ApiError, RoomError) as e:
                        return self.send_json(e.status, {"error": str(e)})
                    except PlayerError as e:
                        return self.send_json(e.status if e.status >= 400 else 502, {"error": str(e)})
                    except Exception as e:
                        traceback.print_exc()
                        return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})
                    code, obj = result if isinstance(result, tuple) else (200, result)
                    return self.send_json(code, obj)
            self.send_json(404, {"error": "not found"})

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request
    return Handler


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--data", required=True, help="曲库、状态和设置所在目录")
    p.add_argument("--media", required=True, help="歌曲文件目录（OpenList 的 /KTV）")
    p.add_argument("--threads", type=int, default=64)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--token", default="")
    a = p.parse_args(argv)
    ktv = Ktv(a.data, a.media, a.threads, a.parallel)
    if not ktv.catalog.con: print("警告：曲库不存在，请在管理页更新曲库")
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(ktv, a.token))
    print(f"home-ktv v{VERSION} 监听 0.0.0.0:{a.port}  歌曲 {a.media}  曲库 {ktv.catalog.song_count()} 首  "
          f"{a.threads} 线程 x {a.parallel} 首", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
