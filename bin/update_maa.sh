#!/bin/bash
set -eo pipefail

if [ -d "/app" ]; then
    BASE_DIR="${ARK_BASE_DIR:-/app}"
else
    BASE_DIR="${ARK_BASE_DIR:-/srv/docker/containers/arknights}"
fi

MAA_DIR="$BASE_DIR/maa"
LOG_FILE="$MAA_DIR/update.log"
RESOURCE_REPO="${MAA_RESOURCE_REPO:-$MAA_DIR/MaaResource}"

mkdir -p "$MAA_DIR"
git config --global --add safe.directory "*" 2>/dev/null || true

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== 开始执行 MAA 每日三重自动更新 (资源库/核心/活动关卡) ==="

# ----------------- 1. 静态特征资源库更新 (MaaResource Hot-Update) -----------------
log "[1/3] 正在检查静态特征识别库 (MaaResource)..."
if [ -d "$RESOURCE_REPO/.git" ]; then
    cd "$RESOURCE_REPO"
    if git -C "$RESOURCE_REPO" pull --ff-only 2>&1 | tee -a "$LOG_FILE"; then
        log "[+] MaaResource 仓库拉取完成"
        if [ -d "$RESOURCE_REPO/resource" ]; then
            cp -ru "$RESOURCE_REPO/resource/"* "$MAA_DIR/resource/" 2>/dev/null || true
            log "[✔] 静态特征资源库已同步更新至 $MAA_DIR/resource/"
        fi
    else
        log "[!] git pull 异常，尝试重置并重新拉取..."
        git -C "$RESOURCE_REPO" fetch origin main 2>&1 | tee -a "$LOG_FILE" || true
        git -C "$RESOURCE_REPO" reset --hard origin/main 2>&1 | tee -a "$LOG_FILE" || true
        if [ -d "$RESOURCE_REPO/resource" ]; then
            cp -ru "$RESOURCE_REPO/resource/"* "$MAA_DIR/resource/" 2>/dev/null || true
        fi
    fi
else
    log "[!] MaaResource 本地仓库不存在，正在克隆..."
    git clone --depth=1 https://ghfast.top/https://github.com/MaaAssistantArknights/MaaResource.git "$RESOURCE_REPO" 2>&1 | tee -a "$LOG_FILE" || \
    git clone --depth=1 https://github.com/MaaAssistantArknights/MaaResource.git "$RESOURCE_REPO" 2>&1 | tee -a "$LOG_FILE" || true
    if [ -d "$RESOURCE_REPO/resource" ]; then
        cp -ru "$RESOURCE_REPO/resource/"* "$MAA_DIR/resource/" 2>/dev/null || true
    fi
fi

# ----------------- 2. MAA 核心软件及二进制更新 (MaaCore Release) -----------------
log "[2/3] 正在检查 MAA 核心主程序 (MaaCore) Release 版本..."

# 优先通过 GitHub 重定向探测最新 Tag (零 API Rate Limit 频次限制)，其次回退 GitHub API 与国内镜像
LATEST_TAG=$(curl -sIL --connect-timeout 8 -m 15 https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/latest | grep -i "location:" | awk -F'/' '{print $NF}' | tr -d ' \r\n' || true)

if [ -z "$LATEST_TAG" ]; then
    LATEST_TAG=$(curl -s --connect-timeout 8 -m 15 https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || true)
fi

if [ -z "$LATEST_TAG" ]; then
    LATEST_TAG=$(curl -s --connect-timeout 8 -m 15 https://ghfast.top/https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || true)
fi

if [ -n "$LATEST_TAG" ]; then
    CURRENT_TAG=""
    if [ -f "$MAA_DIR/version.txt" ]; then
        CURRENT_TAG=$(cat "$MAA_DIR/version.txt" | tr -d ' \r\n')
    fi
    log "[+] 本地版本: ${CURRENT_TAG:-未知}, GitHub 最新版本: $LATEST_TAG"

    if [ "$CURRENT_TAG" != "$LATEST_TAG" ]; then
        log "[+] 发现新版本 ($CURRENT_TAG -> $LATEST_TAG)，开始下载更新包..."
        TAR_URL="https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/download/${LATEST_TAG}/MAA-${LATEST_TAG}-linux-x86_64.tar.gz"
        TMP_FILE="/tmp/MAA_${LATEST_TAG}.tar.gz"
        
        if ! curl -L -m 300 "$TAR_URL" -o "$TMP_FILE" 2>/dev/null; then
            TAR_URL="https://ghfast.top/https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/download/${LATEST_TAG}/MAA-${LATEST_TAG}-linux-x86_64.tar.gz"
            curl -L -m 300 "$TAR_URL" -o "$TMP_FILE"
        fi

        if [ -s "$TMP_FILE" ]; then
            log "[+] 下载完成，备份旧核心库并热替换..."
            mkdir -p "$MAA_DIR/backup"
            cp -f "$MAA_DIR/libMaaCore.so" "$MAA_DIR/backup/libMaaCore.so.bak" 2>/dev/null || true
            tar -xzf "$TMP_FILE" -C "$MAA_DIR/"
            echo "$LATEST_TAG" > "$MAA_DIR/version.txt"
            rm -f "$TMP_FILE"
            log "[✔] MAA 核心主程序成功升级至 $LATEST_TAG"
        else
            log "[!] 下载更新包失败，继续保留当前稳定版本"
        fi
    else
        log "[+] MAA 核心主程序已是最新版本 ($CURRENT_TAG)，无需升级"
    fi
else
    log "[!] 网络请求超时或未能解析到最新 Release Tag，跳过软件二进制更新"
fi

# ----------------- 3. 在线活动关卡列表刷新 (gui/StageActivityV2.json) -----------------
# 与 MAA 桌面端一致：活动关卡选择名单来源于官方 API，此处每日刷新落盘缓存，
# 前端理智作战关卡选择器读取本地缓存 (maa/cache/gui/StageActivityV2.json)。
log "[3/3] 正在刷新在线活动关卡列表 (gui/StageActivityV2.json)..."
python3 - << 'PY_ACT' 2>&1 | tee -a "$LOG_FILE" || true
import sys, os
base = os.environ.get("ARK_BASE_DIR", "")
if not base:
    base = "/app" if os.path.isdir("/app") else "/srv/docker/containers/arknights"
sys.path.insert(0, os.path.join(base, "bin"))
try:
    import maa_config_adapter as m
    ok = m.refresh_stage_activity_cache("Official")
    print("[OK] 在线活动关卡列表已刷新落盘" if ok else "[!] 活动关卡列表刷新失败(保留旧缓存)")
except Exception as e:
    print("[!] 活动关卡列表刷新异常: " + str(e))
PY_ACT

log "=== MAA 自动更新检查完成 ==="