# ktv-player

[中文](README.md) | English

The Home KTV player for Android TV (Kotlin + Media3 ExoPlayer). It just plays whatever the server says: the queue,
downloads and vocals/backing-track choice all live on the server, and the TV stores no songs and manages no downloads.
Its screen is the server's `/tv` page, so changing the screen doesn't need a reinstall.

Tested on Chromecast with Google TV; any Android TV running Android 7.0 (API 24) or later should work.

## Install

1. On the TV (or TV box), turn on "Developer options → USB debugging / Wireless debugging".
2. Build:

   ```bash
   ./gradlew :app:assembleDebug
   ```

   To build the server address into the app (so nobody has to type it with the remote):

   ```bash
   ./gradlew :app:assembleDebug -PktvServer=http://192.168.1.10:8787
   ```

3. Install:

   ```bash
   adb connect <TV's IP>:<port>
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

4. The first time it opens, it asks for the server address, for example `http://192.168.1.10:8787`. Long-press OK on
   the remote to change it later. You can also set it with adb:

   ```bash
   adb shell am start -n com.homektv.player/.MainActivity --es server http://192.168.1.10:8787
   ```

On the server's admin page, 「曲库与设置 → 电视播放器」(TV player) must be set to **家庭KTV 播放器** (the default).

The app's name on the TV is 「家庭KTV」.

## Remote

| Key | Action |
|---|---|
| OK | Pause / resume |
| → | Press twice to skip |
| ↑ ↓ | Original vocals / backing track |
| ← | Queue and QR codes |
| Long-press OK | Change the server address |
| Back | Close the list (never exits, to avoid accidental presses) |
| Home | Exit |

## Song format

MKV: H.264 video plus two Vorbis audio tracks, the first with the original vocals and the second the backing track.
Switching between them switches audio tracks, with a pause of about 0.3 seconds.
