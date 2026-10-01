#!/bin/bash
set -o pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$BASE_DIR/bin"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
APK_PATH="$BASE_DIR/arknights.apk"
ADB="${ADB_BIN:-$(which adb 2>/dev/null || echo "$HOME/.local/bin/adb")}"
DEVICE="127.0.0.1:5555"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "=== 开始检查明日方舟官方服版本更新 ==="

WAS_STOPPED=false

ensure_emu_running() {
    if ! docker ps --filter "name=redroid_arknights" --filter "status=running" | grep -q redroid_arknights; then
        log "[*] 检测到模拟器处于休眠状态，正在按需启动以检查官方服版本..."
        WAS_STOPPED=true
        docker compose -f "$COMPOSE_FILE" up -d
        local max_retry=30
        local count=0
        while [ $count -lt $max_retry ]; do
            if "$ADB" connect "$DEVICE" >/dev/null 2>&1; then
                local boot_status
                boot_status=$("$ADB" -s "$DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
                if [ "$boot_status" = "1" ]; then
                    log "[+] 模拟器已就绪，ADB 正常连通"
                    break
                fi
            fi
            sleep 2
            count=$((count + 1))
        done
    fi
}

cleanup_emu() {
    if [ "$WAS_STOPPED" = true ]; then
        log "[*] 更新任务结束，恢复模拟器休眠以释放硬件资源..."
        docker compose -f "$COMPOSE_FILE" stop
        "$ADB" disconnect "$DEVICE" >/dev/null 2>&1 || true
    fi
}

trap cleanup_emu EXIT

ensure_emu_running

# 获取当前模拟器中已安装的官方服 versionName
INSTALLED_VER=$("$ADB" -s "$DEVICE" shell dumpsys package com.hypergryph.arknights 2>/dev/null | grep -m1 "versionName" | awk -F'=' '{print $2}' | tr -d '\r\n ' || true)
log "[+] 模拟器当前已安装版本: ${INSTALLED_VER:-未安装}"

# 获取官网最新版本信息 (示例调用官方接口或直接下载最新包)
DOWNLOAD_URL="https://ak.hypergryph.com/downloads/android_lastest"
HEADER_INFO=$(curl -sI -L --connect-timeout 10 -m 20 "$DOWNLOAD_URL" || true)

# 比较并按需升级覆盖安装
# "$ADB" -s "$DEVICE" install -r "$APK_PATH"

log "=== 明日方舟更新检查完毕 ==="
