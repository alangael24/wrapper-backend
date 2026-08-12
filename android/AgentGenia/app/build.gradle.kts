plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

fun releaseSecret(name: String): String? = providers.gradleProperty(name).orNull
    ?: providers.environmentVariable(name).orNull

val releaseStoreFile = releaseSecret("AGENTGENIA_RELEASE_STORE_FILE")
val releaseStorePassword = releaseSecret("AGENTGENIA_RELEASE_STORE_PASSWORD")
val releaseKeyAlias = releaseSecret("AGENTGENIA_RELEASE_KEY_ALIAS")
val releaseKeyPassword = releaseSecret("AGENTGENIA_RELEASE_KEY_PASSWORD")
val releaseVersionCode = releaseSecret("AGENTGENIA_VERSION_CODE")?.toIntOrNull() ?: 1
android {
    namespace = "com.agentgenia.android"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.agentgenia.android"
        minSdk = 26
        targetSdk = 36
        versionCode = releaseVersionCode
        versionName = "1.0.0"

        buildConfigField("String", "API_BASE_URL", "\"https://agentgenia-api.onrender.com\"")
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables.useSupportLibrary = true
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    signingConfigs {
        create("release") {
            // A deliberately missing path makes Android's validateSigningRelease
            // fail closed when CI has not supplied the four private values.
            storeFile = rootProject.file(releaseStoreFile ?: ".missing-agentgenia-release-keystore")
            storePassword = releaseStorePassword ?: ""
            keyAlias = releaseKeyAlias ?: ""
            keyPassword = releaseKeyPassword ?: ""
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
            buildConfigField("boolean", "EXTERNAL_BILLING_ENABLED", "true")
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            buildConfigField("boolean", "EXTERNAL_BILLING_ENABLED", "false")
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    packaging.resources.excludes += setOf(
        "/META-INF/{AL2.0,LGPL2.1}",
        "META-INF/DEPENDENCIES"
    )
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2026.06.00")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.browser:browser:1.10.0")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    // Lifecycle 2.11 is compiled against the preview API 37. Keep the
    // production app on the latest line compatible with stable API 36.
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.10.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.10.0")
    implementation("androidx.navigation:navigation-compose:2.9.8")
    implementation("com.squareup.okhttp3:okhttp:5.4.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20260719")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
}
