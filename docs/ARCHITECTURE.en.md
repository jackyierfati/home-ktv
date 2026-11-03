# Design

[中文](ARCHITECTURE.md) | English

## Who does what

- **Guests**: open the request page on a phone or computer to find songs, request them and control playback. No login,
  and no need to know there's a NAS.
- **Admin**: the admin page bulk-downloads (by singer, playlist or chart), manages the download queue, updates the
  catalog and changes settings.
- **Server** (Docker on the NAS): the catalog, the queue, downloads and song files. All state lives here.
- **TV**: only plays. The player follows the server's state; it stores no songs and manages neither downloads nor the
  queue.
- **Microphones**: not part of this system; a mixer handles them.

```
Phone / computer ──HTTP──▶ ktv-server (port 8787)
                            ├─ Request page /, admin page /admin, TV home /tv
                            ├─ Queue (room.py) ──── long polling ───▶ TV player, phones
                            ├─ Download queue (jobs.py) ── downloader (igeba_dl.py) ── optional proxy ──▶ i歌霸
                            ├─ Catalog (catalog.py, SQLite, can be rebuilt online from i歌霸)
                            └─ Song files /media/<number> (Range supported) ◀── TV player
```

## Server (`ktv-server/server`)

Uses only the Python 3.11+ standard library (`http.server`, `sqlite3`, `urllib`, `threading`), with no third-party
dependencies.

| File | Contents |
|---|---|
| `app.py` | HTTP routes (JSON API and static pages), settings, wiring everything together |
| `room.py` | Song queue, the song playing now, playback state, activity feed, how many songs each person requested and finished; saved in `data/room.json` |
| `jobs.py` | Download queue: two priorities, saved to disk, resumes after a restart, re-downloads |
| `igeba_dl.py` | Multi-connection, chunked downloader; also usable on its own from the command line |
| `catalog.py` | Catalog queries, search ranking, rebuilding the catalog from i歌霸 |
| `igeba_api.py` | i歌霸 catalog API, used to rebuild the catalog |
| `player.py` | Optional: pass song requests and remote control on to Maidong on the TV |
| `repair.py` | Repairs files from the old downloader that are missing bytes |
| `web/` | Request page (`index`), TV home (`tv`), admin page (`admin`) |

Files under `data/`: `igeba_catalog.db` (catalog), `config.json` (settings, including the WiFi password), `room.json`
(queue and activity feed), `agent_state.json` (download queue), `repaired.json` (repair log).

### Queue and playback

- The server owns the queue. When a requested song isn't on the NAS yet, it joins the download queue with high
  priority. It stays in the queue and waits while songs behind it that are already downloaded play first; when the
  download finishes and nothing is playing, it starts automatically.
- A song being re-downloaded doesn't count as downloaded, so the old file is never played. The song playing right now
  can't be re-downloaded, because replacing its file would break playback on the TV.
- State carries a version number, and clients long-poll with `?since=<last version>&wait=3`: the response comes back as
  soon as the state changes, or after at most 3 seconds if nothing changed.
  - TV player: `GET /api/player/state`; it reports finished songs, errors and playback position with
    `POST /api/player/report`.
  - TV home: `GET /api/tv/state` (who requested each song, singer photos, download progress, activity feed, NAS status,
    WiFi QR code).
  - Phones: `GET /api/room`.
- Requests carry a nickname (`by`). The activity feed shows "小王 点了《晴天》" (Xiao Wang requested 晴天), and `users`
  counts how many songs each person requested and finished (for a future top-singers board).
- "我点的" (my songs): a successful request returns the entry's `id` and how many songs are ahead of it (`ahead`). The
  phone stores the id in localStorage, and songs requested under the same nickname count too. Every song in
  `/api/room` carries its length (`duration`) and the playing one its position (`position`), which the phone uses to
  estimate the wait in minutes.

### Downloader

i歌霸's CDN returns at most 2 MiB per request (larger Ranges get a 403). From overseas, a single connection to it is slow
and now and then stalls completely. So:

- Each song is split into 512 KiB pieces (`KTV_CHUNK_KIB`), downloaded over 64 connections at once by default
  (`--threads`).
- A piece with no data for 20 seconds is requested again; from the second failure on, a fresh download address is used,
  up to 6 attempts.
- Hedging the tail: when only a few pieces are left, a piece with no data for 6 seconds gets a second connection, and
  whichever finishes first wins, without waiting for the stuck connection to time out.
- The download-address API (`telnet.php`) sometimes never answers: the request is sent again every 5 seconds and the
  first reply wins.
- Progress is reported every second from the bytes actually received, so the pages show the real speed.
- i歌霸 treats the end of a Range as exclusive (ask for 10 bytes, get 9). Each piece asks for 1 extra byte and is
  trimmed; a short piece counts as a failure and is retried, so no empty bytes are left behind.
- Data is written to `<file name>.part` and moved into place with `os.replace` only once every byte is there; during a
  re-download the old file stays until the new one is complete.
- The download queue has two priorities: songs requested by guests go to the front, bulk jobs to the back. Songs already
  on disk are skipped, and the queue is saved to disk and resumes after a restart.
- Downloads use the `http_proxy` environment variable (which can point to a proxy with a mainland-China exit); requests
  to the home network never use the proxy.

### Search ranking

`catalog.ranked`:

1. Match quality: title or pinyin identical > starts with > contains.
2. Songs already on the NAS.
3. How many playlists and charts the song is on.
4. Singer popularity (i歌霸's singer list is itself ordered by popularity); for duets, the most popular singer counts.
5. Shorter titles first.

When searching by singer, it first looks at which singer of a duet matched and how popular they are, then at points 2
and 3 above. A singer's song list and the language lists use the same order, so the admin page's "at most N songs per
singer" downloads that singer's N most popular songs. When searching by title, popular singers (top 300) whose name or
pinyin matches exactly get a shortcut at the top of the results (`zjl` → 周杰伦's songs).

## TV player (`ktv-player`)

- Android TV app, Kotlin + Media3 ExoPlayer, minSdk 24.
- Songs are MKV: H.264 video plus two Vorbis audio tracks, the first with the original vocals and the second the backing
  track; switching between them means switching audio tracks.
- The app only plays the video. A transparent WebView on top shows the server's `/tv` page, which draws the home screen,
  the now-playing bar, the queue and the activity pop-ups. The `ui` value in `/api/tv/state` is the page files'
  modification time, so the TV reloads by itself when the page changes, with no app reinstall.
- QR codes on the page are generated by the app with ZXing (`KtvNative.qr(text, size)`); the remote's ← and Back keys
  call the page's `ktv.togglePanel()` / `ktv.hidePanel()`.
- Remote: OK pauses or resumes, → twice skips, ↑↓ switch vocals or backing track, ← shows the queue and QR codes, long
  press OK changes the server address. Back only closes the list; Home exits (to avoid accidental exits).
- When you leave and come back, playback resumes 2 seconds before the position the server recorded.

## Request page and admin page

- The request page (`/`) is built for phones first and becomes two columns on computers and iPads. It only has what
  guests need: finding songs, requesting, the queue and remote control.
- The admin page (`/admin`): bulk downloads by singer, playlist or chart, the download queue (pause, retry, clear),
  re-downloading broken songs, catalog updates, and settings for the name, WiFi and player.
- Neither page has a password; they are for your home network only.
