import select
import threading
import os
import sys
import json
import re
import time
import hmac
import base64
import signal
import shutil
import secrets
import hashlib
import pathlib
import datetime
import subprocess
import urllib.parse
import urllib.request
import http.server
import socketserver

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
BIN_DIR = BASE_DIR / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

# 统一时区为北京时间 (游戏日 04:00 刷新、日志日期、调度展示均以此为准)
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass

import db
import scheduler_guard
import maa_config_adapter

DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")
# 会话签名密钥：优先取环境变量；未设置时退化为进程级随机值（重启后登录态失效，属预期）
SESSION_HMAC_KEY = os.getenv("SESSION_HMAC_KEY", secrets.token_hex(32)).encode("utf-8")
ACTIVE_USER_SESSIONS = {}

def create_session_token(username: str, role: str, display_name: str) -> str:
    exp = int(time.time()) + 30 * 86400  # 30 天有效期
    payload = f"{username}|{role}|{display_name}|{exp}"
    sig = hmac.new(SESSION_HMAC_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")

def verify_session_token(token: str):
    if not token or not isinstance(token, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(token.strip().strip('"\'').encode("ascii")).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 5:
            return None
        username, role, display_name, exp_str, sig = parts
        exp = int(exp_str)
        if time.time() > exp:
            return None
        expected_payload = f"{username}|{role}|{display_name}|{exp}"
        expected_sig = hmac.new(SESSION_HMAC_KEY, expected_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return {
                "username": username,
                "role": role,
                "display_name": display_name
            }
    except Exception:
        pass
    return None

def find_adb():
    candidate = shutil.which("adb")
    if candidate:
        return candidate
    local_adb = pathlib.Path.home() / ".local" / "bin" / "adb"
    if local_adb.exists():
        return str(local_adb)
    return "adb"

PORT = int(os.environ.get("PORT", 8090))
ADB = os.getenv("ADB_BIN", find_adb())
def get_live_device():
    try:
        out = subprocess.check_output([ADB, "devices"], text=True)
        devices = [l.split()[0] for l in out.splitlines() if "\tdevice" in l]
        if "emulator-5554" in devices: return "emulator-5554"
        if "127.0.0.1:5555" in devices: return "127.0.0.1:5555"
        if devices: return devices[0]
    except Exception:
        pass
    return "127.0.0.1:5555"

DEVICE = get_live_device()

CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
MAA_DIR = BASE_DIR / "maa"
INFRAST_DIR = BASE_DIR / "maa" / "infrast"
DEPOT_FILE = BASE_DIR / "maa" / "data" / "DepotData.json"
DEBUG_DIR = BASE_DIR / "maa" / "debug"
LOG_FILE = DEBUG_DIR / "asst.log"
RUNNER_SCRIPT = BASE_DIR / "bin" / "maa_runner.py"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
# 容器由 compose 项目 arknights 创建；/app 目录默认项目名会变成 "app"，导致
# docker compose stop/up 静默失效 (模拟器停不掉)。显式指定项目名修复。
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "arknights")

def compose_cmd(*args):
    return ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(COMPOSE_FILE)] + list(args)

def emulator_running():
    """检测 redroid 模拟器容器是否在运行 (供智能退出策略使用)。"""
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", "redroid_arknights"],
            text=True, stderr=subprocess.DEVNULL, timeout=8)
        return out.strip() == "true"
    except Exception:
        return False

_EMULATOR_OP_LOCK = threading.Lock()

def start_emulator_blocking(timeout_sec=90):
    """
    启动 redroid 模拟器并等待系统就绪 + ADB 连接 + 校准时区。
    带全局互斥锁，防止与 stop/其他 start 竞态。返回 (ok, message)。
    """
    with _EMULATOR_OP_LOCK:
        if emulator_running():
            # 已运行：仅确认 ADB 连接即可
            try:
                subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except Exception:
                pass
            return True, "模拟器已在运行"
        try:
            subprocess.run(compose_cmd("up", "-d", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except Exception as e:
            return False, f"启动容器失败: {e}"
        # 等待 boot_completed + ADB 就绪 (处理 recreate 后常见 adb offline)
        import time as _t
        deadline = _t.time() + timeout_sec
        adbd_restarted = False
        while _t.time() < deadline:
            try:
                subprocess.run([ADB, "disconnect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                st = subprocess.run([ADB, "-s", DEVICE, "get-state"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=6).stdout.strip()
                if st != "device":
                    if not adbd_restarted and _t.time() > deadline - timeout_sec + 15:
                        subprocess.run(["docker", "exec", "redroid_arknights", "sh", "-c",
                                        "setprop service.adb.tcp.port 5555; stop adbd; start adbd"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                        adbd_restarted = True
                        _t.sleep(4)
                    _t.sleep(2)
                    continue
                boot = subprocess.run([ADB, "-s", DEVICE, "shell", "getprop", "sys.boot_completed"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=6).stdout.strip()
                if boot == "1":
                    subprocess.run([ADB, "-s", DEVICE, "root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    _t.sleep(2)
                    subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                    # 校准时区为北京时间
                    subprocess.run([ADB, "-s", DEVICE, "shell", "setprop", "persist.sys.timezone", "Asia/Shanghai"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                    subprocess.run([ADB, "-s", DEVICE, "shell", "setprop", "ro.boot.timezone", "Asia/Shanghai"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
                    return True, "模拟器已启动并就绪"
            except Exception:
                pass
            _t.sleep(2)
        return False, "模拟器启动超时，请稍后重试"

def stop_emulator_blocking():
    """停止 redroid 模拟器 (带全局互斥锁)。返回 (ok, message)。"""
    with _EMULATOR_OP_LOCK:
        try:
            subprocess.run([ADB, "-s", DEVICE, "shell", "am", "force-stop", "com.hypergryph.arknights"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            pass
        try:
            subprocess.run(compose_cmd("stop", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            subprocess.run([ADB, "disconnect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            return True, "模拟器已停止"
        except Exception as e:
            return False, str(e)

def get_copilot_jobs_list(username=None):
    """仅返回用户私有上传的作业（已彻底移除内置作业库预设）。"""
    categories = []
    user_uploads = []
    u_dir = BASE_DIR / "data" / "copilot_uploads"
    if username:
        owner_dir = u_dir / username
        if owner_dir.exists():
            for f in sorted(owner_dir.glob("*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as jf:
                        meta = json.load(jf)
                    doc = meta.get("doc", {}) if isinstance(meta, dict) else {}
                    user_uploads.append({
                        "filename": f.name,
                        "title": doc.get("title") or f.stem,
                        "details": doc.get("details", ""),
                        "category": "我的上传",
                        "is_user": True
                    })
                except Exception:
                    user_uploads.append({"filename": f.name, "title": f.stem, "details": "", "category": "我的上传", "is_user": True})

    if user_uploads:
        categories.append({"name": "我的专属作业", "items": user_uploads})

    return categories

def adb_cmd(args):
    cmd = [ADB, "-s", DEVICE] + args
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def is_emulator_alive():
    try:
        res = adb_cmd(["get-state"])
        return res.returncode == 0 and b"device" in res.stdout
    except Exception:
        return False


RUNNING_TASK_FILE = CONFIG_DIR / "running_task.json"

def resolve_active_account_for_user(cur_user, explicit_acc_id=None):
    """
    解析请求对应的有效账号 ID，实现多用户隔离：
    - 显式指定时直接使用 (越权校验交由各 endpoint 完成)
    - 未指定时：管理员取全局激活账号；普通用户取其自身维度的激活账号
    """
    if explicit_acc_id:
        return explicit_acc_id
    if cur_user and cur_user.get("role") != "admin":
        return db.get_active_account_id(owner_username=cur_user.get("username"))
    return db.get_active_account_id()

def get_current_taskchain():
    """从 MAA asst.log 尾部读取当前正在执行的 taskchain (供网页实时展示)。"""
    try:
        if not LOG_FILE.exists():
            return ""
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "ignore")
        matches = re.findall(r'"taskchain"\s*:\s*"([A-Za-z]+)"', tail)
        if matches:
            return matches[-1]
    except Exception:
        pass
    return ""


def get_running_task_info():
    """
    权威托管状态：以「真实 maa_runner.py 进程」为准。
    - 若 DB 标记 running 但已无 runner 进程 (容器重启/异常退出留下脏状态)，自动清理并返回 None；
    - 若存在 runner 进程，返回其账号/归属信息。
    """
    real_running = is_task_running()
    task = db.get_running_task()
    if not real_running:
        # 清理脏状态，避免"明明没在跑却显示托管中"
        if task and task.get("running"):
            try:
                db.set_running_task({"running": False})
            except Exception:
                pass
        return None
    if task and task.get("running") and task.get("account_id"):
        return task
    acc = db.get_account(db.get_active_account_id())
    if acc:
        return {
            "running": True,
            "account_id": acc.get("id"),
            "account_name": acc.get("name"),
            "owner_username": acc.get("owner_username", DEFAULT_ADMIN_USER)
        }
    return {"running": True, "account_id": "", "account_name": "", "owner_username": ""}

RUNNER_LOCK_PATH = "/tmp/ark_runner.lock"

def _iter_runner_pids():
    """
    精确列出正在执行的 maa_runner.py 进程 PID。
    仅当进程 argv[0] 是 python 解释器 且 argv[1] 以 maa_runner.py 结尾时才算命中，
    避免 `pgrep -f maa_runner.py` 误匹配到命令行里含该字符串的 shell/包装进程。
    """
    pids = []
    try:
        for pid_dir in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                raw = (pid_dir / "cmdline").read_bytes()
            except Exception:
                continue
            if not raw:
                continue
            parts = [p.decode("utf-8", "ignore") for p in raw.split(b"\x00") if p]
            if len(parts) < 2:
                continue
            argv0 = os.path.basename(parts[0])
            if "python" not in argv0:
                continue
            # argv[1] 应为 .../maa_runner.py
            if any(os.path.basename(a) == "maa_runner.py" for a in parts[1:3]):
                try:
                    pids.append(int(pid_dir.name))
                except Exception:
                    continue
    except Exception:
        pass
    return pids

def is_task_running():
    """权威判定：存在真实 maa_runner.py 进程 (精确 argv 匹配)。"""
    return len(_iter_runner_pids()) > 0

def stop_task_process():
    stopped = False
    for pid in _iter_runner_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            stopped = True
        except Exception:
            continue
    # 兜底：清理包装脚本中的 ark_service_ctl.sh / timeout 包装
    try:
        for pid_dir in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                raw = (pid_dir / "cmdline").read_bytes()
            except Exception:
                continue
            if b"ark_service_ctl.sh" in raw:
                try:
                    os.kill(int(pid_dir.name), signal.SIGKILL)
                    stopped = True
                except Exception:
                    pass
    except Exception:
        pass
    return stopped

def load_accounts():
    accs = db.list_accounts()
    active_id = db.get_active_account_id()
    return {"active_account_id": active_id, "accounts": accs}

def save_accounts(data):
    for a in data.get("accounts", []):
        db.save_account(a)
    active_id = data.get("active_account_id")
    if active_id:
        db.set_active_account_id(active_id)

def get_account_infrast_dir(account_id: str) -> pathlib.Path:
    """每个方舟账号一个独立目录，仅保留一个基建排班文件。"""
    aid = (account_id or "unknown").strip()
    d = INFRAST_DIR / aid
    d.mkdir(parents=True, exist_ok=True)
    return d

def list_infrast_plans(account_id: str = None):
    """返回该账号目录下的基建文件列表 (正常至多 1 个)。"""
    if not account_id:
        return []
    d = get_account_infrast_dir(account_id)
    return sorted(p.name for p in d.glob("*.json"))

def parse_infrast_schedule_info(filename, account_id: str = None):
    filename = os.path.basename(filename)
    if not filename.endswith(".json"):
        filename += ".json"
    if not account_id:
        return {"error": "File not found"}
    p = get_account_infrast_dir(account_id) / filename
    if not p.exists():
        return {"error": "File not found"}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        plans = data.get("plans", [])
        schedule_list = []
        for i, plan in enumerate(plans):
            name = plan.get("name", f"班次{i+1}")
            periods = plan.get("period", [])
            period_str = ", ".join([f"{s}~{e}" for s, e in periods]) if periods else "全天"
            trigger_time = periods[0][0] if periods and len(periods[0]) > 0 else "未指定"
            schedule_list.append({
                "index": i,
                "name": name,
                "period": period_str,
                "trigger_time": trigger_time,
                "rooms_count": len(plan.get("rooms", {}))
            })
        return {
            "title": data.get("title", filename),
            "plans_count": len(plans),
            "schedules": schedule_list
        }
    except Exception as e:
        return {"error": str(e)}

def get_today_logs(owner_username=None, limit=250):
    if not LOG_FILE.exists():
        return []
    if owner_username:
        running = get_running_task_info()
        if not running or running.get("owner_username") != owner_username:
            return []
    today_str = datetime.date.today().strftime("[%Y-%m-%d")
    matched = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith(today_str):
                    matched.append(line.strip())
        return matched[-limit:]
    except Exception:
        return []

def get_today_human_logs(limit=80):
    events = []
    running_task = is_task_running()
    if not LOG_FILE.exists():
        return []

    task_meta = {
        "StartUp": {"icon": "🎮", "name": "客户端唤醒与登录", "desc": "启动游戏客户端，验证登录并进入主界面大厅"},
        "Recruit": {"icon": "📋", "name": "公开招募", "desc": "自动刷新词条并根据最优星级策略开槽招募"},
        "Infrast": {"icon": "🏢", "name": "基建排班管理", "desc": "按排班表调配干员换班、无人机全额加速与产物收取"},
        "Fight": {"icon": "⚔️", "name": "关卡作战 (理智清空)", "desc": "关卡作战完成，理智已全部清空结算"},
        "Mall": {"icon": "🛒", "name": "信用商店消费", "desc": "宿舍信用已收取，完成每日优惠物资兑换"},
        "Award": {"icon": "🎁", "name": "日常奖励与邮件", "desc": "完成日常任务见闻与邮件补给一键领取"},
        "Depot": {"icon": "📦", "name": "仓库物资全量识别", "desc": "全量材料与龙门币库存识别完成并同步落盘"},
    }

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []

    sessions = []
    curr_session = []
    for l in lines:
        if "MaaCore Process Start" in l or "StartGameTaskPlugin" in l:
            if curr_session:
                sessions.append(curr_session)
                curr_session = []
        curr_session.append(l)
    if curr_session:
        sessions.append(curr_session)

    # 重点提炼最近的 1~2 轮会话
    for s_idx, session_lines in enumerate(sessions[-2:]):
        s_time = ""
        s_status = {}
        active_tc = None
        recruit_extra = ""
        infrast_extra = ""

        for l in session_lines:
            m_t = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
            t_str = m_t.group(1).split()[1] if m_t else ""
            if not s_time and t_str:
                s_time = t_str

            if "TaskChainStart" in l:
                m = re.search(r"\{.*\}", l)
                if m:
                    try:
                        tc = json.loads(m.group(0)).get("taskchain")
                        if tc in task_meta:
                            active_tc = tc
                            s_status[tc] = {"status": "running", "time": t_str, "extra": ""}
                    except: pass
            elif "TaskChainCompleted" in l:
                m = re.search(r"\{.*\}", l)
                if m:
                    try:
                        tc = json.loads(m.group(0)).get("taskchain")
                        if tc in task_meta:
                            s_status[tc] = {"status": "completed", "time": t_str, "extra": ""}
                            if active_tc == tc:
                                active_tc = None
                    except: pass
            elif "TaskChainError" in l:
                m = re.search(r"\{.*\}", l)
                if m:
                    try:
                        tc = json.loads(m.group(0)).get("taskchain")
                        if tc in task_meta:
                            s_status[tc] = {"status": "error", "time": t_str, "extra": "该任务执行超时或异常中断"}
                            if active_tc == tc:
                                active_tc = None
                    except: pass
            elif "RecruitTagsDetected" in l:
                m = re.search(r"\{.*\}", l)
                if m:
                    try:
                        tags = json.loads(m.group(0)).get("details", {}).get("tags", [])
                        if tags:
                            recruit_extra = f"识别标签: [{', '.join(tags)}]"
                    except: pass
            elif "RecruitConfirm" in l and "SubTaskCompleted" in l:
                recruit_extra = (recruit_extra + " ➔ 锁定干员并确认开槽").strip()
            elif "DroneAssistMfg" in l and "SubTaskCompleted" in l:
                infrast_extra = "无人机加速制造完成，产物已收获"

        is_latest_session = (s_idx == len(sessions[-2:]) - 1)
        round_title = f"日常托管流水线 (启动时间: {s_time})" if s_time else "日常托管流水线"
        all_completed = all(s_status.get(tc, {}).get("status") == "completed" for tc in ["StartUp", "Recruit", "Infrast", "Fight", "Mall", "Award", "Depot"] if tc in s_status)

        if not any(e.get("title") == f"【运行批次】{round_title}" for e in events):
            events.append({
                "time": s_time,
                "level": "info",
                "icon": "🚀",
                "title": f"【运行批次】{round_title}",
                "desc": "全套日常任务链自动装配就绪"
            })

        for tc in ["StartUp", "Recruit", "Infrast", "Fight", "Mall", "Award", "Depot"]:
            if tc in s_status:
                info = s_status[tc]
                st = info["status"]
                t = info["time"]
                meta = task_meta[tc]
                
                if st == "running" and (not is_latest_session or not running_task):
                    st = "completed"

                if st == "completed":
                    desc = meta["desc"]
                    if tc == "Recruit" and recruit_extra:
                        desc = recruit_extra
                    elif tc == "Infrast" and infrast_extra:
                        desc = f"{desc} ({infrast_extra})"
                    events.append({
                        "time": t,
                        "level": "success",
                        "icon": "✔",
                        "title": f"{meta['name']} 顺利完成",
                        "desc": desc
                    })
                elif st == "running":
                    events.append({
                        "time": t,
                        "level": "running",
                        "icon": meta["icon"],
                        "title": f"正在执行: {meta['name']}...",
                        "desc": f"任务执行中: {meta['desc']}"
                    })
                elif st == "error":
                    events.append({
                        "time": t,
                        "level": "error",
                        "icon": "❌",
                        "title": f"{meta['name']} 执行中断",
                        "desc": info.get("extra") or "任务执行超时或异常中断"
                    })

        if all_completed and len(s_status) >= 5:
            last_time = max([s_status[tc]["time"] for tc in s_status if s_status[tc]["time"]])
            events.append({
                "time": last_time,
                "level": "success",
                "icon": "🎉",
                "title": "本轮全套日常任务 100% 顺利完成！",
                "desc": "所有 7 项日常阶段均成功执行，模拟器已安全休眠释放系统资源"
            })

    return events[-limit:]

def get_today_card_logs(owner_username=None):
    cards = []
    if owner_username:
        running = get_running_task_info()
        if not running or running.get("owner_username") != owner_username:
            return cards
    today_str = datetime.date.today().strftime("[%Y-%m-%d")
    drops_dir = DEBUG_DIR / "drops"
    latest_drop_img = None
    if drops_dir.exists():
        today_shots = sorted(drops_dir.glob("*.png"), key=os.path.getmtime, reverse=True)
        if today_shots:
            latest_drop_img = "/debug_img?cat=drops&file=" + urllib.parse.quote(today_shots[0].name)

    if not LOG_FILE.exists():
        return cards

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

        tag_configs = [
            ("StartUp", "startup", "唤醒登入", "var(--claude-clay)", "游戏启动与唤醒完成", "官方服客户端登录成功，已自动穿透更新与公告弹窗进入主大厅。", None),
            ("Recruit", "recruit", "自动公招", "var(--claude-clay)", "公开招募识别与自选完成", "已完成全部公招槽位标签识别并按规则拉满 9 小时，未触发 6 星高资需人工干预的情况。", None),
            ("Infrast", "infrast", "基建换班", "var(--claude-sage)", "基建全套换班与产出收取完成", "已完成制造站、贸易站、发电站、会客室、办公室及中枢的干员轮换；赤金加速与线索已处理完毕。", None),
            ("Fight", "fight", "理智作战", "var(--claude-clay)", "理智作战已完成 (清空归零)", "已采用 Auto 自动连战完成关卡作战，剩余理智已完全清空，结算掉落物资已核验入库。", latest_drop_img),
            ("Mall", "mall", "信用收支", "var(--claude-clay)", "信用商店采购完成", "已完成好友宿舍拜访领取今日信用点，并优先采购赤金、招聘许可与龙门币。", None),
            ("Award", "award", "日常奖励", "var(--claude-sage)", "任务奖励与邮件领取完成", "已一键领取今日日常任务、周常进度点数以及系统邮件福利。", None),
            ("Depot", "depot", "物资同步", "#8b5cf6", "全量仓库物资数据已更新", "已扫描全量仓库，合成玉、至纯源石、龙门币、固源岩最新存量已同步入库。", None)
        ]
        esc_today = re.escape(today_str)
        for task_name, c_type, badge, color, title, desc, img in tag_configs:
            pattern = esc_today + r" (\d\d:\d\d:\d\d)\..*?TaskChainCompleted.*?\"taskchain\":\"" + task_name
            for m in re.finditer(pattern, text):
                cards.append({
                    "type": c_type, "badge": badge, "color": color, "time": m.group(1),
                    "title": title, "desc": desc, "img": img
                })
    except Exception:
        pass

    cards.sort(key=lambda x: x.get("time", ""), reverse=False)
    return cards[:20]

def cleanup_old_logs(days=30):
    cutoff = time.time() - (days * 86400)
    try:
        for p in DEBUG_DIR.glob("asst_*.log"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
        cutoff_img = time.time() - (14 * 86400)
        for sub in ["drops", "interface"]:
            d = DEBUG_DIR / sub
            if d.exists():
                for img in d.glob("*.png"):
                    if img.stat().st_mtime < cutoff_img:
                        img.unlink()
    except Exception:
        pass

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>明日方舟多账号托管 · 用户鉴权中心</title>
    <style>
        /* 1. 默认夜间: Claude 暖石炭官方风格 */
        :root {
            --claude-bg: #181816;
            --claude-surface: #22221f;
            --claude-surface-hover: #2a2a26;
            --claude-surface-inset: #1b1b18;
            --claude-border: rgba(255, 255, 255, 0.08);
            --claude-border-hover: rgba(255, 255, 255, 0.16);
            --claude-border-subtle: rgba(255, 255, 255, 0.05);
            --claude-clay: #d97745;
            --claude-clay-hover: #e08253;
            --claude-text-main: #ede8df;
            --claude-text-muted: #a8a29a;
            --claude-text-dim: #736e65;
            --claude-danger: #c94a44;
            --shadow-card: 0 1px 3px rgba(0,0,0,0.2), 0 8px 32px rgba(0,0,0,0.4);

            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            --font-serif: "Tiempos Headline", "Copernicus", "Charter", "Georgia", "Songti SC", serif;
            --font-mono: ui-monospace, "SF Mono", "Fira Code", monospace;

            --radius-md: 10px;
            --radius-lg: 16px;
            --radius-pill: 9999px;
        }

        /* 2. 白天模式: Google 现代白蓝设计 */
        :root[data-theme="light"], html[data-theme="light"], [data-theme="light"] {
            --claude-bg: #f8fafd !important;
            --claude-surface: #ffffff !important;
            --claude-surface-hover: #f1f3f4 !important;
            --claude-surface-inset: #f1f3f4 !important;
            --claude-border: #dadce0 !important;
            --claude-border-hover: #bdc1c6 !important;
            --claude-border-subtle: #e8eaed !important;
            --claude-clay: #1a73e8 !important;
            --claude-clay-hover: #1557b0 !important;
            --claude-text-main: #202124 !important;
            --claude-text-muted: #5f6368 !important;
            --claude-text-dim: #80868b !important;
            --claude-danger: #d93025 !important;
            --shadow-card: 0 1px 3px rgba(60,64,67,0.3), 0 4px 16px rgba(60,64,67,0.15) !important;
        }

        html, body {
            transition: background-color 0.25s ease, color 0.25s ease;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            background-color: var(--claude-bg);
            color: var(--claude-text-main);
            font-family: var(--font-sans);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px 16px;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            letter-spacing: -0.012em;
        }

        .auth-container {
            width: 100%;
            max-width: 410px;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            padding: 32px 28px;
            box-shadow: var(--shadow-card);
            text-align: center;
            animation: fadeIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .brand-header {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            margin-bottom: 8px;
        }

        .brand-dot {
            width: 10px;
            height: 10px;
            background: var(--claude-clay);
            border-radius: 50%;
            box-shadow: 0 0 12px rgba(217, 119, 69, 0.7);
        }
        [data-theme="light"] .brand-dot {
            box-shadow: 0 0 12px rgba(26, 115, 232, 0.7) !important;
        }

        h1 {
            font-family: var(--font-serif);
            font-size: 21px;
            font-weight: 500;
            color: var(--claude-text-main);
            letter-spacing: -0.02em;
        }
        [data-theme="light"] h1 {
            font-family: var(--font-sans) !important;
            font-weight: 600 !important;
        }

        .subtitle {
            font-size: 13px;
            color: var(--claude-text-muted);
            margin-bottom: 22px;
            line-height: 1.5;
        }

        .toggle-capsule {
            display: flex;
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: var(--radius-pill);
            padding: 3px;
            margin-bottom: 20px;
        }

        .toggle-btn {
            flex: 1;
            padding: 7px 12px;
            border-radius: var(--radius-pill);
            border: none;
            background: transparent;
            color: var(--claude-text-muted);
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .toggle-btn.active {
            background: var(--claude-surface);
            color: var(--claude-clay);
            font-weight: 600;
            box-shadow: 0 1px 4px rgba(0,0,0,0.2);
        }
        [data-theme="light"] .toggle-btn.active {
            background: #c2e7ff !important;
            color: #001d35 !important;
        }

        .form-input {
            width: 100%;
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border);
            color: var(--claude-text-main);
            padding: 10px 14px;
            border-radius: var(--radius-md);
            font-size: 14px;
            outline: none;
            margin-bottom: 12px;
            transition: border-color 0.2s;
        }
        .form-input:focus { border-color: var(--claude-clay); }

        .btn-action {
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            color: var(--claude-text-main);
            padding: 5px 12px;
            border-radius: var(--radius-pill);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        .btn-action:hover {
            border-color: var(--claude-clay);
            color: var(--claude-clay);
        }

        .btn-primary {
            width: 100%;
            background: var(--claude-clay);
            border: 1px solid var(--claude-clay);
            color: #fff;
            padding: 11px 18px;
            border-radius: var(--radius-md);
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 6px;
            transition: background 0.2s;
        }
        .btn-primary:hover { background: var(--claude-clay-hover); }

        .tip-msg {
            font-size: 12px;
            color: var(--claude-danger);
            margin-top: 12px;
            display: none;
            padding: 8px 10px;
            background: rgba(201, 74, 68, 0.1);
            border-radius: var(--radius-md);
            border: 1px solid rgba(201, 74, 68, 0.25);
        }

        .footer-note {
            margin-top: 24px;
            font-size: 11px;
            color: var(--claude-text-dim);
            text-align: center;
        }

        @media (max-width: 480px) {
            .auth-container {
                padding: 24px 20px;
            }
            .form-input {
                font-size: 16px;
            }
        }
    
        /* 管理员面板与时间轴样式 */
        .admin-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            text-align: left;
        }
        .admin-table th {
            background: var(--claude-surface-inset);
            color: var(--claude-text-muted);
            padding: 10px 12px;
            border-bottom: 1px solid var(--claude-border);
            font-weight: 600;
        }
        .admin-table td {
            padding: 12px;
            border-bottom: 1px solid var(--claude-border-subtle);
            color: var(--claude-text-main);
            vertical-align: middle;
        }
        .admin-table tr:hover {
            background: rgba(255, 255, 255, 0.02);
        }
        [data-theme="light"] .admin-table tr:hover {
            background: rgba(0, 0, 0, 0.02);
        }
        .slot-pill {
            display: inline-flex;
            align-items: center;
            padding: 3px 8px;
            border-radius: var(--radius-pill);
            font-size: 11px;
            font-family: var(--font-mono);
            background: rgba(217, 119, 69, 0.15);
            color: var(--claude-clay);
            border: 1px solid rgba(217, 119, 69, 0.3);
            margin: 2px;
        }
        [data-theme="light"] .slot-pill {
            background: rgba(26, 115, 232, 0.12);
            color: #1a73e8;
            border-color: rgba(26, 115, 232, 0.3);
        }

    </style>
</head>
<body>
    <div class="auth-container">
        <div style="display: flex; justify-content: flex-end; margin-bottom: 6px;"><button type="button" class="btn-action" style="padding: 3px 9px; font-size: 11px; border-radius: var(--radius-pill); cursor: pointer;" onclick="toggleTheme()" id="loginThemeBtn">☀️ 白天</button></div>
        <div class="brand-header">
            <span class="brand-dot"></span>
            <h1>明日方舟多账号托管</h1>
        </div>
        <p class="subtitle">请验证用户访问权限</p>

        <div class="toggle-capsule">
            <button type="button" id="tabBtnLogin" class="toggle-btn active" onclick="switchMode('login')">用户登录</button>
            <button type="button" id="tabBtnRegister" class="toggle-btn" onclick="switchMode('register')">新用户注册</button>
        </div>

        <!-- 登录容器 (彻底免除 form 原生 GET 刷新) -->
        <div id="formLogin">
            <input type="text" id="loginUser" class="form-input" placeholder="请输入用户名" autocomplete="username" autofocus style="text-align: center;" />
            <input type="password" id="loginPwd" class="form-input" placeholder="访问密码" autocomplete="current-password" autofocus style="text-align: center;" onkeydown="if(event.key==='Enter') submitLogin();" />
            <div style="display: flex; justify-content: space-between; align-items: center; margin: 6px 4px 12px 4px; font-size: 12px; color: var(--claude-text-muted);">
                <label style="display: flex; align-items: center; gap: 6px; cursor: pointer;">
                    <input type="checkbox" id="rememberMe" checked style="accent-color: var(--claude-clay);" />
                    记住账号与密码
                </label>
                <span style="font-size: 11px; color: var(--claude-text-dim);">支持系统密码钥匙串</span>
            </div>
            <button type="button" class="btn-primary" id="btnSubmitLogin" onclick="submitLogin()">确认登录</button>
            <div id="loginTip" class="tip-msg"></div>
        </div>

        <!-- 注册表单 -->
        <form id="formRegister" onsubmit="event.preventDefault(); submitRegister();" style="display: none;">
            <input type="text" id="regUser" class="form-input" placeholder="新用户名" autocomplete="username" style="text-align: center;" />
            <input type="password" id="regPwd" class="form-input" placeholder="设置访问密码" autocomplete="new-password" style="text-align: center;" />
            <input type="password" id="regInvite" class="form-input" placeholder="专属注册邀请码" style="text-align: center; border-color: var(--claude-clay);" />
            <button type="submit" class="btn-primary" id="btnSubmitRegister">提交注册申请</button>
            <div id="regTip" class="tip-msg"></div>
        </form>

        <div class="footer-note">
            明日方舟多账号自动化托管系统
        </div>
    </div>

    <script>
        function initTheme() {
            const saved = localStorage.getItem('ark_theme') || 'dark';
            document.documentElement.setAttribute('data-theme', saved);
            updateThemeBtns(saved);
        }
        function toggleTheme() {
            const cur = document.documentElement.getAttribute('data-theme') || 'dark';
            const next = (cur === 'dark') ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('ark_theme', next);
            updateThemeBtns(next);
        }
        function updateThemeBtns(t) {
            const lBtn = document.getElementById('loginThemeBtn');
            const label = (t === 'dark') ? '☀️ 白天' : '🌙 夜间';
            if (lBtn) lBtn.innerText = label;
        }
        initTheme();

        function switchMode(m) {
            const btnL = document.getElementById('tabBtnLogin');
            const btnR = document.getElementById('tabBtnRegister');
            const formL = document.getElementById('formLogin');
            const formR = document.getElementById('formRegister');
            const tipL = document.getElementById('loginTip');
            const tipR = document.getElementById('regTip');
            if (tipL) tipL.style.display = 'none';
            if (tipR) tipR.style.display = 'none';

            if (m === 'register') {
                btnL.classList.remove('active');
                btnR.classList.add('active');
                formL.style.display = 'none';
                formR.style.display = 'block';
                const el = document.getElementById('regUser');
                if (el) el.focus();
            } else {
                btnR.classList.remove('active');
                btnL.classList.add('active');
                formR.style.display = 'none';
                formL.style.display = 'block';
                const el = document.getElementById('loginUser');
                if (el) el.focus();
            }
        }

        function loadSavedCredentials() {
            try {
                const u = localStorage.getItem('ark_saved_username') || '';
                const p = localStorage.getItem('ark_saved_password') || '';
                const rem = localStorage.getItem('ark_remember_me') !== 'false';
                const elRem = document.getElementById('rememberMe');
                if (elRem) elRem.checked = rem;
                const elU = document.getElementById('loginUser');
                const elP = document.getElementById('loginPwd');
                if (elU) elU.value = u;
                if (rem && p && elP) elP.value = p;
            } catch (e) {}
        }
        window.addEventListener('DOMContentLoaded', loadSavedCredentials);

        function submitLogin() {
            const uInput = document.getElementById('loginUser');
            const pInput = document.getElementById('loginPwd');
            const tip = document.getElementById('loginTip');
            const btn = document.getElementById('btnSubmitLogin');

            const u = uInput ? uInput.value.trim() : '';
            const p = pInput ? pInput.value.trim() : '';

            if (!u) {
                if (tip) {
                    tip.style.display = 'block';
                    tip.innerText = '请输入用户名';
                }
                if (uInput) uInput.focus();
                return;
            }

            if (!p) {
                if (tip) {
                    tip.style.display = 'block';
                    tip.innerText = '请输入访问密码';
                }
                if (pInput) pInput.focus();
                return;
            }

            if (btn) {
                btn.disabled = true;
                btn.innerText = '正在验证...';
            }
            if (tip) tip.style.display = 'none';

            fetch('/api/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p})
            }).then(r => r.json()).then(data => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerText = '确认登录';
                }
                if (data.success) {
                    try {
                        const remEl = document.getElementById('rememberMe');
                        const rem = remEl ? remEl.checked : true;
                        if (rem) {
                            localStorage.setItem('ark_saved_username', u);
                            localStorage.setItem('ark_saved_password', p);
                            localStorage.setItem('ark_remember_me', 'true');
                        } else {
                            localStorage.removeItem('ark_saved_username');
                            localStorage.removeItem('ark_saved_password');
                            localStorage.setItem('ark_remember_me', 'false');
                        }
                        if (data.token) {
                            localStorage.setItem('ark_session_token', data.token);
                        }
                    } catch (e) {}

                    const targetUrl = '/?token=' + encodeURIComponent(data.token || '') + '&t=' + Date.now();
                    window.location.replace(targetUrl);
                } else {
                    if (tip) {
                        tip.style.display = 'block';
                        tip.innerText = data.error || '用户名或密码错误';
                    }
                }
            }).catch(e => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerText = '确认登录';
                }
                if (tip) {
                    tip.style.display = 'block';
                    tip.innerText = '网络异常: ' + e;
                }
            });
        }

        function submitRegister() {
            const uInput = document.getElementById('regUser');
            const pInput = document.getElementById('regPwd');
            const invInput = document.getElementById('regInvite');
            const tip = document.getElementById('regTip');
            const btn = document.getElementById('btnSubmitRegister');

            const u = uInput ? uInput.value.trim() : '';
            const p = pInput ? pInput.value.trim() : '';
            const invite = invInput ? invInput.value.trim() : '';

            if (!u || !p || !invite) {
                if (tip) {
                    tip.style.display = 'block';
                    tip.innerText = '请完整填写用户名、密码与安全邀请码';
                }
                return;
            }

            if (btn) {
                btn.disabled = true;
                btn.innerText = '正在提交...';
            }
            if (tip) tip.style.display = 'none';

            fetch('/api/register', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: u, password: p, invite_code: invite})
            }).then(r => r.json()).then(data => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerText = '提交注册申请';
                }
                if (data.success) {
                    alert('注册成功！已自动完成鉴权，正在进入控制台...');
                    if (data.token) {
                        localStorage.setItem('ark_session_token', data.token);
                    }
                    window.location.replace('/?token=' + encodeURIComponent(data.token || '') + '&t=' + Date.now());
                } else {
                    if (tip) {
                        tip.style.display = 'block';
                        tip.innerText = data.error || '注册失败';
                    }
                }
            }).catch(e => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerText = '提交注册申请';
                }
                if (tip) {
                    tip.style.display = 'block';
                    tip.innerText = '请求异常: ' + e;
                }
            });
        }

// 自动回登监听器已彻底销毁
    </script>
</body>
</html>
"""

APP_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>明日方舟多账号托管管理控制台</title>
    <script src="/jmuxer.min.js"></script>
        <style>
        /* 1. 默认夜间主题: Anthropic Claude 暖石炭官方风格 */
        .log-mode-switch {
            display: inline-flex;
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-pill);
            padding: 2px;
            margin-left: 10px;
        }
        .log-mode-btn {
            border: none;
            background: transparent;
            color: var(--claude-text-muted);
            font-size: 11px;
            font-weight: 500;
            padding: 2px 8px;
            border-radius: var(--radius-pill);
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .log-mode-btn.active {
            background: var(--claude-surface-hover);
            color: var(--claude-clay);
            font-weight: 600;
            box-shadow: 0 1px 3px rgba(0,0,0,0.15);
        }
        .human-log-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
            padding: 4px;
        }
        .human-log-item {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            padding: 8px 12px;
            background: #ffffff !important;
            border: 1px solid #e1e4e8 !important;
            border-radius: 6px;
            transition: background 0.15s, border-color 0.15s;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
        }
        .human-log-item:hover {
            background: #f6f8fa !important;
        }
        .human-log-item.level-success {
            border-left: 3px solid #2da44e !important;
            background: #fbfdfc !important;
        }
        .human-log-item.level-running {
            border-left: 3px solid #d97745 !important;
            background: #fffcf8 !important;
        }
        .human-log-item.level-info {
            border-left: 3px solid #0969da !important;
            background: #fcfdfe !important;
        }
        .human-log-item.level-error {
            border-left: 3px solid #cf222e !important;
            background: #fff8f8 !important;
        }
        .human-log-time {
            font-size: 11px;
            font-family: var(--font-mono);
            color: #6e7781 !important;
            white-space: nowrap;
            margin-top: 1px;
        }
        .human-log-icon {
            font-size: 14px;
            line-height: 1.2;
        }
        .human-log-content {
            flex: 1;
            min-width: 0;
        }
        .human-log-title {
            font-size: 12px;
            font-weight: 600;
            color: #1f2328 !important;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .human-log-desc {
            font-size: 11px;
            color: #57606a !important;
            margin-top: 2px;
            line-height: 1.4;
            word-break: break-all;
        }

        :root {
            --claude-bg: #181816;
            --claude-surface: #22221f;
            --claude-surface-hover: #2a2a26;
            --claude-surface-inset: #1b1b18;
            --claude-border: rgba(255, 255, 255, 0.08);
            --claude-border-hover: rgba(255, 255, 255, 0.16);
            --claude-border-subtle: rgba(255, 255, 255, 0.05);
            --claude-clay: #d97745;
            --claude-clay-hover: #e08253;
            --claude-clay-subtle: rgba(217, 119, 69, 0.12);
            --claude-text-main: #ede8df;
            --claude-text-muted: #a8a29a;
            --claude-text-dim: #736e65;
            --claude-sage: #588157;
            --claude-sage-subtle: rgba(88, 129, 87, 0.15);
            --claude-danger: #c94a44;
            --claude-danger-subtle: rgba(201, 74, 68, 0.12);
            --shadow-subtle: 0 1px 3px rgba(0, 0, 0, 0.2), 0 4px 12px rgba(0, 0, 0, 0.15);
            --shadow-elevated: 0 8px 24px rgba(0, 0, 0, 0.35);

            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Roboto, sans-serif;
            --font-serif: "Tiempos Headline", "Copernicus", "Charter", "Georgia", "Songti SC", serif;
            --font-mono: ui-monospace, "SF Mono", "Fira Code", "Cascadia Code", "Roboto Mono", Menlo, monospace;

            --radius-sm: 6px;
            --radius-md: 10px;
            --radius-lg: 14px;
            --radius-pill: 9999px;
        }

        /* 2. 白天模式: Google Material 3 / Google Workspace 官方白蓝设计 (后置定义 + 高优先级) */
        :root[data-theme="light"], html[data-theme="light"], [data-theme="light"] {
            --claude-bg: #f8fafd !important;
            --claude-surface: #ffffff !important;
            --claude-surface-hover: #f1f3f4 !important;
            --claude-surface-inset: #f0f4f9 !important;
            --claude-border: #dadce0 !important;
            --claude-border-hover: #bdc1c6 !important;
            --claude-border-subtle: #e8eaed !important;
            --claude-clay: #1a73e8 !important;
            --claude-clay-hover: #1557b0 !important;
            --claude-clay-subtle: rgba(26, 115, 232, 0.1) !important;
            --claude-text-main: #202124 !important;
            --claude-text-muted: #5f6368 !important;
            --claude-text-dim: #80868b !important;
            --claude-sage: #1e8e3e !important;
            --claude-sage-subtle: rgba(30, 142, 62, 0.12) !important;
            --claude-danger: #d93025 !important;
            --claude-danger-subtle: rgba(217, 48, 37, 0.12) !important;
            --shadow-subtle: 0 1px 3px rgba(60,64,67,0.3), 0 2px 6px rgba(60,64,67,0.15) !important;
            --shadow-elevated: 0 4px 16px rgba(60,64,67,0.2) !important;
        }

        /* 模态框样式与开闭状态 */
        #modal-new-account, #modal-rename-account, #modal-img-viewer, #modal-add-maa-task, #modal-notify-config {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(18, 18, 16, 0.85);
            backdrop-filter: blur(8px);
            z-index: 9999;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        #modal-new-account.show, #modal-rename-account.show, #modal-img-viewer.show, #modal-add-maa-task.show, #modal-notify-config.show {
            display: flex !important;
        }

        [data-theme="light"] #modal-new-account,
        [data-theme="light"] #modal-rename-account,
        [data-theme="light"] #modal-img-viewer,
        [data-theme="light"] #modal-add-maa-task,
        [data-theme="light"] #modal-notify-config,
            background: rgba(32, 33, 36, 0.65) !important;
        }

        html, body {
            transition: background-color 0.25s ease, color 0.25s ease;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            background-color: var(--claude-bg);
            color: var(--claude-text-main);
            font-family: var(--font-sans);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 16px;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            text-rendering: optimizeLegibility;
            line-height: 1.5;
            letter-spacing: -0.012em;
        }

        h1, h2, h3, .brand {
            font-family: var(--font-serif);
            letter-spacing: -0.02em;
            font-weight: 500;
        }

        [data-theme="light"] h1,
        [data-theme="light"] h2,
        [data-theme="light"] h3,
        [data-theme="light"] .brand {
            font-family: var(--font-sans); /* Google 风格下标题回归利落无衬线 */
            font-weight: 600;
        }

        .inv-value, .mono, #task-status-badge, .log-terminal, input[type="number"], .badge {
            font-family: var(--font-mono);
            font-variant-numeric: tabular-nums;
        }

        /* 顶部 Header */
        header {
            width: 100%;
            max-width: 1380px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0 16px 0;
            border-bottom: 1px solid var(--claude-border);
            margin-bottom: 16px;
            gap: 12px;
            flex-wrap: wrap;
        }

        .brand {
            font-size: 19px;
            color: var(--claude-text-main);
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .brand-dot {
            width: 9px;
            height: 9px;
            background: var(--claude-clay);
            border-radius: 50%;
            box-shadow: 0 0 10px rgba(217, 119, 69, 0.6);
        }
        [data-theme="light"] .brand-dot {
            box-shadow: 0 0 10px rgba(26, 115, 232, 0.6) !important;
        }

        .account-selector {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--claude-surface);
            padding: 4px 10px;
            border-radius: var(--radius-md);
            border: 1px solid var(--claude-border);
            box-shadow: var(--shadow-subtle);
        }

        /* 全局托管状态徽标 */
        .global-task-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 7px 14px;
            border-radius: var(--radius-pill);
            font-size: 13px;
            font-weight: 700;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            box-shadow: var(--shadow-subtle);
            white-space: nowrap;
            transition: all 0.25s ease;
            cursor: default;
        }
        .global-task-badge .gtb-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #94a3b8;
            flex-shrink: 0;
        }
        .global-task-badge .gtb-elapsed {
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
            opacity: 0.85;
        }
        .global-task-badge .gtb-stop {
            margin-left: 4px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 700;
            border-radius: var(--radius-pill);
            border: 1px solid var(--claude-danger);
            background: rgba(217, 48, 37, 0.12);
            color: var(--claude-danger);
            cursor: pointer;
        }
        .global-task-badge .gtb-stop:hover { background: rgba(217, 48, 37, 0.25); }
        .global-task-badge.state-running {
            background: linear-gradient(135deg, rgba(217, 119, 69, 0.22), rgba(217, 119, 69, 0.10));
            border-color: var(--claude-clay);
            color: var(--claude-clay);
            box-shadow: 0 0 0 1px rgba(217,119,69,0.25), 0 2px 10px rgba(217,119,69,0.25);
        }
        .global-task-badge.state-running .gtb-dot {
            background: var(--claude-clay);
            animation: gtb-pulse 1.1s ease-in-out infinite;
        }
        .global-task-badge.state-idle {
            color: var(--claude-text-muted);
        }
        .global-task-badge.state-idle .gtb-dot {
            background: #64748b;
        }
        @keyframes gtb-pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.3; transform: scale(0.65); }
        }

        .account-selector select {
            background: transparent;
            border: none;
            color: var(--claude-text-main);
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            outline: none;
            padding: 2px 4px;
        }

        .theme-switch-btn {
            background: var(--claude-surface);
            color: var(--claude-text-main);
            border: 1px solid var(--claude-border);
            padding: 6px 14px;
            border-radius: var(--radius-pill);
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
            box-shadow: var(--shadow-subtle);
            white-space: nowrap;
        }
        .theme-switch-btn:hover {
            border-color: var(--claude-clay);
            color: var(--claude-clay);
        }

        /* 胶囊导航 Tabs */
        .tabs {
            display: flex;
            gap: 4px;
            background: var(--claude-surface-inset);
            padding: 4px;
            border-radius: var(--radius-pill);
            border: 1px solid var(--claude-border);
            margin-bottom: 20px;
            max-width: 100%;
            overflow-x: auto;
            scrollbar-width: none;
            -webkit-overflow-scrolling: touch;
        }
        .tabs::-webkit-scrollbar { display: none; }

        .tab-btn {
            padding: 8px 18px;
            border-radius: var(--radius-pill);
            border: none;
            background: transparent;
            color: var(--claude-text-muted);
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .tab-btn:hover {
            color: var(--claude-text-main);
            background: rgba(255, 255, 255, 0.04);
        }
        .tab-btn.active {
            background: var(--claude-surface);
            color: var(--claude-clay);
            box-shadow: 0 1px 4px rgba(0,0,0,0.2);
            font-weight: 600;
        }
        [data-theme="light"] .tab-btn.active {
            background: #c2e7ff !important;
            color: #001d35 !important;
        }

        .tab-content {
            display: none;
            width: 100%;
            max-width: 1380px;
            animation: fadeIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .tab-content.active { display: block; }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* 卡片系统 */
        .card {
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            padding: 20px;
            box-shadow: var(--shadow-subtle);
            margin-bottom: 16px;
        }

        .card-header {
            font-family: var(--font-serif);
            font-size: 16px;
            font-weight: 500;
            color: var(--claude-text-main);
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--claude-border-subtle);
            padding-bottom: 10px;
        }
        [data-theme="light"] .card-header {
            font-family: var(--font-sans) !important;
            font-weight: 600 !important;
        }

        .screen-split-layout {
            display: grid;
            grid-template-columns: 62% 38%;
            gap: 20px;
            align-items: stretch;
            width: 100%;
        }

        .left-stream-col {
            display: flex;
            flex-direction: column;
            width: 100%;
            height: 100%;
        }

        .screen-card {
            position: relative;
            width: 100%;
            flex: 1;
            min-height: 480px;
            aspect-ratio: 16 / 9;
            background: #0d0d0c;
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            overflow: hidden;
            box-shadow: var(--shadow-elevated);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        #screen, #screen-video {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
            cursor: default;
            user-select: none;
            -webkit-user-drag: none;
        }

        .screen-offline-placeholder {
            position: absolute;
            top: 0; left: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 24px;
            background: rgba(18, 18, 16, 0.95);
            width: 100%;
            height: 100%;
            z-index: 5;
        }
        .screen-offline-placeholder h3 {
            font-size: 17px;
            color: var(--claude-clay);
            margin-bottom: 8px;
        }
        .screen-offline-placeholder p {
            font-size: 13px;
            color: var(--claude-text-muted);
            max-width: 440px;
            line-height: 1.6;
        }

        

        .input-group {
            display: flex;
            gap: 6px;
            flex: 1;
            min-width: 220px;
        }

        .input-group input {
            flex: 1;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            color: var(--claude-text-main);
            padding: 8px 12px;
            border-radius: var(--radius-md);
            font-size: 13px;
            outline: none;
            transition: border-color 0.2s;
        }
        .input-group input:focus { border-color: var(--claude-clay); }

        .btn-action {
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            color: var(--claude-text-main);
            padding: 8px 14px;
            border-radius: var(--radius-md);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        .btn-action:hover {
            background: var(--claude-surface-hover);
            border-color: var(--claude-border-hover);
        }

        .btn-primary {
            background: var(--claude-clay);
            border: 1px solid var(--claude-clay);
            color: #fff;
            padding: 9px 18px;
            border-radius: var(--radius-md);
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        .btn-primary:hover { background: var(--claude-clay-hover); }

        .dashboard-panel {
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            padding: 16px;
            display: flex;
            flex-direction: column;
            height: 100%;
            box-shadow: var(--shadow-subtle);
        }

        .dash-control-bar {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: var(--radius-md);
            padding: 12px;
            margin-bottom: 12px;
        }

        .dash-btn-run {
            background: var(--claude-clay);
            color: #fff;
            border: none;
            padding: 7px 14px;
            border-radius: var(--radius-md);
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        .dash-btn-run:hover { background: var(--claude-clay-hover); }

        .dash-btn-stop {
            background: rgba(201, 74, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(201, 74, 68, 0.35);
            padding: 7px 14px;
            border-radius: var(--radius-md);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
        }

        .dash-btn-close-emu {
            background: rgba(255, 255, 255, 0.05);
            color: var(--claude-text-muted);
            border: 1px solid var(--claude-border);
            padding: 7px 14px;
            border-radius: var(--radius-md);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
        }

        .card-log-stream {
            flex: 1;
            min-height: 260px;
            max-height: 380px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 10px;
            padding-right: 4px;
        }
        .card-log-stream::-webkit-scrollbar { width: 5px; }
        .card-log-stream::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }

        .maa-log-card {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-left: 3px solid var(--claude-clay);
            border-radius: var(--radius-md);
            padding: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.15);
        }

        .maa-desktop-panel {
            display: flex;
            flex-direction: column;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            padding: 12px;
            box-shadow: var(--shadow-subtle);
            min-height: 520px;
        }
        .maa-top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--claude-border-subtle);
            margin-bottom: 8px;
        }
        .maa-logo-badge {
            display: inline-block;
            background: var(--claude-clay);
            color: #fff;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 4px;
            letter-spacing: 0.5px;
        }
        .maa-subnav {
            display: flex;
            gap: 4px;
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: 6px;
            padding: 3px;
            margin-bottom: 10px;
        }
        .maa-subnav-btn {
            flex: 1;
            border: none;
            background: transparent;
            color: var(--claude-text-muted);
            font-size: 12px;
            font-weight: 500;
            padding: 6px 4px;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.15s;
            text-align: center;
            white-space: nowrap;
        }
        .maa-subnav-btn:hover {
            color: var(--claude-text-main);
            background: rgba(255, 255, 255, 0.04);
        }
        .maa-subnav-btn.active {
            background: var(--claude-surface);
            color: var(--claude-clay);
            font-weight: 600;
            box-shadow: 0 1px 3px rgba(0,0,0,0.25);
        }
        .maa-subcontent {
            flex: 1;
            overflow-y: auto;
            min-height: 0;
            padding-right: 2px;
        }
        .maa-action-bar {
            display: flex;
            gap: 8px;
            margin-bottom: 10px;
        }
        .maa-task-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding-right: 4px;
        }
        
        .maa-task-action-btn {
            background: transparent;
            border: 1px solid var(--claude-border-subtle);
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 11px;
            color: var(--claude-text-muted);
            cursor: pointer;
            transition: all 0.2s;
        }
        .maa-task-action-btn:hover {
            background: var(--claude-surface-hover);
            color: var(--claude-text-main);
            border-color: var(--claude-border);
        }
        .weekly-days-grid {
            display: flex;
            gap: 4px;
            flex-wrap: wrap;
            margin-top: 4px;
        }
        .weekly-day-chip {
            padding: 2px 6px;
            font-size: 11px;
            border-radius: 4px;
            border: 1px solid var(--claude-border-subtle);
            background: var(--claude-surface);
            color: var(--claude-text-dim);
            cursor: pointer;
            user-select: none;
        }
        .weekly-day-chip.active {
            background: rgba(217, 119, 69, 0.2);
            color: var(--claude-clay);
            border-color: var(--claude-clay);
            font-weight: 600;
        }
        .stage-chip {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            font-size: 11px;
            border-radius: 4px;
            background: rgba(217, 119, 69, 0.15);
            color: var(--claude-clay);
            border: 1px solid var(--claude-clay);
            font-family: var(--font-mono);
        }
        .stage-chip-x {
            cursor: pointer;
            font-size: 10px;
            opacity: 0.7;
        }
        .stage-chip-x:hover { opacity: 1; }
        .stage-select-chip {
            display: inline-block;
            padding: 3px 9px;
            font-size: 11px;
            border-radius: 4px;
            background: var(--claude-surface);
            color: var(--claude-text-main);
            border: 1px solid var(--claude-border-subtle);
            cursor: pointer;
            font-family: var(--font-mono);
            transition: all 0.15s;
        }
        .stage-select-chip:hover {
            background: var(--claude-clay);
            color: #fff;
            border-color: var(--claude-clay);
        }

        .maa-task-card {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border);
            border-radius: 6px;
            overflow: hidden;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .maa-task-card:hover {
            border-color: var(--claude-border-hover);
        }
        .maa-task-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 10px;
            user-select: none;
        }
        .maa-task-left {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .maa-task-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--claude-text-main);
        }
        .maa-task-desc {
            font-size: 11px;
            color: var(--claude-text-dim);
            margin-left: 4px;
        }
        .maa-task-toggle {
            cursor: pointer;
            accent-color: var(--claude-clay);
            width: 16px;
            height: 16px;
        }
        .maa-task-body {
            padding: 10px 12px;
            border-top: 1px solid var(--claude-border-subtle);
            background: rgba(0, 0, 0, 0.15);
            font-size: 12px;
            display: none;
        }
        .maa-task-card.expanded .maa-task-body {
            display: block;
        }
        .maa-task-expand-btn {
            background: transparent;
            border: none;
            color: var(--claude-text-muted);
            cursor: pointer;
            padding: 2px 6px;
            font-size: 11px;
            border-radius: 4px;
        }
        .maa-task-expand-btn:hover {
            color: var(--claude-clay);
            background: rgba(255, 255, 255, 0.05);
        }
        .timer-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: 6px;
            padding: 8px 10px;
            gap: 8px;
        }
        .touch-bar-container {
            margin-top: 8px;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: 8px;
            padding: 8px 12px;
        }

        .form-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid var(--claude-border-subtle);
            font-size: 13px;
        }
        .form-row:last-child { border-bottom: none; }

        .form-input, select {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border);
            color: var(--claude-text-main);
            padding: 8px 12px;
            border-radius: var(--radius-md);
            font-size: 13px;
            outline: none;
            width: 100%;
            margin-bottom: 8px;
            transition: border-color 0.2s;
        }
        .form-input:focus, select:focus { border-color: var(--claude-clay); }

        .badge {
            display: inline-block;
            padding: 2px 7px;
            border-radius: var(--radius-pill);
            font-size: 11px;
            background: rgba(255, 255, 255, 0.06);
            color: var(--claude-text-muted);
            border: 1px solid var(--claude-border-subtle);
        }
        [data-theme="light"] .badge {
            background: #e8eaed !important;
            color: #3c4043 !important;
        }

        /* 投屏三模式切换器 (分段控件) */
        .screen-mode-switch {
            display: inline-flex;
            align-items: stretch;
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-md);
            padding: 2px;
            gap: 2px;
        }
        .screen-mode-btn {
            border: none;
            background: transparent;
            color: var(--claude-text-muted);
            font-size: 12px;
            font-weight: 600;
            padding: 5px 12px;
            border-radius: calc(var(--radius-md) - 2px);
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
            white-space: nowrap;
        }
        .screen-mode-btn:hover {
            color: var(--claude-text-main);
            background: rgba(127, 127, 127, 0.08);
        }
        .screen-mode-btn.active {
            background: rgba(40, 167, 69, 0.18) !important;
            color: #28a745 !important;
            box-shadow: inset 0 0 0 1px rgba(40, 167, 69, 0.45) !important;
        }
        #modeBtnPoll.active {
            background: rgba(64, 132, 214, 0.18) !important;
            color: #4090d6 !important;
            box-shadow: inset 0 0 0 1px rgba(64, 132, 214, 0.45) !important;
        }
        #modeBtnOff.active {
            background: rgba(148, 163, 184, 0.22) !important;
            color: #94a3b8 !important;
            box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.45) !important;
        }

        .inv-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-top: 10px;
        }
        .inv-card {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: var(--radius-md);
            padding: 16px 14px;
            text-align: center;
        }
        .inv-title { font-size: 12px; color: var(--claude-text-muted); margin-bottom: 6px; }
        .inv-value { font-size: 22px; font-weight: 600; color: var(--claude-text-main); }

        .modal-box {
            background: var(--claude-surface);
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-lg);
            width: 100%;
            max-width: 480px;
            padding: 24px;
            max-height: 85vh;
            overflow-y: auto;
            box-shadow: 0 16px 48px rgba(0,0,0,0.6);
        }
        .modal-box h2 {
            font-family: var(--font-serif);
            font-size: 18px;
            color: var(--claude-text-main);
            margin-bottom: 6px;
        }
        .modal-box p {
            font-size: 13px;
            color: var(--claude-text-muted);
            margin-bottom: 16px;
            line-height: 1.5;
        }

        .log-terminal {
            background: #ffffff !important;
            border: 1px solid #d0d7de !important;
            border-radius: var(--radius-md);
            padding: 12px;
            font-size: 11px;
            line-height: 1.55;
            color: #24292f !important;
            min-height: 380px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
            box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.05);
        }

        .account-card-item {
            background: var(--claude-surface-inset);
            border: 1px solid var(--claude-border-subtle);
            border-radius: var(--radius-md);
            padding: 12px 16px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .account-card-item.selected {
            border-color: var(--claude-clay);
            background: rgba(217, 119, 69, 0.05);
        }
        [data-theme="light"] .account-card-item.selected {
            border-color: #1a73e8 !important;
            background: rgba(26, 115, 232, 0.06) !important;
        }

        /* 移动端专属响应式调优 (<= 960px 屏幕) */
        @media (max-width: 960px) {
            body { padding: 10px; }
            
            header {
                flex-direction: column;
                align-items: stretch;
                gap: 12px;
                padding-bottom: 12px;
            }
            .header-right-tools {
                display: flex;
                justify-content: space-between;
                align-items: center;
                width: 100%;
                flex-wrap: wrap;
                gap: 8px;
            }

            .screen-split-layout {
                grid-template-columns: 1fr;
                gap: 16px;
            }

            .tabs {
                border-radius: var(--radius-md);
                padding: 3px;
            }
            .tab-btn {
                padding: 7px 14px;
                font-size: 12px;
            }

            .grid-2 {
                grid-template-columns: 1fr !important;
            }

            .inv-grid {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 8px;
            }
            .inv-value { font-size: 18px; }

            #chips-grid-container {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 8px;
            }

            .modal-box {
                width: 94vw !important;
                max-width: 94vw !important;
                padding: 20px 16px !important;
                border-radius: var(--radius-lg) !important;
            }

            .form-input, select {
                font-size: 15px !important;
            }

            
        }
    </style>
</head>
<body>
    <!-- 新增账号模态框 -->
    <div id="modal-new-account" style="display: none;">
        <div class="modal-box">
            <h2>添加新托管账号</h2>
            <p>配置新小号，实现单客户端无多开直接自动轮换托管</p>
            <label style="font-size: 12px; color: var(--claude-text-muted);">游戏账号标识 (尾号/登录名)</label>
            <input type="text" id="newAccAccount" class="form-input" placeholder="例如 8019（官服填尾号，B服填登录名）" oninput="autoSuggestName(this.value)" />
            <label style="font-size: 12px; color: var(--claude-text-muted);">账号备注名</label>
            <input type="text" id="newAccName" class="form-input" placeholder="例如 官服·尾号1234" />
            <label style="font-size: 12px; color: var(--claude-text-muted);">客户端服务器平台</label>
            <select id="newAccPlatform" class="form-input">
                <option value="Official">官方服</option>
                <option value="Bilibili">Bilibili服</option>
            </select>
            <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border-subtle); border-radius: 6px; padding: 10px; margin-bottom: 12px; font-size: 12px; color: var(--claude-text-muted); line-height: 1.5;">
                🔐 <b>登录与切换说明</b>：<br>
                1. 请在左侧投屏解锁后用<b>触屏手动登录</b>该账号一次（无需填写密码，系统不保存任何游戏账密）；<br>
                2. 之后切换账号由 MAA 通过触屏点击已登录账号列表完成，<b>不读写任何游戏配置文件</b>；<br>
                3. 创建后将自动初始化 MAA 默认配置与每日定时班次 (10:00 / 16:00 / 22:00，每次 40 分钟独占)，可随时调整。
            </div>
            <div style="display: flex; gap: 10px; margin-top: 8px;">
                <button class="btn-primary" onclick="submitNewAccount()">确认创建账号</button>
                <button class="btn-action" style="flex: 1;" onclick="closeNewAccountModal()">取消</button>
            </div>
        </div>
    </div>


    <!-- 添加 MAA 任务模态框 -->
    <div id="modal-add-maa-task" style="display: none;" onclick="closeAddMaaTaskModal()">
        <div class="modal-box" style="max-width: 480px;" onclick="event.stopPropagation()">
            <h2>➕ 添加 MAA 任务</h2>
            <p>选择要加入一键长草队列的任务类型，加入后可自由配置参数与调整执行顺序。</p>
            <div id="maa-add-task-catalog-list" style="display: flex; flex-direction: column; gap: 8px; margin: 12px 0; max-height: 360px; overflow-y: auto;">
                <!-- 动态渲染任务类型目录 -->
            </div>
            <div style="display: flex; gap: 10px; margin-top: 10px;">
                <button class="btn-action" style="flex: 1;" onclick="closeAddMaaTaskModal()">关闭取消</button>
            </div>
        </div>
    </div>

    <!-- 重命名模态框 -->
    <div id="modal-rename-account" style="display: none;">
        <div class="modal-box">
            <h2>修改账号备注名</h2>
            <p>为当前选中的托管账号设置一个易记的名称</p>
            <input type="hidden" id="renameAccId" />
            <label style="font-size: 12px; color: var(--claude-text-muted);">新账号名称</label>
            <input type="text" id="renameAccName" class="form-input" />
            <div style="display: flex; gap: 10px; margin-top: 8px;">
                <button class="btn-primary" onclick="submitRenameAccount()">保存名称</button>
                <button class="btn-action" style="flex: 1;" onclick="closeRenameModal()">取消</button>
            </div>
        </div>
    </div>

    <!-- 账号通知渠道配置模态框 -->
    <div id="modal-notify-config" style="display: none;">
        <div class="modal-box" style="max-width: 520px; max-height: 88vh; overflow-y: auto;">
            <h2>🔔 通知渠道设置</h2>
            <p>为该托管账号独立配置消息推送渠道，配置互相隔离</p>
            <input type="hidden" id="notifyAccId" />
            <div style="font-size: 12px; color: var(--claude-text-muted); margin-bottom: 10px;">
                当前账号：<span id="notifyAccName" style="color: var(--claude-clay); font-weight: 600;">--</span>
            </div>

            <div class="form-row">
                <span>通知方式</span>
                <select id="cfgNotifyChannel" onchange="toggleNotifyUI(this.value)">
                    <option value="none">关闭通知 (不推送)</option>
                    <option value="wxpusher">仅使用 WxPusher 微信推送</option>
                    <option value="email">仅使用 SMTP 邮箱推送</option>
                    <option value="both">同时使用 WxPusher 与 邮箱推送</option>
                </select>
            </div>

            <div id="notify-wx-box" style="margin-top: 8px; padding: 10px; background: var(--claude-surface-inset); border-radius: 8px; border: 1px solid var(--claude-border-subtle); display: none;">
                <div style="font-size: 12px; font-weight: 600; color: var(--claude-text-main); margin-bottom: 4px;">WxPusher 配置</div>
                <label style="font-size: 11px; color: var(--claude-text-dim);">应用 AppToken (支持 SPT_ 极简Token 或 AT_ 标准Token)</label>
                <input type="text" id="cfgWxToken" class="form-input" placeholder="留空使用默认，或填 SPT_xxxx / AT_xxxx" />
                <label style="font-size: 11px; color: var(--claude-text-dim);">目标接收 UID (仅 AT_ 时需要，逗号隔开；SPT_留空)</label>
                <input type="text" id="cfgWxUids" class="form-input" placeholder="例如: UID_a1b2c3d4e5" />
            </div>

            <div id="notify-email-box" style="margin-top: 8px; padding: 10px; background: var(--claude-surface-inset); border-radius: 8px; border: 1px solid var(--claude-border-subtle); display: none;">
                <div style="font-size: 12px; font-weight: 600; color: var(--claude-text-main); margin-bottom: 4px;">SMTP 邮箱配置 (授权码模式)</div>
                <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 6px;">
                    <div>
                        <label style="font-size: 11px; color: var(--claude-text-dim);">SMTP 服务器地址</label>
                        <input type="text" id="cfgEmailHost" class="form-input" placeholder="例如: smtp.qq.com" />
                    </div>
                    <div>
                        <label style="font-size: 11px; color: var(--claude-text-dim);">端口</label>
                        <input type="number" id="cfgEmailPort" class="form-input" value="465" />
                    </div>
                </div>
                <label style="font-size: 11px; color: var(--claude-text-dim);">发件邮箱账号</label>
                <input type="text" id="cfgEmailSender" class="form-input" placeholder="例如: your_qq@qq.com" />
                <label style="font-size: 11px; color: var(--claude-text-dim);">发件邮箱授权码 (16位独立服务授权码)</label>
                <input type="password" id="cfgEmailAuth" class="form-input" placeholder="邮箱服务商生成的授权码" />
                <label style="font-size: 11px; color: var(--claude-text-dim);">收件人邮箱</label>
                <input type="text" id="cfgEmailReceiver" class="form-input" placeholder="接收通知的邮箱地址" />
            </div>

            <div style="display: flex; gap: 10px; margin-top: 14px;">
                <button class="btn-primary" style="flex: 1;" onclick="saveNotifyFromModal()">💾 保存通知设置</button>
                <button class="btn-action" onclick="testNotifyFromModal()">📨 测试发送</button>
                <button class="btn-action" onclick="closeNotifyModal()">取消</button>
            </div>
        </div>
    </div>

    <!-- 截图全屏放大模态框 -->
    <div id="modal-img-viewer" style="display: none;" onclick="closeImgViewer()">
        <img id="viewer-img-src" style="max-width: 90vw; max-height: 85vh; border-radius: 8px; border: 2px solid var(--claude-clay); box-shadow: 0 16px 48px rgba(0,0,0,0.8);" />
    </div>

    <input type="file" id="fileUploadInfrast" accept=".json" style="display: none;" onchange="handleInfrastUpload(event)" />

    <!-- 顶部状态栏 -->
    <header>
        <div class="brand">
            <span class="brand-dot"></span>
            明日方舟多账号托管
        </div>
        <div class="header-right-tools" style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
            <!-- 全局托管状态徽标 (所有页面常驻可见) -->
            <div id="globalTaskBadge" class="global-task-badge" title="全局托管任务状态">
                <span class="gtb-dot"></span>
                <span class="gtb-text">状态检测中…</span>
                <span class="gtb-elapsed" id="gtbElapsed" style="display:none;"></span>
                <button type="button" class="gtb-stop" id="gtbStopBtn" style="display:none;" onclick="triggerTaskStop()">⏹ 停止</button>
            </div>
            <div class="account-selector">
                <span style="color: var(--claude-text-muted); font-size: 12px; font-weight: 500;">🎮 当前方舟账号:</span>
                <select id="headerAccSelect" style="font-weight: 600; cursor: pointer;" onchange="changeActiveAccount(this.value)"></select>
            </div>
            <div style="display: flex; align-items: center; gap: 6px; font-size: 12px; background: var(--claude-surface); padding: 4px 10px; border-radius: var(--radius-md); border: 1px solid var(--claude-border);">
                <span style="color: var(--claude-text-muted);">登录:</span>
                <span id="current-user-tag" style="color: var(--claude-clay); font-weight: 600;">--</span>
                <button class="btn-action" style="padding: 2px 8px; font-size: 11px; border-radius: var(--radius-pill); margin-right: 4px; cursor: pointer;" onclick="toggleTheme()" id="appThemeBtn">☀️ 白天</button><button class="btn-action" style="padding: 2px 7px; font-size: 11px; margin-left: 4px;" onclick="doLogout()">登出</button>
            </div>
        </div>
    </header>

    <!-- 导航切换 (已精简合并策略至 MAA 桌面面板) -->
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('screen-tab')">🎮 实时交互投屏</button>
        <button class="tab-btn" onclick="switchTab('account-tab')">👥 托管账号管理</button>
        <button class="tab-btn" onclick="switchTab('data-tab')">📊 物资与运行看板</button>
        <button class="tab-btn" id="tabBtnAdmin" style="display: none;" onclick="switchTab('admin-tab')">👑 管理员面板</button>
    </div>

    <!-- TAB 1: 实时交互投屏与 MAA 桌面版原生小框面板 (左栏投屏等比 62%，右栏 MAA 38%，底端严格对齐) -->
    <div id="screen-tab" class="tab-content active">
        <div class="screen-split-layout">
            <!-- 左栏: 放大投屏大屏与控制 -->
            <div class="left-stream-col">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                        <div class="screen-mode-switch" id="screenModeSwitch">
                            <button type="button" class="screen-mode-btn active" id="modeBtnStream" onclick="setScreenMode('30fps')" title="硬件编码 H.264 实时直通 (~30 帧/秒)，最低延迟">⚡ 30帧</button>
                            <button type="button" class="screen-mode-btn" id="modeBtnPoll" onclick="setScreenMode('0.2fps')" title="每 5 秒抓取一次静态截图 (0.2 帧/秒)，极低资源占用">📸 0.2帧</button>
                            <button type="button" class="screen-mode-btn" id="modeBtnOff" onclick="setScreenMode('off')" title="完全停止画面传输，零网络带宽与设备算力占用">⏹ 关闭</button>
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 6px; font-size: 12px;">
                        <span id="screen-poll-status" class="badge" style="background: rgba(40,167,69,0.15); color: #28a745; font-weight: bold;">🟢 30 FPS 硬件直通 · 延迟 &lt;30ms</span>
                    </div>
                </div>
                <div class="screen-card" id="container" style="position: relative; overflow: hidden; border-radius: 8px; background: #000; pointer-events: none; user-select: none; cursor: default;">
                    <video id="screen-video" autoplay muted playsinline style="display: block; width: 100%; height: auto; pointer-events: none; user-select: none; object-fit: contain;"></video>
                    <img id="screen" src="/screencap" alt="游戏画面" style="display: none; width: 100%; height: auto; pointer-events: none; user-select: none;" />
                    <div class="screen-offline-placeholder" id="screen-offline" style="display: none;">
                        <h3>⏸ 模拟器休眠待命中</h3>
                        <p>当前模拟器已停止以释放硬件资源。<br>可在右侧点击 <b>▶ 开启模拟器</b> 唤醒，或直接 <b>▶ 启动长草队列</b>（会自动唤醒）。</p>
                    </div>
                    <div class="screen-offline-placeholder" id="screen-privacy" style="display: none; background: rgba(24, 24, 22, 0.95);">
                        <h3 style="color: var(--claude-clay);">🔒 其他账号占用中</h3>
                        <p>当前系统正在执行其他受邀用户的托管任务。<br>为保障用户隐私与账号安全，非本人账号执行期间画面已脱敏屏蔽。</p>
                    </div>
                </div>

                <!-- 触控安全与文字注入工具条 (安全防误触 + 首次手动登录专用) -->
                <div class="touch-bar-container">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span id="touchLockStatusBadge" class="badge" style="background: rgba(100, 116, 139, 0.15); color: #94a3b8; font-weight: bold;">🔒 屏幕触控已安全锁定</span>
                            <button type="button" id="btnToggleTouch" class="btn-action" style="padding: 3px 10px; font-size: 12px; font-weight: 600;" onclick="toggleScreenTouch()">🔓 解锁屏幕触控</button>
                        </div>
                        <div style="font-size: 11px; color: var(--claude-text-dim);">
                            切换账号、离开或 5 分钟无操作将自动安全上锁 · 🔐 首次添加账号请解锁后触屏手动登录一次
                        </div>
                    </div>
                    <!-- ADB 文本注入与虚拟按键栏 -->
                    <div id="adbInputRow" style="display: flex; gap: 6px; align-items: center; margin-top: 8px;">
                        <input type="text" id="adbTextInput" class="form-input" placeholder="输入要发送到模拟器的文字/手机号/密码..." style="flex: 1; margin-bottom: 0; font-size: 12px;" onkeydown="if(event.key==='Enter') sendAdbText();" />
                        <button type="button" class="btn-action" style="padding: 6px 12px; font-size: 12px; white-space: nowrap;" onclick="sendAdbText()">⌨️ 发送文字</button>
                        <button type="button" class="btn-action" style="padding: 6px 10px; font-size: 12px; white-space: nowrap;" onclick="sendAdbKey(66)" title="模拟器回车键">↩️ 回车</button>
                        <button type="button" class="btn-action" style="padding: 6px 10px; font-size: 12px; white-space: nowrap;" onclick="sendAdbKey(4)" title="模拟器返回键">⬅️ 返回</button>
                    </div>
                </div>
            </div>

            <!-- 右栏: MAA 桌面版原生小框面板 (高度 100%，与左栏底端平齐) -->
            <div class="dashboard-panel maa-desktop-panel">
                <!-- MAA 顶部控制条与当前配置 -->
                <div class="maa-top-bar">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="maa-logo-badge">MAA</span>
                        <div style="display: flex; flex-direction: column;">
                            <span style="font-size: 13px; font-weight: 700; color: var(--claude-text-main);">
                                <span id="maaCurrentAccountTitle">当前账号</span> · 任务配置
                            </span>
                            <span id="maaTaskStatusBadge" style="font-size: 11px; color: var(--claude-text-muted);">检查中...</span>
                        </div>
                    </div>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="btn-action" style="padding: 3px 8px; font-size: 11px;" onclick="loadMaaConfigForCurrentAccount()" title="重载最新配置">⟳ 刷新</button>
                        <button type="button" class="btn-primary" style="padding: 3px 10px; font-size: 11px;" onclick="saveMaaConfigForCurrentAccount()" id="btnSaveMaaConfig">💾 保存长草配置</button>
                    </div>
                </div>

                <!-- MAA 4 个子选项卡 -->
                <div class="maa-subnav">
                    <button type="button" class="maa-subnav-btn active" onclick="switchMaaSubTab('maa-tab-queue')" id="subtab-btn-queue">🌾 一键长草</button>
                    <button type="button" class="maa-subnav-btn" onclick="switchMaaSubTab('maa-tab-copilot')" id="subtab-btn-copilot">⚔️ 自动战斗</button>
                    <button type="button" class="maa-subnav-btn" onclick="switchMaaSubTab('maa-tab-timers')" id="subtab-btn-timers">⏱️ 定时执行</button>
                    <button type="button" class="maa-subnav-btn" onclick="switchMaaSubTab('maa-tab-logs')" id="subtab-btn-logs">📜 运行日志</button>
                </div>

                <!-- 子页面 1: 一键长草任务队列 -->
                <div id="maa-tab-queue" class="maa-subcontent active">
                    <div class="maa-action-bar">
                        <button type="button" class="dash-btn-run" id="btnRunDaily" onclick="triggerMaaQueueRun()">▶ 启动长草队列</button>
                        <button type="button" class="btn-action" style="font-size: 11px; padding: 6px 10px; font-weight: 600;" onclick="openAddMaaTaskModal()">➕ 添加任务</button>
                        <button type="button" class="dash-btn-stop" id="btnStopTask" onclick="triggerTaskStop()">⏸ 强制停止</button>
                        <button type="button" class="btn-action" id="btnStartEmu" onclick="triggerStartEmu()" style="color: var(--claude-sage); border-color: var(--claude-sage); font-weight: 600;">▶ 开启模拟器</button>
                        <button type="button" class="dash-btn-close-emu" onclick="triggerCloseEmu()">⏹ 停止模拟器</button>
                    </div>
                    <div id="maa-queue-list" class="maa-task-list">
                        <!-- 9 项任务卡片动态渲染 -->
                    </div>
                </div>

                <!-- 子页面 2: Copilot 自动战斗小工具 (1:1 复刻 MAA 桌面版 自动战斗) -->
                <div id="maa-tab-copilot" class="maa-subcontent" style="display: none;">
                    <!-- 作业导入区 -->
                    <div class="card" style="margin-bottom: 10px; padding: 12px;">
                        <div style="font-size: 13px; font-weight: 600; color: var(--claude-clay); margin-bottom: 8px;">🎯 导入自动战斗作业</div>
                        <div style="display: flex; gap: 8px; margin-bottom: 8px;">
                            <button type="button" class="btn-action" style="flex:1; padding: 8px; font-size: 12px;" onclick="triggerCopilotUpload()">📤 添加本地 JSON 作业</button>
                            <input type="file" id="fileUploadCopilot" accept=".json" multiple style="display: none;" onchange="handleCopilotUpload(event)" />
                        </div>
                        <div style="display: flex; gap: 6px; align-items: center;">
                            <input type="text" id="copilotMysteryCode" class="form-input" placeholder="输入作业站神秘代码 (纯数字 ID)" style="flex:1; margin-bottom:0; font-size:12px;" onkeydown="if(event.key==='Enter') fetchCopilotByCode();" />
                            <button type="button" class="btn-action" style="padding: 7px 12px; font-size: 12px; white-space: nowrap; color: var(--claude-clay); border-color: var(--claude-clay);" onclick="fetchCopilotByCode(this)">☁️ 拉取</button>
                        </div>
                        <div id="copilotJobListBox" style="margin-top: 10px; display: none;">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
                                <span style="font-size: 11px; font-weight: 600; color: var(--claude-text-main);">已导入作业列表</span>
                                <button type="button" class="btn-action" style="font-size: 10px; padding: 2px 6px; color: var(--claude-danger);" onclick="clearCopilotJobs()">🧹 清空</button>
                            </div>
                            <div id="copilotJobList" style="display: flex; flex-direction: column; gap: 5px;"></div>
                        </div>
                        <div id="copilotJobDetails" style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border-subtle); border-radius: 6px; padding: 8px 10px; margin-top: 8px; font-size: 11px; color: var(--claude-text-muted); display: none;">
                            <div id="copilotJobTitle" style="font-weight: bold; color: var(--claude-text-main); margin-bottom: 2px;"></div>
                            <div id="copilotJobDesc"></div>
                        </div>
                    </div>

                    <!-- 编队与作战参数 -->
                    <div class="card" style="padding: 12px; margin-bottom: 10px;">
                        <!-- 两个初始开关 -->
                        <div class="form-row">
                            <div style="display:flex;flex-direction:column;">
                                <span style="font-size: 12px; font-weight: 600;">自动编队</span>
                                <span style="font-size:10px;color:var(--claude-text-dim);">清空当前编队并按作业自动编排干员</span>
                            </div>
                            <input type="checkbox" id="copilotForm" onchange="onCopilotFormChange(this.checked)" />
                        </div>
                        <div class="form-row">
                            <div style="display:flex;flex-direction:column;">
                                <span style="font-size: 12px; font-weight: 600;">多作业模式</span>
                                <span style="font-size:10px;color:var(--claude-text-dim);">连续执行列表中的多个作业 (同一关卡区域)</span>
                            </div>
                            <input type="checkbox" id="copilotUseList" onchange="onCopilotUseListChange(this.checked)" />
                        </div>

                        <!-- 自动编队依赖项 (Form=true 时显示) -->
                        <div id="copilotFormDeps" style="display: none; border-top: 1px dashed var(--claude-border-subtle); padding-top: 8px; margin-top: 6px;">
                            <div class="form-row">
                                <span style="font-size: 12px;">使用指定编队栏位</span>
                                <input type="checkbox" id="copilotUseFormation" onchange="toggleCopilotUseFormation(this.checked)" />
                            </div>
                            <div class="form-row" id="rowCopilotFormationIndex" style="display: none;">
                                <span style="font-size: 12px;">编队栏位 (1-4)</span>
                                <select id="copilotFormationIndex">
                                    <option value="1">第一编队</option>
                                    <option value="2">第二编队</option>
                                    <option value="3">第三编队</option>
                                    <option value="4">第四编队</option>
                                </select>
                            </div>
                            <div class="form-row">
                                <span style="font-size: 12px;">忽视干员练度要求</span>
                                <input type="checkbox" id="copilotIgnoreReq" />
                            </div>
                            <div class="form-row">
                                <span style="font-size: 12px;">使用助战干员</span>
                                <input type="checkbox" id="copilotUseSupport" onchange="toggleCopilotSupport(this.checked)" />
                            </div>
                            <div class="form-row" id="rowCopilotSupport" style="display: none;">
                                <span style="font-size: 12px;">助战模式</span>
                                <select id="copilotSupport">
                                    <option value="1">仅补充必要 (WhenNeeded)</option>
                                    <option value="3">随机加一个, 刷信用点用 (Random)</option>
                                </select>
                            </div>
                            <div class="form-row">
                                <span style="font-size: 12px;">追加信赖干员 (信赖升序填充空位)</span>
                                <input type="checkbox" id="copilotAddTrust" />
                            </div>
                            <div class="form-row">
                                <span style="font-size: 12px;">追加自定干员</span>
                                <input type="checkbox" id="copilotAddUserAdditional" onchange="toggleCopilotUserAdditional(this.checked)" />
                            </div>
                            <div id="copilotUserAdditionalBox" style="display: none; border-top: 1px dashed var(--claude-border-subtle); padding-top: 8px; margin-top: 4px;">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                    <span style="font-size: 11px; font-weight: 600; color: var(--claude-text-main);">自定干员列表</span>
                                    <button type="button" class="btn-action" style="font-size: 10px; padding: 2px 6px;" onclick="addCopilotOperatorRow()">+ 添加干员</button>
                                </div>
                                <div id="copilotUserAdditionalList" style="display: flex; flex-direction: column; gap: 4px;"></div>
                            </div>
                        </div>

                        <!-- 多作业模式依赖项 (UseCopilotList=true 时显示) -->
                        <div id="copilotListDeps" style="display: none; border-top: 1px dashed var(--claude-border-subtle); padding-top: 8px; margin-top: 6px;">
                            <div class="form-row">
                                <span style="font-size: 12px;">理智不足时允许吃理智药</span>
                                <input type="checkbox" id="copilotSanityPotion" />
                            </div>
                            <div style="font-size:10px;color:var(--claude-text-dim);line-height:1.5;">多作业模式下从「关卡所在的地图界面」开始，自动连续执行列表中已勾选的作业。</div>
                        </div>

                        <!-- 单作业循环 (UseCopilotList=false 时显示) -->
                        <div id="copilotLoopDeps">
                            <div class="form-row">
                                <span style="font-size: 12px;">循环战斗次数</span>
                                <input type="number" id="copilotLoopTimes" class="form-input" value="1" min="1" max="99" style="width: 80px; text-align: center;" />
                            </div>
                        </div>
                    </div>

                    <div class="maa-action-bar">
                        <button type="button" class="dash-btn-run" onclick="triggerMaaCopilotRun()">▶ 启动 Copilot 自动战斗</button>
                        <button type="button" class="dash-btn-stop" onclick="triggerTaskStop()">⏸ 强制停止</button>
                    </div>
                </div>

                <!-- 子页面 3: 定时执行与 40 分钟独占时段 -->
                <div id="maa-tab-timers" class="maa-subcontent" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div>
                            <span style="font-size: 13px; font-weight: 600; color: var(--claude-clay);">⏱ 每日自动定时执行</span>
                            <span class="badge" style="font-size: 10px; margin-left: 6px; background: rgba(217, 119, 69, 0.15); color: var(--claude-clay);">40 分钟独占保护</span>
                        </div>
                        <button type="button" class="btn-action" style="padding: 3px 8px; font-size: 11px;" onclick="addNewMaaTimerRow()">+ 添加班次</button>
                    </div>

                    <div style="font-size: 11px; color: var(--claude-text-dim); margin-bottom: 8px; line-height: 1.4;">
                        说明：系统到达设定时间将自动唤醒模拟器并执行该账号的长草任务。每个账号独占 40 分钟，重叠冲突将无法保存。
                    </div>
                    <div class="form-row" style="background: var(--claude-surface-inset); padding: 6px 10px; border-radius: 6px; margin-bottom: 10px;">
                        <span style="font-size: 12px; font-weight: 600;">强制按排班计划唤醒执行 (ForceScheduledStart)</span>
                        <input type="checkbox" id="maaTimerForceStart" checked onchange="if(CURRENT_MAA_TIMERS) CURRENT_MAA_TIMERS.ForceScheduledStart = this.checked;" />
                    </div>

                    <!-- 冲突警示横幅 (动态显隐) -->
                    <div id="maaTimerConflictAlert" class="tip-msg" style="display: none; background: rgba(201, 74, 68, 0.15); border: 1px solid var(--claude-danger); color: #ff8077; padding: 8px 10px; border-radius: 6px; font-size: 12px; margin-bottom: 10px;"></div>

                    <div id="maaTimersList" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;">
                        <!-- 动态渲染定时班次 -->
                    </div>

                    <!-- 24小时全服排班时段可视化条 -->
                    <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border); border-radius: 8px; padding: 10px; margin-bottom: 12px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 11px;">
                            <span style="font-weight: 600; color: var(--claude-text-main);">全服 24 小时排班时段分布</span>
                            <span style="color: var(--claude-text-dim);"><span style="display:inline-block;width:8px;height:8px;background:var(--claude-clay);border-radius:2px;margin-right:3px;"></span>当前账号 <span style="display:inline-block;width:8px;height:8px;background:#64748b;border-radius:2px;margin-left:6px;margin-right:3px;"></span>其他账号</span>
                        </div>
                        <div id="maa-timeline-bar" style="position: relative; height: 26px; background: var(--claude-surface); border: 1px solid var(--claude-border-subtle); border-radius: 4px; overflow: hidden; margin-bottom: 4px;">
                            <div style="position: absolute; top:0; left:0; width:100%; height:100%; display: flex;">
                                <div style="flex:1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                                <div style="flex:1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                                <div style="flex:1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                                <div style="flex:1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                                <div style="flex:1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                                <div style="flex:1;"></div>
                            </div>
                            <div id="maa-timeline-blocks" style="position: absolute; top:0; left:0; width:100%; height:100%;"></div>
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 9px; color: var(--claude-text-dim);">
                            <span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>24:00</span>
                        </div>
                    </div>

                    <div style="display: flex; gap: 8px;">
                        <button type="button" class="btn-primary" style="flex: 1;" onclick="saveMaaTimersForCurrentAccount()">💾 保存定时执行设置</button>
                    </div>
                </div>

                <!-- 子页面 4: 运行日志 -->
                <div id="maa-tab-logs" class="maa-subcontent" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; margin-top: 4px;">
                        <div class="log-mode-switch" id="logModeSwitch">
                            <button type="button" class="log-mode-btn active" id="btnLogHuman" onclick="setLogViewMode('human')">✨ 人类可读</button>
                            <button type="button" class="log-mode-btn" id="btnLogRaw" onclick="setLogViewMode('raw')">🖥️ 原始终端</button>
                        </div>
                        <button type="button" class="btn-action" style="padding: 2px 8px; font-size: 11px;" onclick="loadTodayLogs()">⟳ 刷新日志</button>
                    </div>
                    <div style="margin-top: 14px;">
                        <div class="log-terminal" id="logTerminal" style="height: 420px; overflow-y: auto;">正在读取今日运行日志...</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <!-- TAB 3: 托管账号管理 -->
    <div id="account-tab" class="tab-content">
        <div class="card">
            <div class="card-header">
                <span>已绑定的托管账号列表 (官方服)</span>
                <button class="btn-action" style="font-size: 12px; padding: 4px 12px; background: var(--claude-clay); color: #fff; font-weight: 600; border-color: var(--claude-clay);" onclick="openNewAccountModal()">+ 新增托管账号</button>
            </div>
            <div id="accounts-list-container"></div>
            <p style="font-size: 12px; color: var(--claude-text-muted); margin-top: 10px;">
                
            </p>
        </div>

        <!-- 管理员专属邀请码设置卡片 -->
        <div class="card" id="admin-invite-card" style="margin-top: 16px; display: none;">
            <div class="card-header">
                <div>
                    <span>🔑 系统注册邀请码设置 (管理员专属)</span>
                    <span style="font-size: 11px; color: var(--claude-text-muted); margin-left: 8px;">控制新用户注册准入，可随时修改重设</span>
                </div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; max-width: 480px;">
                <input type="text" id="adminInviteCodeInput" class="form-input" placeholder="输入新的专属邀请码..." style="font-family: monospace; font-size: 14px; font-weight: 600; color: var(--claude-clay); text-align: center;" />
                <button class="btn-primary" style="white-space: nowrap; padding: 8px 16px;" onclick="saveAdminInviteCode()">更新邀请码</button>
            </div>
        </div>
    </div>

    <!-- TAB 4: 物资与运行看板 -->
    <div id="data-tab" class="tab-content">
        <!-- 账号归属与全局同步显示栏 -->
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; background: var(--claude-surface); padding: 10px 14px; border-radius: var(--radius-md); border: 1px solid var(--claude-border);">
            <div style="display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 13px; font-weight: 600; color: var(--claude-text-main);">📊 当前展示方舟账号:</span>
                <span id="data-acc-tag-main" class="badge" style="background: rgba(217, 119, 69, 0.15); color: var(--claude-clay); font-size: 13px; padding: 4px 10px; font-weight: 600;">--</span>
                <span style="font-size: 11px; color: var(--claude-text-dim);">（如需切换查看其他账号，请直接在右上角【方舟账号】下拉切换）</span>
            </div>
            <div id="data-owner-badge" style="font-size: 12px;"></div>
        </div>

        <div class="card">
            <div class="card-header">
                <div>
                    <span>硬通货与基石资源</span>
                    <span id="data-acc-tag" style="font-size: 13px; color: var(--claude-clay); margin-left: 8px;"></span>
                </div>
                <div style="font-size: 12px; color: var(--claude-text-dim);" id="data-last-updated">最后同步: --</div>
            </div>
            <div class="inv-grid" style="grid-template-columns: repeat(4, 1fr);">
                <div class="inv-card">
                    <div class="inv-title">合成玉</div>
                    <div class="inv-value" id="inv-orundum" style="color: var(--claude-clay);">--</div>
                </div>
                <div class="inv-card">
                    <div class="inv-title">至纯源石</div>
                    <div class="inv-value" id="inv-stone" style="color: #e5a93b;">--</div>
                </div>
                <div class="inv-card">
                    <div class="inv-title">龙门币</div>
                    <div class="inv-value" id="inv-lmd" style="color: #81c784;">--</div>
                </div>
                <div class="inv-card">
                    <div class="inv-title">固源岩 (土块)</div>
                    <div class="inv-value" id="inv-rock" style="color: var(--claude-clay);">--</div>
                </div>
            </div>
        </div>

        <!-- 职业芯片与芯片助剂储备看板 -->
        <div class="card" style="margin-top: 16px;">
            <div class="card-header">
                <div>
                    <span>全职业芯片与芯片助剂储备</span>
                    <span style="font-size: 12px; color: var(--claude-text-muted); margin-left: 8px;">仓库全量识别数据</span>
                </div>
                <div style="font-size: 12px; color: var(--claude-sage);">助剂储备: <b id="chip-catalyst-count" style="font-size: 14px; font-family: monospace;">--</b> 瓶</div>
            </div>
            <div id="chips-grid-container" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 8px;"></div>
        </div>
    </div>

    
    <!-- TAB 5: 👑 管理员面板 -->
    <div id="admin-tab" class="tab-content">
        <div class="grid-2">
            <!-- 邀请码与系统管理 -->
            <div class="card">
                <div class="card-header">
                    <span>👑 系统邀请码与注册通道</span>
                    <span id="invite-status-badge" class="badge">检查中...</span>
                </div>
                <p style="font-size: 12px; color: var(--claude-text-muted); margin-bottom: 14px; line-height: 1.5;">
                    管理系统的邀请码准入机制。受邀注册的用户信息与托管排班将永久保留在下方，即使后续切换或清空邀请码，已注册用户与账号也不会受影响。
                </p>
                <div class="form-row">
                    <span>当前生效邀请码</span>
                    <input type="text" id="adminInviteCode" class="form-input" placeholder="留空则关闭新用户注册" style="width: 180px; text-align: center; font-family: monospace; font-size: 14px; font-weight: bold;" />
                </div>
                <div style="display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap;">
                    <button class="btn-primary" style="flex: 2; margin-top: 0;" onclick="saveAdminInviteCode()">💾 保存邀请码设置</button>
                    <button class="btn-action" style="flex: 1;" onclick="generateRandomInviteCode()">🎲 随机生成</button>
                    <button class="btn-action" style="color: var(--claude-danger); border-color: rgba(217,48,37,0.3);" onclick="clearAdminInviteCode()">🚫 清空关闭注册</button>
                </div>
            </div>

            <!-- 系统运行与调度统计 -->
            <div class="card">
                <div class="card-header">
                    <span>⏱ 全服调度资源独占看板</span>
                </div>
                <div id="admin-stats-summary" style="display: flex; flex-direction: column; gap: 10px; font-size: 13px;">
                    <div style="display: flex; justify-content: space-between; padding: 8px 12px; background: var(--claude-surface-inset); border-radius: 8px;">
                        <span>系统受邀注册用户数</span>
                        <strong id="stat-total-users" style="color: var(--claude-clay); font-size: 15px;">--</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; padding: 8px 12px; background: var(--claude-surface-inset); border-radius: 8px;">
                        <span>全服生效托管方舟账号数</span>
                        <strong id="stat-total-accs" style="color: var(--claude-sage); font-size: 15px;">--</strong>
                    </div>
                    <div style="display: flex; justify-content: space-between; padding: 8px 12px; background: var(--claude-surface-inset); border-radius: 8px;">
                        <span>24 小时已占用 40 分钟时段</span>
                        <strong id="stat-total-slots" style="color: var(--claude-text-main); font-size: 15px;">--</strong>
                    </div>
                </div>
            </div>
        </div>

        <!-- 被邀请用户与托管总览表 (切换邀请码后仍完整保留并展示) -->
        <div class="card" style="margin-top: 16px;">
            <div class="card-header">
                <span>👥 受邀用户与托管方舟账号总览</span>
                <button class="btn-action" style="font-size: 11px;" onclick="loadAdminOverview()">🔄 刷新列表</button>
            </div>
            <div id="admin-users-table-container" style="overflow-x: auto; margin-top: 10px;">
                <!-- 动态表格 -->
            </div>
        </div>
    </div>

    <script>

        function initTheme() {
            const saved = localStorage.getItem('ark_theme') || 'dark';
            document.documentElement.setAttribute('data-theme', saved);
            updateThemeBtns(saved);
        }
        function toggleTheme() {
            const cur = document.documentElement.getAttribute('data-theme') || 'dark';
            const next = (cur === 'dark') ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('ark_theme', next);
            updateThemeBtns(next);
        }
        function updateThemeBtns(t) {
            const lBtn = document.getElementById('loginThemeBtn');
            const aBtn = document.getElementById('appThemeBtn');
            const label = (t === 'dark') ? '☀️ 白天' : '🌙 夜间';
            if (lBtn) lBtn.innerText = label;
            if (aBtn) aBtn.innerText = label;
        }
        initTheme();
        setTimeout(() => {
            initScreenInteraction();
            loadMaaConfigForCurrentAccount();
        }, 300);

        let GLOBAL_ACCOUNTS_DATA = { accounts: [], active_account_id: "" };
        
        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        let AVAILABLE_PLANS = [];

                        function loadSavedCredentials() {
            try {
                const u = localStorage.getItem('ark_saved_username');
                const p = localStorage.getItem('ark_saved_password');
                const rem = localStorage.getItem('ark_remember_me') !== 'false';
                const elRem = document.getElementById('rememberMe');
                if (elRem) elRem.checked = rem;
                if (rem && u) {
                    const elU = document.getElementById('loginUser');
                    if (elU && !elU.value) elU.value = u;
                }
                if (rem && p) {
                    const elP = document.getElementById('loginPwd');
                    if (elP && !elP.value) elP.value = p;
                }
            } catch (e) {}
        }
        window.addEventListener('DOMContentLoaded', loadSavedCredentials);

        function togglePwdVisibility(inputId) {
            const input = document.getElementById(inputId);
            if (!input) return;
            input.type = (input.type === 'password') ? 'text' : 'password';
        }

                function deleteAccount(accId, accName) {
            if (!confirm(`确定彻底删除托管账号【${accName}】吗？
删除后该账号的所有排班、策略与定时计划将一并永久移除，此操作不可逆！`)) return;
            fetch('/api/accounts/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account_id: accId})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`账号【${accName}】已成功彻底删除！`);
                    loadAccountsData().then(() => {
                        load24HTimeline();
                        if (typeof loadAdminOverview === 'function') loadAdminOverview();
                    });
                } else {
                    alert('删除失败: ' + (res.error || ''));
                }
            }).catch(e => {
                alert('请求异常: ' + e);
            });
        }

        
        let CURRENT_USER = { authenticated: false, role: 'user', username: '' };

        function initData() {
            // 1. 获取并鉴权当前登录用户，判断超级管理员权限
            fetch('/api/auth_check').then(r => r.json()).then(user => {
                if (user && user.authenticated) {
                    CURRENT_USER = user;
                    const uTag = document.getElementById('current-user-tag');
                    if (uTag) {
                        uTag.innerText = (user.user || user.username) + (user.role === 'admin' ? ' 👑' : '');
                    }
                    if (user.role === 'admin') {
                        const admBtn = document.getElementById('tabBtnAdmin');
                        if (admBtn) admBtn.style.display = 'inline-block';
                    }
                }
            }).catch(e => console.warn('auth_check error:', e));

            // 2. 加载排班表和账号
            loadInfrastPlans();
            loadAccountsData().then(() => {
                load24HTimeline();
            });

            // 3. 启动投屏、日志与任务状态轮询
            loadTaskStatus();
            startTaskStatusPolling();
            loadTodayLogs();
            // 默认 30FPS 直通模式 (若不支持 WebCodecs 会自动降级为 5 秒刷新)
            setScreenMode('30fps');
        }
        window.addEventListener('DOMContentLoaded', initData);


                function doLogout() {
            if (!confirm('确定退出当前登录状态吗？')) return;
            fetch('/api/logout', {method: 'POST'}).then(() => {
                window.location.href = '/';
            }).catch(() => {
                window.location.href = '/';
            });
        }

        
        // --- 账号安全脱敏与显隐控制 ---
        function maskAccountDisplay(accStr) {
            if (!accStr) return "未设置";
            accStr = String(accStr).trim();
            if (accStr.length >= 7) return accStr.slice(0, 3) + "****" + accStr.slice(-4);
            if (accStr.length >= 4) return "****" + accStr.slice(-2);
            return "****";
        }

        function toggleAccountEdit() {
            const disp = document.getElementById("cfgGameAccountDisplay");
            const inp = document.getElementById("cfgGameAccount");
            const btn = document.getElementById("btnToggleAccEdit");
            if (!disp || !inp) return;
            if (inp.style.display === "none") {
                disp.style.display = "none";
                inp.style.display = "inline-block";
                inp.focus();
                if (btn) btn.innerText = "🔒 锁定";
            } else {
                inp.style.display = "none";
                disp.style.display = "inline-block";
                disp.innerText = maskAccountDisplay(inp.value);
                if (btn) btn.innerText = "✏️ 修改";
            }
        }

        // --- 24 小时全天时段甘特分布与互斥渲染 ---
        function load24HTimeline() {
            fetch("/api/scheduler/timeline").then(r => r.json()).then(res => {
                const curAcc = getCurrentAccount();
                render24HTimeline(res.slots || [], curAcc.id);
            }).catch(e => console.warn("load24HTimeline error:", e));
        }

        function render24HTimeline(allSlots, curAccId) {
            const container = document.getElementById("timeline-blocks");
            const details = document.getElementById("timeline-slot-details");
            if (!container || !details) return;

            container.innerHTML = "";
            details.innerHTML = "";

            if (!allSlots || allSlots.length === 0) {
                details.innerHTML = "<span style='color: var(--claude-text-dim);'>全天暂无已排班的定时任务，所有时段均可预约。</span>";
                return;
            }

            let detailHtml = "";
            allSlots.forEach(slot => {
                const isCur = (slot.account_id === curAccId);
                const startMin = slot.start_minutes;
                const winMin = slot.window_minutes || 40;
                const leftPct = (startMin / 1440 * 100).toFixed(2);
                const widthPct = (winMin / 1440 * 100).toFixed(2);

                const blk = document.createElement("div");
                blk.style.position = "absolute";
                blk.style.top = "3px";
                blk.style.bottom = "3px";
                blk.style.left = leftPct + "%";
                blk.style.width = widthPct + "%";
                blk.style.minWidth = "6px";
                blk.style.borderRadius = "3px";
                blk.style.background = isCur ? "var(--claude-clay)" : "#64748b";
                blk.style.boxShadow = isCur ? "0 0 6px rgba(217, 119, 69, 0.6)" : "none";
                blk.style.cursor = "pointer";
                blk.title = `【${slot.account_name}】${slot.range_str} (独占${winMin}分钟)`;
                container.appendChild(blk);

                const tagColor = isCur ? "color: var(--claude-clay); font-weight: 600;" : "color: var(--claude-text-main);";
                detailHtml += `
                    <div style="display: flex; justify-content: space-between; align-items: center; background: var(--claude-surface); padding: 5px 10px; border-radius: 6px; border: 1px solid var(--claude-border-subtle);">
                        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                            <span class="slot-pill" style="${isCur ? "" : "background: rgba(100,116,139,0.15); color: #94a3b8; border-color: rgba(100,116,139,0.3);"}">⏱ ${slot.range_str}</span>
                            <span class="badge" style="font-size: 10px;">${slot.mode_label || 'MAA定时'}</span>
                            <span style="font-size: 11px; ${tagColor}">${escapeHtml(slot.account_name)}</span>
                            ${isCur ? '<span class="badge" style="background: rgba(217,119,69,0.15); color: var(--claude-clay); padding: 1px 5px; font-size: 10px;">当前账号</span>' : ""}
                        </div>
                        <span style="font-size: 10px; color: var(--claude-text-dim);">独占 ${winMin} 分钟，单模拟器重叠冲突禁止保存</span>
                    </div>
                `;
            });
            details.innerHTML = detailHtml;
        }

        // --- 管理员专属面板控制 ---
        function loadAdminOverview() {
            fetch("/api/admin/overview").then(r => {
                if (r.status === 403) return null;
                return r.json();
            }).then(data => {
                if (!data) return;
                const badge = document.getElementById("invite-status-badge");
                const codeInp = document.getElementById("adminInviteCode");
                if (codeInp) codeInp.value = data.invite_code || "";

                if (badge) {
                    if (data.invite_open) {
                        badge.innerText = "🟢 开放注册中";
                        badge.style.background = "rgba(40, 167, 69, 0.15)";
                        badge.style.color = "#28a745";
                        badge.style.border = "1px solid rgba(40, 167, 69, 0.3)";
                    } else {
                        badge.innerText = "🔴 注册通道已关闭";
                        badge.style.background = "rgba(220, 53, 69, 0.15)";
                        badge.style.color = "#dc3545";
                        badge.style.border = "1px solid rgba(220, 53, 69, 0.3)";
                    }
                }

                let totalAccs = 0;
                let totalSlots = 0;
                (data.users || []).forEach(u => {
                    const accs = u.accounts || [];
                    totalAccs += accs.length;
                    accs.forEach(a => {
                        totalSlots += (a.occupied_times || []).length;
                    });
                });
                safeSetText("stat-total-users", (data.users || []).length + " 人");
                safeSetText("stat-total-accs", totalAccs + " 个");
                safeSetText("stat-total-slots", totalSlots + " 个时段 (40分独占)");

                renderAdminUsersTable(data.users || []);
            }).catch(e => console.warn("loadAdminOverview error:", e));
        }

        function adminEditAccountTimers(accId, accName) {
            switchTab('screen-tab');
            changeActiveAccount(accId);
            setTimeout(() => {
                switchMaaSubTab('maa-tab-timers');
            }, 100);
        }

        function renderAdminUsersTable(users) {
            const container = document.getElementById("admin-users-table-container");
            if (!container) return;

            if (users.length === 0) {
                container.innerHTML = "<div style='padding: 16px; color: var(--claude-text-dim);'>暂无受邀注册用户。</div>";
                return;
            }

            let html = `
                <table class="admin-table">
                    <thead>
                        <tr>
                            <th>用户身份</th>
                            <th>注册时间</th>
                            <th>托管方舟账号</th>
                            <th>全天独占时段 (40分钟)</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            users.forEach(u => {
                const isAdmin = (u.role === "admin");
                const roleBadge = isAdmin ? 
                    '<span class="badge" style="background: rgba(217, 119, 69, 0.2); color: var(--claude-clay); border: 1px solid rgba(217, 119, 69, 0.4);">👑 管理员</span>' :
                    '<span class="badge">👤 受邀用户</span>';

                let accsHtml = "";
                let slotsHtml = "";

                if (!u.accounts || u.accounts.length === 0) {
                    accsHtml = "<span style='color: var(--claude-text-dim);'>暂无托管账号</span>";
                    slotsHtml = "<span style='color: var(--claude-text-dim);'>未占用</span>";
                } else {
                    accsHtml = u.accounts.map(a => `
                        <div style="margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; background: var(--claude-surface-inset); padding: 6px 10px; border-radius: 6px;">
                            <div>
                                <b>${escapeHtml(a.name)}</b> 
                                <span style="font-family: monospace; color: var(--claude-text-dim);">(${a.account_name_masked})</span>
                                <div style="font-size: 10px; color: var(--claude-text-muted); margin-top: 2px;">
                                    ⏱ 启用班次: <b>${a.timers_count || (a.occupied_slots||[]).length}</b> 个 (${a.window_minutes || 40}分钟独占)
                                </div>
                            </div>
                            <button type="button" class="btn-action" style="font-size: 11px; padding: 3px 8px; font-weight: 600; color: var(--claude-clay); border-color: var(--claude-clay);" onclick="adminEditAccountTimers('${a.id}', '${escapeHtml(a.name)}')">⏱️ 代管定时</button>
                        </div>
                    `).join("");

                    slotsHtml = u.accounts.map(a => {
                        if (!a.occupied_slots || a.occupied_slots.length === 0) return "<span style='color: var(--claude-text-dim);'>无生效时段</span>";
                        return `<div style="display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px;">
                            ${a.occupied_slots.map(s => `<span class="slot-pill" style="font-size: 11px;">⏱ ${s} (${a.window_minutes || 40}分)</span>`).join("")}
                        </div>`;
                    }).join("");
                }

                const actionHtml = isAdmin ?
                    '<span style="font-size: 11px; color: var(--claude-text-dim);">管理员保护</span>' :
                    `<button class="btn-action" style="color: var(--claude-danger); font-size: 11px; padding: 2px 8px;" onclick="deleteUserByAdmin('${u.username}')">🗑️ 移除</button>`;

                html += `
                    <tr>
                        <td>
                            <div style="font-weight: 600;">${escapeHtml(u.display_name)}</div>
                            <div style="font-size: 11px; color: var(--claude-text-dim); margin-top: 2px;">@${u.username} ${roleBadge}</div>
                        </td>
                        <td style="color: var(--claude-text-muted); font-size: 11px;">${u.created_at}</td>
                        <td>${accsHtml}</td>
                        <td>${slotsHtml}</td>
                        <td>${actionHtml}</td>
                    </tr>
                `;
            });

            html += "</tbody></table>";
            container.innerHTML = html;
        }

        function saveAdminInviteCode() {
            const inp = document.getElementById("adminInviteCode");
            const newCode = inp ? inp.value.trim() : "";
            fetch("/api/admin/invite_code", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ invite_code: newCode })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(newCode ? `邀请码已更新为【${newCode}】（已开放注册）` : "邀请码已清空，系统已关闭新用户注册通道！");
                    loadAdminOverview();
                } else {
                    alert("更新失败: " + (res.error || ""));
                }
            }).catch(e => alert("请求失败: " + e));
        }

        function generateRandomInviteCode() {
            const chars = "abcdefghjkmnpqrstuvwxyz23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
            let res = "";
            for (let i = 0; i < 10; i++) {
                res += chars.charAt(Math.floor(Math.random() * chars.length));
            }
            const inp = document.getElementById("adminInviteCode");
            if (inp) inp.value = res;
        }

        function clearAdminInviteCode() {
            if (confirm("确定要清空邀请码吗？清空后新用户将无法注册，仅已有用户可登录。")) {
                const inp = document.getElementById("adminInviteCode");
                if (inp) inp.value = "";
                saveAdminInviteCode();
            }
        }

        function deleteUserByAdmin(username) {
            if (!confirm(`警告：确定要删除用户【${username}】吗？\n该用户名下的所有托管方舟账号与时段预约也将同步移除！`)) return;
            fetch("/api/admin/user/delete", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ username: username })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(res.message || "已删除该用户！");
                    loadAdminOverview();
                    loadAccountsData();
                    load24HTimeline();
                } else {
                    alert("删除失败: " + (res.message || ""));
                }
            }).catch(e => alert("请求异常: " + e));
        }
    
        function loadAccountsData() {
            return fetch('/api/accounts').then(r => r.json()).then(data => {
                GLOBAL_ACCOUNTS_DATA = data;
                renderHeaderAccounts();
                renderAccountsList();
                renderCurrentAccountDetails();
                loadMaaConfigForCurrentAccount();
                const curAcc = getCurrentAccount();
                if (curAcc && curAcc.id) {
                    renderAccountDataTab(curAcc);
                }
            });
        }

        function renderHeaderAccounts() {
            const sel = document.getElementById('headerAccSelect');
            if (!sel) return;
            sel.innerHTML = '';
            (GLOBAL_ACCOUNTS_DATA.accounts || []).forEach(acc => {
                let label = acc.name;
                if (typeof CURRENT_USER !== 'undefined' && CURRENT_USER.role === 'admin' && acc.owner_username !== CURRENT_USER.username) {
                    label += ` [${acc.owner_username}]`;
                }
                const opt = new Option(label, acc.id);
                if (acc.id === GLOBAL_ACCOUNTS_DATA.active_account_id) opt.selected = true;
                sel.add(opt);
            });
        }

        function getCurrentAccount() {
            const accs = GLOBAL_ACCOUNTS_DATA.accounts || [];
            return accs.find(a => a.id === GLOBAL_ACCOUNTS_DATA.active_account_id) || accs[0] || {};
        }

        function changeActiveAccount(accId) {
            lockTouch();
            fetch('/api/accounts/switch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account_id: accId})
            }).then(r => r.json()).then(res => {
                GLOBAL_ACCOUNTS_DATA.active_account_id = accId;
                renderHeaderAccounts();
                renderAccountsList();
                renderCurrentAccountDetails();
                loadMaaConfigForCurrentAccount(accId);
                loadMaaTimersForCurrentAccount();
                const curAcc = getCurrentAccount();
                if (curAcc && curAcc.id) {
                    renderAccountDataTab(curAcc);
                }
            });
        }

        function safeSetVal(id, val) {
            const el = document.getElementById(id);
            if (el && val !== undefined && val !== null) el.value = val;
        }
        function safeSetText(id, txt) {
            const el = document.getElementById(id);
            if (el) el.innerText = txt;
        }

        function renderCurrentAccountDetails() {
            try {
                const acc = getCurrentAccount();
                if (!acc.id) return;

                const inf = acc.infrast || {};
                const fight = acc.fight || {};
                safeSetVal('cfgInfrastMode', inf.mode || 'custom_plan');
                safeSetVal('cfgDailySingleTime', inf.daily_single_time || '16:00');
                safeSetVal('cfgDronesTarget', inf.drones || 'Money');
                updateDailySingleTimeUI(inf.daily_single_time || '16:00');
                toggleInfrastModeUI(inf.mode || 'custom_plan');
                if (inf.plan_file) {
                    safeSetVal('cfgInfrastPlan', inf.plan_file);
                    loadInfrastPlanInfo(inf.plan_file);
                } else {
                    const curPlanEl = document.getElementById('cfgInfrastPlan');
                    if (curPlanEl) loadInfrastPlanInfo(curPlanEl.value);
                }

                safeSetVal('cfgFightMode', fight.mode || 'daily_depot_maintain');
                safeSetVal('cfgManualStage', fight.manual_stage || 'CW-10');
                const rawAcc = acc.account_name || '';
                safeSetText('cfgGameAccountDisplay', maskAccountDisplay(rawAcc));
                safeSetVal('cfgGameAccount', rawAcc);
                const dispBox = document.getElementById('cfgGameAccountDisplay');
                const inpBox = document.getElementById('cfgGameAccount');
                if (dispBox && inpBox) {
                    dispBox.style.display = 'inline-block';
                    inpBox.style.display = 'none';
                    const btn = document.getElementById('btnToggleAccEdit');
                    if (btn) btn.innerText = '✏️ 修改';
                }
                toggleFightModeUI(fight.mode || 'daily_depot_maintain');
                safeSetVal('cfgExpiringMed', fight.use_expiring_medicine ? 'true' : 'false');
                safeSetVal('cfgAnnihilationMed', fight.annihilation_use_medicine ? 'true' : 'false');
                safeSetVal('cfgOnComplete', acc.on_complete || 'stop_emu');

                // 6星策略与公招日限额
                const recruit = acc.recruit || {};
                safeSetVal('cfgRecruitDailyLimit', recruit.daily_limit || 4);
                safeSetVal('cfg6StarStrategy', recruit.select_6star_strategy || 'notify_user');
                const todayCount = recruit.today_count || 0;
                const dailyLimit = recruit.daily_limit || 4;
                const recruitBadge = document.getElementById('recruit-today-badge');
                if (recruitBadge) {
                    recruitBadge.innerText = `今日已消耗: ${todayCount} / ${dailyLimit} 张`;
                    if (todayCount >= dailyLimit) {
                        recruitBadge.style.background = 'rgba(217, 119, 69, 0.15)';
                        recruitBadge.style.color = 'var(--claude-clay)';
                    } else {
                        recruitBadge.style.background = 'rgba(40, 167, 69, 0.15)';
                        recruitBadge.style.color = '#28a745';
                    }
                }

                // 通信配置
                const notify = acc.notify || {};
                const ch = notify.channel || 'none';
                safeSetVal('cfgNotifyChannel', ch);
                toggleNotifyUI(ch);

                const wx = notify.wxpusher || {};
                safeSetVal('cfgWxToken', wx.app_token || '');
                safeSetVal('cfgWxUids', (wx.uids || []).join(', '));

                const em = notify.email || {};
                safeSetVal('cfgEmailHost', em.smtp_host || 'smtp.qq.com');
                safeSetVal('cfgEmailPort', em.smtp_port || 465);
                safeSetVal('cfgEmailSender', em.sender_email || '');
                safeSetVal('cfgEmailAuth', em.auth_code || '');
                safeSetVal('cfgEmailReceiver', em.receiver_email || '');

                const inv = acc.inventory || {};
                safeSetText('data-acc-tag', acc.name || '');
                safeSetText('data-last-updated', `最后同步: ${inv.last_updated || '未同步'}`);
                safeSetText('inv-orundum', (inv['合成玉'] || 0).toLocaleString());
                safeSetText('inv-stone', (inv['至纯源石'] || 0).toLocaleString());
                safeSetText('inv-lmd', (inv['龙门币'] || 0).toLocaleString());
                safeSetText('inv-rock', (inv['固源岩'] || 0).toLocaleString());
                renderCurrentAccountChips();
                safeSetText('inv-shard', (inv['源石碎片'] || 0).toLocaleString());
            } catch (e) {
                console.warn('renderCurrentAccountDetails caught:', e);
            }
        }

        
        // --- 一天一登 59 分钟独占窗口计算与冲突检测 ---
        function updateDailySingleTimeUI(timeVal) {
            if (!timeVal) timeVal = '16:00';
            const parts = timeVal.split(':');
            const h = parseInt(parts[0], 10);
            const m = parseInt(parts[1], 10);
            const startMin = h * 60 + m;
            const endMin = (startMin + 59) % 1440;
            const endH = Math.floor(endMin / 60);
            const endM = endMin % 60;
            const endStr = String(endH).padStart(2, '0') + ':' + String(endM).padStart(2, '0');
            const rangeStr = `${timeVal} ~ ${endStr}`;

            const prevH = (h - 1 + 24) % 24;
            const prevStr = String(prevH).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ' 止';
            const nextH = (h + 1) % 24;
            const nextStr = String(nextH).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ' 起';

            safeSetText('daily-run-time-display', timeVal);
            safeSetText('daily-exclusive-window-display', rangeStr);
            safeSetText('daily-prev-safe', prevStr);
            safeSetText('daily-next-safe', nextStr);

            // 实时冲突嗅探
            const curAcc = getCurrentAccount();
            const curAccId = curAcc ? curAcc.id : '';
            fetch('/api/scheduler/timeline').then(r => r.json()).then(res => {
                const slots = res.slots || [];
                let conflictOcc = null;
                for (const s of slots) {
                    if (s.account_id === curAccId) continue;
                    const diff = Math.min(Math.abs(startMin - s.start_minutes), 1440 - Math.abs(startMin - s.start_minutes));
                    if (diff < 60) {
                        conflictOcc = s;
                        break;
                    }
                }
                const badge = document.getElementById('daily-conflict-status');
                if (badge) {
                    if (conflictOcc) {
                        badge.innerText = `⚠️ 时段冲突 与 ${conflictOcc.account_name} 重叠`;
                        badge.style.background = 'rgba(217, 48, 37, 0.15)';
                        badge.style.color = '#d93025';
                    } else {
                        badge.innerText = '🟢 时段空闲安全';
                        badge.style.background = 'rgba(40, 167, 69, 0.15)';
                        badge.style.color = '#28a745';
                    }
                }
            }).catch(() => {});
        }

        function toggleNewModalInfrastMode(val) {
            const isCustom = (val === 'custom_plan');
            const areaPlan = document.getElementById('new-modal-area-plan');
            const areaDaily = document.getElementById('new-modal-area-daily');
            if (areaPlan) areaPlan.style.display = isCustom ? 'block' : 'none';
            if (areaDaily) areaDaily.style.display = isCustom ? 'none' : 'block';
        }

        function updateNewModalDailyTime(val) {
            if (!val) val = '16:00';
            const parts = val.split(':');
            const h = parseInt(parts[0], 10);
            const m = parseInt(parts[1], 10);
            const endMin = (h * 60 + m + 59) % 1440;
            const endH = Math.floor(endMin / 60);
            const endM = endMin % 60;
            const endStr = String(endH).padStart(2, '0') + ':' + String(endM).padStart(2, '0');
            safeSetText('new-modal-exclusive-badge', `${val} ~ ${endStr} 独占`);
        }

        function toggleInfrastModeUI(mode) {
            const isCustom = (mode === 'custom_plan');
            document.getElementById('area-custom-plan').style.display = isCustom ? 'block' : 'none';
            document.getElementById('area-auto-rotation').style.display = isCustom ? 'none' : 'block';
        }

        function toggleFightModeUI(val) {
            document.getElementById('rowPrimaryStage').style.display = (val === 'manual_stage') ? 'flex' : 'none';
        }

        function renderAccountsList() {
            const box = document.getElementById('accounts-list-container');
            box.innerHTML = '';
            (GLOBAL_ACCOUNTS_DATA.accounts || []).forEach(acc => {
                const isSelected = acc.id === GLOBAL_ACCOUNTS_DATA.active_account_id;
                const div = document.createElement('div');
                div.className = `account-card-item ${isSelected ? 'selected' : ''}`;
                const inf = acc.infrast || {};
                const modeDisplay = (inf.mode === 'daily_once')
                    ? `<span style="color: var(--claude-clay); font-weight: 600;">一天一登 (${inf.daily_single_time || '15:00'} · 原生轮换)</span>`
                    : `<span style="color: var(--claude-text-main);">自定义排班 (${inf.plan_file || 'default_plan.json'})</span>`;
                div.innerHTML = `
                    <div>
                        <div style="font-weight: 600; font-size: 14px; margin-bottom: 4px; display: flex; align-items: center; gap: 8px;">
                            <span>${escapeHtml(acc.name)}</span>
                            <span class="badge">${acc.platform === 'Bilibili' ? 'B服' : '官服'}</span>
                            <button class="btn-action" style="padding: 2px 8px; font-size: 11px;" onclick="openRenameModal('${acc.id}', '${acc.name}')">✎ 改名</button>
                            <button class="btn-action" style="padding: 2px 8px; font-size: 11px;" onclick="openNotifyModal('${acc.id}')">🔔 通知</button>
                        </div>
                        <div style="font-size: 12px; color: var(--claude-text-muted);">
                            账号标识: <span style="font-family: monospace; color: var(--claude-text-main);">${acc.account_name || '未填'}</span> · 
                            托管模式: ${modeDisplay}
                        </div>
                        <div style="font-size: 12px; color: var(--claude-text-muted); margin-top: 6px; display: flex; align-items: center; gap: 6px;">
                            <span>🎬 完成后动作:</span>
                            <select style="font-size:12px;padding:3px 6px;margin-bottom:0;" onchange="updateAccountOnComplete('${acc.id}', this.value)">
                                <option value="auto" ${(acc.on_complete||'auto')==='auto'?'selected':''}>智能 (1小时内无班次则关模拟器，否则仅关游戏)</option>
                                <option value="close_game" ${acc.on_complete==='close_game'?'selected':''}>仅退出明日方舟 (保留模拟器)</option>
                                <option value="stop_emu" ${acc.on_complete==='stop_emu'?'selected':''}>退出模拟器 Docker (彻底释放资源)</option>
                                <option value="keep_running" ${acc.on_complete==='keep_running'?'selected':''}>保持运行 (不退出)</option>
                            </select>
                        </div>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        ${isSelected ? '<span class="badge" style="background: rgba(217, 119, 69, 0.2); color: var(--claude-clay); border: 1px solid rgba(217, 119, 69, 0.4); font-weight: bold;">⭐ 当前全局账号</span>' : ''}
                        <button class="btn-action" style="padding: 4px 10px; font-size: 12px; color: var(--claude-danger); border-color: rgba(201, 74, 68, 0.35); background: rgba(201, 74, 68, 0.08);" onclick="deleteAccount('${acc.id}', '${acc.name}')">🗑️ 删除此方舟账号</button>
                    </div>
                `;
                box.appendChild(div);
            });
        }

        function updateAccountOnComplete(accId, val) {
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!acc) return;
            acc.on_complete = val;
            fetch('/api/accounts/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: acc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    const names = {auto:'智能退出', close_game:'仅退出游戏', stop_emu:'退出模拟器', keep_running:'保持运行'};
                    console.log('退出策略已更新:', accId, names[val] || val);
                } else {
                    alert('保存失败: ' + (res.error || ''));
                }
            }).catch(e => alert('请求异常: ' + e));
        }

        function autoSuggestName(val) {
            if (val && val.length >= 4) {
                const suffix = val.slice(-4);
                document.getElementById('newAccName').value = `官服·尾号${suffix}`;
            }
        }

        
        
        // --- 模态框显隐控制 ---
        function openNewAccountModal() {
            const modalSel = document.getElementById('newAccInfrast');
            if (modalSel && typeof AVAILABLE_PLANS !== 'undefined') {
                modalSel.innerHTML = '';
                AVAILABLE_PLANS.forEach(p => modalSel.add(new Option(p, p)));
            }
            const m = document.getElementById('modal-new-account');
            if (m) {
                m.classList.add('show');
                m.style.display = 'flex';
            }
        }
        function closeNewAccountModal() {
            const m = document.getElementById('modal-new-account');
            if (m) {
                m.classList.remove('show');
                m.style.display = 'none';
            }
        }

        function openRenameModal(accId, oldName) {
            document.getElementById('renameAccId').value = accId;
            document.getElementById('renameAccName').value = oldName;
            const m = document.getElementById('modal-rename-account');
            if (m) {
                m.classList.add('show');
                m.style.display = 'flex';
            }
        }
        function closeRenameModal() {
            const m = document.getElementById('modal-rename-account');
            if (m) {
                m.classList.remove('show');
                m.style.display = 'none';
            }
        }

        function openImgViewer(url) {
            document.getElementById('viewer-img-src').src = url;
            const m = document.getElementById('modal-img-viewer');
            if (m) {
                m.classList.add('show');
                m.style.display = 'flex';
            }
        }
        function closeImgViewer() {
            const m = document.getElementById('modal-img-viewer');
            if (m) {
                m.classList.remove('show');
                m.style.display = 'none';
            }
        }

        // --- 排班表内置时钟解析展示 ---
        function loadInfrastPlanInfo(filename) {
            if (!filename) return;
            fetch('/api/infrast_plan_info?filename=' + encodeURIComponent(filename)).then(r => r.json()).then(data => {
                if (data.error) return;
                const tag = document.getElementById('plan-title-tag');
                if (tag) tag.innerText = data.title || filename;
                const container = document.getElementById('plan-schedules-container');
                if (!container) return;
                container.innerHTML = '';
                (data.schedules || []).forEach(s => {
                    const div = document.createElement('div');
                    div.style.cssText = 'display: flex; justify-content: space-between; align-items: center; background: var(--claude-surface); padding: 5px 10px; border-radius: 6px; border: 1px solid var(--claude-border-subtle); font-size: 11px;';
                    div.innerHTML = `
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <span class="badge" style="background: rgba(217, 119, 69, 0.15); color: var(--claude-clay);">${escapeHtml(s.name)}</span>
                            <span>时段: ${s.period}</span>
                        </div>
                        <span style="font-family: monospace; color: var(--claude-sage); font-weight: bold;">启动: ${s.trigger_time}</span>
                    `;
                    container.appendChild(div);
                });
            }).catch(e => console.warn('loadInfrastPlanInfo error:', e));
        }

        // --- 全职业芯片与芯片助剂看板渲染 ---
        
        // --- 物资看板多账号切换与审查逻辑 ---
        function switchDataAccount(accId) {
            if (!accId) return;
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!acc) return;
            renderAccountDataTab(acc);
        }

        function renderAccountDataTab(acc) {
            if (!acc) return;
            const inv = acc.inventory || {};
            safeSetText('data-acc-tag', `· ${acc.name}`);
            safeSetText('data-acc-tag-main', acc.name || '默认账号');
            safeSetText('data-last-updated', `最后同步: ${inv.last_updated || '未同步'}`);
            safeSetText('inv-orundum', (inv['合成玉'] || 0).toLocaleString());
            safeSetText('inv-stone', (inv['至纯源石'] || 0).toLocaleString());
            safeSetText('inv-lmd', (inv['龙门币'] || 0).toLocaleString());
            safeSetText('inv-rock', (inv['固源岩'] || 0).toLocaleString());
            safeSetText('inv-shard', (inv['源石碎片'] || 0).toLocaleString());

            renderChipsForAccount(acc);

            const badgeEl = document.getElementById('data-owner-badge');
            if (badgeEl) {
                if (typeof CURRENT_USER !== 'undefined' && CURRENT_USER.role === 'admin') {
                    const isMine = (acc.owner_username === CURRENT_USER.username);
                    badgeEl.innerHTML = `<span class="badge" style="${isMine ? 'background: rgba(217, 119, 69, 0.15); color: var(--claude-clay);' : 'background: rgba(100, 116, 139, 0.15); color: #94a3b8;'}">所有者: ${escapeHtml(acc.owner_username)}${isMine ? ' · 我的账号' : ''}</span>`;
                } else {
                    badgeEl.innerHTML = '';
                }
            }
        }

        function renderChipsForAccount(acc) {
            const chips = (acc && acc.chips) ? acc.chips : {};
            const catalystEl = document.getElementById('chip-catalyst-count');
            if (catalystEl) catalystEl.innerText = chips['芯片助剂'] || chips['32001'] || 0;

            const box = document.getElementById('chips-grid-container');
            if (!box) return;
            box.innerHTML = '';

            const chipIdMap = {
                '先锋': ['3211', '3212', '3213'],
                '近卫': ['3221', '3222', '3223'],
                '重装': ['3231', '3232', '3233'],
                '狙击': ['3241', '3242', '3243'],
                '术师': ['3251', '3252', '3253'],
                '医疗': ['3261', '3262', '3263'],
                '辅助': ['3271', '3272', '3273'],
                '特种': ['3281', '3282', '3283']
            };

            const classes = ['先锋', '近卫', '重装', '狙击', '术师', '医疗', '辅助', '特种'];
            classes.forEach(cls => {
                const ids = chipIdMap[cls] || [];
                const c1 = chips[`${cls}初级`] || chips[`${cls}芯片`] || chips[ids[0]] || 0;
                const c2 = chips[`${cls}芯片组`] || chips[`${cls}组`] || chips[ids[1]] || 0;
                const c3 = chips[`${cls}双芯片`] || chips[`${cls}双`] || chips[ids[2]] || 0;

                const div = document.createElement('div');
                div.style.cssText = 'background: var(--claude-surface); border: 1px solid var(--claude-border); border-radius: 8px; padding: 10px; font-size: 12px;';
                div.innerHTML = `
                    <div style="font-weight: 600; color: var(--claude-clay); margin-bottom: 6px; display: flex; justify-content: space-between;">
                        <span>${cls}</span>
                        <span style="font-size: 10px; color: var(--claude-text-dim);">初 / 组 / 双</span>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-family: var(--font-mono); font-weight: bold;">
                        <span style="color: var(--claude-text-main);">${c1}</span>
                        <span style="color: var(--claude-sage);">${c2}</span>
                        <span style="color: var(--claude-clay);">${c3}</span>
                    </div>
                `;
                box.appendChild(div);
            });
        }

        function renderCurrentAccountChips() {
            renderChipsForAccount(getCurrentAccount());
        }

        // --- 配置保存别名 ---
        function saveCurrentConfig() {
            saveCurrentAccountConfig();
        }

        function submitRenameAccount() {
            const accId = document.getElementById('renameAccId').value;
            const newName = document.getElementById('renameAccName').value.trim();
            if (!newName) return;
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!acc) return;
            acc.name = newName;
            fetch('/api/accounts/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: acc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    closeRenameModal();
                    loadAccountsData();
                }
            });
        }

        function saveCurrentAccountConfig() {
            const acc = getCurrentAccount();
            if (!acc.id) return;

            const getElVal = (id, defVal) => {
                const el = document.getElementById(id);
                return el ? el.value : defVal;
            };

            acc.infrast = acc.infrast || {};
            acc.infrast.mode = getElVal('cfgInfrastMode', 'custom_plan');
            acc.infrast.daily_single_time = getElVal('cfgDailySingleTime', '16:00');
            acc.infrast.plan_file = getElVal('cfgInfrastPlan', 'default_plan.json');
            acc.infrast.drones = getElVal('cfgDronesTarget', 'Money');
            acc.infrast.dorm_trust_enabled = true;
            acc.infrast.dorm_not_stationed_enabled = false;
            acc.infrast.fiammetta_recovery_enabled = true;

            acc.fight = acc.fight || {};
            acc.fight.mode = getElVal('cfgFightMode', 'daily_depot_maintain');
            acc.fight.manual_stage = getElVal('cfgManualStage', 'CW-10').trim() || 'CW-10';
            acc.account_name = getElVal('cfgGameAccount', acc.account_name || '').trim();
            acc.fight.use_expiring_medicine = (getElVal('cfgExpiringMed', 'true') === 'true');
            acc.fight.annihilation_use_medicine = (getElVal('cfgAnnihilationMed', 'true') === 'true');

            acc.on_complete = getElVal('cfgOnComplete', 'stop_emu');

            acc.recruit = acc.recruit || {};
            acc.recruit.daily_limit = parseInt(getElVal('cfgRecruitDailyLimit', '4')) || 4;
            acc.recruit.select_6star_strategy = getElVal('cfg6StarStrategy', 'notify_user');
            acc.recruit.enabled = true;

            acc.notify = acc.notify || {};
            acc.notify.channel = getElVal('cfgNotifyChannel', 'none');
            acc.notify.triggers = acc.notify.triggers || {
                on_6star_recruit: true,
                on_daily_summary: true,
                on_error: true
            };
            acc.notify.wxpusher = {
                app_token: getElVal('cfgWxToken', '').trim(),
                uids: getElVal('cfgWxUids', '').split(',').map(s => s.trim()).filter(Boolean),
                topic_ids: []
            };
            acc.notify.email = {
                smtp_host: getElVal('cfgEmailHost', 'smtp.qq.com').trim(),
                smtp_port: parseInt(getElVal('cfgEmailPort', '465')) || 465,
                use_ssl: true,
                sender_email: getElVal('cfgEmailSender', '').trim(),
                auth_code: getElVal('cfgEmailAuth', '').trim(),
                receiver_email: getElVal('cfgEmailReceiver', '').trim()
            };

            fetch('/api/accounts/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: acc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`账号 [${acc.name}] 策略已成功保存！下次唤醒自动执行此策略。`);
                    loadAccountsData();
                } else {
                    alert('保存失败: ' + (res.error || ''));
                }
            });
        }

        let NEW_MODAL_UPLOAD_TARGET = null;
        function triggerInfrastUpload() { NEW_MODAL_UPLOAD_TARGET = null; document.getElementById('fileUploadInfrast').click(); }
        function triggerInfrastUploadForNewModal() { NEW_MODAL_UPLOAD_TARGET = 'new'; document.getElementById('fileUploadInfrast').click(); }

                function loadInfrastPlans() {
            return fetch('/api/infrast_plans').then(r => r.json()).then(plans => {
                AVAILABLE_PLANS = plans || [];
                const sel = document.getElementById('cfgInfrastPlan');
                const modalSel = document.getElementById('newAccInfrast');
                if (sel) {
                    sel.innerHTML = '';
                    AVAILABLE_PLANS.forEach(p => sel.add(new Option(p, p)));
                }
                if (modalSel) {
                    modalSel.innerHTML = '';
                    AVAILABLE_PLANS.forEach(p => modalSel.add(new Option(p, p)));
                }
            }).catch(e => console.warn('loadInfrastPlans error:', e));
        }

        
        function deleteCurrentInfrastPlan() {
            const sel = document.getElementById('cfgInfrastPlan');
            const fn = sel ? sel.value : '';
            if (!fn) {
                alert('暂无选中的排班文件可删除。');
                return;
            }
            if (!confirm(`确定彻底删除排班文件【${fn}】吗？`)) return;

            fetch('/api/infrast_plans/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ filename: fn })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`排班文件【${fn}】已成功删除！`);
                    loadInfrastPlans().then(() => {
                        const newSel = document.getElementById('cfgInfrastPlan');
                        if (newSel && newSel.value) {
                            loadInfrastPlanInfo(newSel.value);
                        } else {
                            const tag = document.getElementById('plan-title-tag');
                            if (tag) tag.innerText = '暂无排班';
                            const container = document.getElementById('plan-schedules-container');
                            if (container) container.innerHTML = '<span style="color: var(--claude-text-dim);">当前账号无生效排班文件，请导入或切换为一天一登模式。</span>';
                        }
                    });
                } else {
                    alert('删除失败: ' + (res.error || ''));
                }
            }).catch(e => alert('请求异常: ' + e));
        }

        function handleInfrastUpload(e) {
            const file = e.target.files[0];
            if (!file) return;
            const uploadForNewModal = (NEW_MODAL_UPLOAD_TARGET === 'new');
            NEW_MODAL_UPLOAD_TARGET = null;
            const curAcc = uploadForNewModal ? {} : getCurrentAccount();
            const reader = new FileReader();
            reader.onload = function(evt) {
                try {
                    const jsonContent = JSON.parse(evt.target.result);
                    fetch('/api/infrast_plans/upload', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ 
                            filename: file.name, 
                            content: jsonContent,
                            account_id: curAcc.id
                        })
                    }).then(r => r.json()).then(res => {
                        if (res.success) {
                            let msg = `排班表【${file.name}】已成功导入！`;
                            msg += uploadForNewModal ? `\n已自动选入新账号的基建排班预设，请继续完成账号创建。` : `\n已绑定至当前账号！\n多余非使用排班已自动清理，后端仅保留生效文件。`;
                            if (res.conflict_warning) {
                                msg += `\n\n${res.conflict_warning}`;
                            }
                            alert(msg);
                            loadInfrastPlans().then(() => {
                                if (uploadForNewModal) {
                                    const mSel = document.getElementById('newAccInfrast');
                                    if (mSel) mSel.value = file.name;
                                } else {
                                    const sel = document.getElementById('cfgInfrastPlan');
                                    if (sel) sel.value = file.name;
                                    loadInfrastPlanInfo(file.name);
                                    loadAccountsData();
                                    load24HTimeline();
                                }
                            });
                        } else {
                            alert('导入失败: ' + (res.error || ''));
                        }
                    }).catch(err => {
                        alert('网络上传异常: ' + err);
                    });
                } catch(err) {
                    alert('文件非合法 JSON 格式，请检查排班表内容！');
                }
                e.target.value = '';
            };
            reader.readAsText(file);
        }


        
        function toggleNotifyUI(ch) {
            const wxBox = document.getElementById('notify-wx-box');
            const emBox = document.getElementById('notify-email-box');
            if (wxBox) wxBox.style.display = (ch === 'wxpusher' || ch === 'both') ? 'block' : 'none';
            if (emBox) emBox.style.display = (ch === 'email' || ch === 'both') ? 'block' : 'none';
        }

        // --- 账号通知渠道配置模态框 ---
        function openNotifyModal(accId) {
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!acc) return;
            document.getElementById('notifyAccId').value = acc.id;
            safeSetText('notifyAccName', acc.name || '--');

            const notify = acc.notify || {};
            const ch = notify.channel || 'none';
            safeSetVal('cfgNotifyChannel', ch);
            toggleNotifyUI(ch);

            const wx = notify.wxpusher || {};
            safeSetVal('cfgWxToken', wx.app_token || '');
            safeSetVal('cfgWxUids', (wx.uids || []).join(', '));

            const em = notify.email || {};
            safeSetVal('cfgEmailHost', em.smtp_host || 'smtp.qq.com');
            safeSetVal('cfgEmailPort', em.smtp_port || 465);
            safeSetVal('cfgEmailSender', em.sender_email || '');
            safeSetVal('cfgEmailAuth', em.auth_code || '');
            safeSetVal('cfgEmailReceiver', em.receiver_email || '');

            const m = document.getElementById('modal-notify-config');
            if (m) {
                m.classList.add('show');
                m.style.display = 'flex';
            }
        }

        function closeNotifyModal() {
            const m = document.getElementById('modal-notify-config');
            if (m) {
                m.classList.remove('show');
                m.style.display = 'none';
            }
        }

        // 仅收集通知字段，避免动到其它账号配置
        function collectNotifyConfig() {
            const getElVal = (id, defVal) => {
                const el = document.getElementById(id);
                return el ? el.value : defVal;
            };
            const notify = {
                channel: getElVal('cfgNotifyChannel', 'none'),
                triggers: {
                    on_6star_recruit: true,
                    on_daily_summary: true,
                    on_error: true
                },
                wxpusher: {
                    app_token: getElVal('cfgWxToken', '').trim(),
                    uids: getElVal('cfgWxUids', '').split(',').map(s => s.trim()).filter(Boolean),
                    topic_ids: []
                },
                email: {
                    smtp_host: getElVal('cfgEmailHost', 'smtp.qq.com').trim(),
                    smtp_port: parseInt(getElVal('cfgEmailPort', '465')) || 465,
                    use_ssl: true,
                    sender_email: getElVal('cfgEmailSender', '').trim(),
                    auth_code: getElVal('cfgEmailAuth', '').trim(),
                    receiver_email: getElVal('cfgEmailReceiver', '').trim()
                }
            };
            return notify;
        }

        function persistNotifyConfig(accId, notify, onDone) {
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!acc) { alert('账号不存在'); return; }
            acc.notify = notify;
            fetch('/api/accounts/update', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: acc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    if (onDone) onDone();
                } else {
                    alert('保存失败: ' + (res.error || ''));
                }
            }).catch(e => alert('请求异常: ' + e));
        }

        function saveNotifyFromModal() {
            const accId = document.getElementById('notifyAccId').value;
            if (!accId) return;
            persistNotifyConfig(accId, collectNotifyConfig(), () => {
                closeNotifyModal();
                renderCurrentAccountDetails();
            });
        }

        function testNotifyFromModal() {
            const accId = document.getElementById('notifyAccId').value;
            if (!accId) return;
            const notify = collectNotifyConfig();
            if (notify.channel === 'none') {
                alert('请先将通知方式切换为 WxPusher 或 邮箱，再进行测试发送！');
                return;
            }
            const acc = (GLOBAL_ACCOUNTS_DATA.accounts || []).find(a => a.id === accId);
            if (!confirm(`将使用当前界面填写的凭据向账号 ${acc ? acc.name : accId} 发送测试消息，确定吗？`)) return;

            // 先落盘，再用落盘后的配置发测试
            persistNotifyConfig(accId, notify, () => {
                fetch('/api/notify/test', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({account_id: accId})
                }).then(r => r.json()).then(res => {
                    alert('测试推送结果: ' + JSON.stringify(res.results || res));
                }).catch(e => alert('请求异常: ' + e));
            });
        }

        // 兼容旧调用名
        function testNotifyConfig() { testNotifyFromModal(); }

        function submitNewAccount() {
            const name = document.getElementById('newAccName').value.trim();
            const account_name = document.getElementById('newAccAccount').value.trim();
            const platform = document.getElementById('newAccPlatform').value;

            if (!name || !account_name) {
                alert('请填写完整账号备注名和账号标识（尾号/登录名）！');
                return;
            }

            const newAcc = {
                id: 'acc_' + Date.now(),
                name: name,
                account_name: account_name,
                platform: platform || "Official",
                enabled: true,
                owner_username: (typeof CURRENT_USER !== 'undefined' && CURRENT_USER.username) ? CURRENT_USER.username : 'admin',
                schedules: ['10:00', '16:00', '22:00'],
                infrast: { 
                    mode: "custom_plan", 
                    plan_file: "default_plan.json", 
                    drones: "PureGold", 
                    threshold: 50, 
                    dorm_not_stationed_enabled: true
                },
                fight: { mode: "daily_depot_maintain", manual_stage: "1-7", fallback_stage: "1-7", series: 0, use_expiring_medicine: true, medicine_expire_days: 2, use_medicine: false, use_stone: false, annihilation_monday: true, annihilation_use_medicine: true },
                recruit: { enabled: true, times: 3, select_6star_strategy: "notify_user", refresh_level3: true },
                mall: { enabled: true, buy_first: ["赤金", "招聘许可", "龙门币"], blacklist: ["碳", "家具", "加急许可"], reserve_max_credit: true },
                award: { enabled: true, mail: true, free_gacha: true, orundum: true, mining: true, special_access: true },
                on_complete: "auto",
                inventory: { "合成玉": 0, "至纯源石": 0, "龙门币": 0, "固源岩": 0, "源石碎片": 0, "last_updated": "未初始化" }
            };

            fetch('/api/accounts/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: newAcc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`新托管账号 ${name} 创建成功！\n请到左侧投屏解锁后，触屏手动登录该账号一次。`);
                    closeNewAccountModal();
                    loadAccountsData().then(() => {
                        load24HTimeline();
                        if (typeof loadAdminOverview === 'function') loadAdminOverview();
                    });
                } else {
                    alert('创建失败: ' + (res.error || ''));
                }
            }).catch(e => alert('请求异常: ' + e));
        }

        // 看板操作与图文卡片流
        let TASK_RUN_START_TS = null;
        function loadTaskStatus() {
            fetch('/api/task/status').then(r => r.json()).then(st => {
                const badge = document.getElementById('task-status-badge');
                const gbadge = document.getElementById('globalTaskBadge');
                const btnDaily = document.getElementById('btnRunDaily');
                const btnStop = document.getElementById('btnStopTask');
                const stageBtns = document.querySelectorAll('.stage-run-btn');

                const accLabel = (st && st.account_name) ? st.account_name : '';
                const ownerLabel = (st && st.owner_username) ? st.owner_username : '';
                const TASK_CN = {
                    'StartUp': '开始唤醒', 'Recruit': '自动公招', 'Infrast': '基建换班',
                    'Fight': '理智作战', 'Mall': '信用收支', 'Award': '领取奖励',
                    'Depot': '仓库识别', 'OperBox': '干员识别', 'Copilot': '自动战斗',
                    'CloseDown': '关闭游戏'
                };
                const curTask = (st && st.current_task) ? (TASK_CN[st.current_task] || st.current_task) : '';

                if (gbadge) {
                    const txt = gbadge.querySelector('.gtb-text');
                    const elapsedEl = document.getElementById('gtbElapsed');
                    const stopBtnEl = document.getElementById('gtbStopBtn');
                    if (st && st.running) {
                        gbadge.classList.remove('state-idle');
                        gbadge.classList.add('state-running');
                        let label = '⚡ 托管中';
                        if (accLabel) label += `: ${accLabel}`;
                        if (curTask) label += ` · ${curTask}`;
                        if (txt) txt.innerText = label;
                        gbadge.title = `正在执行托管任务` + (accLabel ? ` · 账号: ${accLabel}` : '') + (curTask ? ` · 当前: ${curTask}` : '') + (ownerLabel ? ` · 归属: ${ownerLabel}` : '');
                        // 以服务端 started_at 为准显示已运行时长 (刷新/重连后不归零)
                        if (st.started_at) {
                            TASK_RUN_START_TS = st.started_at * 1000;
                        } else if (TASK_RUN_START_TS === null) {
                            TASK_RUN_START_TS = Date.now();
                        }
                        if (elapsedEl) {
                            const secs = Math.max(0, Math.floor((Date.now() - TASK_RUN_START_TS) / 1000));
                            const hh = String(Math.floor(secs/3600)).padStart(2,'0');
                            const mm = String(Math.floor((secs%3600)/60)).padStart(2,'0');
                            const ss = String(secs%60).padStart(2,'0');
                            elapsedEl.innerText = (Math.floor(secs/3600) > 0) ? `⏱ ${hh}:${mm}:${ss}` : `⏱ ${mm}:${ss}`;
                            elapsedEl.style.display = 'inline';
                        }
                        if (stopBtnEl) stopBtnEl.style.display = 'inline-block';
                    } else {
                        gbadge.classList.remove('state-running');
                        gbadge.classList.add('state-idle');
                        if (txt) txt.innerText = '⏸ 当前空闲';
                        gbadge.title = '当前没有正在运行的托管任务';
                        TASK_RUN_START_TS = null;
                        if (elapsedEl) elapsedEl.style.display = 'none';
                        if (stopBtnEl) stopBtnEl.style.display = 'none';
                    }
                }

                if (st && st.running) {
                    if (badge) badge.innerHTML = `<span style="color: var(--claude-clay); font-weight: 600;">⚡ 正在执行任务...</span>`;
                    if (btnDaily) { btnDaily.disabled = true; btnDaily.style.opacity = '0.5'; }
                    if (btnStop) { btnStop.disabled = false; btnStop.style.opacity = '1'; }
                    stageBtns.forEach(b => { b.disabled = true; b.style.opacity = '0.5'; });
                } else {
                    if (badge) badge.innerHTML = `<span style="color: var(--claude-sage); font-weight: 600;">⏸ 空闲待命</span>`;
                    if (btnDaily) { btnDaily.disabled = false; btnDaily.style.opacity = '1'; }
                    if (btnStop) { btnStop.disabled = true; btnStop.style.opacity = '0.5'; }
                    stageBtns.forEach(b => { b.disabled = false; b.style.opacity = '1'; });
                }
            }).catch(() => {});
        }

        let GLOBAL_STATUS_TIMER = null;
        let GLOBAL_ELAPSED_TIMER = null;
        function startTaskStatusPolling() {
            if (GLOBAL_STATUS_TIMER) return;
            GLOBAL_STATUS_TIMER = setInterval(loadTaskStatus, 5000);
            // 秒级刷新已运行时长
            GLOBAL_ELAPSED_TIMER = setInterval(() => {
                const elapsedEl = document.getElementById('gtbElapsed');
                if (elapsedEl && elapsedEl.style.display !== 'none' && TASK_RUN_START_TS !== null) {
                    const secs = Math.floor((Date.now() - TASK_RUN_START_TS) / 1000);
                    const mm = String(Math.floor(secs/60)).padStart(2,'0');
                    const ss = String(secs%60).padStart(2,'0');
                    elapsedEl.innerText = `⏱ ${mm}:${ss}`;
                }
            }, 1000);
        }

        // --- 今日运行终端日志渲染 ---
        let logViewMode = localStorage.getItem('ark_log_view_mode') || 'human';

        function setLogViewMode(mode) {
            logViewMode = (mode === 'raw') ? 'raw' : 'human';
            localStorage.setItem('ark_log_view_mode', logViewMode);
            const btnH = document.getElementById('btnLogHuman');
            const btnR = document.getElementById('btnLogRaw');
            if (btnH && btnR) {
                if (logViewMode === 'human') {
                    btnH.classList.add('active');
                    btnR.classList.remove('active');
                } else {
                    btnR.classList.add('active');
                    btnH.classList.remove('active');
                }
            }
            loadTodayLogs();
        }

        // --- 今日运行终端日志渲染 (支持人类可读翻译层与原始终端视图) ---
        function loadTodayLogs() {
            const term = document.getElementById('logTerminal');
            if (!term) return;

            // 保持按钮激活状态同步
            const btnH = document.getElementById('btnLogHuman');
            const btnR = document.getElementById('btnLogRaw');
            if (btnH && btnR) {
                if (logViewMode === 'human') {
                    btnH.classList.add('active');
                    btnR.classList.remove('active');
                } else {
                    btnR.classList.add('active');
                    btnH.classList.remove('active');
                }
            }

            fetch('/api/log_today').then(r => r.json()).then(data => {
                if (logViewMode === 'human') {
                    const hlogs = (data && data.human_logs) ? data.human_logs : [];
                    if (!hlogs.length) {
                        term.innerHTML = '<span style="color: var(--claude-text-dim); padding: 12px; display: block;">今日暂无任务运行记录。任务开始执行后，系统将自动提炼关键事件流在此呈现。</span>';
                        return;
                    }
                    const html = hlogs.map(item => {
                        const lvl = item.level || 'info';
                        const icon = item.icon || 'ℹ️';
                        const t = item.time ? `[${escapeHtml(item.time)}]` : '';
                        const title = escapeHtml(item.title || '');
                        const desc = escapeHtml(item.desc || '');
                        return `
                        <div class="human-log-item level-${lvl}">
                            <div class="human-log-time">${t}</div>
                            <div class="human-log-icon">${icon}</div>
                            <div class="human-log-content">
                                <div class="human-log-title">${title}</div>
                                ${desc ? `<div class="human-log-desc">${desc}</div>` : ''}
                            </div>
                        </div>`;
                    }).join('');
                    term.innerHTML = `<div class="human-log-list">${html}</div>`;
                    term.scrollTop = term.scrollHeight;
                } else {
                    const logs = (data && data.logs) ? data.logs : [];
                    if (!logs.length) {
                        term.innerHTML = '<span style="color: var(--claude-text-dim);">今日暂无运行日志。任务开始或切换账号后将自动刷新。</span>';
                        return;
                    }
                    term.innerHTML = logs.map(l => {
                        let color = '#24292f'; // 默认白底深灰文字，清晰舒适
                        if (/\\b(?:WARN|WARNING)\\b|\\[WARN\\]/i.test(l)) color = '#b06000'; // 精准黄色/琥珀告警
                        else if (/\\b(?:ERR|ERROR|FATAL)\\b|\\[ERROR\\]|失败|异常/i.test(l)) color = '#cf222e'; // 红色报错
                        else if (/TaskChainCompleted|AllTasksCompleted|Success/i.test(l)) color = '#0969da'; // 成功深蓝
                        else if (/[✔+]/i.test(l)) color = '#1a7f37'; // 成功绿色
                        return `<span style="color:${color};">${escapeHtml(l)}</span>`;
                    }).join(String.fromCharCode(10));
                    term.scrollTop = term.scrollHeight;
                }
            }).catch(e => console.warn('loadTodayLogs error:', e));
        }

        function switchTab(tabId) {
            lockTouch();
            document.querySelectorAll('.tab-content').forEach(tc => {
                if (tc.id === tabId) { tc.classList.add('active'); } else { tc.classList.remove('active'); }
            });
            document.querySelectorAll('.tab-btn').forEach(btn => {
                const oc = btn.getAttribute('onclick') || '';
                if (oc.indexOf("'" + tabId + "'") !== -1) { btn.classList.add('active'); } else { btn.classList.remove('active'); }
            });

            if (tabId === 'screen-tab') {
                if (screenMode === '30fps') {
                    if (!document.hidden) initStreamPlayer();
                } else if (screenMode === '0.2fps') {
                    if (!document.hidden) { refreshScreen(true); startScreenPolling(); }
                }
            } else {
                stopScreenPolling();
                stopStreamPlayer();
            }

            if (tabId === 'data-tab') {
                const curAcc = getCurrentAccount();
                if (curAcc && curAcc.id) {
                    const sel = document.getElementById('dataAccountSelect');
                    if (sel) sel.value = curAcc.id;
                    renderAccountDataTab(curAcc);
                }
            }
            if (tabId === 'account-tab') {
                loadAccountsData();
                load24HTimeline();
            }
            if (tabId === 'admin-tab') {
                loadAdminOverview();
            }
        }

        function triggerStageRun(stage, stageName) {
            const acc = getCurrentAccount();
            if (!acc.id) {
                alert('未检测到可用托管账号，请在账号管理中选择或创建账号');
                return;
            }
            const isDaily = (stage === 'daily');
            const tip = isDaily 
                ? `确定为账号 ${acc.name} 立即运行【全套日常流水线】吗？`
                : `确定为账号 ${acc.name} 单独执行【${stageName}】吗？\n执行完成后模拟器将保持运行，便于在左侧大屏直接核对结果。`;
            if (!confirm(tip)) return;
            fetch('/api/task/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account_id: acc.id, stage: stage})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    loadTaskStatus();
                    
                    loadTodayLogs();
                    refreshScreen(true);
                } else {
                    alert('启动失败: ' + (res.error || ''));
                }
            }).catch(e => {
                alert('网络请求异常: ' + e);
            });
        }

        function triggerTaskRun() {
            triggerStageRun('daily', '全套日常');
        }

        function triggerTaskStop() {
            if (!confirm('确定强制停止当前正在运行的任务吗？')) return;
            fetch('/api/task/stop', {method: 'POST'}).then(r => r.json()).then(() => {
                loadTaskStatus();
                
                loadTodayLogs();
            }).catch(e => {
                alert('请求异常: ' + e);
            });
        }

        function triggerStartEmu() {
            if (!confirm('确定开启模拟器容器吗？(约需 30-90 秒就绪)')) return;
            const btn = document.getElementById('btnStartEmu');
            if (btn) { btn.disabled = true; btn.innerText = '⏳ 启动中...'; }
            fetch('/api/emulator/start', {method: 'POST'}).then(r => r.json()).then(res => {
                if (btn) { btn.disabled = false; btn.innerText = '▶ 开启模拟器'; }
                if (res.success) {
                    alert('✔ ' + (res.message || '模拟器已启动并就绪'));
                    loadTaskStatus();
                    if (screenMode === '30fps') initStreamPlayer();
                    else if (screenMode === '0.2fps') refreshScreen(true);
                } else {
                    alert('开启失败: ' + (res.message || res.error || ''));
                }
            }).catch(e => {
                if (btn) { btn.disabled = false; btn.innerText = '▶ 开启模拟器'; }
                alert('请求异常: ' + e);
            });
        }

        function triggerCloseEmu() {
            if (!confirm('确定停止模拟器容器并释放 4GB+ 内存与 GPU 资源吗？')) return;
            fetch('/api/emulator/stop', {method: 'POST'}).then(r => r.json()).then(res => {
                if (res.success) {
                    alert('模拟器已停止，硬件资源已完全释放。');
                    refreshScreen(true);
                    loadTaskStatus();
                } else {
                    alert('停止失败: ' + (res.message || res.error || ''));
                }
            }).catch(e => {
                alert('请求异常: ' + e);
            });
        }

                // 投屏与触控管理: 独立开关、5 秒轮询、页面失焦或切走 Tab 彻底关停
        const img = document.getElementById('screen');
        const container = document.getElementById('container');
        const offlinePlaceholder = document.getElementById('screen-offline');
        const privacyPlaceholder = document.getElementById('screen-privacy');
        // ==================== 投屏三模式统一管理 (仅显示，纯展示无触控) ====================
        //  '30fps'  : 30 帧/秒 H.264 硬件直通 (WebCodecs，最低延迟，画质流畅)
        //  '0.2fps' : 0.2 帧/秒 (每 5 秒抓取一次静态截图，极低资源占用)
        //  'off'    : 完全关闭画面传输 (零网络带宽 / 零设备算力占用)
        let CURRENT_SESSION_TOKEN = "";
        let screenMode = '30fps';
        let screenPollTimer = null;
        let isRefreshing = false;
        let streamWs = null;
        let jmuxer = null;
        let nalBuffer = new Uint8Array(0);
        let cachedSps = null;
        let cachedPps = null;
        let hasDecodedFirstKeyframe = false;
        let fpsCounter = 0;
        let lastFpsTime = performance.now();

        function isScreenTabActive() {
            const tab = document.getElementById('screen-tab');
            return !!(tab && tab.classList.contains('active'));
        }

        function setScreenMode(mode) {
            // 兼容别名与标准化参数
            if (mode === 'stream') mode = '30fps';
            if (mode === 'poll') mode = '0.2fps';
            if (mode !== '30fps' && mode !== '0.2fps' && mode !== 'off') mode = '30fps';

            screenMode = mode;
            updateModeUI();

            const vd = document.getElementById('screen-video');
            const im = document.getElementById('screen');
            const offlinePl = document.getElementById('screen-offline');
            const privacyPl = document.getElementById('screen-privacy');

            // 1. 先彻底关闭后台轮询与 WebSocket 直通流，释放后端资源
            stopScreenPolling();
            stopStreamPlayer();

            if (privacyPl) privacyPl.style.display = 'none';

            if (mode === '30fps') {
                if (vd) {
                    vd.style.display = 'block';
                    vd.poster = '/screencap?t=' + Date.now();
                }
                if (im) im.style.display = 'none';
                if (offlinePl) offlinePl.style.display = 'none';
                initStreamPlayer();
            } else if (mode === '0.2fps') {
                if (vd) vd.style.display = 'none';
                if (im) im.style.display = 'block';
                if (offlinePl) offlinePl.style.display = 'none';
                refreshScreen(true);
                startScreenPolling();
            } else if (mode === 'off') {
                if (vd) vd.style.display = 'none';
                if (im) im.style.display = 'none';
                if (offlinePl) {
                    offlinePl.style.display = 'flex';
                    const h = offlinePl.querySelector('h3');
                    const p = offlinePl.querySelector('p');
                    if (h) h.innerText = '⏸ 画面传输已关闭';
                    if (p) p.innerHTML = '已关闭实时画面传输，零网络带宽与设备算力占用。<br>点击上方「30帧」或「0.2帧」随时重新开启。';
                }
            }
        }

        function updateModeUI() {
            const badges = {
                '30fps': '🟢 30帧 硬件直通 (MSE) · 延迟 <40ms',
                '0.2fps': '🔵 0.2帧 截屏轮询 (5秒)',
                'off': '⚪ 画面传输已关闭 · 零资源占用'
            };
            const colors = {
                '30fps': ['rgba(40,167,69,0.15)', '#28a745'],
                '0.2fps': ['rgba(64,132,214,0.15)', '#4090d6'],
                'off': ['rgba(148,163,184,0.15)', '#94a3b8']
            };
            const key = (screenMode === '30fps') ? '30fps' : ((screenMode === '0.2fps') ? '0.2fps' : 'off');

            const map = {
                '30fps': document.getElementById('modeBtnStream'),
                '0.2fps': document.getElementById('modeBtnPoll'),
                'off': document.getElementById('modeBtnOff')
            };

            ['30fps', '0.2fps', 'off'].forEach(k => {
                const b = map[k];
                if (b) {
                    if (k === key) b.classList.add('active');
                    else b.classList.remove('active');
                }
            });

            const status = document.getElementById('screen-poll-status');
            if (status) {
                status.innerText = badges[key];
                status.style.background = colors[key][0];
                status.style.color = colors[key][1];
            }
        }

        function initStreamPlayer() {
            const video = document.getElementById('screen-video');
            if (!video) return;
            const badge = document.getElementById('screen-poll-status');

            stopStreamPlayer();

            // 无信号看门狗：进入 30fps 后若 3 秒内未收到任何视频帧，
            // 显示占位提示而非纯黑屏 (与 0.2fps 关屏行为一致)。
            const _frameWatchdog = setTimeout(() => {
                if (screenMode !== '30fps' || !isScreenTabActive()) return;
                const im0 = document.getElementById('screen');
                const offline0 = document.getElementById('screen-offline');
                const priv0 = document.getElementById('screen-privacy');
                if (priv0 && priv0.style.display === 'flex') return;
                if (offline0) {
                    offline0.style.display = 'flex';
                    const h = offline0.querySelector('h3');
                    const p = offline0.querySelector('p');
                    if (h) h.innerText = '⏸ 暂无画面信号';
                    if (p) p.innerHTML = '模拟器休眠或暂时无画面输出。<br>启动模拟器后将自动恢复 30 帧直通画面。';
                }
            }, 3000);

            if (badge) {
                badge.innerText = "🟡 30帧 正在连接直通流...";
                badge.style.background = "rgba(217, 119, 69, 0.15)";
                badge.style.color = "#d97706";
            }

            try {
                if (typeof JMuxer !== 'undefined') {
                    jmuxer = new JMuxer({
                        node: 'screen-video',
                        mode: 'video',
                        flushingTime: 0,
                        clearBuffer: true,
                        fps: 30,
                        debug: false,
                        onError: function(data) {
                            console.warn('JMuxer warning:', data);
                        }
                    });
                } else {
                    console.error('JMuxer 脚本未加载！');
                }
            } catch(e) {
                console.error('JMuxer 初始化异常:', e);
            }

            const proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
            const sToken = window.CURRENT_SESSION_TOKEN || localStorage.getItem('ark_session_token') || '';
            const wsParam = sToken ? `?token=${encodeURIComponent(sToken)}` : '';
            const wsUrl = `${proto}//${location.host}/ws/stream${wsParam}`;

            streamWs = new WebSocket(wsUrl);
            streamWs.binaryType = 'arraybuffer';

            streamWs.onopen = () => {
                if (badge && (screenMode === '30fps' || screenMode === 'stream')) {
                    badge.innerText = "🟢 30帧 直通已就绪，正在播放...";
                    badge.style.background = "rgba(40,167,69,0.15)";
                    badge.style.color = "#28a745";
                }
                if (video) {
                    video.muted = true;
                    video.play().catch(() => {});
                }
            };

            streamWs.onmessage = (evt) => {
                if (typeof evt.data === 'string' || screenMode !== '30fps') return;
                const raw = new Uint8Array(evt.data);
                if (raw.length === 0) return;

                // 判定是否为「帧」(VCL NALU: 类型 1=非关键帧, 5=关键帧)，
                // 仅按真实帧计数，避免把 SPS/PPS/SEI 也计入导致帧率虚高。
                let isFrame = false;
                try {
                    let off = 0;
                    if (raw[0] === 0 && raw[1] === 0 && raw[2] === 0 && raw[3] === 1) off = 4;
                    else if (raw[0] === 0 && raw[1] === 0 && raw[2] === 1) off = 3;
                    const ntype = raw[off] & 0x1f;
                    if (ntype === 1 || ntype === 5) isFrame = true;
                } catch(e) {}

                if (isFrame) {
                    // 收到真实帧：取消看门狗并隐藏占位
                    try { clearTimeout(_frameWatchdog); } catch(e) {}
                    const offline1 = document.getElementById('screen-offline');
                    if (offline1) offline1.style.display = 'none';
                    const vd1 = document.getElementById('screen-video');
                    if (vd1) vd1.style.display = 'block';
                }

                if (jmuxer) {
                    try {
                        jmuxer.feed({
                            video: raw
                        });
                    } catch(e) {
                        console.warn('JMuxer feed error:', e);
                    }
                    if (isFrame) fpsCounter++;
                    const now = performance.now();
                    if (now - lastFpsTime >= 1000) {
                        let fps = Math.round(fpsCounter * 1000 / (now - lastFpsTime));
                        if (fps > 30) fps = 30;  // 硬性上限 30 帧/秒
                        fpsCounter = 0;
                        lastFpsTime = now;
                        if (badge && (screenMode === '30fps' || screenMode === 'stream')) {
                            badge.innerText = `🟢 30帧 硬件直通 (${fps} FPS) · 延迟 <40ms`;
                            badge.style.background = 'rgba(40,167,69,0.15)';
                            badge.style.color = '#28a745';
                        }
                    }
                }
            };

            streamWs.onclose = () => {
                if (screenMode === '30fps') {
                    if (badge) badge.innerText = "⏸ 30帧 模拟器休眠待命";
                    // 无信号时显示占位提示，避免只剩纯黑屏
                    const im0 = document.getElementById('screen');
                    const offline0 = document.getElementById('screen-offline');
                    const priv0 = document.getElementById('screen-privacy');
                    if (im0) im0.style.display = 'none';
                    if (priv0 && priv0.style.display !== 'flex' && offline0) {
                        offline0.style.display = 'flex';
                        const h = offline0.querySelector('h3');
                        const p = offline0.querySelector('p');
                        if (h) h.innerText = '⏸ 暂无画面信号';
                        if (p) p.innerHTML = '模拟器休眠或暂时无画面输出。<br>模拟器启动后将自动恢复 30 帧直通画面。';
                    }
                    if (!document.hidden && isScreenTabActive()) {
                        setTimeout(() => {
                            if (screenMode === '30fps' && !document.hidden && isScreenTabActive()) {
                                initStreamPlayer();
                            }
                        }, 2000);
                    }
                }
            };
        }

        function stopStreamPlayer() {
            if (streamWs) {
                try {
                    streamWs.onopen = null;
                    streamWs.onmessage = null;
                    streamWs.onclose = null;
                    streamWs.onerror = null;
                    streamWs.close();
                } catch(e) {}
                streamWs = null;
            }
            if (jmuxer) {
                try {
                    jmuxer.destroy();
                } catch(e) {}
                jmuxer = null;
            }
            fpsCounter = 0;
        }

        // ==================== 0.2帧 截屏轮询 (每 5 秒刷新一次) ====================
        function startScreenPolling() {
            stopScreenPolling();
            screenPollTimer = setInterval(() => {
                if (screenMode === '0.2fps' && !document.hidden && isScreenTabActive()) {
                    refreshScreen(false);
                }
            }, 5000);
        }

        function stopScreenPolling() {
            if (screenPollTimer) {
                clearInterval(screenPollTimer);
                screenPollTimer = null;
            }
        }

        function refreshScreen(force = false) {
            if (isRefreshing && !force) return;
            if (!force && (document.hidden || !isScreenTabActive() || screenMode !== '0.2fps')) {
                return;
            }
            isRefreshing = true;

            fetch('/screencap?t=' + Date.now()).then(r => {
                isRefreshing = false;
                const im = document.getElementById('screen');
                const offlinePl = document.getElementById('screen-offline');
                const privacyPl = document.getElementById('screen-privacy');

                if (r.status === 403) {
                    if (im) im.style.display = 'none';
                    if (offlinePl) offlinePl.style.display = 'none';
                    if (privacyPl) privacyPl.style.display = 'flex';
                    return null;
                }
                if (r.status === 200) {
                    return r.blob();
                }
                throw new Error('Offline');
            }).then(blob => {
                if (!blob) return;
                const im = document.getElementById('screen');
                const offlinePl = document.getElementById('screen-offline');
                const privacyPl = document.getElementById('screen-privacy');

                if (screenMode === '0.2fps') {
                    if (im) {
                        im.src = URL.createObjectURL(blob);
                        im.style.display = 'block';
                    }
                    if (offlinePl) offlinePl.style.display = 'none';
                    if (privacyPl) privacyPl.style.display = 'none';
                }
            }).catch(() => {
                const im = document.getElementById('screen');
                const offlinePl = document.getElementById('screen-offline');
                if (screenMode === '0.2fps') {
                    if (im) im.style.display = 'none';
                    if (offlinePl) offlinePl.style.display = 'flex';
                }
            });
        }

        // ==================== MAA 桌面版面板与触控安全引擎 ====================
        let CURRENT_MAA_CONFIG = null;
        let CURRENT_MAA_TIMERS = null;
        let CURRENT_MAA_ALL_SLOTS = [];
        let COPILOT_JOB_LIST = [];
        let touchUnlocked = false;
        let touchAutoLockTimer = null;

        function updateTouchUI() {
            const badge = document.getElementById('touchLockStatusBadge');
            const btn = document.getElementById('btnToggleTouch');
            const container = document.getElementById('container');

            if (touchUnlocked) {
                if (badge) {
                    badge.innerText = '🟢 屏幕触控已激活 (可点击/拖拽滑动)';
                    badge.style.background = 'rgba(40, 167, 69, 0.15)';
                    badge.style.color = '#28a745';
                }
                if (btn) {
                    btn.innerText = '🔒 立即锁定触控';
                    btn.style.color = 'var(--claude-clay)';
                }
                if (container) {
                    container.style.pointerEvents = 'auto';
                    container.style.cursor = 'crosshair';
                }
            } else {
                if (badge) {
                    badge.innerText = '🔒 屏幕触控已安全锁定';
                    badge.style.background = 'rgba(100, 116, 139, 0.15)';
                    badge.style.color = '#94a3b8';
                }
                if (btn) {
                    btn.innerText = '🔓 解锁屏幕触控';
                    btn.style.color = '';
                }
                if (container) {
                    container.style.pointerEvents = 'none';
                    container.style.cursor = 'default';
                }
            }
        }

        function resetTouchTimer() {
            if (touchAutoLockTimer) clearTimeout(touchAutoLockTimer);
            if (touchUnlocked) {
                touchAutoLockTimer = setTimeout(() => {
                    lockTouch();
                    console.log('[Touch] 5分钟无操作，已自动安全上锁');
                }, 5 * 60 * 1000);
            }
        }

        function lockTouch() {
            touchUnlocked = false;
            if (touchAutoLockTimer) {
                clearTimeout(touchAutoLockTimer);
                touchAutoLockTimer = null;
            }
            updateTouchUI();
        }

        function toggleScreenTouch() {
            if (touchUnlocked) {
                lockTouch();
                return;
            }
            const curAcc = getCurrentAccount();
            const curUser = (typeof CURRENT_USER !== 'undefined' && CURRENT_USER) ? CURRENT_USER : {};
            const isOwner = (curUser.role === 'admin' || (curAcc && curAcc.owner_username === curUser.username));
            if (!isOwner) {
                alert('安全拦截：当前画面不属于您的账号环境，无法解锁触控！');
                return;
            }
            touchUnlocked = true;
            updateTouchUI();
            resetTouchTimer();
        }

        function sendAdbText() {
            const inp = document.getElementById('adbTextInput');
            const val = inp ? inp.value.trim() : '';
            if (!val) return;
            fetch(`/text?val=${encodeURIComponent(val)}`)
                .then(r => r.json())
                .then(res => {
                    if (res.status === 'ok') {
                        if (inp) inp.value = '';
                        resetTouchTimer();
                    } else if (res.error) {
                        alert('发送失败: ' + (res.message || res.error));
                    }
                }).catch(e => alert('发送异常: ' + e));
        }

        function sendAdbKey(code) {
            fetch(`/key?code=${code}`)
                .then(r => r.json())
                .then(res => {
                    resetTouchTimer();
                }).catch(e => console.warn('sendAdbKey error:', e));
        }

        function initScreenInteraction() {
            const container = document.getElementById('container');
            if (!container) return;

            let isDown = false;
            let startX = 0, startY = 0, startTime = 0;

            container.addEventListener('pointerdown', (e) => {
                if (!touchUnlocked) return;
                isDown = true;
                const rect = container.getBoundingClientRect();
                startX = e.clientX - rect.left;
                startY = e.clientY - rect.top;
                startTime = Date.now();
                resetTouchTimer();
            });

            container.addEventListener('pointerup', (e) => {
                if (!touchUnlocked || !isDown) return;
                isDown = false;
                const rect = container.getBoundingClientRect();
                const endX = e.clientX - rect.left;
                const endY = e.clientY - rect.top;
                const dx = endX - startX;
                const dy = endY - startY;
                const dist = Math.sqrt(dx * dx + dy * dy);
                const duration = Math.max(100, Math.min(800, Date.now() - startTime));

                const realX1 = Math.max(0, Math.min(1280, Math.round((startX / container.clientWidth) * 1280)));
                const realY1 = Math.max(0, Math.min(720, Math.round((startY / container.clientHeight) * 720)));
                const realX2 = Math.max(0, Math.min(1280, Math.round((endX / container.clientWidth) * 1280)));
                const realY2 = Math.max(0, Math.min(720, Math.round((endY / container.clientHeight) * 720)));

                if (dist > 15) {
                    fetch(`/swipe?x1=${realX1}&y1=${realY1}&x2=${realX2}&y2=${realY2}&duration=${duration}`)
                        .catch(err => console.warn('swipe err:', err));
                } else {
                    fetch(`/tap?x=${realX1}&y=${realY1}`)
                        .catch(err => console.warn('tap err:', err));
                }
                resetTouchTimer();
            });
        }

        function switchMaaSubTab(subId) {
            document.querySelectorAll('.maa-subcontent').forEach(el => {
                el.style.display = (el.id === subId) ? 'block' : 'none';
            });
            document.querySelectorAll('.maa-subnav-btn').forEach(btn => {
                const oc = btn.getAttribute('onclick') || '';
                if (oc.indexOf("'" + subId + "'") !== -1) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
            if (subId === 'maa-tab-timers') {
                loadMaaTimersForCurrentAccount();
            } else if (subId === 'maa-tab-copilot') {
                loadCopilotJobList();
            } else if (subId === 'maa-tab-logs') {
                loadTodayLogs();
            }
        }

        let CURRENT_MAA_TASK_SCHEMA = {};
        let CURRENT_MAA_TASK_CATALOG = [];
        let CURRENT_COPILOT_USER_ADDITIONAL = [];
        let CURRENT_MAA_INFRAST_FILES = [];
        let CURRENT_MAA_CHIP_DROP_OPTIONS = [];
        let CURRENT_MAA_DROP_STAGE_MAP = {};
        let CURRENT_MAA_STAGES = null;

        function loadMaaConfigForCurrentAccount(explicitAccId) {
            const acc = explicitAccId ? { id: explicitAccId } : getCurrentAccount();
            if (!acc || !acc.id) return;

            const titleEl = document.getElementById('maaCurrentAccountTitle');
            if (titleEl) titleEl.innerText = acc.name || acc.id;
            const statusEl = document.getElementById('maaTaskStatusBadge');
            if (statusEl) statusEl.innerText = '正在加载配置…';

            fetch(`/api/maa/config?account_id=${acc.id}`)
                .then(r => r.json())
                .then(res => {
                    if (res.success && res.config) {
                        CURRENT_MAA_CONFIG = res.config;
                        if (res.task_schema) CURRENT_MAA_TASK_SCHEMA = res.task_schema;
                        if (res.task_catalog) CURRENT_MAA_TASK_CATALOG = res.task_catalog;
                        CURRENT_MAA_INFRAST_FILES = res.infrast_files || [];
                        CURRENT_MAA_CHIP_DROP_OPTIONS = res.chip_drop_options || [];
                        CURRENT_MAA_DROP_STAGE_MAP = res.drop_stage_map || {};
                        renderMaaTaskQueueList(res.config.TaskQueue || []);
                        renderCopilotFormFromConfig(res.config.Copilot || {});
                        updateMaaPanelStatusBadge();
                    }
                }).catch(e => { console.warn('loadMaaConfig error:', e); const s=document.getElementById('maaTaskStatusBadge'); if (s) s.innerText = '配置加载失败'; });
        }

        function updateMaaPanelStatusBadge() {
            const el = document.getElementById('maaTaskStatusBadge');
            if (!el) return;
            const q = (CURRENT_MAA_CONFIG && CURRENT_MAA_CONFIG.TaskQueue) || [];
            const enabled = q.filter(t => t.IsEnable !== false).length;
            const total = q.length;
            if (total === 0) { el.innerText = '队列为空 · 请添加任务'; return; }
            el.innerText = `共 ${total} 项任务 · ${enabled} 项已启用（启动时按序执行）`;
        }

        function renderMaaTaskQueueList(queue) {
            const listEl = document.getElementById('maa-queue-list');
            if (!listEl) return;
            if (!queue || queue.length === 0) {
                listEl.innerHTML = '<div style="color:var(--claude-text-dim);padding:14px;text-align:center;">暂无长草任务配置，请点击上方【➕ 添加任务】添加</div>';
                return;
            }

            const icons = {
                'StartUpTask': '⚡',
                'RecruitTask': '📋',
                'InfrastTask': '🏢',
                'FightTask': '⚔️',
                'AnnihilationTask': '🛡️',
                'DepotMaintainTask': '📦',
                'MallTask': '🛒',
                'AwardTask': '🎁',
                'UserDataUpdateTask': '📊'
            };

            let html = '';
            queue.forEach((task, idx) => {
                const type = task['$type'] || '';
                const icon = icons[type] || '📌';
                const name = task.Name || type;
                const isEnabled = Boolean(task.IsEnable);

                html += `
                <div class="maa-task-card" id="maa-task-card-${idx}">
                    <div class="maa-task-header" onclick="toggleMaaTaskExpand(${idx})">
                        <div class="maa-task-left">
                            <input type="checkbox" class="maa-task-toggle" id="task-toggle-${idx}" ${isEnabled ? 'checked' : ''} onclick="event.stopPropagation(); updateMaaTaskEnable(${idx}, this.checked)" title="启用/禁用此任务" />
                            <span style="font-size:14px;">${icon}</span>
                            <span class="maa-task-title" id="maa-task-title-${idx}">${escapeHtml(name)}</span>
                            <span class="maa-task-desc">${escapeHtml(getMaaTaskSummary(task))}</span>
                        </div>
                        <div style="display:flex;align-items:center;gap:4px;" onclick="event.stopPropagation()">
                            <button type="button" class="maa-task-action-btn" onclick="renameMaaTask(${idx})" title="重命名此任务">✎</button>
                            <button type="button" class="maa-task-action-btn" onclick="moveMaaTask(${idx}, -1)" ${idx === 0 ? 'disabled style="opacity:0.3;cursor:not-allowed;"' : ''} title="向上移动次序">⬆</button>
                            <button type="button" class="maa-task-action-btn" onclick="moveMaaTask(${idx}, 1)" ${idx === queue.length - 1 ? 'disabled style="opacity:0.3;cursor:not-allowed;"' : ''} title="向下移动次序">⬇</button>
                            <button type="button" class="maa-task-action-btn" style="color:var(--claude-danger);" onclick="deleteMaaTask(${idx})" title="删除此任务">🗑</button>
                            <button type="button" class="maa-task-expand-btn" onclick="toggleMaaTaskExpand(${idx})">设置 ▾</button>
                        </div>
                    </div>
                    <div class="maa-task-body" id="maa-task-body-${idx}">
                        ${renderMaaTaskDetailForm(task, idx)}
                    </div>
                </div>`;
            });
            listEl.innerHTML = html;
        }

        function renameMaaTask(idx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[idx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[idx];
            const oldName = t.Name || t['$type'] || '任务';
            const input = prompt('重命名任务（可自由命名，方便区分多个同类任务）:', oldName);
            if (input === null) return;
            const newName = input.trim();
            if (!newName) { alert('任务名称不能为空'); return; }
            t.Name = newName;
            const titleEl = document.getElementById(`maa-task-title-${idx}`);
            if (titleEl) titleEl.innerText = newName;
            rerenderMaaTaskBody(idx);
        }

        function syncMaaTaskTitle(idx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[idx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[idx];
            const titleEl = document.getElementById(`maa-task-title-${idx}`);
            if (titleEl) titleEl.innerText = t.Name || t['$type'] || '任务';
        }

        function toggleMaaTaskExpand(idx) {
            const card = document.getElementById(`maa-task-card-${idx}`);
            if (card) card.classList.toggle('expanded');
            if (card && card.classList.contains('expanded')) {
                requestAnimationFrame(() => card.scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
            }
        }

        function updateMaaTaskEnable(idx, enabled) {
            if (CURRENT_MAA_CONFIG && CURRENT_MAA_CONFIG.TaskQueue && CURRENT_MAA_CONFIG.TaskQueue[idx]) {
                CURRENT_MAA_CONFIG.TaskQueue[idx].IsEnable = enabled;
                updateMaaPanelStatusBadge();
            }
        }

        function moveMaaTask(idx, dir) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue) return;
            const q = CURRENT_MAA_CONFIG.TaskQueue;
            const targetIdx = idx + dir;
            if (targetIdx < 0 || targetIdx >= q.length) return;
            const moved = q.splice(idx, 1)[0];
            q.splice(targetIdx, 0, moved);
            renderMaaTaskQueueList(q);
            toggleMaaTaskExpand(targetIdx);
        }

        function deleteMaaTask(idx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[idx];
            const name = t ? (t.Name || t['$type']) : '此任务';
            if (!confirm(`确定从任务队列中删除【${name}】吗？`)) return;
            CURRENT_MAA_CONFIG.TaskQueue.splice(idx, 1);
            renderMaaTaskQueueList(CURRENT_MAA_CONFIG.TaskQueue);
        }

        function openAddMaaTaskModal() {
            const listEl = document.getElementById('maa-add-task-catalog-list');
            const m = document.getElementById('modal-add-maa-task');
            if (!listEl || !m) return;

            let html = '';
            (CURRENT_MAA_TASK_CATALOG || []).forEach(cat => {
                html += `
                <div class="card" style="padding:10px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;background:var(--claude-surface);border:1px solid var(--claude-border-subtle);border-radius:6px;transition:background 0.2s;" onmouseover="this.style.background='var(--claude-surface-hover)'" onmouseout="this.style.background='var(--claude-surface)'" onclick="confirmAddMaaTask('${cat.type}', '${escapeHtml(cat.name)}')">
                    <div style="display:flex;gap:10px;align-items:center;">
                        <span style="font-size:20px;">${cat.icon}</span>
                        <div>
                            <div style="font-weight:600;font-size:13px;color:var(--claude-text-main);">${escapeHtml(cat.name)} <span style="font-size:11px;color:var(--claude-text-dim);font-family:var(--font-mono);">(${cat.type})</span></div>
                            <div style="font-size:11px;color:var(--claude-text-muted);">${escapeHtml(cat.desc)}</div>
                        </div>
                    </div>
                    <button type="button" class="btn-action" style="font-size:11px;padding:4px 10px;color:var(--claude-clay);border-color:var(--claude-clay);font-weight:600;">+ 加入队列</button>
                </div>`;
            });
            listEl.innerHTML = html;
            m.classList.add('show');
            m.style.display = 'flex';
        }

        function closeAddMaaTaskModal() {
            const m = document.getElementById('modal-add-maa-task');
            if (m) {
                m.classList.remove('show');
                m.style.display = 'none';
            }
        }

        function confirmAddMaaTask(taskType, taskName) {
            if (!CURRENT_MAA_CONFIG) CURRENT_MAA_CONFIG = { TaskQueue: [] };
            if (!CURRENT_MAA_CONFIG.TaskQueue) CURRENT_MAA_CONFIG.TaskQueue = [];

            let newTask = { "$type": taskType, "Name": taskName, "IsEnable": true };
            const schemaFields = CURRENT_MAA_TASK_SCHEMA[taskType] || [];
            schemaFields.forEach(f => {
                if (f.default !== undefined) {
                    try {
                        newTask[f.key] = JSON.parse(JSON.stringify(f.default));
                    } catch(e) {
                        newTask[f.key] = f.default;
                    }
                }
            });

            CURRENT_MAA_CONFIG.TaskQueue.push(newTask);
            closeAddMaaTaskModal();
            renderMaaTaskQueueList(CURRENT_MAA_CONFIG.TaskQueue);
            const newIdx = CURRENT_MAA_CONFIG.TaskQueue.length - 1;
            toggleMaaTaskExpand(newIdx);
        }

        function getMaaTaskSummary(t) {
            const type = t['$type'];
            if (type === 'StartUpTask') return t.AccountName ? `尾号: ${t.AccountName}` : '当前会话环境';
            if (type === 'RecruitTask') return `上限 ${t.MaxTimes || 4} 次 · ${t.Level6Choose ? '自动确认高资' : '高资告警人工'}`;
            if (type === 'InfrastTask') {
                const modeLabel = { 'Default': '常规模式', 'Rotation': '队列轮换', 'Custom': '自定义配置' }[t.Mode] || '常规模式';
                return t.Mode === 'Custom' ? `${modeLabel} · ${t.Filename || '未导入文件'}` : modeLabel;
            }
            if (type === 'AnnihilationTask') {
                const cur = t.Stage && t.Stage !== 'Annihilation' ? '常驻' : '当期';
                return `剿灭 (${cur})`;
            }
            if (type === 'FightTask') {
                const stages = Array.isArray(t.StagePlan) ? t.StagePlan.join(', ') : (t.Stage || '1-7');
                const extra = t.SanityOnly ? '剩余理智' : '理智作战';
                return `${extra}: ${stages || '1-7'}${t.UseWeeklySchedule ? ' (周计划)' : ''}`;
            }
            if (type === 'DepotMaintainTask') {
                const n = (t.PlanList || []).length;
                return `库存保持 (${n} 条芯片计划)`;
            }
            if (type === 'MallTask') return '信用商店购物与好友拜访';
            if (type === 'AwardTask') return '日常/邮件/单抽/采矿奖励';
            if (type === 'UserDataUpdateTask') return '全量仓库与干员列表刷新';
            return '';
        }

        function maaFieldVisible(f, t) {
            if (f.showWhen) {
                for (const k in f.showWhen) {
                    if (String(t[k]) !== String(f.showWhen[k])) return false;
                }
            }
            if (f.hideWhen) {
                for (const k in f.hideWhen) {
                    if (String(t[k]) === String(f.hideWhen[k])) return false;
                }
            }
            return true;
        }

        function renderMaaTaskDetailForm(t, idx) {
            const type = t['$type'];
            let fields = CURRENT_MAA_TASK_SCHEMA[type] || [];
            // 剩余理智：仅保留关卡选择, 且改为单关卡控件
            if (t.SanityOnly) {
                fields = fields.filter(f => f.key === 'StagePlan').map(f => Object.assign({}, f, { type: 'sanity_stage', label: '剩余理智刷取关卡 (仅单个)' }));
            }

            // 通用任务名称编辑 (所有任务均可自由命名)
            let body = `
            <div class="form-row">
                <div style="display:flex;flex-direction:column;">
                    <span>任务名称</span>
                    <span style="font-size:10px;color:var(--claude-text-dim);">可自由命名，便于区分多个同类任务</span>
                </div>
                <input type="text" class="form-input" style="flex:1;max-width:240px;margin-bottom:0;" value="${escapeHtml(t.Name || type)}" placeholder="任务名称" onchange="updateMaaTaskField(${idx}, 'Name', this.value); syncMaaTaskTitle(${idx})" />
            </div>`;

            if (!fields || fields.length === 0) {
                return body + '<div style="color:var(--claude-text-dim);font-size:11px;">按官方标准默认算法执行。</div>';
            }

            fields.forEach(f => {
                if (!maaFieldVisible(f, t)) return;
                const key = f.key;
                let val = t[key];
                if (val === undefined) val = f.default;

                if (f.type === 'bool') {
                    body += `
                    <div class="form-row">
                        <div style="display:flex;flex-direction:column;">
                            <span>${escapeHtml(f.label)}</span>
                            ${f.hint ? `<span style="font-size:10px;color:var(--claude-text-dim);">${escapeHtml(f.hint)}</span>` : ''}
                        </div>
                        <input type="checkbox" ${val ? 'checked' : ''} onchange="updateMaaTaskField(${idx}, '${key}', this.checked); rerenderMaaTaskBody(${idx})" />
                    </div>`;
                } else if (f.type === 'number') {
                    body += `
                    <div class="form-row">
                        <span>${escapeHtml(f.label)}</span>
                        <input type="number" class="form-input" style="width:90px;text-align:center;margin-bottom:0;" min="${f.min !== undefined ? f.min : 0}" max="${f.max !== undefined ? f.max : 999999}" value="${val !== undefined ? val : (f.default||0)}" onchange="updateMaaTaskField(${idx}, '${key}', parseInt(this.value)||0)" />
                    </div>`;
                } else if (f.type === 'text') {
                    const ph = f.key === 'AccountName' ? '仅填尾号，如 4567' : (f.hint || '');
                    body += `
                    <div class="form-row">
                        <div style="display:flex;flex-direction:column;">
                            <span>${escapeHtml(f.label)}</span>
                            ${f.hint ? `<span style="font-size:10px;color:var(--claude-text-dim);">${escapeHtml(f.hint)}</span>` : ''}
                        </div>
                        <input type="text" class="form-input" style="flex:1;max-width:240px;margin-bottom:0;" value="${escapeHtml(val || '')}" placeholder="${escapeHtml(ph)}" onchange="updateMaaTaskField(${idx}, '${key}', this.value)" />
                    </div>`;
                } else if (f.type === 'select') {
                    let opts = '';
                    (f.options || []).forEach(o => {
                        const sel = String(val) === String(o.value) ? 'selected' : '';
                        opts += `<option value="${escapeHtml(o.value)}" ${sel}>${escapeHtml(o.label)}</option>`;
                    });
                    body += `
                    <div class="form-row">
                        <div style="display:flex;flex-direction:column;">
                            <span>${escapeHtml(f.label)}</span>
                            ${f.hint ? `<span style="font-size:10px;color:var(--claude-text-dim);">${escapeHtml(f.hint)}</span>` : ''}
                        </div>
                        <select style="max-width:280px;margin-bottom:0;" onchange="updateMaaTaskField(${idx}, '${key}', isNaN(Number(this.value)) ? this.value : Number(this.value)); rerenderMaaTaskBody(${idx})">
                            ${opts}
                        </select>
                    </div>`;
                } else if (f.type === 'stage_list') {
                    const strVal = Array.isArray(val) ? val.join(', ') : (val || '');
                    body += `
                    <div class="form-row" style="align-items:flex-start;">
                        <div style="display:flex;flex-direction:column;">
                            <span>${escapeHtml(f.label)}</span>
                            <span style="font-size:10px;color:var(--claude-text-dim);">${escapeHtml(f.hint || '')}</span>
                        </div>
                        <input type="text" class="form-input" style="flex:1;max-width:280px;margin-bottom:0;" value="${escapeHtml(strVal)}" placeholder="例如: 1-7, CE-6, Annihilation" onchange="updateMaaTaskField(${idx}, '${key}', this.value.split(/[,，\\s]+/).map(s=>s.trim()).filter(Boolean))" />
                    </div>`;
                } else if (f.type === 'infrast_file') {
                    let opts = '<option value="">-- 请选择已导入的排班文件 --</option>';
                    (CURRENT_MAA_INFRAST_FILES || []).forEach(fn => {
                        const sel = String(val) === String(fn) ? 'selected' : '';
                        opts += `<option value="${escapeHtml(fn)}" ${sel}>${escapeHtml(fn)}</option>`;
                    });
                    body += `
                    <div class="form-row" style="align-items:flex-start;flex-direction:column;gap:4px;">
                        <span>${escapeHtml(f.label)}</span>
                        <div style="display:flex;gap:6px;width:100%;align-items:center;">
                            <select style="flex:1;margin-bottom:0;max-width:none;" onchange="updateMaaTaskField(${idx}, '${key}', this.value)">
                                ${opts}
                            </select>
                            <button type="button" class="btn-action" style="padding:6px 10px;font-size:11px;white-space:nowrap;" onclick="triggerMaaInfrastUpload(${idx})">📤 导入文件</button>
                        </div>
                        <span style="font-size:10px;color:var(--claude-text-dim);">从本地导入 MAA 基建排班 JSON；切换文件时旧文件将被服务端清理，不保留历史</span>
                        <input type="file" id="maaInfrastFileInput" accept=".json" style="display:none;" onchange="handleMaaInfrastUpload(event, ${idx})" />
                    </div>`;
                } else if (f.type === 'stage_plan') {
                    const strVal = Array.isArray(val) ? val.join(', ') : (val || '');
                    body += `
                    <div class="form-row" style="align-items:flex-start;flex-direction:column;gap:4px;">
                        <div style="display:flex;flex-direction:column;width:100%;">
                            <span>${escapeHtml(f.label)}</span>
                            <span style="font-size:10px;color:var(--claude-text-dim);">手动输入关卡名（多个用逗号分隔，例如 1-7, CE-6, PR-A-1, Annihilation）；保存时会校验合法性</span>
                        </div>
                        <input type="text" id="maa-stage-input-${idx}" class="form-input" style="width:100%;margin-bottom:0;font-family:var(--font-mono);" value="${escapeHtml(strVal)}" placeholder="例如: 1-7, CE-6, PR-A-1, Annihilation" onchange="updateMaaTaskField(${idx}, '${key}', this.value.split(/[,，\\s]+/).map(s=>s.trim()).filter(Boolean)); checkMaaStageInput(${idx})" oninput="checkMaaStageInput(${idx})" />
                        <span id="maa-stage-tip-${idx}" style="font-size:10px;color:var(--claude-text-dim);">支持：主线关卡、资源本(CE/LS/CA/AP/SK)、芯片本(PR-)、剿灭、活动关卡</span>
                    </div>`;
                } else if (f.type === 'plan_list') {
                    let rows = '';
                    const plans = Array.isArray(val) ? val : [];
                    plans.forEach((p, pi) => {
                        let dropOpts = '<option value="">-- 选择目标材料 --</option>';
                        (CURRENT_MAA_CHIP_DROP_OPTIONS || []).forEach(o => {
                            const sel = String(p.DropId) === String(o.value) ? 'selected' : '';
                            dropOpts += `<option value="${escapeHtml(o.value)}" ${sel}>${escapeHtml(o.label)}</option>`;
                        });
                        rows += `
                        <div style="display:flex;gap:6px;align-items:center;background:var(--claude-surface);padding:5px 8px;border-radius:5px;border:1px solid var(--claude-border-subtle);">
                            <select style="flex:1;margin-bottom:0;font-size:11px;" onchange="updateMaaPlanDrop(${idx}, ${pi}, this.value)">${dropOpts}</select>
                            <input type="text" class="form-input" style="width:78px;margin-bottom:0;font-size:11px;text-align:center;" value="${escapeHtml(p.Stage||'')}" placeholder="关卡" readonly title="由材料自动填充" />
                            <input type="number" class="form-input" style="width:64px;margin-bottom:0;font-size:11px;text-align:center;" value="${p.DropCount||0}" min="0" onchange="updateMaaPlanField(${idx}, ${pi}, 'DropCount', parseInt(this.value)||0)" title="目标库存数量" />
                            <button type="button" class="btn-action" style="padding:2px 6px;font-size:10px;color:var(--claude-danger);" onclick="deleteMaaPlanItem(${idx}, ${pi})">✕</button>
                        </div>`;
                    });
                    body += `
                    <div class="form-row" style="flex-direction:column;align-items:stretch;gap:6px;">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <span>${escapeHtml(f.label)}</span>
                            <button type="button" class="btn-action" style="font-size:10px;padding:2px 8px;" onclick="addMaaPlanItem(${idx})">+ 添加计划</button>
                        </div>
                        <div style="display:flex;flex-direction:column;gap:5px;">${rows || '<span style="font-size:11px;color:var(--claude-text-dim);">暂无计划条目</span>'}</div>
                    </div>`;
                } else if (f.type === 'sanity_stage') {
                    const arr = Array.isArray(val) ? val : (val ? [val] : []);
                    const cur = arr[0] || '1-7';
                    const isCustom = cur !== '1-7' && cur !== 'Current';
                    const choices = [['1-7','1-7'],['Current','当前/上次'],['__custom__','自定义']];
                    let chips = '';
                    choices.forEach(([v,l]) => {
                        const active = (v === '__custom__' ? isCustom : cur === v) ? 'active' : '';
                        chips += `<span class="weekly-day-chip ${active}" onclick="setMaaSanityStage(${idx}, '${v}')">${l}</span>`;
                    });
                    body += `
                    <div class="form-row" style="flex-direction:column;align-items:flex-start;gap:6px;">
                        <span>${escapeHtml(f.label)}</span>
                        <div class="weekly-days-grid">${chips}</div>
                        <input type="text" class="form-input" style="max-width:220px;margin-bottom:0;font-size:12px;display:${isCustom?'block':'none'};font-family:var(--font-mono);" id="maa-sanity-custom-${idx}" placeholder="手动输入关卡号，如 1-7 / CE-6" value="${isCustom?escapeHtml(cur):''}" onchange="updateMaaTaskField(${idx}, 'StagePlan', [this.value.trim()]); checkMaaStageInput(${idx})" oninput="checkMaaStageInput(${idx})" />
                        <span id="maa-stage-tip-${idx}" style="font-size:10px;color:var(--claude-text-dim);"></span>
                    </div>`;
                } else if (f.type === 'weekly') {
                    const days = [
                        {k: "Monday", l: "周一"}, {k: "Tuesday", l: "周二"}, {k: "Wednesday", l: "周三"},
                        {k: "Thursday", l: "周四"}, {k: "Friday", l: "周五"}, {k: "Saturday", l: "周六"}, {k: "Sunday", l: "周日"}
                    ];
                    const ws = val || {};
                    let chips = '';
                    days.forEach(d => {
                        const act = ws[d.k] ? 'active' : '';
                        chips += `<span class="weekly-day-chip ${act}" onclick="toggleMaaWeeklyDay(${idx}, '${d.k}')">${d.l}</span>`;
                    });
                    body += `
                    <div class="form-row" style="flex-direction:column;align-items:flex-start;gap:4px;">
                        <span>${escapeHtml(f.label)}</span>
                        <div class="weekly-days-grid">${chips}</div>
                    </div>`;
                }
            });
            return body;
        }

        function rerenderMaaTaskBody(taskIdx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const bodyEl = document.getElementById(`maa-task-body-${taskIdx}`);
            if (bodyEl) bodyEl.innerHTML = renderMaaTaskDetailForm(CURRENT_MAA_CONFIG.TaskQueue[taskIdx], taskIdx);
        }

        function addMaaPlanItem(taskIdx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            if (!Array.isArray(t.PlanList)) t.PlanList = [];
            t.PlanList.push({ Stage: "", DropId: "", DropCount: 0, UseMedicine: false, MedicineCount: 0, UseStone: false, StoneCount: 0 });
            rerenderMaaTaskBody(taskIdx);
        }

        function deleteMaaPlanItem(taskIdx, pi) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            (t.PlanList || []).splice(pi, 1);
            rerenderMaaTaskBody(taskIdx);
        }

        function updateMaaPlanField(taskIdx, pi, key, val) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            if (!Array.isArray(t.PlanList) || !t.PlanList[pi]) return;
            t.PlanList[pi][key] = val;
            const cardEl = document.getElementById(`maa-task-card-${taskIdx}`);
            if (cardEl) {
                const descEl = cardEl.querySelector('.maa-task-desc');
                if (descEl) descEl.innerText = getMaaTaskSummary(t);
            }
        }

        function updateMaaPlanDrop(taskIdx, pi, dropId) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            if (!Array.isArray(t.PlanList) || !t.PlanList[pi]) return;
            t.PlanList[pi].DropId = dropId;
            // 自动填充关卡名 (芯片 x1 默认 5, 芯片组 x2 默认 8)
            const map = CURRENT_MAA_DROP_STAGE_MAP || {};
            if (dropId && map[dropId]) t.PlanList[pi].Stage = map[dropId];
            if (!t.PlanList[pi].DropCount || t.PlanList[pi].DropCount <= 0) {
                t.PlanList[pi].DropCount = (dropId && dropId.endsWith("2")) ? 8 : 5;
            }
            rerenderMaaTaskBody(taskIdx);
        }

        function setMaaSanityStage(taskIdx, choice) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            if (choice === '__custom__') {
                const cur = (Array.isArray(t.StagePlan) && t.StagePlan[0]) || "";
                if (cur === '1-7' || cur === 'Current' || !cur) {
                    const inp = document.getElementById(`maa-sanity-custom-${taskIdx}`);
                    if (inp && inp.value.trim()) { t.StagePlan = [inp.value.trim()]; }
                    else { t.StagePlan = [""]; }
                }
            } else {
                t.StagePlan = [choice];
            }
            rerenderMaaTaskBody(taskIdx);
        }


        function triggerMaaInfrastUpload(taskIdx) {
            const fi = document.getElementById('maaInfrastFileInput');
            if (fi) { fi.dataset.taskIdx = taskIdx; fi.click(); }
        }

        function handleMaaInfrastUpload(e, taskIdx) {
            const file = e.target.files[0];
            if (!file) return;
            const acc = getCurrentAccount();
            const reader = new FileReader();
            reader.onload = (evt) => {
                try {
                    const content = JSON.parse(evt.target.result);
                    fetch('/api/infrast_plans/upload', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename: file.name, content: content, account_id: acc ? acc.id : null })
                    }).then(r => r.json()).then(res => {
                        if (res.success) {
                            CURRENT_MAA_INFRAST_FILES = [res.filename];
                            if (CURRENT_MAA_CONFIG && CURRENT_MAA_CONFIG.TaskQueue && CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) {
                                CURRENT_MAA_CONFIG.TaskQueue[taskIdx].Filename = res.filename;
                            }
                            alert(`✔ 排班文件 [${res.filename}] 已导入并绑定 (该账号仅保留此文件)`);
                            rerenderMaaTaskBody(taskIdx);
                        } else {
                            alert('导入失败: ' + (res.error || '未知错误'));
                        }
                    }).catch(err => alert('网络异常: ' + err));
                } catch(err) {
                    alert('文件非合法 JSON 格式！');
                }
                e.target.value = '';
            };
            reader.readAsText(file);
        }

        // 关卡名合法性校验 (与后端 maa_runner / MAA 协议一致的常见格式)
        function isValidStageCode(s) {
            s = (s || '').trim();
            if (!s) return false;
            if (s === 'Current' || s === '当前' || s === '当前/上次') return true;
            if (['Annihilation','Chernobog@Annihilation','LungmenOutskirts@Annihilation','LungmenDowntown@Annihilation'].includes(s)) return true;
            if (/^[0-9]{1,2}-[0-9]{1,2}(-NORMAL|-HARD)?$/i.test(s)) return true;
            if (/^(R|TR|EG|LT)[0-9]{1,2}-[0-9]{1,2}$/i.test(s)) return true;
            if (/^(CE|LS|CA|AP|SK)-[0-9]{1,2}$/i.test(s)) return true;
            if (/^PR-[A-D]-[0-9]$/i.test(s)) return true;
            if (/^[A-Za-z]{2,4}-[0-9]{1,2}$/.test(s)) return true;
            if (/^SSReopen-[A-Za-z0-9]+$/i.test(s)) return true;
            return false;
        }

        function checkMaaStageInput(taskIdx) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return true;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            const tipEl = document.getElementById(`maa-stage-tip-${taskIdx}`);
            const arr = Array.isArray(t.StagePlan) ? t.StagePlan.filter(Boolean) : [];
            const inp = document.getElementById(`maa-stage-input-${taskIdx}`);
            if (arr.length === 0) {
                if (tipEl) { tipEl.style.color = 'var(--claude-danger)'; tipEl.innerText = '⚠️ 请至少填写一个关卡名'; }
                if (inp) inp.style.borderColor = 'var(--claude-danger)';
                return false;
            }
            const bad = arr.filter(s => !isValidStageCode(s));
            if (bad.length > 0) {
                if (tipEl) { tipEl.style.color = 'var(--claude-danger)'; tipEl.innerText = `⚠️ 非法关卡名：${bad.join(', ')}（格式如 1-7 / CE-6 / PR-A-1 / Annihilation）`; }
                if (inp) inp.style.borderColor = 'var(--claude-danger)';
                return false;
            }
            if (inp) inp.style.borderColor = '';
            if (tipEl) { tipEl.style.color = 'var(--claude-sage)'; tipEl.innerText = `✔ 关卡格式校验通过：${arr.join(', ')}`; }
            return true;
        }

        function validateAllMaaStages() {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue) return true;
            let ok = true;
            CURRENT_MAA_CONFIG.TaskQueue.forEach((t, idx) => {
                if (!t.IsEnable) return;
                if (t['$type'] !== 'FightTask') return;
                if (!checkMaaStageInput(idx)) ok = false;
            });
            return ok;
        }

        function toggleMaaWeeklyDay(taskIdx, dayKey) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            const t = CURRENT_MAA_CONFIG.TaskQueue[taskIdx];
            if (!t.WeeklySchedule) t.WeeklySchedule = {};
            t.WeeklySchedule[dayKey] = !t.WeeklySchedule[dayKey];
            rerenderMaaTaskBody(taskIdx);
        }

        function updateMaaTaskField(taskIdx, key, val) {
            if (!CURRENT_MAA_CONFIG || !CURRENT_MAA_CONFIG.TaskQueue || !CURRENT_MAA_CONFIG.TaskQueue[taskIdx]) return;
            CURRENT_MAA_CONFIG.TaskQueue[taskIdx][key] = val;
            const cardEl = document.getElementById(`maa-task-card-${taskIdx}`);
            if (cardEl) {
                const descEl = cardEl.querySelector('.maa-task-desc');
                if (descEl) descEl.innerText = getMaaTaskSummary(CURRENT_MAA_CONFIG.TaskQueue[taskIdx]);
            }
        }

        function saveMaaConfigForCurrentAccount() {
            const acc = getCurrentAccount();
            if (!acc || !acc.id || !CURRENT_MAA_CONFIG) return;
            if (!validateAllMaaStages()) {
                alert('⚠️ 有理智作战任务的关卡名不合法，请修正后再保存（支持格式：1-7 / CE-6 / PR-A-1 / Annihilation / 活动关卡代码）。');
                return;
            }
            const btn = document.getElementById('btnSaveMaaConfig');
            if (btn) { btn.disabled = true; btn.innerText = '保存中...'; }

            fetch('/api/maa/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ account_id: acc.id, config: CURRENT_MAA_CONFIG })
            }).then(r => r.json()).then(res => {
                if (btn) { btn.disabled = false; btn.innerText = '💾 保存长草配置'; }
                if (res.success) {
                    alert('✔ MAA 桌面版长草配置已保存成功！');
                } else if (res.error === 'invalid_stage') {
                    alert('⚠️ ' + (res.message || '关卡名不合法，请修正后再保存'));
                } else {
                    alert('保存失败: ' + (res.error || '未知错误'));
                }
            }).catch(e => {
                if (btn) { btn.disabled = false; btn.innerText = '💾 保存长草配置'; }
                alert('网络异常: ' + e);
            });
        }

        function loadMaaTimersForCurrentAccount() {
            const acc = getCurrentAccount();
            if (!acc || !acc.id) return;
            fetch(`/api/maa/timers?account_id=${acc.id}`)
                .then(r => r.json())
                .then(res => {
                    if (res.success && res.timers) {
                        CURRENT_MAA_TIMERS = res.timers;
                        CURRENT_MAA_ALL_SLOTS = res.all_slots || [];
                        const forceCb = document.getElementById('maaTimerForceStart');
                        if (forceCb) forceCb.checked = (res.timers.ForceScheduledStart !== false);
                        renderMaaTimersList(res.timers.List || [], res.window_minutes || 40);
                        renderMaaTimelineGantt(res.all_slots || [], res.window_minutes || 40);
                    }
                }).catch(e => console.warn('loadMaaTimers error:', e));
        }

        function renderMaaTimersList(list, windowMinutes) {
            const container = document.getElementById('maaTimersList');
            if (!container) return;
            if (!list || list.length === 0) {
                container.innerHTML = '<div style="color:var(--claude-text-dim);padding:8px;font-size:12px;text-align:center;">暂未启用任何定时班次</div>';
                return;
            }

            let html = '';
            list.forEach((t, idx) => {
                const h = String(t.Hour || 0).padStart(2, '0');
                const m = String(t.Minute || 0).padStart(2, '0');
                const timeStr = `${h}:${m}`;
                const endTotal = (parseInt(h) * 60 + parseInt(m) + windowMinutes) % 1440;
                const endStr = `${String(Math.floor(endTotal / 60)).padStart(2, '0')}:${String(endTotal % 60).padStart(2, '0')}`;
                const isEn = Boolean(t.IsEnabled);

                html += `
                <div class="timer-row" id="timer-row-${idx}">
                    <div style="display:flex;align-items:center;gap:10px;">
                        <input type="checkbox" class="maa-task-toggle" ${isEn ? 'checked' : ''} onchange="updateMaaTimerField(${idx}, 'IsEnabled', this.checked)" title="开关此班次" />
                        <input type="time" class="form-input" style="width:110px;text-align:center;font-size:14px;font-weight:bold;margin-bottom:0;" value="${timeStr}" onchange="updateMaaTimerTime(${idx}, this.value)" />
                        <span class="slot-pill" style="font-size:11px;font-weight:bold;">${timeStr} ~ ${endStr} (${windowMinutes}分独占)</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px;">
                        <button type="button" class="btn-action" style="color:var(--claude-danger);padding:2px 8px;font-size:12px;" onclick="deleteMaaTimerRow(${idx})" title="删除此班次">🗑️</button>
                    </div>
                </div>`;
            });
            container.innerHTML = html;
        }

        function updateMaaTimerField(idx, field, val) {
            if (CURRENT_MAA_TIMERS && CURRENT_MAA_TIMERS.List && CURRENT_MAA_TIMERS.List[idx]) {
                CURRENT_MAA_TIMERS.List[idx][field] = val;
                rerenderMaaTimers();
            }
        }

        function updateMaaTimerTime(idx, timeVal) {
            if (!timeVal || !CURRENT_MAA_TIMERS || !CURRENT_MAA_TIMERS.List || !CURRENT_MAA_TIMERS.List[idx]) return;
            const parts = timeVal.split(':');
            CURRENT_MAA_TIMERS.List[idx].Hour = parseInt(parts[0]) || 0;
            CURRENT_MAA_TIMERS.List[idx].Minute = parseInt(parts[1]) || 0;
            rerenderMaaTimers();
        }

        function rerenderMaaTimers() {
            if (!CURRENT_MAA_TIMERS) return;
            renderMaaTimersList(CURRENT_MAA_TIMERS.List || [], CURRENT_MAA_TIMERS.WindowMinutes || 40);
            renderMaaTimelineGantt(buildLocalSlots(), CURRENT_MAA_TIMERS.WindowMinutes || 40);
        }

        function buildLocalSlots() {
            const slots = [];
            // 保留其他账号的已占用时段
            (CURRENT_MAA_ALL_SLOTS || []).forEach(s => {
                if (!s.is_current) slots.push(s);
            });
            if (!CURRENT_MAA_TIMERS || !CURRENT_MAA_TIMERS.List) return slots;
            // 叠加当前账号本地编辑中的班次
            CURRENT_MAA_TIMERS.List.forEach(t => {
                if (t.IsEnabled) {
                    slots.push({
                        hour: parseInt(t.Hour) || 0,
                        minute: parseInt(t.Minute) || 0,
                        window_minutes: CURRENT_MAA_TIMERS.WindowMinutes || 40,
                        is_current: true,
                        account_name: '当前账号'
                    });
                }
            });
            return slots;
        }

        function addNewMaaTimerRow() {
            if (!CURRENT_MAA_TIMERS) CURRENT_MAA_TIMERS = { List: [], WindowMinutes: 40 };
            if (!CURRENT_MAA_TIMERS.List) CURRENT_MAA_TIMERS.List = [];
            // 选取一个当前未被占用的候选时间，避免新班次默认互相冲突
            const used = new Set(CURRENT_MAA_TIMERS.List.map(t => (((parseInt(t.Hour) || 0) * 60) + (parseInt(t.Minute) || 0))));
            const candidates = [8, 10, 12, 14, 16, 18, 20, 22, 6, 0, 9, 11, 13, 15, 17, 19, 21, 23];
            let hour = 10, minute = 0;
            for (const h of candidates) {
                if (!used.has(h * 60)) { hour = h; minute = 0; break; }
            }
            const nextId = CURRENT_MAA_TIMERS.List.reduce((mx, t) => Math.max(mx, parseInt(t.Id) || 0), -1) + 1;
            CURRENT_MAA_TIMERS.List.push({
                Id: nextId,
                IsEnabled: true,
                Config: 'Default',
                Hour: hour,
                Minute: minute,
                Remark: '新班次'
            });
            rerenderMaaTimers();
        }

        function deleteMaaTimerRow(idx) {
            if (!CURRENT_MAA_TIMERS || !CURRENT_MAA_TIMERS.List) return;
            CURRENT_MAA_TIMERS.List.splice(idx, 1);
            rerenderMaaTimers();
        }

        function saveMaaTimersForCurrentAccount() {
            const acc = getCurrentAccount();
            if (!acc || !acc.id || !CURRENT_MAA_TIMERS) return;
            const alertBox = document.getElementById('maaTimerConflictAlert');
            if (alertBox) alertBox.style.display = 'none';

            fetch('/api/maa/timers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ account_id: acc.id, timers: CURRENT_MAA_TIMERS, window_minutes: 40 })
            }).then(r => r.json().then(data => ({ status: r.status, data }))).then(({ status, data }) => {
                if (status === 200 && data.success) {
                    alert('✔ 定时执行配置已成功保存并同步系统调度！');
                    loadMaaTimersForCurrentAccount();
                } else if (status === 409 || data.error === 'conflict') {
                    if (alertBox) {
                        alertBox.style.display = 'block';
                        alertBox.innerHTML = `⚠️ <b>${escapeHtml(data.message || '时段冲突禁止保存')}</b>`;
                    }
                    alert(data.message || '时段冲突禁止保存，请错开时段！');
                } else {
                    alert('保存异常: ' + (data.message || data.error || '未知错误'));
                }
            }).catch(e => alert('网络异常: ' + e));
        }

        function renderMaaTimelineGantt(allSlots, windowMinutes) {
            const blocksEl = document.getElementById('maa-timeline-blocks');
            if (!blocksEl) return;
            blocksEl.innerHTML = '';

            allSlots.forEach(s => {
                const startM = (s.hour * 60 + s.minute) % 1440;
                const dur = s.window_minutes || windowMinutes || 40;
                const leftPct = (startM / 1440) * 100;
                const widthPct = (dur / 1440) * 100;
                const bg = s.is_current ? 'var(--claude-clay)' : '#64748b';

                const block = document.createElement('div');
                block.style.position = 'absolute';
                block.style.top = '2px';
                block.style.bottom = '2px';
                block.style.left = leftPct + '%';
                block.style.width = Math.max(0.6, widthPct) + '%';
                block.style.background = bg;
                block.style.borderRadius = '3px';
                block.style.opacity = '0.9';
                block.title = `${s.account_name} (${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')} ~ ${dur}分钟)`;
                blocksEl.appendChild(block);
            });
        }

        // --- Copilot 自动战斗逻辑 (1:1 复刻 MAA 桌面版) ---
        let CURRENT_COPILOT_JOBS = [];   // [{filename,title,details,is_raid,checked}]

        function onCopilotFormChange(on) {
            const deps = document.getElementById('copilotFormDeps');
            if (deps) deps.style.display = on ? 'block' : 'none';
        }
        function onCopilotUseListChange(on) {
            const listDeps = document.getElementById('copilotListDeps');
            const loopDeps = document.getElementById('copilotLoopDeps');
            if (listDeps) listDeps.style.display = on ? 'block' : 'none';
            if (loopDeps) loopDeps.style.display = on ? 'none' : 'block';
        }
        function toggleCopilotUseFormation(on) {
            const row = document.getElementById('rowCopilotFormationIndex');
            if (row) row.style.display = on ? 'flex' : 'none';
        }
        function toggleCopilotSupport(on) {
            const row = document.getElementById('rowCopilotSupport');
            if (row) row.style.display = on ? 'flex' : 'none';
        }
        function toggleCopilotUserAdditional(on) {
            const box = document.getElementById('copilotUserAdditionalBox');
            if (box) box.style.display = on ? 'block' : 'none';
        }

        function renderCopilotFormFromConfig(cop) {
            cop = cop || {};
            const setChecked = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
            setChecked('copilotForm', cop.Form !== false);
            setChecked('copilotUseList', !!cop.UseCopilotList);
            setChecked('copilotUseFormation', !!cop.UseFormation);
            const fi = document.getElementById('copilotFormationIndex');
            if (fi) fi.value = String(cop.SelectFormation || 1);
            const suppEl = document.getElementById('copilotSupport');
            if (suppEl) suppEl.value = String(cop.SupportUnitUsage === 3 ? 3 : 1);
            setChecked('copilotUseSupport', !!cop.UseSupportUnitUsage);
            setChecked('copilotIgnoreReq', !!cop.IgnoreRequirements);
            setChecked('copilotAddTrust', !!cop.AddTrust);
            setChecked('copilotAddUserAdditional', !!cop.AddUserAdditional);
            const loopEl = document.getElementById('copilotLoopTimes');
            if (loopEl) loopEl.value = cop.LoopTimes || 1;
            setChecked('copilotSanityPotion', !!cop.UseSanityPotion);
            CURRENT_COPILOT_USER_ADDITIONAL = Array.isArray(cop.UserAdditional) ? cop.UserAdditional : [];
            renderCopilotUserAdditional();
            // 联动
            onCopilotFormChange(cop.Form !== false);
            onCopilotUseListChange(!!cop.UseCopilotList);
            toggleCopilotUseFormation(!!cop.UseFormation);
            toggleCopilotSupport(!!cop.UseSupportUnitUsage);
            toggleCopilotUserAdditional(!!cop.AddUserAdditional);
        }

        function renderCopilotUserAdditional() {
            const container = document.getElementById('copilotUserAdditionalList');
            if (!container) return;
            if (!CURRENT_COPILOT_USER_ADDITIONAL || CURRENT_COPILOT_USER_ADDITIONAL.length === 0) {
                container.innerHTML = '<span style="font-size:10px;color:var(--claude-text-dim);">未追加自定干员</span>';
                return;
            }
            let html = '';
            CURRENT_COPILOT_USER_ADDITIONAL.forEach((op, idx) => {
                html += `
                <div style="display:flex;gap:6px;align-items:center;background:var(--claude-surface-inset);padding:4px 8px;border-radius:4px;">
                    <input type="text" class="form-input" style="flex:1;font-size:11px;margin-bottom:0;" placeholder="干员中文名 (例如 史尔特尔)" value="${escapeHtml(op.Name||'')}" onchange="CURRENT_COPILOT_USER_ADDITIONAL[${idx}].Name = this.value" />
                    <select style="font-size:11px;padding:2px 4px;margin-bottom:0;" onchange="CURRENT_COPILOT_USER_ADDITIONAL[${idx}].Skill = parseInt(this.value)">
                        <option value="1" ${op.Skill === 1 ? 'selected' : ''}>一技能</option>
                        <option value="2" ${op.Skill === 2 ? 'selected' : ''}>二技能</option>
                        <option value="3" ${op.Skill === 3 ? 'selected' : ''}>三技能</option>
                    </select>
                    <button type="button" class="btn-action" style="color:var(--claude-danger);padding:2px 6px;font-size:10px;" onclick="removeCopilotOperatorRow(${idx})">✕</button>
                </div>`;
            });
            container.innerHTML = html;
        }
        function addCopilotOperatorRow() {
            if (!CURRENT_COPILOT_USER_ADDITIONAL) CURRENT_COPILOT_USER_ADDITIONAL = [];
            CURRENT_COPILOT_USER_ADDITIONAL.push({ Name: "", Skill: 1 });
            renderCopilotUserAdditional();
        }
        function removeCopilotOperatorRow(idx) {
            if (!CURRENT_COPILOT_USER_ADDITIONAL) return;
            CURRENT_COPILOT_USER_ADDITIONAL.splice(idx, 1);
            renderCopilotUserAdditional();
        }

        function loadCopilotJobList() {
            return fetch('/api/maa/copilot/list')
                .then(r => r.json())
                .then(res => {
                    if (res.success && res.categories) {
                        const flat = [];
                        res.categories.forEach(cat => (cat.items || []).forEach(it => {
                            const exist = CURRENT_COPILOT_JOBS.find(j => j.filename === it.filename);
                            flat.push({
                                filename: it.filename,
                                title: it.title || it.filename,
                                details: it.details || '',
                                is_raid: exist ? !!exist.is_raid : false,
                                checked: exist ? !!exist.checked : true
                            });
                        }));
                        CURRENT_COPILOT_JOBS = flat;
                        renderCopilotJobList();
                    }
                    return res;
                }).catch(e => { console.warn('loadCopilot error:', e); return { success: false }; });
        }

        function renderCopilotJobList() {
            const box = document.getElementById('copilotJobListBox');
            const list = document.getElementById('copilotJobList');
            if (!box || !list) return;
            if (!CURRENT_COPILOT_JOBS.length) { box.style.display = 'none'; return; }
            box.style.display = 'block';
            let html = '';
            CURRENT_COPILOT_JOBS.forEach((j, idx) => {
                html += `
                <div style="display:flex;gap:6px;align-items:center;background:var(--claude-surface-inset);padding:5px 8px;border-radius:5px;border:1px solid var(--claude-border-subtle);">
                    <input type="checkbox" ${j.checked ? 'checked' : ''} onchange="CURRENT_COPILOT_JOBS[${idx}].checked = this.checked" />
                    <span style="flex:1;font-size:11px;color:var(--claude-text-main);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(j.details)}">${escapeHtml(j.title)}</span>
                    <label style="font-size:10px;color:var(--claude-text-muted);display:flex;align-items:center;gap:3px;cursor:pointer;"><input type="checkbox" ${j.is_raid ? 'checked' : ''} onchange="CURRENT_COPILOT_JOBS[${idx}].is_raid = this.checked" />突袭</label>
                    <button type="button" class="btn-action" style="padding:2px 6px;font-size:10px;color:var(--claude-danger);" onclick="removeCopilotJob(${idx})">✕</button>
                </div>`;
            });
            list.innerHTML = html;
        }
        function removeCopilotJob(idx) {
            CURRENT_COPILOT_JOBS.splice(idx, 1);
            renderCopilotJobList();
        }
        function clearCopilotJobs() {
            if (!confirm('确定清空当前作业列表吗？(服务端文件不受影响，可重新拉取)')) return;
            CURRENT_COPILOT_JOBS = [];
            renderCopilotJobList();
        }

        function triggerCopilotUpload() {
            const fi = document.getElementById('fileUploadCopilot');
            if (fi) fi.click();
        }
        function handleCopilotUpload(e) {
            const files = Array.from(e.target.files || []);
            if (!files.length) return;
            let pending = files.length;
            files.forEach(file => {
                const reader = new FileReader();
                reader.onload = (evt) => {
                    try {
                        const content = JSON.parse(evt.target.result);
                        fetch('/api/maa/copilot/upload', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ filename: file.name, content: content })
                        }).then(r => r.json()).then(res => {
                            if (!res.success) alert('上传失败 [' + file.name + ']: ' + res.error);
                            if (--pending === 0) {
                                alert('✔ 作业导入完成 (使用即生效，不落库)');
                                loadCopilotJobList();
                            }
                        }).catch(err => { alert('网络异常: ' + err); if (--pending === 0) loadCopilotJobList(); });
                    } catch(err) {
                        alert('作业 JSON 解析失败 [' + file.name + ']: ' + err);
                        if (--pending === 0) loadCopilotJobList();
                    }
                };
                reader.readAsText(file);
            });
            e.target.value = '';
        }

        function fetchCopilotByCode(btnEl) {
            const input = document.getElementById('copilotMysteryCode');
            const code = (input ? input.value : '').trim();
            if (!code) { alert('请输入作业站神秘代码 (纯数字 ID)'); return; }
            if (!/^\\d+$/.test(code)) { alert('神秘代码必须为纯数字 ID'); return; }
            const btn = btnEl || null;
            if (btn) btn.disabled = true;
            fetch('/api/maa/copilot/fetch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code })
            }).then(r => r.json()).then(res => {
                if (btn) btn.disabled = false;
                if (res.success) {
                    alert(`✔ 神秘代码 [${code}] 作业已导入：${res.title || res.filename}`);
                    if (input) input.value = '';
                    loadCopilotJobList();
                } else {
                    alert('拉取失败: ' + (res.error || res.message || '未知错误'));
                }
            }).catch(e => { if (btn) btn.disabled = false; alert('网络异常: ' + e); });
        }

        function triggerMaaQueueRun() {
            const acc = getCurrentAccount();
            if (!acc || !acc.id) { alert('请先选择托管账号'); return; }
            fetch('/api/maa/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ account_id: acc.id, mode: 'queue' })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert('✔ 一键长草任务已启动！可在左侧观察画面或在日志查看进度。');
                } else {
                    alert('启动失败: ' + (res.message || res.error));
                }
            }).catch(e => alert('启动异常: ' + e));
        }

        function triggerMaaCopilotRun() {
            const acc = getCurrentAccount();
            if (!acc || !acc.id) { alert('请先选择托管账号'); return; }

            const formOn = Boolean((document.getElementById('copilotForm') || {}).checked);
            const useList = Boolean((document.getElementById('copilotUseList') || {}).checked);
            const useFormation = Boolean((document.getElementById('copilotUseFormation') || {}).checked);
            const formationIndex = parseInt((document.getElementById('copilotFormationIndex') || {}).value || 1);
            const useSupport = Boolean((document.getElementById('copilotUseSupport') || {}).checked);
            const supportUsage = parseInt((document.getElementById('copilotSupport') || {}).value || 1);
            const addTrust = Boolean((document.getElementById('copilotAddTrust') || {}).checked);
            const addUser = Boolean((document.getElementById('copilotAddUserAdditional') || {}).checked);
            const ignoreReq = Boolean((document.getElementById('copilotIgnoreReq') || {}).checked);
            const loopTimes = parseInt((document.getElementById('copilotLoopTimes') || {}).value || 1);
            const usePot = Boolean((document.getElementById('copilotSanityPotion') || {}).checked);

            const checked = (CURRENT_COPILOT_JOBS || []).filter(j => j.checked);
            if (!checked.length) { alert('请先上传本地 JSON 或通过神秘代码导入作业，并至少勾选一个'); return; }

            let payload = {
                account_id: acc.id,
                mode: 'copilot',
                formation: formOn,
                use_copilot_list: useList,
                formation_index: useFormation ? formationIndex : 0,
                support_unit_usage: useSupport ? supportUsage : 0,
                add_trust: addTrust,
                ignore_requirements: ignoreReq,
                user_additional: addUser ? CURRENT_COPILOT_USER_ADDITIONAL : []
            };
            if (useList) {
                payload.copilot_list = checked.map(j => ({ filename: j.filename, stage_name: "", is_raid: !!j.is_raid }));
                payload.use_sanity_potion = usePot;
            } else {
                payload.copilot_file = checked[0].filename;
                payload.loop_times = loopTimes;
            }

            fetch('/api/maa/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`✔ Copilot 自动战斗已成功调度 (${useList ? checked.length + ' 个作业' : checked[0].title})`);
                } else {
                    alert('启动失败: ' + (res.message || res.error));
                }
            }).catch(e => alert('启动异常: ' + e));
        }

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopScreenPolling();
                stopStreamPlayer();
            } else {
                if (screenMode === '0.2fps' && isScreenTabActive()) {
                    refreshScreen(true);
                    startScreenPolling();
                } else if (screenMode === '30fps' && isScreenTabActive()) {
                    initStreamPlayer();
                }
            }
        });

            </script>
</body>
</html>
"""





# ==================== 30FPS H.264 硬件直通流媒体服务 ====================
# ==================== 30FPS H.264 硬件直通流媒体服务 (极速直传) ====================
class H264StreamServer:
    PRIVACY_TTL = 0.5  # 隐私/托管状态缓存有效期(秒)，避免每帧扫 /proc、每帧开 DB

    def __init__(self):
        self.clients = {}  # sock -> (username, role)
        self.lock = threading.Lock()
        self.proc = None
        self.running = False
        self._stop_evt = threading.Event()   # 停播信号(可中断 wait)，杜绝并发 worker
        self._worker_alive = False           # 是否已有存活采集线程(仅在 self.lock 下读写)
        self._privacy_lock = threading.Lock()
        self._privacy_cache = None           # (ts, running_owner, active_owner)
        self.cached_sps = None
        self.cached_pps = None
        self.cached_idr = None
        self.thread = None

    def add_client(self, sock, username="admin", role="admin"):
        start_worker = False
        with self.lock:
            self.clients[sock] = (username, role)
            if self.cached_sps and self.cached_pps and self.cached_idr:
                try:
                    self._send_frame(sock, self.cached_sps)
                    self._send_frame(sock, self.cached_pps)
                    self._send_frame(sock, self.cached_idr)
                except Exception:
                    pass
            # 仅当当前「没有存活采集线程」时才启动新线程。
            # 早期版本用 self.running 判定，而 worker 在 time.sleep(0.5) 期间 running
            # 可能已被 remove_client 置 False，客户端重连会再起一个线程，导致
            # 两个 screenrecord 并发抢同一个软件编码器 —— 帧率瞬间跌到 ~1fps。
            if not self._worker_alive:
                self._worker_alive = True
                self.running = True
                self._stop_evt.clear()
                start_worker = True
        if start_worker:
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()

    def remove_client(self, sock):
        proc = None
        with self.lock:
            self.clients.pop(sock, None)
            if not self.clients:
                self.running = False
                self._stop_evt.set()
                proc = self.proc
                self.proc = None
        if proc:
            self._terminate_proc(proc)

    def _terminate_proc(self, proc):
        """结束本地 adb 进程及其整个进程组，避免 adb shell 残留。"""
        if not proc:
            return
        try:
            if proc.poll() is not None:
                return
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass

    def _kill_remote_screenrecord(self):
        """兜底清理设备侧残留的 screenrecord。
        proc.terminate() 只杀本地 adb，设备侧 screenrecord 未必随之退出；残留会话会
        持续占用(软件)编码器，与新会话互相拖慢。"""
        try:
            subprocess.run([ADB, "-s", DEVICE, "shell", "pkill", "-l", "SIGINT", "screenrecord"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    def _get_privacy_snapshot(self):
        """带 TTL 的托管/隐私状态快照 (running_owner, active_owner)。
        每帧都扫 /proc (is_task_running) + 开 3 个 sqlite 连接会吃掉大量 CPU，
        直接把软编码器的算力挤掉；这里按 PRIVACY_TTL 缓存。"""
        now = time.time()
        with self._privacy_lock:
            c = self._privacy_cache
            if c and now - c[0] < self.PRIVACY_TTL:
                return c[1], c[2]
        running_info = get_running_task_info()
        running_owner = running_info.get("owner_username") if running_info else None
        active_owner = None
        if not running_info:
            try:
                acc = db.get_account(db.get_active_account_id())
                if acc:
                    active_owner = acc.get("owner_username")
            except Exception:
                pass
        with self._privacy_lock:
            self._privacy_cache = (now, running_owner, active_owner)
        return running_owner, active_owner

    def _send_frame(self, sock, data):
        length = len(data)
        hdr = bytearray([0x82])
        if length < 126:
            hdr.append(length)
        elif length < 65536:
            hdr.extend([126, (length >> 8) & 0xff, length & 0xff])
        else:
            hdr.extend([127] + list(length.to_bytes(8, "big")))
        sock.sendall(hdr + data)

    def _broadcast(self, data):
        dead = []
        running_owner, active_owner = self._get_privacy_snapshot()

        with self.lock:
            for s, (uname, role) in list(self.clients.items()):
                can_see = False
                if role == "admin":
                    can_see = True
                elif running_owner is not None:
                    if running_owner == uname:
                        can_see = True
                else:
                    if active_owner == uname:
                        can_see = True

                if not can_see:
                    continue

                try:
                    self._send_frame(s, data)
                except Exception:
                    dead.append(s)
            for d in dead:
                self.clients.pop(d, None)

    def _worker(self):
        print("[H264Stream] 启动 1280x720@30fps 硬件直通流媒体采集...", flush=True)
        try:
            while self.running:
                with self.lock:
                    if not self.clients:
                        break
                if not is_emulator_alive():
                    if self._stop_evt.wait(1):
                        break
                    continue
                # 启动前清掉设备侧可能残留的 screenrecord，避免多个软编会话并发
                self._kill_remote_screenrecord()
                # --time-limit 上限为 180s（Android 硬性限制，超出会直接报错不产出任何数据），
                # 到期由外层 while 循环自动重启，间隔约 0.5s。
                cmd = [ADB, "-s", DEVICE, "shell", "screenrecord", "--output-format=h264", "--size", "1280x720", "--bit-rate", "4000000", "--time-limit", "180", "-"]
                proc = None
                try:
                    # start_new_session：让 adb 及其 shell 子进程独立成组，便于整组回收
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
                    with self.lock:
                        self.proc = proc
                    buf = bytearray()
                    while self.running and proc.poll() is None:
                        with self.lock:
                            if not self.clients:
                                break
                        if not proc.stdout:
                            break
                        r, _, _ = select.select([proc.stdout], [], [], 0.04)
                        if r:
                            chunk = os.read(proc.stdout.fileno(), 16384)
                            if not chunk: break
                            buf.extend(chunk)

                        # 用 C 层 find 定位起始码，替代逐字节 Python 切片比较
                        # (大 I 帧可达数十 KB，逐字节扫描会吃掉大量 CPU，挤占软编码器)
                        starts = []
                        j = 0
                        while True:
                            j = buf.find(b"\x00\x00\x01", j)
                            if j < 0:
                                break
                            if j > 0 and buf[j-1] == 0:
                                starts.append((j - 1, 4))
                            else:
                                starts.append((j, 3))
                            j += 3

                        nalus = []
                        if len(starts) > 1:
                            for idx in range(len(starts) - 1):
                                p1, _ = starts[idx]
                                p2, _ = starts[idx+1]
                                nalus.append(bytes(buf[p1:p2]))
                            last_p, _ = starts[-1]
                            buf = buf[last_p:]
                        elif not r and buf and starts:
                            nalus.append(bytes(buf))
                            buf = bytearray()

                        for nalu in nalus:
                            slen = 4 if nalu.startswith(b"\x00\x00\x00\x01") else 3
                            ntype = nalu[slen] & 0x1f
                            if ntype == 7:
                                self.cached_sps = nalu
                            elif ntype == 8:
                                self.cached_pps = nalu
                            elif ntype == 5:
                                self.cached_idr = nalu
                            self._broadcast(nalu)
                except Exception as e:
                    print(f"[H264Stream] 异常: {e}", flush=True)
                finally:
                    if proc:
                        self._terminate_proc(proc)
                        self._kill_remote_screenrecord()
                    with self.lock:
                        if self.proc is proc:
                            self.proc = None
                if self._stop_evt.wait(0.5):
                    break
        finally:
            # 线程退出：原子清理存活标记与停播信号；若退出瞬间恰有新客户端连入，
            # 则原地再拉一个线程，避免出现「有客户端却无采集」的空窗。
            restart = False
            with self.lock:
                self._worker_alive = False
                self.running = False
                self.proc = None
                if self.clients:
                    self._worker_alive = True
                    self.running = True
                    self._stop_evt.clear()
                    restart = True
            if restart:
                self.thread = threading.Thread(target=self._worker, daemon=True)
                self.thread.start()
            else:
                self._stop_evt.clear()
                print("[H264Stream] 客户端全部离开，硬件直通流媒体休眠待命 (零算力消耗)。", flush=True)

STREAM_SERVER = H264StreamServer()

def read_ws_message(sock):
    try:
        r, _, _ = select.select([sock], [], [], 0.5)
        if not r:
            return True
        b = sock.recv(2)
        if len(b) < 2: return None
        b1, b2 = b[0], b[1]
        opcode = b1 & 0x0f
        if opcode == 0x08: return None
        masked = bool(b2 & 0x80)
        length = b2 & 0x7f
        if length == 126:
            ext = sock.recv(2)
            if len(ext) < 2: return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = sock.recv(8)
            if len(ext) < 8: return None
            length = int.from_bytes(ext, "big")
        mask = sock.recv(4) if masked else None
        data = bytearray()
        rem = length
        while rem > 0:
            c = sock.recv(min(rem, 65536))
            if not c: return None
            data.extend(c)
            rem -= len(c)
        if masked and mask:
            for i in range(len(data)):
                data[i] ^= mask[i % 4]
        if opcode == 0x01:
            try: return json.loads(data.decode("utf-8"))
            except Exception: return None
        elif opcode == 0x09:
            sock.sendall(bytearray([0x8a, 0x00]))
        return True
    except Exception:
        return None

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def get_current_user(self):
        # 1. 尝试从 Cookie 中解析
        cookies = self.headers.get("Cookie", "")
        for c in cookies.split(";"):
            if "ark_session=" in c:
                val = c.split("ark_session=")[1].strip().strip('"\'')
                user = verify_session_token(val)
                if user:
                    return user
                if val in ACTIVE_USER_SESSIONS:
                    return ACTIVE_USER_SESSIONS[val]

        # 2. 尝试从 URL query 参数中解析 (?token= 或 ?ark_token=)
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            url_tok = qs.get("token", [None])[0] or qs.get("ark_token", [None])[0]
            if url_tok:
                user = verify_session_token(url_tok.strip().strip('"\''))
                if user:
                    return user
        except Exception:
            pass

        # 3. 尝试从 Authorization Header 中解析
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            bearer_tok = auth_hdr[7:].strip()
            user = verify_session_token(bearer_tok)
            if user:
                return user

        return None

    def check_cookie_auth(self):
        return self.get_current_user() is not None

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(length) if length > 0 else b""

        if path == "/api/login":
            

            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                u = (body.get("username") or "").strip()
                pwd = (body.get("password") or "").strip()
                if not u or not pwd:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": "请输入用户名与密码"}).encode("utf-8"))
                    return
                pwd = body.get("password", "")
                ok, user_obj = scheduler_guard.authenticate_user(u, pwd)
                if ok:
                    u_role = user_obj.get("role", "user")
                    u_disp = user_obj.get("display_name") or user_obj.get("username")
                    token = create_session_token(u, u_role, u_disp)
                    ACTIVE_USER_SESSIONS[token] = user_obj
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"ark_session={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": True,
                        "token": token,
                        "user": u_disp,
                        "username": u,
                        "role": u_role
                    }).encode("utf-8"))
                    return
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": "用户名或密码错误"}).encode("utf-8"))
                    return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                return

        elif path == "/api/register":
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                u = body.get("username", "")
                pwd = body.get("password", "")
                invite = body.get("invite_code", "")
                ok, res = scheduler_guard.register_user(u, pwd, invite)
                if ok:
                    u_role = res.get("role", "user")
                    u_disp = res.get("display_name") or res.get("username")
                    token = create_session_token(u, u_role, u_disp)
                    ACTIVE_USER_SESSIONS[token] = res
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"ark_session={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(b'{"success": true}')
                    return
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(res)}).encode("utf-8"))
                    return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                return

        elif path == "/api/logout":
            cookie = self.headers.get("Cookie", "")
            if "ark_session=" in cookie:
                for item in cookie.split(";"):
                    if "ark_session=" in item:
                        t = item.split("=")[1].strip()
                        ACTIVE_USER_SESSIONS.pop(t, None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "ark_session=deleted; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
            self.wfile.write(b'{"success": true}')
            return

        elif not self.check_cookie_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        elif path == "/api/task/start":
            if is_task_running():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": false, "error": "Task already running"}')
                return
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = body.get("account_id", "")
                stage = body.get("stage", "daily")

                # 水平越权校验：普通用户绝不可触发他人账号任务
                target_acc = db.get_account(acc_id)
                if not target_acc:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Account not found"}')
                    return
                if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Forbidden: Not your account"}')
                    return

                subprocess.run(compose_cmd("up", "-d", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # 启动前清理孤儿残留，避免叠加触发 OOM 黑屏
                try:
                    reap_orphan_arknights_processes()
                except Exception:
                    pass
                # 统一走 ark_service_ctl.sh (一致的排他锁与超时保护); 无 ctl 时退回直调 runner
                ctl = BASE_DIR / "bin" / "ark_service_ctl.sh"
                if ctl.exists():
                    subprocess.Popen(["bash", str(ctl), "run-daily", acc_id])
                else:
                    subprocess.Popen(["python3", str(RUNNER_SCRIPT), stage, acc_id])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/task/stop":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.end_headers()
                    return
                running_info = get_running_task_info()
                if running_info and cur_user.get("role") != "admin":
                    if cur_user.get("username") != running_info.get("owner_username"):
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"success": false, "error": "Forbidden: Cannot stop other user task"}')
                        return
                stop_task_process()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/emulator/start":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.end_headers()
                    return
                # 权限：管理员 或 拥有账号的属主均可开启
                ok_perm = cur_user.get("role") == "admin"
                if not ok_perm:
                    owned = db.list_accounts(owner_username=cur_user.get("username"))
                    ok_perm = bool(owned)
                if not ok_perm:
                    self.send_json({"success": False, "error": "Forbidden: 无可用托管账号，无法开启模拟器"}, code=403)
                    return
                # 任务执行中：模拟器由任务链自行管理，避免与启停时序竞争
                if is_task_running():
                    self.send_json({"success": True, "message": "已有托管任务在执行，模拟器由任务链自动管理，无需手动开启。"})
                    return
                ok, msg = start_emulator_blocking()
                self.send_json({"success": ok, "message": msg})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/emulator/stop":
            try:
                cur_user = self.get_current_user()
                if not cur_user or cur_user.get("role") != "admin":
                    self.send_json({"success": False, "error": "Forbidden: Only admin can stop emulator"}, code=403)
                    return
                if is_task_running():
                    self.send_json({"success": False, "error": "托管任务正在执行中，请先「强制停止」任务后再停止模拟器。"}, code=409)
                    return
                ok, msg = stop_emulator_blocking()
                self.send_json({"success": ok, "message": msg})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/accounts/switch":
            try:
                cur_user = self.get_current_user()
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                target_id = body.get("account_id")
                acc = db.get_account(target_id)
                if acc and cur_user:
                    is_admin = cur_user.get("role") == "admin"
                    is_owner = acc.get("owner_username") == cur_user.get("username")
                    if is_admin or is_owner:
                        if is_admin:
                            # 管理员：维护全局激活账号 (可跨用户代管)
                            db.set_active_account_id(target_id)
                        else:
                            # 普通用户：仅维护本人维度的激活账号，与他人完全隔离
                            db.set_active_account_id(target_id, cur_user.get("username"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/admin/user/delete":
            cur_user = self.get_current_user()
            if not cur_user or cur_user.get("role") != "admin":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden"}')
                return
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
            target_user = body.get("username", "")
            ok, msg = scheduler_guard.delete_user_by_admin(target_user)
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "message": msg}).encode("utf-8"))
            return

        elif path == "/api/admin/invite_code":
            cur_user = self.get_current_user()
            if not cur_user or cur_user.get("role") != "admin":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden"}')
                return
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
            new_code = body.get("invite_code", "")
            ok, res = scheduler_guard.update_invite_code(new_code)
            if ok:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            else:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(res)}).encode("utf-8"))
            return

        elif path == "/api/notify/test":
            try:
                import notifier
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = body.get("account_id")
                accs = load_accounts()
                target_acc = next((a for a in accs.get("accounts", []) if a.get("id") == acc_id), None)
                if not target_acc:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Account not found"}')
                    return
                res = notifier.dispatch_account_notify(
                    target_acc,
                    subject="明日方舟测试通知",
                    markdown_content=f"### 明日方舟通信连通性测试\n- **账号**: {target_acc.get('name')}\n- **服务器**: {target_acc.get('platform', 'Official')}\n- **状态**: 凭据验证正常，通信链路畅通！",
                    event_type=None
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "results": res}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/accounts/update":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return
                body = json.loads(post_data.decode("utf-8"))
                updated_acc = body.get("account", body)
                acc_id = updated_acc.get("id")

                old_acc = db.get_account(acc_id)
                if not old_acc:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Account not found"}')
                    return

                # 越权安全校验：只有 admin 或本人可修改该账号
                if cur_user.get("role") != "admin" and old_acc.get("owner_username") != cur_user.get("username"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Forbidden: Not your account"}')
                    return

                # 系统不再保存任何游戏账密：强制清空旧字段，避免历史残留
                updated_acc.pop("game_password", None)

                # 归属不可被篡改：强制沿用原所有者
                updated_acc["owner_username"] = old_acc.get("owner_username", cur_user.get("username", DEFAULT_ADMIN_USER))
                # 保留运行期字段，避免被前端表单遗漏清空
                for keep in ("schedules", "created_at"):
                    if keep not in updated_acc and keep in old_acc:
                        updated_acc[keep] = old_acc[keep]
                db.save_account(updated_acc)
                scheduler_guard.sync_all_system_crontabs()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/accounts/delete":
            try:
                cur_user = self.get_current_user()
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = body.get("account_id")
                target = db.get_account(acc_id)
                if not target:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Account not found"}')
                    return

                if cur_user.get("role") != "admin" and target.get("owner_username") != cur_user.get("username"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Forbidden: Not your account"}')
                    return

                db.delete_account(acc_id)
                scheduler_guard.sync_all_system_crontabs()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/accounts/add":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Unauthorized"}')
                    return

                body = json.loads(post_data.decode("utf-8"))
                new_acc = body.get("account", {})
                new_acc["owner_username"] = cur_user.get("username", DEFAULT_ADMIN_USER)

                acc_id = new_acc.get("id") or ("acc_" + str(int(time.time() * 1000)))
                new_acc["id"] = acc_id

                db.save_account(new_acc)
                scheduler_guard.sync_all_system_crontabs()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/infrast_plans/delete":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                filename = os.path.basename(body.get("filename", ""))
                acc_id = body.get("account_id")
                if not filename.endswith(".json"):
                    filename += ".json"

                if acc_id:
                    d = get_account_infrast_dir(acc_id)
                    target = d / filename
                    if target.exists():
                        target.unlink()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/infrast_plans/upload":
            try:
                cur_user = self.get_current_user()
                if not cur_user:
                    self.send_response(401)
                    self.end_headers()
                    return

                body = json.loads(post_data.decode("utf-8"))
                filename = os.path.basename(body.get("filename", ""))
                content = body.get("content", {})
                acc_id = body.get("account_id")
                if not acc_id:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "account_id required"}')
                    return
                if not filename.endswith(".json"):
                    filename += ".json"

                # 每账号仅保留一个基建文件：先清空该账号目录内所有旧文件
                u_dir = get_account_infrast_dir(acc_id)
                for old in u_dir.glob("*.json"):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                target_path = u_dir / filename

                with open(target_path, "w", encoding="utf-8") as out:
                    json.dump(content, out, ensure_ascii=False, indent=2)

                plans_list = content.get("plans", [])
                prop_times = []
                for p in plans_list:
                    periods = p.get("period", [])
                    if periods and len(periods[0]) > 0:
                        prop_times.append(periods[0][0])

                conflict_warning = None
                target_acc = db.get_account(acc_id)
                if target_acc:
                    target_acc.setdefault("infrast", {})
                    target_acc["infrast"]["mode"] = "custom_plan"
                    target_acc["infrast"]["plan_file"] = filename
                    db.save_account(target_acc)
                    # 同步写入该账号 MAA 配置队列中的基建任务 Filename
                    try:
                        cfg_rec = db.get_maa_config(acc_id)
                        if cfg_rec and isinstance(cfg_rec.get("config"), dict):
                            for t in cfg_rec["config"].get("TaskQueue", []):
                                if t.get("$type") == "InfrastTask" and t.get("Mode") == "Custom":
                                    t["Filename"] = filename
                            db.save_maa_config(acc_id, target_acc.get("owner_username"), cfg_rec["config"], config_name=cfg_rec.get("config_name", "Default"))
                    except Exception:
                        pass

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True, 
                    "filename": filename,
                    "conflict_warning": conflict_warning,
                    "times": prop_times
                }).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            return

        elif path == "/api/maa/config":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = resolve_active_account_for_user(cur_user, body.get("account_id"))
                target_acc = db.get_account(acc_id)
                if not target_acc:
                    self.send_json({"success": False, "error": "Account not found"}, code=404)
                    return
                if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                    self.send_json({"success": False, "error": "Forbidden: Not your account"}, code=403)
                    return

                new_cfg = body.get("config", {})
                cfg_name = body.get("config_name", "Default")
                if not isinstance(new_cfg, dict):
                    self.send_json({"success": False, "error": "Invalid config format"}, code=400)
                    return

                # 服务端权威校验：理智作战关卡名合法性
                ok_stage, stage_err = maa_config_adapter.validate_stage_plan(new_cfg)
                if not ok_stage:
                    self.send_json({"success": False, "error": "invalid_stage", "message": stage_err}, code=400)
                    return

                db.save_maa_config(acc_id, target_acc.get("owner_username"), new_cfg, config_name=cfg_name)
                self.send_json({"success": True, "message": "MAA 桌面版配置已更新", "account_id": acc_id})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/maa/timers":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = resolve_active_account_for_user(cur_user, body.get("account_id"))
                target_acc = db.get_account(acc_id)
                if not target_acc:
                    self.send_json({"success": False, "error": "Account not found"}, code=404)
                    return
                if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                    self.send_json({"success": False, "error": "Forbidden: Not your account"}, code=403)
                    return

                new_timers = body.get("timers", {})
                window_min = int(body.get("window_minutes") or 40)
                if not isinstance(new_timers, dict):
                    self.send_json({"success": False, "error": "Invalid timers format"}, code=400)
                    return

                # 时段独占碰撞校验 (核心报错拦截：冲突禁止保存)
                all_existing = db.list_all_maa_timers()
                acc_names = {a["id"]: a["name"] for a in db.list_accounts()}
                is_valid, err_msg = maa_config_adapter.check_timers_conflict(
                    acc_id, new_timers, all_existing, window_minutes=window_min, acc_names_map=acc_names
                )
                if not is_valid:
                    self.send_json({
                        "success": False,
                        "error": "conflict",
                        "message": err_msg
                    }, code=409)
                    return

                db.save_maa_timers(acc_id, target_acc.get("owner_username"), new_timers, window_minutes=window_min)

                # 同步回写 account.schedules 以便 crontab 调度
                active_slots = []
                for t in new_timers.get("List", []):
                    if t.get("IsEnabled"):
                        h = int(t.get("Hour", 0))
                        m = int(t.get("Minute", 0))
                        active_slots.append(f"{h:02d}:{m:02d}")
                target_acc["schedules"] = active_slots
                db.save_account(target_acc)
                scheduler_guard.sync_all_system_crontabs()

                self.send_json({
                    "success": True,
                    "message": "定时执行配置已保存并同步至系统调度器",
                    "account_id": acc_id,
                    "active_slots": active_slots
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/maa/copilot/upload":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                fname = os.path.basename(body.get("filename", "").strip())
                if not fname:
                    self.send_json({"success": False, "error": "Filename is required"}, code=400)
                    return
                if not fname.endswith(".json"):
                    fname += ".json"

                content = body.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        self.send_json({"success": False, "error": "Invalid JSON content"}, code=400)
                        return

                uname = cur_user.get("username", DEFAULT_ADMIN_USER)
                u_dir = BASE_DIR / "data" / "copilot_uploads" / uname
                u_dir.mkdir(parents=True, exist_ok=True)
                target_file = u_dir / fname
                with open(target_file, "w", encoding="utf-8") as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)

                self.send_json({"success": True, "filename": fname, "message": "作业上传成功"})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/maa/copilot/fetch":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                code_val = str(body.get("code", "")).strip()
                if not code_val.isdigit():
                    self.send_json({"success": False, "error": "神秘代码必须为纯数字作业 ID"}, code=400)
                    return
                api_url = f"https://prts.maa.plus/copilot/get/{code_val}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (MAA-Web-Client)"})
                payload = None
                last_err = None
                for _attempt in range(3):
                    try:
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            payload = json.loads(resp.read().decode("utf-8"))
                        break
                    except Exception as net_err:
                        last_err = net_err
                        time.sleep(1.5)
                if payload is None:
                    self.send_json({"success": False, "error": f"作业站访问失败(已重试3次): {last_err}"}, code=502)
                    return

                if int(payload.get("status_code", 0)) != 200 or not payload.get("data"):
                    self.send_json({"success": False, "error": payload.get("message") or "神秘代码无效或作业不存在"}, code=404)
                    return

                data = payload["data"]
                content = data.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        self.send_json({"success": False, "error": "作业内容解析失败"}, code=502)
                        return
                if not isinstance(content, dict):
                    self.send_json({"success": False, "error": "作业内容为空"}, code=502)
                    return

                stage_name = str(content.get("stage_name", "")).strip() or f"copilot_{code_val}"
                safe_stage = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", stage_name)[:40]
                fname = f"code{code_val}_{safe_stage}.json"

                uname = cur_user.get("username", DEFAULT_ADMIN_USER)
                u_dir = BASE_DIR / "data" / "copilot_uploads" / uname
                u_dir.mkdir(parents=True, exist_ok=True)
                with open(u_dir / fname, "w", encoding="utf-8") as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)

                doc = content.get("doc", {}) if isinstance(content.get("doc"), dict) else {}
                self.send_json({
                    "success": True,
                    "filename": fname,
                    "title": doc.get("title") or stage_name,
                    "details": doc.get("details", ""),
                    "message": f"神秘代码 {code_val} 作业已拉取就绪"
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/maa/run":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            if is_task_running():
                self.send_json({"success": False, "error": "已有任务正在执行中，请稍候"}, code=409)
                return
            try:
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                acc_id = resolve_active_account_for_user(cur_user, body.get("account_id"))
                target_acc = db.get_account(acc_id)
                if not target_acc:
                    self.send_json({"success": False, "error": "Account not found"}, code=404)
                    return
                if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                    self.send_json({"success": False, "error": "Forbidden: Not your account"}, code=403)
                    return

                mode = body.get("mode", "queue")
                subprocess.run(compose_cmd("up", "-d", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # 启动前清理孤儿残留，避免叠加触发 OOM 黑屏
                try:
                    reap_orphan_arknights_processes()
                except Exception:
                    pass

                if mode == "copilot":
                    extra_data = {
                        "copilot_file": body.get("copilot_file", ""),
                        "copilot_list": body.get("copilot_list", []) or [],
                        "use_copilot_list": bool(body.get("use_copilot_list", False)),
                        "formation": bool(body.get("formation", True)),
                        "formation_index": int(body.get("formation_index", 0) or 0),
                        "support_unit_usage": int(body.get("support_unit_usage", 0)),
                        "add_trust": bool(body.get("add_trust", False)),
                        "ignore_requirements": bool(body.get("ignore_requirements", False)),
                        "user_additional": body.get("user_additional", []),
                        "loop_times": body.get("loop_times", 1),
                        "use_sanity_potion": bool(body.get("use_sanity_potion", False))
                    }
                    extra_json = json.dumps(extra_data, ensure_ascii=False)
                    subprocess.Popen(["python3", str(RUNNER_SCRIPT), "copilot", acc_id, extra_json])
                else:
                    ctl = BASE_DIR / "bin" / "ark_service_ctl.sh"
                    if ctl.exists():
                        subprocess.Popen(["bash", str(ctl), "run-daily", acc_id])
                    else:
                        subprocess.Popen(["python3", str(RUNNER_SCRIPT), "daily", acc_id])

                self.send_json({"success": True, "message": f"任务已成功调度启动 ({mode})"})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if self.headers.get("Upgrade", "").lower() == "websocket" or path.startswith("/ws/"):
            cur_user = self.get_current_user()
            if not cur_user:
                token_param = params.get("token", [""])[0]
                if token_param:
                    cur_user = verify_session_token(token_param)
            if not cur_user:
                self.send_response(401)
                self.end_headers()
                return

            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_response(400)
                self.end_headers()
                return

            magic = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept_val = base64.b64encode(hashlib.sha1(key.encode() + magic).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_val)
            self.end_headers()

            sock = self.request
            sock.setblocking(True)
            STREAM_SERVER.add_client(sock, cur_user.get("username", "admin"), cur_user.get("role", "user"))
            try:
                while True:
                    m = read_ws_message(sock)
                    if m is None:
                        break
                    # 仅显示模式：忽略任何客户端误发或恶意伪造的触控点击事件，防止误触
                    if isinstance(m, dict):
                        pass
            except Exception:
                pass
            finally:
                STREAM_SERVER.remove_client(sock)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/jmuxer.min.js":
            jm_file = os.path.join(BASE_DIR, "jmuxer.min.js")
            if os.path.exists(jm_file):
                with open(jm_file, "rb") as f:
                    c = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(c)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(c)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        if path == "/" or path == "/index.html":
            if not self.check_cookie_auth():
                encoded_login = LOGIN_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_login)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded_login)
                return
            else:
                cur_user = self.get_current_user()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                
                # 若 URL 带有 token，在 HTTP 响应头把 Cookie 种下（兼容任意网络与协议）
                parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                url_tok = parsed_qs.get("token", [None])[0] or parsed_qs.get("ark_token", [None])[0]
                if url_tok and verify_session_token(url_tok):
                    self.send_header("Set-Cookie", f"ark_session={url_tok}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")

                cur_token = ""
                cookies = self.headers.get("Cookie", "")
                for c in cookies.split(";"):
                    if "ark_session=" in c:
                        cur_token = c.split("ark_session=")[1].strip().strip('"\'')
                        break
                if not cur_token and url_tok:
                    cur_token = url_tok

                page_html = APP_PAGE.replace('let CURRENT_SESSION_TOKEN = "";', f'let CURRENT_SESSION_TOKEN = "{cur_token}";')
                if cur_user and cur_user.get("role") == "admin":
                    page_html = page_html.replace('id="tabBtnAdmin" style="display: none;"', 'id="tabBtnAdmin" style="display: inline-block;"')
                    u_disp = cur_user.get("display_name") or cur_user.get("username")
                    page_html = page_html.replace('id="current-user-tag">--', f'id="current-user-tag">{u_disp} 👑')
                encoded_app = page_html.encode("utf-8")
                self.send_header("Content-Length", str(len(encoded_app)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded_app)
                return

        if path == "/api/auth_check":
            user = self.get_current_user()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if user:
                self.wfile.write(json.dumps({"authenticated": True, "user": user.get("display_name") or user.get("username"), "username": user.get("username"), "role": user.get("role")}).encode("utf-8"))
            else:
                self.wfile.write(b'{"authenticated": false}')
            return

        elif path == "/api/admin/overview":
            cur_user = self.get_current_user()
            if not cur_user or cur_user.get("role") != "admin":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden"}')
                return
            overview = scheduler_guard.get_users_overview()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(overview).encode("utf-8"))
            return

        elif path == "/api/admin/invite_code":
            cur_user = self.get_current_user()
            if not cur_user or cur_user.get("role") != "admin":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden"}')
                return
            code = scheduler_guard.get_current_invite_code()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"invite_code": code}).encode("utf-8"))
            return

        elif path == "/api/scheduler/timeline":
            cur_user = self.get_current_user()
            slots = scheduler_guard.get_all_occupied_slots()
            is_admin = (cur_user and cur_user.get("role") == "admin")
            my_uname = cur_user.get("username") if cur_user else ""
            
            # 严格脱敏隔离：非管理员用户只能看到自身账号名称；其他用户的账号名、用户ID一律脱敏为“系统任务占用”
            safe_slots = []
            for s in slots:
                s_copy = dict(s)
                if not is_admin and s.get("owner_username") != my_uname:
                    s_copy["account_name"] = "系统任务占用"
                    s_copy["owner_username"] = ""
                    s_copy["account_id"] = ""
                    s_copy["mode_label"] = "已占用"
                safe_slots.append(s_copy)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"slots": safe_slots}).encode("utf-8"))
            return

        elif path == "/api/depot_full":
            cur_user = self.get_current_user()
            depot_data = {}
            if cur_user and DEPOT_FILE.exists():
                try:
                    # 普通用户只有在当前正处于本人账号任务且归属本人时才下发全量扫描缓存
                    running_task = get_running_task_info()
                    task_owner = running_task.get("owner_username") if running_task else DEFAULT_ADMIN_USER
                    if cur_user.get("role") == "admin" or cur_user.get("username") == task_owner:
                        with open(DEPOT_FILE, "r", encoding="utf-8") as df:
                            depot_data = json.load(df)
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(depot_data).encode("utf-8"))
            return

        if not self.check_cookie_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        if path == "/api/task/status":
            running = is_task_running()
            info = get_running_task_info() if running else None
            payload = {"running": running}
            if info:
                payload["account_id"] = info.get("account_id", "")
                payload["account_name"] = info.get("account_name", "")
                payload["owner_username"] = info.get("owner_username", "")
                payload["started_at"] = info.get("timestamp", 0)
                payload["current_task"] = get_current_taskchain()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        elif path == "/api/maa/config":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            acc_id = resolve_active_account_for_user(cur_user, params.get("account_id", [""])[0])
            target_acc = db.get_account(acc_id)
            if not target_acc:
                self.send_json({"success": False, "error": "Account not found"}, code=404)
                return
            if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                self.send_json({"success": False, "error": "Forbidden: Not your account"}, code=403)
                return

            cfg_rec = db.get_maa_config(acc_id)
            if not cfg_rec or not cfg_rec.get("config"):
                def_cfg = maa_config_adapter.get_default_maa_config(target_acc.get("account_name", ""))
                db.save_maa_config(acc_id, target_acc.get("owner_username"), def_cfg)
                cfg_rec = {"account_id": acc_id, "config": def_cfg, "config_name": "Default"}
            else:
                # 自动进行无损结构升级迁移
                migrated = maa_config_adapter.migrate_legacy_config(cfg_rec.get("config", {}), target_acc.get("account_name", ""))
                cfg_rec["config"] = migrated

            # 附带该账号可用自定义基建排班文件 (账号维度) 与芯片掉落选项
            try:
                infra_files = list_infrast_plans(acc_id)
            except Exception:
                infra_files = []

            self.send_json({
                "success": True,
                "account_id": acc_id,
                "config_name": cfg_rec.get("config_name", "Default"),
                "config": cfg_rec.get("config", {}),
                "task_schema": getattr(maa_config_adapter, "TASK_SCHEMA", {}),
                "task_catalog": getattr(maa_config_adapter, "TASK_CATALOG", []),
                "infrast_files": infra_files,
                "drop_stage_map": getattr(maa_config_adapter, "DROP_STAGE_MAP", {}),
                "chip_drop_options": getattr(maa_config_adapter, "CHIP_DROP_OPTIONS", [])
            })
            return

        elif path == "/api/maa/timers":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            acc_id = resolve_active_account_for_user(cur_user, params.get("account_id", [""])[0])
            target_acc = db.get_account(acc_id)
            if not target_acc:
                self.send_json({"success": False, "error": "Account not found"}, code=404)
                return
            if cur_user.get("role") != "admin" and target_acc.get("owner_username") != cur_user.get("username"):
                self.send_json({"success": False, "error": "Forbidden: Not your account"}, code=403)
                return

            timers_rec = db.get_maa_timers(acc_id)
            if not timers_rec or not timers_rec.get("timers"):
                def_timers = maa_config_adapter.get_default_maa_timers()
                db.save_maa_timers(acc_id, target_acc.get("owner_username"), def_timers, window_minutes=40)
                timers_rec = {"account_id": acc_id, "timers": def_timers, "window_minutes": 40}

            # 汇总全系统所有已占用的 40 分钟独占时段
            all_records = db.list_all_maa_timers()
            all_slots = []
            for r in all_records:
                r_acc_id = r.get("account_id")
                r_dur = int(r.get("window_minutes") or 40)
                r_name = r.get("account_name") or r_acc_id
                t_list = (r.get("timers") or {}).get("List", [])
                for item in t_list:
                    if item.get("IsEnabled"):
                        h = int(item.get("Hour", 0))
                        m = int(item.get("Minute", 0))
                        is_mine = (r_acc_id == acc_id)
                        all_slots.append({
                            "account_id": r_acc_id if is_mine or cur_user.get("role") == "admin" else "",
                            "account_name": r_name if is_mine or cur_user.get("role") == "admin" else "其他账号",
                            "is_current": is_mine,
                            "hour": h,
                            "minute": m,
                            "window_minutes": r_dur,
                            "remark": item.get("Remark", "")
                        })

            self.send_json({
                "success": True,
                "account_id": acc_id,
                "window_minutes": int(timers_rec.get("window_minutes") or 40),
                "timers": timers_rec.get("timers", {}),
                "all_slots": all_slots
            })
            return

        elif path == "/api/maa/copilot/list":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            uname = cur_user.get("username", DEFAULT_ADMIN_USER)
            categories = get_copilot_jobs_list(uname)
            self.send_json({"success": True, "categories": categories})
            return

        elif path == "/api/maa/stages":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_json({"success": False, "error": "Unauthorized"}, code=401)
                return
            try:
                # 对齐 MAA 桌面版：精选常驻关卡 + 在线活动关卡(随官方 API 更新)
                acc_id = params.get("account_id", [""])[0] or db.get_active_account_id()
                acc = db.get_account(acc_id) or {}
                client_type = "Bilibili" if acc.get("platform") == "Bilibili" else "Official"
                catalog = maa_config_adapter.get_stage_catalog(client_type=client_type, include_activity=True)
                self.send_json({"success": True, "categories": catalog.get("categories", [])})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, code=500)
            return

        elif path == "/api/emulator_status":
            alive = is_emulator_alive()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"alive": alive}).encode("utf-8"))

        elif path == "/api/card_logs":
            cur_user = self.get_current_user()
            uname = cur_user.get("username") if (cur_user and cur_user.get("role") != "admin") else None
            cards = get_today_card_logs(owner_username=uname)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"cards": cards}).encode("utf-8"))

        elif path == "/api/log_today":
            cur_user = self.get_current_user()
            uname = cur_user.get("username") if (cur_user and cur_user.get("role") != "admin") else None
            logs = get_today_logs(owner_username=uname, limit=250)
            human_logs = get_today_human_logs(limit=100)
            cleanup_old_logs(days=30)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"logs": logs, "human_logs": human_logs}).encode("utf-8"))

        elif path == "/debug_img":
            cat = params.get("cat", [""])[0]
            fname = urllib.parse.unquote(params.get("file", [""])[0])
            if cat in ["drops", "interface"] and fname:
                img_p = DEBUG_DIR / cat / os.path.basename(fname)
                if img_p.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    with open(img_p, "rb") as f:
                        self.wfile.write(f.read())
                    return
            self.send_response(404)
            self.end_headers()

        elif path == "/api/infrast_plan_info":
            fname = urllib.parse.unquote(params.get("file", [""])[0])
            _cu = self.get_current_user()
            acc_id = resolve_active_account_for_user(_cu, params.get("account_id", [""])[0])
            info = parse_infrast_schedule_info(fname, account_id=acc_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(info).encode("utf-8"))

        elif path == "/api/infrast_plans":
            _cu = self.get_current_user()
            acc_id = resolve_active_account_for_user(_cu, params.get("account_id", [""])[0])
            plans = list_infrast_plans(account_id=acc_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(plans).encode("utf-8"))

        elif path == "/api/accounts":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_response(401)
                self.end_headers()
                return

            if cur_user.get("role") != "admin":
                # 普通用户严格只查询自身名下的账号，绝不可查询或触碰管理员账号！
                raw_accs = db.list_accounts(owner_username=cur_user.get("username"))
                active_id = db.get_active_account_id(owner_username=cur_user.get("username"))
            else:
                raw_accs = db.list_accounts()
                active_id = db.get_active_account_id()

            safe_accounts = []
            for a in raw_accs:
                a_copy = dict(a)
                # 系统不再保存/下发任何游戏账密，彻底剥离该字段
                a_copy.pop("game_password", None)
                safe_accounts.append(a_copy)

            out_data = {
                "active_account_id": active_id,
                "accounts": safe_accounts
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out_data).encode("utf-8"))

        elif path == "/screencap":
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_response(401)
                self.end_headers()
                return

            is_admin = bool(cur_user.get("role") == "admin")
            running_info = get_running_task_info()

            if not is_admin:
                if running_info:
                    # 正在跑其他人的任务：绝不调用底层截图！
                    if cur_user.get("username") != running_info.get("owner_username"):
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "privacy_shield"}')
                        return
                else:
                    # 模拟器空闲：仅当当前活动账号属于本人时允许查看，否则展示隐私遮罩
                    cur_acc = db.get_account(db.get_active_account_id())
                    if not (cur_acc and cur_acc.get("owner_username") == cur_user.get("username")):
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "privacy_shield"}')
                        return

            if not is_emulator_alive():
                self.send_response(503)
                self.end_headers()
                return
            res = adb_cmd(["exec-out", "screencap", "-p"])
            if res.returncode == 0 and len(res.stdout) > 0:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(res.stdout)
            else:
                self.send_response(503)
                self.end_headers()

        elif path in ("/tap", "/swipe", "/text", "/key"):
            cur_user = self.get_current_user()
            if not cur_user:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            is_admin = bool(cur_user.get("role") == "admin")
            running_info = get_running_task_info()
            can_touch = False

            if is_admin:
                can_touch = True
            elif running_info:
                # 任务运行中：仅任务发起人本人可触控
                if running_info.get("owner_username") == cur_user.get("username"):
                    can_touch = True
            else:
                # 模拟器空闲：若活动账号属于本人，放行触控与文字注入 (用于手动输入账密首次登录)
                cur_acc = db.get_account(db.get_active_account_id())
                if cur_acc and cur_acc.get("owner_username") == cur_user.get("username"):
                    can_touch = True

            if not can_touch:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "forbidden", "message": "其他账号正在运行或占用中，触控已安全锁定"}).encode("utf-8"))
                return
            if path == "/tap":
                x = params.get("x", ["0"])[0]
                y = params.get("y", ["0"])[0]
                adb_cmd(["shell", "input", "tap", str(x), str(y)])
            elif path == "/swipe":
                x1 = params.get("x1", ["0"])[0]
                y1 = params.get("y1", ["0"])[0]
                x2 = params.get("x2", ["0"])[0]
                y2 = params.get("y2", ["0"])[0]
                duration = params.get("duration", ["400"])[0]
                adb_cmd(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)])
            elif path == "/text":
                val = params.get("val", [""])[0]
                safe_val = val.replace(" ", "%s").replace("&", "\\&")
                adb_cmd(["shell", "input", "text", safe_val])
            elif path == "/key":
                code = params.get("code", ["0"])[0]
                adb_cmd(["shell", "input", "keyevent", str(code)])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        else:
            self.send_response(404)
            self.end_headers()

def _iter_arknights_procs():
    """
    只读列出方舟相关进程，返回 [(pid, ppid, name, comm)]。
    通过 /proc/<pid>/comm 取真实线程名 (shell 权限即可读)：
      - 主进程 comm 形如 'gryph.arknights' (以 '.arknights' 结尾)
      - Unity 派生的残留子进程 comm 形如 'sm-thread-http1/2'
      - ':GP7Service' 等独立进程 comm 形如 'ghts:GP7Service'
    """
    procs = []
    try:
        shell_cmd = (
            "ps -A -o PID,PPID,NAME | grep -E 'com.hypergryph.arknights|com.bilibili.arknights' | "
            "while read pid ppid name; do comm=$(cat /proc/$pid/comm 2>/dev/null); "
            "echo \"$pid $ppid $name $comm\"; done"
        )
        res = subprocess.run(
            [ADB, "-s", DEVICE, "shell", shell_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
                comm = parts[3] if len(parts) >= 4 else ""
                procs.append((int(parts[0]), int(parts[1]), parts[2], comm))
    except Exception:
        pass
    return procs

def _is_main_proc(comm):
    """主进程判定：comm 以 '.arknights' 结尾 (如 gryph.arknights / bili.arknights)。"""
    return comm.endswith(".arknights")

def _classify_arknights_procs(procs):
    """
    将方舟「裸名进程」分类 (带 ':' 后缀的 :GP7Service/:GP7Worker/:pushcore 一律不动):
      - mains:   主进程 (comm 以 '.arknights' 结尾)，正常恰好 1 个
      - leaked:  PPID==1 的 Unity 残留子进程 (comm 非主进程，如 sm-thread-http*)，可安全清理；
                 这些是主进程被 MAA/守护重启后 reparent 到 init 的泄漏，各占约 1GB。
    关键：不以 PPID==1 单独判定 —— 主进程即便因 zygote 重启变为 PPID==1 也一律保留，
    因此手动态/正在运行的实例永不被误杀。
    """
    bare_names = ("com.hypergryph.arknights", "com.bilibili.arknights")
    mains, leaked = [], []
    for pid, ppid, name, comm in procs:
        if name not in bare_names:
            continue  # 独立子进程 (:GP7Service 等) 不参与回收
        if _is_main_proc(comm):
            mains.append((pid, ppid, name, comm))
        elif ppid == 1:
            leaked.append((pid, ppid, name, comm))
    return mains, leaked

def _module_ensure_adb_root(cache={"t": 0.0}):
    """
    确保 adb 处于 root (redroid 容器重启后会丢失 root，导致 kill 静默失败)。
    至少每 5 分钟校验一次，避免频繁 root 触发 adbd 重连。
    """
    now = time.time()
    if now - cache["t"] < 300:
        return
    try:
        out = subprocess.run([ADB, "-s", DEVICE, "shell", "id"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=6).stdout
        if "uid=0" not in out:
            subprocess.run([ADB, "-s", DEVICE, "root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            time.sleep(1.5)
            subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
        cache["t"] = now
    except Exception:
        pass

def reap_orphan_arknights_processes():
    """
    精准的进程健康巡检 (绝不干扰手动登录/正在运行的实例)：
      1) 清理 PPID==1 的 Unity 残留子进程 (comm 非主进程，如 sm-thread-http*)；
      2) 重复「主进程」(comm 以 '.arknights' 结尾) 叠加时，保留 PID 最大(最新)的一个，
         其余连同其 worker 子进程一并清理。
    主进程本身永不被作为孤儿清理 —— 手动态绝不受影响。
    """
    procs = _iter_arknights_procs()
    if not procs:
        return 0
    mains, leaked = _classify_arknights_procs(procs)
    if not leaked and len(mains) <= 1:
        return 0
    _module_ensure_adb_root()
    killed = 0

    def _kill(pid):
        nonlocal killed
        try:
            subprocess.run([ADB, "-s", DEVICE, "shell", "kill", "-9", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            killed += 1
        except Exception:
            pass

    # 1) 清理 Unity 残留子进程 (PPID==1 且非主进程)
    for pid, _ppid, _name, _comm in leaked:
        _kill(pid)

    # 2) 重复主进程治理 (保留最新，连同旧主进程的 worker 一起清)
    if len(mains) > 1:
        mains.sort(key=lambda x: x[0])  # PID 升序，最新在最后
        keep_pid = mains[-1][0]
        stale_pids = {m[0] for m in mains[:-1]}
        print(f"[Reaper] 检测到重复方舟主进程 {len(mains)} 个 (保留最新 PID={keep_pid})，已清理旧副本及其子进程。", flush=True)
        for pid, _ppid, _name, _comm in mains[:-1]:
            _kill(pid)
        # 旧主进程的 worker 子进程 (PPID 为旧主进程 PID，或已 reparent)
        for pid, ppid, _name, _comm in procs:
            if ppid in stale_pids and not _is_main_proc(_comm):
                _kill(pid)

    if killed:
        print(f"[Reaper] 已清理方舟残留子进程/重复主进程 {killed} 个 (防 OOM)。", flush=True)
    return killed

def start_orphan_reaper():
    def _loop():
        # 启动时先清理一次，避免历史残留叠加
        try:
            reap_orphan_arknights_processes()
        except Exception:
            pass
        while True:
            time.sleep(10)
            try:
                reap_orphan_arknights_processes()
            except Exception:
                pass
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    start_orphan_reaper()
    with ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"ArkWeb control panel listening on 0.0.0.0:{PORT}...")
        httpd.serve_forever()