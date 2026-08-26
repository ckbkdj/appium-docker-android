package io.ie520.semanticmobilebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Path
import android.graphics.Rect
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

class SemanticAccessibilityService : AccessibilityService() {
    companion object {
        private const val SOCKET_NAME = "semantic_mobile_agent"
        private const val TOKEN_SETTING = "semantic_mobile_agent_token"
        private const val MAX_NODES = 1500
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile
    private var running = false

    @Volatile
    private var server: LocalServerSocket? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        startServer()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        stopServer()
        super.onDestroy()
    }

    private fun startServer() {
        if (running) return
        running = true
        thread(name = "semantic-mobile-agent-server", isDaemon = true) {
            try {
                val localServer = LocalServerSocket(SOCKET_NAME)
                server = localServer
                while (running) {
                    val socket = try {
                        localServer.accept()
                    } catch (error: Throwable) {
                        if (running) throw error else break
                    }
                    thread(name = "semantic-mobile-agent-client", isDaemon = true) {
                        handleClient(socket)
                    }
                }
            } catch (_: Throwable) {
                // AccessibilityService may be restarted by Android. Closing the old
                // local socket in stopServer allows the next instance to bind again.
            } finally {
                running = false
                try {
                    server?.close()
                } catch (_: Throwable) {
                }
                server = null
            }
        }
    }

    private fun stopServer() {
        running = false
        try {
            server?.close()
        } catch (_: Throwable) {
        }
        server = null
    }

    private fun handleClient(socket: LocalSocket) {
        try {
            val reader = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))
            val writer = BufferedWriter(OutputStreamWriter(socket.outputStream, Charsets.UTF_8))
            while (running) {
                val line = reader.readLine() ?: break
                val response = try {
                    handleRequest(JSONObject(line))
                } catch (error: Throwable) {
                    JSONObject()
                        .put("ok", false)
                        .put("error", error.message ?: error.javaClass.simpleName)
                }
                writer.write(response.toString())
                writer.newLine()
                writer.flush()
            }
        } catch (_: Throwable) {
        } finally {
            try {
                socket.close()
            } catch (_: Throwable) {
            }
        }
    }

    private fun handleRequest(request: JSONObject): JSONObject {
        val requestId = request.optString("id", "")
        val response = JSONObject().put("id", requestId)
        val configuredToken = Settings.Secure.getString(contentResolver, TOKEN_SETTING).orEmpty()
        if (configuredToken.isNotEmpty() && request.optString("token", "") != configuredToken) {
            return response.put("ok", false).put("error", "unauthorized")
        }

        return try {
            when (request.getString("command")) {
                "ping" -> response
                    .put("ok", true)
                    .put("service", "semantic-mobile-agent-bridge")
                    .put("version", "0.1.0")

                "snapshot" -> onMain { snapshot() }
                    .put("id", requestId)
                    .put("ok", true)

                "click" -> response
                    .put("ok", onMain { clickNode(request.getString("path")) })
                    .put("error", if (onMain { nodeExists(request.getString("path")) }) "" else "node not found")

                "set_text" -> {
                    val changed = onMain {
                        setNodeText(request.getString("path"), request.optString("text", ""))
                    }
                    response
                        .put("ok", changed)
                        .put("error", if (changed) "" else "unable to set text")
                }

                "tap" -> response.put(
                    "ok",
                    onMain { tap(request.getInt("x"), request.getInt("y")) },
                )

                "swipe" -> response.put(
                    "ok",
                    onMain {
                        swipe(
                            request.getInt("x1"),
                            request.getInt("y1"),
                            request.getInt("x2"),
                            request.getInt("y2"),
                            request.optInt("duration_ms", 250),
                        )
                    },
                )

                "global" -> response.put(
                    "ok",
                    onMain { globalAction(request.getString("action")) },
                )

                "open_app" -> response.put(
                    "ok",
                    onMain { openApp(request.getString("package")) },
                )

                "installed_apps" -> response
                    .put("ok", true)
                    .put("apps", installedLauncherApps())

                else -> response.put("ok", false).put("error", "unknown command")
            }
        } catch (error: Throwable) {
            response.put("ok", false).put("error", error.message ?: error.javaClass.simpleName)
        }
    }

    private fun snapshot(): JSONObject {
        val metrics = resources.displayMetrics
        val root = rootInActiveWindow
        val nodes = JSONArray()
        if (root != null) {
            appendNode(root, "0", nodes)
        }
        return JSONObject()
            .put("package", root?.packageName?.toString().orEmpty())
            .put("activity", "")
            .put("width", metrics.widthPixels)
            .put("height", metrics.heightPixels)
            .put("nodes", nodes)
    }

    private fun appendNode(node: AccessibilityNodeInfo, path: String, output: JSONArray) {
        if (output.length() >= MAX_NODES) return
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        val actions = node.actionList.map { it.id }.toSet()
        val editable = node.isEditable ||
            actions.contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_TEXT.id) ||
            node.className?.toString()?.contains("EditText", ignoreCase = true) == true

        output.put(
            JSONObject()
                .put("path", path)
                .put("text", node.text?.toString().orEmpty())
                .put("description", node.contentDescription?.toString().orEmpty())
                .put("resource_id", node.viewIdResourceName.orEmpty())
                .put("class_name", node.className?.toString().orEmpty())
                .put("package", node.packageName?.toString().orEmpty())
                .put("clickable", node.isClickable)
                .put("editable", editable)
                .put("scrollable", node.isScrollable)
                .put("enabled", node.isEnabled)
                .put("selected", node.isSelected)
                .put("checked", node.isChecked)
                .put("focused", node.isFocused)
                .put(
                    "bounds",
                    JSONArray()
                        .put(bounds.left)
                        .put(bounds.top)
                        .put(bounds.right)
                        .put(bounds.bottom),
                ),
        )

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            appendNode(child, "$path/$index", output)
            if (output.length() >= MAX_NODES) break
        }
    }

    private fun resolveNode(path: String): AccessibilityNodeInfo? {
        var current = rootInActiveWindow ?: return null
        val parts = path.split('/').filter { it.isNotBlank() }
        if (parts.isEmpty() || parts.first() != "0") return null
        for (part in parts.drop(1)) {
            val index = part.toIntOrNull() ?: return null
            current = current.getChild(index) ?: return null
        }
        return current
    }

    private fun nodeExists(path: String): Boolean = resolveNode(path) != null

    private fun clickNode(path: String): Boolean {
        var node = resolveNode(path) ?: return false
        repeat(6) {
            if (node.isClickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                return true
            }
            node = node.parent ?: return false
        }
        return false
    }

    private fun setNodeText(path: String, text: String): Boolean {
        val node = resolveNode(path) ?: return false
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
    }

    private fun tap(x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 1))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    private fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): Boolean {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val gesture = GestureDescription.Builder()
            .addStroke(
                GestureDescription.StrokeDescription(
                    path,
                    0,
                    durationMs.coerceIn(50, 5000).toLong(),
                ),
            )
            .build()
        return dispatchGesture(gesture, null, null)
    }

    private fun globalAction(action: String): Boolean {
        val value = when (action.uppercase(Locale.ROOT)) {
            "BACK" -> GLOBAL_ACTION_BACK
            "HOME" -> GLOBAL_ACTION_HOME
            "RECENTS" -> GLOBAL_ACTION_RECENTS
            "NOTIFICATIONS" -> GLOBAL_ACTION_NOTIFICATIONS
            "QUICK_SETTINGS" -> GLOBAL_ACTION_QUICK_SETTINGS
            else -> return false
        }
        return performGlobalAction(value)
    }

    private fun openApp(packageName: String): Boolean {
        val intent = packageManager.getLaunchIntentForPackage(packageName) ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        startActivity(intent)
        return true
    }

    private fun installedLauncherApps(): JSONArray {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val activities = packageManager.queryIntentActivities(intent, PackageManager.MATCH_ALL)
        val rows = activities
            .mapNotNull { resolveInfo ->
                val packageName = resolveInfo.activityInfo?.packageName ?: return@mapNotNull null
                val label = resolveInfo.loadLabel(packageManager)?.toString().orEmpty().ifBlank { packageName }
                packageName to label
            }
            .distinctBy { it.first }
            .sortedBy { it.second.lowercase(Locale.getDefault()) }

        return JSONArray().also { output ->
            for ((packageName, label) in rows) {
                output.put(JSONObject().put("package", packageName).put("label", label))
            }
        }
    }

    private fun <T> onMain(timeoutMs: Long = 5000, block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val result = AtomicReference<T?>()
        val failure = AtomicReference<Throwable?>()
        val latch = CountDownLatch(1)
        mainHandler.post {
            try {
                result.set(block())
            } catch (error: Throwable) {
                failure.set(error)
            } finally {
                latch.countDown()
            }
        }
        if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            throw IllegalStateException("main-thread accessibility command timed out")
        }
        failure.get()?.let { throw it }
        @Suppress("UNCHECKED_CAST")
        return result.get() as T
    }
}
