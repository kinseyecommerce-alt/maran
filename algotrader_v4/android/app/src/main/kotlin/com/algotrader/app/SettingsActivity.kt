package com.algotrader.app

import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.preference.PreferenceManager

class SettingsActivity : AppCompatActivity() {

    private val prefs by lazy { PreferenceManager.getDefaultSharedPreferences(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        setSupportActionBar(findViewById(R.id.toolbar_settings))
        supportActionBar?.apply {
            setDisplayHomeAsUpEnabled(true)
            title = getString(R.string.title_settings)
        }

        val urlField = findViewById<EditText>(R.id.edit_server_url)
        urlField.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        urlField.setText(prefs.getString("server_url", MainActivity.DEFAULT_URL))

        findViewById<Button>(R.id.btn_save).setOnClickListener {
            val raw = urlField.text.toString().trim()
            if (raw.isEmpty() || (!raw.startsWith("http://") && !raw.startsWith("https://"))) {
                urlField.error = "Must start with http:// or https://"
                return@setOnClickListener
            }
            prefs.edit().putString("server_url", raw.trimEnd('/')).apply()
            Toast.makeText(this, "Server URL saved", Toast.LENGTH_SHORT).show()
            finish()
        }

        findViewById<Button>(R.id.btn_reset).setOnClickListener {
            urlField.setText(MainActivity.DEFAULT_URL)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressed()
        return true
    }
}
