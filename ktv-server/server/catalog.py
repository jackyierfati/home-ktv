"""i歌霸曲库：只读查询，以及从 i歌霸公开接口重建整份曲库。

曲库结构和重建逻辑移植自麦动 App（IgebaCatalog / IgebaCatalogBootstrapper），遵循其非商用许可。
"""
import json, os, re, sqlite3, threading

HERE = os.path.dirname(os.path.abspath(__file__))
SONG_COLS = "s.number,s.name,s.singer,s.language,s.filename,s.duration"
SINGER_SPLIT = re.compile(r"[/、&]+")
UNRANKED = 1 << 30
CANDIDATES = 3000        # 搜索时最多拿这么多首来排序；一两个字母这种很宽的搜索，后面的本来也没人翻
# 歌手热度名次（单个歌手精确匹配；合唱在 Python 里再拆开算）；只用来决定先拿哪些候选
SINGER_RANK_SQL = f"coalesce((select min(x.sort_no) from source_singers x where x.name=s.singer), {UNRANKED})"


def _int(value):
    try: return int(value or 0)
    except (TypeError, ValueError): return 0


def like(value):
    return f"%{value.strip().upper()}%"


class Catalog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.con = None
        self.reopen()

    def reopen(self):
        with self.lock:
            if self.con: self.con.close()
            self.con = None
            self._signals = None
            if os.path.exists(self.path):
                self.con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)

    def query(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall() if self.con else []

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def meta(self):
        return dict(self.query("select key,value from meta"))

    def song_count(self):
        row = self.one("select count(*) from source_songs")
        return row[0] if row else 0

    def lookup(self, number):
        """(number, name, singer, filename)；下载队列用。"""
        return self.one("select number,name,singer,filename from source_songs where number=?", (number,))

    def songs_by_numbers(self, numbers):
        numbers = list(dict.fromkeys(numbers))
        rows = {}
        for i in range(0, len(numbers), 300):
            batch = numbers[i:i + 300]
            for r in self.query(f"select {SONG_COLS} from source_songs s where s.number in "
                                f"({','.join('?' * len(batch))})", batch):
                rows[r[0]] = r
        return [rows[n] for n in numbers if n in rows]

    # ---- 热门程度：搜索和歌手的歌都按它排，不再按 i歌霸的拼音顺序
    def signals(self):
        """(每首歌在几个歌单/榜单里, 歌手名 → (热度名次, 拼音首字母, 全拼))。
        i歌霸的歌手列表本身就是按热度排的（周杰伦第一）；重名的取最热门的那个。"""
        with self.lock:
            if self._signals is None:
                popular = dict(self.query("select song_number, count(*) from source_playlist_songs group by song_number"))
                singers = {r[0]: r[1:] for r in self.query(
                    "select name, min(sort_no), pinyin, pinyin_full from source_singers group by name")}
                self._signals = (popular, singers)
            return self._signals

    def ranked(self, rows, cached, first=lambda r: 0, singer_q=None):
        """rows 是 SONG_COLS + sort_no (+ 匹配程度)。先看 first（匹配程度），再看：NAS 上已经有的、
        上了歌单榜单的、歌手热门的、歌名短的。按歌手搜（singer_q）时先看搜中的是哪个歌手、他有多热门。"""
        popular, singers = self.signals()
        parts = lambda singer: [n.strip() for n in SINGER_SPLIT.split(singer or "") if n.strip()]
        info = lambda name: singers.get(name, (UNRANKED, "", ""))
        def singer_rank(singer):
            return min((info(n)[0] for n in parts(singer)), default=UNRANKED)
        def matched_singer(singer):
            """合唱里和搜索词对上的那个歌手：(完全相同 0 / 开头相同 1 / 包含 2, 热度名次)。"""
            key, best = singer_q.strip().upper(), (3, UNRANKED)
            for n in parts(singer):
                rank, *spellings = info(n)
                spellings = [n.upper(), *spellings]
                tier = 0 if key in spellings else 1 if any(s.startswith(key) for s in spellings) \
                    else 2 if any(key in s for s in spellings) else 3
                best = min(best, (tier, rank))
            return best
        def key(r):
            hot = (r[4] not in cached, -popular.get(r[0], 0))
            if singer_q is not None: return (*matched_singer(r[2]), *hot, r[6])
            return (first(r), *hot, singer_rank(r[2]), len(r[1]), r[6])
        return [r[:6] for r in sorted(rows, key=key)]

    # ---- 歌曲
    def search_songs(self, q, offset, limit, cached=frozenset()):
        """歌名或拼音。完全相同 > 开头相同 > 包含，同一档里热门的在前：qt 先出周杰伦的《晴天》。"""
        q = q.strip(); key = q.upper()
        rows = self.query(f"select {SONG_COLS}, s.sort_no, case when s.name=? or s.pinyin=? or s.pinyin_full=? then 0 "
                          "when s.name like ? or s.pinyin like ? or s.pinyin_full like ? then 1 else 2 end as tier "
                          "from source_songs s where s.name like ? or s.pinyin like ? or s.pinyin_full like ? "
                          f"order by tier, {SINGER_RANK_SQL}, s.sort_no limit ?",
                          (q, key, key, key + "%", key + "%", key + "%", like(q), like(q), like(q), CANDIDATES))
        return self.ranked(rows, cached, first=lambda r: r[7])[offset:offset + limit]

    def search_by_singer(self, q, offset, limit, cached=frozenset()):
        """歌手名或拼音，包括合唱；热门歌手的歌在前，同一个歌手里热门的歌在前。"""
        rows = self.query(f"select {SONG_COLS}, s.sort_no from source_songs s where s.singer like ? or s.number in "
                          "(select r.song_number from source_singer_songs r join source_singers sg "
                          "on sg.number=r.singer_number where sg.name like ? or sg.pinyin like ? or sg.pinyin_full like ?) "
                          f"order by {SINGER_RANK_SQL}, s.sort_no limit ?", (like(q), like(q), like(q), like(q), CANDIDATES))
        return self.ranked(rows, cached, singer_q=q)[offset:offset + limit]

    def hot(self, offset, limit):
        return self.query(f"select {SONG_COLS} from source_playlist_songs ps join source_playlists p "
                          "on p.number=ps.playlist_number and p.package_type=ps.package_type "
                          "join source_songs s on s.number=ps.song_number "
                          "where p.package_type=4 and p.name like '热歌榜%' order by ps.sort_no limit ? offset ?",
                          (limit, offset))

    # ---- 歌手
    def singers(self, q, region, kind, offset, limit):
        where, args = ["1=1"], []
        if q: where.append("(name like ? or pinyin like ? or pinyin_full like ?)"); args += [like(q)] * 3
        if region: where.append("region=?"); args.append(region)
        if kind: where.append("type=?"); args.append(kind)
        sql = " and ".join(where)
        rows = self.query(f"select number,name,region,type,image_url from source_singers where {sql} "
                          "order by sort_no limit ? offset ?", args + [limit, offset])
        total = self.one(f"select count(*) from source_singers where {sql}", args)[0] if self.con else 0
        return rows, total

    def singers_named(self, q, limit=2, famous=300):
        """名字或拼音和搜索词完全一样的热门歌手：按歌名搜时顺便给出歌手入口（zjl → 周杰伦）。
        只要前 famous 名，免得 qlx 冒出个冷门的秦立新挡在《七里香》前面。"""
        key = q.strip().upper()
        return self.query("select number,name,region,type,image_url from source_singers "
                          "where (upper(name)=? or pinyin=? or pinyin_full=?) and sort_no<? order by sort_no limit ?",
                          (key, key, key, famous, limit))

    def singer(self, number):
        return self.one("select number,name,region,type,image_url from source_singers where number=?", (number,))

    def singer_songs(self, number, offset, limit, language="", cached=frozenset()):
        """这个歌手的歌，热门的在前（管理页“每位最多 N 首”也就是下最热门的 N 首）。"""
        sql = (f"select {SONG_COLS}, s.sort_no from source_singer_songs ss join source_songs s on s.number=ss.song_number "
               "where ss.singer_number=?"); args = [number]
        if language: sql += " and s.language=?"; args.append(language)
        rows = self.ranked(self.query(sql, args), cached)
        return rows[offset:offset + limit] if limit else rows

    def singer_song_count(self, number):
        row = self.one("select count(*) from source_singer_songs where singer_number=?", (number,))
        return row[0] if row else 0

    # ---- 歌单 / 排行榜（type 3=歌单 4=排行榜）
    def playlists(self, kind=None):
        sql = "select number,package_type,name,song_count from source_playlists"; args = []
        if kind: sql += " where package_type=?"; args.append(kind)
        return self.query(sql + " order by package_type,sort_no", args)

    def playlist_songs(self, number, kind=None, offset=0, limit=0):
        sql = (f"select {SONG_COLS} from source_playlist_songs ps join source_songs s on s.number=ps.song_number "
               "where ps.playlist_number=?"); args = [number]
        if kind: sql += " and ps.package_type=?"; args.append(kind)
        sql += " order by ps.sort_no"
        if limit: sql += " limit ? offset ?"; args += [limit, offset]
        return self.query(sql, args)

    # ---- 语种
    def languages(self):
        return self.query("select language,count(*) from source_songs where language!='' "
                          "group by language order by count(*) desc")

    def language_songs(self, language, offset, limit, cached=frozenset()):
        rows = self.query(f"select {SONG_COLS}, s.sort_no from source_songs s where s.language=?", (language,))
        return self.ranked(rows, cached)[offset:offset + limit]


# ---------------- 曲库重建 ----------------
SCHEMA = [
    "CREATE TABLE source_songs(number TEXT PRIMARY KEY,name TEXT NOT NULL,singer TEXT NOT NULL,language TEXT NOT NULL,filename TEXT NOT NULL,version TEXT NOT NULL,duration INTEGER NOT NULL,voice_channel INTEGER NOT NULL,pinyin TEXT NOT NULL,pinyin_full TEXT NOT NULL,name_len INTEGER NOT NULL,sort_no INTEGER NOT NULL,album TEXT NOT NULL)",
    "CREATE TABLE source_singers(number TEXT PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL,type TEXT NOT NULL,pinyin TEXT NOT NULL,pinyin_full TEXT NOT NULL,image_url TEXT NOT NULL,sort_no INTEGER NOT NULL)",
    "CREATE TABLE source_playlists(number TEXT NOT NULL,package_type INTEGER NOT NULL,name TEXT NOT NULL,picture TEXT NOT NULL,description TEXT NOT NULL,sort_no INTEGER NOT NULL,song_count INTEGER NOT NULL,PRIMARY KEY(number,package_type))",
    "CREATE TABLE source_playlist_songs(playlist_number TEXT NOT NULL,package_type INTEGER NOT NULL,song_number TEXT NOT NULL,sort_no INTEGER NOT NULL,PRIMARY KEY(playlist_number,package_type,song_number))",
    "CREATE TABLE source_singer_songs(singer_number TEXT NOT NULL,song_number TEXT NOT NULL,PRIMARY KEY(singer_number,song_number))",
    "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE INDEX song_name ON source_songs(name)",
    "CREATE INDEX singer_name ON source_singers(name)",
    "CREATE INDEX singer_song ON source_singer_songs(song_number)",
    "CREATE INDEX song_order ON source_songs(sort_no)",
    "CREATE INDEX song_language ON source_songs(language,sort_no)",
    "CREATE INDEX song_length ON source_songs(name_len,sort_no)",
    "CREATE INDEX song_filename ON source_songs(filename)",
    "CREATE INDEX playlist_song ON source_playlist_songs(song_number,package_type)",
]
PAGE = 1000


class Pinyin:
    def __init__(self, path=os.path.join(HERE, "pinyin_chars.json")):
        with open(path, encoding="utf-8") as f: self.chars = json.load(f)

    def full(self, name):
        return "".join(self.chars.get(c, c) for c in name).upper()

    def initials(self, name):
        return "".join(self.chars.get(c, c)[:1] for c in name).upper()


def remote_version(api):
    r = api.songs(1, 1)
    return r["List"][0]["SongNumber"] + ":" + str(r["Page"]["RecordCount"])


def _page_all(request, consume):
    page, pages = 1, 1
    while page <= pages:
        result = request(page)
        pages = max(1, int((result.get("Page") or {}).get("PageCount") or 1))
        for i, row in enumerate(result.get("List") or []):
            consume(row, (page - 1) * PAGE + i, page, pages)
        page += 1


def rebuild(target, api, progress=lambda percent: None, pinyin=None):
    """从 i歌霸重建曲库到 target.sync，校验通过后替换 target；失败时保留旧曲库。返回新版本号。"""
    pinyin = pinyin or Pinyin()
    version = remote_version(api)
    temporary = target + ".sync"
    if os.path.exists(temporary): os.remove(temporary)
    db = sqlite3.connect(temporary)
    try:
        for sql in SCHEMA: db.execute(sql)
        insert = lambda table, values: db.execute(
            f"insert or ignore into {table}({','.join(values)}) values({','.join('?' * len(values))})", list(values.values()))

        def put_song(row, order):
            name = row.get("SongName") or ""
            insert("source_songs", {
                "number": str(row["SongNumber"]), "name": name, "singer": row.get("SingerName") or "",
                "language": row.get("LanguageName") or "", "filename": row.get("FileName") or "",
                "version": row.get("Version") or "", "duration": _int(row.get("Duration")),
                "voice_channel": _int(row.get("VoiceChannel")), "pinyin": pinyin.initials(name),
                "pinyin_full": pinyin.full(name), "name_len": sum(1 for c in name if c.isalnum()),
                "sort_no": order, "album": row.get("Album") or ""})

        _page_all(lambda p: api.songs(p, PAGE), lambda row, i, p, n: (put_song(row, i), progress(min(55, p * 55 // n))))
        singer_ids = {}

        def put_singer(row, i, _p, _n):
            name, mark, number = row.get("SingerName") or "", row.get("Mark") or "", str(row["SingerId"])
            singer_ids[name] = number
            insert("source_singers", {
                "number": number, "name": name,
                "region": next((r for r in ("大陆", "港台", "日韩", "欧美") if r in mark), "其他"),
                "type": "男" if "男" in mark else "女" if "女" in mark else "组合",
                "pinyin": pinyin.initials(name), "pinyin_full": pinyin.full(name),
                "image_url": row.get("MidPicture") or row.get("SmallPicture") or "", "sort_no": i})

        _page_all(lambda p: api.packages(1, p, PAGE), put_singer)
        progress(60)
        packages = []
        for kind in (3, 4):
            _page_all(lambda p, k=kind: api.packages(k, p, PAGE), lambda row, *_ , k=kind: packages.append((k, row)))
        for index, (kind, row) in enumerate(packages):
            number = str(row["PlayId" if kind == 3 else "RankId"])
            count = [0]

            def put_member(song, order, *_):
                put_song(song, 1_000_000 + index * PAGE + order)
                insert("source_playlist_songs", {"playlist_number": number, "package_type": kind,
                                                 "song_number": str(song["SongNumber"]), "sort_no": order})
                count[0] += 1

            _page_all(lambda p: api.songs(p, PAGE, {"Package": {"Type": kind, "Id": number}}), put_member)
            insert("source_playlists", {"number": number, "package_type": kind,
                                        "name": row.get("PlayTitle" if kind == 3 else "RankTitle") or "",
                                        "picture": row.get("Picture") or "", "description": row.get("Description") or "",
                                        "sort_no": index, "song_count": count[0]})
            progress(60 + (index + 1) * 30 // max(1, len(packages)))
        for number, singer in db.execute("select number,singer from source_songs").fetchall():
            for part in dict.fromkeys([singer] + re.split(r"[/、&]+", singer)):
                sid = singer_ids.get(part.strip())
                if sid: insert("source_singer_songs", {"singer_number": sid, "song_number": number})
        for key, value in (("provider", "igeba"), ("schema_version", "1"), ("remote_version", version)):
            insert("meta", {"key": key, "value": value})
        db.commit()
        if db.execute("pragma integrity_check").fetchone()[0] != "ok": raise RuntimeError("新曲库校验失败")
        if not db.execute("select count(*) from source_songs").fetchone()[0]: raise RuntimeError("新曲库是空的")
        progress(95)
    except BaseException:
        db.close()
        if os.path.exists(temporary): os.remove(temporary)
        raise
    db.close()
    os.replace(temporary, target)
    progress(100)
    return version
