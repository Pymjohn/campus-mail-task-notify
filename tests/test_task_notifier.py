# -*- coding: utf-8 -*-
"""task_notifier.py 的单元测试，仅使用标准库，不访问网络、不写入用户目录。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "task_notifier.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import task_notifier as tn  # noqa: E402


class DurationTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(tn.fmt_duration(3.4), "3.4 秒")

    def test_minutes(self):
        self.assertEqual(tn.fmt_duration(83), "1 分 23 秒")

    def test_hours(self):
        self.assertEqual(tn.fmt_duration(3723), "1 小时 2 分 3 秒")

    def test_invalid(self):
        self.assertEqual(tn.fmt_duration(None), "未知")
        self.assertEqual(tn.fmt_duration(-1), "未知")


class CredentialTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.txt"
            path.write_text(
                "# 注释\nSMTP_HOST=smtp.example.edu\nSMTP_PORT=465\n\nSMTP_PASSWORD=a=b\n",
                encoding="utf-8",
            )
            data = tn.load_credentials(path)
        self.assertEqual(data["SMTP_HOST"], "smtp.example.edu")
        self.assertEqual(data["SMTP_PORT"], "465")
        self.assertEqual(data["SMTP_PASSWORD"], "a=b")

    def test_missing_file(self):
        with self.assertRaises(OSError):
            tn.load_credentials(Path("/nonexistent/credentials.txt"))

    def test_candidates_from_config(self):
        cfg = {"smtp": {"credential_file": "secret.txt"}}
        candidates = tn.credential_candidates(cfg, Path("/tmp/config-dir"))
        self.assertEqual(candidates, [Path("/tmp/config-dir/secret.txt")])

    def test_env_credential_path(self):
        with mock.patch.dict(os.environ, {"CAMPUS_MAIL_CREDENTIALS": "/tmp/env-cred.txt"}):
            candidates = tn.credential_candidates({"smtp": {}}, Path("/tmp/config-dir"))
        self.assertEqual(candidates, [Path("/tmp/env-cred.txt")])


class ConfigTest(unittest.TestCase):
    def test_deep_merge(self):
        merged = tn.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}})
        self.assertEqual(merged["a"], {"b": 3, "c": 2})

    def test_env_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"notify": {"to": ["old@example.com"]}}), encoding="utf-8")
            env = {"CAMPUS_MAIL_TO": "a@example.com,b@example.com", "CAMPUS_MAIL_PASSWORD": "pw"}
            with mock.patch.dict(os.environ, env):
                cfg = tn.load_config(config)
        self.assertEqual(cfg["notify"]["to"], ["a@example.com", "b@example.com"])
        self.assertEqual(cfg["smtp"]["password"], "pw")

    def test_resolve_cwd(self):
        cfg = {"base_dir": "cwd", "_config_path": "/tmp/config.json", "paths": {}}
        paths = tn.resolve_paths(cfg)
        self.assertEqual(paths["log"], Path.cwd() / ".task_notify" / "task_notifier.log")

    def test_resolve_absolute(self):
        cfg = {"base_dir": "/tmp/base", "_config_path": "/tmp/config.json",
               "paths": {"log": "custom.log"}}
        paths = tn.resolve_paths(cfg)
        self.assertEqual(paths["log"], Path("/tmp/base/custom.log"))


class LogTest(unittest.TestCase):
    def test_format_line(self):
        record = {
            "finished_at": "2026-10-23T14:00:00+08:00",
            "status": "success",
            "duration_seconds": 65,
            "task": "训练",
            "exit_code": 0,
            "host": "node1",
        }
        line = tn.format_log_line(record)
        self.assertIn("2026-10-23 14:00:00", line)
        self.assertIn("任务完成", line)
        self.assertIn("1 分 5 秒", line)
        self.assertIn("退出码 0", line)

    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                "log": Path(tmp) / "a.log",
                "jsonl": Path(tmp) / "a.jsonl",
                "notify_log": Path(tmp) / "n.log",
                "state": Path(tmp) / "s.json",
            }
            record = tn.make_record("t", "success", tn.now(), tn.now(), 1.0, 0)
            tn.append_record(paths, record)
            tn.append_record(paths, record)
            recent = tn.read_recent(paths["jsonl"], 10)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[-1]["task"], "t")
            self.assertIn("任务完成", Path(paths["log"]).read_text(encoding="utf-8"))

    def test_rotate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.log"
            target.write_text("x" * 100, encoding="utf-8")
            self.assertTrue(tn.rotate_if_needed(target, 50))
            self.assertTrue((Path(tmp) / "a.log.1").exists())
            self.assertFalse(target.exists())
            self.assertFalse(tn.rotate_if_needed(target, 50))

    def test_tail(self):
        text = "\n".join("line%d" % i for i in range(10))
        self.assertIn("仅显示最后 3 行", tn.tail_text(text, 3))


class MailTest(unittest.TestCase):
    def test_build_message(self):
        cfg = {
            "smtp": {"sender": "sender@example.edu", "display_name": "Notifier"},
            "notify": {"to": ["to@example.com"]},
        }
        message, recipients = tn.build_message(cfg, "主题", "正文")
        self.assertEqual(recipients, ["to@example.com"])
        self.assertEqual(message.get("To"), "to@example.com")
        self.assertEqual(message.get("From"), "Notifier <sender@example.edu>")

    def test_missing_recipient(self):
        cfg = {"smtp": {"sender": "sender@example.edu"}, "notify": {"to": []}}
        with self.assertRaises(RuntimeError):
            tn.build_message(cfg, "主题", "正文")

    def test_report_payload(self):
        cfg = {"notify": {"subject_prefix": "[任务通知]"}, "smtp": {}}
        record = tn.make_record("训练", "failure", tn.now(), tn.now(), 12, 1)
        subject, text, html_body = tn.report_payload(cfg, record, "日志")
        self.assertIn("任务失败", subject)
        self.assertIn("训练", subject)
        self.assertIn("退出码：1", text)
        self.assertIn("日志最近内容", html_body)

    def test_dry_run_send(self):
        cfg = {
            "smtp": {"sender": "sender@example.edu", "password": "x"},
            "notify": {"to": ["to@example.com"]},
        }
        ok, detail = tn.send_mail(cfg, "主题", "正文", dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(detail, "dry-run")


class CliTest(unittest.TestCase):
    def run_cli(self, *args, env=None):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=merged, timeout=30,
        )

    def test_version(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn(tn.VERSION, result.stdout)

    def test_run_without_command(self):
        result = self.run_cli("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run 需要待执行的命令", result.stdout + result.stderr)

    def test_show_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("show-config", env={"CAMPUS_MAIL_HOME": tmp})
        self.assertEqual(result.returncode, 0)
        self.assertIn("配置文件：", result.stdout)
        self.assertIn("凭据文件：", result.stdout)


if __name__ == "__main__":
    unittest.main()
