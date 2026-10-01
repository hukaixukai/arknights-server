#!/bin/bash
set -o pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$BASE_DIR/bin"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
APK_PATH="$BASE_DIR/arknights.apk"
ADB="${ADB_BIN:-$(which adb 2>/dev/null || echo "$HOME/.local/bin/adb")}"
DEVICE="${ADB_TARGET:-127.0.0.1:5555}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "=== 开始检查明日方舟官方服大版本更新 ==="

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
    else
        "$ADB" connect "$DEVICE" >/dev/null 2>&1 || true
    fi
}

ensure_emu_cleanup() {
    if [ "$WAS_STOPPED" = true ]; then
        log "[*] 检查/更新任务结束，将模拟器重新归位休眠以释放内存与 GPU..."
        docker compose -f "$COMPOSE_FILE" stop >/dev/null 2>&1 || true
        "$ADB" disconnect "$DEVICE" >/dev/null 2>&1 || true
        log "[✔] 模拟器已安全休眠。"
    fi
}

trap ensure_emu_cleanup EXIT

ensure_emu_running

# ----------------- 官方服 (HyperGryph) 检查与更新 -----------------
log "[+] 正在检查【官方服】客户端版本..."
CURRENT_VER=$("$ADB" -s "$DEVICE" shell dumpsys package com.hypergryph.arknights 2>/dev/null | grep -i versionName | head -n 1 | awk -F'=' '{print $2}' | tr -d ' \r\n' || true)
log "[+] 官方服当前安装版本: ${CURRENT_VER:-未安装}"

REDIRECT_URL=$(curl -sD - -o /dev/null -m 10 https://ak.hypergryph.com/downloads/android_lastest 2>&1 | grep -i '^location:' | tail -n 1 | awk '{print $2}' | tr -d ' \r\n' || true)
if [ -z "$REDIRECT_URL" ]; then
    REDIRECT_URL="https://launcher.hypergryph.com/game/latest/GzD1CpaWgmSq1wew/1/1"
fi

FINAL_URL=$(curl -sD - -o /dev/null -m 10 "$REDIRECT_URL" 2>&1 | grep -i '^location:' | tail -n 1 | awk '{print $2}' | tr -d ' \r\n' || true)
if [ -z "$FINAL_URL" ]; then
    FINAL_URL="https://ak.hypergryph.com/downloads/android_lastest"
fi

APK_NAME=$(basename "$FINAL_URL" | awk -F'?' '{print $1}')
log "[+] 官方 CDN 最新包名: $APK_NAME"

NEED_OFFICIAL_UPDATE=false
CDN_FORMATTED_VER=""

if [ "$1" = "--force" ]; then
    NEED_OFFICIAL_UPDATE=true
    log "[*] 强制更新模式启用"
elif [[ "$APK_NAME" =~ arknights-hg-([0-9]+)\.apk ]]; then
    CDN_RAW_VER="${BASH_REMATCH[1]}"
    CDN_FORMATTED_VER="${CDN_RAW_VER:0:1}.${CDN_RAW_VER:1:1}.${CDN_RAW_VER:2}"
    log "[+] 官方 CDN 最新版本为: $CDN_FORMATTED_VER"
    if [ "$CURRENT_VER" != "$CDN_FORMATTED_VER" ]; then
        log "[!] 检测到官方服版本落后 ($CURRENT_VER -> $CDN_FORMATTED_VER)，需要执行大更覆盖安装！"
        NEED_OFFICIAL_UPDATE=true
    else
        log "[✔] 官方服客户端已是最新版本 ($CURRENT_VER)，无需重复安装。"
    fi
else
    if [ -z "$CURRENT_VER" ]; then
        NEED_OFFICIAL_UPDATE=true
    fi
fi

if [ "$NEED_OFFICIAL_UPDATE" = true ]; then
    log "[*] 开始下载最新官方服 APK (支持断点续传)..."
    curl -L -C - --retry 3 --retry-delay 3 "$FINAL_URL" -o "$APK_PATH"
    log "[+] 下载完成: $(ls -lh "$APK_PATH" | awk '{print $5}')"

    log "[*] 执行无损覆盖安装 (保留玩家所有配置与登录缓存)..."
    "$ADB" -s "$DEVICE" install -r -d "$APK_PATH"
    log "[✔] 官方服覆盖安装成功！"

    # 发送更新通报
    python3 -c "
import sys, json, pathlib
sys.path.insert(0, '$BIN_DIR')
import notifier
p = pathlib.Path('$BASE_DIR/config/accounts.json')
if p.exists():
    with open(p, 'r') as f: data = json.load(f)
    acc = next((a for a in data.get('accounts', []) if a.get('platform') == 'Official'), None)
    if acc:
        notifier.dispatch_account_notify(acc, '【明日方舟大版本更新】官方服已自动升级', '### 官方服大版本更新完成\n- **版本升级**: $CURRENT_VER -> $CDN_FORMATTED_VER\n- **安装方式**: 无损覆盖安装 (已保留登录凭据)\n- **状态**: 模拟器已安全归位待命', event_type='on_daily_summary')
" 2>/dev/null || true
fi

log "=== 明日方舟官方服版本检查全部完毕 ==="