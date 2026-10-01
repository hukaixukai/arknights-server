#!/bin/bash
set -e

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAA_DIR="$BASE_DIR/maa"
BIN_DIR="$BASE_DIR/bin"
ADB="${ADB_BIN:-$(which adb 2>/dev/null || echo "$HOME/.local/bin/adb")}"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"

# 确保 binderfs 内核驱动就绪 (仅用于 ReDroid 安卓底层通信)
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

# 启动模拟器 (纯后台托管)
start_emu() {
    echo "[*] 正在启动安卓模拟器 (ReDroid)..."
    ensure_binderfs
    docker compose -f "$COMPOSE_FILE" up -d

    echo "[+] 等待安卓系统启动并连接 ADB (127.0.0.1:5555)..."
    local max_retry=30
    local count=0
    while [ $count -lt $max_retry ]; do
        "$ADB" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
        local boot_status=""
        boot_status=$("$ADB" -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null || true)
        boot_status=$(echo "$boot_status" | tr -d '\r\n')
        if [ "$boot_status" = "1" ]; then
            echo "[+] 安卓系统启动完成，ADB 就绪！"
            "$ADB" -s 127.0.0.1:5555 root >/dev/null 2>&1 || true
            sleep 2
            "$ADB" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
            break
        fi
        sleep 2
        count=$((count + 1))
    done
}

# 停止模拟器以释放 CPU/内存
stop_emu() {
    echo "[*] 正在关闭安卓模拟器以释放 CPU/内存..."
    docker compose -f "$COMPOSE_FILE" stop
    "$ADB" disconnect 127.0.0.1:5555 >/dev/null 2>&1 || true
    echo "[✔] 容器已停止，硬件资源完全释放。"
}

restart_emu() {
    stop_emu
    sleep 2
    start_emu
}

start_game() {
    echo "[+] 启动明日方舟客户端..."
    "$ADB" -s 127.0.0.1:5555 shell am start -n com.hypergryph.arknights/com.u8.sdk.U8UnityContext || true
}

show_status() {
    echo "=== 明日方舟全自动挂机系统运行状态 ==="
    echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "--- 容器状态 ---"
    docker ps -a --filter "name=redroid_arknights" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo "--- ADB 连接状态 ---"
    "$ADB" devices
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

    # 单班次统一执行时长上限（秒）。无论是否跑完，到点即终止。
    local max_runtime="${ARK_TASK_MAX_SECONDS:-3600}"

    # 系统级排他锁，防止定时调度与手动并发启动
    exec 200>/tmp/ark_service_ctl.lock
    if ! flock -n 200; then
        # 已有班次在跑：新班次不再放弃，而是礼貌地请旧班次让位。
        # 这是设计行为——每个班次固定占用约一小时，到点必须交接给下一班。
        echo "[!] 检测到上一班次尚未结束，正在请求其退出以让位给新班次..."

        # 先向旧的 maa_runner 进程发送 SIGTERM，它会按“被顶号/超时”逻辑优雅收尾。
        # 注意：调度锁由旧的 ark_service_ctl.sh 持有，旧脚本会在其 timeout 返回后释放锁；
        # 因此这里不能只杀 Python 进程，还必须等待锁真正释放。
        local old_pids
        old_pids=$(pgrep -f "maa_runner.py" || true)
        if [ -n "$old_pids" ]; then
            echo "[*] 向旧任务进程发送 SIGTERM 请求优雅退出: $old_pids"
            kill -TERM $old_pids 2>/dev/null || true
        else
            echo "[*] 未发现运行中的 MAA 进程，可能是残留锁，直接等待释放。"
        fi

        # 轮询等待旧班次释放调度锁（最长 90 秒）
        local waited=0
        local got_lock=false
        while [ $waited -lt 90 ]; do
            if flock -n 200; then
                got_lock=true
                break
            fi
            sleep 2
            waited=$((waited + 2))
        done

        if [ "$got_lock" != true ]; then
            # 超时仍拿不到锁，强制清理旧进程后做最后一次尝试
            echo "[!] 等待 90 秒后旧班次仍未释放，执行强制终止。"
            pkill -9 -f "maa_runner.py" 2>/dev/null || true
            sleep 3
            if ! flock -n 200; then
                echo "[✗] 抢占失败，旧班次锁未释放，本次班次放弃执行（已记录日志）。"
                return 0
            fi
        fi
        echo "[✔] 旧班次已让位，新班次开始。"
    fi

    if ! docker ps --filter "name=redroid_arknights" --filter "status=running" | grep -q redroid_arknights; then
        echo "[*] 检测到安卓模拟器未运行，正在按需自动拉起..."
        start_emu
        auto_stop=true
    fi

    echo "[*] 启动 MAA 任务: $task_type $arg2 $arg3 (单班次上限 ${max_runtime}s)"
    # timeout 语义: 到点先发 SIGTERM 请求优雅退出，15 秒后仍未退出则 SIGKILL。
    # 收到超时信号通常意味着账号被顶号，maa_runner 会按“放弃”处理并记录日志。
    timeout --preserve-status --kill-after=15s "${max_runtime}s" \
        python3 -u "$BIN_DIR/maa_runner.py" "$task_type" "$arg2" "$arg3" || true

    if [ "$auto_stop" = true ]; then
        echo "[*] 任务已完成，按需停止模拟器以保护硬件资源..."
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
    *)
        echo "用法: $0 {start|stop|restart|status|start-game|run-daily|run-recruit|run-fight <stage>|update-maa|update-game}"
        exit 1
        ;;
esac
