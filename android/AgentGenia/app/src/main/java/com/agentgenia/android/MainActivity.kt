package com.agentgenia.android

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.agentgenia.android.ui.AgentGeniaRoot
import com.agentgenia.android.ui.theme.AgentGeniaTheme

class MainActivity : ComponentActivity() {
    private val model by viewModels<AppViewModel> {
        AppViewModel.factory(application as AgentGeniaApplication)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        setContent {
            AgentGeniaTheme { AgentGeniaRoot(model) }
        }
    }
}
