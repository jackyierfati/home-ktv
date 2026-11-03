import os, shutil, sqlite3, unittest

from server import catalog
from server.catalog import Catalog
from tests.helpers import make_catalog, temp_dirs


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.root, self.data, _ = temp_dirs()
        self.path = os.path.join(self.data, "igeba_catalog.db")
        make_catalog(self.path)
        self.catalog = Catalog(self.path)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_exact_title_comes_first_and_pinyin_matches(self):
        self.assertEqual(["一半", "一人一半"], [r[1] for r in self.catalog.search_songs("一半", 0, 10)])
        self.assertEqual(["一半", "一人一半"], [r[1] for r in self.catalog.search_songs("yb", 0, 10)])
        self.assertEqual(["江南"], [r[1] for r in self.catalog.search_songs("jn", 0, 10)])

    def test_singer_search_covers_duets(self):
        names = [r[1] for r in self.catalog.search_by_singer("XZQ", 0, 10)]
        self.assertEqual(["一半", "合唱"], names)

    def test_hot_follows_the_hot_chart_order(self):
        self.assertEqual(["山丘", "一半"], [r[1] for r in self.catalog.hot(0, 10)])

    def test_singers_filter_and_count(self):
        rows, total = self.catalog.singers("", "港台", "", 0, 10)
        self.assertEqual(2, total)
        self.assertEqual({"林俊杰", "李宗盛"}, {r[1] for r in rows})
        self.assertEqual(2, self.catalog.singer_song_count("885"))

    def test_playlists_and_languages(self):
        self.assertEqual(["江南"], [r[1] for r in self.catalog.playlist_songs("51890", 3)])
        self.assertEqual(("国语", 4), self.catalog.languages()[0])
        self.assertEqual(["天命最高"], [r[1] for r in self.catalog.language_songs("粤语", 0, 10)])

    def test_search_puts_popular_and_nas_songs_first(self):
        db = sqlite3.connect(self.path)
        db.execute("insert into source_singers values('1066','周杰伦','港台','男','ZJL','ZHOUJIELUN','',-1)")
        songs = (("01137004", "七天", "蔡义德", "QT", 10), ("10004942", "企图", "袁惟仁", "QT", 11),
                 ("10005213", "晴天", "周杰伦", "QT", 12), ("10099999", "晴天娃娃", "江美琪", "QTWW", 9))
        for number, name, singer, py, order in songs:
            db.execute("insert into source_songs values(?,?,?,'国语',?,'',240,0,?,?,?,?,'')",
                       (number, name, singer, number + ".mkv", py, py, len(name), order))
        db.commit()
        self.catalog.reopen()
        names = lambda cached=frozenset(): [r[1] for r in self.catalog.search_songs("qt", 0, 10, cached)]
        self.assertEqual(["晴天", "七天", "企图", "晴天娃娃"], names())                 # 完全对上的里面，热门歌手的在前
        self.assertEqual(["企图", "晴天", "七天", "晴天娃娃"], names({"10004942.mkv"}))  # NAS 上已经有的更靠前
        db.execute("insert into source_playlist_songs values('51890',3,'01137004',1)")
        db.commit(); db.close()
        self.catalog.reopen()
        self.assertEqual(["七天", "晴天", "企图", "晴天娃娃"], names())                 # 上了歌单的也靠前
        self.assertEqual(["周杰伦"], [r[1] for r in self.catalog.singers_named("zjl")])
        self.assertEqual([], self.catalog.singers_named("zj"))

    def test_singer_search_ranks_by_the_singer_that_matched(self):
        # 合唱《合唱》里有更热门的薛之谦，但搜的是林俊杰，就按林俊杰排
        self.assertEqual(["江南", "合唱"], [r[1] for r in self.catalog.search_by_singer("LJJ", 0, 10)])
        self.assertEqual(["江南", "合唱"], [r[1] for r in self.catalog.singer_songs("521", 0, 0)])
        self.assertEqual(["合唱"], [r[1] for r in self.catalog.singer_songs("521", 1, 5)])

    def test_missing_catalog_is_empty_not_an_error(self):
        empty = Catalog(os.path.join(self.data, "missing.db"))
        self.assertEqual([], empty.search_songs("一半", 0, 10))
        self.assertIsNone(empty.lookup("20019055"))


class FakeApi:
    """两页歌曲、一页歌手、一个歌单和一个排行榜。"""

    def __init__(self):
        self.calls = []
        catalog.PAGE = 2

    def songs(self, page, size, filter=None):
        self.calls.append(("songs", page, filter))
        if filter:
            members = {3: ["1", "3"], 4: ["2"]}[filter["Package"]["Type"]]
            return {"Page": {"PageCount": 1, "RecordCount": len(members)}, "List": [self.song(n) for n in members]}
        pages = [["3", "2"], ["1"]]
        if size == 1: return {"Page": {"PageCount": 3, "RecordCount": 3}, "List": [self.song("3")]}
        return {"Page": {"PageCount": 2, "RecordCount": 3}, "List": [self.song(n) for n in pages[page - 1]]}

    def song(self, n):
        return {"SongNumber": n, "SongName": {"1": "一半", "2": "江南", "3": "合唱"}[n],
                "SingerName": {"1": "薛之谦", "2": "林俊杰", "3": "薛之谦/林俊杰"}[n],
                "LanguageName": "国语", "FileName": f"2{n}.mkv", "Duration": "240", "VoiceChannel": 0}

    def packages(self, kind, page, size):
        if kind == 1:
            return {"Page": {"PageCount": 1}, "List": [
                {"SingerId": 885, "SingerName": "薛之谦", "Mark": "大陆男歌手", "MidPicture": "http://x/885.jpg"},
                {"SingerId": 521, "SingerName": "林俊杰", "Mark": "港台男歌手"}]}
        if kind == 3: return {"Page": {"PageCount": 1}, "List": [{"PlayId": 7, "PlayTitle": "合唱歌单"}]}
        return {"Page": {"PageCount": 1}, "List": [{"RankId": 9, "RankTitle": "热歌榜（测试）"}]}


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.root, self.data, _ = temp_dirs()
        self.path = os.path.join(self.data, "igeba_catalog.db")
        self.old_page = catalog.PAGE

    def tearDown(self):
        catalog.PAGE = self.old_page
        shutil.rmtree(self.root, ignore_errors=True)

    def test_rebuild_builds_every_table(self):
        progress = []
        version = catalog.rebuild(self.path, FakeApi(), progress.append)
        self.assertEqual("3:3", version)
        self.assertEqual(100, progress[-1])
        c = Catalog(self.path)
        self.assertEqual(3, c.song_count())
        self.assertEqual("3:3", c.meta()["remote_version"])
        self.assertEqual(["合唱", "一半"], [r[1] for r in c.singer_songs("885", 0, 0)])  # 合唱拆开歌手
        self.assertEqual(("885", "薛之谦", "大陆", "男", "http://x/885.jpg"), c.singer("885"))
        self.assertEqual(["一半", "合唱"], [r[1] for r in c.playlist_songs("7", 3)])
        self.assertEqual(["江南"], [r[1] for r in c.hot(0, 10)])
        self.assertTrue(c.search_songs("YB", 0, 10))                          # 生成了拼音
        self.assertFalse(os.path.exists(self.path + ".sync"))

    def test_failed_rebuild_keeps_the_old_catalog(self):
        make_catalog(self.path)

        class Broken(FakeApi):
            def packages(self, kind, page, size): raise RuntimeError("歌源超时")

        with self.assertRaises(RuntimeError):
            catalog.rebuild(self.path, Broken())
        self.assertEqual(6, Catalog(self.path).song_count())
        self.assertFalse(os.path.exists(self.path + ".sync"))


if __name__ == "__main__":
    unittest.main()
