package ai.semantic.mobile.bridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Path
import android.graphics.Rect
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class FastBridgeService : AccessibilityService() {
    private val mainHandler = Handler(Looper.getMainLooper())
    private val ioPool = Executors.newCachedThreadPool()
    private val nodeLock = Any()
    private val nodeCache = mutableMapOf<Int, AccessibilityNodeInfo>()

    @Volatile
    private var serverSocket: LocalServerSocket? = null

    @Volatile
    private var activePackage: String = ""

    @Volatile
    private var activeActivity: String = ""

    override fun onServiceConnected() {
        super.onServiceConnected()
        isRunning = true
        startServer()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        event.packageName?.toString()?.let { activePackage = it }
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            event.className?.toString()?.let { activeActivity = it }
        }
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        isRunning = false
        try {
            serverSocket?.close()
        } catch (_: Exception) {
            // Socket is already closed.
        }
        serverSocket = null
        ioPool.shutdownNow()
        clearNodeCache()
        super.onDestroy()
    }

    private fun startServer() {
        ioPool.execute {
            try {
                val server = LocalServerSocket(SOCKET_NAME)
                serverSocket = server
                while (!Thread.currentThread().isInterrupted) {
                    val client = server.accept()
                    ioPool.execute { handleClient(client) }
                }
            } catch (_: Exception) {
                // Closing the socket during service shutdown exits the accept loop.
            }
        }
    }

    private fun handleClient(socket: LocalSocket) {
        socket.use { client ->
            val reader = BufferedReader(InputStreamReader(client.inputStream, Charsets.UTF_8))
            val writer = BufferedWriter(OutputStreamWriter(client.outputStream, Charsets.UTF_8))
            while (!Thread.currentThread().isInterrupted) {
                val line = reader.readLine() ?: break
                if (line.length > MAX_REQUEST_CHARS) {
                    writeResponse(
                        writer,
                        JSONObject.NULL,
                        null,
                        "request too large",
                    )
                    break
                }
                val request = try {
                    JSONObject(line)
                } catch (_: Exception) {
                    writeResponse(writer, JSONObject.NULL, null, "invalid JSON")
                    continue
                }
                val id = request.opt("id") ?: JSONObject.NULL
                try {
                    validateToken(request.optString("token", ""))
                    val result = executeCommand(request)
                    writeResponse(writer, id, result, null)
                } catch (error: Throwable) {
                    val message = error.message?.take(500) ?: error.javaClass.simpleName
                    writeResponse(writer, id, null, message)
                }
            }
        }
    }

    private fun writeResponse(
        writer: BufferedWriter,
        id: Any,
        result: JSONObject?,
        error: String?,
    ) {
        val response = JSONObject().put("id", id)
        if (error == null) {
            response.put("ok", true)
            response.put("result", result ?: JSONObject())
        } else {
            response.put("ok", false)
            response.put("error", error)
        }
        writer.write(response.toString())
        writer.newLine()
        writer.flush()
    }

    private fun validateToken(supplied: String) {
        val expected = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .getString(PREF_TOKEN, "")
            .orEmpty()
        if (expected.length < 16 || supplied.isEmpty()) {
            throw SecurityException("bridge token is not configured or missing")
        }
        val valid = MessageDigest.isEqual(
            expected.toByteArray(Charsets.UTF_8),
            supplied.toByteArray(Charsets.UTF_8),
        )
        if (!valid) {
            throw SecurityException("unauthorized")
        }
    }

    private fun executeCommand(request: JSONObject): JSONObject {
        return when (request.optString("cmd")) {
            "ping" -> JSONObject()
                .put("service", "semantic-mobile-bridge")
                .put("running", true)
                .put("socket", SOCKET_NAME)

            "snapshot" -> onMain { buildSnapshot() }

            "tap" -> {
                val performed = if (request.has("nodeId") && !request.isNull("nodeId")) {
                    onMain { clickNode(request.getInt("nodeId")) }
                } else {
                    val x = request.getInt("x")
                    val y = request.getInt("y")
                    onMain { dispatchPath(x, y, x, y, 60L) }
                }
                JSONObject().put("performed", performed)
            }

            "input" -> {
                val nodeId = if (request.has("nodeId") && !request.isNull("nodeId")) {
                    request.getInt("nodeId")
                } else {
                    null
                }
                val performed = onMain {
                    setNodeText(nodeId, request.getString("text"))
                }
                JSONObject().put("performed", performed)
            }

            "gesture" -> {
                val performed = onMain {
                    dispatchPath(
                        request.getInt("x1"),
                        request.getInt("y1"),
                        request.getInt("x2"),
                        request.getInt("y2"),
                        request.optLong("durationMs", 250L).coerceIn(1L, 30_000L),
                    )
                }
                JSONObject().put("performed", performed)
            }

            "global" -> {
                val action = when (request.getString("action").lowercase()) {
                    "back" -> GLOBAL_ACTION_BACK
                    "home" -> GLOBAL_ACTION_HOME
                    "recents" -> GLOBAL_ACTION_RECENTS
                    "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                    else -> throw IllegalArgumentException("unsupported global action")
                }
                JSONObject().put("performed", onMain { performGlobalAction(action) })
            }

            "launch" -> JSONObject().put(
                "performed",
                launchPackage(request.getString("package")),
            )

            "apps" -> JSONObject().put("apps", listLauncherApps())
            else -> throw IllegalArgumentException("unsupported command")
        }
    }

    private fun buildSnapshot(): JSONObject {
        val nodes = JSONArray()
        val root = rootInActiveWindow
        var rootPackage = activePackage
        synchronized(nodeLock) {
            clearNodeCacheLocked()
            if (root != null) {
                rootPackage = root.packageName?.toString().orEmpty().ifBlank { activePackage }
                val counter = intArrayOf(0)
                traverseNode(root, 0, counter, nodes)
            }
        }
        root?.recycle()

        val metrics = resources.displayMetrics
        @Suppress("DEPRECATION")
        val rotation = (getSystemService(WINDOW_SERVICE) as WindowManager)
            .defaultDisplay.rotation

        return JSONObject()
            .put("package", rootPackage)
            .put("activity", activeActivity)
            .put("rotation", rotation)
            .put("width", metrics.widthPixels)
            .put("height", metrics.heightPixels)
            .put("nodes", nodes)
    }

    private fun traverseNode(
        node: AccessibilityNodeInfo,
        depth: Int,
        counter: IntArray,
        output: JSONArray,
    ) {
        if (counter[0] >= MAX_NODES) return
        val nodeId = counter[0]++
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        nodeCache[nodeId] = AccessibilityNodeInfo.obtain(node)

        output.put(
            JSONObject()
                .put("id", nodeId)
                .put("text", node.text?.toString().orEmpty())
                .put("desc", node.contentDescription?.toString().orEmpty())
                .put("resourceId", node.viewIdResourceName.orEmpty())
                .put("className", node.className?.toString().orEmpty())
                .put("package", node.packageName?.toString().orEmpty())
                .put("clickable", node.isClickable)
                .put("editable", node.isEditable)
                .put("enabled", node.isEnabled)
                .put("focused", node.isFocused)
                .put("visible", node.isVisibleToUser)
                .put("depth", depth)
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
            if (counter[0] >= MAX_NODES) break
            val child = node.getChild(index) ?: continue
            try {
                traverseNode(child, depth + 1, counter, output)
            } finally {
                child.recycle()
            }
        }
    }

    private fun clickNode(nodeId: Int): Boolean {
        val cached = synchronized(nodeLock) { nodeCache[nodeId] } ?: return false
        var current: AccessibilityNodeInfo? = AccessibilityNodeInfo.obtain(cached)
        while (current != null) {
            if (current.isEnabled && current.isClickable &&
                current.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            ) {
                current.recycle()
                return true
            }
            val parent = current.parent
            current.recycle()
            current = parent
        }
        return false
    }

    private fun setNodeText(nodeId: Int?, text: String): Boolean {
        val target = if (nodeId != null) {
            synchronized(nodeLock) {
                nodeCache[nodeId]?.let { AccessibilityNodeInfo.obtain(it) }
            }
        } else {
            rootInActiveWindow?.let { root ->
                try {
                    root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                } finally {
                    root.recycle()
                }
            }
        } ?: return false

        return try {
            target.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            val args = Bundle().apply {
                putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    text,
                )
            }
            target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        } finally {
            target.recycle()
        }
    }

    private fun dispatchPath(
        x1: Int,
        y1: Int,
        x2: Int,
        y2: Int,
        durationMs: Long,
    ): Boolean {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            if (x1 != x2 || y1 != y2) {
                lineTo(x2.toFloat(), y2.toFloat())
            }
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0L, durationMs))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    private fun launchPackage(packageName: String): Boolean {
        val intent = packageManager.getLaunchIntentForPackage(packageName) ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        startActivity(intent)
        return true
    }

    private fun listLauncherApps(): JSONArray {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val resolved = if (Build.VERSION.SDK_INT >= 33) {
            packageManager.queryIntentActivities(
                intent,
                PackageManager.ResolveInfoFlags.of(PackageManager.MATCH_ALL.toLong()),
            )
        } else {
            @Suppress("DEPRECATION")
            packageManager.queryIntentActivities(intent, PackageManager.MATCH_ALL)
        }

        val unique = resolved
            .distinctBy { it.activityInfo.packageName }
            .sortedBy { it.loadLabel(packageManager).toString().lowercase() }
        val apps = JSONArray()
        for (info in unique) {
            apps.put(
                JSONObject()
                    .put("label", info.loadLabel(packageManager).toString())
                    .put("package", info.activityInfo.packageName)
                    .put("activity", info.activityInfo.name)
                    .put("source", "bridge"),
            )
        }
        return apps
    }

    private fun clearNodeCache() {
        synchronized(nodeLock) {
            clearNodeCacheLocked()
        }
    }

    private fun clearNodeCacheLocked() {
        nodeCache.values.forEach {
            try {
                it.recycle()
            } catch (_: Exception) {
                // Ignore stale node cleanup failures.
            }
        }
        nodeCache.clear()
    }

    @Suppress("UNCHECKED_CAST")
    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()

        val value = AtomicReference<Any?>()
        val error = AtomicReference<Throwable?>()
        val latch = CountDownLatch(1)
        mainHandler.post {
            try {
                value.set(block())
            } catch (throwable: Throwable) {
                error.set(throwable)
            } finally {
                latch.countDown()
            }
        }
        if (!latch.await(MAIN_THREAD_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            throw IllegalStateException("main thread operation timed out")
        }
        error.get()?.let { throw it }
        return value.get() as T
    }

    companion object {
        const val SOCKET_NAME = "semantic_mobile_agent"
        const val PREFS_NAME = "semantic_mobile_bridge"
        const val PREF_TOKEN = "bridge_token"
        private const val MAX_NODES = 600
        private const val MAX_REQUEST_CHARS = 1_000_000
        private const val MAIN_THREAD_TIMEOUT_SECONDS = 5L

        @Volatile
        var isRunning: Boolean = false
            private set
    }
}
