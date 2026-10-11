#!/bin/bash
set -e

export TZ="Asia/Shanghai"
export ARK_BASE_DIR="/app"
export ADB_BIN="${ADB_BIN:-/usr/bin/adb}"
export ADB_TARGET="${ADB_TARGET:-127.0.0.1:5555}"
export PORT="${PORT:-8090}"

# 确保符号链接兼容性
mkdir -p /opt/arknights
ln -sfn /app /opt/arknights/arknights

mkdir -p /root/.cache
# 启动 cron 守护进程
echo "[+] 启动 cronie 任务调度器..."
crond -s

# 1. 写入基底定时任务：每日凌晨 04:30 MAA 更新 + 04:45 方舟游戏更新
echo "[+] 初始化系统运维定时任务 (04:30 MAA 双更 / 04:45 方舟 CDN 更新)..."
cat << "CRON_BASE" | crontab -
CRON_TZ=Asia/Shanghai
ARK_BASE_DIR=/app
TZ=Asia/Shanghai
ARK_BASE_DIR=/app
30 4 * * * /app/bin/update_maa.sh >> /app/maa/update.log 2>&1
45 4 * * * /app/bin/update_arknights.sh >> /app/update_apk.log 2>&1
CRON_BASE

# 2. 从 SQLite / accounts 同步生成最新多账号排班调度
if [ -f /app/bin/scheduler_guard.py ]; then
    echo "[+] 同步多账号排班调度到系统 crontab..."
    python3 -c "import sys; sys.path.insert(0, \"/app/bin\"); import scheduler_guard; scheduler_guard.sync_all_system_crontabs()" 2>/dev/null || true
fi

# 3. 启动 Web 控制面板
echo "[+] 启动明日方舟 Web 控制面板 (端口: ${PORT})..."
exec python3 /app/bin/web_touch.py
