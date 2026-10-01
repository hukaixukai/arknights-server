# -*- coding: utf-8 -*-
"""
明日方舟自动化托管平台 - SQLite 数据库持久化核心
提供原子性事务、自愈型连接、表结构自动初始化与历史 JSON 数据无损平滑迁移。
"""

import os
import time
import json
import sqlite3
import pathlib

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
CONFIG_DIR = BASE_DIR / "config"
DB_FILE = CONFIG_DIR / "arknights.db"
DEFAULT_ADMIN_USER = os.getenv("DEFAULT_ADMIN_USER", "admin")

USERS_JSON_FILE = CONFIG_DIR / "users.json"
ACCOUNTS_JSON_FILE = CONFIG_DIR / "accounts.json"
SETTINGS_JSON_FILE = CONFIG_DIR / "system_settings.json"

def get_connection():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. 用户鉴权表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            role TEXT DEFAULT 'user',
            created_at TEXT
        );
        """)

        # 2. 系统全局配置表 (邀请码等)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """)

        # 3. 明日方舟托管账号表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            account_name TEXT,
            game_password TEXT,
            platform TEXT DEFAULT 'Official',
            enabled INTEGER DEFAULT 1,
            owner_username TEXT NOT NULL,
            on_complete TEXT DEFAULT 'stop_emu',
            schedules_json TEXT,
            infrast_json TEXT,
            fight_json TEXT,
            recruit_json TEXT,
            mall_json TEXT,
            award_json TEXT,
            notify_json TEXT,
            inventory_json TEXT,
            chips_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """)

        # 4. 运行状态表 (活动账号ID、正在运行任务状态等)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """)
        conn.commit()

    # 尝试平滑无损迁移已有 JSON 数据
    migrate_from_json_if_needed()

def migrate_from_json_if_needed():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]

    # 如果数据库用户为空，从历史 JSON 迁移数据
    if user_count == 0 and USERS_JSON_FILE.exists():
        try:
            with open(USERS_JSON_FILE, "r", encoding="utf-8") as f:
                u_data = json.load(f)
            for u in u_data.get("users", []):
                add_user(
                    username=u.get("username"),
                    password_hash=u.get("password_hash"),
                    display_name=u.get("display_name"),
                    role=u.get("role", "user"),
                    created_at=u.get("created_at")
                )
            print("[DB Migration] 成功迁移用户数据到 SQLite")
        except Exception as e:
            print(f"[DB Migration Error] 迁移 users.json 失败: {e}")

    # 迁移系统设置 (邀请码)
    if get_system_setting("invite_code") is None and SETTINGS_JSON_FILE.exists():
        try:
            with open(SETTINGS_JSON_FILE, "r", encoding="utf-8") as f:
                s_data = json.load(f)
            code = s_data.get("invite_code", os.getenv("ARK_INVITE_CODE", "ARKNIGHTS_DEFAULT_KEY"))
            set_system_setting("invite_code", code)
            print("[DB Migration] 成功迁移系统设置到 SQLite")
        except Exception as e:
            print(f"[DB Migration Error] 迁移 system_settings.json 失败: {e}")

    # 迁移方舟账号数据
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM accounts")
        acc_count = cursor.fetchone()[0]

    if acc_count == 0 and ACCOUNTS_JSON_FILE.exists():
        try:
            with open(ACCOUNTS_JSON_FILE, "r", encoding="utf-8") as f:
                a_data = json.load(f)
            active_id = a_data.get("active_account_id", "")
            if active_id:
                set_active_account_id(active_id)
            for a in a_data.get("accounts", []):
                save_account(a)
            print("[DB Migration] 成功迁移明日方舟账号到 SQLite")
        except Exception as e:
            print(f"[DB Migration Error] 迁移 accounts.json 失败: {e}")

# ==================== 用户 DAO ====================

def get_user_by_username(username: str):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        return dict(row) if row else None

def list_users():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

def add_user(username: str, password_hash: str, display_name: str = None, role: str = "user", created_at: str = None):
    now_str = created_at or time.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO users (username, password_hash, display_name, role, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (username.strip(), password_hash, display_name or username, role, now_str))
        conn.commit()
    return True

def delete_user(username: str):
    if username == DEFAULT_ADMIN_USER:
        return False, "系统超级管理员不可删除"
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        # 级联删除该用户托管的全部方舟账号
        conn.execute("DELETE FROM accounts WHERE owner_username = ?", (username,))
        conn.commit()
    return True, f"已成功移除用户 [{username}] 及其名下所有托管账号"

# ==================== 系统全局设置 DAO ====================

def get_system_setting(key: str, default: str = None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

def set_system_setting(key: str, value: str):
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO system_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        """, (key, value, now_str))
        conn.commit()
    return True

# ==================== 方舟托管账号 DAO ====================

def get_arknights_game_day():
    import datetime
    now = datetime.datetime.now()
    if now.hour < 4:
        game_day = now.date() - datetime.timedelta(days=1)
    else:
        game_day = now.date()
    return game_day.strftime("%Y-%m-%d")

def update_account_recruit_progress(acc_id: str, delta_count: int = 0):
    game_day = get_arknights_game_day()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        row = conn.execute("SELECT recruit_json FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        if not row:
            return None
        r_cfg = json.loads(row["recruit_json"]) if row["recruit_json"] else {}
        if not isinstance(r_cfg, dict):
            r_cfg = {}
        last_day = r_cfg.get("last_reset_date", "")
        today_count = int(r_cfg.get("today_count", 0))

        if last_day != game_day:
            today_count = 0
            r_cfg["last_reset_date"] = game_day

        today_count += int(delta_count)
        r_cfg["today_count"] = max(0, today_count)
        if "daily_limit" not in r_cfg:
            r_cfg["daily_limit"] = 4
        if "select_6star_strategy" not in r_cfg:
            r_cfg["select_6star_strategy"] = "notify_user"

        new_json = json.dumps(r_cfg, ensure_ascii=False)
        conn.execute("UPDATE accounts SET recruit_json = ?, updated_at = ? WHERE id = ?", (new_json, now_str, acc_id))
        conn.commit()
        return r_cfg

def _account_row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    # 将序列化的 JSON 字段还原为字典/列表
    json_fields = ["schedules", "infrast", "fight", "recruit", "mall", "award", "notify", "inventory", "chips"]
    for f in json_fields:
        raw_val = d.pop(f"{f}_json", None)
        if raw_val:
            try:
                d[f] = json.loads(raw_val)
            except Exception:
                d[f] = {}
        else:
            d[f] = {} if f != "schedules" else ["10:00", "16:00", "22:00"]
    d["enabled"] = bool(d.get("enabled", 1))

    # 规范化公招日限额与 04:00 重置状态
    recruit = d.get("recruit", {})
    if not isinstance(recruit, dict):
        recruit = {}
    if "daily_limit" not in recruit:
        recruit["daily_limit"] = 4
    if "select_6star_strategy" not in recruit:
        recruit["select_6star_strategy"] = "notify_user"
    cur_game_day = get_arknights_game_day()
    if recruit.get("last_reset_date") != cur_game_day:
        recruit["today_count"] = 0
        recruit["last_reset_date"] = cur_game_day
    d["recruit"] = recruit

    return d

def get_account(acc_id: str):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        return _account_row_to_dict(row)

def list_accounts(owner_username: str = None):
    with get_connection() as conn:
        if owner_username:
            rows = conn.execute("SELECT * FROM accounts WHERE owner_username = ? ORDER BY id ASC", (owner_username,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()
        return [_account_row_to_dict(r) for r in rows]

def save_account(acc: dict):
    acc_id = acc.get("id") or f"acc_{int(time.time()*1000)}"
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # 提取各模块 JSON 字符串
    schedules_json = json.dumps(acc.get("schedules", ["10:00", "16:00", "22:00"]), ensure_ascii=False)
    infrast_json = json.dumps(acc.get("infrast", {}), ensure_ascii=False)
    fight_json = json.dumps(acc.get("fight", {}), ensure_ascii=False)
    recruit_json = json.dumps(acc.get("recruit", {}), ensure_ascii=False)
    mall_json = json.dumps(acc.get("mall", {}), ensure_ascii=False)
    award_json = json.dumps(acc.get("award", {}), ensure_ascii=False)
    notify_json = json.dumps(acc.get("notify", {}), ensure_ascii=False)
    inventory_json = json.dumps(acc.get("inventory", {}), ensure_ascii=False)
    chips_json = json.dumps(acc.get("chips", {}), ensure_ascii=False)

    with get_connection() as conn:
        # 查询已有的 created_at，避免每次覆盖丢失
        row = conn.execute("SELECT created_at FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        created_at = row["created_at"] if (row and row["created_at"]) else (acc.get("created_at") or now_str)

        conn.execute("""
        INSERT OR REPLACE INTO accounts (
            id, name, account_name, game_password, platform, enabled, owner_username,
            on_complete, schedules_json, infrast_json, fight_json, recruit_json,
            mall_json, award_json, notify_json, inventory_json, chips_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            acc_id,
            acc.get("name", "未命名账号"),
            acc.get("account_name", ""),
            acc.get("game_password", ""),
            acc.get("platform", "Official"),
            1 if acc.get("enabled", True) else 0,
            acc.get("owner_username", DEFAULT_ADMIN_USER),
            acc.get("on_complete", "stop_emu"),
            schedules_json,
            infrast_json,
            fight_json,
            recruit_json,
            mall_json,
            award_json,
            notify_json,
            inventory_json,
            chips_json,
            created_at,
            now_str
        ))
        conn.commit()

    # 保持与历史 active_account_id 兼容
    curr_active = get_active_account_id()
    if not curr_active:
        set_active_account_id(acc_id)
    return acc_id

def update_account_inventory_and_chips(acc_id: str, inventory_dict: dict, chips_dict: dict):
    if not acc_id:
        return
    with get_connection() as conn:
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            UPDATE accounts 
            SET inventory_json = ?, chips_json = ?, updated_at = ?
            WHERE id = ?
        """, (
            json.dumps(inventory_dict, ensure_ascii=False),
            json.dumps(chips_dict, ensure_ascii=False),
            now_str,
            acc_id
        ))
        conn.commit()

def delete_account(acc_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
        conn.commit()
    
    # 如果删除的是当前激活账号，自动切到下一个
    curr_active = get_active_account_id()
    if curr_active == acc_id:
        accs = list_accounts()
        new_active = accs[0]["id"] if accs else ""
        set_active_account_id(new_active)
    return True

def get_active_account_id() -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM system_state WHERE key = 'active_account_id'").fetchone()
        if row and row[0]:
            return row[0]
        # 兜底返回第一个账号
        acc_row = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()
        return acc_row[0] if acc_row else ""

def set_active_account_id(acc_id: str):
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO system_state (key, value, updated_at)
        VALUES ('active_account_id', ?, ?)
        """, (acc_id, now_str))
        conn.commit()
    return True

# ==================== 运行时任务状态 DAO ====================

def get_running_task():
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM system_state WHERE key = 'running_task'").fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                pass
    return None

def set_running_task(task_info: dict):
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    val_str = json.dumps(task_info or {}, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO system_state (key, value, updated_at)
        VALUES ('running_task', ?, ?)
        """, (val_str, now_str))
        conn.commit()
    return True

if __name__ == "__main__":
    init_db()
    print("SQLite 数据库初始化与测试完毕！当前用户列表:", list_users())
    print("账号列表:", list_accounts())
