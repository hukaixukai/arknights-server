import os
import sys
import secrets
import pathlib

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
BIN_DIR = BASE_DIR / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import http.server
import socketserver
import subprocess
import urllib.parse
import hashlib
import time
import hmac
import base64

import db
import scheduler_guard

DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")
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

import json
import os
import secrets
import signal
import sys
import time
import datetime
import pathlib
import re

PORT = int(os.environ.get("PORT", 8090))
ADB = os.getenv("ADB_BIN", "adb")
DEVICE = os.getenv("ADB_TARGET", "127.0.0.1:5555")

CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
INFRAST_DIR = BASE_DIR / "maa" / "infrast"
DEPOT_FILE = BASE_DIR / "maa" / "data" / "DepotData.json"
DEBUG_DIR = BASE_DIR / "maa" / "debug"
LOG_FILE = DEBUG_DIR / "asst.log"
RUNNER_SCRIPT = BASE_DIR / "bin" / "maa_runner.py"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"

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

def get_running_task_info():
    task = db.get_running_task()
    if task and task.get("running") and is_task_running():
        return task
    if is_task_running():
        acc = db.get_account(db.get_active_account_id())
        if acc:
            return {
                "running": True,
                "account_id": acc.get("id"),
                "account_name": acc.get("name"),
                "owner_username": acc.get("owner_username", DEFAULT_ADMIN_USER)
            }
    return None

def is_task_running():
    try:
        for pid_dir in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                cmd = (pid_dir / "cmdline").read_bytes()
                if b"maa_runner.py" in cmd:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    try:
        out = subprocess.check_output(["pgrep", "-f", "maa_runner.py"], text=True)
        return bool(out.strip())
    except Exception:
        return False

def stop_task_process():
    stopped = False
    try:
        for pid_dir in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                cmd = (pid_dir / "cmdline").read_bytes()
                if b"maa_runner.py" in cmd:
                    pid = int(pid_dir.name)
                    os.kill(pid, signal.SIGKILL)
                    stopped = True
            except Exception:
                continue
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-9", "-f", "maa_runner.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

def get_user_infrast_dir(username: str = None) -> pathlib.Path:
    uname = (username or DEFAULT_ADMIN_USER).strip()
    u_dir = INFRAST_DIR / uname
    u_dir.mkdir(parents=True, exist_ok=True)
    return u_dir

def list_infrast_plans(owner_username: str = None):
    plans = set()
    u_dir = get_user_infrast_dir(owner_username)
    if u_dir.exists():
        for p in u_dir.glob("*.json"):
            plans.add(p.name)
    # 仅超级管理员可查阅管理员专属私有排班
    if owner_username == DEFAULT_ADMIN_USER or not owner_username:
        h_dir = INFRAST_DIR / DEFAULT_ADMIN_USER
        if h_dir.exists():
            for p in h_dir.glob("*.json"):
                plans.add(p.name)
    return sorted(list(plans))

def parse_infrast_schedule_info(filename, owner_username: str = None):
    filename = os.path.basename(filename)
    if not filename.endswith(".json"):
        filename += ".json"
    
    u_dir = get_user_infrast_dir(owner_username)
    p = u_dir / filename
    if not p.exists():
        # 仅超级管理员可查阅管理员专属私有排班，严禁其他普通用户穿透借读
        if owner_username == DEFAULT_ADMIN_USER or not owner_username:
            p = INFRAST_DIR / DEFAULT_ADMIN_USER / filename
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
        <style>
        /* 1. 默认夜间主题: Anthropic Claude 暖石炭官方风格 */
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
        #modal-new-account, #modal-rename-account, #modal-img-viewer {
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
        #modal-new-account.show, #modal-rename-account.show, #modal-img-viewer.show {
            display: flex !important;
        }

        [data-theme="light"] #modal-new-account,
        [data-theme="light"] #modal-rename-account,
        [data-theme="light"] #modal-img-viewer {
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

        #screen {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
            cursor: default;
            user-select: none;
            -webkit-user-drag: none;
        }

        .screen-offline-placeholder {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 24px;
            background: rgba(18, 18, 16, 0.95);
            width: 100%;
            height: 100%;
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
            background: #0d0d0c;
            border: 1px solid var(--claude-border);
            border-radius: var(--radius-md);
            padding: 10px;
            font-size: 11px;
            line-height: 1.5;
            color: #81c784;
            max-height: 140px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
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
            <label style="font-size: 12px; color: var(--claude-text-muted);">明日方舟游戏账号</label>
            <input type="text" id="newAccAccount" class="form-input" placeholder="例如 13800000000" oninput="autoSuggestName(this.value)" />
            <label style="font-size: 12px; color: var(--claude-text-muted);">明日方舟游戏登录密码</label>
            <div style="display: flex; gap: 6px; align-items: center;">
                <input type="password" id="newAccGamePwd" class="form-input" placeholder="输入游戏登录密码..." autocomplete="current-password" style="margin-bottom: 0;" />
                <button type="button" class="btn-action" style="padding: 8px 10px; font-size: 11px; white-space: nowrap;" onclick="togglePwdVisibility('newAccGamePwd')">👁 显隐</button>
            </div>
            <label style="font-size: 12px; color: var(--claude-text-muted);">账号备注名</label>
            <input type="text" id="newAccName" class="form-input" placeholder="例如 官服·尾号1234" />
            <label style="font-size: 12px; color: var(--claude-text-muted);">客户端服务器平台</label>
            <select id="newAccPlatform" class="form-input">
                <option value="Official">官方服</option>
                <option value="Bilibili">Bilibili服</option>
            </select>
            <label style="font-size: 12px; color: var(--claude-text-muted);">托管与排班模式</label>
            <select id="newAccInfrastMode" class="form-input" onchange="toggleNewModalInfrastMode(this.value)">
                <option value="custom_plan">📋 绑定单一基建排班文件</option>
                <option value="daily_once">🔄 一天一登轮换模式</option>
            </select>

            <div id="new-modal-area-plan">
                <label style="font-size: 12px; color: var(--claude-text-muted);">基建排班预设</label>
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 4px;">
                    <select id="newAccInfrast" class="form-input" style="margin-bottom: 0; flex: 1; font-weight: 500;"></select>
                    <button type="button" class="btn-action" style="padding: 7px 12px; font-size: 12px; white-space: nowrap; background: rgba(217, 119, 69, 0.15); color: var(--claude-clay); border-color: var(--claude-clay);" onclick="triggerInfrastUploadForNewModal()">📥 导入新排班</button>
                </div>
                <div style="font-size: 11px; color: var(--claude-text-dim); margin-top: 2px; margin-bottom: 12px;">说明：支持 MAA 官方排班 JSON 格式，导入后自动识别班次。</div>
            </div>

            <div id="new-modal-area-daily" style="display: none;">
                <label style="font-size: 12px; color: var(--claude-text-muted);">无人机加速对象</label>
                <select id="newAccDronesTarget" class="form-input" style="margin-bottom: 8px;">
                    <option value="Money">龙门币</option>
                    <option value="BattleRecord">经验书</option>
                    <option value="Orundum">合成玉</option>
                    <option value="OriginiumShard">源石碎片</option>
                    <option value="PureGold">赤金</option>
                </select>
                <label style="font-size: 12px; color: var(--claude-text-muted);">每日单次运行时间</label>
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 4px;">
                    <input type="time" id="newAccDailyTime" class="form-input" value="16:00" style="width: 140px; text-align: center; font-size: 13px; font-weight: bold; margin-bottom: 0;" onchange="updateNewModalDailyTime(this.value)" />
                    <span id="new-modal-exclusive-badge" class="slot-pill" style="font-weight: bold;">16:00 ~ 16:59 独占</span>
                </div>
                <div style="font-size: 11px; color: var(--claude-text-dim); margin-top: 2px; margin-bottom: 12px;">说明：每天在设定时间执行 1 次全自动任务，独占 59 分钟，完成后休眠。</div>
            </div>
            <div style="display: flex; gap: 10px; margin-top: 8px;">
                <button class="btn-primary" onclick="submitNewAccount()">确认创建账号</button>
                <button class="btn-action" style="flex: 1;" onclick="closeNewAccountModal()">取消</button>
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

    <!-- 导航切换 -->
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('screen-tab')">🎮 实时交互投屏</button>
        <button class="tab-btn" onclick="switchTab('config-tab')">⚙️ 托管账号策略</button>
        <button class="tab-btn" onclick="switchTab('account-tab')">👥 托管账号管理</button>
        <button class="tab-btn" onclick="switchTab('data-tab')">📊 物资与运行看板</button>
        <button class="tab-btn" id="tabBtnAdmin" style="display: none;" onclick="switchTab('admin-tab')">👑 管理员面板</button>
    </div>

    <!-- TAB 1: 实时交互投屏与看板 (左栏投屏等比放大至 62%，右栏 38%，底端严格对齐) -->
    <div id="screen-tab" class="tab-content active">
        <div class="screen-split-layout">
            <!-- 左栏: 放大投屏大屏与控制 -->
            <div class="left-stream-col">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <button type="button" class="btn-action" id="btnToggleScreenPoll" onclick="toggleScreenPolling()" style="padding: 5px 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 4px;">▶ 开启画面轮询 5秒</button>
                        <button type="button" class="btn-action" onclick="refreshScreen(true)" style="padding: 5px 10px; font-size: 12px;">📸 手动抓拍一次</button>
                    </div>
                    <div style="display: flex; align-items: center; gap: 6px; font-size: 12px;">
                        <span id="screen-poll-status" class="badge" style="background: rgba(100,116,139,0.15); color: #94a3b8;">已暂停轮询 零资源占用</span>
                    </div>
                </div>
                <div class="screen-card" id="container">
                    <img id="screen" src="/screencap" alt="游戏画面加载中..." style="pointer-events: none; user-select: none; cursor: default;" />
                    <div class="screen-offline-placeholder" id="screen-offline" style="display: none;">
                        <h3>⏸ 模拟器休眠待命中</h3>
                        <p>当前模拟器已停止以释放硬件资源。<br>可在右侧点击立即运行日常唤醒执行。</p>
                    </div>
                    <div class="screen-offline-placeholder" id="screen-privacy" style="display: none; background: rgba(24, 24, 22, 0.95);">
                        <h3 style="color: var(--claude-clay);">🔒 隐私保护屏蔽待命中</h3>
                        <p>当前系统正在执行其他受邀用户的托管任务。<br>为保障用户隐私与账号安全，非本人账号执行期间画面已脱敏屏蔽。</p>
                    </div>
                </div>
                </div>

            <!-- 右栏: 仿 MAA GUI 实时图文执行看板 (高度 100%，与左栏底端平齐) -->
            <div class="dashboard-panel">
                <div class="dash-control-bar">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <span style="font-size: 12px; color: var(--claude-text-muted);">运行控制:</span>
                            <span id="task-status-badge" style="font-size: 13px; font-weight: 600; margin-left: 6px; color: var(--claude-text-main);">检查中...</span>
                        </div>
                        <div style="display: flex; gap: 8px;">
                            <button class="dash-btn-run" id="btnRunDaily" onclick="triggerTaskRun()">▶ 立即运行日常</button>
                            <button class="dash-btn-stop" id="btnStopTask" onclick="triggerTaskStop()">⏸ 强制停止任务</button>
                            <button class="dash-btn-close-emu" onclick="triggerCloseEmu()">⏹ 停止模拟器</button>
                        </div>
                    </div>
                </div>

                <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; margin-bottom: 8px;">
                    <span style="color: var(--claude-clay); font-weight: 600;">📄 实时运行终端日志</span>
                    <button type="button" class="btn-action" style="padding: 2px 8px; font-size: 11px;" onclick="loadTodayLogs()">⟳ 刷新日志</button>
                </div>
                <div class="log-terminal" id="logTerminal" style="flex: 1; min-height: 480px; overflow-y: auto;">正在读取今日运行日志...</div>
            </div>
        </div>
    </div>

    <!-- TAB 2: 独立策略配置 -->
    <div id="config-tab" class="tab-content">
        <div class="grid-2">
            <div class="card">
                <div class="card-header">
                    <span>基建排班与关卡作战</span>
                </div>
                <div class="form-row">
                    <span>基建排班与托管模式</span>
                    <select id="cfgInfrastMode" onchange="toggleInfrastModeUI(this.value)">
                        <option value="custom_plan">📋 绑定单一基建排班文件</option>
                        <option value="daily_once">🔄 一天一登轮换模式</option>
                    </select>
                </div>
                <div id="area-custom-plan">
                    <div class="form-row">
                        <span>当前绑定的基建排班文件</span>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <select id="cfgInfrastPlan" onchange="loadInfrastPlanInfo(this.value)" style="font-weight: 600;"></select>
                            <button type="button" class="btn-action" style="padding: 4px 10px; font-size: 12px; background: rgba(217, 119, 69, 0.15); color: var(--claude-clay); border-color: var(--claude-clay); white-space: nowrap;" onclick="triggerInfrastUpload()">📥 导入新排班替换</button>
                            <button type="button" class="btn-action" style="padding: 4px 10px; font-size: 12px; color: var(--claude-danger); border-color: rgba(217, 48, 37, 0.35); background: rgba(217, 48, 37, 0.08); white-space: nowrap;" onclick="deleteCurrentInfrastPlan()">🗑️ 删除此排班</button>
                        </div>
                    </div>
                    <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border); border-radius: 8px; padding: 12px; margin: 10px 0;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-size: 12px; font-weight: 600; color: var(--claude-clay);">⏱ 排班表内置执行班次</span>
                            <span id="plan-title-tag" style="font-size: 11px; color: var(--claude-text-dim);"></span>
                        </div>
                        <div id="plan-schedules-container" style="display: flex; flex-direction: column; gap: 6px;"></div>
                    </div>
                </div>
                <div id="area-auto-rotation" style="display: none;">
                    <div class="form-row">
                        <span>无人机加速对象</span>
                        <select id="cfgDronesTarget">
                            <option value="Money">龙门币</option>
                            <option value="BattleRecord">经验书</option>
                            <option value="Orundum">合成玉</option>
                            <option value="OriginiumShard">源石碎片</option>
                            <option value="PureGold">赤金</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span>每日单次运行时间</span>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <input type="time" id="cfgDailySingleTime" class="form-input" style="width: 130px; text-align: center; font-size: 14px; font-weight: bold;" onchange="updateDailySingleTimeUI(this.value)" />
                        </div>
                    </div>
                    <!-- 一天一登 59 分钟独占时段卡片 -->
                    <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border); border-radius: 8px; padding: 12px; margin: 10px 0;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-size: 12px; font-weight: 600; color: var(--claude-clay);">⏱ 每日单次独占时段</span>
                            <span id="daily-conflict-status" class="badge" style="background: rgba(40,167,69,0.15); color: #28a745;">检查中...</span>
                        </div>
                        <div style="display: flex; flex-direction: column; gap: 6px; font-size: 12px;">
                            <div style="display: flex; justify-content: space-between; align-items: center; background: var(--claude-surface); padding: 8px 10px; border-radius: 6px; border: 1px solid var(--claude-border-subtle);">
                                <div>
                                    <span style="color: var(--claude-text-muted);">计划执行时刻:</span>
                                    <b id="daily-run-time-display" style="font-family: var(--font-mono); margin-left: 6px; color: var(--claude-text-main);">16:00</b>
                                </div>
                                <div>
                                    <span style="color: var(--claude-text-muted);">59 分钟独占窗口:</span>
                                    <span id="daily-exclusive-window-display" class="slot-pill" style="font-weight: bold; margin-left: 6px;">16:00 ~ 16:59</span>
                                </div>
                            </div>
                            <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--claude-text-dim); padding: 0 4px;">
                                <span>前序最晚登录: <b id="daily-prev-safe" style="color: var(--claude-text-muted);">15:00 止</b></span>
                                <span>后续最早登录: <b id="daily-next-safe" style="color: var(--claude-text-muted);">17:00 起</b></span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 24小时全天时段分布甘特轴与冲突监控 -->
                <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border); border-radius: var(--radius-md); padding: 14px; margin: 12px 0;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;">
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span style="font-size: 13px; font-weight: 600; color: var(--claude-clay);">🕒 全天 24 小时排班时段分布</span>
                            <span class="badge" style="font-size: 10px; background: rgba(217, 119, 69, 0.15); color: var(--claude-clay);">59 分钟独占保护</span>
                        </div>
                        <div style="font-size: 11px; color: var(--claude-text-muted);">
                            <span style="display: inline-block; width: 8px; height: 8px; background: var(--claude-clay); border-radius: 2px; margin-right: 4px;"></span>当前账号
                            <span style="display: inline-block; width: 8px; height: 8px; background: #64748b; border-radius: 2px; margin-left: 8px; margin-right: 4px;"></span>其他账号
                        </div>
                    </div>

                    <!-- 24小时甘特条 -->
                    <div id="timeline-bar-container" style="position: relative; height: 32px; background: var(--claude-surface); border: 1px solid var(--claude-border); border-radius: 6px; overflow: hidden; margin-bottom: 6px;">
                        <div style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex;">
                            <div style="flex: 1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                            <div style="flex: 1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                            <div style="flex: 1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                            <div style="flex: 1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                            <div style="flex: 1; border-right: 1px dashed var(--claude-border-subtle);"></div>
                            <div style="flex: 1;"></div>
                        </div>
                        <div id="timeline-blocks" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"></div>
                    </div>
                    <!-- 刻度标签 -->
                    <div style="display: flex; justify-content: space-between; font-size: 10px; color: var(--claude-text-dim); margin-bottom: 10px; padding: 0 2px;">
                        <span>00:00</span>
                        <span>04:00</span>
                        <span>08:00</span>
                        <span>12:00</span>
                        <span>16:00</span>
                        <span>20:00</span>
                        <span>24:00</span>
                    </div>

                    <div id="timeline-slot-details" style="display: flex; flex-direction: column; gap: 6px; font-size: 11px;"></div>
                    
                    <div style="margin-top: 10px; padding: 8px 10px; background: rgba(0,0,0,0.18); border-radius: 6px; font-size: 11px; color: var(--claude-text-muted); line-height: 1.5;">
                        🔒 <b>单模拟器防冲突规则</b>：每个账号登录后独占 59 分钟。任意两次登录之间必须间隔至少 60 分钟例如 10:00 登录占用至 10:59，其他账号最晚 09:00 登录，最早 11:00 登录。。导入基建表或修改时间时系统将自动校验。
                    </div>
                </div>

                <div class="form-row">
                    <span>作战模式策略</span>
                    <select id="cfgFightMode" onchange="toggleFightModeUI(this.value)">
                        <option value="daily_depot_maintain">日常清智与芯片作战</option>
                        <option value="manual_stage">主刷指定活动关卡</option>
                    </select>
                </div>
                <div class="form-row" id="rowPrimaryStage">
                    <span>主刷活动关卡名称</span>
                    <input type="text" id="cfgManualStage" placeholder="例如 CW-10" style="width: 140px; text-align: center;" />
                </div>
                <div class="form-row">
                    <span>明日方舟游戏账号</span>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <span id="cfgGameAccountDisplay" style="font-family: monospace; font-size: 13px; font-weight: 600; color: var(--claude-text-main); background: var(--claude-surface-inset); padding: 4px 10px; border-radius: var(--radius-md); border: 1px solid var(--claude-border);">******1234</span>
                        <input type="text" id="cfgGameAccount" class="form-input" placeholder="输入游戏手机号/UID..." style="width: 140px; text-align: center; display: none;" />
                        <button type="button" class="btn-action" style="padding: 3px 8px; font-size: 11px;" id="btnToggleAccEdit" onclick="toggleAccountEdit()">✏️ 修改</button>
                    </div>
                </div>
                <div class="form-row">
                    <span>明日方舟游戏登录密码</span>
                    <div style="display: flex; gap: 6px; align-items: center;">
                        <input type="password" id="cfgGamePassword" class="form-input" placeholder="输入登录密码..." style="width: 140px; text-align: center;" />
                        <button type="button" class="btn-action" style="padding: 3px 8px; font-size: 11px;" onclick="togglePwdVisibility('cfgGamePassword')">👁 显隐</button>
                    </div>
                </div>

                <button class="btn-primary" style="margin-top: 14px;" onclick="saveCurrentAccountConfig()">保存当前账号策略更改</button>
            </div>

            <!-- 右栏: 包含公招策略卡片 与 理智药退出策略卡片 -->
            <div style="display: flex; flex-direction: column; gap: 16px;">
                <!-- 卡片 2: 公招策略与日常限额 -->
                <div class="card">
                    <div class="card-header">
                        <span>公招策略与日常限额</span>
                        <span id="recruit-today-badge" class="badge" style="background: rgba(40,167,69,0.15); color: #28a745;">今日已消耗: 0 / 4 张</span>
                    </div>
                    <div class="form-row">
                        <span>每日公招上限 (张/天)</span>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <input type="number" id="cfgRecruitDailyLimit" min="1" max="12" class="form-input" style="width: 100px; text-align: center; font-weight: bold;" value="4" />
                            <span style="font-size: 11px; color: var(--claude-text-muted);">张 (建议 1~8 张)</span>
                        </div>
                    </div>
                    <div class="form-row">
                        <span>公招六星高级资深策略</span>
                        <select id="cfg6StarStrategy">
                            <option value="notify_user">保留标签并推送通知 (等待人工确认)</option>
                            <option value="unowned_first">优先锁定未获取干员 (全自动锁定)</option>
                        </select>
                    </div>
                    <div style="background: var(--claude-surface-inset); border: 1px solid var(--claude-border); border-radius: 8px; padding: 10px 12px; margin-top: 8px; font-size: 11px; color: var(--claude-text-muted); line-height: 1.5;">
                        💡 <b>公招策略机制</b>：<br>
                        • <b>刷新优先</b>：每次运行优先无条件消耗 3 次刷新机会寻找高星干员，刷新不占用公招券额度。<br>
                        • <b>严禁用加急券</b>：作为日常托管，本系统严格禁止使用加急许可，仅依靠自然时间轮转。<br>
                        • <b>按日智能限额</b>：每日 04:00 自动重置计数。单次登录最多开启 min(4, 剩余配额) 个新槽位，达到上限后仅收干员不再扣券。
                    </div>
                </div>

                <!-- 卡片 3: 理智药、周常与退出行为 -->
                <div class="card">
                    <div class="card-header">
                        <span>理智药、周常与退出行为</span>
                    </div>
                    <div class="form-row">
                        <span>临期理智药消耗策略</span>
                        <select id="cfgExpiringMed">
                            <option value="true">临期 2 天内自动使用</option>
                            <option value="false">不使用</option>
                        </select>
                    </div>

                    <div class="form-row">
                        <span>每周一自动清空剿灭</span>
                        <select id="cfgAnnihilationMed">
                            <option value="true">允许吃药补足</option>
                            <option value="false">仅自然理智</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span>任务完成退出动作</span>
                        <select id="cfgOnComplete">
                            <option value="stop_emu">退出游戏并休眠模拟器</option>
                            <option value="close_game">仅退出游戏客户端</option>
                            <option value="keep_running">保持运行</option>
                        </select>
                    </div>

                    <!-- 账号独立通信渠道设置 -->
                    <div style="border-top: 1px solid var(--claude-border-subtle); padding-top: 12px; margin-top: 12px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                            <span style="font-size: 13px; font-weight: 600; color: var(--claude-clay);">🔔 独立通信渠道设置</span>
                            <button class="btn-action" style="padding: 2px 10px; font-size: 11px;" onclick="testNotifyConfig()">📨 测试当前通知</button>
                        </div>
                        <div style="font-size: 11px; color: var(--claude-text-muted); margin-bottom: 8px;">
                            支持 WxPusher 微信消息与 SMTP 邮箱授权码，每个账号配置独立隔离
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

                        <div id="notify-wx-box" style="margin-top: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; border: 1px solid var(--claude-border-subtle); display: none;">
                            <div style="font-size: 12px; font-weight: 600; color: var(--claude-text-main); margin-bottom: 4px;">WxPusher 配置</div>
                            <label style="font-size: 11px; color: var(--claude-text-dim);">应用 AppToken (支持 SPT_ 极简Token 或 AT_ 标准Token)</label>
                            <input type="text" id="cfgWxToken" class="form-input" placeholder="留空使用默认，或填 SPT_xxxx / AT_xxxx" />
                            <label style="font-size: 11px; color: var(--claude-text-dim);">目标接收 UID (仅 AT_ 时需要，逗号隔开；SPT_留空)</label>
                            <input type="text" id="cfgWxUids" class="form-input" placeholder="例如: UID_a1b2c3d4e5" />
                        </div>

                        <div id="notify-email-box" style="margin-top: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; border: 1px solid var(--claude-border-subtle); display: none;">
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
                        <span>24 小时已占用 59 分钟时段</span>
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
            refreshScreen(true);
            loadTaskStatus();
            
            loadTodayLogs();
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
                const leftPct = (startMin / 1440 * 100).toFixed(2);
                const widthPct = (59 / 1440 * 100).toFixed(2);

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
                blk.title = `【${slot.account_name}】${slot.range_str} (独占59分钟)`;
                container.appendChild(blk);

                const tagColor = isCur ? "color: var(--claude-clay); font-weight: 600;" : "color: var(--claude-text-main);";
                detailHtml += `
                    <div style="display: flex; justify-content: space-between; align-items: center; background: var(--claude-surface); padding: 5px 10px; border-radius: 6px; border: 1px solid var(--claude-border-subtle);">
                        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                            <span class="slot-pill" style="${isCur ? "" : "background: rgba(100,116,139,0.15); color: #94a3b8; border-color: rgba(100,116,139,0.3);"}">⏱ ${slot.range_str}</span>
                            <span class="badge" style="font-size: 10px;">${slot.mode_label || (slot.mode === 'daily_once' ? '一天一登' : '排班表')}</span>
                            <span style="font-size: 11px; ${tagColor}">${escapeHtml(slot.account_name)}</span>
                            ${isCur ? '<span class="badge" style="background: rgba(217,119,69,0.15); color: var(--claude-clay); padding: 1px 5px; font-size: 10px;">当前账号</span>' : ""}
                        </div>
                        <span style="font-size: 10px; color: var(--claude-text-dim);">独占 59 分钟，前后间隔需大于 60 分钟</span>
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
                safeSetText("stat-total-slots", totalSlots + " 个时段");

                renderAdminUsersTable(data.users || []);
            }).catch(e => console.warn("loadAdminOverview error:", e));
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
                            <th>全天独占时段</th>
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
                        <div style="margin-bottom: 6px;">
                            <b>${escapeHtml(a.name)}</b> 
                            <span style="font-family: monospace; color: var(--claude-text-dim);">(${a.account_name_masked})</span>
                            <span style="font-size: 11px; color: ${a.has_password ? "var(--claude-sage)" : "var(--claude-text-dim)"};">
                                ${a.has_password ? " · 已存密" : " · 无密码"}
                            </span>
                            <div style="font-size: 10px; color: var(--claude-text-muted);">
                                模式: ${a.infrast_mode === "daily_once" ? "一天一登 (" + a.daily_single_time + ")" : "排班表 " + (a.plan_file || "默认") + ")"}
                            </div>
                        </div>
                    `).join("");

                    slotsHtml = u.accounts.map(a => {
                        if (!a.occupied_slots || a.occupied_slots.length === 0) return "<span style='color: var(--claude-text-dim);'>无生效时段</span>";
                        return `<div style="display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px;">
                            ${a.occupied_slots.map(s => `<span class="slot-pill">⏱ ${s}</span>`).join("")}
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
            fetch('/api/accounts/switch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account_id: accId})
            }).then(r => r.json()).then(res => {
                GLOBAL_ACCOUNTS_DATA.active_account_id = accId;
                renderHeaderAccounts();
                renderAccountsList();
                renderCurrentAccountDetails();
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
                safeSetVal('cfgGamePassword', acc.game_password || '');
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
                        </div>
                        <div style="font-size: 12px; color: var(--claude-text-muted);">
                            游戏账号: <span style="font-family: monospace; color: var(--claude-text-main);">${acc.account_name || '未填'}</span> · 
                            密码状态: <span style="color: ${acc.game_password ? 'var(--claude-sage)' : 'var(--claude-text-dim)'};">${acc.game_password ? '已保存密码' : '未存密码'}</span> · 
                            托管模式: ${modeDisplay}
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
            acc.infrast.dorm_not_stationed_enabled = true;
            acc.infrast.fiammetta_recovery_enabled = true;

            acc.fight = acc.fight || {};
            acc.fight.mode = getElVal('cfgFightMode', 'daily_depot_maintain');
            acc.fight.manual_stage = getElVal('cfgManualStage', 'CW-10').trim() || 'CW-10';
            acc.account_name = getElVal('cfgGameAccount', acc.account_name || '').trim();
            acc.game_password = getElVal('cfgGamePassword', '').trim();
            acc.fight.use_expiring_medicine = (getElVal('cfgExpiringMed', 'true') === 'true');
            acc.fight.annihilation_use_medicine = (getElVal('cfgAnnihilationMed', 'true') === 'true');

            acc.on_complete = getElVal('cfgOnComplete', 'stop_emu');

            acc.recruit = acc.recruit || {};
            acc.recruit.daily_limit = parseInt(getElVal('cfgRecruitDailyLimit', '4')) || 4;
            acc.recruit.select_6star_strategy = getElVal('cfg6StarStrategy', 'notify_user');
            acc.recruit.enabled = true;

            acc.notify = acc.notify || {};
            acc.notify.channel = getElVal('cfgNotifyChannel', 'none');
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

        function testNotifyConfig() {
            const acc = getCurrentAccount();
            if (!acc.id) return;
            const ch = document.getElementById('cfgNotifyChannel').value;
            if (ch === 'none') {
                alert('请先将通知方式切换为 WxPusher 或 邮箱，再进行测试发送！');
                return;
            }
            if (!confirm(`将使用当前界面填写的凭据向账号 ${acc.name} 发送测试消息，确定吗？`)) return;
            
            // 先临时提交当前通知配置
            saveCurrentConfig();
            
            fetch('/api/notify/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account_id: acc.id})
            }).then(r => r.json()).then(res => {
                alert('测试推送结果: ' + JSON.stringify(res.results || res));
            }).catch(e => {
                alert('请求异常: ' + e);
            });
        }

        function submitNewAccount() {
            const name = document.getElementById('newAccName').value.trim();
            const account_name = document.getElementById('newAccAccount').value.trim();
            const plan_file = document.getElementById('newAccInfrast').value;
            const platform = document.getElementById('newAccPlatform').value;
            const game_pwd = document.getElementById('newAccGamePwd').value.trim();

            if (!name || !account_name) {
                alert('请填写完整账号备注名和游戏账号！');
                return;
            }

            const infMode = document.getElementById('newAccInfrastMode') ? document.getElementById('newAccInfrastMode').value : 'custom_plan';
            const dailyTime = document.getElementById('newAccDailyTime') ? document.getElementById('newAccDailyTime').value : '16:00';
            const effectiveSchedules = (infMode === 'daily_once') ? [dailyTime] : ['10:00', '16:00', '22:00'];

            const newAcc = {
                id: 'acc_' + Date.now(),
                name: name,
                account_name: account_name,
                game_password: game_pwd,
                platform: platform || "Official",
                enabled: true,
                owner_username: (typeof CURRENT_USER !== 'undefined' && CURRENT_USER.username) ? CURRENT_USER.username : 'admin',
                schedules: effectiveSchedules,
                infrast: { 
                    mode: infMode, 
                    plan_file: plan_file || "default_plan.json", 
                    drones: "Money", 
                    threshold: 50, 
                    dorm_not_stationed_enabled: true, 
                    daily_single_time: dailyTime 
                },
                fight: { mode: "daily_depot_maintain", manual_stage: "CW-10", fallback_stage: "1-7", series: 0, use_expiring_medicine: true, medicine_expire_days: 2, use_medicine: false, use_stone: false, annihilation_monday: true, annihilation_use_medicine: true },
                recruit: { enabled: true, times: 3, select_6star_strategy: "unowned_first", refresh_level3: true },
                mall: { enabled: true, buy_first: ["赤金", "招聘许可", "龙门币"], blacklist: ["碳", "家具", "加急许可"], reserve_max_credit: true },
                award: { enabled: true, mail: true, free_gacha: true, orundum: true, mining: true, special_access: true },
                on_complete: "stop_emu",
                inventory: { "合成玉": 0, "至纯源石": 0, "龙门币": 0, "固源岩": 0, "源石碎片": 0, "last_updated": "未初始化" }
            };

            fetch('/api/accounts/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({account: newAcc})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    alert(`新托管账号 ${name} 创建成功！`);
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
        function loadTaskStatus() {
            fetch('/api/task/status').then(r => r.json()).then(st => {
                const badge = document.getElementById('task-status-badge');
                const btnDaily = document.getElementById('btnRunDaily');
                const btnStop = document.getElementById('btnStopTask');
                const stageBtns = document.querySelectorAll('.stage-run-btn');

                if (st.running) {
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

        // --- 今日运行终端日志渲染 ---
        function loadTodayLogs() {
            const term = document.getElementById('logTerminal');
            if (!term) return;
            fetch('/api/log_today').then(r => r.json()).then(data => {
                const logs = (data && data.logs) ? data.logs : [];
                if (!logs.length) {
                    term.innerHTML = '<span style="color: var(--claude-text-dim);">今日暂无运行日志。任务开始或切换账号后将自动刷新。</span>';
                    return;
                }
                term.innerHTML = logs.map(l => {
                    let color = '#81c784';
                    if (/\\[WAR\\]|WARN/i.test(l)) color = '#ffb74d';
                    else if (/\\[ERR\\]|ERROR|失败|异常/i.test(l)) color = '#e57373';
                    else if (/TaskChainCompleted|Success/i.test(l)) color = '#4fc3f7';
                    return `<span style="color:${color};">${escapeHtml(l)}</span>`;
                }).join(String.fromCharCode(10));
                term.scrollTop = term.scrollHeight;
            }).catch(e => console.warn('loadTodayLogs error:', e));
        }

        // --- 顶部导航 Tab 切换控制 ---
        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(tc => {
                if (tc.id === tabId) { tc.classList.add('active'); } else { tc.classList.remove('active'); }
            });
            document.querySelectorAll('.tab-btn').forEach(btn => {
                const oc = btn.getAttribute('onclick') || '';
                if (oc.indexOf("'" + tabId + "'") !== -1) { btn.classList.add('active'); } else { btn.classList.remove('active'); }
            });

            if (tabId === 'screen-tab') {
                refreshScreen(true);
                if (isScreenPollingActive && !document.hidden) {
                    startScreenPolling();
                }
            } else {
                stopScreenPolling();
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

        function triggerCloseEmu() {
            if (!confirm('确定停止模拟器容器并释放 4GB+ 内存与 GPU 资源吗？')) return;
            fetch('/api/emulator/stop', {method: 'POST'}).then(r => r.json()).then(res => {
                if (res.success) {
                    alert('模拟器已停止，硬件资源已完全释放。');
                    refreshScreen(true);
                    loadTaskStatus();
                } else {
                    alert('停止失败: ' + (res.error || ''));
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
        let startX = 0, startY = 0, isMouseDown = false, isRefreshing = false;
        let screenPollTimer = null;
        let isScreenPollingActive = false; // 默认关闭长期截图

        function toggleScreenPolling() {
            isScreenPollingActive = !isScreenPollingActive;
            updateScreenPollUI();
            if (isScreenPollingActive) {
                refreshScreen(true);
                startScreenPolling();
            } else {
                stopScreenPolling();
            }
        }

        function updateScreenPollUI() {
            const btn = document.getElementById('btnToggleScreenPoll');
            const status = document.getElementById('screen-poll-status');
            if (isScreenPollingActive) {
                if (btn) {
                    btn.innerText = '⏸ 暂停画面轮询';
                    btn.style.background = 'rgba(217, 119, 69, 0.15)';
                    btn.style.color = 'var(--claude-clay)';
                    btn.style.borderColor = 'var(--claude-clay)';
                }
                if (status) {
                    status.innerText = '🟢 实时轮询中 5秒间隔';
                    status.style.background = 'rgba(40, 167, 69, 0.15)';
                    status.style.color = '#28a745';
                }
            } else {
                if (btn) {
                    btn.innerText = '▶ 开启画面轮询 5秒';
                    btn.style.background = 'var(--claude-surface)';
                    btn.style.color = 'var(--claude-text-main)';
                    btn.style.borderColor = 'var(--claude-border)';
                }
                if (status) {
                    status.innerText = '已暂停轮询 零资源占用';
                    status.style.background = 'rgba(100,116,139,0.15)';
                    status.style.color = '#94a3b8';
                }
            }
        }

        function startScreenPolling() {
            if (screenPollTimer) clearInterval(screenPollTimer);
            // 精准 5 秒截一张
            screenPollTimer = setInterval(() => {
                refreshScreen(false);
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
            const tab = document.getElementById('screen-tab');
            if (!force && (document.hidden || !tab || !tab.classList.contains('active') || !isScreenPollingActive)) {
                return;
            }
            isRefreshing = true;

            fetch('/screencap?t=' + Date.now()).then(r => {
                isRefreshing = false;
                if (r.status === 403) {
                    // 触发多用户隐私屏蔽
                    img.style.display = 'none';
                    if (offlinePlaceholder) offlinePlaceholder.style.display = 'none';
                    if (privacyPlaceholder) privacyPlaceholder.style.display = 'flex';
                    return null;
                }
                if (r.status === 200) {
                    return r.blob();
                }
                throw new Error('Offline');
            }).then(blob => {
                if (!blob) return;
                const objectUrl = URL.createObjectURL(blob);
                img.style.display = 'block';
                if (offlinePlaceholder) offlinePlaceholder.style.display = 'none';
                if (privacyPlaceholder) privacyPlaceholder.style.display = 'none';
                img.src = objectUrl;
            }).catch(() => {
                img.style.display = 'none';
                if (offlinePlaceholder) offlinePlaceholder.style.display = 'flex';
                if (privacyPlaceholder) privacyPlaceholder.style.display = 'none';
            });
        }

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                // 用户没看这个界面了就直接关停轮询，零后台消耗
                stopScreenPolling();
            } else {
                if (isScreenPollingActive) {
                    refreshScreen(true);
                    startScreenPolling();
                }
            }
        });

        function getCoords(e) {
            const rect = img.getBoundingClientRect();
            const clientX = e.clientX || (e.touches && e.touches[0].clientX);
            const clientY = e.clientY || (e.touches && e.touches[0].clientY);
            const x = Math.round((clientX - rect.left) / rect.width * 1280);
            const y = Math.round((clientY - rect.top) / rect.height * 720);
            return { x: Math.max(0, Math.min(1280, x)), y: Math.max(0, Math.min(720, y)) };
        }

            </script>
</body>
</html>
"""


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

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

                subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run([ADB, "connect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        elif path == "/api/emulator/stop":
            try:
                cur_user = self.get_current_user()
                if not cur_user or cur_user.get("role") != "admin":
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Forbidden: Only admin can stop emulator"}')
                    return
                subprocess.run([ADB, "-s", DEVICE, "shell", "am", "force-stop", "com.hypergryph.arknights"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run([ADB, "disconnect", DEVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": true}')
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/accounts/switch":
            try:
                cur_user = self.get_current_user()
                body = json.loads(post_data.decode("utf-8")) if post_data else {}
                target_id = body.get("account_id")
                acc = db.get_account(target_id)
                if acc:
                    if cur_user and (cur_user.get("role") == "admin" or acc.get("owner_username") == cur_user.get("username")):
                        if cur_user.get("role") == "admin":
                            db.set_active_account_id(target_id)
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

                # 冲突检测: 提取此账号修改后的定时时间点 (间隔必须 >= 60 分钟)
                prop_times = scheduler_guard.get_account_effective_times(updated_acc)
                ok, conflict_msg, _ = scheduler_guard.check_schedule_conflicts(acc_id, prop_times)
                if not ok:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": conflict_msg}).encode("utf-8"))
                    return

                data = load_accounts()
                accs = data.get("accounts", [])
                target_idx = next((i for i, a in enumerate(accs) if a.get("id") == acc_id), -1)
                if target_idx == -1:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Account not found"}')
                    return

                old_acc = accs[target_idx]
                # 越权安全校验：只有 admin 或本人可修改该账号
                if cur_user.get("role") != "admin" and old_acc.get("owner_username") != cur_user.get("username"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Forbidden: Not your account"}')
                    return

                # 密码保存处理：若未修改密码(仍为掩码••••••••)，保留原存储密码
                submitted_pwd = updated_acc.get("game_password", "")
                if submitted_pwd == "••••••••":
                    updated_acc["game_password"] = old_acc.get("game_password", "")

                updated_acc["owner_username"] = old_acc.get("owner_username", cur_user.get("username", DEFAULT_ADMIN_USER))
                accs[target_idx] = updated_acc
                data["accounts"] = accs
                save_accounts(data)
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

                # 严格时段冲突排他校验 (任务时段间隔必须 >= 60 分钟)
                acc_id = new_acc.get("id") or ("acc_" + str(int(time.time() * 1000)))
                new_acc["id"] = acc_id
                prop_times = scheduler_guard.get_account_effective_times(new_acc)
                ok, conflict_msg, _ = scheduler_guard.check_schedule_conflicts(acc_id, prop_times)
                if not ok:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": conflict_msg}).encode("utf-8"))
                    return

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
                if not filename.endswith(".json"):
                    filename += ".json"

                uname = cur_user.get("username", DEFAULT_ADMIN_USER)
                u_dir = get_user_infrast_dir(uname)
                target = u_dir / filename
                if target.exists():
                    target.unlink()
                elif cur_user.get("role") == "admin":
                    target_root = INFRAST_DIR / DEFAULT_ADMIN_USER / filename
                    if target_root.exists():
                        target_root.unlink()
                    target_shared = INFRAST_DIR / filename
                    if target_shared.exists():
                        target_shared.unlink()

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
                if not filename.endswith(".json"):
                    filename += ".json"

                uname = cur_user.get("username", DEFAULT_ADMIN_USER)
                u_dir = get_user_infrast_dir(uname)
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
                if prop_times and acc_id:
                    ok_conf, c_msg, _ = scheduler_guard.check_schedule_conflicts(acc_id, prop_times)
                    if not ok_conf:
                        conflict_warning = c_msg

                if acc_id:
                    target_acc = db.get_account(acc_id)
                    if target_acc:
                        target_acc.setdefault("infrast", {})
                        target_acc["infrast"]["mode"] = "custom_plan"
                        target_acc["infrast"]["plan_file"] = filename
                        if prop_times:
                            target_acc["schedules"] = prop_times
                        db.save_account(target_acc)
                        scheduler_guard.sync_all_system_crontabs()

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

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

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

                page_html = APP_PAGE
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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"running": running}).encode("utf-8"))

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
            cleanup_old_logs(days=30)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"logs": logs}).encode("utf-8"))

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
            fname = urllib.parse.unquote(params.get("file", ["default_plan.json"])[0])
            info = parse_infrast_schedule_info(fname)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(info).encode("utf-8"))

        elif path == "/api/infrast_plans":
            cur_user = self.get_current_user()
            uname = cur_user.get("username", DEFAULT_ADMIN_USER) if cur_user else DEFAULT_ADMIN_USER
            plans = list_infrast_plans(owner_username=uname)
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
                active_id = raw_accs[0]["id"] if raw_accs else ""
            else:
                raw_accs = db.list_accounts()
                active_id = db.get_active_account_id()

            safe_accounts = []
            for a in raw_accs:
                a_copy = dict(a)
                raw_pwd = a_copy.get("game_password", "")
                a_copy["has_game_password"] = bool(raw_pwd)
                if cur_user.get("role") == "admin" or a.get("owner_username") == cur_user.get("username"):
                    a_copy["game_password"] = raw_pwd
                else:
                    a_copy["game_password"] = ""
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
                    # 模拟器空闲且非管理员：不进行后端截图
                    self.send_response(503)
                    self.end_headers()
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
            # 严格安全防护：监控大屏面向普通用户彻底只读，触控注入仅超级管理员可用
            if cur_user.get("role") != "admin":
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "forbidden", "message": "大屏已设为纯净只读监控，触控仅对超级管理员开放"}).encode("utf-8"))
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

if __name__ == "__main__":
    with ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"ArkWeb control panel listening on 0.0.0.0:{PORT}...")
        httpd.serve_forever()