#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
import shutil
import datetime
import pathlib
import subprocess
import re
import fcntl
import tempfile
import threading
import xml.etree.ElementTree as ET
import notifier
import db
import hypergryph_guard

# 强制统一时区为北京时间，确保周计划判定、公招 04:00 刷新、基建时间轮换等
# 与游戏日一致 (避免容器默认 UTC 导致选班/周计划错位)
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass

def find_adb():
    candidate = shutil.which("adb")
    if candidate:
        return candidate
    local_adb = pathlib.Path.home() / ".local" / "bin" / "adb"
    if local_adb.exists():
        return str(local_adb)
    return "adb"

# 被顶号 / 超时抢占标志。
# 当收到 SIGTERM（通常由 timeout 到点触发，意味着账号被顶号或占用超时），
# 置位该标志，主循环检测到后立即放弃整个任务，不重试、不接续。
_KICKED_OUT = False


def _handle_termination(signum, frame):
    global _KICKED_OUT
    _KICKED_OUT = True
    print(f"\n[!] 收到终止信号 (signal={signum})，判定为账号被顶号或班次超时，准备放弃当前任务...", flush=True)


signal.signal(signal.SIGTERM, _handle_termination)
signal.signal(signal.SIGINT, _handle_termination)

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")
MAA_DIR = BASE_DIR / "maa"
PYTHON_DIR = MAA_DIR / "Python"
INFRAST_DIR = MAA_DIR / "infrast"
# MAA 资源增量覆盖目录：用于修正上游 MaaResource 与当前游戏 UI 失配的资源。
# 该目录独立于 maa/resource/，每日 update_maa.sh 覆盖基准资源时不会清除它。
MAA_RESOURCE_OVERRIDE_DIR = MAA_DIR / "resource_override"
CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
DEPOT_FILE = MAA_DIR / "data" / "DepotData.json"
OPER_BOX_FILE = MAA_DIR / "data" / "OperBoxData.json"
EVENT_6STAR_FILE = pathlib.Path(os.getenv("EVENT_6STAR_FILE", str(BASE_DIR / "data" / "recruit_6star_event.json")))
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
# 容器由 compose 项目 arknights 创建；在 /app 目录下默认项目名会变成 "app"，
# 导致 docker compose stop/up 静默失效 (模拟器永远停不掉)。显式指定项目名修复。
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "arknights")


def compose_cmd(*args):
    """构造带显式项目名的 docker compose 命令。"""
    return ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(COMPOSE_FILE)] + list(args)

ADB_BIN = os.getenv("ADB_BIN", find_adb())
def get_live_adb_target():
    try:
        out = subprocess.check_output([ADB_BIN, "devices"], text=True)
        devices = [l.split()[0] for l in out.splitlines() if "\tdevice" in l]
        if "emulator-5554" in devices: return "emulator-5554"
        if "127.0.0.1:5555" in devices: return "127.0.0.1:5555"
        if devices: return devices[0]
    except Exception:
        pass
    return "127.0.0.1:5555"

ADB_TARGET = get_live_adb_target()

PROFILES_DIR = BASE_DIR / "data" / "account_profiles"
LOCK_FILE_PATH = pathlib.Path("/tmp/ark_runner.lock")
_LOCK_FILE_HANDLE = None

def acquire_runner_lock():
    global _LOCK_FILE_HANDLE
    if _LOCK_FILE_HANDLE is not None:
        return False
    try:
        LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_FILE_PATH, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FILE_HANDLE = handle
        _LOCK_FILE_HANDLE.seek(0)
        _LOCK_FILE_HANDLE.truncate()
        _LOCK_FILE_HANDLE.write(f"{os.getpid()}|{time.time()}\n")
        _LOCK_FILE_HANDLE.flush()
        return True
    except (BlockingIOError, IOError):
        return False

def release_runner_lock():
    global _LOCK_FILE_HANDLE
    if _LOCK_FILE_HANDLE:
        try:
            fcntl.flock(_LOCK_FILE_HANDLE, fcntl.LOCK_UN)
            _LOCK_FILE_HANDLE.close()
        except Exception:
            pass
        _LOCK_FILE_HANDLE = None

def get_account_dir(acc_id):
    d = PROFILES_DIR / acc_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_account_depot_file(acc_id):
    return get_account_dir(acc_id) / "DepotData.json"

def get_account_oper_box_file(acc_id):
    return get_account_dir(acc_id) / "OperBoxData.json"

def get_account_6star_event_file(acc_id):
    return get_account_dir(acc_id) / "recruit_6star_event.json"

def get_account_switch_key(account):
    """
    计算用于 MAA 触屏账号切换 (StartUp.account_name) 的匹配标识。
    MAA 仅支持切换至设备已登录过的账号，用登录名唯一子串匹配:
      官服: 输入尾号(如 4567) 即可匹配 123****4567
    这里优先用纯数字尾号；账号标识本身非数字 (如 B 服的 用户名) 则原样返回。
    """
    raw = str(account.get("account_name", "") or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return digits[-4:]
    return raw

def get_device_active_account_key(pkg="com.hypergryph.arknights"):
    """
    只读探测设备当前实际登录的账号标识 (不修改任何文件)。
    优先官方 SDK USER_CACHE 的 phone 尾号；SDK 为空时回退游戏 playerprefs 的
    HGSDKV2_USERNAME (游戏自身记录的当前登录账号)。读取失败返回 ""。
    """
    try:
        res = subprocess.run(
            [ADB_BIN, "-s", ADB_TARGET, "shell",
             f"cat /data/data/{pkg}/shared_prefs/HypergryphSdkPreferences.xml 2>/dev/null || true"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        m = re.search(r'"phone"\s*:\s*"([^"]+)"', res.stdout)
        if not m:
            m = re.search(r'&quot;phone&quot;:&quot;([^&]+)&quot;', res.stdout)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                return digits[-4:] if len(digits) >= 4 else digits
    except Exception:
        pass
    # 回退：游戏 playerprefs 记录的用户名 (如 130****8019)
    try:
        res2 = subprocess.run(
            [ADB_BIN, "-s", ADB_TARGET, "shell",
             f"cat /data/data/{pkg}/shared_prefs/com.hypergryph.arknights.v2.playerprefs.xml 2>/dev/null || true"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        m2 = re.search(r'HGSDKV2_USERNAME"[^>]*>([^<]+)<', res2.stdout)
        if m2:
            digits = re.sub(r"\D", "", m2.group(1))
            return digits[-4:] if len(digits) >= 4 else m2.group(1).strip()
    except Exception:
        pass
    return ""

def load_account_depot(acc_id):
    """读取指定账号最近一次仓库识别缓存 (item_id -> 数量)。"""
    if not acc_id:
        return {}
    try:
        f = get_account_depot_file(acc_id)
        if not f.exists() and DEPOT_FILE.exists():
            f = DEPOT_FILE
        if f.exists():
            d = json.load(open(f, "r", encoding="utf-8"))
            data = d.get("data", d) if isinstance(d, dict) else {}
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}

sys.path.insert(0, str(PYTHON_DIR))

from asst.asst import Asst
from asst.utils import Message, InstanceOptionType

OWNED_6STAR_SET = set()
CURRENT_ACCOUNT_DATA = {}
_NEED_ACCOUNT_SWITCH = False

def load_accounts_file():
    accs = db.list_accounts()
    active_id = db.get_active_account_id()
    return {"active_account_id": active_id, "accounts": accs}

def save_accounts_file(data):
    for a in data.get("accounts", []):
        db.save_account(a)
    active_id = data.get("active_account_id")
    if active_id:
        db.set_active_account_id(active_id)

def get_account_by_id(acc_id=None):
    if acc_id:
        return db.get_account(acc_id)
    active_id = db.get_active_account_id()
    if active_id:
        acc = db.get_account(active_id)
        if acc:
            return acc
    accs = db.list_accounts()
    return accs[0] if accs else None

def list_available_infrast_plans():
    plans = []
    if INFRAST_DIR.exists():
        for p in INFRAST_DIR.glob("*.json"):
            plans.append(p.name)
    return sorted(plans)

def resolve_infrast_file(plan_filename, owner_username=None, account_id=None):
    """按方舟账号维度解析自定义基建排班文件 (每账号一个目录，仅保留一个文件)。"""
    if not plan_filename:
        return ""
    if not plan_filename.endswith(".json"):
        plan_filename += ".json"

    acc_id = account_id or (CURRENT_ACCOUNT_DATA.get("id") if CURRENT_ACCOUNT_DATA else None)
    if acc_id:
        target = INFRAST_DIR / str(acc_id) / plan_filename
        if target.exists():
            return str(target)
        # 兜底：该账号目录内任意 json
        acc_dir = INFRAST_DIR / str(acc_id)
        if acc_dir.exists():
            found = sorted(acc_dir.glob("*.json"))
            if found:
                return str(found[0])
        return str(target)

    # 无账号上下文时的极简兜底
    return str(INFRAST_DIR / plan_filename)

def resolve_copilot_file(filename, owner_username=None):
    if not filename:
        return None
    p = pathlib.Path(filename)
    if p.is_file():
        return str(p.resolve())

    owner = owner_username or (CURRENT_ACCOUNT_DATA.get("owner_username") if CURRENT_ACCOUNT_DATA else DEFAULT_ADMIN_USER)
    u_dir = BASE_DIR / "data" / "copilot_uploads"
    if (u_dir / owner / filename).is_file():
        return str((u_dir / owner / filename).resolve())
    if (u_dir / filename).is_file():
        return str((u_dir / filename).resolve())

    c_dir = MAA_DIR / "resource" / "copilot"
    if (c_dir / filename).is_file():
        return str((c_dir / filename).resolve())

    # 递归模糊匹配
    clean_name = p.stem.strip()
    for match in c_dir.rglob("*.json"):
        if clean_name in match.stem:
            return str(match.resolve())

    return str(p)

def load_owned_6stars(acc_id=None):
    global OWNED_6STAR_SET
    acc_id = acc_id or CURRENT_ACCOUNT_DATA.get("id")
    oper_file = get_account_oper_box_file(acc_id) if acc_id else OPER_BOX_FILE
    if not oper_file.exists() and OPER_BOX_FILE.exists():
        oper_file = OPER_BOX_FILE
    if oper_file.exists():
        try:
            with open(oper_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            own_list = d.get("own_opers", [])
            OWNED_6STAR_SET = {op["name"] for op in own_list if op.get("own") and op.get("rarity") == 6}
            print(f"[+] 已加载账号专属干员库: 已拥有 6 星干员 {len(OWNED_6STAR_SET)} 位")
        except Exception as e:
            print(f"[!] 加载干员库失败: {e}")

def handle_6star_recruit_event(raw_tags, result_list):
    combos_6star = [r for r in result_list if r.get("level") == 6]
    if not combos_6star and "高级资深干员" not in raw_tags:
        return

    print("\n" + "="*50)
    print("🚨 [ALERT] 检测到高级资深干员 (6星) 公招词条！")
    print(f"[*] 出现标签: {raw_tags}")

    unowned_matches = []
    for c in combos_6star:
        opers = [op.get("name") for op in c.get("opers", [])]
        unowned = [name for name in opers if name not in OWNED_6STAR_SET]
        if unowned:
            unowned_matches.append((c, unowned, opers))

    if unowned_matches:
        best_c, unowned_targets, potential_opers = unowned_matches[0]
        chosen_tags = best_c.get("tags", ["高级资深干员"])
        all_owned = False
        print(f"[+] 策略判定: 锁定未拥有 6 星干员 -> {unowned_targets}")
        print(f"[+] 优选词条组合: {chosen_tags}")
    else:
        first_c = combos_6star[0] if combos_6star else {"tags": ["高级资深干员"], "opers": []}
        chosen_tags = first_c.get("tags", ["高级资深干员"])
        unowned_targets = []
        potential_opers = [op.get("name") for op in first_c.get("opers", [])]
        all_owned = True
        print(f"[+] 策略判定: 当前 6 星已全部拥有 (Nobody cares)，自动选择保底组合 -> {chosen_tags}")

    event_payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "account_id": CURRENT_ACCOUNT_DATA.get("id", ""),
        "account_name": CURRENT_ACCOUNT_DATA.get("name", ""),
        "raw_tags": raw_tags,
        "chosen_tags": chosen_tags,
        "potential_opers": potential_opers,
        "unowned_targets": unowned_targets,
        "all_owned": all_owned,
        "notified": False
    }

    # 6 星公招独立落盘保存
    try:
        acc_id = CURRENT_ACCOUNT_DATA.get("id")
        event_file = get_account_6star_event_file(acc_id) if acc_id else EVENT_6STAR_FILE
        event_file.parent.mkdir(parents=True, exist_ok=True)
        with open(event_file, "w", encoding="utf-8") as f:
            json.dump(event_payload, f, ensure_ascii=False, indent=2)
        print(f"[+] 6 星公招事件已按账号隔离落盘: {event_file}")
    except Exception as e:
        print(f"[!] 写入 6 星事件失败: {e}")
    
    # 触发账号通信渠道即时推送
    strat = CURRENT_ACCOUNT_DATA.get("recruit", {}).get("select_6star_strategy", "unowned_first")
    if strat == "notify_user":
        sub = f"【明日方舟高资干员待选】账号: {CURRENT_ACCOUNT_DATA.get('name')}"
        md = f"### ⚠️ 发现【高级资深干员】(六星公招)\n- **账号**: {CURRENT_ACCOUNT_DATA.get('name')}\n- **当前标签**: `{', '.join(raw_tags)}`\n- **策略设定**: 保留并等待人工选择\n\n系统已为您保留公招界面，请尽快打开控制台完成选择确认！"
    else:
        sub = f"【明日方舟六星公招锁定】账号: {CURRENT_ACCOUNT_DATA.get('name')}"
        t_str = ', '.join(chosen_tags)
        p_str = ', '.join(potential_opers)
        md = f"### 🎉 发现并自动优选锁定【高级资深干员】\n- **账号**: {CURRENT_ACCOUNT_DATA.get('name')}\n- **选择标签**: `{t_str}`\n- **潜在六星**: {p_str}\n- **未拥有优先**: {'是' if unowned_targets else '全部已拥有(保底选择)'}\n- **锁定时间**: 9小时满额"
    
    try:
        res = notifier.dispatch_account_notify(CURRENT_ACCOUNT_DATA, sub, md, event_type="on_6star_recruit")
        print(f"[+] 6 星公招推送结果: {res}")
    except Exception as err:
        print(f"[!] 推送异常: {err}")
    print("="*50 + "\n")

def normalize_depot_items(data_json):
    """
    将 MAA 仓库扫描结果归一化为 {item_id_str: count_int} 映射表
    兼容量化支持 MAA v6 的 {"item": [{"id":..., "count":...}]} 官方结构与扁平字典
    """
    items_map = {}
    if not isinstance(data_json, dict):
        return items_map

    # 1. MAA v6 官方标准 item 列表格式
    if "item" in data_json and isinstance(data_json["item"], list):
        for it in data_json["item"]:
            if isinstance(it, dict) and "id" in it and "count" in it:
                items_map[str(it["id"])] = int(it["count"])

    # 2. 扁平键值对格式
    for k, v in data_json.items():
        if k != "item" and (isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit())):
            items_map[str(k)] = int(v)

    return items_map

def update_account_inventory(data_json):
    target_ids = {"4003": "合成玉", "4002": "至纯源石", "4001": "龙门币", "30012": "固源岩"}
    chip_ids = {
        "32001": "芯片助剂",
        "3211": "先锋芯片", "3212": "先锋芯片组", "3213": "先锋双芯片",
        "3221": "近卫芯片", "3222": "近卫芯片组", "3223": "近卫双芯片",
        "3231": "重装芯片", "3232": "重装芯片组", "3233": "重装双芯片",
        "3241": "狙击芯片", "3242": "狙击芯片组", "3243": "狙击双芯片",
        "3251": "术师芯片", "3252": "术师芯片组", "3253": "术师双芯片",
        "3261": "医疗芯片", "3262": "医疗芯片组", "3263": "医疗双芯片",
        "3271": "辅助芯片", "3272": "辅助芯片组", "3273": "辅助双芯片",
        "3281": "特种芯片", "3282": "特种芯片组", "3283": "特种双芯片"
    }

    acc_id = CURRENT_ACCOUNT_DATA.get("id")
    if not acc_id:
        return

    items_map = normalize_depot_items(data_json)
    acc = db.get_account(acc_id) or {}
    inv = acc.get("inventory", {})

    # 1. 更新基础货币
    for item_id, name in target_ids.items():
        if item_id in items_map:
            inv[name] = items_map[item_id]

    inv.pop("源石碎片", None)

    # 2. 独立更新账号级芯片储备字典 (支持中文名称与数字ID双重索引)
    chips_dict = {}
    for cid, cname in chip_ids.items():
        if cid in items_map:
            cnt = int(items_map[cid])
            chips_dict[cname] = cnt
            chips_dict[cid] = cnt

    inv["chips"] = chips_dict
    inv["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 3. 原子性写入 SQLite 对应账号行，防止全表回写竞态覆盖
    db.update_account_inventory_and_chips(acc_id, inv, chips_dict)
    print(f"[✔] 已同步账号 [{acc.get('name')}] 的全量货币与芯片数据至 SQLite 数据库 (已收录芯片 {len(chips_dict)} 种)")

CURRENT_RECRUIT_CONFIRMED = 0
CURRENT_ASST_INSTANCE = None
FAILED_TASKCHAINS = set()
# 账号切换失败计数：MAA 的 AccountSwitchTask 失败后会重跑整条 StartUp，
# 每次都会重启客户端。若不设上限，遇到 UI 失配会无限重启直至 OOM。
ACCOUNT_SWITCH_FAILURES = 0
ACCOUNT_SWITCH_FAILURE_LIMIT = 3

@Asst.CallBackType
def log_callback(msg, details, arg):
    global CURRENT_RECRUIT_CONFIRMED, FAILED_TASKCHAINS, CURRENT_ASST_INSTANCE, ACCOUNT_SWITCH_FAILURES
    m = Message(msg)
    try:
        d = json.loads(details.decode('utf-8'))
    except Exception:
        d = details.decode('utf-8', errors='ignore')
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] [{m.name}] {d}")

    # 捕获任务链失败 (若关键唤醒 StartUp 失败，立即中止后续空转)
    if m.name == "TaskChainError" and isinstance(d, dict):
        tc = d.get("taskchain")
        if tc:
            FAILED_TASKCHAINS.add(tc)
            if tc == "StartUp":
                print(f"[!] 关键任务 [StartUp] 唤醒失败，紧急中止后续任务链，避免无效空转！")
                if CURRENT_ASST_INSTANCE:
                    try:
                        CURRENT_ASST_INSTANCE.stop()
                    except Exception:
                        pass

    # 账号切换硬护栏：AccountSwitchTask 反复失败会不断重启客户端直至 OOM。
    # 达到阈值即强制 stop，交由上层按失败处理，绝不无限重试。
    if m.name == "SubTaskError" and isinstance(d, dict) and d.get("subtask") == "AccountSwitchTask":
        ACCOUNT_SWITCH_FAILURES += 1
        print(f"[!] 账号切换失败 ({ACCOUNT_SWITCH_FAILURES}/{ACCOUNT_SWITCH_FAILURE_LIMIT})")
        if ACCOUNT_SWITCH_FAILURES >= ACCOUNT_SWITCH_FAILURE_LIMIT:
            print("[!] 账号切换连续失败达上限，强制中止任务链，防止无限重启客户端导致 OOM！")
            if CURRENT_ASST_INSTANCE:
                try:
                    CURRENT_ASST_INSTANCE.stop()
                except Exception:
                    pass

    # 捕获公招开槽确认并统计今日已消耗数量
    if isinstance(d, dict) and d.get("taskchain") == "Recruit":
        if m.name == "SubTaskCompleted" and d.get("details", {}).get("task") == "RecruitConfirm":
            CURRENT_RECRUIT_CONFIRMED += 1
            print(f"[+] 记录到有效公招确认 (本次累计消耗: {CURRENT_RECRUIT_CONFIRMED} 张)")
        elif m.name == "TaskChainCompleted":
            if CURRENT_ACCOUNT_DATA and CURRENT_RECRUIT_CONFIRMED > 0:
                acc_id = CURRENT_ACCOUNT_DATA.get("id")
                updated_stats = db.update_account_recruit_progress(acc_id, CURRENT_RECRUIT_CONFIRMED)
                print(f"[✔] 公招阶段结束：本次消耗 {CURRENT_RECRUIT_CONFIRMED} 张公招券，今日已累计消耗 {updated_stats.get('today_count', 0)} / {updated_stats.get('daily_limit', 4)} 张。")

    # 捕获仓库扫描结果并自动落盘更新 DepotData.json 及对应账号库存
    if isinstance(d, dict) and d.get("what") == "DepotInfo":
        det = d.get("details", {})
        if det.get("done") and "data" in det:
            try:
                raw_data = det["data"]
                data_json = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                
                # 账号隔离存储与公共兼容存储
                acc_id = CURRENT_ACCOUNT_DATA.get("id")
                acc_depot_file = get_account_depot_file(acc_id) if acc_id else DEPOT_FILE
                with open(acc_depot_file, "w", encoding="utf-8") as out:
                    json.dump({"done": True, "data": data_json}, out, ensure_ascii=False, indent=2)
                with open(DEPOT_FILE, "w", encoding="utf-8") as out:
                    json.dump({"done": True, "data": data_json}, out, ensure_ascii=False, indent=2)

                print(f"[✔] 仓库数据已落盘隔离归档: {acc_depot_file.name}")
                update_account_inventory(data_json)
            except Exception as e:
                print(f"[!] 保存仓库数据异常: {e}")

    # 捕获公招结果并触发 6 星决策
    if isinstance(d, dict) and d.get("what") == "RecruitResult":
        det = d.get("details", {})
        raw_tags = det.get("tags", [])
        result_list = det.get("result", [])
        if "高级资深干员" in raw_tags or any(r.get("level") == 6 for r in result_list):
            handle_6star_recruit_event(raw_tags, result_list)

def calculate_plan_index(infrast_path):
    """
    自定义基建排班的「时间轮换」选班 (对齐 MAA 桌面版 InfrastSettingsUserControlModel)。

    背景: MAA 协议文档明确指出 period 字段 Core 不处理，需由集成层按时间自行选择 plan_index。
    MAA 官方规则 (PlanSelect == -1 时间轮换):
      - 取「任一 period 覆盖当前时间」的第一个方案；
      - 无任何方案匹配则回退方案 0。

    边界处理: MAA 的 now 是带毫秒的 TimeOnly，闭区间仅在恰好 :00.000 命中上一个方案；
    我们的定时器在 10:00/16:00/22:00 精确触发，且选班发生在启动守护之后 (仍处于整点分钟)，
    因此采用「左闭右开 [start, end)」以在整点正确交接下一班；对 23:59/24:00 收尾端点保持闭区间，
    以覆盖当天最后时段 (与文件习惯写法一致)。
    """
    try:
        with open(infrast_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        plans = data.get("plans", [])
        if not plans:
            return 0

        now_str = time.strftime("%H:%M")
        for idx, plan in enumerate(plans):
            for period in plan.get("period", []):
                if len(period) == 2:
                    start, end = str(period[0]), str(period[1])
                    if start <= end:
                        # 左闭右开；收尾端点 23:59/24:00 视为闭区间
                        hit = (start <= now_str <= end) if end in ("23:59", "24:00") else (start <= now_str < end)
                        if hit:
                            print(f"[+] 自定义基建时间轮换: 当前 {now_str} 命中方案 [{plan.get('name', idx)}] (index={idx})")
                            return idx
                    else:
                        # 跨午夜 (start > end): now >= start 或 now < end
                        if now_str >= start or now_str < end:
                            print(f"[+] 自定义基建时间轮换(跨午夜): 当前 {now_str} 命中方案 [{plan.get('name', idx)}] (index={idx})")
                            return idx

        # 无任何方案覆盖当前时间 -> 与 MAA 一致，回退方案 0
        print(f"[!] 自定义基建: 当前 {now_str} 未命中任何方案的时间段，按 MAA 规则回退方案 0。")
        return 0
    except Exception as e:
        print(f"[!] 排班表时间匹配异常: {e}")
        return 0



def kill_rogue_browsers():
    try:
        browsers = [
            "com.android.chrome",
            "com.android.browser",
            "org.chromium.webview_shell",
            "com.google.android.webview"
        ]
        for b in browsers:
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", b], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def force_kill_arknights(pkg="com.hypergryph.arknights", deep=False):
    """
    彻底清理明日方舟全部相关进程 (主进程 + GP7Service/GP7Worker 等 Unity 子进程)。
    Android 的 am force-stop 常常无法回收 Unity 多进程，残留进程各占约 1GB，
    多次运行后会触发内核 OOM killer，导致下一次启动直接黑屏闪退。
    """
    # 1. 常规 force-stop
    try:
        subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass

    # kill -9 需要 root (redroid 重启后会丢失 root)
    try:
        ensure_adb_root()
    except Exception:
        pass

    # 2. 深度清理：列出所有含包名的进程并 kill -9
    killed = 0
    try:
        res = subprocess.run(
            [ADB_BIN, "-s", ADB_TARGET, "shell", f"ps -A -o PID,NAME | grep {pkg}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=8)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                try:
                    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "kill", "-9", parts[0]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass

    if killed:
        print(f"[+] 已强制清理明日方舟残留进程 {killed} 个 (防 OOM 黑屏)。")
    if deep:
        # 3. 极致清理：连同包相关的一切服务进程一并清理
        try:
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"pkill -9 -f {pkg}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception:
            pass
    return killed

def _iter_arknights_procs(pkg="com.hypergryph.arknights"):
    """
    只读列出方舟相关进程，返回 [(pid, ppid, name, comm)]。
    通过 /proc/<pid>/comm 取真实线程名 (shell 权限即可读)：
      - 主进程 comm 以 '.arknights' 结尾 (如 gryph.arknights)
      - Unity 残留子进程 comm 形如 'sm-thread-http1/2'
    """
    procs = []
    try:
        shell_cmd = (
            f"ps -A -o PID,PPID,NAME | grep -E '{pkg}|com.bilibili.arknights' | "
            "while read pid ppid name; do comm=$(cat /proc/$pid/comm 2>/dev/null); "
            "echo \"$pid $ppid $name $comm\"; done"
        )
        res = subprocess.run(
            [ADB_BIN, "-s", ADB_TARGET, "shell", shell_cmd],
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
    """主进程判定：comm 以 '.arknights' 结尾。"""
    return comm.endswith(".arknights")

def _classify_arknights_procs(procs, pkg="com.hypergryph.arknights"):
    """
    将方舟「裸名进程」分类 (带 ':' 后缀的 :GP7Service/:GP7Worker/:pushcore 一律不动):
      - mains:  主进程 (comm 以 '.arknights' 结尾)，正常恰好 1 个
      - leaked: PPID==1 的 Unity 残留子进程 (comm 非主进程，如 sm-thread-http*)，可安全清理
    主进程即便因 zygote 重启变为 PPID==1 也一律保留，手动态永不被误杀。
    """
    bare_names = (pkg, "com.bilibili.arknights")
    mains, leaked = [], []
    for pid, ppid, name, comm in procs:
        if name not in bare_names:
            continue
        if _is_main_proc(comm):
            mains.append((pid, ppid, name, comm))
        elif ppid == 1:
            leaked.append((pid, ppid, name, comm))
    return mains, leaked

def reap_orphan_arknights(pkg="com.hypergryph.arknights"):
    """
    精准回收「残留子进程/重复主进程」(绝不干扰正在运行的实例)：
      1) 清理 PPID==1 的 Unity 残留子进程 (comm 非主进程)；
      2) 重复主进程 (comm 以 '.arknights' 结尾) 叠加时，保留最新，清旧及其子进程。
    主进程本身永不被作为孤儿清理。
    """
    procs = _iter_arknights_procs(pkg)
    if not procs:
        return 0
    mains, leaked = _classify_arknights_procs(procs, pkg)
    if not leaked and len(mains) <= 1:
        return 0
    # kill 需要 root (redroid 重启后会丢失 root，导致 kill 静默失败)
    try:
        ensure_adb_root()
    except Exception:
        pass
    killed = 0

    def _kill(pid):
        nonlocal killed
        try:
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "kill", "-9", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            killed += 1
        except Exception:
            pass

    # 1) Unity 残留子进程
    for pid, _ppid, _name, _comm in leaked:
        _kill(pid)

    # 2) 重复主进程治理
    if len(mains) > 1:
        mains.sort(key=lambda x: x[0])
        keep_pid = mains[-1][0]
        stale_pids = {m[0] for m in mains[:-1]}
        print(f"[!] 检测到重复方舟主进程 {len(mains)} 个 (保留最新 PID={keep_pid})，已清理旧副本及其子进程。", flush=True)
        for pid, _ppid, _name, _comm in mains[:-1]:
            _kill(pid)
        for pid, ppid, _name, _comm in procs:
            if ppid in stale_pids and not _is_main_proc(_comm):
                _kill(pid)

    if killed:
        print(f"[+] 已回收孤儿游戏残留/重复主进程 {killed} 个 (防 OOM 黑屏)。")
    return killed

def count_arknights_mains(pkg="com.hypergryph.arknights"):
    """返回真正的主进程数量 (comm 以 '.arknights' 结尾)。正常为 0 或 1。"""
    procs = _iter_arknights_procs(pkg)
    return len(_classify_arknights_procs(procs, pkg)[0])

def _minutes_until_next_schedule(horizon_minutes=60):
    """
    检查全服所有账号的定时班次 (maa_timers) 中，是否存在未来 horizon_minutes
    分钟内将要触发的任务。返回 (has_upcoming, nearest_minutes)。
    用于智能退出策略：临近有任务 -> 保留模拟器；长时间无任务 -> 关闭模拟器省电。
    """
    try:
        now = datetime.datetime.now()
        now_min = now.hour * 60 + now.minute
        nearest = 10 ** 9
        for rec in db.list_all_maa_timers():
            t_list = (rec.get("timers") or {}).get("List", [])
            for item in t_list:
                if not item.get("IsEnabled"):
                    continue
                h = int(item.get("Hour", 0))
                m = int(item.get("Minute", 0))
                slot = h * 60 + m
                delta = (slot - now_min) % 1440
                if 0 <= delta <= horizon_minutes:
                    if delta < nearest:
                        nearest = delta
        if nearest < 10 ** 9:
            return True, nearest
        return False, None
    except Exception as e:
        print(f"[!] 智能退出策略：查询未来班次异常，默认保留模拟器: {e}")
        # 异常时保守处理：保留模拟器，避免误关导致下一班无法启动
        return True, None


def execute_exit_action(on_complete):
    kill_rogue_browsers()
    pkg = "com.bilibili.arknights" if CURRENT_ACCOUNT_DATA.get("platform") == "Bilibili" else "com.hypergryph.arknights"

    # 说明: 本系统已改为 MAA 原生触屏账号切换，不再读写/归档任何游戏配置文件。

    # 智能退出策略：auto 模式下按「未来 1 小时内是否有班次」决定关游戏还是关模拟器
    effective_action = on_complete
    if on_complete == "auto":
        has_upcoming, nearest = _minutes_until_next_schedule(60)
        if has_upcoming:
            effective_action = "close_game"
            print(f"[+] 智能退出策略：{('约 ' + str(nearest) + ' 分钟后') if nearest is not None else '近期'}还有排班任务，仅关闭游戏并保留模拟器待命。")
        else:
            effective_action = "stop_emu"
            print("[+] 智能退出策略：未来 1 小时内无任何排班任务，关闭游戏并停止模拟器以释放硬件资源。")

    print(f"\n[*] 任务完成，正在执行退出动作: [{effective_action}] (原始策略={on_complete})...")
    if effective_action == "stop_emu":
        print("[+] 正在关闭明日方舟客户端 (含全部 Unity 子进程)...")
        force_kill_arknights(pkg, deep=True)
        print("[+] 正在停止安卓模拟器 (ReDroid) 容器以完全释放 4GB+ 内存与 GPU...")
        subprocess.run(compose_cmd("stop", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([ADB_BIN, "disconnect", ADB_TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[✔] 模拟器已完全停止，硬件资源已彻底释放！")
    elif effective_action == "close_game":
        print("[+] 正在关闭明日方舟客户端并保持模拟器运行...")
        force_kill_arknights(pkg, deep=True)
        print("[✔] 客户端已退出，模拟器保持待命状态。")
    elif effective_action == "keep_running":
        print("[+] 保持当前前台与模拟器运行状态不变。")


RUNNING_TASK_FILE = CONFIG_DIR / "running_task.json"

def mark_running_task(account, is_running=True):
    try:
        db.set_running_task({
            "running": is_running,
            "account_id": account.get("id", "") if is_running else "",
            "account_name": account.get("name", "") if is_running else "",
            "owner_username": account.get("owner_username", DEFAULT_ADMIN_USER) if is_running else "",
            "timestamp": int(time.time())
        })
    except Exception:
        pass


def ensure_adb_root():
    try:
        res = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "whoami"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        if res.stdout.strip() == "root":
            return
    except Exception:
        pass
    try:
        subprocess.run([ADB_BIN, "-s", ADB_TARGET, "root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        for _ in range(10):
            time.sleep(1)
            try:
                subprocess.run([ADB_BIN, "connect", ADB_TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                chk = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "whoami"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
                if chk.stdout.strip() == "root":
                    break
            except Exception:
                pass
    except Exception:
        pass

def dump_ui_xml():
    try:
        res = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if res.returncode == 0:
            out = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "cat", "/sdcard/window_dump.xml"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, text=True)
            return out.stdout
    except Exception:
        pass
    return ""

def find_node_bounds(xml_str, resource_id=None, text=None, exact=False):
    if not xml_str:
        return None
    try:
        root = ET.fromstring(xml_str)
        for node in root.iter("node"):
            matched = True
            if resource_id and resource_id not in node.attrib.get("resource-id", ""):
                matched = False
            node_txt = node.attrib.get("text", "")
            if text:
                if exact:
                    if node_txt != text:
                        matched = False
                else:
                    if text not in node_txt:
                        matched = False
            if matched:
                b = node.attrib.get("bounds")
                if b:
                    nums = re.findall(r"\d+", b)
                    if len(nums) == 4:
                        x1, y1, x2, y2 = map(int, nums)
                        return (x1 + x2) // 2, (y1 + y2) // 2
    except Exception:
        pass
    return None

def tap_screen(x, y):
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "input", "tap", str(x), str(y)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def keyevent_press(code):
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "input", "keyevent", str(code)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def input_text_safe(text):
    clean_text = str(text)
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "input", "text", clean_text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_device_rx_bytes():
    try:
        out = subprocess.check_output([ADB_BIN, "-s", ADB_TARGET, "shell", "cat", "/proc/net/dev"], text=True, timeout=5)
        for line in out.splitlines():
            if "eth0:" in line:
                parts = line.split("eth0:")[1].split()
                return int(parts[0])
    except Exception:
        pass
    return 0

def guard_game_startup(pkg, platform):
    """
    启动守护探针：
    1. 区分“连接假死”（0 流量停滞）与“真下载热更”（流量持续增加，即使 500MB 也动态守护放行）。
    2. 自动检测并点击更新确认弹窗。
    3. 若真卡死无流量超过 60 秒，自动强杀重启重试。
    """
    print(f"[*] 启动游戏守护探针: 正在预唤醒客户端 [{pkg}] 并探测更新状态...")
    # 启动前彻底清理残留进程，避免历史孤儿进程叠加导致 OOM 黑屏
    force_kill_arknights(pkg)
    time.sleep(2)
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "start", "-n", f"{pkg}/com.u8.sdk.U8UnityContext"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    last_rx = get_device_rx_bytes()
    stall_sec = 0
    total_dl_bytes = 0
    restarts = 0
    max_restarts = 2
    
    start_time = time.time()
    while time.time() - start_time < 900:  # 最长宽限 15 分钟
        time.sleep(5)
        
        # 1. 检测弹窗：自动确认热更下载对话框或公告
        xml = dump_ui_xml()
        if xml:
            # 确认更新弹窗
            confirm_pos = find_node_bounds(xml, text="确认") or find_node_bounds(xml, text="确定")
            if confirm_pos and ("更新" in xml or "下载" in xml or "资源" in xml or "新版本" in xml):
                print(f"[+] 检测到热更/资源确认弹窗，自动点击确认 ({confirm_pos[0]}, {confirm_pos[1]})...")
                tap_screen(confirm_pos[0], confirm_pos[1])
                time.sleep(2)
                continue
            
            # 公告关闭
            if find_node_bounds(xml, text="活动公告") or find_node_bounds(xml, text="系统公告"):
                print("[+] 检测到游戏公告，自动关闭...")
                tap_screen(940, 104)
                time.sleep(2)
        
        # 2. 检查网络流量变化
        curr_rx = get_device_rx_bytes()
        diff = max(0, curr_rx - last_rx)
        last_rx = curr_rx
        
        if diff > 100 * 1024:  # 5 秒内流量超过 100KB，说明正在活跃下载更新包
            stall_sec = 0
            total_dl_bytes += diff
            speed_kb = diff / 5.0 / 1024.0
            print(f"[+] 检测到游戏正在下载热更新数据... (已传输: {total_dl_bytes / 1024 / 1024:.1f} MB, 速率: {speed_kb:.1f} KB/s)")
            continue
        else:
            stall_sec += 5

        # 3. 检查是否平稳就绪 (Activity 聚焦且网络流量已平稳)
        try:
            focus_out = subprocess.check_output([ADB_BIN, "-s", ADB_TARGET, "shell", "dumpsys", "window"], text=True, timeout=5)
            if f"{pkg}/" in focus_out and "mCurrentFocus" in focus_out and (time.time() - start_time) >= 15:
                if stall_sec >= 15 and total_dl_bytes > 0:
                    print(f"[✔] 热更下载与解包完成 (共下载 {total_dl_bytes / 1024 / 1024:.1f} MB)，客户端已平稳就绪！")
                    return True
                elif stall_sec >= 15 and total_dl_bytes == 0:
                    print("[+] 客户端启动平稳就绪，无需热更下载，交接给 MAA 任务链。")
                    return True
        except Exception:
            pass

        # 4. 假死处理：如果持续 60 秒 0 字节下载且未就绪，判定为握手假死
        if stall_sec >= 60 and total_dl_bytes == 0:
            if restarts < max_restarts:
                restarts += 1
                print(f"[!] 警告: 客户端启动 60 秒无网络数据流动，判定为 CDN 握手假死！正在执行看门狗强杀重启 (第 {restarts}/{max_restarts} 次)...")
                # 深度清理全部子进程，避免残留进程堆积触发 OOM
                force_kill_arknights(pkg, deep=True)
                time.sleep(2)
                subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "start", "-n", f"{pkg}/com.u8.sdk.U8UnityContext"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                stall_sec = 0
                start_time = time.time()
                continue
            else:
                print(f"[!] 客户端经 {max_restarts} 次重启依然处于未就绪状态，交由 MAA 尝试最后唤醒。")
                break

    return True

def switch_game_profile(account_or_id):
    """
    【MAA 原生触屏账号切换】— 不再读写/注入任何游戏配置文件，也无需账密。

    原理: 设备上可存在多个已登录账号(游戏账号管理列表)。MAA 的 StartUp 任务
    支持通过 account_name (登录名唯一子串，官服填尾号) 触屏点击切换到目标账号。
    因此这里不再做本地存档注入，仅做一次「账号上下文校验/提示」：
      - 目标账号有可用的切换标识(尾号) -> 交由 MAA StartUp.account_name 触屏切换；
      - 无标识则沿用设备当前已登录账号 (首次需用户手动触屏登录一次)。
    """
    if not account_or_id:
        return False
    if isinstance(account_or_id, dict):
        account = account_or_id
    else:
        account = get_account_by_id(account_or_id) or {"id": account_or_id, "name": account_or_id}

    global _NEED_ACCOUNT_SWITCH
    acc_name = account.get("name", "未命名账号")
    key = get_account_switch_key(account)
    if not key:
        _NEED_ACCOUNT_SWITCH = False
        print(f"[!] 账号 [{acc_name}] 未设置切换标识(尾号)，将沿用设备当前已登录账号；如需切换请在网页端补充账号标识。")
        return True

    # 只读探测设备当前登录账号，决定是否需要触屏切换。
    # 关键：仅在「明确探测到不同账号」时才请求 MAA 触屏切换；
    # 探测不到 (''，会话不可读/未登录) 时一律不切换 —— 否则 MAA 的
    # AccountSwitchTask 会反复重启客户端寻找账号管理界面，产生大量孤儿进程直至 OOM。
    # 读应用私有 prefs 需要 adb root，先确保提权再探测。
    try:
        ensure_adb_root()
    except Exception:
        pass
    cur_key = get_device_active_account_key()
    if not cur_key:
        _NEED_ACCOUNT_SWITCH = False
        print(f"[!] 无法读取设备当前登录账号 (可能未登录/会话已失效)，本次不触发触屏切换，"
              f"将直接以设备现状运行；如需切换请先在网页端手动触屏登录目标账号 [{acc_name}]。")
    elif cur_key == key:
        _NEED_ACCOUNT_SWITCH = False
        print(f"[+] 设备当前已登录目标账号 [{acc_name}] (尾号 {key})，无需切换。")
    else:
        _NEED_ACCOUNT_SWITCH = True
        print(f"[+] 检测到设备当前账号(尾号 {cur_key}) 与目标账号 [{acc_name}] (尾号 {key}) 不同，"
              f"将由 MAA StartUp 触屏切换已登录账号。")
    # 触屏切换交由 MAA StartUp 完成，这里恒返回 True（切换失败由 MAA 任务链反馈）
    return True

def _maa_resource_override_tasks():
    """
    返回需要增量覆盖的 MAA 任务定义。

    背景：游戏 2.7.81 在登录界面底部新增了「扫码登录」按钮，使「账号管理」由第 3 格
    左移一格。上游 MaaResource 的 AccountManager.roi=[855,660,150,40] 恰好落在
    「查看公告」上，导致模板始终只能匹配到 0.71 分而被判失败，AccountSwitchTask
    反复重启客户端直至 OOM。这里用整条底部按钮栏的大 ROI 覆盖，既修正当前错位，
    也能容忍未来按钮横向再微调。
    """
    return {
        "AccountManager": {
            "action": "ClickSelf",
            "algorithm": "MatchTemplate",
            "roi": [430, 650, 720, 70],
            "next": ["LoginOther"],
        }
    }


def ensure_maa_resource_override():
    """
    确保 MAA 增量资源覆盖目录存在且内容最新，返回可用的覆盖目录路径 (不可用时返回 None)。

    MAA 的 Asst.load(path, incremental_path) 会把 incremental_path 当作与 path 同构的
    资源根再次加载，同名任务以后者覆盖前者。因此覆盖目录需保持 <root>/resource/tasks/tasks.json
    的层级结构。
    """
    try:
        tasks_dir = MAA_RESOURCE_OVERRIDE_DIR / "resource" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        target = tasks_dir / "tasks.json"
        payload = json.dumps(_maa_resource_override_tasks(), ensure_ascii=False, indent=4)
        old = None
        if target.exists():
            try:
                old = target.read_text(encoding="utf-8")
            except Exception:
                old = None
        if old != payload:
            target.write_text(payload, encoding="utf-8")
            print(f"[+] 已写入 MAA 资源覆盖补丁: {target}")
        return MAA_RESOURCE_OVERRIDE_DIR
    except Exception as e:
        print(f"[!] 写入 MAA 资源覆盖补丁失败 ({e})，将使用上游原始资源。")
        return None


def run_task(task_type="daily", extra_args=None):
    global CURRENT_ACCOUNT_DATA
    if extra_args is None:
        extra_args = {}

    target_acc_id = extra_args.get("account_id")
    account = get_account_by_id(target_acc_id)
    if not account:
        print(f"[!] 错误: 未找到目标账号 ID: {target_acc_id}，任务中止！")
        return False

    # 1. 获取全局排他锁，防止任何并发执行导致抢占模拟器或串号
    if not acquire_runner_lock():
        print(f"[!] 警告: 检测到另一任务实例正在执行，为保护托管状态防止冲突，本次任务安全退出。")
        return False

    CURRENT_ACCOUNT_DATA = account
    global CURRENT_RECRUIT_CONFIRMED, FAILED_TASKCHAINS, CURRENT_ASST_INSTANCE, ACCOUNT_SWITCH_FAILURES
    CURRENT_RECRUIT_CONFIRMED = 0
    FAILED_TASKCHAINS = set()
    ACCOUNT_SWITCH_FAILURES = 0
    mark_running_task(account, True)

    try:
        # 鹰角官方服状态前置自检 (维护停服跳过 / 强制大更先行)
        if account.get("platform", "Official") == "Official":
            guard = hypergryph_guard.preflight_check(ADB_BIN, ADB_TARGET)
            if guard.get("is_maintenance"):
                print(f"[!] {guard['reason']}，安全跳过本次挂机任务！")
                try:
                    notifier.dispatch_account_notify(account, "【明日方舟托管跳过】官方维护中", f"### 官方服务器停服维护中\n- **原因**: {guard['reason']}\n- **状态**: 自动跳过当前挂机时段，待开服后恢复执行", event_type="on_daily_summary")
                except Exception:
                    pass
                return True
            if guard.get("need_update"):
                print(f"[!] {guard['reason']}，正在先触发官方大版本覆盖升级...")
                subprocess.run(["bash", str(BIN_DIR / "update_arknights.sh")])

        # 启动前先彻底清理上一次可能残留的游戏进程 (防止 OOM 黑屏闪退)
        _pkg_pre = "com.bilibili.arknights" if account.get("platform") == "Bilibili" else "com.hypergryph.arknights"
        try:
            force_kill_arknights(_pkg_pre)
        except Exception:
            pass

        # 账号切换采用 MAA 原生触屏方式 (StartUp.account_name)，不读写任何配置文件
        switch_game_profile(account)
        acc_name = account.get("name", "默认账号")
        print(f"[*] 启动任务: {task_type} · 目标账号: [{acc_name}] (ID: {account.get('id')})")

        # 启动下载守护与前置假死看门狗
        pkg = "com.bilibili.arknights" if account.get("platform") == "Bilibili" else "com.hypergryph.arknights"
        guard_game_startup(pkg, account.get("platform", "Official"))

        load_owned_6stars(account.get("id"))
    
        print(f"[*] 正在初始化 MAA 官方核心引擎: {MAA_DIR}")
        resource_override = ensure_maa_resource_override()
        if resource_override:
            print(f"[+] 已启用 MAA 资源增量覆盖: {resource_override} (修正登录界面账号管理按钮 ROI)")
            Asst.load(path=MAA_DIR, incremental_path=resource_override)
        else:
            Asst.load(path=MAA_DIR)
        asst = Asst(callback=log_callback)
        CURRENT_ASST_INSTANCE = asst
    
        asst.set_instance_option(InstanceOptionType.touch_type, "adb")
    
        print(f"[*] 正在连接 ADB ({ADB_TARGET})...")
        if not asst.connect(ADB_BIN, ADB_TARGET):
            print("[*] ADB 未直连，尝试自动唤醒模拟器容器...")
            subprocess.run(compose_cmd("up", "-d", "redroid"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)
            subprocess.run([ADB_BIN, "connect", ADB_TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not asst.connect(ADB_BIN, ADB_TARGET):
                print("[!] 模拟器唤醒后依然无法连接，任务中止")
                return False
    
        print(f"[+] ADB 连接就绪，配置任务队列: {task_type}")

        # 校准时区为北京时间 (redroid 默认 GMT，会导致游戏内时间错位 8 小时)
        try:
            tz = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "getprop", "persist.sys.timezone"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5).stdout.strip()
            if tz != "Asia/Shanghai":
                subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "setprop", "persist.sys.timezone", "Asia/Shanghai"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "setprop", "ro.boot.timezone", "Asia/Shanghai"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                print("[+] 已将模拟器时区校准为 Asia/Shanghai。")
        except Exception:
            pass

        # 检查该账号是否具备 MAA 桌面版协议 TaskQueue
        custom_task_queue = None
        maa_cfg_record = db.get_maa_config(account.get("id"))
        if maa_cfg_record and isinstance(maa_cfg_record.get("config"), dict):
            q = maa_cfg_record["config"].get("TaskQueue")
            if isinstance(q, list) and len(q) > 0:
                custom_task_queue = q

        if task_type in ("daily", "maa_queue") and custom_task_queue:
            print(f"[+] 正在装配 MAA 桌面版 TaskQueue 任务队列 (共 {len(custom_task_queue)} 项)...")
            platform = account.get("platform", "Official")
            for t_item in custom_task_queue:
                if not t_item.get("IsEnable", True):
                    continue
                t_type = t_item.get("$type", "")

                if t_type == "StartUpTask":
                    # 【MAA 原生触屏账号切换】不读写任何游戏配置文件、无需账密。
                    # 通过 account_name 传入目标账号尾号/标识，MAA 在设备已登录账号
                    # 列表中触屏点击切换 (MAA 官方支持的 StartUp.account_name 能力)。
                    su_params = {
                        "client_type": t_item.get("ClientType", platform),
                        "start_game_enabled": bool(t_item.get("StartGame", True))
                    }
                    switch_key = get_account_switch_key(account)
                    # 优先用户显式配置的 AccountName，其次用账号自身标识尾号
                    explicit = str(t_item.get("AccountName", "")).strip()
                    account_switch_name = explicit or switch_key
                    # 仅当设备当前不是目标账号时才触屏切换；已在目标账号则跳过切换，
                    # 避免 MAA AccountSwitchTask 反复找不到账号管理界面而失败。
                    if account_switch_name and _NEED_ACCOUNT_SWITCH:
                        su_params["account_name"] = account_switch_name
                        print(f"[+] StartUp 触屏切换目标账号: {account_switch_name}")
                    elif account_switch_name:
                        print(f"[+] 设备已在目标账号 [{account_switch_name}]，跳过触屏切换。")
                    asst.append_task("StartUp", su_params)

                elif t_type == "RecruitTask":
                    daily_limit = int(t_item.get("MaxTimes", 4))
                    acc_id = account.get("id")
                    cur_r_stats = db.update_account_recruit_progress(acc_id, 0)
                    today_count = int(cur_r_stats.get("today_count", 0)) if cur_r_stats else 0
                    remaining_quota = max(0, daily_limit - today_count)
                    run_allowance = min(4, remaining_quota)

                    # 官方默认：自动选择 3/4/5★ 干员，时长拉满；仅 6★ 由用户决定是否自动确认
                    select_stars = [3, 4, 5]
                    confirm_stars = [3, 4, 5]
                    if t_item.get("Level6Choose", False):
                        confirm_stars.append(6)

                    r_params = {
                        "refresh": bool(t_item.get("ForceRefresh", True)),
                        "select": select_stars,
                        "confirm": confirm_stars if run_allowance > 0 else [],
                        "times": run_allowance if run_allowance > 0 else 0,
                        "set_time": True,
                        "level3_use_time": 540,
                        "level4_use_time": 540,
                        "extra_tags_mode": 0,
                        "skip_robot": True,
                        "expedite": False
                    }
                    asst.append_task("Recruit", r_params)

                elif t_type == "AnnihilationTask":
                    # 刷剿灭本质是特化的理智作战 (周计划门控)
                    ws = t_item.get("WeeklySchedule", {})
                    day_keys = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                    cur_day = day_keys[datetime.datetime.now().weekday()]
                    if not ws.get(cur_day, False):
                        print(f"[+] 任务 [刷剿灭] 周计划判定：今日 ({cur_day}) 不执行剿灭，安全跳过。")
                        continue
                    stage = str(t_item.get("Stage", "Annihilation")).strip() or "Annihilation"
                    if stage == "Annihilation":
                        # 防御性：先打当期剿灭；若当期不可用 (MAA 找不到剿灭入口即直接结束)，再由常驻龙门市区兜底
                        print(f"[+] 装配剿灭任务: 当期剿灭 (不可用时回退常驻龙门市区 LungmenDowntown)")
                        asst.append_task("Fight", {
                            "stage": "Annihilation",
                            "series": -1, "medicine": 0, "stone": 0,
                            "expiring_medicine": 0, "report_to_penguin": False
                        })
                        asst.append_task("Fight", {
                            "stage": "LungmenDowntown@Annihilation",
                            "series": -1, "medicine": 0, "stone": 0,
                            "expiring_medicine": 0, "report_to_penguin": False
                        })
                    else:
                        print(f"[+] 装配剿灭任务: 指定常驻关卡 {stage}")
                        asst.append_task("Fight", {
                            "stage": stage,
                            "series": -1, "medicine": 0, "stone": 0,
                            "expiring_medicine": 0, "report_to_penguin": False
                        })

                elif t_type == "InfrastTask":
                    mode_str = str(t_item.get("Mode", "Default"))
                    drones_target = t_item.get("UsesOfDrones", "PureGold")
                    dorm_threshold = float(t_item.get("DormThreshold", 50)) / 100.0

                    # 设施列表排序与开关解析
                    default_facilities = ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing", "Training"]
                    room_list = t_item.get("RoomList", [])
                    if room_list and isinstance(room_list, list):
                        facility_targets = [r["Room"] for r in room_list if r.get("IsEnabled", True) and r.get("Room") in default_facilities]
                    else:
                        facility_targets = ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing"]

                    if mode_str == "Rotation":
                        # 队列轮换：跳过控制中枢/发电站/宿舍/办公室
                        asst.append_task("Infrast", {
                            "facility": facility_targets,
                            "mode": 20000,
                            "drones": drones_target,
                            "threshold": dorm_threshold,
                            "dorm_notstationed_enabled": False
                        })
                    elif mode_str == "Custom":
                        # 自定义：读取用户导入的排班文件 (账号维度)
                        fname = str(t_item.get("Filename", "")).strip()
                        plan_file = resolve_infrast_file(fname, account_id=account.get("id"))
                        if not plan_file or not os.path.exists(plan_file):
                            print(f"[!] 自定义基建模式未找到排班文件 (Filename={fname or '空'})，自动回退为常规模式执行。")
                            asst.append_task("Infrast", {
                                "facility": facility_targets,
                                "mode": 0,
                                "drones": drones_target,
                                "threshold": dorm_threshold,
                                "replenish": bool(t_item.get("OriginiumShardAutoReplenishment", False)),
                                "dorm_notstationed_enabled": bool(t_item.get("DormFilterNotStationed", True)),
                                "dorm_trust_enabled": bool(t_item.get("DormTrustEnabled", True)),
                                "reception_message_board": bool(t_item.get("ReceptionMessageBoard", True)),
                                "reception_clue_exchange": bool(t_item.get("ReceptionClueExchange", True)),
                                "reception_send_clue": bool(t_item.get("SendClue", True))
                            })
                        else:
                            plan_idx = calculate_plan_index(plan_file)
                            asst.append_task("Infrast", {
                                "facility": facility_targets,
                                "mode": 10000,
                                "filename": plan_file,
                                "plan_index": plan_idx,
                                "drones": drones_target,
                                "threshold": dorm_threshold,
                                "replenish": bool(t_item.get("OriginiumShardAutoReplenishment", False)),
                                "dorm_notstationed_enabled": bool(t_item.get("DormFilterNotStationed", True)),
                                "dorm_trust_enabled": bool(t_item.get("DormTrustEnabled", True)),
                                "reception_message_board": bool(t_item.get("ReceptionMessageBoard", True)),
                                "reception_clue_exchange": bool(t_item.get("ReceptionClueExchange", True)),
                                "reception_send_clue": bool(t_item.get("SendClue", True))
                            })
                    else:
                        # 常规模式 (算法自动最优换班)
                        asst.append_task("Infrast", {
                            "facility": facility_targets,
                            "mode": 0,
                            "drones": drones_target,
                            "threshold": dorm_threshold,
                            "replenish": bool(t_item.get("OriginiumShardAutoReplenishment", False)),
                            "dorm_notstationed_enabled": bool(t_item.get("DormFilterNotStationed", True)),
                            "dorm_trust_enabled": bool(t_item.get("DormTrustEnabled", True)),
                            "reception_message_board": bool(t_item.get("ReceptionMessageBoard", True)),
                            "reception_clue_exchange": bool(t_item.get("ReceptionClueExchange", True)),
                            "reception_send_clue": bool(t_item.get("SendClue", True))
                        })

                elif t_type == "FightTask":
                    # 检查周计划
                    if t_item.get("UseWeeklySchedule"):
                        ws = t_item.get("WeeklySchedule", {})
                        day_keys = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                        cur_day = day_keys[datetime.datetime.now().weekday()]
                        if not ws.get(cur_day, True):
                            print(f"[+] 任务 [{t_item.get('Name', '理智作战')}] 周计划判定：今日 ({cur_day}) 不在计划内，安全跳过。")
                            continue

                    stage_plan = t_item.get("StagePlan", [])
                    if isinstance(stage_plan, str):
                        stage_plan = [s.strip() for s in stage_plan.split(",") if s.strip()]
                    if not stage_plan:
                        stage_plan = [t_item.get("Stage", "1-7")]

                    for stg in stage_plan:
                        f_params = {
                            "stage": stg,
                            "series": int(t_item.get("Series", 0)),
                            "medicine": int(t_item.get("MedicineCount", 0)) if t_item.get("UseMedicine") else 0,
                            "stone": int(t_item.get("StoneCount", 0)) if t_item.get("UseStone") else 0,
                            "expiring_medicine": 999 if t_item.get("UseExpiringMedicine", True) else 0,
                            "report_to_penguin": False
                        }
                        asst.append_task("Fight", f_params)

                elif t_type == "DepotMaintainTask":
                    if t_item.get("UpdateDepot", True):
                        asst.append_task("Depot")
                    # 依据最近一次仓库识别缓存，仅对「实际缺口」安排作战 (对齐 MAA 库存保持语义)：
                    #   need = 目标库存 - 当前持有；need<=0 直接跳过；need>0 只刷「缺口数量」而非绝对目标。
                    # 避免把已达标的计划也全部空刷一遍。
                    depot_cache = load_account_depot(account.get("id"))
                    only_first = bool(t_item.get("OnlyFirstInsufficientPlan", True))
                    planned = 0
                    for p in t_item.get("PlanList", []):
                        if not (p.get("Stage") and p.get("DropId")):
                            continue
                        target = int(p.get("DropCount", 0) or 0)
                        have = int(depot_cache.get(str(p["DropId"]), 0) or 0)
                        need = target - have
                        if need <= 0:
                            print(f"[+] 库存保持: 材料 {p['DropId']} 已有 {have} >= 目标 {target}，跳过 {p['Stage']}。")
                            continue
                        asst.append_task("Fight", {
                            "stage": p["Stage"],
                            "series": 0,
                            "medicine": int(p.get("MedicineCount", 0)) if p.get("UseMedicine") else 0,
                            "stone": int(p.get("StoneCount", 0)) if p.get("UseStone") else 0,
                            "drops": {str(p["DropId"]): need},
                            "report_to_penguin": False
                        })
                        print(f"[+] 库存保持: 材料 {p['DropId']} 缺口 {need} (目标 {target} / 现有 {have})，安排刷取 {p['Stage']}。")
                        planned += 1
                        if only_first:
                            print("[+] 库存保持: 仅补足首个缺口计划 (OnlyFirstInsufficientPlan)，其余交由下次运行。")
                            break
                    if planned == 0:
                        print("[+] 库存保持: 所有目标材料均已达标，本次无需安排供应链刷取。")

                elif t_type == "MallTask":
                    first_raw = t_item.get("FirstList") or t_item.get("BuyFirst", ["赤金", "招聘许可", "龙门币"])
                    black_raw = t_item.get("BlackList") or t_item.get("Blacklist", ["碳", "家具", "加急许可"])
                    if isinstance(first_raw, str):
                        buy_first = [x.strip() for x in re.split(r"[:：;,；]+", first_raw) if x.strip()]
                    else:
                        buy_first = first_raw
                    if isinstance(black_raw, str):
                        blacklist = [x.strip() for x in re.split(r"[:：;,；]+", black_raw) if x.strip()]
                    else:
                        blacklist = black_raw

                    asst.append_task("Mall", {
                        "shopping": bool(t_item.get("Shopping", True)),
                        "buy_first": buy_first,
                        "blacklist": blacklist,
                        "reserve_max_credit": bool(t_item.get("ReserveMaxCredit", True)),
                        "only_buy_discount": bool(t_item.get("OnlyBuyDiscount", False)),
                        "force_shopping_if_credit_full": bool(t_item.get("ShoppingIgnoreBlackListWhenFull", False)),
                        "visit_friends": bool(t_item.get("VisitFriends", True)),
                        "credit_fight": bool(t_item.get("CreditFight", False)),
                        "formation_index": int(t_item.get("CreditFightFormation", 0))
                    })

                elif t_type == "AwardTask":
                    asst.append_task("Award", {
                        "award": bool(t_item.get("Award", True)),
                        "mail": bool(t_item.get("Mail", True)),
                        "recruit": bool(t_item.get("FreeGacha", True)),
                        "orundum": bool(t_item.get("Orundum", True)),
                        "mining": bool(t_item.get("Mining", True)),
                        "specialaccess": bool(t_item.get("SpecialAccess", True))
                    })

                elif t_type == "UserDataUpdateTask":
                    if t_item.get("UpdateDepot", True):
                        asst.append_task("Depot")
                    if t_item.get("UpdateOperBox", True):
                        asst.append_task("OperBox")

        elif task_type == "daily":
            infrast_cfg = account.get("infrast", {})
            fight_cfg = account.get("fight", {})
            recruit_cfg = account.get("recruit", {})
            mall_cfg = account.get("mall", {})
            award_cfg = account.get("award", {})
    
            infrast_file = resolve_infrast_file(infrast_cfg.get("plan_file", ""), account_id=account.get("id"))
            plan_idx = calculate_plan_index(infrast_file)
            print(f"[+] 基建排班表: {infrast_file}, 启用班次: {plan_idx}")
    
            # 1. 开始唤醒 (与指定账号平台对齐)
            platform = account.get("platform", "Official")
            t1 = asst.append_task("StartUp", {
                "client_type": platform,
                "start_game_enabled": True
            })
    
            # 2. 自动公招 (支持日限额、04:00 重置、刷新优先与严禁加急券)
            recruit_cfg = account.get("recruit", {})
            recruit_enabled = recruit_cfg.get("enabled", True)
            daily_limit = int(recruit_cfg.get("daily_limit", 4))
            acc_id = account.get("id")

            cur_r_stats = db.update_account_recruit_progress(acc_id, 0)
            today_count = int(cur_r_stats.get("today_count", 0)) if cur_r_stats else 0
            remaining_quota = max(0, daily_limit - today_count)
            run_allowance = min(4, remaining_quota)

            print(f"[+] 公招限额计算: 每日上限={daily_limit}次, 今日已用={today_count}次, 本次允许开新槽={run_allowance}次")

            strat = recruit_cfg.get("select_6star_strategy", "notify_user")
            confirm_list = [3, 4, 5] if strat == "notify_user" else [3, 4, 5, 6]

            if recruit_enabled and run_allowance > 0:
                t2 = asst.append_task("Recruit", {
                    "refresh": True,
                    "select": [4, 5, 6],
                    "confirm": confirm_list,
                    "times": run_allowance,
                    "set_time": True,
                    "level3_use_time": 540,
                    "level4_use_time": 540,
                    "expedited": False
                })
            else:
                print("[+] 今日公招配额已达上限，本次仅收取已完成干员与刷新词条，绝不开新槽。")
                t2 = asst.append_task("Recruit", {
                    "refresh": True,
                    "select": [4, 5, 6],
                    "confirm": [],
                    "times": 0,
                    "expedited": False
                })
    
            # 3. 基建换班 (支持自定义排班表模式 VS 原生一键轮换懒癌模式)
            infrast_mode_type = infrast_cfg.get("mode", "custom_plan")
            drones_target = infrast_cfg.get("drones", "Money")
            dorm_threshold = infrast_cfg.get("threshold", 50)
    
            if infrast_mode_type in ("daily_once", "auto_rotation"):
                print(f"[+] 基建换班模式: 【一天一登 / 原生一键轮换模式】 (无需外部排班表)")
                t3 = asst.append_task("Infrast", {
                    "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing"],
                    "mode": 0,
                    "drones": drones_target,
                    "threshold": dorm_threshold,
                    "dorm_not_stationed_enabled": False
                })
            else:
                print(f"[+] 基建换班模式: 【自定义排班表】 ({infrast_file}, 启用班次: {plan_idx})")
                t3 = asst.append_task("Infrast", {
                    "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing"],
                    "mode": 10000,
                    "filename": infrast_file,
                    "plan_index": plan_idx,
                    "drones": drones_target,
                    "threshold": dorm_threshold,
                    "dorm_not_stationed_enabled": infrast_cfg.get("dorm_not_stationed_enabled", False),
                    "dorm_trust_enabled": infrast_cfg.get("dorm_trust_enabled", True),
                    "fiammetta_recovery_enabled": True
                })
    
            # 4. 刷剿灭 (周计划仅周一运行)
            is_monday = (datetime.datetime.now().weekday() == 0)
            annihilation_task = None
            if fight_cfg.get("annihilation_monday", True) and is_monday:
                print("[+] 检测到今天是周一，排入每周剿灭清理任务...")
                annihilation_task = asst.append_task("Fight", {
                    "stage": "Annihilation",
                    "series": -1,
                    "medicine": 0,
                    "stone": 0,
                    "report_to_penguin": False
                })
    
            # 5 & 6. 理智作战 (规范化: 日常库存保持 vs 活动关卡，全局 Auto 连战倍率)
            fight_tasks = []
            fight_mode = fight_cfg.get("mode", "daily_depot_maintain")
            series_multiplier = fight_cfg.get("series", 0)  # 0 为 Auto 自动连战
            expiring_med = 999 if fight_cfg.get("use_expiring_medicine", True) else 0
    
            if fight_mode == "manual_stage":
                # 活动模式: 优先刷手动指定的活动主关卡
                manual_stg = fight_cfg.get("manual_stage", "1-7")
                fallback_stg = fight_cfg.get("fallback_stage", "1-7")
                if manual_stg and manual_stg != fallback_stg:
                    fid1 = asst.append_task("Fight", {
                        "stage": manual_stg,
                        "series": series_multiplier,
                        "medicine": 0,
                        "stone": 0,
                        "expiring_medicine": expiring_med,
                        "report_to_penguin": False
                    })
                    fight_tasks.append((manual_stg, fid1))
                fid2 = asst.append_task("Fight", {
                    "stage": fallback_stg,
                    "series": series_multiplier,
                    "medicine": 0,
                    "stone": 0,
                    "expiring_medicine": expiring_med,
                    "report_to_penguin": False
                })
                fight_tasks.append((fallback_stg, fid2))
            else:
                # 日常模式: 1-7 兜底清空理智 (Auto 连战倍率)
                fallback_stg = fight_cfg.get("fallback_stage", "1-7")
                fid = asst.append_task("Fight", {
                    "stage": fallback_stg,
                    "series": series_multiplier,
                    "medicine": 0,
                    "stone": 0,
                    "expiring_medicine": expiring_med,
                    "report_to_penguin": False
                })
                fight_tasks.append((fallback_stg, fid))
    
            # 7. 信用收支
            t7 = asst.append_task("Mall", {
                "shopping": mall_cfg.get("enabled", True),
                "buy_first": mall_cfg.get("buy_first", ["赤金", "招聘许可", "龙门币"]),
                "blacklist": mall_cfg.get("blacklist", ["碳", "家具", "加急许可"]),
                "reserve_max_credit": mall_cfg.get("reserve_max_credit", True)
            })
    
            # 8. 领取奖励
            t8 = asst.append_task("Award", {
                "award": award_cfg.get("enabled", True),
                "mail": award_cfg.get("mail", True),
                "free_gacha": award_cfg.get("free_gacha", True),
                "orundum": award_cfg.get("orundum", True),
                "mining": award_cfg.get("mining", True),
                "special_access": award_cfg.get("special_access", True)
            })
    
            # 9. 仓库数据全量扫描更新
            t9 = asst.append_task("Depot")
    
            print(f"[+] 规范化任务链已装配: StartUp={t1}, Recruit={t2}, Infrast={t3}, 剿灭(周一)={annihilation_task}, 战斗={fight_tasks}, 信用={t7}, 奖励={t8}, 仓库={t9}")
    
        elif task_type == "startup":
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
    
        elif task_type == "recruit":
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            strat = account.get("recruit", {}).get("select_6star_strategy", "notify_user")
            confirm_list = [3, 4, 5] if strat == "notify_user" else [3, 4, 5, 6]
            asst.append_task("Recruit", {
                "refresh": True, "select": [4, 5, 6], "confirm": confirm_list, "times": 4,
                "set_time": True, "level3_use_time": 540, "level4_use_time": 540, "expedited": False
            })
    
        elif task_type == "infrast":
            infrast_file = resolve_infrast_file(account.get("infrast", {}).get("plan_file", ""), account_id=account.get("id"))
            plan_idx = calculate_plan_index(infrast_file)
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            asst.append_task("Infrast", {
                "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing"],
                "mode": 10000,
                "filename": infrast_file, "plan_index": plan_idx, "drones": "Money", "threshold": 50,
                "dorm_not_stationed_enabled": False
            })
    
        elif task_type == "fight":
            stg = extra_args.get("stage", account.get("fight", {}).get("manual_stage", "1-7"))
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            asst.append_task("Fight", {"stage": stg, "series": 0, "medicine": 0, "stone": 0, "expiring_medicine": 999, "report_to_penguin": False})
    
        elif task_type in ("award", "mall", "mall_award"):
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            mall_cfg = account.get("mall", {})
            asst.append_task("Mall", {
                "shopping": True,
                "buy_first": mall_cfg.get("buy_first", ["赤金", "招聘许可", "龙门币"]),
                "blacklist": mall_cfg.get("blacklist", ["碳", "家具", "加急许可"]),
                "reserve_max_credit": mall_cfg.get("reserve_max_credit", True)
            })
            award_cfg = account.get("award", {})
            asst.append_task("Award", {
                "award": True, "mail": award_cfg.get("mail", True),
                "free_gacha": award_cfg.get("free_gacha", True),
                "orundum": award_cfg.get("orundum", True),
                "mining": award_cfg.get("mining", True),
                "special_access": award_cfg.get("special_access", True)
            })
    
        elif task_type == "depot":
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            asst.append_task("Depot")

        elif task_type == "copilot":
            # 自动战斗参数全部来自运行时入参，不落库 (1:1 对齐 MAA 行为)
            use_list = bool(extra_args.get("use_copilot_list", False))
            formation_on = bool(extra_args.get("formation", True))
            formation_idx = int(extra_args.get("formation_index", 0) or 0)
            support_usage = int(extra_args.get("support_unit_usage", 0))
            add_tr = bool(extra_args.get("add_trust", False))
            ignore_req = bool(extra_args.get("ignore_requirements", False))
            user_add = extra_args.get("user_additional") or []
            loop_times = int(extra_args.get("loop_times", 1) or 1)
            use_pot = bool(extra_args.get("use_sanity_potion", False))

            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})

            copilot_params = {
                "formation": formation_on,
                "support_unit_usage": support_usage,
                "add_trust": add_tr,
                "ignore_requirements": ignore_req,
                "loop_times": loop_times,
                "use_sanity_potion": use_pot
            }
            if formation_on and formation_idx > 0:
                copilot_params["formation_index"] = formation_idx
            if formation_on and isinstance(user_add, list) and user_add:
                copilot_params["user_additional"] = [
                    {"name": op.get("Name") or op.get("name"), "skill": int(op.get("Skill") or op.get("skill", 1))}
                    for op in user_add if (op.get("Name") or op.get("name"))
                ]

            if use_list:
                # 多作业模式：copilot_list 连续执行
                raw_list = extra_args.get("copilot_list") or []
                multi = []
                for item in raw_list:
                    fn = item.get("filename") or ""
                    resolved = resolve_copilot_file(fn, account.get("owner_username"))
                    if resolved and os.path.exists(resolved):
                        mt = {"filename": resolved}
                        if item.get("stage_name"):
                            mt["stage_name"] = item["stage_name"]
                        if item.get("is_raid"):
                            mt["is_raid"] = True
                        multi.append(mt)
                    else:
                        print(f"[!] 多作业模式跳过缺失文件: {fn}")
                if not multi:
                    print("[!] 错误: 多作业模式无有效作业文件，任务中止。")
                    return False
                copilot_params.pop("loop_times", None)  # 多作业模式不使用 loop_times
                copilot_params["copilot_list"] = multi
                print(f"[+] 装配 Copilot 多作业模式 (共 {len(multi)} 个作业)")
            else:
                cop_file = extra_args.get("copilot_file") or extra_args.get("stage") or ""
                resolved_copilot = resolve_copilot_file(cop_file, account.get("owner_username"))
                if not resolved_copilot or not os.path.exists(resolved_copilot):
                    print(f"[!] 错误: Copilot 作业文件不存在: {cop_file} (解析: {resolved_copilot})")
                    return False
                copilot_params["filename"] = resolved_copilot
                print(f"[+] 装配 Copilot 自动战斗任务: {resolved_copilot}")

            asst.append_task("Copilot", copilot_params)
    
        else:
            print(f"[!] 未知任务类型: {task_type}")
            return False
    
        if task_type in ("daily", "depot"):
            # 防御性退出保险库或二级子界面 (若误入保险库则点击左上角返回)
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "input", "tap", "70", "50"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
    
        print("[*] 开始启动 MAA 任务链...")
        asst.start()

        # 后台进程守护线程：高频清理游离孤儿 + 重复主进程治理，防止 OOM
        _reap_stop = threading.Event()
        def _reaper_loop():
            while not _reap_stop.wait(8):
                try:
                    reap_orphan_arknights(pkg)
                except Exception:
                    pass
                # 硬保护：以「裸名主进程数量」判定重复启动 (正常应为 1)。
                # 若 >1，说明发生了重复拉起 (账户切换失败/守护重启)，清理旧副本防 OOM。
                # 不再以进程总数判定 —— 正常的多进程架构本就可能达 4~5 个。
                try:
                    mains = count_arknights_mains(pkg)
                    if mains > 1:
                        print(f"[!] 检测到重复方舟主进程 {mains} 个 (疑似重复启动)，执行主进程治理以防 OOM。", flush=True)
                        reap_orphan_arknights(pkg)
                except Exception:
                    pass
        _reaper = threading.Thread(target=_reaper_loop, daemon=True)
        _reaper.start()

        while asst.running():
            # 收到终止信号（被顶号 / 班次超时）立即中止，不等任务自然跑完
            if _KICKED_OUT:
                print("[!] 检测到被顶号或超时信号，正在中止 MAA 任务链...")
                try:
                    asst.stop()
                except Exception:
                    pass
                break
            time.sleep(2)

        _reap_stop.set()
    
        if _KICKED_OUT:
            print("[✗] 本次班次判定为【被顶号 / 超时】，任务放弃（未完成，不作完成简报）。")
            # 按退出策略收尾：默认关闭客户端与模拟器，避免占用下一班次资源
            on_complete_action = account.get("on_complete", "stop_emu")
            if on_complete_action == "stop_emu":
                execute_exit_action(on_complete_action)
            return False
    
        print("[✔] MAA 任务链执行完毕！")
    
        # 可选同步外部审计日志软/硬链接
        audit_log_dst = os.getenv("AUDIT_LOG_DST")
        if audit_log_dst:
            try:
                src = MAA_DIR / "debug" / "asst.log"
                dst = pathlib.Path(audit_log_dst)
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        os.link(src, dst)
            except Exception:
                pass
    
        # 全套日常完成时分发简报
        if task_type == "daily":
            try:
                inv = account.get("inventory", {})
                is_failed = bool(FAILED_TASKCHAINS)
                status_word = "部分失败" if is_failed else "执行完成"
                sub = f"【明日方舟托管简报】{account.get('name')} 日常流水线{status_word}"
                failed_detail = f"\n- **失败任务**: {', '.join(FAILED_TASKCHAINS)}" if is_failed else ""
                md = f"### 明日方舟每日托管{status_word}\n- **账号**: {account.get('name')} ({account.get('platform', 'Official')})\n- **完成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}{failed_detail}\n- **合成玉**: {inv.get('合成玉', '--')}\n- **至纯源石**: {inv.get('至纯源石', '--')}\n- **龙门币**: {inv.get('龙门币', '--')}\n- **固源岩**: {inv.get('固源岩', '--')}\n- **退出策略**: {account.get('on_complete', 'stop_emu')}"
                notifier.dispatch_account_notify(account, sub, md, event_type="on_daily_summary")
            except Exception:
                pass
            on_complete_action = account.get("on_complete", "stop_emu")
            execute_exit_action(on_complete_action)
        else:
            print(f"[+] 单阶段任务 [{task_type}] 执行完毕！保持模拟器待命，便于在网页大屏即时检查。")
    
        return True
    finally:
        mark_running_task(CURRENT_ACCOUNT_DATA, False)
        release_runner_lock()
    return True

if __name__ == "__main__":
    t_type = sys.argv[1] if len(sys.argv) > 1 else "daily"
    extra = {}
    if len(sys.argv) > 2:
        extra["account_id"] = sys.argv[2]
    if len(sys.argv) > 3:
        arg3 = sys.argv[3]
        if arg3.startswith("{") and arg3.endswith("}"):
            try:
                extra.update(json.loads(arg3))
            except Exception:
                extra["stage"] = arg3
        else:
            extra["stage"] = arg3
            extra["copilot_file"] = arg3

    success = run_task(t_type, extra)
    sys.exit(0 if success else 1)
