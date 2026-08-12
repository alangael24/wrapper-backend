package com.agentgenia.android

import android.app.Application
import com.agentgenia.android.data.AgentGeniaApi
import com.agentgenia.android.data.SecureStore

class AgentGeniaApplication : Application() {
    lateinit var secureStore: SecureStore
        private set
    lateinit var api: AgentGeniaApi
        private set

    override fun onCreate() {
        super.onCreate()
        secureStore = SecureStore(this)
        api = AgentGeniaApi(secureStore)
    }
}
