#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知中心: 支持 WxPusher 与 SMTP 邮箱，每个账号独立配置
"""

import os
import sys
import json
import smtplib
import urllib.request
import urllib.parse
import pathlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
# 默认 WxPusher App Token，可在账号配置中覆盖
DEFAULT_WXPUSHER_APP_TOKEN = os.getenv("WXPUSHER_APP_TOKEN", "")
# 控制台地址前缀，用于通知里附带的跳转链接
CONSOLE_URL = os.getenv("CONSOLE_URL", "")

def send_wxpusher(content, summary="明日方舟托管通知", uids=None, topic_ids=None, app_token=None, url=None):
    """通过 WxPusher API 发送通知，自适应支持 SPT_ 极简推送与 AT_ 完整推送"""
    try:
        token = (app_token or "").strip()
        if not token:
            token = DEFAULT_WXPUSHER_APP_TOKEN
        if not token:
            return False, "未配置 WxPusher Token"

        # 1. 如果是 SPT_ (Simple Push Token)，直接走简易推送通道
        if token.startswith("SPT_"):
            query = {
                "title": summary[:50],
                "content": content,
            }
            if url:
                query["url"] = url
            spt_url = f"https://wxpusher.zjiecode.com/api/send/message/spt/{token}?{urllib.parse.urlencode(query)}"
            req = urllib.request.Request(spt_url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("code") == 1000:
                    return True, "SPT 极简推送成功"
                else:
                    return False, res_data.get("msg", "SPT 推送失败")

        # 2. 否则按标准 AT_ 应用推送
        payload = {
            "appToken": token,
            "content": content,
            "summary": summary[:50],
            "contentType": 3,  # Markdown
        }
        if uids and isinstance(uids, list) and len(uids) > 0:
            payload["uids"] = [u.strip() for u in uids if u.strip()]
        if topic_ids and isinstance(topic_ids, list) and len(topic_ids) > 0:
            payload["topicIds"] = topic_ids
        if url:
            payload["url"] = url

        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("code") == 1000:
                return True, "AT_ 微信消息发送成功"
            else:
                return False, res_data.get("msg", "WxPusher 接口返回错误")
    except Exception as e:
        return False, f"WxPusher 发送异常: {str(e)}"

def send_email(subject, html_content, smtp_host, smtp_port, sender_email, auth_code, receiver_email, use_ssl=True):
    """通过 SMTP 邮箱授权码发送 HTML 邮件"""
    try:
        smtp_port = int(smtp_port) if smtp_port else (465 if use_ssl else 587)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = Header(f"明日方舟托管 <{sender_email}>", "utf-8")
        msg["To"] = Header(receiver_email, "utf-8")

        part_html = MIMEText(html_content, "html", "utf-8")
        msg.attach(part_html)

        if use_ssl or smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
            server.starttls()

        server.login(sender_email, auth_code)
        server.sendmail(sender_email, [receiver_email], msg.as_string())
        server.quit()
        return True, "邮件发送成功"
    except Exception as e:
        return False, f"邮件发送失败: {str(e)}"

def dispatch_account_notify(account_data, subject, markdown_content, html_content=None, event_type=None):
    """
    根据账号独立的 notify 配置分发消息到 WxPusher 和/或 邮箱
    event_type: 'on_6star_recruit' | 'on_daily_summary' | 'on_error'
    """
    notify_cfg = account_data.get("notify", {})
    channel = notify_cfg.get("channel", "none")
    if channel == "none":
        return {"status": "skipped", "reason": "通知通道已关闭"}

    triggers = notify_cfg.get("triggers", {})
    if event_type and not triggers.get(event_type, True):
        return {"status": "skipped", "reason": f"事件 [{event_type}] 未开启触发"}

    results = {}

    # 1. WxPusher 渠道
    if channel in ("wxpusher", "both"):
        wx_cfg = notify_cfg.get("wxpusher", {})
        token = wx_cfg.get("app_token", "")
        uids = wx_cfg.get("uids", [])
        topics = wx_cfg.get("topic_ids", [])
        ok, msg = send_wxpusher(
            content=markdown_content,
            summary=subject,
            uids=uids,
            topic_ids=topics,
            app_token=token,
            url=CONSOLE_URL or None
        )
        results["wxpusher"] = {"success": ok, "msg": msg}

    # 2. Email 渠道
    if channel in ("email", "both"):
        em_cfg = notify_cfg.get("email", {})
        host = em_cfg.get("smtp_host", "")
        port = em_cfg.get("smtp_port", 465)
        sender = em_cfg.get("sender_email", "")
        auth_code = em_cfg.get("auth_code", "")
        receiver = em_cfg.get("receiver_email", "")
        use_ssl = em_cfg.get("use_ssl", True)

        if not (host and sender and auth_code and receiver):
            results["email"] = {"success": False, "msg": "邮箱配置未完整填写 (需要服务器、发送邮箱、授权码与接收邮箱)"}
        else:
            # 简单将 markdown 转为美观 HTML 邮件
            console_link = f'<a href="{CONSOLE_URL}" style="color: #d97745;">点击打开控制台</a>' if CONSOLE_URL else ""
            body_html = html_content or f"""
            <div style="font-family: -apple-system, sans-serif; background: #181816; color: #ede8df; padding: 24px; border-radius: 8px;">
                <h2 style="color: #d97745; margin-top: 0;">{subject}</h2>
                <div style="white-space: pre-wrap; font-size: 14px; line-height: 1.6; background: #21211e; padding: 16px; border-radius: 6px; border: 1px solid #383833;">
{markdown_content}
                </div>
                <div style="margin-top: 20px; font-size: 12px; color: #736e65;">
                    明日方舟自动化托管控制台 {console_link}
                </div>
            </div>
            """
            ok, msg = send_email(subject, body_html, host, port, sender, auth_code, receiver, use_ssl)
            results["email"] = {"success": ok, "msg": msg}

    return results

if __name__ == "__main__":
    # 命令行测试模式: python3 notifier.py test <account_id>
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        acc_id = sys.argv[2] if len(sys.argv) > 2 else ""
        acc_file = BASE_DIR / "config" / "accounts.json"
        if acc_file.exists():
            with open(acc_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            acc = next((a for a in d.get("accounts", []) if a.get("id") == acc_id), None)
            if acc:
                res = dispatch_account_notify(
                    acc,
                    subject="明日方舟测试通知",
                    markdown_content="### 通信测试\n这是一条测试消息，验证 WxPusher 与邮箱配置是否正常连通！\n- 时间: 刚刚\n- 状态: 正常",
                    event_type=None
                )
                print(json.dumps(res, ensure_ascii=False, indent=2))