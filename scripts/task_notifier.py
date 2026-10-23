#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务结束邮件通知器（校园邮箱）
================================

用途
----
1. 包装任意命令（或手工登记一次已结束的任务），记录开始时间、结束时间、耗时与退出码；
2. 将记录同时写入人类可读日志与 JSON Lines 结构化日志；
3. 日志发生变更时，通过 SMTP 向目标邮箱发送通知邮件。

配置文件默认位于 ~/.config/campus_mail/config.json，凭据默认位于
~/.config/campus_mail/credentials.txt（KEY=VALUE，权限 600）。
可用 CAMPUS_MAIL_CONFIG 与 CAMPUS_MAIL_CREDENTIALS 覆盖两个路径。

常用命令
--------
    # 0) 首次使用：生成配置与凭据模板
    python3 task_notifier.py init

    # 1) 包装一条命令：自动计时、写日志、结束后发邮件
    python3 task_notifier.py run --name 训练主模型 -- python train.py

    # 2) 手工登记一条已完成的任务（例如外部进程已跑完）
    python3 task_notifier.py record --name 数据预处理 --duration 813.5 --status success

    # 3) 常驻监视日志文件：文件一被修改就发邮件（适合任务由别处驱动）
    python3 task_notifier.py watch

    # 4) 查看最近若干条记录及耗时统计
    python3 task_notifier.py report --last 10

    # 5) 验证 SMTP 配置是否能发出邮件
    python3 task_notifier.py test-mail

凭据优先级：环境变量（CAMPUS_MAIL_PASSWORD 等）> 凭据文件 > 配置文件明文字段。

无第三方依赖，Python 3.9+ 均可运行。
"""

import argparse
import getpass
import html
import json
import os
import shlex
import smtplib
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

VERSION = "1.0.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_HOME = Path(
    os.environ.get("CAMPUS_MAIL_HOME") or (Path.home() / ".config" / "campus_mail")
).expanduser()
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("CAMPUS_MAIL_CONFIG") or (DEFAULT_HOME / "config.json")
).expanduser()

STATUS_LABELS = {
    "success": "任务完成",
    "failure": "任务失败",
    "interrupted": "任务中断",
    "partial": "部分完成",
    "unknown": "任务结束",
}

CREDENTIAL_TEMPLATE = """\
# 校园邮箱 SMTP 凭据（KEY=VALUE）
# 权限应保持 600，不得提交至版本库或复制到公开位置
SMTP_HOST=smtp.dlut.edu.cn
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_SENDER=你的校园邮箱@example.edu.cn
SMTP_PASSWORD=密码或客户端授权码
"""

DEFAULT_CONFIG = {
    "_说明": "base_dir 为 cwd 时日志写在工作目录；凭据默认从 ~/.config/campus_mail/credentials.txt 读取，也可用环境变量 CAMPUS_MAIL_PASSWORD 覆盖。",
    "base_dir": "cwd",
    "max_log_bytes": 5242880,
    "smtp": {
        "host": "smtp.dlut.edu.cn",
        "port": 465,
        "security": "ssl",
        "sender": "",
        "display_name": "任务结束通知",
        "credential_file": "",
        "password": "",
        "password_env": "CAMPUS_MAIL_PASSWORD",
        "timeout": 30,
    },
    "notify": {
        "enabled": True,
        "to": [],
        "subject_prefix": "[任务通知]",
        "retries": 3,
        "tail_lines": 60,
    },
    "paths": {
        "log": ".task_notify/task_notifier.log",
        "jsonl": ".task_notify/task_notifier.jsonl",
        "notify_log": ".task_notify/task_notifier_notify.log",
        "state": ".task_notify/.task_notifier_state.json",
    },
    "watch": {
        "interval": 5.0,
        "settle": 10.0,
    },
}

EXAMPLES = """\
示例：
  python3 task_notifier.py init
  python3 task_notifier.py run --name 训练 -- python -m scripts.xxx.train -c cfg.json
  python3 task_notifier.py record --name 推理实验 --duration 95
  python3 task_notifier.py watch --interval 5 --settle 15
  python3 task_notifier.py watch --once        # 供 cron 调用，只检查一次
  python3 task_notifier.py report --last 20
  python3 task_notifier.py test-mail

配置与凭据默认位于 ~/.config/campus_mail/，可用 CAMPUS_MAIL_CONFIG 与
CAMPUS_MAIL_CREDENTIALS 指定其他位置；密码也可由 CAMPUS_MAIL_PASSWORD 提供。
"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def now():
    return datetime.now().astimezone()


def iso(moment):
    return moment.isoformat(timespec="seconds")


def fmt_duration(seconds):
    """把秒数格式化为便于阅读的时长文本。"""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "未知"
    if seconds < 0:
        return "未知"
    if seconds < 60:
        return "%.1f 秒" % seconds
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append("%d 小时" % hours)
    if minutes:
        parts.append("%d 分" % minutes)
    if secs or not parts:
        parts.append("%d 秒" % secs)
    return " ".join(parts)


def deep_merge(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def tail_text(text, limit):
    if not limit or limit <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    head = "……（共 %d 行，仅显示最后 %d 行）" % (len(lines), limit)
    return "\n".join([head] + lines[-limit:])


# --------------------------------------------------------------------------
# 配置与路径
# --------------------------------------------------------------------------
def load_credentials(path):
    """读取 KEY=VALUE 形式的凭据文件，忽略空行与 # 注释。"""
    data = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip().upper()] = value.strip()
    return data


def credential_candidates(cfg, config_dir):
    """按优先级返回凭据文件候选路径。"""
    raw = cfg.get("smtp", {}).get("credential_file")
    if raw:
        path = Path(raw).expanduser()
        return [path if path.is_absolute() else (Path(config_dir) / path).resolve()]
    env_path = os.environ.get("CAMPUS_MAIL_CREDENTIALS")
    if env_path:
        return [Path(env_path).expanduser()]
    return [
        Path(config_dir) / "credentials.txt",
        DEFAULT_HOME / "credentials.txt",
        Path.cwd() / "credentials.txt",
        Path.cwd() / "task_reporter" / "credentials.txt",
    ]


def credential_path_of(cfg, config_dir):
    candidates = credential_candidates(cfg, config_dir)
    for item in candidates:
        if item.is_file():
            return item
    return candidates[0]


def env_value(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def load_config(config_path=None):
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit("配置文件解析失败：%s (%s)" % (path, exc))
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    cfg["_config_path"] = str(path)
    cfg["_config_exists"] = path.exists()

    # 第二优先级：共享凭据文件（低于环境变量，高于配置文件中的明文字段）
    cred_path = credential_path_of(cfg, path.parent)
    cfg["_credential_path"] = str(cred_path)
    cfg["_credential_exists"] = cred_path.exists()
    if cred_path.exists():
        try:
            creds = load_credentials(cred_path)
        except OSError as exc:
            raise SystemExit("凭据文件读取失败：%s (%s)" % (cred_path, exc))
        smtp = cfg["smtp"]
        if creds.get("SMTP_HOST"):
            smtp["host"] = creds["SMTP_HOST"]
        if creds.get("SMTP_PORT"):
            smtp["port"] = int(creds["SMTP_PORT"])
        if creds.get("SMTP_SECURITY"):
            smtp["security"] = creds["SMTP_SECURITY"]
        if creds.get("SMTP_SENDER"):
            smtp["sender"] = creds["SMTP_SENDER"]
        if creds.get("SMTP_PASSWORD"):
            smtp["password"] = creds["SMTP_PASSWORD"]

    smtp = cfg["smtp"]
    host = env_value("CAMPUS_MAIL_HOST", "TASK_REPORT_SMTP_HOST")
    port = env_value("CAMPUS_MAIL_PORT", "TASK_REPORT_SMTP_PORT")
    security = env_value("CAMPUS_MAIL_SECURITY", "TASK_REPORT_SECURITY")
    sender = env_value("CAMPUS_MAIL_SENDER", "TASK_REPORT_SENDER")
    password = env_value("CAMPUS_MAIL_PASSWORD", "TASK_REPORT_PASSWORD")
    recipients = env_value("CAMPUS_MAIL_TO", "TASK_REPORT_TO")
    if host:
        smtp["host"] = host
    if port:
        smtp["port"] = int(port)
    if security:
        smtp["security"] = security
    if sender:
        smtp["sender"] = sender
    if password:
        smtp["password"] = password
    if recipients:
        cfg["notify"]["to"] = [s.strip() for s in recipients.split(",") if s.strip()]
    return cfg


def resolve_paths(cfg):
    config_path = Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)
    raw_base = str(cfg.get("base_dir") or "cwd")
    if raw_base.lower() == "cwd":
        base = Path.cwd()
    elif raw_base.lower() == "config":
        base = config_path.parent
    else:
        base = Path(raw_base).expanduser()
        if not base.is_absolute():
            base = (config_path.parent / base).resolve()
    paths = {}
    for key, default in (
        ("log", ".task_notify/task_notifier.log"),
        ("jsonl", ".task_notify/task_notifier.jsonl"),
        ("notify_log", ".task_notify/task_notifier_notify.log"),
        ("state", ".task_notify/.task_notifier_state.json"),
    ):
        item = Path(cfg.get("paths", {}).get(key) or default)
        paths[key] = item if item.is_absolute() else (base / item)
    return paths


def ensure_dirs(paths):
    for item in paths.values():
        Path(item).parent.mkdir(parents=True, exist_ok=True)


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path, offset):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"offset": int(offset), "updated_at": iso(now())}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def read_slice(path, start, end):
    length = max(0, int(end) - int(start))
    if length == 0:
        return ""
    with open(path, "rb") as handle:
        handle.seek(int(start))
        data = handle.read(length)
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 日志读写
# --------------------------------------------------------------------------
def format_log_line(record):
    exit_code = record.get("exit_code")
    exit_text = "退出码 %s" % exit_code if exit_code is not None else "无退出码"
    return " | ".join(
        [
            str(record.get("finished_at", ""))[:19].replace("T", " "),
            STATUS_LABELS.get(record.get("status"), str(record.get("status"))),
            "用时 " + fmt_duration(record.get("duration_seconds")),
            str(record.get("task", "未命名任务")),
            exit_text,
            "主机 " + str(record.get("host", "")),
        ]
    )


def rotate_if_needed(path, max_bytes):
    """日志超过阈值时重命名为 .1，只保留一份历史，避免长期运行无限增长。"""
    target = Path(path)
    if not max_bytes or max_bytes <= 0 or not target.exists():
        return False
    try:
        if target.stat().st_size < int(max_bytes):
            return False
        backup = target.with_name(target.name + ".1")
        if backup.exists():
            backup.unlink()
        os.replace(target, backup)
        return True
    except OSError:
        return False


def append_record(paths, record, max_bytes=0):
    log_path = Path(paths["log"])
    jsonl_path = Path(paths["jsonl"])
    ensure_dirs({"log": log_path, "jsonl": jsonl_path})
    rotate_if_needed(log_path, max_bytes)
    rotate_if_needed(jsonl_path, max_bytes)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(format_log_line(record) + "\n")
    with open(jsonl_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_notice(paths, ok, subject, detail):
    line = " | ".join(
        [
            now().strftime("%Y-%m-%d %H:%M:%S"),
            "邮件已发送" if ok else "邮件发送失败",
            subject,
            str(detail),
        ]
    )
    with open(paths["notify_log"], "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_recent(path, limit):
    target = Path(path)
    if not target.exists():
        return []
    with open(target, encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=limit)
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def tail_of_log(path, limit):
    target = Path(path)
    if not target.exists():
        return ""
    with open(target, encoding="utf-8", errors="replace") as handle:
        if limit and limit > 0:
            return "".join(deque(handle, maxlen=limit))
        return handle.read()


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------
def smtp_password(cfg):
    smtp = cfg.get("smtp", {})
    env_key = smtp.get("password_env")
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return smtp.get("password") or ""


def build_message(cfg, subject, text, html_body=None, attachments=(), recipients=None):
    smtp = cfg.get("smtp", {})
    sender = smtp.get("sender", "")
    to_list = recipients or cfg.get("notify", {}).get("to") or []
    if not sender:
        raise RuntimeError("未配置发件人 smtp.sender")
    if not to_list:
        raise RuntimeError("未配置收件人：请在配置 notify.to 中填写，或设置环境变量 CAMPUS_MAIL_TO")

    message = MIMEMultipart("mixed")
    display_name = smtp.get("display_name") or ""
    message["From"] = formataddr((display_name, sender)) if display_name else sender
    message["To"] = ", ".join(to_list)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(text, "plain", "utf-8"))
    if html_body:
        alternative.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alternative)

    for item in attachments or ():
        file_path = Path(item).expanduser()
        if not file_path.is_file():
            raise RuntimeError("附件不存在：%s" % item)
        with open(file_path, "rb") as handle:
            part = MIMEApplication(handle.read())
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", file_path.name),
        )
        message.attach(part)

    return message, to_list


def open_smtp(cfg):
    smtp = cfg.get("smtp", {})
    host = smtp.get("host")
    if not host:
        raise RuntimeError("未配置 SMTP 主机 smtp.host")
    port = int(smtp.get("port", 465))
    security = str(smtp.get("security", "ssl")).lower()
    timeout = float(smtp.get("timeout", 30))
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    server = smtplib.SMTP(host, port, timeout=timeout)
    if security == "starttls":
        server.starttls()
    return server


def send_mail(cfg, subject, text, html_body=None, attachments=(), recipients=None, dry_run=False):
    """返回 (是否成功, 说明文本)。"""
    try:
        message, to_list = build_message(cfg, subject, text, html_body, attachments, recipients)
    except Exception as exc:  # 配置或附件错误
        return False, str(exc)

    if dry_run:
        print("---------- 邮件预览（未实际发送） ----------")
        print("收件人：%s" % ", ".join(to_list))
        print("主题：%s" % subject)
        print(text)
        print("--------------------------------------------")
        return True, "dry-run"

    smtp = cfg.get("smtp", {})
    sender = smtp.get("sender", "")
    password = smtp_password(cfg)
    retries = int(cfg.get("notify", {}).get("retries", 3) or 1)
    last_error = ""

    for attempt in range(1, retries + 1):
        server = None
        try:
            server = open_smtp(cfg)
            if password:
                server.login(sender, password)
            server.sendmail(sender, to_list, message.as_string())
            return True, "已发送至 %s" % ", ".join(to_list)
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
    return False, last_error


def report_payload(cfg, record, tail=""):
    prefix = cfg.get("notify", {}).get("subject_prefix", "[任务日志]")
    status_label = STATUS_LABELS.get(record.get("status"), "任务结束")
    duration_text = fmt_duration(record.get("duration_seconds"))
    subject = "%s %s · %s · 用时 %s" % (
        prefix,
        status_label,
        record.get("task", "任务"),
        duration_text,
    )

    exit_code = record.get("exit_code")
    rows = [
        ("任务", record.get("task", "未命名任务")),
        ("状态", status_label),
        ("开始时间", str(record.get("started_at", "未记录"))),
        ("结束时间", str(record.get("finished_at", "未记录"))),
        ("耗时", "%s（%.1f 秒）" % (duration_text, float(record.get("duration_seconds") or 0.0))),
        ("退出码", str(exit_code) if exit_code is not None else "无"),
        ("主机", str(record.get("host", ""))),
        ("工作目录", str(record.get("cwd", ""))),
        ("执行用户", str(record.get("user", ""))),
    ]
    if record.get("command"):
        rows.append(("命令", str(record["command"])))
    if record.get("note"):
        rows.append(("备注", str(record["note"])))

    text_lines = ["%s：%s" % (key, value) for key, value in rows]
    text = "\n".join(text_lines)
    if tail.strip():
        text += "\n\n日志最近内容：\n" + tail.strip()

    table = "".join(
        '<tr><td style="padding:6px 10px;border:1px solid #dcdcdc;background:#f7f9fb;'
        'white-space:nowrap;font-weight:600;">%s</td>'
        '<td style="padding:6px 10px;border:1px solid #dcdcdc;">%s</td></tr>'
        % (html.escape(key), html.escape(str(value)).replace("\n", "<br>"))
        for key, value in rows
    )
    tail_block = ""
    if tail.strip():
        tail_block = (
            '<h3 style="margin:20px 0 8px;font-size:14px;">日志最近内容</h3>'
            '<pre style="margin:0;padding:12px;background:#f5f5f5;border:1px solid #dcdcdc;'
            'font-size:12px;line-height:1.5;white-space:pre-wrap;">%s</pre>'
            % html.escape(tail.strip())
        )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;font-size:14px;color:#222;">'
        '<h3 style="margin:0 0 12px;font-size:16px;">%s</h3>'
        '<table style="border-collapse:collapse;font-size:13px;">%s</table>%s'
        '<p style="margin:18px 0 0;color:#888;font-size:12px;">本邮件由 campus-mail-task-notify 技能自动发送。</p>'
        "</div>" % (html.escape(subject), table, tail_block)
    )
    return subject, text, html_body


# --------------------------------------------------------------------------
# 记录与通知
# --------------------------------------------------------------------------
def make_record(task, status, started, finished, duration, exit_code=None, command=None, note=""):
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "")
    return {
        "task": task,
        "status": status,
        "started_at": iso(started),
        "finished_at": iso(finished),
        "duration_seconds": round(float(duration), 3),
        "duration_text": fmt_duration(duration),
        "exit_code": exit_code,
        "command": command or "",
        "note": note or "",
        "host": socket.gethostname(),
        "cwd": str(Path.cwd()),
        "user": user,
    }


def finalize(cfg, record, attachments=(), dry_run=False, email_on="always", recipients=None):
    paths = resolve_paths(cfg)
    ensure_dirs(paths)
    append_record(paths, record, int(cfg.get("max_log_bytes") or 0))
    print(
        "[%s] %s | 用时 %s | 退出码 %s | 日志 %s"
        % (
            STATUS_LABELS.get(record["status"], "任务结束"),
            record["task"],
            record["duration_text"],
            record["exit_code"] if record["exit_code"] is not None else "-",
            paths["log"],
        )
    )

    notify_cfg = cfg.get("notify", {})
    if not notify_cfg.get("enabled", True) or email_on == "never":
        return True
    if email_on == "failure" and record["status"] == "success":
        return True

    tail = tail_of_log(paths["log"], int(notify_cfg.get("tail_lines", 60) or 0))
    subject, text, html_body = report_payload(cfg, record, tail)
    ok, detail = send_mail(cfg, subject, text, html_body, attachments,
                           recipients=recipients, dry_run=dry_run)
    if not dry_run:
        write_notice(paths, ok, subject, detail)
        if ok:
            save_state(paths["state"], paths["log"].stat().st_size)
    print("[邮件] %s：%s" % ("成功" if ok else "失败", detail))
    return ok


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_init(cfg, args):
    target = Path(cfg["_config_path"])
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not args.force:
        print("配置文件已存在：%s（如需覆盖请加 --force）" % target)
    else:
        template = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
        template["base_dir"] = "cwd"
        template["smtp"]["credential_file"] = "credentials.txt"
        target.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("已生成配置文件：%s" % target)

    credential = target.parent / "credentials.txt"
    if not credential.exists():
        credential.write_text(CREDENTIAL_TEMPLATE, encoding="utf-8")
        os.chmod(credential, 0o600)
        print("已生成凭据模板：%s（权限 600，请填入真实账号密码）" % credential)
    else:
        print("凭据文件已存在：%s（未改动）" % credential)

    print("下一步：")
    print("  1) 在 %s 中填入 SMTP_SENDER 与 SMTP_PASSWORD" % credential)
    print("  2) 在 %s 的 notify.to 中填写收件邮箱" % target)
    print("  3) 执行：python3 %s test-mail" % Path(__file__).name)
    return 0


def cmd_run(cfg, args):
    command = list(args.command or [])
    while command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit(
            "run 需要待执行的命令，例如：\n"
            "  python3 task_notifier.py run --name 训练 -- python -m scripts.xxx.train -c cfg.json"
        )

    task = args.name or Path(shlex.split(command[0])[0]).name
    printable = command[0] if len(command) == 1 else " ".join(shlex.quote(part) for part in command)
    print("[开始] %s | %s" % (task, now().strftime("%Y-%m-%d %H:%M:%S")))
    print("[命令] %s" % printable)
    sys.stdout.flush()

    started = now()
    clock = time.monotonic()
    status = "unknown"
    exit_code = None
    try:
        if args.shell:
            exit_code = subprocess.call(printable, shell=True)
        else:
            exit_code = subprocess.call(command)
        status = "success" if exit_code == 0 else "failure"
    except KeyboardInterrupt:
        exit_code = 130
        status = "interrupted"
        print("\n[中断] 收到 Ctrl+C，正在记录日志")
    except FileNotFoundError as exc:
        exit_code = 127
        status = "failure"
        print("[错误] 找不到可执行文件或脚本：%s" % exc)
    finally:
        duration = time.monotonic() - clock
        finished = now()

    record = make_record(
        task=task,
        status=status,
        started=started,
        finished=finished,
        duration=duration,
        exit_code=exit_code,
        command=printable,
        note=args.note or "",
    )
    finalize(cfg, record, args.attach, args.dry_run, args.email_on, args.to)
    return int(exit_code or 0)


def cmd_record(cfg, args):
    started_at = None
    finished_at = None
    if args.started:
        started_at = datetime.fromisoformat(args.started).astimezone()
    if args.finished:
        finished_at = datetime.fromisoformat(args.finished).astimezone()

    finished = finished_at or now()
    if args.duration is not None:
        duration = float(args.duration)
        started = started_at or datetime.fromtimestamp(finished.timestamp() - duration).astimezone()
    elif started_at is not None:
        started = started_at
        duration = (finished - started).total_seconds()
    else:
        started = finished
        duration = 0.0

    record = make_record(
        task=args.name,
        status=args.status,
        started=started,
        finished=finished,
        duration=duration,
        exit_code=args.exit_code,
        command=args.command or "",
        note=args.note or "",
    )
    finalize(cfg, record, args.attach, args.dry_run, args.email_on, args.to)
    return 0


def notify_log_update(cfg, chunk, dry_run=False):
    if not chunk.strip():
        return True
    prefix = cfg.get("notify", {}).get("subject_prefix", "[任务日志]")
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "%s 日志更新 · %s" % (prefix, stamp)
    text = (
        "检测到任务日志文件发生更新。\n\n"
        "时间：%s\n日志文件：%s\n\n新增内容：\n%s\n"
        % (stamp, resolve_paths(cfg)["log"], chunk.strip())
    )
    escaped = html.escape(chunk.strip())
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;font-size:14px;color:#222;">'
        '<h3 style="margin:0 0 10px;font-size:16px;">任务日志更新</h3>'
        '<p style="margin:0 0 10px;color:#555;">时间：%s</p>'
        '<pre style="margin:0;padding:12px;background:#f5f5f5;border:1px solid #dcdcdc;'
        'font-size:12px;line-height:1.5;white-space:pre-wrap;">%s</pre>'
        '<p style="margin:18px 0 0;color:#888;font-size:12px;">本邮件由 campus-mail-task-notify 技能自动发送。</p>'
        "</div>" % (stamp, escaped)
    )
    ok, detail = send_mail(cfg, subject, text, html_body, dry_run=dry_run)
    if not dry_run:
        write_notice(resolve_paths(cfg), ok, subject, detail)
    print("[邮件] %s：%s" % ("成功" if ok else "失败", detail))
    return ok


def cmd_watch(cfg, args):
    paths = resolve_paths(cfg)
    ensure_dirs(paths)
    log_path = Path(paths["log"])
    log_path.touch(exist_ok=True)

    interval = float(args.interval if args.interval is not None else cfg["watch"].get("interval", 5.0))
    settle = float(args.settle if args.settle is not None else cfg["watch"].get("settle", 10.0))
    state = load_state(paths["state"])
    offset = int(state.get("offset", 0) or 0)
    first_start = not Path(paths["state"]).exists()
    if log_path.stat().st_size < offset:
        offset = 0
    if first_start and not args.from_start:
        offset = log_path.stat().st_size
        print("[初始化] 无历史状态文件，从当前日志末尾开始监视（如需处理已有内容请加 --from-start）")

    print("[监视] %s" % log_path)
    print("[参数] 轮询 %.1f 秒，静默 %.1f 秒后发送%s" % (interval, settle, "，单次模式" if args.once else ""))
    sys.stdout.flush()

    pending_from = None
    last_growth = time.monotonic()
    sent_any = False
    last_size = offset
    while True:
        size = log_path.stat().st_size
        if size < offset:
            print("[提示] 日志被截断或重写，重置读取位置")
            offset = 0
            pending_from = None
            last_size = 0
        if size > last_size:  # 仅在确有新增内容时刷新静默计时
            if pending_from is None:
                pending_from = offset
            last_growth = time.monotonic()
            last_size = size

        due = pending_from is not None and (args.once or (time.monotonic() - last_growth) >= settle)
        if due:
            chunk = read_slice(log_path, pending_from, size)
            print("[变化] 新增 %d 字节，%s" % (size - pending_from, now().strftime("%H:%M:%S")))
            if chunk.strip():
                notify_log_update(cfg, chunk, args.dry_run)
            offset = size
            pending_from = None
            sent_any = True
            if not args.dry_run:
                save_state(paths["state"], offset)

        if args.once:
            if not sent_any:
                print("[单次] 日志无变化")
            return 0
        time.sleep(interval)


def cmd_report(cfg, args):
    paths = resolve_paths(cfg)
    records = read_recent(paths["jsonl"], max(1, args.last))
    print("日志文件：%s" % paths["log"])
    print("结构化记录：%s" % paths["jsonl"])
    if not records:
        print("暂无任务记录。")
        return 0
    print("-" * 100)
    for record in records:
        print(format_log_line(record))
    print("-" * 100)

    total = sum(float(record.get("duration_seconds") or 0.0) for record in records)
    success = sum(1 for record in records if record.get("status") == "success")
    failure = sum(1 for record in records if record.get("status") == "failure")
    others = len(records) - success - failure
    print(
        "合计 %d 条：成功 %d，失败 %d，其他 %d；总耗时 %s；平均耗时 %s"
        % (len(records), success, failure, others, fmt_duration(total), fmt_duration(total / len(records)))
    )
    return 0


def cmd_test_mail(cfg, args):
    recipients = [args.to] if args.to else None
    subject = "%s 连通性测试 · %s" % (
        cfg.get("notify", {}).get("subject_prefix", "[任务日志]"),
        now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    text = (
        "这是一封来自 campus-mail-task-notify 技能的测试邮件。\n\n"
        "主机：%s\n配置：%s\nSMTP：%s:%s（%s）\n"
        % (
            socket.gethostname(),
            cfg["_config_path"],
            cfg["smtp"].get("host"),
            cfg["smtp"].get("port"),
            cfg["smtp"].get("security"),
        )
    )
    ok, detail = send_mail(cfg, subject, text, recipients=recipients, dry_run=args.dry_run)
    print("[邮件] %s：%s" % ("成功" if ok else "失败", detail))
    return 0 if ok else 1


def cmd_show_config(cfg, args):
    shown = json.loads(json.dumps(cfg, ensure_ascii=False))
    for key in ("_config_path", "_config_exists", "_credential_path", "_credential_exists"):
        shown.pop(key, None)
    if shown.get("smtp", {}).get("password"):
        shown["smtp"]["password"] = "***"
    print("配置文件：%s" % cfg["_config_path"])
    print(
        "凭据文件：%s（%s）"
        % (cfg["_credential_path"], "存在" if cfg["_credential_exists"] else "不存在")
    )
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    paths = resolve_paths(cfg)
    print("解析后的路径：")
    for key, value in paths.items():
        print("  %-10s %s" % (key, value))
    env_key = cfg["smtp"].get("password_env") or ""
    if env_key and os.environ.get(env_key):
        source = "环境变量 %s" % env_key
    elif cfg["_credential_exists"]:
        source = "凭据文件 %s" % cfg["_credential_path"]
    elif cfg["smtp"].get("password"):
        source = "配置文件"
    else:
        source = "未设置"
    print("密码来源：%s" % source)
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def add_common_flags(sub_parser):
    """让子命令前后的 --config / --dry-run 都能生效，且不覆盖全局取值。"""
    sub_parser.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（同全局 --config）")
    sub_parser.add_argument("--dry-run", default=argparse.SUPPRESS, action="store_true",
                            help="只打印邮件内容，不实际发送")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="task_notifier.py",
        description="任务结束邮件通知器（校园邮箱）：计时、记录日志并在任务结束时发信",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--config", default=os.environ.get("CAMPUS_MAIL_CONFIG") or str(DEFAULT_CONFIG_PATH),
                        help="配置文件路径（默认 ~/.config/campus_mail/config.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印邮件内容，不实际发送")
    parser.add_argument("--version", action="version", version="task_notifier.py " + VERSION)

    sub = parser.add_subparsers(dest="action", required=True)

    init_parser = sub.add_parser("init", help="生成配置文件模板")
    init_parser.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    add_common_flags(init_parser)
    init_parser.set_defaults(func=cmd_init)

    run_parser = sub.add_parser("run", help="执行命令并计时、记录、通知")
    run_parser.add_argument("--name", help="任务名称（默认为命令对应的程序名）")
    run_parser.add_argument("--note", default="", help="备注信息")
    run_parser.add_argument("--attach", action="append", default=[], help="邮件附件路径，可重复")
    run_parser.add_argument("--shell", action="store_true", help="通过 shell 执行（支持管道等语法）")
    run_parser.add_argument("--email-on", choices=["always", "failure", "never"], default="always",
                            help="邮件触发条件，默认 always")
    run_parser.add_argument("--to", action="append", default=[], help="临时收件邮箱，可重复，覆盖配置")
    run_parser.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令，前面加 --")
    add_common_flags(run_parser)
    run_parser.set_defaults(func=cmd_run)

    record_parser = sub.add_parser("record", help="登记一条已结束的任务")
    record_parser.add_argument("--name", required=True, help="任务名称")
    record_parser.add_argument("--status", choices=list(STATUS_LABELS), default="success", help="任务状态")
    record_parser.add_argument("--started", help="开始时间，ISO 格式，如 2026-10-23T15:00:00+08:00")
    record_parser.add_argument("--finished", help="结束时间，ISO 格式，默认为当前时间")
    record_parser.add_argument("--duration", type=float, help="耗时秒数（未给出开始时间时使用）")
    record_parser.add_argument("--exit-code", type=int, help="退出码")
    record_parser.add_argument("--command", help="关联的命令文本")
    record_parser.add_argument("--note", default="", help="备注信息")
    record_parser.add_argument("--attach", action="append", default=[], help="邮件附件路径，可重复")
    record_parser.add_argument("--email-on", choices=["always", "failure", "never"], default="always")
    record_parser.add_argument("--to", action="append", default=[], help="临时收件邮箱，可重复，覆盖配置")
    add_common_flags(record_parser)
    record_parser.set_defaults(func=cmd_record)

    watch_parser = sub.add_parser("watch", help="监视日志文件，变更时发送邮件")
    watch_parser.add_argument("--interval", type=float, help="轮询间隔秒数，默认取配置")
    watch_parser.add_argument("--settle", type=float, help="静默多少秒后发送，默认取配置")
    watch_parser.add_argument("--once", action="store_true", help="只检查一次即退出（适合 cron）")
    watch_parser.add_argument("--from-start", action="store_true", help="首次启动时处理日志中已有内容")
    add_common_flags(watch_parser)
    watch_parser.set_defaults(func=cmd_watch)

    report_parser = sub.add_parser("report", help="打印最近的任务记录与耗时统计")
    report_parser.add_argument("--last", type=int, default=10, help="显示的记录条数，默认 10")
    add_common_flags(report_parser)
    report_parser.set_defaults(func=cmd_report)

    test_parser = sub.add_parser("test-mail", help="发送测试邮件以验证 SMTP 配置")
    test_parser.add_argument("--to", help="临时指定收件人")
    add_common_flags(test_parser)
    test_parser.set_defaults(func=cmd_test_mail)

    show_parser = sub.add_parser("show-config", help="打印生效配置（密码打码）")
    add_common_flags(show_parser)
    show_parser.set_defaults(func=cmd_show_config)

    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 便于后台运行时实时查看输出
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if not cfg["_config_exists"] and args.action != "init":
        print("[提示] 未找到配置文件 %s，将使用内置默认值；建议先执行 init。" % cfg["_config_path"])
    try:
        return args.func(cfg, args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n[中断] 已退出")
        return 130
    except BrokenPipeError:  # 输出被 head 等命令截断时安静退出
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
