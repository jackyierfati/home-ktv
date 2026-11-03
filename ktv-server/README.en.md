# ktv-server

[中文](README.md) | English

The Home KTV server: the request page, the admin page, the TV home screen, the queue, the download queue and the
catalog, all in this one Python program. Deployment: [../docs/DEPLOY.en.md](../docs/DEPLOY.en.md). Design:
[../docs/ARCHITECTURE.en.md](../docs/ARCHITECTURE.en.md).

## Running it directly (without Docker)

You only need Python 3.11+; there are no third-party dependencies:

```bash
python3 -m server.app --data ./data --media ./media --port 8787
```

- Request page: `http://<IP>:8787/`
- Admin page: `http://<IP>:8787/admin`
- TV home: `http://<IP>:8787/tv` (what the player shows; you can also open it in a computer browser)

Command-line options: `--threads` connections per song (default 64), `--parallel` songs downloaded at once (default 2),
`--token` requires an `X-Agent-Token` header on write requests (off by default).

The downloader also works on its own:

```bash
python3 server/igeba_dl.py search 晴天 --singer 周杰伦
python3 server/igeba_dl.py get 10005213 --out ./media
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests don't touch the internet: the downloader runs against a local fake CDN (which mimics i歌霸's Range behaviour),
and the player and Maidong are fakes too.
