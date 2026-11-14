#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_checkin_earned.py —— 「签到成功但到账 +0」回归

背景（线上实测）：/trae/api/v2/ug/checkin_credits/status 返回的 credits 是
「每日签到奖励额」（常量 100，与 extra_credits 同值），**不是账户积分余额**。
旧实现用 after.credits - before.credits 算到账，两边都是 100，差值恒为 0，
于是每次签到都显示「到账 +0」，而实际 100 分已经作为「签到奖励」额度包到账。

覆盖：
  · 到账以「签到奖励包增量」为准（最直接证据）；
  · 签到包读不到时退化到余额增量；
  · 余额也读不到 / 上游尚未结算时，用响应里的奖励额兜底，而不是报 0；
  · 上游未确认签到（checked_in 仍为 false）时如实报「金额未知」，不编数字；
  · 任务层文案：金额未知时不能打印「到账 +0」；
  · 已签到的幂等路径不发领取请求、不记到账。

跑法：python test_checkin_earned.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_client                    # noqa: E402
import trae_tasks                     # noqa: E402


def ent(balance=None, packs=0, pack_limit=100, consumed=None,
        pack_name="签到奖励", nested=False):
    """构造一份 ent_usage；packs 是「签到奖励」额度包个数。

    nested=True 复刻线上真实层级：credits_limit 在
    entitlement_base_info.quota / product_extra.package_extra.quota 上，
    包顶层 quota 为 null。
    """
    usage = {}
    if balance is not None:
        usage["total_balance"] = balance
    pack_list = []
    for _ in range(packs):
        if nested:
            pack_list.append({
                "display_desc": pack_name,
                "entitlement_base_info": {
                    "product_id": 208,
                    "quota": {"credits_limit": pack_limit},
                    "product_extra": {"package_extra": {
                        "package_name": pack_name,
                        "quota": {"credits_limit": pack_limit}}},
                },
                "group_name": "每日签到",
            })
        else:
            pack_list.append({"display_desc": pack_name,
                              "quota": {"credits_limit": pack_limit}})
    usage["user_entitlement_pack_list"] = pack_list
    if consumed is not None:
        usage["usage_summary"] = {"consumed_amount": consumed}
    return usage


def status(checked_in, credits=100, extra=100):
    return {"checked_in": checked_in, "code": 0, "credits": credits,
            "did_checked_in": False, "enable": True, "extra_credits": extra,
            "message": "success"}


class Upstream(object):
    """按队列喂给 claim_checkin 的上游桩。"""

    def __init__(self, statuses, balances, claim=None):
        self.statuses = list(statuses)
        self.balances = list(balances)
        self.claim = claim if claim is not None else {"code": 0, "message": "success"}
        self.claim_calls = 0

    def checkin_status(self, account, realm=None, timeout=30):
        return dict(self.statuses.pop(0))

    def request_json(self, account, realm=None, path="", body=None, **kwargs):
        self.claim_calls += 1
        return dict(self.claim)

    def fetch_entitlement(self, account, realm=None, strict=False, timeout=45):
        value = self.balances.pop(0) if self.balances else None
        if value is None:
            return {"realm": "cn", "ent_usage": None, "pay_status": None,
                    "errors": [{"endpoint": "ent_usage", "code": "network"}]}
        return {"realm": "cn", "ent_usage": value, "pay_status": None,
                "errors": []}


def patch_upstream(upstream):
    return mock.patch.multiple(
        trae_client,
        checkin_status=upstream.checkin_status,
        request_json=upstream.request_json,
        fetch_entitlement=upstream.fetch_entitlement)


ACCOUNT = {"uid": "3330536375", "access_token": "t", "realm": "cn"}


class EarnedCreditTests(unittest.TestCase):
    def run_claim(self, upstream):
        with patch_upstream(upstream):
            return trae_client.claim_checkin(ACCOUNT, realm="cn")

    def test_uses_checkin_pack_delta_not_status_credits(self):
        """状态接口的 credits 恒为 100，到账必须按签到包增量算。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[ent(1006.35, packs=6), ent(1106.35, packs=7)])
        result = self.run_claim(upstream)
        self.assertTrue(result["ok"])
        self.assertFalse(result["already"])
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "checkin_pack")
        # 不能把常量 100 当成余额
        self.assertNotEqual(result["credits"], 1006.35)

    def test_reads_nested_pack_quota_like_production(self):
        """线上真实层级：credits_limit 在 entitlement_base_info 里，包顶层是 null。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[ent(1003.85, packs=6, consumed=96.15, nested=True),
                      ent(1103.85, packs=7, consumed=96.15, nested=True)])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "checkin_pack")
        self.assertEqual(result["balance"]["before"]["checkin_packs"], 600.0)
        self.assertEqual(result["balance"]["after"]["checkin_packs"], 700.0)

    def test_falls_back_to_balance_delta(self):
        """签到包名字对不上（上游改文案）时，用余额增量。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[{"total_balance": 1006.35}, {"total_balance": 1106.35}])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "balance_diff")

    def test_unreadable_entitlement_falls_back_to_reward_amount(self):
        """权益接口挂了也要报真实奖励额，而不是 +0。"""
        upstream = Upstream(statuses=[status(False), status(True)],
                            balances=[None, None])
        result = self.run_claim(upstream)
        self.assertTrue(result["ok"])
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "reward")

    def test_stale_balance_still_reports_reward(self):
        """上游结算有延迟（余额还没动）时，也不能报 0。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[ent(1006.35, packs=6), ent(1006.35, packs=6)])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "reward")

    def test_concurrent_consumption_is_added_back(self):
        """签到期间有流量在扣积分：Δbalance 少了 50，必须用 Δconsumed 补回。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            # 包名对不上（上游改文案）→ 只能看余额；期间消耗 50，发放 100
            balances=[ent(1006.35, packs=6, consumed=93.65, pack_name="其他奖励"),
                      ent(1056.35, packs=6, consumed=143.65, pack_name="其他奖励")])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "balance_diff")

    def test_polluted_balance_without_consumption_data_uses_reward(self):
        """没有消耗数据可校正、余额增量又低于声明奖励额 → 采信声明值。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[ent(1006.35, packs=6, pack_name="其他奖励"),
                      ent(1056.35, packs=6, pack_name="其他奖励")])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "reward")

    def test_negative_balance_delta_never_becomes_zero(self):
        """消耗比发放还多（Δbalance < 0）也不能算成 0。"""
        upstream = Upstream(
            statuses=[status(False), status(True)],
            balances=[ent(1006.35, packs=6, pack_name="其他奖励"),
                      ent(956.35, packs=6, pack_name="其他奖励")])
        result = self.run_claim(upstream)
        self.assertEqual(result["earned_credit"], 100)
        self.assertEqual(result["earned_source"], "reward")

    def test_already_checked_in_is_idempotent(self):
        upstream = Upstream(statuses=[status(True)], balances=[])
        result = self.run_claim(upstream)
        self.assertTrue(result["already"])
        self.assertEqual(result["earned_credit"], 0)
        self.assertEqual(upstream.claim_calls, 0)

    def test_unconfirmed_claim_reports_unknown_not_zero(self):
        upstream = Upstream(
            statuses=[status(False), status(False)],
            balances=[ent(1006.35, packs=6), ent(1006.35, packs=6)])
        result = self.run_claim(upstream)
        self.assertFalse(result["ok"])
        self.assertEqual(result["earned_credit"], 0)
        self.assertEqual(result["earned_source"], "unknown")

    def test_raw_keeps_before_and_after_for_audit(self):
        upstream = Upstream(statuses=[status(False), status(True)],
                            balances=[ent(1006.35, packs=6), ent(1106.35, packs=7)])
        result = self.run_claim(upstream)
        self.assertIn("before", result["raw"])
        self.assertIn("status", result["raw"])
        self.assertIn("claim", result["raw"])


class FakeAccount(object):
    def __init__(self, uid="3330536375", realm="cn"):
        self.uid = uid
        self.nickname = "用户" + uid
        self.realm = realm
        self.enabled = True
        self.access_token = "t"
        self.extra = {}
        self.usage = None
        self.entitlement = None
        self.usage_updated_at = 0

    def save(self):
        return True


class FakeClient(object):
    def __init__(self, result):
        self.result = result

    def claim_checkin(self, account, realm=None):
        return dict(self.result)


class TaskLogWordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-earned-")
        self.env = mock.patch.dict(os.environ, {
            "TRAE_PROXY_USAGE_DIR": self.tmp, "USAGE_DIR": self.tmp}, clear=False)
        self.env.start()
        trae_tasks.set_logger(None)

    def tearDown(self):
        trae_tasks.set_logger(None)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_task(self, client, trigger="startup"):
        pacing = trae_tasks.make_pacing("checkin", interval=0, jitter=0,
                                        sleep=lambda _s: None)
        with mock.patch.object(trae_tasks, "_client_module", lambda: client):
            return trae_tasks.task_daily_checkin(
                accounts=[FakeAccount()], trigger=trigger, pacing=pacing)

    def test_unknown_amount_is_not_printed_as_zero(self):
        client = FakeClient({
            "ok": True, "already": False, "checked_in": True, "credits": 100,
            "earned_credit": 0, "earned_source": "unknown", "message": "success"})
        result = self.run_task(client)
        self.assertTrue(result["ok"])
        joined = "\n".join(result["logs"])
        self.assertNotIn("到账 +0", joined)
        self.assertIn("金额未知", joined)
        self.assertEqual(result["stats"]["earned_unknown"], 1)
        self.assertIn("金额未知", result["summary"])

    def test_reward_source_counts_towards_total(self):
        client = FakeClient({
            "ok": True, "already": False, "checked_in": True, "credits": 100,
            "earned_credit": 100, "earned_source": "reward", "message": "success"})
        result = self.run_task(client)
        self.assertIn("到账 +100", "\n".join(result["logs"]))
        self.assertEqual(result["stats"]["earned_credit"], 100)
        self.assertEqual(result["stats"]["earned_unknown"], 0)
        self.assertEqual(result["stats"]["earned_sources"], {"reward": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
