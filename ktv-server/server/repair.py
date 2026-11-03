"""修复旧下载器留下的坏字节。

歌源把 Range 右端点当成不包含，旧版下载器每个分段都少了最后 1 字节（文件里留的是 0），
FFmpeg 能将就着播，ExoPlayer 读到就报错。旧版分段是 2 MiB 或 512 KiB，所以只要把每个
512 KiB 边界前的那 1 字节（2 MiB 边界也在其中）和文件最后 1 字节重新取回来写进去，不用重下。

用法（容器里）：python -m server.repair --data /data --media /media
修好的文件记在 data/repaired.json，重跑会跳过。
"""
import argparse, json, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor

from . import igeba_dl
from .catalog import Catalog

STEP = 512 * 1024


def boundary_offsets(size):
    offsets = set(range(STEP - 1, size - 1, STEP))
    if size: offsets.add(size - 1)
    return sorted(offsets)


def fetch_byte(url, offset):
    # 要 [offset, offset+1]：不包含右端点的歌源正好给 1 字节，包含的给 2 字节，都取第一个
    start, body, _ = igeba_dl.fetch_range(url, offset, offset + 1, timeout=20)
    if start != offset or not body: raise RuntimeError(f"位置 {offset} 没取到数据")
    return body[:1]


def repair_file(path, number, threads=32, attempts=4):
    """返回 (检查的字节数, 改动的字节数)。"""
    size = os.path.getsize(path)
    offsets = boundary_offsets(size)
    url = igeba_dl.get_song_url(number)
    found, lock = {}, threading.Lock()

    def one(offset):
        for attempt in range(attempts):
            try:
                value = fetch_byte(url, offset)
                with lock: found[offset] = value
                return
            except Exception:
                time.sleep(1 + attempt)

    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(one, offsets))
    missing = [o for o in offsets if o not in found]
    if missing: raise RuntimeError(f"{len(missing)} 个位置没取到")
    changed = 0
    with open(path, "r+b") as f:
        for offset in offsets:
            f.seek(offset)
            if f.read(1) != found[offset]:
                f.seek(offset); f.write(found[offset]); changed += 1
    return len(offsets), changed


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--media", required=True)
    p.add_argument("--only", nargs="*", help="只修这些文件名")
    a = p.parse_args(argv)
    catalog = Catalog(os.path.join(a.data, "igeba_catalog.db"))
    record = os.path.join(a.data, "repaired.json")
    try:
        with open(record, encoding="utf-8") as f: done = set(json.load(f))
    except (OSError, ValueError):
        done = set()
    names = sorted(n for n in os.listdir(a.media) if re.match(r"^\d+\.mkv$", n))
    if a.only: names = [n for n in names if n in set(a.only)]
    todo = [n for n in names if n not in done]
    print(f"共 {len(names)} 首，已修过 {len(names) - len(todo)} 首，这次修 {len(todo)} 首", flush=True)
    for i, name in enumerate(todo, 1):
        row = catalog.one("select number from source_songs where filename=?", (name,))
        if not row:
            print(f"[{i}/{len(todo)}] {name}：曲库里找不到，跳过", flush=True); continue
        began = time.time()
        try:
            checked, changed = repair_file(os.path.join(a.media, name), row[0])
        except Exception as e:
            print(f"[{i}/{len(todo)}] {name}：失败 {e}", flush=True); continue
        done.add(name)
        with open(record + ".tmp", "w", encoding="utf-8") as f: json.dump(sorted(done), f)
        os.replace(record + ".tmp", record)
        print(f"[{i}/{len(todo)}] {name}：检查 {checked} 处，修正 {changed} 处，用时 {time.time() - began:.0f}s", flush=True)
    print("完成", flush=True)


if __name__ == "__main__":
    main()
