package com.agentgenia.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.agentgenia.android.AppUiState
import com.agentgenia.android.AppViewModel
import com.agentgenia.android.model.ConnectorCatalog
import com.agentgenia.android.model.ConnectorDefinition
import com.agentgenia.android.model.ConnectorStatus

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PluginsScreen(state: AppUiState, model: AppViewModel) {
    var yours by remember { mutableStateOf(false) }
    var search by remember { mutableStateOf("") }
    LaunchedEffect(Unit) { model.refreshConnectors() }
    val filtered = ConnectorCatalog.all.filter { connector ->
        val status = state.connectorStatuses[connector.id]
        (!yours || status?.connected == true) && (search.isBlank() || listOf(
            connector.name, connector.summary, connector.category,
        ).any { it.contains(search, ignoreCase = true) })
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Plugins", fontWeight = FontWeight.Bold) },
                actions = { IconButton(onClick = model::refreshConnectors) { Icon(Icons.Default.Refresh, "Actualizar") } },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            PrimaryTabRow(selectedTabIndex = if (yours) 1 else 0) {
                Tab(selected = !yours, onClick = { yours = false }, text = { Text("Marketplace") })
                Tab(selected = yours, onClick = { yours = true }, text = { Text("Tuyos") })
            }
            OutlinedTextField(
                value = search,
                onValueChange = { search = it.take(100) },
                modifier = Modifier.fillMaxWidth().padding(16.dp),
                leadingIcon = { Icon(Icons.Default.Search, null) },
                placeholder = { Text("Buscar plugins") },
                singleLine = true,
            )
            if (filtered.isEmpty()) {
                Column(
                    Modifier.fillMaxSize().padding(32.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center,
                ) {
                    Text(if (yours) "Todavía no has conectado plugins" else "No encontramos plugins", fontWeight = FontWeight.SemiBold)
                    Text(
                        if (yours) "Cambia a Marketplace para conectar una cuenta." else "Prueba otra búsqueda.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                LazyColumn(
                    Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
                ) {
                    val groups = filtered.groupBy { it.category }.toSortedMap()
                    groups.forEach { (category, connectors) ->
                        item(category) {
                            Text(
                                category,
                                style = MaterialTheme.typography.labelLarge,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(top = 18.dp, bottom = 8.dp),
                            )
                        }
                        items(connectors, key = ConnectorDefinition::id) { connector ->
                            ConnectorRow(connector, state.connectorStatuses[connector.id], state.busy, model)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ConnectorRow(
    connector: ConnectorDefinition,
    status: ConnectorStatus?,
    busy: Boolean,
    model: AppViewModel,
) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        ConnectorLogo(connector.name, connector.id, Modifier.size(52.dp))
        Spacer(Modifier.width(13.dp))
        Column(Modifier.weight(1f)) {
            Text(connector.name, fontWeight = FontWeight.SemiBold)
            Text(
                connector.summary,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            when {
                status?.connected == true && status.account.isNotBlank() -> Text(status.account, style = MaterialTheme.typography.labelSmall)
                status?.available == false && status.reason.isNotBlank() -> Text(status.reason, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.error)
            }
        }
        Spacer(Modifier.width(8.dp))
        if (status?.connected == true) {
            TextButton(onClick = { model.disconnect(connector.id) }, enabled = !busy) {
                Icon(Icons.Default.Check, null, Modifier.size(17.dp))
                Spacer(Modifier.width(4.dp))
                Text("Añadido")
            }
        } else {
            OutlinedButton(
                onClick = { model.connect(connector.id) },
                enabled = status?.available == true && !busy,
            ) { Text("Añadir") }
        }
    }
}
