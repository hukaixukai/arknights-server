#!/usr/bin/env python3
import os
import sys
import json
import subprocess

BASE_DIR = os.getenv("ARK_BASE_DIR", "/app")
MAA_DIR = os.path.join(BASE_DIR, "maa")
BIN_DIR = os.path.join(BASE_DIR, "bin")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
RUN_CONFIG_FILE = os.path.join(CONFIG_DIR, "run_config.json")
DEPOT_FILE = os.path.join(MAA_DIR, "data", "DepotData.json")
EVENT_6STAR_FILE = os.getenv("ARK_6STAR_EVENT_FILE", "/opt/arknights/data/runtime/recruit_6star_event.json")
CTL_SCRIPT = os.path.join(BIN_DIR, "ark_service_ctl.sh")

def load_run_config():
    default_config = {
        "infrast_plan": "333不搓玉",
        "fight_enabled": True,
        "primary_stage": "1-7",
        "fallback_stage": "1-7",
        "annihilation_enabled": True,
        "depot_update_enabled": True
    }
    if os.path.exists(RUN_CONFIG_FILE):
        try:
            with open(RUN_CONFIG_FILE, "r", encoding="utf-8") as f:
                default_config.update(json.load(f))
        except Exception:
            pass
    return default_config

def save_run_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(RUN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"

def get_status():
    code, out, _ = run_cmd(f"{CTL_SCRIPT} status")
    cfg = load_run_config()
    return {"status_text": out, "code": code, "current_config": cfg}

def set_infrast_plan(plan_name):
    cfg = load_run_config()
    valid_names = ["333不搓玉", "333搓玉", "333搓碎片"]
    matched = None
    for name in valid_names:
        if plan_name in name:
            matched = name
            break
    if not matched:
        matched = plan_name
    cfg["infrast_plan"] = matched
    save_run_config(cfg)
    return {"success": True, "message": f"基建排班方案已切换为: {matched}", "config": cfg}

def set_fight_config(stage=None, enabled=None):
    cfg = load_run_config()
    if stage is not None:
        cfg["primary_stage"] = stage.strip()
    if enabled is not None:
        if isinstance(enabled, str):
            cfg["fight_enabled"] = enabled.lower() in ["true", "1", "yes", "on", "开启"]
        else:
            cfg["fight_enabled"] = bool(enabled)
    save_run_config(cfg)
    return {"success": True, "message": "理智作战配置已更新", "config": cfg}

def get_inventory():
    target_ids = {
        "4003": "合成玉",
        "4002": "至纯源石",
        "4001": "龙门币",
        "30012": "固源岩",
        "3141": "源石碎片"
    }
    res = {name: 0 for name in target_ids.values()}
    
    if os.path.exists(DEPOT_FILE):
        try:
            with open(DEPOT_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            data = d.get("data", {})
            for item_id, name in target_ids.items():
                if item_id in data:
                    res[name] = data[item_id]
        except Exception as e:
            return {"success": False, "error": f"解析仓库文件失败: {e}"}
            
    return {"success": True, "inventory": res}

def get_6star_event(mark_read=False):
    target = EVENT_6STAR_FILE
    if not os.path.exists(target):
        alt = "/opt/data/runtime/recruit_6star_event.json"
        if os.path.exists(alt):
            target = alt
        else:
            return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if mark_read and not data.get("notified"):
            data["notified"] = True
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception:
        return None

def audit_logs():
    candidates = [
        "/opt/data/runtime/arknights_asst.log",
        os.getenv("ARK_HERMES_RUNTIME_DIR", "/opt/arknights/data/runtime") + "/arknights_asst.log",
        os.path.join(MAA_DIR, "asst.log"),
        os.path.join(MAA_DIR, "debug", "asst.log")
    ]
    target_log = None
    for p in candidates:
        if os.path.exists(p):
            target_log = p
            break
            
    if not target_log:
        return {"status": "no_log_found", "message": "暂未检测到 MAA 运行日志"}
        
    with open(target_log, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-300:]
        
    log_text = "".join(lines)
    diagnostics = []
    if "ConnectFailed" in log_text or "adb connection timeout" in log_text.lower():
        diagnostics.append("ADB 连接异常或超时，建议重启容器")
    if "maintenance" in log_text.lower() or "停服" in log_text:
        diagnostics.append("检测到官方服务器维护停服中")
    if "force update" in log_text.lower() or "发现新版本" in log_text:
        diagnostics.append("检测到大版本更新弹窗，需要覆盖安装新版 APK")
        
    inv = get_inventory()
    cfg = load_run_config()
    recruit_6star = get_6star_event(mark_read=False)
    
    return {
        "status": "healthy" if not diagnostics else "warning",
        "diagnostics": diagnostics,
        "current_config": cfg,
        "inventory": inv.get("inventory", {}),
        "recruit_6star_event": recruit_6star,
        "log_file": target_log,
        "tail_lines": len(lines)
    }

def main():
    if len(sys.argv) < 2:
        print("用法: ark_hermes_tool.py {status|get-config|set-infrast <plan>|set-fight <stage> <enabled>|inventory|audit-logs|get-6star}")
        sys.exit(1)
        
    action = sys.argv[1]
    if action == "status":
        print(json.dumps(get_status(), ensure_ascii=False, indent=2))
    elif action == "get-config":
        print(json.dumps(load_run_config(), ensure_ascii=False, indent=2))
    elif action == "set-infrast":
        plan = sys.argv[2] if len(sys.argv) > 2 else "333不搓玉"
        print(json.dumps(set_infrast_plan(plan), ensure_ascii=False, indent=2))
    elif action == "set-fight":
        stage = sys.argv[2] if len(sys.argv) > 2 else None
        enabled = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(set_fight_config(stage=stage, enabled=enabled), ensure_ascii=False, indent=2))
    elif action in ["inventory", "get-inventory"]:
        print(json.dumps(get_inventory(), ensure_ascii=False, indent=2))
    elif action == "get-6star":
        evt = get_6star_event(mark_read=False)
        print(json.dumps(evt if evt else {"has_event": False}, ensure_ascii=False, indent=2))
    elif action == "audit-logs":
        print(json.dumps(audit_logs(), ensure_ascii=False, indent=2))
    else:
        print(f"未知指令: {action}")

if __name__ == "__main__":
    main()
