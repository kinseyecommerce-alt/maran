package com.algotrader.app

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.webkit.*
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.preference.PreferenceManager

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val prefs by lazy { PreferenceManager.getDefaultSharedPreferences(this) }

    private val serverUrl: String
        get() = prefs.getString("server_url", DEFAULT_URL) ?: DEFAULT_URL

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        setSupportActionBar(findViewById(R.id.toolbar))
        supportActionBar?.setDisplayShowTitleEnabled(false)

        webView = findViewById(R.id.webview)
        configureWebView()

        if (!isNetworkAvailable()) {
            showNetworkError()
        } else {
            loadDashboard()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_DEFAULT
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                // Keep navigation inside WebView for same-origin requests
                if (url.startsWith(serverUrl) || url.startsWith("/")) {
                    return false
                }
                return true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    val msg = when (error.errorCode) {
                        ERROR_HOST_LOOKUP, ERROR_CONNECT -> "Cannot reach server at $serverUrl"
                        ERROR_TIMEOUT -> "Connection timed out"
                        else -> "Load error: ${error.description}"
                    }
                    Toast.makeText(this@MainActivity, msg, Toast.LENGTH_LONG).show()
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onJsAlert(view: WebView, url: String, message: String, result: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("OK") { _, _ -> result.confirm() }
                    .setOnCancelListener { result.cancel() }
                    .show()
                return true
            }

            override fun onJsConfirm(view: WebView, url: String, message: String, result: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity)
                    .setMessage(message)
                    .setPositiveButton("OK") { _, _ -> result.confirm() }
                    .setNegativeButton("Cancel") { _, _ -> result.cancel() }
                    .setOnCancelListener { result.cancel() }
                    .show()
                return true
            }
        }
    }

    private fun loadDashboard() {
        val url = serverUrl.trimEnd('/') + "/dashboard"
        webView.loadUrl(url)
    }

    private fun isNetworkAvailable(): Boolean {
        val cm = getSystemService(ConnectivityManager::class.java)
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    private fun showNetworkError() {
        AlertDialog.Builder(this)
            .setTitle("No Network")
            .setMessage("Check your Wi-Fi or mobile data connection, then tap Retry.")
            .setPositiveButton("Retry") { _, _ ->
                if (isNetworkAvailable()) loadDashboard()
                else showNetworkError()
            }
            .setNegativeButton("Settings") { _, _ ->
                startActivity(Intent(this, SettingsActivity::class.java))
            }
            .setCancelable(false)
            .show()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            R.id.action_reload -> {
                webView.reload()
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack()
        else {
            AlertDialog.Builder(this)
                .setMessage("Exit AlgoTrader Pro?")
                .setPositiveButton("Exit") { _, _ -> super.onBackPressed() }
                .setNegativeButton("Stay", null)
                .show()
        }
    }

    override fun onResume() {
        super.onResume()
        // Reload if server URL was changed in SettingsActivity
        val currentBase = webView.url?.let {
            try { it.substringBefore("/dashboard") } catch (_: Exception) { "" }
        } ?: ""
        if (currentBase.isNotEmpty() && !webView.url.orEmpty().startsWith(serverUrl)) {
            loadDashboard()
        }
    }

    companion object {
        const val DEFAULT_URL = "http://192.168.1.100:8000"
    }
}
