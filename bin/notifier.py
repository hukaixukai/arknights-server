#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import pathlib

BASE_DIR = pathlib.Path(os.getenv("ARK_BASE_DIR", str(pathlib.Path(__file__).resolve().parent.parent)))
DEFAULT_WXPUSHER_APP_TOKEN = os.getenv("WXPUSHER_APP_TOKEN", "")

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
                "content": content,
                "summary": summary
            }
            if url:
                query["url"] = url
            spt_url = f"https://wxpusher.zjiecode.com/api/send/message/spt/{token}?{urllib.parse.urlencode(query)}"
            req = urllib.request.Request(spt_url, headers={"User-Agent": "Arknights-Service/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("code") == 1000:
                    return True, "SPT_ 微信消息发送成功"
                return False, f"SPT_ 发送失败: {res_data.get('msg')}"

        # 2. 否则按标准 AT_ 应用推送
        payload = {
            "appToken": token,
            "content": content,
            "summary": summary,
            "contentType": 3  # Markdown
        }
        if uids:
            payload["uids"] = [u.strip() for u in uids if u.strip()]
        if topic_ids:
            payload["topicIds"] = [int(t) for t in topic_ids if str(t).isdigit()]
        if url:
            payload["url"] = url

        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Arknights-Service/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("code") == 1000:
                return True, "AT_ 微信消息发送成功"
            return False, f"AT_ 发送失败: {res_data.get('msg')}"
    except Exception as e:
        return False, f"微信通知异常: {e}"

def send_email(subject, content, host, port, user, password, to_addr, use_ssl=True):
    """发送邮件通知"""
    try:
        msg = MIMEText(content, 'markdown', 'utf-8')
        msg['From'] = Header(f"明日方舟托管 <{user}>", 'utf-8')
        msg['To'] = Header(to_addr, 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')

        if use_ssl:
            server = smtplib.SMTP_SSL(host, int(port), timeout=15)
        else:
            server = smtplib.SMTP(host, int(port), timeout=15)
            server.starttls()

        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
        server.quit()
        return True, "邮件发送成功"
    except Exception as e:
        return False, f"邮件发送失败: {e}"

def notify_account(account_id, title, markdown_body, summary=None):
    """统一向账号所有者分发通知"""
    try:
        from db import get_account_by_id
        acc = get_account_by_id(account_id)
    except Exception:
        acc = None

    if not acc:
        # 回退读取本地 accounts.json
        acc_file = BASE_DIR / "config" / "accounts.json"
        if acc_file.exists():
            with open(acc_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for a in data.get("accounts", []):
                    if a.get("id") == account_id:
                        acc = a
                        break

    if not acc:
        print(f"[Notify] 找不到账号 {account_id} 配置，跳过通知")
        return

    notify_cfg = acc.get("notify", {})
    if not notify_cfg.get("enabled", True):
        return

    acc_name = acc.get("name", account_id)
    full_title = f"【{acc_name}】{title}"
    sum_text = summary or f"{acc_name}: {title}"

    # 1. 微信通知
    wx_cfg = notify_cfg.get("wxpusher", {})
    if wx_cfg.get("enabled", False):
        token = wx_cfg.get("app_token", "")
        uids_str = wx_cfg.get("uids", "")
        uids = [u.strip() for u in uids_str.split(",") if u.strip()] if uids_str else []
        topic_str = wx_cfg.get("topic_ids", "")
        topic_ids = [int(t.strip()) for t in topic_str.split(",") if t.strip().isdigit()] if topic_str else []
        
        ok, msg = send_wxpusher(
            content=f"## {full_title}\n\n{markdown_body}",
            summary=sum_text[:40],
            uids=uids,
            topic_ids=topic_ids,
            app_token=token,
            url=wx_cfg.get("url")
        )
        print(f"[Notify:WxPusher] {acc_name} -> {msg}")

    # 2. 邮件通知
    mail_cfg = notify_cfg.get("email", {})
    if mail_cfg.get("enabled", False):
        ok, msg = send_email(
            subject=full_title,
            content=markdown_body,
            host=mail_cfg.get("host", ""),
            port=mail_cfg.get("port", 465),
            user=mail_cfg.get("user", ""),
            password=mail_cfg.get("password", ""),
            to_addr=mail_cfg.get("to", ""),
            use_ssl=mail_cfg.get("ssl", True)
        )
        print(f"[Notify:Email] {acc_name} -> {msg}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("测试通知模块: 直接调用接口...")
        ok, msg = send_wxpusher("这是一条测试通知\n\n系统运行一切正常。", summary="测试通知")
        print(f"结果: {ok}, 信息: {msg}")
