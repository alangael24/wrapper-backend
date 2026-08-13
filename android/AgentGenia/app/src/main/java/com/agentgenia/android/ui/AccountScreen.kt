package com.agentgenia.android.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.AccountCircle
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.OpenInBrowser
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.unit.dp
import com.agentgenia.android.AppUiState
import com.agentgenia.android.AppViewModel
import com.agentgenia.android.BuildConfig
import com.agentgenia.android.model.BillingPlan
import java.text.NumberFormat
import java.util.Currency

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AccountScreen(state: AppUiState, model: AppViewModel) {
    var confirmsDeletion by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { model.refreshBilling() }
    if (confirmsDeletion) {
        AlertDialog(
            onDismissRequest = { confirmsDeletion = false },
            title = { Text("¿Eliminar tu cuenta definitivamente?") },
            text = { Text("Se eliminarán bots, sesiones, conectores y computadoras. Esta acción no se puede deshacer.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmsDeletion = false
                    model.deleteAccount()
                }) { Text("Eliminar cuenta", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { confirmsDeletion = false }) { Text("Cancelar") }
            },
        )
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Cuenta", fontWeight = FontWeight.Bold) },
                actions = { IconButton(onClick = model::refreshBilling) { Icon(Icons.Default.Refresh, "Actualizar") } },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.padding(padding).fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(18.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            item {
                Card(shape = RoundedCornerShape(24.dp)) {
                    Row(Modifier.fillMaxWidth().padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.AccountCircle, null, Modifier.size(58.dp), tint = MaterialTheme.colorScheme.secondary)
                        Spacer(Modifier.width(14.dp))
                        Column(Modifier.weight(1f)) {
                            Text(state.account?.name?.ifBlank { "Agent Genia" } ?: "Agent Genia", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                            Text(state.account?.email.orEmpty(), color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text("Plan ${state.billing?.tier?.replaceFirstChar { it.uppercase() } ?: state.profile?.tierLabel.orEmpty()}", style = MaterialTheme.typography.labelLarge)
                        }
                    }
                }
            }
            item { Text("Suscripción", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold) }
            val billing = state.billing
            if (billing == null) {
                item { Text("Consultando tu plan…", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            } else if (!BuildConfig.EXTERNAL_BILLING_ENABLED) {
                item {
                    Text(
                        "Tu plan se administra en agentgenia.com. Esta versión de Google Play no ofrece compras ni enlaces externos de pago.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else if (!billing.configured) {
                item { Text("Los pagos no están disponibles en este momento.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            } else if (billing.customer) {
                item {
                    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
                        Column(Modifier.fillMaxWidth().padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.CheckCircle, null, tint = MaterialTheme.colorScheme.secondary)
                                Spacer(Modifier.width(8.dp))
                                Text("Plan ${billing.tier.replaceFirstChar { it.uppercase() }} activo", fontWeight = FontWeight.Bold)
                            }
                            Button(onClick = model::openBillingPortal, enabled = !state.busy) {
                                Icon(Icons.Default.OpenInBrowser, null)
                                Spacer(Modifier.width(8.dp))
                                Text("Administrar en el sitio web")
                            }
                        }
                    }
                }
            } else {
                billing.plans.forEach { (id, plan) ->
                    item(id) { PlanCard(id, plan, state.busy) { model.openCheckout(id) } }
                }
                item {
                    Text(
                        "La suscripción se completa en el sitio seguro de Agent Genia.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            item {
                OutlinedButton(onClick = model::signOut, modifier = Modifier.fillMaxWidth()) {
                    Icon(Icons.AutoMirrored.Filled.Logout, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Cerrar sesión")
                }
            }
            item {
                TextButton(
                    onClick = { confirmsDeletion = true },
                    enabled = !state.busy,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Eliminar cuenta y datos", color = MaterialTheme.colorScheme.error)
                }
            }
            item {
                Text(
                    "Agent Genia ${BuildConfig.VERSION_NAME} · ${BuildConfig.API_BASE_URL.removePrefix("https://")}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun PlanCard(id: String, plan: BillingPlan, busy: Boolean, choose: () -> Unit) {
    val formatter = NumberFormat.getCurrencyInstance().apply {
        currency = runCatching { Currency.getInstance(plan.currency.uppercase()) }.getOrDefault(Currency.getInstance("USD"))
    }
    Card(shape = RoundedCornerShape(24.dp)) {
        Column(Modifier.fillMaxWidth().padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(plan.name, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text("${formatter.format(plan.amount.toDouble())} / ${if (plan.interval == "month") "mes" else plan.interval}")
            Text(
                "${plan.monthlyCredits} créditos · ${plan.maxConcurrentRuns} ejecución${if (plan.maxConcurrentRuns == 1) "" else "es"} simultánea${if (plan.maxConcurrentRuns == 1) "" else "s"}",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Button(onClick = choose, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("Elegir ${plan.name}") }
        }
    }
}
