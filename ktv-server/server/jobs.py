"""下载队列：客人点的歌插队优先，批量任务排在后面；全部排队任务落盘，重启后继续。"""
import json, os, re, threading, time
from collections import OrderedDict

NUM_RE = re.compile(r"^\d{1,16}$")
FN_RE = re.compile(r"^[0-9A-Za-z_.\-]{1,64}$")
ACTIVE = ("queued", "downloading")
HISTORY_KEEP = 300                 # 已结束任务最多保留多少条


class Jobs:
    """download(number, name, singer, filename, outdir, on_progress, should_stop) -> bool；
    取消时抛 cancelled 异常。lookup(number) -> (number, name, singer, filename) 或 None。"""

    def __init__(self, lookup, outdir, state_file, download, cancelled, parallel=2, retry_delay=30, on_done=None):
        self.lookup, self.outdir, self.state_file = lookup, outdir, state_file
        self.download, self.cancelled, self.retry_delay = download, cancelled, retry_delay
        self.on_done = on_done             # 一首歌下完时调用 on_done(number)
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.jobs = OrderedDict()          # number -> dict
        self.queue = []                    # 等待中的编号，优先任务在前
        self.paused = False                # 暂停时不开始新任务，正在下的继续下完
        self.load()
        for _ in range(max(1, parallel)):
            threading.Thread(target=self.worker, daemon=True).start()

    # ---- persistence
    def save(self):
        with self.lock:
            downloading = [j for j in self.jobs.values() if j["state"] == "downloading"]
            queued = [self.jobs[n] for n in self.queue if n in self.jobs]
            finished = [j for j in self.jobs.values() if j["state"] not in ACTIVE][-HISTORY_KEEP:]
            data = {"jobs": finished + downloading + queued, "paused": self.paused}
        try:
            with open(self.state_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(self.state_file + ".tmp", self.state_file)
        except OSError as e:
            print("状态保存失败:", e)

    def load(self):
        if not os.path.exists(self.state_file): return
        try:
            with open(self.state_file, encoding="utf-8") as f: data = json.load(f)
        except Exception:
            return
        self.paused = bool(data.get("paused"))
        for j in data.get("jobs", []):
            if j.get("state") in ACTIVE:
                j["state"] = "queued"; j["progress"] = 0; j["done_bytes"] = 0; j["speed_mib"] = 0.0
                self.queue.append(j["number"])
            self.jobs[j["number"]] = j
        # 下载中断的任务恢复后，优先任务仍排在前面
        self.queue.sort(key=lambda n: not self.jobs[n].get("priority"))
        if self.queue: print(f"恢复 {len(self.queue)} 个未完成任务" + ("（队列已暂停）" if self.paused else ""))

    # ---- helpers
    def downloaded_names(self):
        try: return set(os.listdir(self.outdir))
        except OSError: return set()

    def song_state(self, number, filename, names=None):
        """done / queued / downloading / failed / cancelled / None"""
        j = self.jobs.get(number)
        if j and j.get("replace") and j["state"] in ACTIVE: return j["state"]     # 正在重新下载
        if filename in (names if names is not None else self.downloaded_names()): return "done"
        return j["state"] if j else None

    def progress_of(self, number):
        with self.lock:
            j = self.jobs.get(number)
            return dict(j) if j else None

    def _enqueue(self, number, priority):
        if number in self.queue: self.queue.remove(number)
        if priority:
            pos = 0
            while pos < len(self.queue) and self.jobs[self.queue[pos]].get("priority"): pos += 1
            self.queue.insert(pos, number)
        else:
            self.queue.append(number)
        self.cv.notify()

    def _new(self, number, name, singer, fn, state="queued", progress=0, priority=False):
        j = {"number": number, "name": name, "singer": singer, "filename": fn, "state": state,
             "progress": progress, "done_bytes": 0, "total_bytes": 0, "speed_mib": 0.0, "error": None,
             "created": time.time(), "cancel": False, "priority": priority}
        self.jobs[number] = j
        self.jobs.move_to_end(number)
        finished = [n for n, x in self.jobs.items() if x["state"] not in ACTIVE]
        for n in finished[:max(0, len(finished) - HISTORY_KEEP * 2)]: self.jobs.pop(n, None)
        return j

    # ---- public API
    def submit(self, number, filename=None, priority=False):
        if not NUM_RE.match(number or ""): return 400, {"error": "歌曲编号格式不对"}
        row = self.lookup(number)
        if row:
            _, name, singer, fn = row
        elif filename and FN_RE.match(filename):
            name, singer, fn = number, "", filename
        else:
            return 404, {"error": "曲库里没有这个编号，且没有给文件名"}
        dest = os.path.join(self.outdir, fn)
        with self.lock:
            j = self.jobs.get(number)
            # 先看是不是已经在下（包括重新下载：那时旧文件还在，不能当成“已下好”把任务顶掉）
            if j and j["state"] in ACTIVE:
                if priority and not j.get("priority"):
                    j["priority"] = True
                    if j["state"] == "queued": self._enqueue(number, True)
                    self.save()
                return 200, dict(j)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                j = self._new(number, name, singer, fn, state="done", progress=100, priority=priority)
                self.save(); return 200, dict(j)
            j = self._new(number, name, singer, fn, priority=priority)
            self._enqueue(number, priority)
            self.save()
            return 202, dict(j)

    def redownload(self, number):
        """已经下好的歌坏了：插队重新下。新文件下完整了才替换旧的，下失败旧文件还在。"""
        row = self.lookup(number) if NUM_RE.match(number or "") else None
        if not row: return 404, {"error": "曲库里没有这首歌"}
        _, name, singer, fn = row
        with self.lock:
            j = self.jobs.get(number)
            if j and j["state"] in ACTIVE: return 409, {"error": "这首歌正在下载，下完再说"}
            j = self._new(number, name, singer, fn, priority=True)
            j["replace"] = True
            self._enqueue(number, True)
            self.save()
            return 202, dict(j)

    def replacing(self, number):
        """这首歌正在重新下载（旧文件还在，但不该拿来放）。"""
        with self.lock:
            j = self.jobs.get(number)
            return bool(j and j.get("replace") and j["state"] in ACTIVE)

    def submit_many(self, numbers):
        """批量入队（普通优先级）；已下好的、已在队列里的、曲库没有的分别计数，不占队列。"""
        names = self.downloaded_names()
        res = {"total": 0, "queued": 0, "existing": 0, "active": 0, "missing": 0}
        with self.lock:
            for number in dict.fromkeys(str(n).strip() for n in numbers):
                res["total"] += 1
                row = self.lookup(number) if NUM_RE.match(number) else None
                if not row: res["missing"] += 1; continue
                _, name, singer, fn = row
                if fn in names: res["existing"] += 1; continue
                j = self.jobs.get(number)
                if j and j["state"] in ACTIVE: res["active"] += 1; continue
                self._new(number, name, singer, fn)
                self.queue.append(number); res["queued"] += 1
            if res["queued"]: self.cv.notify_all()
            self.save()
        return res

    def status(self, number):
        with self.lock:
            j = self.jobs.get(number)
            if j: return 200, dict(j)
        row = self.lookup(number)
        if row and os.path.exists(os.path.join(self.outdir, row[3])):
            return 200, {"number": number, "filename": row[3], "state": "done", "progress": 100,
                         "name": row[1], "singer": row[2], "done_bytes": 0, "total_bytes": 0, "speed_mib": 0.0, "error": None}
        return 404, {"error": "没有这个任务"}

    def exists(self, numbers):
        names = self.downloaded_names()
        out = {}
        with self.lock:
            for number in numbers:
                number = str(number).strip()
                row = self.lookup(number) if NUM_RE.match(number) else None
                out[number] = self.song_state(number, row[3], names) if row else None
        return out

    def cancel(self, number):
        with self.lock:
            j = self.jobs.get(number)
            if not j: return 404, {"error": "没有这个任务"}
            if j["state"] == "queued":
                self.queue.remove(number); j["state"] = "cancelled"
            elif j["state"] == "downloading":
                j["cancel"] = True
            else:
                return 200, dict(j)
            self.save()
            return 200, dict(j)

    def set_paused(self, paused):
        with self.lock:
            self.paused = paused
            self.cv.notify_all()
            self.save()
        return self.summary()

    def clear_queue(self):
        """清空普通优先级的排队（客人点的歌和正在下载的不受影响）；清掉的任务不留历史。"""
        with self.lock:
            cleared = {n for n in self.queue if not self.jobs[n].get("priority")}
            for number in cleared: self.jobs.pop(number, None)
            self.queue = [n for n in self.queue if n not in cleared]
            self.save()
        return {"cleared": len(cleared), **self.summary()}

    def retry_failed(self):
        with self.lock:
            failed = [n for n, j in self.jobs.items() if j["state"] == "failed"]
        res = self.submit_many(failed)
        return {"retried": res["queued"], **self.summary()}

    def active(self):
        """正在下载的和优先排队的任务，给点歌页面显示“正在下载”。"""
        with self.lock:
            downloading = [dict(j) for j in self.jobs.values() if j["state"] == "downloading"]
            waiting = [dict(self.jobs[n]) for n in self.queue if self.jobs[n].get("priority")]
            return downloading + waiting

    def summary(self):
        with self.lock:
            states = {}
            for j in self.jobs.values(): states[j["state"]] = states.get(j["state"], 0) + 1
            downloading = [dict(j) for j in self.jobs.values() if j["state"] == "downloading"]
            return {"paused": self.paused, "queued": len(self.queue), "downloading": downloading,
                    "done": states.get("done", 0), "failed": states.get("failed", 0)}

    def all(self, limit=100):
        with self.lock:
            downloading = [dict(j) for j in self.jobs.values() if j["state"] == "downloading"]
            queued = [dict(self.jobs[n]) for n in self.queue if n in self.jobs]
            finished = [dict(j) for j in self.jobs.values() if j["state"] not in ACTIVE][-max(0, limit):]
            # 旧网页按倒序显示，所以活动任务放在最后
            return finished + queued[::-1] + downloading

    # ---- worker
    def worker(self):
        while True:
            with self.cv:
                while not self.queue or self.paused: self.cv.wait()
                number = self.queue.pop(0)
                j = self.jobs[number]
                j["state"] = "downloading"; j["cancel"] = False
                self.save()

            def on_progress(done, total, speed):
                with self.lock:
                    j["done_bytes"] = done; j["total_bytes"] = total; j["speed_mib"] = round(speed, 2)
                    j["progress"] = min(99, int(done * 100 / total)) if total else 0

            def should_stop():
                return j.get("cancel", False)

            try:
                ok = False
                for attempt in range(3):          # 地址接口超时等临时错误自动重试 3 次
                    try:
                        extra = {"replace": True} if j.get("replace") else {}
                        ok = self.download(number, j["name"], j["singer"], j["filename"], self.outdir,
                                           on_progress=on_progress, should_stop=should_stop, **extra)
                    except self.cancelled:
                        raise
                    except Exception as e:
                        if attempt == 2: raise
                        print(f"任务 {number} 第 {attempt+1} 次失败: {e}，{self.retry_delay} 秒后重试")
                        with self.lock: j["error"] = f"重试中: {str(e)[:120]}"
                        for _ in range(self.retry_delay):
                            if should_stop(): raise self.cancelled()
                            time.sleep(1)
                        continue
                    if ok: break
                    if attempt < 2:
                        print(f"任务 {number} 有分段没下全，{self.retry_delay} 秒后重试"); time.sleep(self.retry_delay)
                with self.lock:
                    if ok: j["state"] = "done"; j["progress"] = 100; j["error"] = None; j["finished"] = time.time()
                    else: j["state"] = "failed"; j["error"] = "有分段没下全"
            except self.cancelled:
                with self.lock: j["state"] = "cancelled"
            except BaseException as e:  # 任何异常都不能让 worker 线程死掉
                with self.lock: j["state"] = "failed"; j["error"] = f"{type(e).__name__}: {e}"[:200]
                print(f"任务 {number} 异常: {type(e).__name__}: {e}")
            with self.lock: self.save()
            if j["state"] == "done" and self.on_done:
                try: self.on_done(number)
                except Exception as e: print("下载完成回调出错:", e)
