#!/usr/bin/env bash
# Builds build/droid-agent.jar, the dex jar droid_mcp pushes to the device and runs with app_process.
# Needs a JDK and the Android SDK (a platform for android.jar, build-tools for d8).
set -euo pipefail
cd "$(dirname "$0")"

sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}
android_jar=$sdk/platforms/android-${AGENT_PLATFORM:-36}/android.jar
d8=$(ls -d "$sdk"/build-tools/*/ | sort -V | tail -1)d8

rm -rf build
mkdir -p build/classes
javac --release 11 -Xlint:-options -cp "$android_jar" -d build/classes $(find src -name '*.java')
"$d8" --min-api 26 --lib "$android_jar" --output build/droid-agent.jar $(find build/classes -name '*.class')
echo "built $(pwd)/build/droid-agent.jar"
