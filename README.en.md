# Home KTV (家庭KTV)

[中文](README.md) | English

Karaoke at home: the songs live on your own NAS or computer, **the TV only plays them, and everything else (requesting
songs, remote control, seeing where you are in the queue) happens on your phone**.

You're at home, so the TV doesn't need flashy menus or song picking with a remote. Guests join the WiFi, scan the code on
the TV, and search, request and skip songs on their phones, and see how many songs are ahead of theirs. A song that isn't
on the NAS yet downloads automatically when someone requests it, and starts playing as soon as it's ready.

> The 80,000-song catalog comes from the public API of i歌霸 (igeba), a Chinese karaoke service, so the songs and the
> interface are in Chinese. This project does not ship any songs and is for personal, home, non-commercial use only.

## Features

**Everything is on the phone**

- Scan a QR code to request songs: no app to install, no login, no membership.
- Search by title, singer, or pinyin initials (`qt` → 晴天, `zjl` → 周杰伦). Popular songs and songs already on the NAS
  come first.
- Queue: who requested each song and how far its download has got; move a song to the top or remove it. "我点的"
  (my songs) shows how many songs are ahead of yours and roughly how many minutes to wait.
- Remote control: pause, skip, restart, original vocals or backing track, volume.
- Leave a nickname when you request a song, and the TV pops up "小王 点了《晴天》" (Xiao Wang requested 晴天).

**The TV screen stays simple**

- When nobody is singing: the queue, a QR code for requesting songs and one for joining the WiFi, and who requested what.
- While someone sings: one line at the top with the song, who requested it and what's next, out of the way of the video.
- The remote needs only a few keys: OK to pause, → to skip, ↑↓ for vocals or backing track, ← for the queue and QR codes.

**Fast downloads, even from abroad**

The songs are hosted in mainland China, and downloading them from overseas is slow and often stalls. The downloader is
written for exactly that kind of connection:

- Each song is split into 512 KiB pieces, downloaded over 64 connections at once.
- A connection that gets no data for 20 seconds is dropped and the piece is fetched again; repeated failures switch to a
  fresh download address.
- When only the last few pieces are left, a piece stuck for 6 seconds gets a second connection and whichever finishes
  first wins, so downloads don't hang at 99%.
- The download-address API sometimes never answers: the request is sent again every 5 seconds and the first reply wins.
- Data goes to a `.part` file that only replaces the real file once every byte has arrived. A broken song can be
  re-downloaded from the admin page; the old file stays until the new one is complete.
- Songs that guests request jump ahead of bulk downloads. The queue is saved to disk and resumes after a restart.
- Downloads alone can go through a mainland-China exit (an optional WireGuard container is included); the rest of your
  home network is not affected.

Measured abroad through a mainland-China exit: 0.3 to 1.8 MiB/s per song, so a typical 70 MB song takes a minute or two.

**Easy to run**

- The server uses only the Python standard library and runs on the official `python:3.12-slim` image: no image to
  build, no dependencies to install.
- Any machine that runs Docker works: TrueNAS, Synology, Unraid, a Raspberry Pi, or an ordinary Linux or Windows PC.
- Downloaded songs stay on your own disk and start instantly next time. The admin page can bulk-download by singer,
  playlist or chart.

## How it fits together

```
Phone / computer browser ──┐
                           ├─HTTP─▶ ktv-server (Docker, port 8787) ── optional proxy ──▶ i歌霸 catalog and songs
Player on the TV ──────────┘          ├─ Song requests  /        for guests
  (Android TV)                        ├─ Admin page     /admin   bulk downloads, settings
                                      ├─ TV home        /tv      the screen shown inside the player
                                      ├─ Queue, download queue, catalog (SQLite)
                                      └─ Song files /media/<number> (MKV, supports resuming)
```

- `ktv-server/`: the server (Python).
- `ktv-player/`: the Android TV player (Kotlin + Media3). It only plays the video; its screen is the server's `/tv`
  page, so changing the screen doesn't need a new app build.
- Design details: [docs/ARCHITECTURE.en.md](docs/ARCHITECTURE.en.md).

## Quick start

You need a machine on your home network that runs Docker, an Android TV device (tested on Chromecast with Google TV),
and phones on the same WiFi as the TV.

```bash
git clone https://github.com/jackyierfati/home-ktv.git
cd home-ktv
cp .env.example .env        # set KTV_MEDIA: where songs are stored, needs plenty of space
docker compose up -d
```

1. Open `http://<this machine's IP>:8787/admin` → 「曲库与设置」(catalog and settings) → 「更新曲库」(update catalog),
   and wait a few minutes.
2. On the admin page, enter your KTV's name under 「名字」; to let friends join the WiFi by scanning, enter the WiFi name
   and password too.
3. Install the player on the TV (see [ktv-player/README.en.md](ktv-player/README.en.md)) and enter
   `http://<this machine's IP>:8787` the first time it opens.
4. Scan 「扫码点歌」 on the TV with a phone and start singing.

If you're outside mainland China, also read "Route downloads through a mainland-China exit" in
[docs/DEPLOY.en.md](docs/DEPLOY.en.md).

## Microphones

Microphones don't go through this system. The recommended setup is to connect the TV box over HDMI to a mixer (or a
karaoke amplifier with HDMI), which handles the vocals with no delay.

## Security

The request page and the admin page have no password and are meant for your home network only.
**Do not expose port 8787 to the internet.**

## Credits and license

- The catalog schema, the i歌霸 API client and the pinyin table are ported from
  [麦动 KTV (Maidong KTV)](https://gitee.com/yangyachao-X/maidong-ktv) by 杨家三郎. The first version of this project
  also relied on Maidong for playback on the TV. Thanks to the original author.
- As required by Maidong's license, this project is likewise free, open source and **strictly non-commercial**. See
  [LICENSE](LICENSE).
- Song copyrights belong to their rights holders.

Made by [J]@cK-Y
