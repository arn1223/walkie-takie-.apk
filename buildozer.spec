[app]
# (str) Title of your application
title = PTT Walkie Talkie

# (str) Package name
package.name = pttwalkietalkie

# (str) Package domain (needed for android/ios packaging)
package.domain = org.walkietalkie

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas

# (str) Application versioning
version = 0.1

# (list) Application requirements
# comma separated e.g. requirements = sqlite3,kivy
requirements = python3,kivy,pyjnius

# (str) Supported orientation (one of landscape, sensorLandscape, portrait or all)
orientation = portrait

# (list) Permissions
android.permissions = RECORD_AUDIO,INTERNET,ACCESS_NETWORK_STATE,CHANGE_WIFI_MULTICAST_STATE,ACCESS_WIFI_STATE,ACCESS_COARSE_LOCATION

# (bool) Automatically accept SDK license
android.accept_sdk_license = True

# (bool) Skip Android SDK/NDK updates
android.skip_update = False

# (int) Target Android API: Android 14/15/16 ready
android.api = 34

# (int) Minimum API your APK will support (Android 5.0 through Android 16+)
android.minapi = 21

# (str) Android NDK version to use
android.ndk = 25b

# (list) Dual Architecture: 32-bit (armeabi-v7a) + 64-bit (arm64-v8a)
android.archs = arm64-v8a, armeabi-v7a

# (bool) Copy library instead of making a libpymodules.so
android.copy_libs = 1

# (str) The format used to package the app for release mode (aab or apk)
android.release_artifact = apk

# (str) The format used to package the app for debug mode (apk)
android.debug_artifact = apk

[buildozer]
# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1
