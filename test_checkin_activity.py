#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_checkin_activity.py —— 签到 / 权益流水与自动排程回归

覆盖：
  · 手动签到（成功 / 已签到 / 失败 / 国际版跳过）写 activity.jsonl；
  · 调度器 task_daily_checkin 的「今日已签到」只做状态检查、不写流水（手动才写）；
  · 「刷新权益」写入 kind=refresh 的流水（Trae 没有独立福利包）；
  · 自定义整点排程 / 极简 cron 能被 Scheduler.configure 接受并持久化。

跑法：python test_checkin_activity.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_activity                 # noqa: E402
import trae_scheduler                # noqa: E402
import trae_tasks                    # noqa: E402


class FakeAccount(object):
    def __init__(self, uid="cn1", realm="cn"):
        self.uid = uid
        self.nickname = "acct-" + uid
        self.realm = realm
        self.enabled = True
        self.access_token = "test-token"
        self.refresh_token = "test-refresh"
        self.expires_at = time.time() + 86400
        self.extra = {}
        self.usage = None
        self.entitlement = None
        self.usage_updated_at = 0
        self.path = None
        self.last_error = ""

    def set_usage(self, usage=None, entitlement=None, persist=True):
        if usage:
            self.usage = usage
        if entitlement:
            self.entitlement = entitlement
        self.usage_updated_at = int(time.time())
        return {"ok": True}

    def save(self):
        return True


class FakeClient(object):
    def __init__(self, result=None, error=None):
        self.result = result or {
            "ok": True, "already": False, "checked_in": True,
            "credits": 100, "earned_credit": 100, "message": "success",
        }
        self.error = error

    def claim_checkin(self, account, realm=None):
        if self.error:
            raise self.error
        return dict(self.result)

    def fetch_usage(self, account, realm=None):
        return {"input_token": 3, "output_token": 2, "total_token": 5}

    def fetch_entitlement(self, account, realm=None):
        return {"plan": "Pro", "remain": 10, "total": 100}


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.dir = tempfile.mkdtemp(prefix="checkin-pool-")


class FakeClock(object):
    def __init__(self, ts):
        self.ts = float(ts)

    def __call__(self):
        return self.ts


class FakeRng(object):
    def uniform(self, low, high):
        return float(low) + (float(high) - float(low)) * 0.5


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-activity-")
        self.env = mock.patch.dict(os.environ, {
            "TRAE_PROXY_USAGE_DIR": self.tmp,
            "USAGE_DIR": self.tmp,
        }, clear=False)
        self.env.start()
        # 避免 Scheduler.log 真的 import 网关
        self.proxy_stub = types.SimpleNamespace(
            add_log_entry=lambda *args, **kwargs: None)
        self.sys_patch = mock.patch.dict(
            sys.modules, {"trae_proxy": self.proxy_stub})
        self.sys_patch.start()
        trae_tasks.set_logger(None)

    def tearDown(self):
        trae_tasks.set_logger(None)
        self.sys_patch.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        path = os.path.join(self.tmp, "activity.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def pacing(self):
        return trae_tasks.make_pacing(
            "checkin", interval=0, jitter=0, sleep=lambda _s: None)


class ActivityTests(Base):
    def test_run_checkin_records_success_already_and_failure(self):
        account = FakeAccount()
        client = FakeClient()
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            trae_tasks.run_checkin(account)
        row = self.rows()[-1]
        self.assertEqual(row["kind"], "checkin")
        self.assertEqual(row["state"], "ok")
        self.assertTrue(row["ok"])
        self.assertEqual(row["amount"], 100)
        self.assertEqual(row["trigger"], "manual")
        self.assertIn("签到成功", row["detail"])

        client.result = {
            "ok": True, "already": True, "checked_in": True,
            "credits": 100, "earned_credit": 0, "message": "already",
        }
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            trae_tasks.run_checkin(account)
        row = self.rows()[-1]
        self.assertEqual(row["state"], "already")
        self.assertEqual(row["amount"], 0)

        client = FakeClient(error=RuntimeError("upstream boom"))
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            trae_tasks.run_checkin(account)
        row = self.rows()[-1]
        self.assertEqual(row["state"], "failed")
        self.assertFalse(row["ok"])
        self.assertIn("upstream boom", row["detail"])

    def test_run_checkin_records_intl_skip(self):
        account = FakeAccount(realm="intl")
        with mock.patch.object(trae_tasks, "_client_module", lambda: FakeClient()):
            trae_tasks.run_checkin(account)
        row = self.rows()[-1]
        self.assertEqual(row["kind"], "checkin")
        self.assertEqual(row["state"], "skipped")
        self.assertFalse(row["ok"])
        self.assertIn("国际版", row["detail"])

    def test_scheduler_checkin_skips_already_activity(self):
        """启动/定时巡检的「今日已签到」只是状态检查，不写流水。"""
        account = FakeAccount()
        client = FakeClient(result={
            "ok": True, "already": True, "checked_in": True,
            "credits": 100, "earned_credit": 0, "message": "already",
        })
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            result = trae_tasks.task_daily_checkin(
                accounts=[account], trigger="startup", pacing=self.pacing())
        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["already"], 1)
        self.assertEqual(result["stats"]["earned_credit"], 0)
        self.assertEqual(self.rows(), [])

    def test_manual_checkin_still_records_already(self):
        """手动点击签到仍然保留「已签到」流水（用户需要看到结果）。"""
        account = FakeAccount()
        client = FakeClient(result={
            "ok": True, "already": True, "checked_in": True,
            "credits": 100, "earned_credit": 0, "message": "already",
        })
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            result = trae_tasks.task_daily_checkin(
                accounts=[account], trigger="manual", pacing=self.pacing())
        self.assertTrue(result["ok"])
        row = self.rows()[-1]
        self.assertEqual(row["kind"], "checkin")
        self.assertEqual(row["state"], "already")
        self.assertEqual(row["trigger"], "manual")
        self.assertEqual(row["amount"], 0)

    def test_visible_rows_hides_only_auto_already(self):
        rows = [
            {"state": "already", "trigger": "startup", "amount": 0},
            {"state": "already", "trigger": "hour", "amount": 0},
            {"state": "already", "trigger": "manual", "amount": 0},
            {"state": "ok", "trigger": "startup", "amount": 100},
            {"state": "skipped", "trigger": "startup", "amount": 0},
            {"state": "failed", "trigger": "startup", "amount": 0},
        ]
        out = trae_activity.visible_rows(rows)
        self.assertEqual(len(out), 4)
        self.assertNotIn("startup", [r["trigger"] for r in out
                                     if r["state"] == "already"])
        self.assertIn("manual", [r["trigger"] for r in out
                                 if r["state"] == "already"])

    def test_pro_claim_records_refresh_not_fake_claim(self):
        account = FakeAccount()
        with mock.patch.object(trae_tasks, "_client_module", lambda: FakeClient()):
            result = trae_tasks.run_batch_pro_claim([account])
        self.assertTrue(result["ok"])
        row = self.rows()[-1]
        self.assertEqual(row["kind"], "refresh")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["amount"], 0)
        self.assertIn("无独立福利包", row["detail"])

    def test_summary_counts_skip_separately(self):
        rows = [
            {"ok": True, "state": "ok", "amount": 100},
            {"ok": True, "state": "already", "amount": 0},
            {"ok": False, "state": "skipped", "amount": 0},
            {"ok": False, "state": "failed", "amount": 0},
        ]
        summary = trae_activity.summarize(rows)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["earned_credits"], 100)


class SchedulerConfigTests(Base):
    def make_scheduler(self):
        return trae_scheduler.Scheduler(
            FakePool([FakeAccount()]), state_dir=self.tmp,
            clock=FakeClock(time.mktime((2026, 11, 14, 8, 0, 0, 0, 0, -1))),
            rng=FakeRng(), schedule_jitter_seconds=0)

    def test_custom_hours_persist_and_show_in_status(self):
        scheduler = self.make_scheduler()
        status = scheduler.configure(checkin_times="10:00,22:00",
                                     keepalive_times="23:00")
        self.assertEqual(scheduler.hours, [10, 22, 23])
        self.assertEqual(status["checkin_times"], "10:00,22:00")
        self.assertEqual(status["keepalive_times"], "23:00")
        self.assertIn("10:00", status["mode"])
        # 重新构造：从 state 恢复 hours
        again = self.make_scheduler()
        self.assertEqual(again.hours, [10, 22, 23])
        self.assertEqual(again.checkin_times, "10:00,22:00")
        self.assertEqual(again.keepalive_times, "23:00")

    def test_simple_cron_hours(self):
        scheduler = self.make_scheduler()
        status = scheduler.configure(cron="0 8,20 * * *")
        self.assertEqual(scheduler.hours, [8, 20])
        self.assertEqual(status["cron"], "0 8,20 * * *")
        self.assertEqual(status["checkin_times"], "08:00,20:00")

    def test_invalid_cron_is_rejected(self):
        scheduler = self.make_scheduler()
        with self.assertRaises(ValueError):
            scheduler.configure(cron="5 8 * * *")
        with self.assertRaises(ValueError):
            scheduler.configure(checkin_times="09:30")

    def test_jitter_and_pacing_still_work(self):
        scheduler = self.make_scheduler()
        status = scheduler.configure(schedule_jitter_seconds=60,
                                     checkin_interval=7,
                                     checkin_jitter=3)
        self.assertEqual(scheduler.schedule_jitter_seconds, 60)
        self.assertEqual(status["checkin_pacing"]["interval"], 7)
        self.assertEqual(status["checkin_pacing"]["jitter"], 3)
        self.assertEqual(status["jitter_secs"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
