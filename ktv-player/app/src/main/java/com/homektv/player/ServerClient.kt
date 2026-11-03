package com.homektv.player

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** NAS 上 Home KTV 服务端的接口；只在后台线程调用。 */
class ServerClient(val baseUrl: String) {

    /** 取播放状态：since 是上次拿到的 version，状态没变时服务端最多等 wait 秒。 */
    fun state(since: Int?, waitSeconds: Int): JSONObject =
        request("GET", "/api/player/state" + (if (since != null) "?since=$since&wait=$waitSeconds" else ""),
            readTimeout = (waitSeconds + 10) * 1000)

    fun report(body: JSONObject) {
        request("POST", "/api/player/report", body)
    }

    fun action(action: String) {
        request("POST", "/api/player/action", JSONObject().put("action", action))
    }

    fun vocal(mode: String) {
        request("PATCH", "/api/room/audio", JSONObject().put("vocalMode", mode))
    }

    fun mediaUrl(path: String): String = baseUrl.trimEnd('/') + path

    private fun request(method: String, path: String, body: JSONObject? = null, readTimeout: Int = 10_000): JSONObject {
        val connection = URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = 5_000
            connection.readTimeout = readTimeout
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val text = (if (code in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            val json = runCatching { JSONObject(text) }.getOrDefault(JSONObject())
            if (code !in 200..299) throw IOException(json.optString("error").ifBlank { "服务器返回 HTTP $code" })
            return json
        } finally {
            connection.disconnect()
        }
    }
}
