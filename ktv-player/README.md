# ktv-player

家庭KTV 的 Android TV 播放器（Kotlin + Media3 ExoPlayer）。只负责照着服务端的状态放歌：
已点、下载、原唱/伴唱都在服务端，电视上不存歌、不管下载。界面是服务端上的 `/tv` 网页，改界面不用重装。

在 Chromecast with Google TV 上测试过，Android 7.0（API 24）以上的 Android TV 应该都能用。

## 安装

1. 电视（或盒子）打开「开发者选项 → USB 调试 / 无线调试」。
2. 编译：

   ```bash
   ./gradlew :app:assembleDebug
   ```

   想把服务器地址直接写进 App（省得在电视上用遥控器输入）：

   ```bash
   ./gradlew :app:assembleDebug -PktvServer=http://192.168.1.10:8787
   ```

3. 安装：

   ```bash
   adb connect <电视的 IP>:<端口>
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

4. 第一次打开会让你输入服务器地址，比如 `http://192.168.1.10:8787`。以后长按遥控器 OK 键可以改。
   也可以用 adb 设：

   ```bash
   adb shell am start -n com.homektv.player/.MainActivity --es server http://192.168.1.10:8787
   ```

服务端管理页「曲库与设置 → 电视播放器」要选 **家庭KTV 播放器**（默认就是）。

## 遥控器

| 按键 | 作用 |
|---|---|
| OK | 暂停 / 继续 |
| → | 按两次切歌 |
| ↑ ↓ | 原唱 / 伴唱 |
| ← | 已点列表和二维码 |
| 长按 OK | 改服务器地址 |
| 返回 | 收起列表（不会退出，防止误按） |
| 主页 | 退出 |

## 歌曲格式

MKV：H.264 视频 + 两条 Vorbis 音轨，第一条原唱、第二条伴唱。切原唱/伴唱就是切音轨，会有大约 0.3 秒的停顿。
