#!/usr/bin/env python3
"""
claude-mail-bridge - 给你的 AI 一个邮箱
Lightweight email bridge: IMAP poll → API call → SMTP reply.
No MCP, no framework, just Python stdlib + requests.
"""

import json
import time
import imaplib
import smtplib
import email
import logging
import sys
import signal
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import formataddr
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("需要 requests 库: pip install requests")
    sys.exit(1)

# ── Load Config ────────────────────────────────────────────────

def load_config(path="config.json"):
    p = Path(path)
    if not p.exists():
        print(f"找不到 {path}，请复制 config.example.json 为 config.json 并填写配置")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()
EMAIL_CFG = CONFIG["email"]
API_CFG = CONFIG["api"]
BRIDGE_CFG = CONFIG.get("bridge", {})

# ── Logging ────────────────────────────────────────────────────

log_file = BRIDGE_CFG.get("log_file", "bridge.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("bridge")

# ── State & Rate Limiting ─────────────────────────────────────

seen_uids = set()
running = True

_sender_history = defaultdict(list)   # {sender: [timestamp, ...]}
_daily_count = 0
_daily_reset = 0.0


def _check_rate_limit(sender):
    # type: (str) -> Optional[str]
    """Returns reason string if blocked, None if OK."""
    global _daily_count, _daily_reset

    now = time.time()
    daily_limit = BRIDGE_CFG.get("daily_limit", 50)
    sender_limit = BRIDGE_CFG.get("sender_hourly_limit", 5)

    if now - _daily_reset > 86400:
        _daily_count = 0
        _daily_reset = now

    if _daily_count >= daily_limit:
        return "今日已达 %d 次上限" % daily_limit

    cutoff = now - 3600
    _sender_history[sender] = [t for t in _sender_history[sender] if t > cutoff]
    if len(_sender_history[sender]) >= sender_limit:
        return "%s 一小时内已发 %d 封" % (sender, sender_limit)

    return None


def _record_call(sender):
    global _daily_count
    _daily_count += 1
    _sender_history[sender].append(time.time())


def graceful_exit(sig, frame):
    global running
    log.info("收到退出信号，正在停止...")
    running = False

signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)

# ── Email Helpers ─────────────────────────────────────────────

def decode_header_value(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def extract_email_addr(from_header):
    """Extract bare email from 'Name <addr>' format."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip()
    return from_header.strip()


# ── IMAP ───────────────────────────────────────────────────────

def connect_imap():
    conn = imaplib.IMAP4_SSL(EMAIL_CFG["imap_host"], EMAIL_CFG.get("imap_port", 993))
    conn.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
    return conn


def fetch_unseen():
    """Fetch all unseen emails, return list of {uid, from, subject, body}."""
    try:
        conn = connect_imap()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "UNSEEN")
        if status != "OK" or not data[0]:
            conn.logout()
            return []

        uids = data[0].split()
        allowed = BRIDGE_CFG.get("allowed_senders", [])
        max_chars = BRIDGE_CFG.get("max_body_chars", 3000)
        results = []

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in seen_uids:
                continue

            # BODY.PEEK[] = read without marking as \Seen
            status, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue

            raw = None
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                    raw = item[1]
                    break
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            from_header = decode_header_value(msg.get("From", ""))
            from_addr = extract_email_addr(from_header)

            # allowlist check
            if allowed and from_addr not in allowed:
                log.info("跳过未授权发件人: %s", from_addr)
                seen_uids.add(uid_str)
                conn.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            body = get_body(msg)[:max_chars]
            subject = decode_header_value(msg.get("Subject", ""))

            results.append({
                "uid": uid_str,
                "from": from_header,
                "from_addr": from_addr,
                "subject": subject,
                "body": body,
            })
            seen_uids.add(uid_str)

        conn.logout()
        return results
    except Exception as e:
        log.error("IMAP 错误: %s", e)
        return []


# ── API Call ───────────────────────────────────────────────────

def call_api(sender, subject, body):
    """Call OpenAI-compatible API and return response text."""
    url = API_CFG["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + API_CFG["api_key"],
    }

    user_content = "来自: %s\n主题: %s\n\n%s" % (sender, subject, body)

    payload = {
        "model": API_CFG["model"],
        "messages": [
            {"role": "system", "content": API_CFG.get("system_prompt", "你是一个友善的AI助手。")},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": API_CFG.get("max_tokens", 1000),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("API 错误: %s", e)
        return ""


# ── SMTP ───────────────────────────────────────────────────────

def _save_to_imap_folder(msg_bytes, folder_names, flags="\\Seen"):
    """Try to save email to one of the given IMAP folder names (best-effort)."""
    try:
        imap = imaplib.IMAP4_SSL(EMAIL_CFG["imap_host"], EMAIL_CFG.get("imap_port", 993))
        imap.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
        for folder in folder_names:
            try:
                imap.select(folder)
                imap.append(
                    folder, flags,
                    imaplib.Time2Internaldate(datetime.now(timezone.utc)),
                    msg_bytes,
                )
                break
            except Exception:
                continue
        imap.logout()
    except Exception:
        pass


def send_reply(to_addr, subject, body):
    """Send reply via SMTP."""
    try:
        msg = MIMEMultipart()
        display_name = EMAIL_CFG.get("display_name", "Claude")
        msg["From"] = formataddr((display_name, EMAIL_CFG["address"]))
        msg["To"] = to_addr
        msg["Subject"] = ("Re: " + subject) if not subject.startswith("Re:") else subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(EMAIL_CFG["smtp_host"], EMAIL_CFG.get("smtp_port", 587)) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
            server.sendmail(EMAIL_CFG["address"], [to_addr], msg.as_string())

        _save_to_imap_folder(
            msg.as_bytes(),
            ['"Sent Messages"', '"Sent"', '"已发送"', '"[Gmail]/Sent Mail"'],
            "\\Seen",
        )
        log.info("✅ 已回复 %s: %s", to_addr, subject)
    except Exception as e:
        log.error("SMTP 错误: %s", e)


def save_draft(to_addr, subject, body):
    """Save AI reply as draft, owner reviews and sends manually."""
    try:
        msg = MIMEMultipart()
        display_name = EMAIL_CFG.get("display_name", "Claude")
        msg["From"] = formataddr((display_name, EMAIL_CFG["address"]))
        msg["To"] = to_addr
        msg["Subject"] = ("Re: " + subject) if not subject.startswith("Re:") else subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        _save_to_imap_folder(
            msg.as_bytes(),
            ['"Drafts"', '"Draft"', '"草稿"', '"INBOX.Drafts"', '"[Gmail]/Drafts"'],
            "\\Draft",
        )
        log.info("📝 草稿已保存: → %s", to_addr)
    except Exception as e:
        log.error("保存草稿失败: %s", e)


# ── Notifications ─────────────────────────────────────────────

def notify_owner(sender, subject, body="", event="new_mail"):
    """Notify owner via webhook. Supports ntfy / Bark / pushover etc.
    event: 'new_mail' | 'draft_saved' | 'auto_sent'
    """
    webhook = BRIDGE_CFG.get("notify_webhook")
    if not webhook:
        return
    try:
        if event == "new_mail":
            title = "📨 收到邮件: " + sender
            preview = body[:200] + ("..." if len(body) > 200 else "")
            message = "主题: %s\n\n%s" % (subject, preview)
        elif event == "draft_saved":
            title = "📝 草稿已生成: Re: " + subject
            preview = body[:200] + ("..." if len(body) > 200 else "")
            message = "回复 %s 的草稿已保存，请去邮箱审核后发送。\n\n%s" % (sender, preview)
        else:
            title = "✅ 已自动回复: " + sender
            message = "主题: " + subject

        requests.post(
            webhook,
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "email"},
            timeout=10,
        )
        log.info("📱 已通知主人 (%s)", event)
    except Exception as e:
        log.warning("通知发送失败: %s", e)


# ── Main Loop ──────────────────────────────────────────────────

def main():
    interval = BRIDGE_CFG.get("poll_interval", 10)
    auto_send = BRIDGE_CFG.get("auto_send", False)
    daily_limit = BRIDGE_CFG.get("daily_limit", 50)

    log.info("🚀 claude-mail-bridge 启动")
    log.info("   邮箱: %s", EMAIL_CFG["address"])
    log.info("   模型: %s", API_CFG["model"])
    log.info("   模式: %s", "自动发送" if auto_send else "草稿审核（需手动发送）")
    log.info("   每日上限: %d 次", daily_limit)
    log.info("   轮询间隔: %ds", interval)

    allowed = BRIDGE_CFG.get("allowed_senders", [])
    if allowed:
        log.info("   白名单: %s", allowed)
    else:
        log.info("   白名单: 无限制（所有人可发）")

    webhook = BRIDGE_CFG.get("notify_webhook")
    if webhook:
        log.info("   通知: %s", webhook[:40] + "...")
    else:
        log.info("   通知: 未设置（仅日志记录）")

    # init: mark existing emails so we don't reply to old mail
    try:
        conn = connect_imap()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            for uid in data[0].split():
                seen_uids.add(uid.decode())
        conn.logout()
        log.info("   已标记 %d 封历史邮件", len(seen_uids))
    except Exception as e:
        log.warning("初始化标记失败: %s", e)

    while running:
        emails = fetch_unseen()
        for mail in emails:
            log.info("📨 收到邮件: %s - %s", mail["from"], mail["subject"])
            notify_owner(mail["from"], mail["subject"], mail["body"], "new_mail")

            blocked = _check_rate_limit(mail["from_addr"])
            if blocked:
                log.warning("⚠️ 限流: %s，跳过", blocked)
                continue

            reply = call_api(mail["from"], mail["subject"], mail["body"])
            if not reply:
                log.warning("API 无回复，跳过")
                continue

            _record_call(mail["from_addr"])

            if auto_send:
                send_reply(mail["from_addr"], mail["subject"], reply)
                notify_owner(mail["from"], mail["subject"], reply, "auto_sent")
            else:
                save_draft(mail["from_addr"], mail["subject"], reply)
                notify_owner(mail["from"], mail["subject"], reply, "draft_saved")

        time.sleep(interval)

    log.info("👋 bridge 已停止")


if __name__ == "__main__":
    main()
