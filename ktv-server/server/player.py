"""第一阶段的播放桥接：点歌、已点和遥控都转给电视上的麦动（本机局域网接口 :8765）。"""
import json, urllib.error, urllib.parse, urllib.request

# 局域网请求不能走 VPN 代理：不读 http_proxy 环境变量。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PlayerError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class MaidongBridge:
    def __init__(self, base_url):
        self.base_url = base_url            # 可调用对象，返回当前设置的麦动地址

    def request(self, method, path, body=None, timeout=4):
        base = (self.base_url() or "").rstrip("/")
        if not base: raise PlayerError("还没有设置电视播放器地址", 503)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try: payload = json.loads(e.read().decode("utf-8") or "{}")
            except Exception: payload = {}
            raise PlayerError(payload.get("error") or f"电视返回错误（HTTP {e.code}）", e.code)
        except (urllib.error.URLError, OSError, ValueError):
            raise PlayerError("电视没有连接：请确认电视已打开麦动", 503)

    def state(self):
        return self.request("GET", "/api/v1/state")

    def queue(self):
        return self.request("GET", "/api/v1/queue").get("queue") or []

    def order(self, number):
        return self.request("POST", "/api/v1/queue", {"songId": f"igeba:song:{number}"})

    def move_to_next(self, song_id):
        return self.request("PATCH", "/api/v1/queue/" + urllib.parse.quote(song_id, safe=""), {})

    def remove(self, song_id):
        return self.request("DELETE", "/api/v1/queue/" + urllib.parse.quote(song_id, safe=""))

    def action(self, action):
        return self.request("POST", "/api/v1/player/actions", {"action": action})

    def audio(self, values):
        return self.request("PATCH", "/api/v1/player/audio", values)


def song_number(item):
    """麦动已点里的歌 → i歌霸编号（新版麦动直接给 number，旧版只有 igeba:song:<编号>）。"""
    number = item.get("number")
    if number: return str(number)
    song_id = item.get("id") or ""
    return song_id.rsplit(":", 1)[-1] if song_id.startswith("igeba:song:") else None
