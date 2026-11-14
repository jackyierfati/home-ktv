"""i歌霸 歌源：账号登录和下载线路。

2026-11 起，i歌霸 的地址接口（telnet.php GET_SONGADDR）只给登录了的账号返回歌曲地址，没登录一律 403。
登录协议和麦动 2.1.6 ~ 2.1.12 一样：用官方客户端的 User-Agent POST DO_LOGIN，带着返回的 Cookie 和
真实的 UserId 去取地址；会话过期时地址接口又会 403，这时用存着的账号密码重新登录一次再取。

地址给的文件服务器（g8file.php）只在账号“在线”时、只给这个账号“已点”的歌，别的一律 403。所以取地址前
先像官方客户端那样报在线（NOTIFY_MYSTATUS，一分钟最多一次）和已点列表（NOTIFY_MYORDER）。
Orders 是整个已点列表、会替换掉上一次的，要带上还在下载的歌，不然它们中途重新取地址会 403。
一个账号同一时间只认最后登录的那个地方；在别处（官方客户端、麦动）登录后，我们的会话还能用，
但报在线时会收到“您的账号在另一地点登录”，管理页上会提示。

下载线路：一行一个，按顺序试，比如 VPN 的 HTTP 代理、另一个代理、最后 direct（直连）。
一条线路取地址失败就换下一条；失败过的线路 10 分钟内排到最后。一首歌用哪条线路取到地址，
分段下载也走同一条线路。
"""
import http.cookiejar, json, os, queue, threading, time, urllib.error, urllib.parse, urllib.request

ADDRESS_URL = "http://app.ige8.net/telnet.php"
UA = "Mozilla/3.0 (compatible; Indy Library)"     # i歌霸 官方客户端的 User-Agent
DIRECT = "direct"
SKIP_FAILED = 600                                   # 失败过的线路这么多秒内先排到最后
RELOGIN_GAP = 60                                    # 两次自动重新登录至少隔这么久
ORDERS_KEEP = 50                                    # 报给 i歌霸 的已点列表最多带这么多首（最近下的）
ONLINE_EVERY = 60                                   # 报在线的间隔（秒）


class SourceError(RuntimeError):
    pass


class LoginError(SourceError):
    """没有账号，或者 i歌霸 不认账号密码：换线路也没用。"""


class Forbidden(Exception):
    """地址接口返回 403：没登录或者会话过期。"""


def parse_routes(value, default=()):
    """设置里的线路（列表或多行文本）→ 去重后的列表；“直连”也算 direct。空的时候用 default。"""
    if isinstance(value, str): value = value.splitlines()
    routes = []
    for item in value or []:
        item = str(item).strip().rstrip("/")
        if not item: continue
        if item.lower() in (DIRECT, "直连"): item = DIRECT
        if item not in routes: routes.append(item)
    return routes or list(default)


def check_route(route):
    if route == DIRECT: return
    parsed = urllib.parse.urlparse(route)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError(f"线路格式不对：{route}（要写成 http://地址:端口，或者 direct）")


def default_routes():
    """没设置过线路时：有 http_proxy 环境变量就先走它，再直连。"""
    proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or ""
    return parse_routes([proxy, DIRECT])


def race(call, attempts=4, stagger=5, timeout=20):
    """地址接口平时 2 秒左右，偶尔几十秒不回：每 stagger 秒没结果就再并发发一个（最多 attempts 个），
    谁先成功用谁。403 说明要登录，马上抛 Forbidden，不再等别的请求。"""
    results = queue.Queue()
    def one():
        try: results.put((True, call()))
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
        if isinstance(value, urllib.error.HTTPError) and value.code == 403: raise Forbidden() from value
        failed, last = failed + 1, value
    raise SourceError(f"地址接口失败（{failed} 次）: {last or '超时'}")


def post(opener, url, command, params, timeout=20):
    body = urllib.parse.urlencode({"VER": "10.0", "CMD": command, "PARAMS": json.dumps(params, ensure_ascii=False)}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": UA})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def short(error):
    if isinstance(error, Forbidden): return "HTTP 403（账号会话无效）"
    if isinstance(error, urllib.error.HTTPError): return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError): return f"连不上：{error.reason}"
    return str(error)[:160] or type(error).__name__


class Account:
    """自己的 i歌霸 账号。会话（Cookie、UserId）存在 data 目录，重启不用重新登录。
    credentials() -> (账号, 密码)；存着密码才能在会话过期时自动重新登录。"""

    def __init__(self, data_dir, credentials, address_url=ADDRESS_URL):
        self.credentials, self.address_url = credentials, address_url
        self.info_path = os.path.join(data_dir, "igeba_session.json")
        self.cookies = http.cookiejar.LWPCookieJar(os.path.join(data_dir, "igeba_cookies.txt"))
        self.lock = threading.Lock()
        self.info, self.error, self.login_at = {}, None, 0
        self.elsewhere = None                          # 报在线时 i歌霸 说这个账号在别处登录着
        try:
            with open(self.info_path, encoding="utf-8") as f: self.info = json.load(f)
            self.cookies.load(ignore_discard=True)
        except (OSError, ValueError, http.cookiejar.LoadError):
            pass

    @property
    def user_id(self):
        return self.info.get("user_id") or 0

    def has_credentials(self):
        user, password = self.credentials()
        return bool(user and password)

    def login(self, build, user=None, password=None):
        """build(cookie 罐) -> opener，决定走哪条线路。成功返回昵称；i歌霸 不认账号密码抛 LoginError，
        连不上抛 SourceError。"""
        if user is None: user, password = self.credentials()
        if not user or not password: raise LoginError("还没登录 i歌霸 账号：请在管理页「曲库与设置」里登录")
        with self.lock:
            fresh = http.cookiejar.CookieJar()              # 登录用干净的 Cookie 罐，成功了才换掉旧会话
            params = {"UserName": user.strip(), "Password": password,
                      "ExeInfos": json.dumps({"HomeKTV": "3"}), "EnvInfos": json.dumps({"Os": "Linux"}),
                      "PlayerInfos": "{}", "ReferId": 0}
            try:
                result = post(build(fresh), self.address_url, "DO_LOGIN", params)
            except Exception as e:
                self.error = f"登录请求失败：{short(e)}"
                raise SourceError(self.error) from e
            info = result.get("UserInfo") or {}
            if result.get("Code", -1) <= 0 or not info.get("UserId"):
                self.error = f"登录失败：{result.get('Reason') or '账号或密码不对'}"
                raise LoginError(self.error)
            vip = info.get("Vip") or {}
            self.info = {"user_id": info["UserId"], "nickname": info.get("NickName") or user.strip(),
                         "user": user.strip(), "vip": vip.get("VipName") or "", "vip_days": vip.get("RemainDays") or 0}
            self.cookies.clear()
            for cookie in fresh: self.cookies.set_cookie(cookie)
            self.error, self.login_at, self.elsewhere = None, time.time(), None
            self._save()
            return self.info["nickname"]

    def relogin(self, build):
        """地址接口 403 时调用：有存着的密码、而且不是刚登录过，就重新登录一次。返回是否登录了。"""
        if not self.has_credentials() or time.time() - self.login_at < RELOGIN_GAP: return False
        self.login(build)
        return True

    def logout(self):
        with self.lock:
            self.info, self.error, self.login_at = {}, None, 0
            self.cookies.clear()
            for path in (self.info_path, self.cookies.filename):
                try: os.remove(path)
                except OSError: pass

    def _save(self):
        try:
            with open(self.info_path + ".tmp", "w", encoding="utf-8") as f: json.dump(self.info, f, ensure_ascii=False)
            os.replace(self.info_path + ".tmp", self.info_path)
            self.cookies.save(ignore_discard=True)
        except OSError as e:
            print("i歌霸 会话保存失败:", e)

    def status(self):
        return {"logged_in": bool(self.user_id), "user": self.info.get("user") or self.credentials()[0] or "",
                "nickname": self.info.get("nickname") or "", "vip": self.info.get("vip") or "",
                "vip_days": self.info.get("vip_days") or 0, "auto_relogin": self.has_credentials(), "error": self.error,
                "elsewhere": self.elsewhere}


class Routes:
    """下载线路。get_list() -> 线路列表；每条线路一个 opener，共用账号的 Cookie。"""

    def __init__(self, get_list, cookies):
        self.get_list, self.cookies = get_list, cookies
        self.lock = threading.Lock()
        self.openers, self.state = {}, {}

    @staticmethod
    def build(route, cookies):
        """走 route 的新 opener；direct 不读 http_proxy 环境变量。"""
        proxies = {} if route == DIRECT else {"http": route, "https": route}
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), urllib.request.HTTPCookieProcessor(cookies))

    def opener(self, route):
        with self.lock:
            if route not in self.openers: self.openers[route] = self.build(route, self.cookies)
            return self.openers[route]

    def ordered(self):
        """按设置的顺序；最近失败过（之后没成功过）的排到最后。"""
        now, routes = time.time(), self.get_list()
        def recently_failed(route):
            s = self.state.get(route, {})
            return s.get("failed", 0) > s.get("ok", 0) and now - s["failed"] < SKIP_FAILED
        return [r for r in routes if not recently_failed(r)] + [r for r in routes if recently_failed(r)]

    def ok(self, route, seconds):
        with self.lock: self.state.setdefault(route, {}).update(ok=time.time(), seconds=round(seconds, 1), error=None)

    def failed(self, route, error):
        with self.lock: self.state.setdefault(route, {}).update(failed=time.time(), error=error)

    def status(self):
        with self.lock:
            return [{"route": r, **{k: self.state.get(r, {}).get(k) for k in ("ok", "failed", "seconds", "error")}}
                    for r in self.get_list()]


def from_data_dir(data_dir):
    """命令行工具（igeba_dl、repair）用：账号和线路读服务端 data 目录里的 config.json，
    环境变量 KTV_IGEBA_USER / KTV_IGEBA_PASSWORD 优先。"""
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8") as f: config = json.load(f)
    except (OSError, ValueError):
        config = {}
    creds = (os.environ.get("KTV_IGEBA_USER") or config.get("igeba_user") or "",
             os.environ.get("KTV_IGEBA_PASSWORD") or config.get("igeba_password") or "")
    return Source(data_dir, lambda: creds, lambda: parse_routes(config.get("download_routes"), default_routes()))


class Source:
    """把账号和线路接起来：song_url(编号) -> (地址, 这首歌后面分段下载用的 opener)。"""

    def __init__(self, data_dir, credentials, get_routes, address_url=ADDRESS_URL):
        self.address_url = address_url
        self.account = Account(data_dir, credentials, address_url)
        self.routes = Routes(get_routes, self.account.cookies)
        self.orders, self.orders_lock = [], threading.Lock()
        self.online_at, self.online_since, self.online_lock = 0, 0, threading.Lock()

    def keep_online(self, opener, timeout=20, force=False):
        """像官方客户端那样报“在线”，文件服务器只在账号在线时给文件。一分钟最多报一次，force 时马上报。"""
        with self.online_lock:
            now = time.time()
            if not force and now - self.online_at < ONLINE_EVERY: return
            self.online_since = self.online_since or now
            params = {"UserId": self.account.user_id, "IsOnline": 1, "OnlSecond": int(now - self.online_since),
                      "VipSecond": 0, "IdleSecond": 0, "PlaySecond": 0, "CacheTraffic": 0, "DownTraffic": 0, "Request": 1}
            try:
                j = post(opener, self.address_url, "NOTIFY_MYSTATUS", params, timeout)
            except urllib.error.HTTPError as e:
                if e.code == 403: raise Forbidden() from e
                raise
            self.online_at = time.time()
            disabled = j.get("DisableOnline") or {}
            self.account.elsewhere = (disabled.get("Reason") or "这个账号在别处登录着") if disabled.get("Value") else None

    def notify_order(self, opener, number, timeout=20):
        """把这首歌加进报给 i歌霸 的已点列表，文件服务器才肯给文件。"""
        with self.orders_lock:
            if number in self.orders: self.orders.remove(number)
            self.orders.append(number)
            del self.orders[:-ORDERS_KEEP]
            orders = ",".join(self.orders)
        try:
            j = post(opener, self.address_url, "NOTIFY_MYORDER",
                     {"UserId": self.account.user_id, "Action": 1, "SongNumbers": number, "Orders": orders}, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 403: raise Forbidden() from e
            raise
        if isinstance(j.get("Result"), int) and j["Result"] < 0: raise SourceError(j.get("Description") or "报已点失败")

    def address_once(self, opener, number, timeout=20):
        j = post(opener, self.address_url, "GET_SONGADDR", {"UserId": self.account.user_id, "SongNumber": number}, timeout)
        rights = j.get("Rights") or {}
        if j.get("Result", 0) < 0 or rights.get("Code") not in (0, 1):
            raise SourceError(j.get("Description") or rights.get("Reason") or "歌源拒绝")
        url = j.get("FileUrl") or ""
        if not url.startswith("http"): raise SourceError("未返回播放地址")
        return url

    def song_url(self, number, attempts=4, stagger=5, timeout=20, refresh=False):
        """refresh：文件服务器刚回过 403（比如在线状态过期了），这次马上重新报在线。"""
        if not self.account.user_id and not self.account.has_credentials():
            raise LoginError("还没登录 i歌霸 账号：请在管理页「曲库与设置」里登录")
        errors = []
        for route in self.routes.ordered():
            opener, began = self.routes.opener(route), time.time()
            build = lambda jar, route=route: self.routes.build(route, jar)
            def ask(force):
                self.keep_online(opener, timeout, force)         # 先报在线和已点，文件服务器才肯给这首
                self.notify_order(opener, number, timeout)
                return race(lambda: self.address_once(opener, number, timeout), attempts, stagger, timeout)
            try:
                if not self.account.user_id: self.account.login(build)
                try:
                    url = ask(refresh)
                except Forbidden:
                    if not self.account.relogin(build): raise
                    url = ask(True)
                self.routes.ok(route, time.time() - began)
                return url, opener
            except LoginError:
                raise                                       # 账号密码不对：换线路也没用
            except Exception as e:
                errors.append(f"{route}：{short(e)}"); self.routes.failed(route, short(e))
        raise SourceError("所有线路都取不到地址 — " + "；".join(errors))

    def login(self, user, password):
        """管理页点「登录」：按线路顺序试，第一条连得上的线路说了算。"""
        last = None
        for route in self.routes.ordered():
            try:
                return self.account.login(lambda jar, route=route: self.routes.build(route, jar), user, password)
            except LoginError:
                raise
            except SourceError as e:
                last = e; self.routes.failed(route, short(e.__cause__ or e))
        raise last or SourceError("没有可用的下载线路")

    def test_routes(self, sample="10005213"):
        """管理页「测试线路」：每条线路能不能连上 i歌霸、从哪个国家出去；登录了的话再取一次地址、
        读一小段歌曲文件（文件服务器 403 也能在这里看出来）。"""
        out = []
        for route in self.routes.get_list():
            opener, item = self.routes.opener(route), {"route": route}
            began = time.time()
            try:
                opener.open(urllib.request.Request(self.address_url.rsplit("/", 1)[0] + "/", headers={"User-Agent": UA}),
                            timeout=15).read(0)
                item["reach"] = round(time.time() - began, 1)
            except Exception as e:
                item["error"] = short(e); out.append(item); continue
            try:
                ip = json.loads(opener.open("https://ipinfo.io/json", timeout=10).read())
                item["exit"] = f"{ip.get('country', '?')} {ip.get('city', '')}".strip()
            except Exception:
                item["exit"] = "?"
            if self.account.user_id:
                began = time.time()
                try:
                    self.keep_online(opener, 20)
                    self.notify_order(opener, sample, 20)
                    url = self.address_once(opener, sample, 20)
                    item["address"] = round(time.time() - began, 1)
                    began = time.time()
                    req = urllib.request.Request(url, headers={"Range": "bytes=0-63", "Accept-Encoding": "identity",
                                                               "User-Agent": UA})
                    with opener.open(req, timeout=20) as r: r.read(64)
                    item["file"] = round(time.time() - began, 1)
                except Exception as e:
                    item["error"] = ("取文件：" if "address" in item else "取地址：") + short(e)
            out.append(item)
        return out
