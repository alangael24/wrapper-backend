package com.agentgenia.android.ui.theme

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

private val LightColors = lightColorScheme(
    primary = Color(0xFF171717),
    onPrimary = Color.White,
    secondary = Color(0xFF2F91F5),
    surface = Color(0xFFFFFFFF),
    surfaceContainer = Color(0xFFF3F3F3),
    background = Color(0xFFFAFAFA),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFF7F7F7),
    onPrimary = Color(0xFF171717),
    secondary = Color(0xFF64AFFF),
    surface = Color(0xFF151515),
    surfaceContainer = Color(0xFF222222),
    background = Color(0xFF101010),
)

@Composable
fun AgentGeniaTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val colors = if (dark) DarkColors else LightColors
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT < 35) window.statusBarColor = Color.Transparent.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !dark
        }
    }
    MaterialTheme(colorScheme = colors, content = content)
}
