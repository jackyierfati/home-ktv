package com.homektv.player

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.util.Base64
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.TextView
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/**
 * 电视上的 Home KTV 播放器：从 NAS 取状态并照着播放，所有已点和下载都由服务端管。
 * 画面（主页、正在播放、已点列表）是 NAS 上的 /tv 页面，盖在视频上面的透明 WebView 里显示，改界面不用重装 App。
 * 遥控器：OK 暂停/继续，→ 切歌（按两次），↑↓ 原唱/伴唱，← 已点列表和点歌二维码，长按 OK 改服务器地址，主页键退出。
 */
class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private val poller = Executors.newSingleThreadExecutor()
    private val sender = Executors.newSingleThreadExecutor()
    private lateinit var player: ExoPlayer
    @Volatile private var client = ServerClient(DEFAULT_SERVER)
    @Volatile private var running = false
    private var generation = 0                 // 换服务器地址时让旧的轮询自己退出

    private var entryId: String? = null        // 正在放的已点条目
    private var replaySeq = -1
    private var vocal = "accompany"
    private var okDownAt = 0L
    private var nextArmedAt = 0L

    private lateinit var webHolder: FrameLayout
    private lateinit var web: WebView
    private var pageFailed = false
    private lateinit var offline: View
    private lateinit var offlineText: TextView
    private lateinit var notice: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)
        webHolder = findViewById(R.id.webHolder)
        offline = findViewById(R.id.offline)
        offlineText = findViewById(R.id.offlineText)
        notice = findViewById(R.id.notice)
        makeWeb()

        // adb shell am start -n com.homektv.player/.MainActivity --es server http://<NAS 的 IP>:8787
        intent?.getStringExtra("server")?.let { prefs().edit().putString("server", it.trimEnd('/')).apply() }
        player = ExoPlayer.Builder(this).build()
        findViewById<PlayerView>(R.id.video).player = player
        player.addListener(listener)
        useServer(prefs().getString("server", DEFAULT_SERVER) ?: DEFAULT_SERVER)
    }

    override fun onStart() {
        super.onStart()
        web.onResume()
        web.resumeTimers()
        startPolling()
        main.post(reporter)
    }

    override fun onStop() {
        super.onStop()
        running = false
        main.removeCallbacks(reporter)
        web.onPause()
        web.pauseTimers()                     // 页面的长轮询也停下，回来再接着
        player.playWhenReady = false          // 离开播放器（回到电视桌面）就先停下
        entryId = null                        // 回来时重新按服务端状态加载
    }

    override fun onDestroy() {
        super.onDestroy()
        running = false
        generation++                          // 还在路上的状态回来后不再碰已经释放的播放器
        main.removeCallbacksAndMessages(null)
        web.destroy()
        player.release()
        poller.shutdownNow()
        sender.shutdownNow()
    }

    private fun prefs() = getSharedPreferences("player", MODE_PRIVATE)

    private fun useServer(url: String) {
        client = ServerClient(url)
        generation++
        if (url.isBlank()) {                  // 第一次打开、编译时也没给默认地址：先问地址
            running = false
            offlineText.text = "还没设置点歌服务器\n长按 OK 键输入 NAS 上 Home KTV 的地址，比如 http://192.168.1.10:8787"
            offline.visibility = View.VISIBLE
            main.post { if (!isFinishing) editServer() }
            return
        }
        loadPage()
        if (running) { running = false; startPolling() }
    }

    // ---------------- NAS 上的电视页面 ----------------
    @SuppressLint("SetJavaScriptEnabled")
    private fun makeWeb() {
        web = WebView(this).apply {
            isFocusable = false                // 遥控器按键全部由播放器处理
            isFocusableInTouchMode = false
            setBackgroundColor(Color.TRANSPARENT)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.textZoom = 100            // 不跟着系统字体大小变，页面按 960×540 排版
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            addJavascriptInterface(Bridge(), "KtvNative")
            webViewClient = PageClient()
        }
        webHolder.addView(web, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
    }

    private val retryPage = Runnable { loadPage() }

    private fun loadPage() {
        main.removeCallbacks(retryPage)
        if (client.baseUrl.isBlank()) return
        pageFailed = false
        web.loadUrl(client.baseUrl + "/tv")
    }

    private fun pageError(message: String?) {
        pageFailed = true
        offlineText.text = "连不上点歌服务器 ${client.baseUrl}\n${message ?: ""}\n长按 OK 键可以修改服务器地址"
        if (entryId == null) offline.visibility = View.VISIBLE     // 正在放歌就别挡住视频
        main.removeCallbacks(retryPage)
        main.postDelayed(retryPage, 5000)
    }

    private inner class PageClient : WebViewClient() {
        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) pageError(error.description?.toString())
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            if (request.isForMainFrame) pageError("HTTP ${response.statusCode}")
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (!pageFailed) offline.visibility = View.GONE
        }

        // 只打开自己 NAS 上的页面
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) =
            !request.url.toString().startsWith(client.baseUrl)

        // 页面渲染进程崩了：换一个新的 WebView，视频不受影响
        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            webHolder.removeView(view)
            view.destroy()
            makeWeb()
            loadPage()
            return true
        }
    }

    /** 给页面用的：页面里的二维码由这里用 ZXing 生成。 */
    private inner class Bridge {
        @JavascriptInterface
        fun qr(text: String, size: Int): String {
            val out = ByteArrayOutputStream()
            qrBitmap(text, size.coerceIn(64, 1024)).compress(Bitmap.CompressFormat.PNG, 100, out)
            return "data:image/png;base64," + Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
        }
    }

    private fun qrBitmap(text: String, size: Int): Bitmap {
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size, mapOf(EncodeHintType.MARGIN to 1))
        val pixels = IntArray(size * size) { if (matrix[it % size, it / size]) Color.BLACK else Color.WHITE }
        return Bitmap.createBitmap(pixels, size, size, Bitmap.Config.RGB_565)
    }

    // ---------------- 和服务端同步 ----------------
    private fun startPolling() {
        if (running || client.baseUrl.isBlank()) return
        running = true
        val mine = generation
        val api = client
        poller.execute {
            var version: Int? = null
            while (running && mine == generation) {
                try {
                    val snap = api.state(version, 3)
                    version = snap.optInt("version")
                    main.post { if (mine == generation && !isDestroyed) apply(snap) }
                } catch (e: Exception) {
                    version = null                 // 连不上时页面自己会显示“正在重新连接”
                    SystemClock.sleep(2000)
                }
            }
        }
    }

    private fun apply(snap: JSONObject) {
        player.volume = snap.optInt("volume", 70) / 100f
        val current = snap.optJSONObject("current")
        if (current == null) {
            if (entryId != null) {
                player.stop()
                player.clearMediaItems()
                entryId = null
            }
            return
        }
        offline.visibility = View.GONE
        val id = current.getString("id")
        val wantPlaying = snap.optBoolean("playing", true)
        val wantVocal = snap.optString("vocal", "accompany")
        if (id != entryId) {
            entryId = id
            replaySeq = snap.optInt("replay")
            vocal = wantVocal
            // 中途退出再进来（或播放器重启）时接着上次的位置唱，往回退 2 秒
            val resumeAt = current.optInt("position") * 1000L - 2000
            player.setMediaItem(MediaItem.fromUri(client.mediaUrl(current.getString("media"))), maxOf(0L, resumeAt))
            player.prepare()
            player.playWhenReady = wantPlaying
        } else {
            if (snap.optInt("replay") != replaySeq) {
                replaySeq = snap.optInt("replay")
                player.seekTo(0)
                notice("重唱")
            }
            if (wantVocal != vocal) {
                vocal = wantVocal
                applyVocal()
                notice(if (vocal == "original") "原唱" else "伴唱")
            }
            if (wantPlaying != player.playWhenReady) {
                player.playWhenReady = wantPlaying
                notice(if (wantPlaying) "继续" else "暂停")
            }
        }
    }

    /** 第一条音轨是原唱，第二条是伴唱。 */
    private fun applyVocal() {
        val groups = player.currentTracks.groups.filter { it.type == C.TRACK_TYPE_AUDIO }
        if (groups.size < 2) return
        val wanted = groups[if (vocal == "original") 0 else 1]
        if (wanted.isSelected) return
        player.trackSelectionParameters = player.trackSelectionParameters.buildUpon()
            .setOverrideForType(TrackSelectionOverride(wanted.mediaTrackGroup, 0))
            .build()
    }

    private val listener = object : Player.Listener {
        override fun onTracksChanged(tracks: Tracks) = applyVocal()

        override fun onPlaybackStateChanged(state: Int) {
            if (state == Player.STATE_ENDED) report("ended")
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            report(if (isPlaying) "playing" else "paused")
        }

        override fun onPlayerError(error: PlaybackException) {
            notice("这首歌放不了，跳到下一首")
            report("error", error.errorCodeName + ": " + (error.message ?: ""))
        }
    }

    private val reporter = object : Runnable {
        override fun run() {
            if (entryId != null && player.isPlaying) report("playing")
            main.postDelayed(this, 5000)
        }
    }

    private fun report(state: String, error: String? = null) {
        val id = entryId ?: return
        val body = JSONObject().put("entryId", id).put("state", state)
            .put("position", player.currentPosition / 1000).put("duration", maxOf(0L, player.duration) / 1000)
        if (error != null) body.put("error", error)
        val api = client
        sender.execute { runCatching { api.report(body) } }
    }

    private fun send(action: String, label: String) {
        notice(label)
        val api = client
        sender.execute {
            runCatching { api.action(action) }.onFailure { main.post { notice("没发出去：${it.message}") } }
        }
    }

    private fun toggleVocal() {
        val target = if (vocal == "original") "accompany" else "original"
        notice(if (target == "original") "原唱" else "伴唱")
        val api = client
        sender.execute {
            runCatching { api.vocal(target) }.onFailure { main.post { notice("没发出去：${it.message}") } }
        }
    }

    // ---------------- 提示和设置 ----------------
    private val hideNotice = Runnable { notice.visibility = View.GONE }

    private fun notice(text: String) {
        notice.text = text
        notice.visibility = View.VISIBLE
        main.removeCallbacks(hideNotice)
        main.postDelayed(hideNotice, 1800)
    }

    private fun editServer() {
        val input = EditText(this).apply {
            setText(client.baseUrl)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSelection(text.length)
        }
        AlertDialog.Builder(this).setTitle("点歌服务器地址").setView(input)
            .setPositiveButton("保存") { _, _ ->
                val url = input.text.toString().trim().trimEnd('/')
                if (url.startsWith("http://") || url.startsWith("https://")) {
                    prefs().edit().putString("server", url).apply()
                    useServer(url)
                } else notice("地址要以 http:// 开头")
            }
            .setNegativeButton("取消", null).show()
    }

    // ---------------- 遥控器 ----------------
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val code = event.keyCode
        val ok = code == KeyEvent.KEYCODE_DPAD_CENTER || code == KeyEvent.KEYCODE_ENTER || code == KeyEvent.KEYCODE_NUMPAD_ENTER
        if (ok) {
            if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) okDownAt = SystemClock.uptimeMillis()
            if (event.action == KeyEvent.ACTION_UP) {
                if (SystemClock.uptimeMillis() - okDownAt > 800) editServer() else togglePlay()
            }
            return true
        }
        if (event.action != KeyEvent.ACTION_DOWN) return super.dispatchKeyEvent(event)
        when (code) {
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE -> togglePlay()
            KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_MEDIA_NEXT -> {
                if (entryId == null) return true
                val now = SystemClock.uptimeMillis()
                if (now - nextArmedAt < 3000) { nextArmedAt = 0; send("next", "切歌") }
                else { nextArmedAt = now; notice("再按一次 → 切歌") }
            }
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN -> if (entryId != null) toggleVocal()
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_INFO ->
                if (pageFailed) notice("连不上点歌服务器") else web.evaluateJavascript("window.ktv && ktv.togglePanel()", null)
            // 返回键很容易误按：只用来收起已点列表，退出用主页键
            KeyEvent.KEYCODE_BACK ->
                web.evaluateJavascript("!!(window.ktv && ktv.hidePanel())") { if (it != "true") notice("按主页键退出") }
            else -> return super.dispatchKeyEvent(event)
        }
        return true
    }

    private fun togglePlay() {
        if (entryId == null) return
        if (player.playWhenReady) send("pause", "暂停") else send("resume", "继续")
    }

    companion object {
        val DEFAULT_SERVER: String = BuildConfig.DEFAULT_SERVER.trimEnd('/')
    }
}
