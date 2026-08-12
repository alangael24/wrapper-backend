package com.agentgenia.android.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Divider
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.core.graphics.toColorInt
import com.agentgenia.android.AppUiState
import com.agentgenia.android.AppViewModel
import com.agentgenia.android.model.BOT_COLORS
import com.agentgenia.android.model.BotMessage
import com.agentgenia.android.model.BotProfile
import com.agentgenia.android.model.BotQuestionWidget
import com.agentgenia.android.model.BotShape
import com.agentgenia.android.model.ComputerState
import com.agentgenia.android.model.MessageRole

@Composable
fun AgentsScreen(state: AppUiState, model: AppViewModel, wide: Boolean) {
    val selected = state.bots.firstOrNull { it.id == state.selectedBotId }
    LaunchedEffect(selected?.id) { selected?.let { model.selectBot(it.id) } }
    if (wide) {
        Row(Modifier.fillMaxSize()) {
            AgentList(state, model, Modifier.width(330.dp).fillMaxHeight())
            HorizontalDivider(Modifier.fillMaxHeight().width(1.dp))
            if (selected == null) EmptyAgentSelection(Modifier.weight(1f))
            else ChatScreen(selected, state, model, onBack = null, modifier = Modifier.weight(1f))
        }
    } else if (selected == null) {
        AgentList(state, model, Modifier.fillMaxSize())
    } else {
        ChatScreen(selected, state, model, onBack = model::showAgentList, modifier = Modifier.fillMaxSize())
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AgentList(state: AppUiState, model: AppViewModel, modifier: Modifier) {
    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text("Agentes", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = model::createBot) {
                        Icon(Icons.Default.Add, "Crear un bot")
                    }
                },
            )
        },
    ) { padding ->
        if (state.bots.isEmpty()) {
            Column(
                Modifier.padding(padding).fillMaxSize().padding(32.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Text("Todavía no hay bots", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(8.dp))
                Text(
                    "Usa el botón + de arriba para crear uno. Se crea con el nombre “Nuevo bot”; después puedes tocar su icono para personalizarlo.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            LazyColumn(Modifier.padding(padding).fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp)) {
                items(state.bots, key = BotProfile::id) { bot ->
                    AgentRow(
                        bot = bot,
                        selected = state.selectedBotId == bot.id,
                        running = bot.id in state.runningBotIds,
                        onClick = { model.selectBot(bot.id) },
                    )
                }
            }
        }
    }
}

@Composable
private fun AgentRow(bot: BotProfile, selected: Boolean, running: Boolean, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(20.dp))
            .background(if (selected) MaterialTheme.colorScheme.surfaceContainer else Color.Transparent)
            .clickable(onClick = onClick)
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Mascot(bot.color, bot.shape, 48.dp)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(bot.name, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                bot.messages.lastOrNull()?.text ?: "Listo para comenzar",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                style = MaterialTheme.typography.bodySmall,
            )
        }
        if (running) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
    }
}

@Composable
private fun EmptyAgentSelection(modifier: Modifier) {
    Box(modifier, contentAlignment = Alignment.Center) {
        Text("Selecciona un bot", color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatScreen(
    bot: BotProfile,
    state: AppUiState,
    model: AppViewModel,
    onBack: (() -> Unit)?,
    modifier: Modifier,
) {
    var draft by remember(bot.id) { mutableStateOf("") }
    var settings by remember { mutableStateOf(false) }
    var computer by remember { mutableStateOf(false) }
    val running = bot.id in state.runningBotIds
    Scaffold(
        modifier = modifier.imePadding(),
        topBar = {
            TopAppBar(
                navigationIcon = {
                    if (onBack != null) IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Volver")
                    }
                },
                title = {
                    Row(
                        Modifier.clip(RoundedCornerShape(12.dp)).clickable { settings = true }.padding(4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Mascot(bot.color, bot.shape, 34.dp)
                        Spacer(Modifier.width(9.dp))
                        Text(bot.name, fontWeight = FontWeight.Bold)
                    }
                },
                actions = {
                    IconButton(onClick = {
                        computer = true
                        model.loadComputer(bot.id)
                    }) { Icon(Icons.Default.Computer, "Computadora") }
                    IconButton(onClick = { settings = true }) { Icon(Icons.Default.Settings, "Personalizar") }
                },
            )
        },
        bottomBar = {
            Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it.take(20_000) },
                    modifier = Modifier.weight(1f),
                    enabled = !running,
                    placeholder = { Text("Mensaje para ${bot.name}") },
                    shape = RoundedCornerShape(28.dp),
                    maxLines = 5,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(onSend = {
                        if (draft.isNotBlank()) { model.sendMessage(bot.id, draft); draft = "" }
                    }),
                )
                Spacer(Modifier.width(8.dp))
                FilledIconButton(
                    onClick = { model.sendMessage(bot.id, draft); draft = "" },
                    enabled = draft.isNotBlank() && !running,
                ) { if (running) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp) else Icon(Icons.AutoMirrored.Filled.Send, "Enviar") }
            }
        },
    ) { padding ->
        MessageTimeline(
            messages = bot.messages,
            running = running,
            onWidgetAnswer = { model.sendMessage(bot.id, it) },
            modifier = Modifier.padding(padding),
        )
    }
    if (settings) BotSettingsDialog(bot, model, onDismiss = { settings = false })
    if (computer) ComputerDialog(bot, state, model, onDismiss = { computer = false })
}

@Composable
private fun MessageTimeline(
    messages: List<BotMessage>,
    running: Boolean,
    onWidgetAnswer: (String) -> Unit,
    modifier: Modifier,
) {
    val listState = rememberLazyListState()
    LaunchedEffect(messages.size, running) {
        if (messages.isNotEmpty()) listState.animateScrollToItem(messages.lastIndex + if (running) 1 else 0)
    }
    LazyColumn(
        modifier.fillMaxSize(),
        state = listState,
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        items(messages, key = BotMessage::id) { message -> MessageBubble(message, onWidgetAnswer) }
        if (running) item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(10.dp))
                Text("Trabajando…", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun MessageBubble(message: BotMessage, onWidgetAnswer: (String) -> Unit) {
    Row(Modifier.fillMaxWidth()) {
        if (message.role == MessageRole.User) Spacer(Modifier.weight(1f))
        Surface(
            modifier = Modifier.fillMaxWidth(if (message.role == MessageRole.User) .82f else .94f),
            shape = RoundedCornerShape(22.dp),
            color = if (message.role == MessageRole.User) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceContainer,
            contentColor = if (message.role == MessageRole.User) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface,
        ) {
            Column(Modifier.padding(15.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(message.text)
                message.widget?.let { QuestionWidget(it, onWidgetAnswer) }
            }
        }
        if (message.role == MessageRole.Assistant) Spacer(Modifier.weight(1f))
    }
}

@Composable
private fun QuestionWidget(widget: BotQuestionWidget, submit: (String) -> Unit) {
    var custom by remember(widget.prompt) { mutableStateOf("") }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(widget.prompt, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        if (widget.helpText.isNotBlank()) Text(widget.helpText, color = MaterialTheme.colorScheme.onSurfaceVariant)
        widget.options.forEachIndexed { index, option ->
            OutlinedButton(onClick = { submit(option.value) }, modifier = Modifier.fillMaxWidth()) {
                Text(('A'.code + index).toChar().toString())
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text(option.label, fontWeight = FontWeight.Medium)
                    if (option.description.isNotBlank()) Text(option.description, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        if (widget.allowCustom) {
            OutlinedTextField(
                value = custom,
                onValueChange = { custom = it.take(1_000) },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Escribe tu propia respuesta") },
                trailingIcon = {
                    IconButton(onClick = { submit(custom); custom = "" }, enabled = custom.isNotBlank()) {
                        Icon(Icons.AutoMirrored.Filled.Send, "Enviar")
                    }
                },
            )
        }
    }
}

@Composable
private fun BotSettingsDialog(bot: BotProfile, model: AppViewModel, onDismiss: () -> Unit) {
    var name by remember(bot.id) { mutableStateOf(bot.name) }
    var title by remember(bot.id) { mutableStateOf(bot.title) }
    var description by remember(bot.id) { mutableStateOf(bot.description) }
    var color by remember(bot.id) { mutableStateOf(bot.color) }
    var shape by remember(bot.id) { mutableStateOf(bot.shape) }
    var notifications by remember(bot.id) { mutableStateOf(bot.notificationsEnabled) }
    Dialog(onDismissRequest = onDismiss) {
        Surface(shape = RoundedCornerShape(28.dp), tonalElevation = 8.dp) {
            LazyColumn(
                Modifier.fillMaxWidth().padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                item {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Personalizar", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                        IconButton(onClick = onDismiss) { Icon(Icons.Default.Close, "Cerrar") }
                    }
                }
                item { Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) { Mascot(color, shape, 96.dp) } }
                item { OutlinedTextField(name, { name = it }, label = { Text("Nombre") }, modifier = Modifier.fillMaxWidth()) }
                item { OutlinedTextField(title, { title = it }, label = { Text("Título") }, modifier = Modifier.fillMaxWidth()) }
                item { OutlinedTextField(description, { description = it }, label = { Text("Descripción") }, modifier = Modifier.fillMaxWidth(), minLines = 3) }
                item {
                    Text("Color", fontWeight = FontWeight.SemiBold)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        BOT_COLORS.forEach { item ->
                            val value = runCatching { Color(item.toColorInt()) }.getOrDefault(Color.Blue)
                            Box(
                                Modifier.size(if (color == item) 28.dp else 24.dp)
                                    .background(value, CircleShape)
                                    .clickable { color = item },
                                contentAlignment = Alignment.Center,
                            ) { if (color == item) Icon(Icons.Default.Check, null, tint = Color.White, modifier = Modifier.size(15.dp)) }
                        }
                    }
                }
                item {
                    Text("Forma", fontWeight = FontWeight.SemiBold)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        BotShape.entries.forEach { item ->
                            Surface(
                                shape = RoundedCornerShape(12.dp),
                                color = if (shape == item) MaterialTheme.colorScheme.surfaceContainer else Color.Transparent,
                                modifier = Modifier.clickable { shape = item },
                            ) { Mascot(color, item, 38.dp, Modifier.padding(5.dp)) }
                        }
                    }
                }
                item {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Notifications, null)
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text("Notificaciones", fontWeight = FontWeight.SemiBold)
                            Text("Cuando el agente termine o necesite ayuda", style = MaterialTheme.typography.bodySmall)
                        }
                        Switch(notifications, { notifications = it })
                    }
                }
                item {
                    Button(
                        onClick = {
                            model.updateBot(bot.id, name, title, description, color, shape, notifications)
                            onDismiss()
                        },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Guardar") }
                }
            }
        }
    }
}

@Composable
private fun ComputerDialog(bot: BotProfile, state: AppUiState, model: AppViewModel, onDismiss: () -> Unit) {
    val snapshot = state.computer.takeIf { state.computerBotId == bot.id }
    AlertDialog(
        onDismissRequest = onDismiss,
        icon = { Icon(Icons.Default.Computer, null, Modifier.size(40.dp)) },
        title = { Text("Computadora de ${bot.name}") },
        text = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(when (snapshot?.state) {
                    ComputerState.Running -> "Está encendida y lista."
                    ComputerState.Hibernated -> "Está hibernada; conserva archivos y sesiones."
                    ComputerState.Pulling -> "Preparando la computadora…"
                    ComputerState.Disabled -> snapshot.reason.ifBlank { "Las computadoras no están habilitadas." }
                    ComputerState.Error -> snapshot.reason.ifBlank { "No pudimos abrir la computadora." }
                    ComputerState.Off -> "Aún no se ha creado."
                    null -> "Consultando estado…"
                })
                if (state.busy || snapshot == null || snapshot.state == ComputerState.Pulling) {
                    Spacer(Modifier.height(18.dp)); CircularProgressIndicator()
                }
            }
        },
        confirmButton = {
            when (snapshot?.state) {
                ComputerState.Running -> Button(onClick = { model.openComputer(bot.id) }) { Text("Abrir") }
                ComputerState.Hibernated, ComputerState.Off, ComputerState.Error -> Button(
                    onClick = { model.ensureComputer(bot.id) }, enabled = !state.busy,
                ) { Text(if (snapshot.state == ComputerState.Hibernated) "Despertar" else "Crear") }
                else -> Unit
            }
        },
        dismissButton = {
            Row {
                if (snapshot?.state == ComputerState.Running) TextButton(onClick = { model.handBackComputer(bot.id) }) { Text("Hibernar") }
                TextButton(onClick = onDismiss) { Text("Cerrar") }
            }
        },
    )
}
