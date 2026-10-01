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
    inf = acc.get("infrast", {})
    mode = inf.get("mode", "custom_plan")
    if mode in ("daily_once", "auto_rotation"):
        single_time = inf.get("daily_single_time", "16:00")
        return [single_time]

    plan_file = inf.get("plan_file", "default_plan.json")
    if not plan_file.endswith(".json"):
        plan_file += ".json"
    
    owner = acc.get("owner_username", DEFAULT_ADMIN_USER)
    # 严格在所有者专属隔离目录查找，绝不向他人私有目录借读
    p_path = INFRAST_DIR / owner / plan_file
    if not p_path.exists():
        p_path = INFRAST_DIR / plan_file

    if p_path.exists():
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)
            times = []
            for plan in p_data.get("plans", []):
                periods = plan.get("period", [])
                if periods and len(periods[0]) > 0:
                    times.append(periods[0][0])
            if times:
                return times
        except Exception:
            pass

    return acc.get("schedules", ["16:00"])

def get_all_occupied_slots(exclude_acc_id=None):
    accs = db.list_accounts()
    slots = []
    for acc in accs:
        if not acc.get("enabled", True):
            continue
        if exclude_acc_id and acc.get("id") == exclude_acc_id:
            continue
        times = get_account_effective_times(acc)
        mode = acc.get("infrast", {}).get("mode", "custom_plan")
        mode_label = "一天一登" if mode in ("daily_once", "auto_rotation") else "排班表"
        for t in times:
            m = parse_time_to_minutes(t)
            end_m = (m + 59) % 1440
            slots.append({
                "time": t,
                "start_minutes": m,
                "end_minutes": end_m,
                "range_str": f"{t} ~ {minutes_to_time_str(end_m)}",
                "account_id": acc.get("id"),
                "account_name": acc.get("name", "未命名账号"),
                "owner_username": acc.get("owner_username", "system"),
                "mode": mode,
                "mode_label": mode_label
            })
    return sorted(slots, key=lambda x: x["start_minutes"])

def check_schedule_conflicts(target_acc_id, proposed_times):
    occupied = get_all_occupied_slots(exclude_acc_id=target_acc_id)
    
    # 1. 检查自身提交的时间点之间是否冲突 (间隔 < 60 分钟)
    prop_minutes = []
    for t in proposed_times:
        m = parse_time_to_minutes(t)
        for pm in prop_minutes:
            dist = time_distance_minutes(m, pm)
            if dist < 60:
                return False, f"排班内部冲突：设置的时间点 [{t}] 与 [{minutes_to_time_str(pm)}] 间隔仅 {dist} 分钟（不足 60 分钟），任务之间必须间隔至少 60 分钟！", occupied
        prop_minutes.append(m)

    # 2. 检查与其他账号的预约时段是否冲突
    for t in proposed_times:
        m = parse_time_to_minutes(t)
        for occ in occupied:
            dist = time_distance_minutes(m, occ["start_minutes"])
            if dist < 60:
                acc_name = occ["account_name"]
                occ_range = occ["range_str"]
                msg = (
                    f"⚠️ 时段冲突：您设置的时间 [{t}] 与账号 [{acc_name}] 的独占时段 [{occ_range}] 间隔仅 {dist} 分钟！\n"
                    f"单模拟器规则：账号登录后 59 分钟内不可再次登录，两次执行之间至少保留 60 分钟安全间隔。\n"
                    f"请调整时间以避开该时段。"
                )
                return False, msg, occupied

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
            times = get_account_effective_times(a)
            slots = []
            for t in times:
                m = parse_time_to_minutes(t)
                slots.append(f"{t} ~ {minutes_to_time_str((m + 59) % 1440)}")
            
            raw_acc_name = a.get("account_name", "")
            raw_pwd = a.get("game_password", "")
            acc_list.append({
                "id": a.get("id"),
                "name": a.get("name", "未命名账号"),
                "account_name_masked": mask_account_str(raw_acc_name),
                "account_name_raw": raw_acc_name,
                "has_password": bool(raw_pwd),
                "enabled": a.get("enabled", True),
                "infrast_mode": a.get("infrast", {}).get("mode", "custom_plan"),
                "plan_file": a.get("infrast", {}).get("plan_file", ""),
                "daily_single_time": a.get("infrast", {}).get("daily_single_time", "16:00"),
                "occupied_times": times,
                "occupied_slots": slots
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
    time_to_acc_map = {}
    for acc in accs:
        if not acc.get("enabled", True):
            continue
        times = get_account_effective_times(acc)
        for t in times:
            # 严格映射时间点到具体的 account_id
            time_to_acc_map[t] = acc.get("id")

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
        cur_cron = subprocess.check_output(["crontab", "-l"], text=True)
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
