#!/usr/bin/env python3
"""
鹰角网络 (HyperGryph) 官方服状态与维护时间窗自检探针
功能：
1. 毫秒级探测鹰角官方当前强制生效的客户端版本号 (clientVersion)
2. 抓取官方实时系统公告，智能解析停机维护时段 (例如 06:00 ~ 12:00)
3. 判定当前挂机是否需要跳过，或是否需要先触发大版本覆盖升级
"""

import urllib.request
import json
import re
import datetime
import subprocess
import pathlib
import os

VERSION_URL = "https://ak-conf.hypergryph.com/config/prod/official/Android/version"
ANNOUNCE_META_URL = "https://ak-conf.hypergryph.com/config/prod/announce_meta/Android/announcement.meta.json"

def get_official_remote_version():
    """获取鹰角官方当前强制要求的客户端版本号"""
    try:
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": "Arknights"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("clientVersion", ""), data.get("resVersion", "")
    except Exception as e:
        return "", ""

def get_installed_client_version(adb_bin="adb", device="127.0.0.1:5555"):
    """通过 ADB 读取当前模拟器内已安装的官方服客户端版本"""
    try:
        res = subprocess.run(
            [adb_bin, "-s", device, "shell", "dumpsys package com.hypergryph.arknights 2>/dev/null | grep -i versionName | head -n 1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5
        )
        for line in res.stdout.splitlines():
            if "versionName" in line:
                return line.split("=")[-1].strip()
    except Exception:
        pass
    return ""

def check_maintenance_status():
    """
    检查当前系统时间是否落在官方停服维护公告的时间窗内
    返回: (is_maintenance: bool, reason: str)
    """
    try:
        req = urllib.request.Request(ANNOUNCE_META_URL, headers={"User-Agent": "Arknights"})
        with urllib.request.urlopen(req, timeout=5) as r:
            meta = json.loads(r.read())
        
        now = datetime.datetime.now()
        cur_year = now.year

        for ann in meta.get("announceList", []):
            title = ann.get("title", "").replace("\n", " ")
            if not any(k in title for k in ["停服", "停机", "维护", "闪断更新"]):
                continue

            web_url = ann.get("webUrl")
            if not web_url:
                continue

            try:
                with urllib.request.urlopen(web_url, timeout=5) as hr:
                    text = hr.read().decode("utf-8", errors="ignore")
            except Exception:
                continue

            # 匹配公告中的日期模式，例如: 10月09日 或 10月9日
            date_matches = re.findall(r"([0-9]{1,2})月([0-9]{1,2})日", text)
            # 匹配维护时段，例如: 06:00 ~ 12:00 或 10:00 ~ 16:00 或 16:00 ~ 16:10
            range_matches = re.findall(r"([0-9]{1,2}):([0-9]{2})\s*~\s*([0-9]{1,2}):([0-9]{2})", text)

            if date_matches and range_matches:
                # 检查日期是否包含今天
                for m_month, m_day in date_matches:
                    m_m = int(m_month)
                    m_d = int(m_day)
                    if m_m == now.month and m_d == now.day:
                        # 检查当前时间是否在任何维护时间段内
                        for sh, sm, eh, em in range_matches:
                            start_dt = now.replace(hour=int(sh), minute=int(sm), second=0, microsecond=0)
                            end_dt = now.replace(hour=int(eh), minute=int(em), second=0, microsecond=0)
                            if start_dt <= now <= end_dt:
                                return True, f"官方正在停服维护中 ({title}: {sh}:{sm} ~ {eh}:{em})"
    except Exception as e:
        pass

    return False, "官方服务器正常开服中"

def preflight_check(adb_bin="adb", device="127.0.0.1:5555"):
    """
    执行挂机前置体检
    返回: dict(
        can_run: bool,
        need_update: bool,
        is_maintenance: bool,
        official_ver: str,
        installed_ver: str,
        reason: str
    )
    """
    remote_ver, res_ver = get_official_remote_version()
    installed_ver = get_installed_client_version(adb_bin, device)
    is_maint, maint_reason = check_maintenance_status()

    need_update = bool(remote_ver and installed_ver and remote_ver != installed_ver)
    can_run = not is_maint and not need_update

    reason = "就绪"
    if is_maint:
        reason = maint_reason
    elif need_update:
        reason = f"检测到鹰角客户端版本更新 ({installed_ver} -> {remote_ver})，需先升级安装包"

    return {
        "can_run": can_run,
        "need_update": need_update,
        "is_maintenance": is_maint,
        "official_ver": remote_ver,
        "installed_ver": installed_ver,
        "reason": reason
    }

if __name__ == "__main__":
    import pprint
    res = preflight_check()
    print("=== 鹰角官方服挂机前置体检结果 ===")
    pprint.pprint(res)
