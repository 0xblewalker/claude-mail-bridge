#!/usr/bin/env python3
"""
claude-mail-bridge MCP Server
给你的 AI 一个邮箱——MCP 版。
AI 可以主动收信、发信、搜索邮件。
"""

import os
import json
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import formataddr
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path

# ── Load Config ────────────────────────────────────────────────

def load_config() -> dict:
    # 优先环境变量，其次 config.json
    if os.environ.get("MAIL_ADDRESS"):
        return {
            "email": {
                "address": os.environ["MAIL_ADDRESS"],
                "password": os.environ["MAIL_PASSWORD"],
                "imap_host": os.environ.get("IMAP_HOST", "imap.qq.com"),
                "imap_port": int(os.environ.get("IMAP_PORT", "993")),
                "smtp_host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
                "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
                "display_name": os.environ.get("DISPLAY_NAME", "Claude"),
            }
        }
    p = Path(__file__).parent / "config.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    raise RuntimeError("请设置环境变量或创建 config.json")

CFG = load_config()
EMAIL = CFG["email"]

# ── MCP Server ─────────────────────────────────────────────────

mcp = FastMCP("mail_bridge")

# ── Helpers ────────────────────────────────────────────────────

def _decode(value: str) -> str:
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


def _get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _imap():
    conn = imaplib.IMAP4_SSL(EMAIL["imap_host"], EMAIL.get("imap_port", 993))
    conn.login(EMAIL["address"], EMAIL["password"])
    return conn


def _summary(msg, uid: str) -> dict:
    return {
        "uid": uid,
        "from": _decode(msg.get("From", "")),
        "to": _decode(msg.get("To", "")),
        "subject": _decode(msg.get("Subject", "")),
        "date": msg.get("Date", ""),
    }


# ── Tools ──────────────────────────────────────────────────────

class InboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, description="获取最近几封邮件", ge=1, le=50)
    folder: str = Field(default="INBOX", description="邮箱文件夹")

@mcp.tool(name="mail_inbox")
async def mail_inbox(params: InboxInput) -> str:
    """查看收件箱最近的邮件列表（标题、发件人、时间）。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        status, data = conn.uid("search", None, "ALL")
        if status != "OK" or not data[0]:
            conn.logout()
            return json.dumps([], ensure_ascii=False)
        uids = data[0].split()[-params.limit:]
        uids.reverse()
        results = []
        for uid in uids:
            uid_str = uid.decode()
            st, md = conn.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            if st == "OK" and md[0]:
                msg = email.message_from_bytes(md[0][1])
                results.append(_summary(msg, uid_str))
        conn.logout()
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str = Field(..., description="邮件 UID（从 mail_inbox 获取）")
    folder: str = Field(default="INBOX")
    max_chars: int = Field(default=5000, ge=100, le=50000)

@mcp.tool(name="mail_read")
async def mail_read(params: ReadInput) -> str:
    """读取一封邮件的完整内容。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        st, md = conn.uid("fetch", params.uid, "(BODY.PEEK[])")
        conn.logout()
        if st != "OK":
            return "错误: 邮件不存在"
        raw = None
        for item in md:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                break
        if not raw:
            return "错误: 无法解析"
        msg = email.message_from_bytes(raw)
        result = _summary(msg, params.uid)
        body = _get_body(msg)
        result["body"] = body[:params.max_chars]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., description='IMAP 搜索条件，如 FROM "xxx" / SUBJECT "hello" / UNSEEN / SINCE 01-Jan-2025')
    folder: str = Field(default="INBOX")
    limit: int = Field(default=10, ge=1, le=50)

@mcp.tool(name="mail_search")
async def mail_search(params: SearchInput) -> str:
    """搜索邮件。支持 IMAP 搜索语法。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        st, data = conn.uid("search", None, params.query)
        if st != "OK" or not data[0]:
            conn.logout()
            return json.dumps([], ensure_ascii=False)
        uids = data[0].split()[-params.limit:]
        uids.reverse()
        results = []
        for uid in uids:
            uid_str = uid.decode()
            st2, md = conn.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            if st2 == "OK" and md[0]:
                msg = email.message_from_bytes(md[0][1])
                results.append(_summary(msg, uid_str))
        conn.logout()
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., description="收件人邮箱")
    subject: str = Field(..., description="主题")
    body: str = Field(..., description="正文（纯文本）")
    cc: Optional[str] = Field(default=None, description="抄送，逗号分隔")

@mcp.tool(name="mail_send")
async def mail_send(params: SendInput) -> str:
    """发送一封邮件。"""
    try:
        msg = MIMEMultipart()
        msg["From"] = formataddr((EMAIL.get("display_name", "Claude"), EMAIL["address"]))
        msg["To"] = params.to
        msg["Subject"] = params.subject
        if params.cc:
            msg["Cc"] = params.cc
        msg.attach(MIMEText(params.body, "plain", "utf-8"))

        with smtplib.SMTP(EMAIL["smtp_host"], EMAIL.get("smtp_port", 587)) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL["address"], EMAIL["password"])
            recipients = [params.to]
            if params.cc:
                recipients.extend([a.strip() for a in params.cc.split(",")])
            server.sendmail(EMAIL["address"], recipients, msg.as_string())

        # 存到已发送（best-effort）
        try:
            imap = imaplib.IMAP4_SSL(EMAIL["imap_host"], EMAIL.get("imap_port", 993))
            imap.login(EMAIL["address"], EMAIL["password"])
            for folder in ['"Sent Messages"', '"Sent"', '"已发送"', '"[Gmail]/Sent Mail"']:
                try:
                    imap.select(folder)
                    imap.append(folder, "\\Seen",
                        imaplib.Time2Internaldate(datetime.now(timezone.utc)),
                        msg.as_bytes())
                    break
                except Exception:
                    continue
            imap.logout()
        except Exception:
            pass

        return json.dumps({"status": "sent", "to": params.to, "subject": params.subject}, ensure_ascii=False)
    except Exception as e:
        return f"发送失败: {e}"


@mcp.tool(name="mail_folders")
async def mail_folders() -> str:
    """列出所有邮箱文件夹。"""
    try:
        conn = _imap()
        st, folders = conn.list()
        conn.logout()
        result = []
        for f in folders:
            if isinstance(f, bytes):
                parts = f.decode("utf-8", errors="replace").split(' "/" ')
                if len(parts) == 2:
                    result.append(parts[1].strip('"'))
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


# ── Entry ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8877
    import uvicorn
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="127.0.0.1", port=port)
