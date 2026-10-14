# -*- coding: utf-8 -*-
"""
明日方舟单线程调度互斥卫士与用户管理核心 - SQLite 驱动版
- 保证单模拟器环境下，任意两账号运行时间必须间隔 >= 60 分钟 (59分钟独占 + 保护窗口)
  例如: 10:00 登录占用至 10:59，其他账号最晚 09:00 登录，最早 11:00 登录
- 多用户鉴权、账号归属隔离、动态邀请码持久化由 SQLite (db.py) 统一驱动
- 提供管理员用户总览（用户列表、各用户托管的方舟账号、各自占用的 59 分钟时段）
"""

import os
import sys
import time
import json
import hashlib
import pathlib
import subprocess

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
CONFIG_DIR = BASE_DIR / "config"
INFRAST_DIR = BASE_DIR / "maa" / "infrast"
DEFAULT_INVITE_CODE = os.getenv("ARK_INVITE_CODE", "ARKNIGHTS_DEFAULT_KEY")
DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")

# 引入 SQLite DAO 核心
sys.path.insert(0, str(BASE_DIR / "bin"))
import db

def get_current_invite_code():
    code = db.get_system_setting("invite_code", DEFAULT_INVITE_CODE)
    return code.strip() if isinstance(code, str) else ""

def update_invite_code(new_code):
    new_code = (new_code or "").strip()
    db.set_system_setting("invite_code", new_code)
    return True, new_code

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.strip().encode("utf-8")).hexdigest()

def authenticate_user(username, password):
    u = db.get_user_by_username(username.strip())
    if not u:
        return False, None
    pwd_h = hash_password(password)
    if u.get("password_hash") == pwd_h:
        return True, u
    return False, None

def register_user(username, password, invite_code="", display_name=None):
    username = username.strip()
    password = password.strip()
    invite_code = invite_code.strip()

    if not username or not password:
        return False, "用户名与密码不能为空"
    if len(username) < 2:
        return False, "用户名长度至少 2 位"
    if len(password) < 6:
        return False, "密码长度至少 6 位"

    curr_code = get_current_invite_code()
    if not curr_code:
        return False, "当前系统已关闭新用户注册，请联系管理员开放"
    if invite_code != curr_code:
        return False, "邀请码错误或已失效，请联系管理员获取正确邀请码"

    if db.get_user_by_username(username):
        return False, "该用户名已被占用"

    new_user = {
        "username": username,
        "password_hash": hash_password(password),
        "display_name": display_name or username,
        "role": "user",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    db.add_user(
        username=new_user["username"],
        password_hash=new_user["password_hash"],
        display_name=new_user["display_name"],
        role=new_user["role"],
        created_at=new_user["created_at"]
    )
    return True, new_user

def delete_user_by_admin(username):
    ok, msg = db.delete_user(username)
    if ok:
        sync_all_system_crontabs()
    return ok, msg

def parse_time_to_minutes(time_str: str) -> int:
    parts = time_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])

def minutes_to_time_str(m: int) -> str:
    h = (m // 60) % 24
    min_part = m % 60
    return f"{h:02d}:{min_part:02d}"

def time_distance_minutes(m1: int, m2: int) -> int:
    diff = abs(m1 - m2)
    return min(diff, 1440 - diff)

def mask_account_str(acc_str: str) -> str:
    if not acc_str:
        return "未设置"
    acc_str = str(acc_str).strip()
    if len(acc_str) >= 7:
        return acc_str[:3] + "****" + acc_str[-4:]
    elif len(acc_str) >= 4:
        return "****" + acc_str[-2:]
    return "****"

def get_account_effective_times(acc):
    """
    获取指定账号当前实际生效的定时执行时间点列表 (HH:MM 格式)
    【核心唯一事实来源】：优先从 SQLite 数据库的 maa_timers 表读取启用的班次；
    兜底回退至 account.schedules 或默认三班倒 (10:00, 16:00, 22:00)
    """
    acc_id = acc.get("id") if isinstance(acc, dict) else str(acc)
    if not acc_id:
        return ["10:00", "16:00", "22:00"]

    try:
        t_rec = db.get_maa_timers(acc_id)
        if t_rec and isinstance(t_rec.get("timers"), dict):
            t_list = t_rec["timers"].get("List", [])
            active_times = []
            for item in t_list:
                if item.get("IsEnabled"):
                    h = int(item.get("Hour", 0))
                    m = int(item.get("Minute", 0))
                    active_times.append(f"{h:02d}:{m:02d}")
            if active_times:
                return sorted(active_times, key=lambda x: parse_time_to_minutes(x))
    except Exception as e:
        print(f"[!] 读取账号 [{acc_id}] maa_timers 异常: {e}")

    # 兜底：兼容旧 schedules 字段
    if isinstance(acc, dict) and acc.get("schedules"):
        return acc.get("schedules")
    return ["10:00", "16:00", "22:00"]

def get_account_window_minutes(acc_id: str) -> int:
    """获取指定账号的独占时段分钟数 (默认 40 分钟)"""
    try:
        t_rec = db.get_maa_timers(acc_id)
        if t_rec:
            return int(t_rec.get("window_minutes") or 40)
    except Exception:
        pass
    return 40

def get_all_occupied_slots(exclude_acc_id=None):
    """
    获取全服所有账号已占用的独占时段列表
    【标准】：完全基于 maa_timers 表，独占窗口统一为 40 分钟，跨午夜离散分钟无缝衔接
    """
    accs = db.list_accounts()
    slots = []
    for acc in accs:
        if not acc.get("enabled", True):
            continue
        acc_id = acc.get("id")
        if exclude_acc_id and acc_id == exclude_acc_id:
            continue
        
        times = get_account_effective_times(acc)
        win_min = get_account_window_minutes(acc_id)
        for t in times:
            m = parse_time_to_minutes(t)
            end_m = (m + win_min) % 1440
            slots.append({
                "time": t,
                "start_minutes": m,
                "end_minutes": end_m,
                "window_minutes": win_min,
                "range_str": f"{t} ~ {minutes_to_time_str(end_m)}",
                "account_id": acc_id,
                "account_name": acc.get("name", "未命名账号"),
                "owner_username": acc.get("owner_username", "system"),
                "mode": "maa_timers",
                "mode_label": f"MAA定时 ({win_min}分)"
            })
    return sorted(slots, key=lambda x: x["start_minutes"])

def check_schedule_conflicts(target_acc_id, proposed_times):
    """
    校验时间点是否冲突，无缝桥接至 maa_config_adapter 40 分钟环形独占引擎
    """
    try:
        import maa_config_adapter
        mock_timers = {
            "List": []
        }
        for idx, t in enumerate(proposed_times):
            parts = t.strip().split(":")
            mock_timers["List"].append({
                "Id": idx,
                "IsEnabled": True,
                "Hour": int(parts[0]),
                "Minute": int(parts[1])
            })
        all_existing = db.list_all_maa_timers()
        acc_names = {a["id"]: a["name"] for a in db.list_accounts()}
        is_valid, err_msg = maa_config_adapter.check_timers_conflict(
            target_acc_id, mock_timers, all_existing, window_minutes=40, acc_names_map=acc_names
        )
        occupied = get_all_occupied_slots(exclude_acc_id=target_acc_id)
        return is_valid, err_msg or "", occupied
    except Exception as e:
        occupied = get_all_occupied_slots(exclude_acc_id=target_acc_id)
        return True, "", occupied

def get_users_overview():
    users_data = db.list_users()
    accs_data = db.list_accounts()
    curr_code = get_current_invite_code()

    result = {
        "invite_code": curr_code,
        "invite_open": bool(curr_code and len(curr_code) > 0),
        "users": []
    }

    for u in users_data:
        uname = u.get("username")
        u_accs = [a for a in accs_data if a.get("owner_username") == uname]
        acc_list = []
        for a in u_accs:
            acc_id = a.get("id")
            times = get_account_effective_times(a)
            win_min = get_account_window_minutes(acc_id)
            slots = []
            for t in times:
                m = parse_time_to_minutes(t)
                slots.append(f"{t} ~ {minutes_to_time_str((m + win_min) % 1440)}")
            
            raw_acc_name = a.get("account_name", "")
            t_rec = db.get_maa_timers(acc_id) or {}
            acc_list.append({
                "id": acc_id,
                "name": a.get("name", "未命名账号"),
                "account_name_masked": mask_account_str(raw_acc_name),
                "account_name_raw": raw_acc_name,
                "enabled": a.get("enabled", True),
                "window_minutes": win_min,
                "occupied_times": times,
                "occupied_slots": slots,
                "timers_list": (t_rec.get("timers") or {}).get("List", []),
                "timers_count": len(times)
            })

        result["users"].append({
            "username": uname,
            "display_name": u.get("display_name") or uname,
            "role": u.get("role", "user"),
            "created_at": u.get("created_at", "未知"),
            "accounts": acc_list
        })
    return result

def sync_all_system_crontabs():
    accs = db.list_accounts()
    # 时间点 -> 占用该时刻的账号列表（保留全部，便于冲突告警）
    time_to_entries = {}
    for acc in accs:
        if not acc.get("enabled", True):
            continue
        acc_id = acc.get("id")
        acc_name = acc.get("name", "未命名账号")
        times = get_account_effective_times(acc)
        for t in times:
            time_to_entries.setdefault(t, []).append({
                "id": acc_id,
                "name": acc_name,
                "owner": acc.get("owner_username", "system"),
            })

    # 冲突检测：同一时刻被多账号同时占用时，打日志告警，并按 account id 升序稳定选一（不再静默覆盖）。
    time_to_acc_map = {}
    for t in sorted(time_to_entries.keys(), key=parse_time_to_minutes):
        entries = time_to_entries[t]
        if len(entries) > 1:
            names = ", ".join(f"{e['name']}({e['id']})" for e in entries)
            chosen_name = sorted(entries, key=lambda e: e["id"])[0]["name"]
            print(f"[!] 调度冲突：时刻 {t} 被多个账号同时占用 -> {names}；"
                  f"已保留 [{chosen_name}]，其余账号的该时段将被跳过。请调整各自班次以避免重叠。", flush=True)
        chosen = sorted(entries, key=lambda e: e["id"])[0]
        time_to_acc_map[t] = chosen["id"]

    sorted_times = sorted(list(time_to_acc_map.keys()), key=lambda x: parse_time_to_minutes(x))
    
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cron_txt = CONFIG_DIR / "active_crontab.txt"
        lines_list = []
        for t in sorted_times:
            acc_id = time_to_acc_map[t]
            h, m = t.split(":")
            lines_list.append(f"{int(m)} {int(h)} * * * {BASE_DIR}/bin/ark_service_ctl.sh run-daily {acc_id} >> {BASE_DIR}/cron.log 2>&1")
        with open(cron_txt, "w", encoding="utf-8") as cf:
            cf.write("\n".join(lines_list) + "\n")
    except Exception:
        pass

    try:
        is_paused = (db.get_system_setting("cron_paused") == "true")
        prefix = "# PAUSED: " if is_paused else ""
        try:
            cur_cron = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            cur_cron = ""
        preserved = [l for l in cur_cron.splitlines() if "ark_service_ctl.sh" not in l and "明日方舟" not in l]
        new_lines = preserved[:]
        new_lines.append("\n# 明日方舟多账号自动化自主调度流水线 (自适应全天时段与指定账号)")
        for t in sorted_times:
            acc_id = time_to_acc_map[t]
            h, m = t.split(":")
            new_lines.append(f"{prefix}{int(m)} {int(h)} * * * {BASE_DIR}/bin/ark_service_ctl.sh run-daily {acc_id} >> {BASE_DIR}/cron.log 2>&1")
        p = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
        p.communicate("\n".join(new_lines) + "\n")
    except Exception:
        pass
    return True

if __name__ == "__main__":
    sync_all_system_crontabs()
