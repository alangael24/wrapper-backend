package com.agentgenia.android.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.Image
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.RoundRect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.agentgenia.android.model.BotShape
import com.agentgenia.android.R
import androidx.core.graphics.toColorInt

@Composable
fun Mascot(
    color: String,
    shape: BotShape,
    size: Dp,
    modifier: Modifier = Modifier,
) {
    val fill = runCatching { Color(color.toColorInt()) }.getOrDefault(Color(0xFF2F91F5))
    Canvas(modifier.size(size)) {
        drawMascotBody(fill, shape)
        val eyeWidth = this.size.width * .10f
        val eyeHeight = this.size.height * .25f
        val y = this.size.height * .34f
        rotate(-8f, Offset(this.size.width * .42f, y)) {
            drawRoundRect(Color.White, Offset(this.size.width * .36f, y), androidx.compose.ui.geometry.Size(eyeWidth, eyeHeight), CornerRadius(eyeWidth))
        }
        rotate(-8f, Offset(this.size.width * .64f, y)) {
            drawRoundRect(Color.White, Offset(this.size.width * .59f, y), androidx.compose.ui.geometry.Size(eyeWidth, eyeHeight), CornerRadius(eyeWidth))
        }
    }
}

private fun DrawScope.drawMascotBody(color: Color, shape: BotShape) {
    val w = size.width
    val h = size.height
    when (shape) {
        BotShape.Circle -> drawCircle(color, w * .42f, Offset(w / 2, h / 2))
        BotShape.Bean -> {
            val path = Path().apply {
                moveTo(w * .50f, h * .08f)
                cubicTo(w * .20f, h * .08f, w * .10f, h * .35f, w * .14f, h * .60f)
                cubicTo(w * .18f, h * .88f, w * .46f, h * .96f, w * .70f, h * .86f)
                cubicTo(w * .92f, h * .76f, w * .92f, h * .42f, w * .76f, h * .20f)
                cubicTo(w * .68f, h * .10f, w * .60f, h * .08f, w * .50f, h * .08f)
                close()
            }
            drawPath(path, color)
        }
        BotShape.Square -> drawRoundRect(color, Offset(w * .12f, h * .12f), androidx.compose.ui.geometry.Size(w * .76f, h * .76f), CornerRadius(w * .16f))
        BotShape.Capsule -> drawRoundRect(color, Offset(w * .06f, h * .25f), androidx.compose.ui.geometry.Size(w * .88f, h * .52f), CornerRadius(h * .30f))
        BotShape.Triangle -> {
            val path = Path().apply { moveTo(w / 2, h * .06f); lineTo(w * .94f, h * .88f); lineTo(w * .08f, h * .88f); close() }
            drawPath(path, color)
        }
        BotShape.Hexagon -> {
            val path = Path().apply {
                moveTo(w * .28f, h * .08f); lineTo(w * .72f, h * .08f); lineTo(w * .94f, h * .5f)
                lineTo(w * .72f, h * .92f); lineTo(w * .28f, h * .92f); lineTo(w * .06f, h * .5f); close()
            }
            drawPath(path, color)
        }
        BotShape.Cloud -> {
            drawCircle(color, w * .27f, Offset(w * .31f, h * .55f))
            drawCircle(color, w * .31f, Offset(w * .53f, h * .42f))
            drawCircle(color, w * .24f, Offset(w * .76f, h * .58f))
            drawRoundRect(color, Offset(w * .18f, h * .48f), androidx.compose.ui.geometry.Size(w * .65f, h * .32f), CornerRadius(w * .15f))
        }
        BotShape.Drop -> {
            val path = Path().apply {
                moveTo(w * .50f, h * .05f)
                cubicTo(w * .42f, h * .22f, w * .12f, h * .49f, w * .14f, h * .68f)
                cubicTo(w * .18f, h * .94f, w * .48f, h * .98f, w * .69f, h * .88f)
                cubicTo(w * .93f, h * .75f, w * .86f, h * .47f, w * .50f, h * .05f)
                close()
            }
            drawPath(path, color)
        }
    }
}

@Composable
fun ConnectorLogo(name: String, id: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier.background(MaterialTheme.colorScheme.surfaceContainer, RoundedCornerShape(13.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Image(
            painter = painterResource(connectorLogoResource(id)),
            contentDescription = name,
            contentScale = ContentScale.Fit,
            modifier = Modifier.size(31.dp),
        )
    }
}

private fun connectorLogoResource(id: String): Int = when (id) {
    "google-workspace" -> R.drawable.logo_google_workspace
    "slack" -> R.drawable.logo_slack
    "notion" -> R.drawable.logo_notion
    "salesforce" -> R.drawable.logo_salesforce
    "microsoft-365" -> R.drawable.logo_microsoft_365
    "linkedin" -> R.drawable.logo_linkedin
    "zoom" -> R.drawable.logo_zoom
    "github" -> R.drawable.logo_github
    "jira" -> R.drawable.logo_jira
    "linear" -> R.drawable.logo_linear
    "asana" -> R.drawable.logo_asana
    "clickup" -> R.drawable.logo_clickup
    "figma" -> R.drawable.logo_figma
    "hubspot" -> R.drawable.logo_hubspot
    "canva" -> R.drawable.logo_canva
    "trello" -> R.drawable.logo_trello
    "monday-com" -> R.drawable.logo_monday_com
    "intercom" -> R.drawable.logo_intercom
    "zendesk" -> R.drawable.logo_zendesk
    "box" -> R.drawable.logo_box
    "dropbox" -> R.drawable.logo_dropbox
    "docusign" -> R.drawable.logo_docusign
    "calendly" -> R.drawable.logo_calendly
    "loom" -> R.drawable.logo_loom
    "outreach" -> R.drawable.logo_outreach
    "salesloft" -> R.drawable.logo_salesloft
    "apollo" -> R.drawable.logo_apollo
    "clay" -> R.drawable.logo_clay
    "zoominfo" -> R.drawable.logo_zoominfo
    "nooks" -> R.drawable.logo_nooks
    "stripe" -> R.drawable.logo_stripe
    "quickbooks" -> R.drawable.logo_quickbooks
    "netsuite" -> R.drawable.logo_netsuite
    "ramp" -> R.drawable.logo_ramp
    "workday" -> R.drawable.logo_workday
    "rippling" -> R.drawable.logo_rippling
    "ashby" -> R.drawable.logo_ashby
    "greenhouse" -> R.drawable.logo_greenhouse
    "vercel" -> R.drawable.logo_vercel
    "tableau" -> R.drawable.logo_tableau
    "hex" -> R.drawable.logo_hex
    "amplitude" -> R.drawable.logo_amplitude
    "mixpanel" -> R.drawable.logo_mixpanel
    "snowflake" -> R.drawable.logo_snowflake
    "databricks" -> R.drawable.logo_databricks
    "mailchimp" -> R.drawable.logo_mailchimp
    "shopify" -> R.drawable.logo_shopify
    "tiendanube" -> R.drawable.logo_tiendanube
    "woocommerce" -> R.drawable.logo_woocommerce
    else -> R.drawable.ic_launcher_foreground
}
