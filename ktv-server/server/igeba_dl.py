#!/usr/bin/env python3
r"""
igeba_dl.py — i歌霸 曲库多线程下载器（纯标准库，任何 Python 3.11+ 都能跑；服务端也用它下歌）

用法：
  python3 igeba_dl.py search 晴天 [--singer 周杰伦]      # 在曲库里找编号
  python3 igeba_dl.py playlists                           # 列出 63 个歌单 + 7 个排行榜
  python3 igeba_dl.py playlist 51890                      # 看某个歌单里的歌
  python3 igeba_dl.py get 10005213 [更多编号...]          # 下载指定编号
  python3 igeba_dl.py get --playlist 51890 --limit 50     # 下载整个歌单（前 50 首）
  python3 igeba_dl.py get --singer 周杰伦 --limit 30      # 下载某歌手的歌

可选参数：
  --out DIR        保存目录，默认当前目录下的 media
  --threads N      每首歌并行连接数，默认 64（越多越快，实测 48 明显优于 16）
  --parallel N     同时下几首，默认 2
  --db PATH        曲库数据库路径，默认脚本同目录的 igeba_catalog.db（没有会自动从 Gitee 下）
"""
import argparse, json, os, queue, random, sqlite3, sys, threading, time, urllib.request, urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

ADDRESS_URL = "http://app.ige8.net/telnet.php"
DB_URL = "https://gitee.com/yangyachao-X/maidong-ktv/raw/feature/igeba-media/app/src/main/assets/igeba_catalog.db"
# CDN 只接受 ≤2 MiB 的 Range，大了 403。分段越小，同时用上的连接越多，最后几段拖尾也越短。
CHUNK = min(2048, int(os.environ.get("KTV_CHUNK_KIB", "512"))) * 1024
UA = "Mozilla/5.0 (Linux; Android 12; TV) MaidongKTV"

# ---------- 曲库 ----------
def ensure_db(path):
    if os.path.exists(path):
        return
    print(f"曲库不存在，正在从 Gitee 下载到 {path} ...")
    urllib.request.urlretrieve(DB_URL, path)
    print("曲库下载完成")

def db(path):
    ensure_db(path)
    return sqlite3.connect(path)

def cmd_search(con, a):
    sql = "select number,name,singer,language,filename,duration from source_songs where name like ?"
    args = [f"%{a.query}%"]
    if a.singer:
        sql += " and singer like ?"; args.append(f"%{a.singer}%")
    rows = con.execute(sql + " order by singer,name limit 50", args).fetchall()
    for n, name, singer, lang, fn, dur in rows:
        print(f"{n}  {name} / {singer}  [{lang}] {dur//60}:{dur%60:02d}  -> {fn}")
    if not rows: print("没找到")

def cmd_playlists(con, a):
    for n, t, name, cnt in con.execute(
            "select number,package_type,name,song_count from source_playlists order by package_type,sort_no"):
        print(f"{n:>6}  {'排行榜' if t == 4 else '歌单'}  {name}  ({cnt} 首)")

def cmd_playlist(con, a):
    rows = con.execute("""select s.number,s.name,s.singer,s.language from source_playlist_songs p
        join source_songs s on s.number=p.song_number where p.playlist_number=? order by p.sort_no""",
        (a.number,)).fetchall()
    for n, name, singer, lang in rows: print(f"{n}  {name} / {singer} [{lang}]")
    print(f"共 {len(rows)} 首")

def resolve_targets(con, a):
    numbers = list(a.numbers or [])
    if a.playlist:
        numbers += [r[0] for r in con.execute(
            "select song_number from source_playlist_songs where playlist_number=? order by sort_no", (a.playlist,))]
    if a.singer:
        numbers += [r[0] for r in con.execute(
            "select number from source_songs where singer like ? order by sort_no", (f"%{a.singer}%",))]
    seen, out = set(), []
    for n in numbers:
        if n in seen: continue
        seen.add(n)
        r = con.execute("select number,name,singer,filename from source_songs where number=?", (n,)).fetchone()
        if r: out.append(r)
        else: print(f"曲库里没有编号 {n}，跳过")
    return out[:a.limit] if a.limit else out

# ---------- 接口 ----------
def get_song_url(number, attempts=6, stagger=5, timeout=20):
    """地址接口（telnet.php）平时 2 秒左右，偶尔几十秒不回：
    每 stagger 秒没结果就再并发发一个请求（最多 attempts 个），谁先成功用谁。"""
    results = queue.Queue()
    def one():
        try: results.put((True, _get_song_url_once(number, timeout)))
        except Exception as e: results.put((False, e))
    sent, failed, last = 0, 0, None
    deadline = time.time() + attempts * stagger + timeout
    while failed < attempts and time.time() < deadline:
        if sent < attempts and sent - failed < 3:            # 同时最多 3 个请求在路上
            threading.Thread(target=one, daemon=True).start(); sent += 1
        try:
            ok, value = results.get(timeout=stagger if sent < attempts else max(0.1, deadline - time.time()))
        except queue.Empty:
            continue
        if ok: return value
        failed, last = failed + 1, value
    raise RuntimeError(f"地址接口失败（{failed} 次）: {last or '超时'}")

def _get_song_url_once(number, timeout=30):
    form = {"VER": "10.0", "CMD": "GET_SONGADDR",
            "PARAMS": json.dumps({"UserId": random.randint(100000, 999999), "SongNumber": number})}
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(ADDRESS_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8"))
    if j.get("Result", 0) < 0 or j.get("Rights", {}).get("Code") not in (0, 1):
        raise RuntimeError(j.get("Description") or j.get("Rights", {}).get("Reason") or "歌源拒绝")
    url = j["FileUrl"]
    if not url.startswith("http"): raise RuntimeError("未返回播放地址")
    return url

def fetch_range(url, start, end, on_data=None, timeout=60):
    """请求 [start, end]（按 HTTP 标准含两端）。返回 (实际起点, 数据)。on_data(n) 每收到一块数据调用一次。"""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}",
                                               "Accept-Encoding": "identity", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cr = r.headers.get("Content-Range", "")
        pieces = []
        while True:
            piece = r.read(64 * 1024)
            if not piece: break
            pieces.append(piece)
            if on_data: on_data(len(piece))
        body = b"".join(pieces)
        total = None
        actual_start = start
        if cr.startswith("bytes "):
            rng, _, tot = cr[6:].partition("/")
            actual_start = int(rng.split("-")[0])
            if tot.isdigit(): total = int(tot)
        return actual_start, body, total

def probe(url):
    _, body, total = fetch_range(url, 0, 62)
    if total is None: raise RuntimeError("服务器没返回 Content-Range，无法分段")
    return total

# ---------- 下载 ----------
class Cancelled(Exception):
    pass

def download_song(number, name, singer, filename, outdir, threads, on_progress=None, should_stop=None, replace=False):
    """on_progress(done_bytes, total_bytes, mib_per_s) 可选；should_stop() 返回 True 时中止并抛 Cancelled。
    replace：文件已经有了也重新下（坏了的歌）；新文件下完整才替换旧的，失败时旧的还在。"""
    dest = os.path.join(outdir, filename)
    part = dest + ".part"
    lockp = dest + ".lock"
    if not replace and os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"[跳过] {name}/{singer} 已存在")
        return True
    # 同一首歌只允许一个进程下：锁文件 10 分钟没更新视为失效
    try:
        if os.path.exists(lockp) and time.time() - os.path.getmtime(lockp) < 600:
            print(f"[跳过] {name}/{singer} 另一个进程正在下载")
            return False
        with open(lockp, "w") as lf: lf.write(str(os.getpid()))
    except OSError:
        pass
    try:
        return _download_locked(number, name, singer, filename, dest, part, lockp, threads, on_progress, should_stop)
    finally:
        try: os.remove(lockp)
        except OSError: pass

def _download_locked(number, name, singer, filename, dest, part, lockp, threads, on_progress, should_stop):
    try:
        url = get_song_url(number)
        total = probe(url)
    except Exception as e:
        print(f"[失败] {name}/{singer} 取地址: {e}")
        raise
    url_box = [url]            # 分段大面积超时/403 时重新取链接
    url_lock = threading.Lock()
    def refresh_url(bad):
        with url_lock:
            if url_box[0] != bad: return url_box[0]
            try:
                url_box[0] = get_song_url(number)
                print(f"  {name}/{singer}: 已重新获取下载地址")
            except Exception as e:
                print(f"  {name}/{singer}: 重新取地址失败 {e}")
            return url_box[0]

    if should_stop and should_stop(): raise Cancelled()
    covered = [False] * ((total + CHUNK - 1) // CHUNK)
    lock = threading.Lock()
    done_bytes = [0]
    received = [0]          # 已收到的字节（含还没下完的分段），用来实时显示进度
    t0 = time.time()
    with open(part, "wb") as f:
        f.truncate(total)

    # 分段要整段下完才算完成，几十段同时下时大多在最后一起完成；进度按实际收到的字节每秒报一次。
    ticking = threading.Event()
    def ticker():
        last_bytes, last_t, speed = 0, time.time(), 0.0
        while not ticking.wait(1):
            now, got = time.time(), received[0]
            rate = (got - last_bytes) / 1048576 / max(now - last_t, 0.001)
            speed = rate if not speed else speed * 0.7 + rate * 0.3
            last_bytes, last_t = got, now
            if on_progress: on_progress(min(got, total), total, speed)
    threading.Thread(target=ticker, daemon=True).start()

    finished = set()        # 已经写好的分段
    active_at = {}          # 分段 -> 当前这次请求最后一次收到数据的时间
    class Superseded(Exception):
        pass

    def work(i):
        start = i * CHUNK
        end = min(start + CHUNK, total) - 1
        for attempt in range(6):
            if i in finished: return True
            if should_stop and should_stop(): raise Cancelled()
            url = url_box[0]
            got = [0]
            def on_data(n):
                if i in finished: raise Superseded()          # 另一路已经下完这段
                if should_stop and should_stop(): raise Cancelled()
                active_at[i] = time.time()
                with lock: received[0] += n; got[0] += n
            active_at[i] = time.time()
            try:
                # 歌源把 Range 右端点当成不包含（要 10 字节只给 9 字节）。多要 1 字节再截断，
                # 两种服务器都能拿全；不够长就当失败重试，不能留下空字节。
                # 卡住的连接 20 秒没数据就换一条，不等 60 秒。
                need = end - start + 1
                a_start, body, _ = fetch_range(url, start, end + 1, on_data, timeout=20)
                if a_start != start: raise RuntimeError(f"返回的起点不对：{a_start}")
                if len(body) < need: raise RuntimeError(f"数据不全：{len(body)}/{need}")
                body = body[:need]
                with lock:
                    if i in finished: raise Superseded()
                    with open(part, "r+b") as f:
                        f.seek(start); f.write(body)
                    done_bytes[0] += len(body)
                    finished.add(i)
                return True
            except Superseded:
                with lock: received[0] -= got[0]
                return True
            except Cancelled:
                raise
            except Exception as e:
                with lock: received[0] -= got[0]       # 这次没下完的不算进度
                if attempt == 5: print(f"  段 {i} 失败: {e}")
                if attempt >= 1: refresh_url(url)      # 第二次失败起换新链接
                try: os.utime(lockp)
                except OSError: pass
                time.sleep(2 + 3 * attempt)
        return False

    chunks, ok = len(covered), False
    ex = ThreadPoolExecutor(max_workers=threads)
    futs = {ex.submit(work, i): i for i in range(chunks)}
    pending, hedged, last = set(futs), set(), 0
    try:
        while pending and len(finished) < chunks:
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for fu in done: fu.result()               # 取消会在这里抛出来
            if should_stop and should_stop(): raise Cancelled()   # 只剩卡住的连接时也能马上取消
            now = time.time()
            # 拖尾：剩下的段不多了，给 6 秒没收到数据的段再开一路，先下完的算数
            left = [i for i in range(chunks) if i not in finished]
            if len(left) <= max(2, threads // 4):
                for i in left:
                    if i not in hedged and now - active_at.get(i, now) > 6:
                        hedged.add(i)
                        fu = ex.submit(work, i); futs[fu] = i; pending.add(fu)
            if now - last > 2:
                last = now
                mb = done_bytes[0] / 1048576
                print(f"  {name}/{singer}: {mb:.1f}/{total/1048576:.1f} MiB  {mb/(now-t0):.2f} MiB/s", flush=True)
        ok = len(finished) == chunks
    except Cancelled:
        for f_ in futs: f_.cancel()
        try: os.remove(part)
        except OSError: pass
        print(f"[取消] {name}/{singer}")
        raise
    finally:
        ticking.set()
        # 不等卡住的连接：它们超时后发现这段已经写好，不会再动文件
        ex.shutdown(wait=False, cancel_futures=True)
    if ok and done_bytes[0] == total and os.path.getsize(part) == total:   # 每个字节都写到了
        os.replace(part, dest)
        dt = time.time() - t0
        print(f"[完成] {name}/{singer} -> {filename}  {total/1048576:.1f} MiB  用时 {dt:.0f}s  平均 {total/1048576/dt:.2f} MiB/s")
        return True
    print(f"[失败] {name}/{singer} 有分段没下全，保留 {part} 可重跑续传")
    return False

def cmd_get(con, a):
    os.makedirs(a.out, exist_ok=True)
    targets = resolve_targets(con, a)
    if not targets:
        print("没有要下载的歌"); return
    print(f"准备下载 {len(targets)} 首到 {a.out}，每首 {a.threads} 线程，同时 {a.parallel} 首")
    okc = 0
    def safe(*args):
        try: return download_song(*args)
        except Exception: return False
    def run(batch):
        done = []
        with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            futs = {ex.submit(safe, n, name, singer, fn, a.out, a.threads): (n, name, singer, fn) for n, name, singer, fn in batch}
            for fu in as_completed(futs):
                if fu.result(): done.append(futs[fu])
        return done
    done = run(targets)
    failed = [t for t in targets if t not in done]
    if failed:
        print(f"第一轮完成 {len(done)}/{len(targets)}，60 秒后重试失败的 {len(failed)} 首")
        time.sleep(60)
        done += run(failed)
    print(f"完成 {len(done)}/{len(targets)}")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "igeba_catalog.db"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--singer")
    sub.add_parser("playlists")
    s = sub.add_parser("playlist"); s.add_argument("number")
    s = sub.add_parser("get")
    s.add_argument("numbers", nargs="*")
    s.add_argument("--playlist"); s.add_argument("--singer")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--out", default="media")
    s.add_argument("--threads", type=int, default=64)
    s.add_argument("--parallel", type=int, default=2)
    a = p.parse_args()
    con = db(a.db)
    {"search": cmd_search, "playlists": cmd_playlists, "playlist": cmd_playlist, "get": cmd_get}[a.cmd](con, a)

if __name__ == "__main__":
    main()
