#!/bin/bash
set -eo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAA_DIR="$BASE_DIR/maa"
LOG_FILE="$MAA_DIR/update.log"
RESOURCE_REPO="${RESOURCE_REPO:-$HOME/.local/share/maa/MaaResource}"

mkdir -p "$MAA_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== 开始执行 MAA 每日双重自动更新 ==="

# ----------------- 1. 静态特征资源库更新 (MaaResource Hot-Update) -----------------
log "[1/2] 正在检查静态特征识别库 (MaaResource)..."
if [ -d "$RESOURCE_REPO/.git" ]; then
    cd "$RESOURCE_REPO"
    if git -C "$RESOURCE_REPO" pull --ff-only 2>&1 | tee -a "$LOG_FILE"; then
        log "[+] MaaResource 仓库拉取完成"
        if [ -d "$RESOURCE_REPO/resource" ]; then
            cp -ru "$RESOURCE_REPO/resource/"* "$MAA_DIR/resource/" 2>/dev/null || true
            log "[✔] 静态特征资源库已同步更新至 $MAA_DIR/resource/"
        fi
    else
        log "[!] git pull 异常，尝试通过 maa hot-update 更新..."
        cd "$MAA_DIR" && ./maa hot-update --batch >> "$LOG_FILE" 2>&1 || true
    fi
else
    log "[!] MaaResource 本地仓库不存在，正在克隆..."
    git clone --depth=1 https://github.com/MaaAssistantArknights/MaaResource.git "$RESOURCE_REPO" >> "$LOG_FILE" 2>&1 || true
fi

# ----------------- 2. MAA 核心软件及二进制更新 (MaaCore Release) -----------------
log "[2/2] 正在检查 MAA 核心主程序 (MaaCore) Release 版本..."
LATEST_TAG=$(curl -s --connect-timeout 10 -m 20 https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || true)

if [ -n "$LATEST_TAG" ]; then
    CURRENT_TAG=""
    if [ -f "$MAA_DIR/version.txt" ]; then
        CURRENT_TAG=$(cat "$MAA_DIR/version.txt" | tr -d ' \r\n')
    fi
    log "[+] 本地版本: ${CURRENT_TAG:-未知}, GitHub 最新版本: $LATEST_TAG"

    if [ "$CURRENT_TAG" != "$LATEST_TAG" ]; then
        log "[+] 发现新版本 ($CURRENT_TAG -> $LATEST_TAG)，开始下载更新包..."
        # 若访问 GitHub 缓慢，可将下方 URL 前缀替换为加速镜像，例如 https://ghfast.top/
        TAR_URL="https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/download/${LATEST_TAG}/MAA-${LATEST_TAG}-linux-x86_64.tar.gz"
        TMP_FILE="/tmp/MAA_${LATEST_TAG}.tar.gz"
        
        if curl -L -m 300 "$TAR_URL" -o "$TMP_FILE"; then
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

log "=== MAA 自动更新检查完成 ==="
