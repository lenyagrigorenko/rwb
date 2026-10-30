# Сборщик Android-приложений: JDK 17 + Android SDK (platform 35, build-tools 35) + Gradle 8.14.
FROM --platform=linux/amd64 eclipse-temurin:17-jdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk ANDROID_HOME=/opt/android-sdk GRADLE_USER_HOME=/cache/gradle
RUN apt-get update && apt-get install -y --no-install-recommends unzip curl && rm -rf /var/lib/apt/lists/* \
 && mkdir -p $ANDROID_SDK_ROOT/cmdline-tools \
 && curl -fsSL -o /tmp/tools.zip https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
 && unzip -q /tmp/tools.zip -d $ANDROID_SDK_ROOT/cmdline-tools && mv $ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools $ANDROID_SDK_ROOT/cmdline-tools/latest \
 && rm /tmp/tools.zip \
 && yes | $ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager --licenses > /dev/null \
 && $ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0" > /dev/null \
 && curl -fsSL -o /tmp/gradle.zip https://services.gradle.org/distributions/gradle-8.14.3-bin.zip \
 && unzip -q /tmp/gradle.zip -d /opt && ln -s /opt/gradle-8.14.3/bin/gradle /usr/local/bin/gradle && rm /tmp/gradle.zip
WORKDIR /src
