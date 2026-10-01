#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
import datetime
import pathlib
import subprocess
import re
import fcntl
import tempfile
import xml.etree.ElementTree as ET
import notifier
import db

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
CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
DEPOT_FILE = MAA_DIR / "data" / "DepotData.json"
OPER_BOX_FILE = MAA_DIR / "data" / "OperBoxData.json"
EVENT_6STAR_FILE = pathlib.Path(os.getenv("EVENT_6STAR_FILE", str(BASE_DIR / "data" / "recruit_6star_event.json")))
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
ADB_BIN = os.getenv("ADB_BIN", "adb")
ADB_TARGET = "127.0.0.1:5555"

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

sys.path.insert(0, str(PYTHON_DIR))

from asst.asst import Asst
from asst.utils import Message, InstanceOptionType

OWNED_6STAR_SET = set()
CURRENT_ACCOUNT_DATA = {}

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

def resolve_infrast_file(plan_filename, owner_username=None):
    if not plan_filename:
        plan_filename = "default_plan.json"
    if not plan_filename.endswith(".json"):
        plan_filename += ".json"
    
    owner = owner_username or (CURRENT_ACCOUNT_DATA.get("owner_username") if CURRENT_ACCOUNT_DATA else DEFAULT_ADMIN_USER)
    
    # 1. 严格在所有者专属隔离目录查找
    target = INFRAST_DIR / owner / plan_filename
    if target.exists():
        return str(target)
    
    # 2. 检查公共共享根目录 (绝不向其他用户私有目录借读！)
    public_target = INFRAST_DIR / plan_filename
    if public_target.exists():
        return str(public_target)

    # 若不存在，返回用户隔离目录的预期路径
    return str(target)

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

@Asst.CallBackType
def log_callback(msg, details, arg):
    global CURRENT_RECRUIT_CONFIRMED
    m = Message(msg)
    try:
        d = json.loads(details.decode('utf-8'))
    except Exception:
        d = details.decode('utf-8', errors='ignore')
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] [{m.name}] {d}")

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
    try:
        with open(infrast_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        now_str = time.strftime("%H:%M")
        plans = data.get("plans", [])
        for idx, plan in enumerate(plans):
            for period in plan.get("period", []):
                if len(period) == 2:
                    start, end = period[0], period[1]
                    if start <= end:
                        is_match = (start <= now_str <= end) if end in ("23:59", "24:00") else (start <= now_str < end)
                        if is_match:
                            print(f"[+] 当前时间 {now_str} 匹配到排班: {plan.get('name', idx)} (index={idx})")
                            return idx
                    else:
                        if now_str >= start or now_str < end:
                            print(f"[+] 当前时间 {now_str} 跨午夜匹配到排班: {plan.get('name', idx)} (index={idx})")
                            return idx
    except Exception as e:
        print(f"[!] 排班表时间匹配异常: {e}")

    hour = time.localtime().tm_hour
    if 10 <= hour < 16:
        idx = 0
    elif 16 <= hour < 22:
        idx = 1
    else:
        idx = 2
    print(f"[+] 按小时 ({hour}:00) 匹配到默认班次: index={idx}")
    return idx


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

def sanitize_profile_sdk_prefs(xml_path, expected_phone):
    if not xml_path or not os.path.exists(str(xml_path)):
        return
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(xml_path))
        root = tree.getroot()
        matched_hg_ids = set()
        modified = False
        for s in root.findall('string'):
            name = s.attrib.get('name')
            if name == 'USER_CACHE' and s.text:
                try:
                    users = json.loads(s.text)
                    filtered = [u for u in users if u.get('phone') == expected_phone]
                    for u in filtered:
                        if 'hgId' in u:
                            matched_hg_ids.add(str(u['hgId']))
                    if len(filtered) != len(users):
                        s.text = json.dumps(filtered)
                        modified = True
                except Exception:
                    pass
        for s in root.findall('string'):
            name = s.attrib.get('name')
            if name == 'DEVICE_CACHE' and s.text and matched_hg_ids:
                try:
                    devs = json.loads(s.text)
                    filtered_devs = [d for d in devs if str(d.get('hgId')) in matched_hg_ids]
                    if len(filtered_devs) != len(devs):
                        s.text = json.dumps(filtered_devs)
                        modified = True
                except Exception:
                    pass
        if modified:
            tree.write(str(xml_path), encoding='utf-8', xml_declaration=True)
    except Exception as e:
        print(f"[!] 纯净化 SDK Preferences 异常: {e}")

def get_device_active_account_phone(pkg="com.hypergryph.arknights"):
    """
    直接从设备底层提取当前实际生效的登录账号手机号掩码
    优先从官方 SDK 底层 HypergryphSdkPreferences.xml 的 USER_CACHE 读取；
    若未检出则退回 playerprefs.xml 的 HGSDKV2_USERNAME。
    """
    try:
        # 1. 优先读取官方 Hypergryph SDK 会话
        res_sdk = subprocess.run([
            ADB_BIN, "-s", ADB_TARGET, "shell",
            f"cat /data/data/{pkg}/shared_prefs/HypergryphSdkPreferences.xml 2>/dev/null || true"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        if res_sdk.stdout and "USER_CACHE" in res_sdk.stdout:
            for line in res_sdk.stdout.splitlines():
                if "USER_CACHE" in line:
                    m = re.search(r'>(\[.+\])<', line)
                    if m:
                        raw_json = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                        users = json.loads(raw_json)
                        if users and isinstance(users, list) and len(users) > 0 and "phone" in users[0]:
                            return str(users[0]["phone"]).strip()

        # 2. 备用读取 Unity 运行时 playerprefs.xml
        res = subprocess.run([
            ADB_BIN, "-s", ADB_TARGET, "shell",
            f"cat /data/data/{pkg}/shared_prefs/com.hypergryph.arknights.v2.playerprefs.xml 2>/dev/null || true"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        for line in res.stdout.splitlines():
            if "HGSDKV2_USERNAME" in line:
                m = re.search(r'>([^<]+)<', line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None

def get_app_uid(pkg="com.hypergryph.arknights"):
    try:
        res = subprocess.run([
            ADB_BIN, "-s", ADB_TARGET, "shell",
            f"dumpsys package {pkg} 2>/dev/null | grep userId= | head -n 1"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        m = re.search(r'userId=(\d+)', res.stdout)
        if m:
            return f"u0_a{int(m.group(1)) % 100000}"
    except Exception:
        pass
    return "u0_a116"

def execute_exit_action(on_complete):
    kill_rogue_browsers()
    pkg = "com.bilibili.arknights" if CURRENT_ACCOUNT_DATA.get("platform") == "Bilibili" else "com.hypergryph.arknights"
    
    # 退出前安全备份：严格核验设备内真实生效的账号才允许落盘，严禁覆盖其他账号！
    try:
        ensure_adb_root()
        device_phone = get_device_active_account_phone(pkg)
        target_phone = str(CURRENT_ACCOUNT_DATA.get("account_name", "")).strip()
        target_masked = f"{target_phone[:3]}****{target_phone[-4:]}" if len(target_phone) >= 7 else target_phone
        curr_id = CURRENT_ACCOUNT_DATA.get("id")

        if curr_id and device_phone == target_masked:
            prof_dest = PROFILES_DIR / curr_id / "shared_prefs"
            prof_dest.mkdir(parents=True, exist_ok=True)
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "pull", f"/data/data/{pkg}/shared_prefs/.", str(prof_dest)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[+] 任务结束：已安全同步账号 [{CURRENT_ACCOUNT_DATA.get('name')}] 最新会话至独立存档。")
    except Exception as e:
        print(f"[!] 退出会话归档跳过: {e}")

    print(f"\n[*] 任务完成，正在执行退出动作: [{on_complete}]...")
    if on_complete == "stop_emu":
        print("[+] 正在关闭明日方舟客户端...")
        subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[+] 正在停止安卓模拟器 (ReDroid) 容器以完全释放 4GB+ 内存与 GPU...")
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([ADB_BIN, "disconnect", ADB_TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[✔] 模拟器已完全停止，硬件资源已彻底释放！")
    elif on_complete == "close_game":
        print("[+] 正在关闭明日方舟客户端并保持模拟器运行...")
        subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[✔] 客户端已退出，模拟器保持待命状态。")
    elif on_complete == "keep_running":
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

def auto_login_arknights(account):
    """
    全自动首次登入流程：适用于 Web 注册的全新账号或 Session 已经失效的账号。
    通过 Android 原生 UIAutomator 抓取控件并注入账号与密码，建立永久 Session 会话。
    """
    phone = str(account.get("account_name", "")).strip()
    password = str(account.get("game_password", "")).strip()
    acc_name = account.get("name", "未命名账号")
    acc_id = account.get("id")
    pkg = "com.bilibili.arknights" if account.get("platform") == "Bilibili" else "com.hypergryph.arknights"

    if not phone or not password:
        print(f"[!] 账号 [{acc_name}] 缺少绑定的手机号或密码，无法执行全自动登入！")
        return False

    phone_masked = f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone
    print(f"[*] 启动账号 [{acc_name}] ({phone_masked}) 全自动登入引擎...")
    ensure_adb_root()

    # 1. 关键优化：绝不删除 playerprefs.xml！保留画质与热更分包资源标记，仅重置官方 SDK 为干净的未登入态
    clean_sdk_xml = '''<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="DEVICE_CACHE">[]</string>
    <string name="USER_CACHE">[]</string>
    <boolean name="cache_account_times" value="true" />
    <string name="HypergryphUserProtocol">1</string>
    <boolean name="IS_AUTO_LOGIN" value="false" />
</map>'''

    # 若设备内缺失基础 playerprefs.xml，从任意已有档案复制一份作为基底，防止弹出完整资源确认框
    dev_pref_chk = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"test -f /data/data/{pkg}/shared_prefs/com.hypergryph.arknights.v2.playerprefs.xml && echo YES"], stdout=subprocess.PIPE, text=True)
    if "YES" not in dev_pref_chk.stdout:
        base_pref = None
        for _pf in sorted(PROFILES_DIR.glob("*/shared_prefs/com.hypergryph.arknights.v2.playerprefs.xml")):
            base_pref = _pf
            break
        if base_pref and base_pref.exists():
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "push", str(base_pref), f"/data/data/{pkg}/shared_prefs/com.hypergryph.arknights.v2.playerprefs.xml"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 推送重置后的 SDK 登录凭据
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
        tf.write(clean_sdk_xml)
        tmp_sdk_path = tf.name
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "push", tmp_sdk_path, f"/data/data/{pkg}/shared_prefs/HypergryphSdkPreferences.xml"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.unlink(tmp_sdk_path)
    except Exception:
        pass

    # 修复属主权限
    uid = get_app_uid(pkg)
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"chown -R {uid}:{uid} /data/data/{pkg}/shared_prefs/ && chmod -R 777 /data/data/{pkg}/shared_prefs/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 停止并重新启动游戏
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    print("[+] 正在唤醒明日方舟客户端进入登录流程...")
    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "start", "-n", f"{pkg}/com.u8.sdk.U8UnityContext"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    start_time = time.time()
    login_submitted = False
    max_wait = 180

    while time.time() - start_time < max_wait:
        time.sleep(3)
        xml = dump_ui_xml()
        if not xml:
            continue

        # 0. 兜底处理可能弹出的用户协议/隐私政策提示 (精确匹配按钮，杜绝匹配大段正文)
        agree_btn = find_node_bounds(xml, text="同意并继续", exact=True) or find_node_bounds(xml, text="同意", exact=True)
        if agree_btn:
            print(f"[+] 检测到用户协议弹窗，精准点击【同意并继续】: {agree_btn}...")
            tap_screen(*agree_btn)
            time.sleep(2)
            continue
        elif "用户协议和游戏隐私政策提示" in xml:
            print("[+] 检出协议弹窗特征，点击默认按钮区域 (812, 522)...")
            tap_screen(812, 522)
            time.sleep(2)
            continue

        # 0.1 兜底处理下载资源提示
        res_btn = find_node_bounds(xml, text="确认", exact=True)
        if res_btn and "下载" in xml:
            print(f"[+] 检测到资源确认弹窗，点击确认: {res_btn}...")
            tap_screen(*res_btn)
            time.sleep(2)
            continue

        # 1. 密码登录表单已显示
        pwd_box = find_node_bounds(xml, resource_id="hg_login_view_edit_text_input_password")
        acc_box = find_node_bounds(xml, resource_id="hg_login_view_edit_text_input_account")

        if pwd_box and acc_box and not login_submitted:
            print(f"[+] 识别到官方 SDK 凭据输入界面，正在自动输入账号与密码...")
            clean_btn = find_node_bounds(xml, resource_id="hg_login_view_button_clean_input_account")
            if clean_btn:
                tap_screen(*clean_btn)
                time.sleep(0.3)
            tap_screen(*acc_box)
            time.sleep(0.3)
            # 彻底清空可能存在的残余字符
            for _ in range(15):
                keyevent_press(67)
            input_text_safe(phone)
            time.sleep(0.3)
            keyevent_press(4)  # 关闭弹出软键盘
            time.sleep(0.5)

            # 聚焦密码输入框并输入
            tap_screen(*pwd_box)
            time.sleep(0.3)
            for _ in range(25):
                keyevent_press(67)
            input_text_safe(password)
            time.sleep(0.3)
            keyevent_press(4)  # 关闭弹出软键盘
            time.sleep(0.5)

            # 勾选用户协议
            xml_cur = dump_ui_xml()
            agree_cb = find_node_bounds(xml_cur, resource_id="hg_login_view_check_box_check_agreement")
            if agree_cb:
                tap_screen(*agree_cb)
            else:
                tap_screen(450, 438)
            time.sleep(0.3)

            # 点击登录
            login_btn = find_node_bounds(xml_cur, resource_id="hg_login_view_button_password_login")
            if login_btn:
                tap_screen(*login_btn)
            else:
                tap_screen(640, 517)

            print("[+] 凭据提交完成，正在等待服务器认证与 Token 下发...")
            login_submitted = True
            time.sleep(6)
            continue

        # 2. 账号管理弹窗中点击“登录其他账号”
        other_btn = find_node_bounds(xml, resource_id="hg_login_view_button_other_login") or find_node_bounds(xml, text="登录其他账号", exact=True)
        if other_btn and not login_submitted:
            print("[+] 点击【登录其他账号】...")
            tap_screen(*other_btn)
            time.sleep(1)
            continue

        # 3. 验证码页面切换为“密码登录”
        use_pwd_btn = find_node_bounds(xml, resource_id="hg_login_view_button_use_password_login") or find_node_bounds(xml, text="密码登录", exact=True)
        if use_pwd_btn and not login_submitted:
            print("[+] 切换为【密码登录】模式...")
            tap_screen(*use_pwd_btn)
            time.sleep(1)
            continue

        # 4. 如果已经提交登录，检测是否已登录成功并拉取归档
        if login_submitted:
            if find_node_bounds(xml, text="活动公告") or find_node_bounds(xml, text="系统公告"):
                print("[+] 检测到登录公告，关闭公告窗口...")
                tap_screen(940, 104)
                time.sleep(2)

            chk = subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"cat /data/data/{pkg}/shared_prefs/HypergryphSdkPreferences.xml 2>/dev/null || true"], stdout=subprocess.PIPE, text=True)
            if "USER_CACHE" in chk.stdout and (phone[-4:] in chk.stdout if len(phone)>=4 else True):
                print(f"[✔] 账号 [{acc_name}] 认证成功！正在导出 Session 会话存档...")
                time.sleep(2)
                target_dir = PROFILES_DIR / acc_id / "shared_prefs"
                target_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run([ADB_BIN, "-s", ADB_TARGET, "pull", f"/data/data/{pkg}/shared_prefs/.", str(target_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                sanitize_profile_sdk_prefs(target_dir / "HypergryphSdkPreferences.xml", phone_masked)
                (PROFILES_DIR / "current_profile.txt").write_text(acc_id)
                print(f"[✔] 账号 [{acc_name}] 专属会话存档建立完成！后续调度将享受 2 秒无感热换。")
                return True

        # 5. 若在封面，点击右下角“账号管理”唤起 SDK 对话框
        if not login_submitted and not pwd_box:
            tap_screen(923, 662)

    print(f"[!] 账号 [{acc_name}] 自动登录超时，请确认账号密码是否正确。")
    return False

    print(f"[!] 账号 [{acc_name}] 自动登录超时，请确认账号密码是否正确。")
    return False

def switch_game_profile(account_or_id):
    if not account_or_id:
        return
    if isinstance(account_or_id, dict):
        account = account_or_id
        target_acc_id = account.get("id")
    else:
        target_acc_id = account_or_id
        account = get_account_by_id(target_acc_id)
        if not account:
            account = {"id": target_acc_id, "name": target_acc_id}

    acc_name = account.get("name", "未命名账号")
    pkg = "com.bilibili.arknights" if account.get("platform") == "Bilibili" else "com.hypergryph.arknights"
    target_phone = str(account.get("account_name", "")).strip()
    target_masked = f"{target_phone[:3]}****{target_phone[-4:]}" if len(target_phone) >= 7 else target_phone

    ensure_adb_root()

    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        target_prof = PROFILES_DIR / target_acc_id / "shared_prefs"
        state_file = PROFILES_DIR / "current_profile.txt"

        # 1. 严格检查设备当前真实生效的账号手机号 (绝不盲信本地文本文件)
        device_phone = get_device_active_account_phone(pkg)

        # 2. 安全备份上一账号：仅当设备当前账号与某合法账号完全匹配且非目标账号时才允许落盘
        if device_phone and device_phone != target_masked:
            for other_acc in db.list_accounts():
                o_phone = str(other_acc.get("account_name", "")).strip()
                o_masked = f"{o_phone[:3]}****{o_phone[-4:]}" if len(o_phone) >= 7 else o_phone
                if o_masked == device_phone:
                    other_dir = PROFILES_DIR / other_acc["id"] / "shared_prefs"
                    other_dir.mkdir(parents=True, exist_ok=True)
                    subprocess.run([ADB_BIN, "-s", ADB_TARGET, "pull", f"/data/data/{pkg}/shared_prefs/.", str(other_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    sanitize_profile_sdk_prefs(other_dir / "HypergryphSdkPreferences.xml", o_masked)
                    print(f"[+] 设备原账号 [{other_acc.get('name')}] ({device_phone}) 最新会话已校验归档并纯净化。")
                    break

        # 3. 检查设备当前是否已经完全处于目标账号环境
        if device_phone == target_masked and target_prof.exists():
            print(f"[+] 设备环境经底层实时校验已匹配目标账号: [{acc_name}] ({device_phone})")
            state_file.write_text(target_acc_id)
            return

        print(f"[*] 正在为任务环境切换至目标账号: [{acc_name}] (ID: {target_acc_id})...")

        # 4. 停止客户端
        subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "am", "force-stop", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        # 5. 校验目标账号本地存档完整性
        has_valid_profile = False
        if target_prof.exists():
            sdk_pref_file = target_prof / "HypergryphSdkPreferences.xml"
            if sdk_pref_file.exists():
                sanitize_profile_sdk_prefs(sdk_pref_file, target_masked)
                txt_sdk = sdk_pref_file.read_text(encoding="utf-8", errors="ignore")
                if target_masked in txt_sdk:
                    has_valid_profile = True
                else:
                    print(f"[!] 账号 [{acc_name}] 本地 SDK 存档未包含目标手机号，判定为失效。")
            else:
                pref_file = target_prof / "com.hypergryph.arknights.v2.playerprefs.xml"
                if pref_file.exists() and target_masked in pref_file.read_text(encoding="utf-8", errors="ignore"):
                    has_valid_profile = True

        # 6. 若有有效存档，全量推入
        if has_valid_profile:
            print(f"[+] 正在装载账号 [{acc_name}] 的独立会话存档...")
            # 关键：先清空设备旧缓存，杜绝双账号文件混合污染
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"rm -rf /data/data/{pkg}/shared_prefs/*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "push", f"{str(target_prof)}/.", f"/data/data/{pkg}/shared_prefs/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 关键：修复应用 UID 属主与权限
            uid = get_app_uid(pkg)
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", f"chown -R {uid}:{uid} /data/data/{pkg}/shared_prefs/ && chmod -R 777 /data/data/{pkg}/shared_prefs/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 注入后复验
            recheck = get_device_active_account_phone(pkg)
            if recheck == target_masked:
                state_file.write_text(target_acc_id)
                print(f"[✔] 账号 [{acc_name}] 独立环境加载与校验完成 ({recheck})！")
                return
            else:
                print(f"[!] 注入后校验未达预期 (设备: {recheck}, 期望: {target_masked})，转入自动登录...")

        # 7. 若无有效本地会话，启动自动登入
        print(f"[*] 账号 [{acc_name}] 启动全自动账密登入及存档初始化...")
        success = auto_login_arknights(account)
        if not success:
            print(f"[!] 账号 [{acc_name}] 自动登录未达预期，尝试以常规唤醒模式继续...")
    except Exception as e:
        print(f"[!] 切换账号登录环境跳过: {e}")

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
    global CURRENT_RECRUIT_CONFIRMED
    CURRENT_RECRUIT_CONFIRMED = 0
    mark_running_task(account, True)

    try:
        switch_game_profile(account)
        acc_name = account.get("name", "默认账号")
        print(f"[*] 启动任务: {task_type} · 目标账号: [{acc_name}] (ID: {account.get('id')})")

        load_owned_6stars(account.get("id"))
    
        print(f"[*] 正在初始化 MAA 官方核心引擎: {MAA_DIR}")
        Asst.load(path=MAA_DIR)
        asst = Asst(callback=log_callback)
    
        asst.set_instance_option(InstanceOptionType.touch_type, "adb")
    
        print(f"[*] 正在连接 ADB ({ADB_TARGET})...")
        if not asst.connect(ADB_BIN, ADB_TARGET):
            print("[*] ADB 未直连，尝试自动唤醒模拟器容器...")
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)
            subprocess.run([ADB_BIN, "connect", ADB_TARGET], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not asst.connect(ADB_BIN, ADB_TARGET):
                print("[!] 模拟器唤醒后依然无法连接，任务中止")
                return False
    
        print(f"[+] ADB 连接就绪，配置任务队列: {task_type}")
    
        if task_type == "daily":
            infrast_cfg = account.get("infrast", {})
            fight_cfg = account.get("fight", {})
            recruit_cfg = account.get("recruit", {})
            mall_cfg = account.get("mall", {})
            award_cfg = account.get("award", {})
    
            infrast_file = resolve_infrast_file(infrast_cfg.get("plan_file", "default_plan.json"))
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
                    "dorm_not_stationed_enabled": True
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
                    "dorm_not_stationed_enabled": infrast_cfg.get("dorm_not_stationed_enabled", True),
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
            infrast_file = resolve_infrast_file(account.get("infrast", {}).get("plan_file", "default_plan.json"))
            plan_idx = calculate_plan_index(infrast_file)
            asst.append_task("StartUp", {"client_type": account.get("platform", "Official"), "start_game_enabled": True})
            asst.append_task("Infrast", {
                "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing"],
                "mode": 10000,
                "filename": infrast_file, "plan_index": plan_idx, "drones": "Money", "threshold": 50,
                "dorm_not_stationed_enabled": True
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
    
        else:
            print(f"[!] 未知任务类型: {task_type}")
            return False
    
        if task_type in ("daily", "depot"):
            # 防御性退出保险库或二级子界面 (若误入保险库则点击左上角返回)
            subprocess.run([ADB_BIN, "-s", ADB_TARGET, "shell", "input", "tap", "70", "50"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
    
        print("[*] 开始启动 MAA 任务链...")
        asst.start()
    
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
                sub = f"【明日方舟托管简报】{account.get('name')} 日常流水线完成"
                md = f"### 明日方舟每日托管执行完成\n- **账号**: {account.get('name')} ({account.get('platform', 'Official')})\n- **完成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n- **合成玉**: {inv.get('合成玉', '--')}\n- **至纯源石**: {inv.get('至纯源石', '--')}\n- **龙门币**: {inv.get('龙门币', '--')}\n- **固源岩**: {inv.get('固源岩', '--')}\n- **退出策略**: {account.get('on_complete', 'stop_emu')}"
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
        extra["stage"] = sys.argv[3]

    success = run_task(t_type, extra)
    sys.exit(0 if success else 1)
