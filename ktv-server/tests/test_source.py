import os, shutil, unittest

from server import igeba_source
from server.igeba_source import DIRECT, LoginError, Source, SourceError
from tests.helpers import FakeIgeba, temp_dirs

DEAD_PROXY = "http://127.0.0.1:9"          # 没有东西在听：连不上


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.root, self.data, _ = temp_dirs()
        self.igeba = FakeIgeba()
        # 环境里的 no_proxy 会让发往 127.0.0.1 的请求绕过代理，测不出“代理连不上”
        self.saved_env = {k: os.environ.pop(k) for k in ("no_proxy", "NO_PROXY") if k in os.environ}
        self.creds, self.routes = ("u", "p"), [DIRECT]

    def tearDown(self):
        os.environ.update(self.saved_env)
        self.igeba.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def source(self):
        return Source(self.data, lambda: self.creds, lambda: self.routes, address_url=self.igeba.url)

    def song_url(self, source, number="10005213"):
        return source.song_url(number, stagger=0.2, timeout=3)[0]

    def test_needs_an_account(self):
        self.creds = ("", "")
        with self.assertRaisesRegex(LoginError, "登录"):
            self.song_url(self.source())
        self.assertEqual(0, self.igeba.addresses)

    def test_logs_in_and_keeps_the_session_across_restarts(self):
        source = self.source()
        self.assertEqual("http://cdn/10005213.mkv", self.song_url(source))
        self.assertEqual(1, self.igeba.logins)
        status = source.account.status()
        self.assertEqual((True, "小王", "u", True), (status["logged_in"], status["nickname"], status["user"], status["auto_relogin"]))
        again = self.source()                                       # 服务端重启
        self.assertEqual("http://cdn/1.mkv", self.song_url(again, "1"))
        self.assertEqual(1, self.igeba.logins)                     # Cookie 存下来了，不用再登录

    def test_relogs_in_once_when_the_session_expires(self):
        source = self.source()
        self.song_url(source)
        self.igeba.expire()
        source.account.login_at -= 120                              # 上次登录已经是两分钟前
        self.assertEqual("http://cdn/10005213.mkv", self.song_url(source))
        self.assertEqual(2, self.igeba.logins)

    def test_does_not_keep_logging_in_when_403_persists(self):
        self.igeba.deny = True
        source = self.source()
        with self.assertRaisesRegex(SourceError, "403"):
            self.song_url(source)
        with self.assertRaisesRegex(SourceError, "403"):
            self.song_url(source)
        self.assertEqual(1, self.igeba.logins)                     # 刚登录过，不再重复登录

    def test_wrong_password_is_reported_without_trying_other_routes(self):
        self.creds, self.routes = ("u", "wrong"), [DIRECT, DEAD_PROXY]
        with self.assertRaisesRegex(LoginError, "密码错误"):
            self.song_url(self.source())
        self.assertEqual(1, self.igeba.logins)

    def test_falls_back_to_the_next_route_and_moves_the_bad_one_last(self):
        self.routes = [DEAD_PROXY, DIRECT]
        source = self.source()
        self.assertEqual("http://cdn/10005213.mkv", self.song_url(source))
        status = {r["route"]: r for r in source.routes.status()}
        self.assertIn("连不上", status[DEAD_PROXY]["error"])
        self.assertIsNotNone(status[DIRECT]["ok"])
        self.assertEqual([DIRECT, DEAD_PROXY], source.routes.ordered())

    def test_admin_login_and_logout(self):
        source = self.source()
        self.assertEqual("小王", source.login("u", "p"))
        with self.assertRaisesRegex(LoginError, "密码错误"):
            source.login("u", "nope")
        source.account.logout()
        self.creds = ("", "")
        self.assertFalse(self.source().account.status()["logged_in"])   # 存的会话也删掉了


class RouteSettingTest(unittest.TestCase):
    def test_parse_and_check(self):
        self.assertEqual(["http://a:1", DIRECT], igeba_source.parse_routes("http://a:1/\n直连\n\nhttp://a:1"))
        self.assertEqual([DIRECT], igeba_source.parse_routes("", default=[DIRECT]))
        igeba_source.check_route("http://igeba-vpn:8888")
        for bad in ("ftp://x:1", "igeba-vpn:8888", "http://x:1/path"):
            with self.assertRaises(ValueError): igeba_source.check_route(bad)


if __name__ == "__main__":
    unittest.main()
