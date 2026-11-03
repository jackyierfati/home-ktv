"""i歌霸目录接口（cloud.php），只用于重建曲库。移植自麦动 App 的 IgebaApiClient。"""
import json, urllib.parse, urllib.request

CLOUD_URL = "http://app.ige8.net/cloud.php"
UA = "Mozilla/5.0 (Linux; Android 12; TV) MaidongKTV"


def post(command, params, timeout=40):
    body = urllib.parse.urlencode({"VER": "6", "CMD": command, "PARAMS": json.dumps(params, ensure_ascii=False)}).encode()
    req = urllib.request.Request(CLOUD_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read().decode("utf-8"))
    if isinstance(result.get("Result"), int) and result["Result"] < 0:
        raise RuntimeError(result.get("Description") or "歌源拒绝请求")
    return result


def songs(page, page_size, filter=None):
    params = {"UserId": 0, "Filter": filter or {}, "Page": {"PageNo": page, "PageSize": page_size}}
    # 默认的发布时间排序有大量并列值，跨页同步全库时会重复或漏歌。
    if not filter and page_size == 1000: params["Order"] = [{"SongName": 0}]
    return post("GET_SONGS", params)


def packages(kind, page, page_size):
    return post("GET_PACGS", {"PackageType": kind, "Filter": "", "Page": {"PageNo": page, "PageSize": page_size}})


