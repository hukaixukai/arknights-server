#!/bin/bash

if [ -d "/app/bin" ]; then BASE_DIR="/app"; else BASE_DIR="${ARK_BASE_DIR:-/srv/docker/containers/arknights}"; fi
MAA_DIR="$BASE_DIR/maa"
BIN_DIR="$BASE_DIR/bin"
ADB="$(which adb 2>/dev/null || echo /usr/bin/adb)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"

# 确保 binderfs 内核驱动就绪 (仅用于 ReDroid 安卓底层通信，不触碰任何 Web 网页)
ensure_binderfs() {
    if [ ! -c /dev/binderfs/binder-control ]; then
        echo "[+] 正在初始化 binderfs 驱动..."
        docker run --rm --privileged --pid=host alpine nsenter -t 1 -m -u -i -n -p sh -c '
            modprobe binder_linux || true
            mkdir -p /dev/binderfs
            mountpoint -q /dev/binderfs || mount -t binder binder /dev/binderfs
            chmod 777 /dev/binderfs
        ' >/dev/null 2>&1 || true
    fi
}

# 启动模拟器 (纯后台托管，绝不自动开启 Web 投屏)
start_emu() {
    echo "[*] 正在启动安卓模拟器 (ReDroid)..."
    ensure_binderfs
    docker compose -p arknights -f "$COMPOSE_FILE" up -d redroid

    echo "[+] 等待安卓系统启动并连接 ADB (127.0.0.1:5555)..."
    local max_retry=40
    local count=0
    local adbd_restarted=false
    while [ $count -lt $max_retry ]; do
        "$ADB" disconnect 127.0.0.1:5555 >/dev/null 2>&1 || true
        "$ADB" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
        local dev_state=""
        dev_state=$("$ADB" -s 127.0.0.1:5555 get-state 2>/dev/null | tr -d '\r\n')
        # 处理 recreate 后常见的 device offline：重启容器内 adbd 并重连
        if [ "$dev_state" != "device" ] && [ "$adbd_restarted" = false ] && [ $count -ge 6 ]; then
            echo "[!] ADB 设备离线，正在重启模拟器内 adbd 守护..."
            docker exec redroid_arknights sh -c "setprop service.adb.tcp.port 5555; stop adbd; start adbd" >/dev/null 2>&1 || true
            adbd_restarted=true
            sleep 4
            "$ADB" disconnect 127.0.0.1:5555 >/dev/null 2>&1 || true
            "$ADB" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
        fi
        local boot_status=""
        boot_status=$("$ADB" -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null || true)
        boot_status=$(echo "$boot_status" | tr -d '\r\n')
        if [ "$boot_status" = "1" ]; then
            echo "[+] 安卓系统启动完成，ADB 就绪！"
            "$ADB" -s 127.0.0.1:5555 root >/dev/null 2>&1 || true
            sleep 2
            "$ADB" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
            # 强制校准时区为北京时间 (redroid 默认 GMT，会导致游戏内时间错位 8 小时)
            "$ADB" -s 127.0.0.1:5555 shell "setprop persist.sys.timezone Asia/Shanghai" >/dev/null 2>&1 || true
            "$ADB" -s 127.0.0.1:5555 shell "setprop ro.boot.timezone Asia/Shanghai" >/dev/null 2>&1 || true
            # 时钟与核心服务预热防抖 (避免 ARM 转译层在证书与时区未就绪时发生 SIGSEGV 段错误)
            echo "[+] 正在进行时区与核心服务预热防抖 (等待 5s 确保完全就绪)..."
            "$ADB" -s 127.0.0.1:5555 shell "date '+%Y-%m-%d %H:%M:%S %Z'" >/dev/null 2>&1 || true
            sleep 5
            break
        fi
        sleep 2
        count=$((count + 1))
    done
}

# 停止模拟器以释放 CPU/内存 (不影响 Workbench Web 服务状态)
stop_emu() {
    echo "[*] 正在关闭安卓模拟器以释放 CPU/内存..."
    docker compose -p arknights -f "$COMPOSE_FILE" stop redroid
    "$ADB" disconnect 127.0.0.1:5555 >/dev/null 2>&1 || true
    echo "[✔] 容器已停止，硬件资源完全释放。"
}

restart_emu() {
    stop_emu
    sleep 2
    start_emu
}

start_game() {
    local pkg="com.hypergryph.arknights"
    echo "[+] 启动明日方舟客户端 ($pkg)..."
    "$ADB" -s 127.0.0.1:5555 shell am start -n "$pkg/com.u8.sdk.U8UnityContext" || true

    # 启动防抖与闪退自愈检测
    sleep 3.5
    local pid=""
    pid=$("$ADB" -s 127.0.0.1:5555 shell "pidof $pkg" 2>/dev/null | tr -d '\r\n')
    if [ -z "$pid" ]; then
        echo "[!] 检测到方舟客户端首启未检测到进程 (可能触发冷启动时区抖动)，正在执行自动自愈重拉..."
        sleep 2
        "$ADB" -s 127.0.0.1:5555 shell am start -n "$pkg/com.u8.sdk.U8UnityContext" || true
        sleep 3
        pid=$("$ADB" -s 127.0.0.1:5555 shell "pidof $pkg" 2>/dev/null | tr -d '\r\n')
        if [ -n "$pid" ]; then
            echo "[✔] 方舟自愈重拉成功 (PID=$pid)，客户端已进入平稳运行状态！"
        else
            echo "[!] 二次唤醒仍在异步启动中，交由 MAA 任务链继续识别。"
        fi
    else
        echo "[✔] 方舟客户端启动平稳 (PID=$pid)。"
    fi
}

show_status() {
    echo "=== 明日方舟全自动挂机系统运行状态 ==="
    echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "--- 容器状态 ---"
    docker ps -a --filter "name=redroid_arknights" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo "--- ADB 连接状态 ---"
    "$ADB" devices
    echo "--- 方舟交互网页状态 (Workbench 独立卡片) ---"
    if curl -s -m 2 http://127.0.0.1:8090/ >/dev/null 2>&1; then
        echo "方舟交互网页 (8090): 开启 (用户在 Workbench 手动启用)"
    else
        echo "方舟交互网页 (8090): 已关闭 (平时默认关闭，保护隐私与端口安全)"
    fi
    echo "--- 系统资源占用 ---"
    echo "CPU 负载: $(uptime | awk -F'load average:' '{print $2}')"
    free -h | grep -E 'Mem|内存'
}

update_maa() {
    "$BIN_DIR/update_maa.sh"
}

update_game() {
    "$BIN_DIR/update_arknights.sh"
}

run_pipeline() {
    local task_type="${1:-daily}"
    local arg2="$2"
    local arg3="$3"
    local auto_stop=false

    # 系统级排他锁，防止定时调度与手动并发启动
    exec 200>/tmp/ark_service_ctl.lock
    if ! flock -n 200; then
        echo "[!] 检测到已有任务流水线正在调度执行，为保障模拟器安全，本次触发放弃执行。"
        return 0
    fi

    if ! docker ps --filter "name=redroid_arknights" --filter "status=running" | grep -q redroid_arknights; then
        echo "[*] 检测到安卓模拟器未运行，正在按需自动拉起..."
        start_emu
        auto_stop=true
        # 冷启动时由托管系统提前拉起方舟并执行自愈检测，让 MAA 介入时处于完全就绪状态
        start_game
    fi

    echo "[*] 启动 MAA 任务: $task_type $arg2 $arg3"
    timeout --preserve-status --kill-after=15s 3000s python3 -u "$BIN_DIR/maa_runner.py" "$task_type" "$arg2" "$arg3" || true

    if [ "$auto_stop" = true ]; then
        echo "[*] 任务已完成，按需停止模拟器以保护 J4105 资源..."
        stop_emu
    fi
    flock -u 200
}

case "$1" in
    start|start-emu)
        start_emu
        ;;
    stop|stop-emu)
        stop_emu
        ;;
    restart|restart-emu)
        restart_emu
        ;;
    start-game)
        start_game
        ;;
    status)
        show_status
        ;;
    update-maa)
        update_maa
        ;;
    update-game)
        update_game
        ;;
    run|run-daily)
        run_pipeline "daily" "$2"
        ;;
    run-recruit)
        run_pipeline "recruit" "$2"
        ;;
    run-fight)
        run_pipeline "fight" "$2" "$3"
        ;;
    run-copilot)
        echo "[!] 暂未配置独立 copilot 作业流"
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|start-game|run-daily|run-recruit|run-fight <stage>|run-copilot <file>|update-maa|update-game}"
        exit 1
        ;;
esac
