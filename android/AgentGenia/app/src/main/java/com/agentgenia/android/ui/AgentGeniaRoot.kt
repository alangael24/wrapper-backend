package com.agentgenia.android.ui

import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.Android
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Extension
import androidx.compose.material.icons.filled.SmartToy
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalWindowInfo
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.core.net.toUri
import com.agentgenia.android.AppPhase
import com.agentgenia.android.AppUiState
import com.agentgenia.android.AppViewModel
import com.agentgenia.android.MainSection
import com.agentgenia.android.model.BotShape

@Composable
fun AgentGeniaRoot(model: AppViewModel) {
    val state by model.state.collectAsStateWithLifecycle()
    val context = LocalContext.current

    LaunchedEffect(state.externalUrl) {
        val url = state.externalUrl ?: return@LaunchedEffect
        runCatching {
            CustomTabsIntent.Builder()
                .setShowTitle(true)
                .setShareState(CustomTabsIntent.SHARE_STATE_OFF)
                .build()
                .launchUrl(context, url.toUri())
        }.onFailure { model.clearError() }
        model.consumeExternalUrl()
    }

    Box(Modifier.fillMaxSize()) {
        when (state.phase) {
            AppPhase.Loading -> LoadingScreen()
            AppPhase.SignedOut -> LoginScreen(state.busy, model::beginSignIn)
            AppPhase.Ready -> MainShell(state, model)
        }
        state.computerViewerUrl?.let { url ->
            ComputerViewer(url = url, onClose = model::dismissComputerViewer)
        }
    }

    state.error?.let { error ->
        AlertDialog(
            onDismissRequest = model::clearError,
            title = { Text("Agent Genia") },
            text = { Text(error) },
            confirmButton = { TextButton(onClick = model::clearError) { Text("OK") } },
        )
    }
}

@Composable
private fun LoadingScreen() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Mascot("#2F91F5", BotShape.Bean, 80.dp)
            CircularProgressIndicator()
            Text("Abriendo Agent Genia…", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun LoginScreen(busy: Boolean, signIn: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Mascot("#2F91F5", BotShape.Bean, 116.dp)
        Spacer(Modifier.height(24.dp))
        Text("Agent Genia", style = MaterialTheme.typography.displaySmall, fontWeight = FontWeight.Bold)
        Text("Dilo una vez. Déjalo hecho.", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(32.dp))
        Button(onClick = signIn, enabled = !busy, modifier = Modifier.fillMaxWidth().height(54.dp)) {
            if (busy) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
            else Icon(Icons.Default.AccountCircle, null)
            Spacer(Modifier.size(10.dp))
            Text("Continuar con Google")
        }
        Spacer(Modifier.height(16.dp))
        Text(
            "Tus bots, conectores y computadora se mantienen separados por cuenta.",
            textAlign = TextAlign.Center,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
private fun MainShell(state: AppUiState, model: AppViewModel) {
    val windowWidth = LocalWindowInfo.current.containerSize.width
    val wide = with(LocalDensity.current) { windowWidth.toDp() >= 840.dp }
    if (wide) {
        Row(Modifier.fillMaxSize()) {
            NavigationRail {
                Spacer(Modifier.height(12.dp))
                Mascot("#2F91F5", BotShape.Bean, 44.dp)
                Spacer(Modifier.height(24.dp))
                MainSection.entries.forEach { section ->
                    NavigationRailItem(
                        selected = state.section == section,
                        onClick = { model.selectSection(section) },
                        icon = { Icon(sectionIcon(section), sectionTitle(section)) },
                        label = { Text(sectionTitle(section)) },
                    )
                }
            }
            MainContent(state, model, wide = true, modifier = Modifier.weight(1f))
        }
    } else {
        Scaffold(
            bottomBar = {
                NavigationBar {
                    MainSection.entries.forEach { section ->
                        NavigationBarItem(
                            selected = state.section == section,
                            onClick = { model.selectSection(section) },
                            icon = { Icon(sectionIcon(section), sectionTitle(section)) },
                            label = { Text(sectionTitle(section)) },
                        )
                    }
                }
            },
        ) { padding -> MainContent(state, model, wide = false, modifier = Modifier.padding(padding)) }
    }
}

@Composable
private fun MainContent(state: AppUiState, model: AppViewModel, wide: Boolean, modifier: Modifier) {
    Box(modifier.fillMaxSize()) {
        when (state.section) {
            MainSection.Agents -> AgentsScreen(state, model, wide)
            MainSection.Plugins -> PluginsScreen(state, model)
            MainSection.Account -> AccountScreen(state, model)
        }
    }
}

private fun sectionTitle(section: MainSection) = when (section) {
    MainSection.Agents -> "Agentes"
    MainSection.Plugins -> "Plugins"
    MainSection.Account -> "Cuenta"
}

private fun sectionIcon(section: MainSection) = when (section) {
    MainSection.Agents -> Icons.Default.SmartToy
    MainSection.Plugins -> Icons.Default.Extension
    MainSection.Account -> Icons.Default.AccountCircle
}
