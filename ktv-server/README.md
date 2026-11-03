# ktv-server

中文 | [English](README.en.md)

家庭KTV 的服务端：点歌网页、管理页、电视主页、已点、下载队列、曲库，都在这一个 Python 程序里。
部署见 [../docs/DEPLOY.md](../docs/DEPLOY.md)，设计见 [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。

## 直接运行（不用 Docker）

只需要 Python 3.11+，没有第三方依赖：

```bash
python3 -m server.app --data ./data --media ./media --port 8787
```

- 点歌网页：`http://<IP>:8787/`
- 管理页：`http://<IP>:8787/admin`
- 电视主页：`http://<IP>:8787/tv`（播放器里显示的就是它，用电脑浏览器打开也能看）

命令行参数：`--threads` 每首歌几路连接（默认 64），`--parallel` 同时下几首（默认 2），
`--token` 给写操作加一个 `X-Agent-Token` 头校验（默认不校验）。

下载器也能单独用：

```bash
python3 server/igeba_dl.py search 晴天 --singer 周杰伦
python3 server/igeba_dl.py get 10005213 --out ./media
```

## 测试

```bash
python3 -m unittest discover -s tests -t .
```

测试不访问外网：下载器用本地的假 CDN（模拟 i歌霸 的 Range 行为），播放器和麦动也是假的。
