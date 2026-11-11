plugins {
    id("com.android.application")
}

android {
    namespace = "com.introvoice.mic"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.introvoice.mic"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"
    }

    // 本机打包用固定自签名证书（不随仓库分发）；没有证书时退回 debug 签名，
    // 保证 clone 下来也能直接构建。
    val sharedKey = file("introvoice.keystore")
    signingConfigs {
        create("shared") {
            if (sharedKey.exists()) {
                storeFile = sharedKey
                storePassword = "introvoice"
                keyAlias = "introvoice"
                keyPassword = "introvoice"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (sharedKey.exists())
                signingConfigs.getByName("shared") else signingConfigs.getByName("debug")
        }
        debug {
            if (sharedKey.exists()) signingConfig = signingConfigs.getByName("shared")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // 内置扫码：库自带 CaptureActivity，扫完回调到 onActivityResult。
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
