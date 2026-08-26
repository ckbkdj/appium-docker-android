package ai.semantic.mobile.bridge

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import java.util.UUID

class MainActivity : Activity() {
    private lateinit var statusView: TextView
    private lateinit var tokenInput: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences(FastBridgeService.PREFS_NAME, MODE_PRIVATE)
        var token = prefs.getString(FastBridgeService.PREF_TOKEN, "").orEmpty()
        if (token.isBlank()) {
            token = UUID.randomUUID().toString().replace("-", "")
            prefs.edit().putString(FastBridgeService.PREF_TOKEN, token).apply()
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(24))
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }

        root.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 24f
        })

        root.addView(TextView(this).apply {
            text = "This helper has no Internet permission. It listens only on the Android local abstract socket and requires an explicit ADB forward from the authorized host."
            textSize = 15f
            setPadding(0, dp(12), 0, dp(16))
        })

        statusView = TextView(this).apply {
            textSize = 17f
            setPadding(0, 0, 0, dp(16))
        }
        root.addView(statusView)

        root.addView(TextView(this).apply {
            text = getString(R.string.token_label)
            textSize = 14f
        })

        tokenInput = EditText(this).apply {
            setText(token)
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
        }
        root.addView(tokenInput)

        root.addView(Button(this).apply {
            text = getString(R.string.save_token)
            setOnClickListener {
                val updated = tokenInput.text.toString().trim()
                if (updated.length < 16) {
                    Toast.makeText(
                        this@MainActivity,
                        "Use a random token with at least 16 characters",
                        Toast.LENGTH_LONG,
                    ).show()
                    return@setOnClickListener
                }
                prefs.edit().putString(FastBridgeService.PREF_TOKEN, updated).apply()
                Toast.makeText(this@MainActivity, "Token saved", Toast.LENGTH_SHORT).show()
            }
        })

        root.addView(Button(this).apply {
            text = getString(R.string.open_accessibility_settings)
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        })

        root.addView(TextView(this).apply {
            text = "Host example:\n" +
                "adb -s emulator-5554 forward tcp:27183 localabstract:${FastBridgeService.SOCKET_NAME}"
            textSize = 13f
            setPadding(0, dp(20), 0, 0)
            setTextIsSelectable(true)
        })

        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        statusView.text = if (FastBridgeService.isRunning) {
            getString(R.string.status_running)
        } else {
            getString(R.string.status_stopped)
        }
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
