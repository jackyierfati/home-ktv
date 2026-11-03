# Deployment

[中文](DEPLOY.md) | English

The server is a single Docker container: the official `python:3.12-slim` image with the `ktv-server/` folder mounted
read-only. There is no image to build. Any machine that runs Docker Compose will do.

## First install

```bash
git clone https://github.com/jackyierfati/home-ktv.git
cd home-ktv
cp .env.example .env
docker compose up -d
```

The settings you'll usually change in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `KTV_MEDIA` | `./media` | Folder for song files. Each song is 50 to 80 MB, so put it on a big disk |
| `KTV_DATA` | `./data` | Catalog, settings, queue and download queue |
| `KTV_PORT` | `8787` | Port for the request page, the admin page and the TV |
| `KTV_NAME` | `家庭KTV` | Name shown on the TV home screen and the request page (can also be changed on the admin page) |
| `KTV_THREADS` | `64` | Connections per song |
| `KTV_PARALLEL` | `2` | Songs downloaded at the same time |
| `KTV_PROXY` | empty | HTTP proxy used for downloads, see the next section |

Then:

1. Open `http://<this machine's IP>:8787/admin` → 「曲库与设置」(catalog and settings) → 「更新曲库」(update catalog).
   The catalog has over 80,000 songs, so the first update takes a few minutes.
2. On the same page, fill in 「名字」(name) and, optionally, the WiFi details; the TV home screen then also shows a
   「连 WiFi」(join WiFi) QR code.
3. Install the player on the TV, see [ktv-player/README.en.md](../ktv-player/README.en.md).

To check:

```bash
curl http://127.0.0.1:8787/health          # {"ok": true, "version": "3"}
docker logs --tail 50 home-ktv
```

## Route downloads through a mainland-China exit (when you're abroad)

The songs are hosted in mainland China. From overseas, direct downloads are often slow, or the download address can't
be fetched at all. Sending only the downloads through a mainland-China exit fixes this, and nothing else on your home
network is affected.

**If you already have an HTTP proxy with a mainland-China exit**: set `KTV_PROXY=http://<proxy address>:<port>` in
`.env`, then run `docker compose up -d`.

**With WireGuard** (any VPN that has mainland-China servers and can export a WireGuard config works):

1. Download a WireGuard config file for a mainland-China server from your VPN provider.
2. Copy its values into `.env`:

   ```ini
   KTV_PROXY=http://vpn:8888
   WIREGUARD_ENDPOINT_IP=     # IP from [Peer] Endpoint
   WIREGUARD_ENDPOINT_PORT=51820
   WIREGUARD_PUBLIC_KEY=      # [Peer] PublicKey
   WIREGUARD_ADDRESSES=       # [Interface] Address, IPv4 only, e.g. 10.2.0.2/32
   ```

3. Store the private key (`[Interface] PrivateKey`) in its own file, not in `.env`:

   ```bash
   mkdir -p secrets
   printf '%s' '<your private key>' > secrets/wireguard_private_key
   chmod 600 secrets/wireguard_private_key
   ```

4. Start with the `vpn` profile: `docker compose --profile vpn up -d`

Check that downloads really go through the VPN (it should show a mainland-China IP):

```bash
docker exec home-ktv python -c "import urllib.request;print(urllib.request.urlopen('https://ipinfo.io/json',timeout=15).read().decode())"
```

The proxy is only used for downloads: requests from the server to your home network (for example to Maidong on the TV)
never go through it.

## Updating

```bash
git pull
docker compose restart ktv
```

**Don't restart while someone is singing**: it cuts off the song that's playing. First check that `"current"` is `null`
at `http://<IP>:8787/api/room`. If you only changed the pages under `ktv-server/server/web/`, no restart is needed:
reload the page, and the TV home screen reloads itself.

## TrueNAS SCALE

The steps above work on TrueNAS as they are. A few tips:

- Put the repo in a dataset, for example `/mnt/<pool>/apps/home-ktv`, and run `sudo docker compose up -d` in the Shell.
  You can also paste `compose.yaml` into "Apps → Discover Apps → Install via YAML"; in that case change relative paths
  such as `./ktv-server` to absolute paths.
- Point `KTV_MEDIA` at the dataset that holds your songs, for example `/mnt/<pool>/media/KTV`. The same folder can also
  be shared over SMB or OpenList.
- Keep the catalog and settings under the `apps` dataset, not next to the songs.

## Repairing songs from the old downloader

i歌霸 treats the end of an HTTP Range as exclusive. Before October 2026 this project's downloader therefore lost the last
byte of every piece, and such songs glitch or fail partway through. The current downloader is fixed, and old files can
be repaired in place:

```bash
docker exec home-ktv python -u -m server.repair --data /data --media /media
```

Repaired files are recorded in `data/repaired.json` and skipped next time. If a song can't be repaired, search for it on
the admin page and click 「重新下载」(re-download).

## Using it with Maidong KTV (optional)

If the TV already runs [麦动 KTV (Maidong KTV)](https://gitee.com/yangyachao-X/maidong-ktv), you can choose 「麦动」 on
the admin page under 「曲库与设置 → 电视播放器」(TV player) and enter Maidong's address on your network
(`http://<TV's IP>:8765`). Song requests and remote control are then passed on to Maidong, and the NAS only handles
downloads.
