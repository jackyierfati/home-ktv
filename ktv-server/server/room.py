"""第二阶段：服务端自己管点歌队列和播放状态，电视播放器按这里的状态播放。

电视播放器长轮询 snapshot(since)：状态有变化立刻返回，否则最多等几秒，顺便带上最新下载进度。
每首已点记着是谁点的（by），每次变化记一条动态（feed），电视主页拿来滚动显示和弹提示。
"""
import json, os, threading, time, uuid

PLAYER_TIMEOUT = 15            # 播放器这么久没来取状态，就算“电视未连接”
FEED_KEEP = 50


class RoomError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Room:
    def __init__(self, path, lookup, is_ready):
        """lookup(number) -> (number, name, singer, filename)；is_ready(number) -> 歌曲文件已在 NAS 上。"""
        self.path, self.lookup, self.is_ready = path, lookup, is_ready
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.queue = []                 # 等待中的歌：{"id","number","name","singer","by","added"}
        self.current = None             # 正在播放的歌
        self.playing, self.vocal, self.volume, self.replay = True, "accompany", 70, 0
        self.version = 0
        self.feed, self.feed_seq = [], 0
        self.users = {}                 # 昵称 -> {"requested": 点了几首, "sung": 唱完几首}
        self.player = {"seen": 0, "state": None, "position": 0, "duration": 0, "entry": None, "error": None}
        self.load()

    # ---- 持久化
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f: data = json.load(f)
        except (OSError, ValueError):
            return
        self.queue = data.get("queue", [])
        if data.get("current"): self.queue.insert(0, data["current"])     # 重启后从头唱这首
        self.vocal = data.get("vocal", self.vocal)
        self.volume = data.get("volume", self.volume)
        self.feed = data.get("feed", [])
        self.feed_seq = max([e["id"] for e in self.feed], default=0)
        self.users = data.get("users", {})

    def save(self):
        data = {"queue": self.queue, "current": self.current, "vocal": self.vocal, "volume": self.volume,
                "feed": self.feed, "users": self.users}
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            print("已点保存失败:", e)

    def _changed(self):
        self.version += 1
        self.save()
        self.changed.notify_all()

    def _event(self, kind, text, by=""):
        self.feed_seq += 1
        self.feed.append({"id": self.feed_seq, "t": time.time(), "kind": kind, "text": text, "by": by})
        del self.feed[:-FEED_KEEP]

    def _count(self, by, key):
        if not by: return
        user = self.users.setdefault(by, {"requested": 0, "sung": 0})
        user[key] += 1

    @staticmethod
    def _who(entry):
        return entry.get("by") or "有人"

    # ---- 队列
    def add(self, number, by=""):
        row = self.lookup(number)
        if not row: raise RoomError(404, "曲库里没有这首歌")
        by = (by or "").strip()[:16]
        with self.lock:
            if any(e["number"] == number for e in self.queue) or (self.current and self.current["number"] == number):
                raise RoomError(409, "这首歌已经点过了")
            entry = {"id": uuid.uuid4().hex[:12], "number": number, "name": row[1], "singer": row[2],
                     "by": by, "added": time.time()}
            self.queue.append(entry)
            self._count(by, "requested")
            self._event("order", f"{self._who(entry)} 点了《{row[1]}》", by)
            self._advance_if_idle()
            self._changed()
            return entry

    def remove(self, entry_id):
        with self.lock:
            if self.current and self.current["id"] == entry_id: raise RoomError(409, "正在播放的歌不能删除，可以切歌")
            before = len(self.queue)
            self.queue = [e for e in self.queue if e["id"] != entry_id]
            if len(self.queue) == before: raise RoomError(404, "已点里没有这首歌")
            self._changed()

    def move_to_next(self, entry_id):
        with self.lock:
            entry = next((e for e in self.queue if e["id"] == entry_id), None)
            if not entry: raise RoomError(404, "已点里没有这首歌")
            self.queue.remove(entry)
            self.queue.insert(0, entry)
            self._event("top", f"《{entry['name']}》顶到了下一首", entry.get("by", ""))
            self._changed()

    def _advance_if_idle(self):
        """没在放歌时，从已点里挑第一首已经下好的开始放；还没下好的留在原位等。"""
        if self.current: return False
        for entry in self.queue:
            if self.is_ready(entry["number"]):
                self.queue.remove(entry)
                self.current, self.playing = entry, True
                self._event("start", f"轮到 {self._who(entry)} 的《{entry['name']}》", entry.get("by", ""))
                return True
        return False

    def song_ready(self, number):
        """下载器下完一首歌时调用：记一条动态，正好空闲就开始放。"""
        with self.lock:
            entry = next((e for e in self.queue if e["number"] == number), None)
            if not entry: return
            self._event("ready", f"《{entry['name']}》下载好了", entry.get("by", ""))
            self._advance_if_idle()
            self._changed()

    def finish(self, entry_id=None, reason="skip"):
        """放完（ended）、切歌（skip）或播放出错（error）：换下一首。entry_id 不是当前这首（旧消息）就忽略。"""
        with self.lock:
            if not self.current or (entry_id and self.current["id"] != entry_id): return False
            done = self.current
            if reason == "ended": self._count(done.get("by"), "sung")
            elif reason == "skip": self._event("skip", f"切掉了《{done['name']}》", done.get("by", ""))
            else: self._event("error", f"《{done['name']}》放不了，已跳过", done.get("by", ""))
            self.current = None
            self._advance_if_idle()
            self._changed()
            return True

    # ---- 遥控
    def action(self, action):
        with self.lock:
            if action == "pause": self.playing = False
            elif action == "resume": self.playing = True
            elif action == "replay": self.replay += 1; self.playing = True
            elif action == "next": return self.finish(reason="skip")
            else: raise RoomError(400, "不支持的操作")
            self._changed()

    def audio(self, vocal=None, volume=None):
        with self.lock:
            if vocal is not None:
                if vocal not in ("original", "accompany"): raise RoomError(400, "原唱/伴唱参数不对")
                self.vocal = vocal
            if volume is not None:
                self.volume = max(0, min(100, int(volume)))
            self._changed()

    # ---- 播放器
    def report(self, data):
        with self.lock:
            self.player.update(seen=time.time(), state=data.get("state"), position=int(data.get("position") or 0),
                               duration=int(data.get("duration") or 0), entry=data.get("entryId"), error=data.get("error"))
            if data.get("state") in ("ended", "error") and data.get("entryId"):
                if data.get("state") == "error": print("播放出错，跳过:", self.current, data.get("error"))
                self.finish(data["entryId"], data["state"])

    def connected(self):
        return time.time() - self.player["seen"] < PLAYER_TIMEOUT

    def wait(self, since=None, wait=0):
        """状态没变（version == since）时最多等 wait 秒；电视主页和点歌页长轮询用。"""
        with self.lock:
            if since is not None and wait > 0 and since == self.version: self.changed.wait(wait)

    def poll(self, since=None, wait=0):
        """电视播放器取状态：since 是它上次拿到的 version，状态没变时最多等 wait 秒。"""
        with self.lock:
            self.player["seen"] = time.time()
            self.wait(since, wait)
            self.player["seen"] = time.time()
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            brief = lambda e: e and {"id": e["id"], "number": e["number"], "name": e["name"], "singer": e["singer"],
                                     "by": e.get("by", "")}
            current = brief(self.current)
            if current:
                current["media"] = f"/media/{current['number']}"
                # 播放器上次汇报的位置（秒）：退出再进来时接着唱
                current["position"] = self.player["position"] if self.player["entry"] == current["id"] else 0
            return {"version": self.version, "current": current, "queue": [brief(e) for e in self.queue],
                    "playing": self.playing, "vocal": self.vocal, "volume": self.volume, "replay": self.replay,
                    "feed": self.feed[-20:], "users": dict(self.users)}
