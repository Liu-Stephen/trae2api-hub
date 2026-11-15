# -*- coding: utf-8 -*-
"""调度派发三项借鉴的回归测试（只读假账号，不触碰真实凭证、不打上游）。

覆盖（全部借自 workbuddy2api-hub）：
  · 区域连坐暂停：同一区域多个不同账号接连被限制 -> 整区暂停派发；
    同一个账号反复失败不算、凭证过期/额度用尽/内容审核不算、暂停到期自动恢复、
    暂停落盘后重启仍然有效、派发闸门直接 429 而不是换号；
  · 会话暖号：粘性号不可用时降级（demote）而不是解绑，历史里的暖号仍被优先试；
  · 超长会话的页轮转：页成员按 uid 稳定、页内轮转独立于整池轮转、
    页轮转不绑粘性、页内无人可用时退回整池。

运行：
    python -m unittest test_issue_dispatch -v
    python test_issue_dispatch.py
"""
import io
import os
import sys
import tempfile
import shutil
import time
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import trae_accounts
import trae_restriction
import trae_settings


# --------------------------------------------------------------------- 夹具
def make_account(uid, realm="cn", token=None, expires_in=3600):
    account = trae_accounts.Account({
        "uid": uid,
        "realm": realm,
        "token": token if token is not None else ("tok-" + uid),
        "expired_at": time.time() + expires_in,
    })
    return account


def make_pool(uids, realm="cn"):
    # AccountPool.__init__ 会把全局 _ACTIVE_POOL 指向自己；测试里建完就还原，
    # 免得同进程里后面的测试把临时池当成「当前活动池」（test_issue_http 同款）。
    previous = getattr(trae_accounts, "_ACTIVE_POOL", None)
    pool = trae_accounts.AccountPool(directory=tempfile.mkdtemp())
    trae_accounts._ACTIVE_POOL = previous
    pool.accounts = [make_account(uid, realm=realm) for uid in uids]
    return pool


class FakePool(object):
    """只实现选号所需最小接口的桩（用于「不认识 page 关键字」的兼容路径）。"""

    def __init__(self, accounts):
        self.accounts = accounts

    def by_realm(self, realm=None):
        return [a for a in self.accounts if not realm or a.realm == realm]


# --------------------------------------------------------------- 会话暖号
class SessionAffinityTests(unittest.TestCase):
    def setUp(self):
        self.affinity = trae_accounts.SessionAffinity(history=3)

    def test_bind_promotes_and_keeps_history(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        self.assertEqual(self.affinity.get("s"), "b")
        self.assertEqual(self.affinity.recent("s"), ["b", "a"])

    def test_rebinding_existing_uid_moves_it_to_front_without_duplicates(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        self.affinity.bind("s", "a")
        self.assertEqual(self.affinity.recent("s"), ["a", "b"])

    def test_history_is_capped(self):
        for uid in ("a", "b", "c", "d", "e"):
            self.affinity.bind("s", uid)
        self.assertEqual(self.affinity.recent("s"), ["e", "d", "c"])
        self.assertEqual(self.affinity.get("s"), "e")

    def test_demote_moves_front_to_back(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        self.affinity.bind("s", "c")
        self.assertEqual(self.affinity.demote("s"), "b")
        self.assertEqual(self.affinity.recent("s"), ["b", "a", "c"])

    def test_demote_specific_uid(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        self.affinity.bind("s", "c")
        self.affinity.demote("s", "b")
        self.assertEqual(self.affinity.recent("s"), ["c", "a", "b"])

    def test_demote_unknown_key_is_noop(self):
        self.assertIsNone(self.affinity.demote("nope"))
        self.assertEqual(self.affinity.recent("nope"), [])

    def test_demote_unknown_uid_leaves_the_queue_alone(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        # 页轮转不绑粘性：出错的号可能根本不在这段会话的暖号队里。那时降级
        # 队首等于把无辜的暖号挤下去（它还握着这段会话的前缀缓存）。
        self.assertEqual(self.affinity.demote("s", "zzz"), "b")
        self.assertEqual(self.affinity.recent("s"), ["b", "a"])

    def test_expired_binding_is_dropped(self):
        self.affinity.ttl = -1          # 立即过期
        self.affinity.bind("s", "a")
        self.assertIsNone(self.affinity.get("s"))
        self.assertEqual(self.affinity.recent("s"), [])

    def test_unbind_removes_everything(self):
        self.affinity.bind("s", "a")
        self.affinity.bind("s", "b")
        self.affinity.unbind("s")
        self.assertIsNone(self.affinity.get("s"))
        self.assertEqual(self.affinity.size(), 0)


# ------------------------------------------------------------- 暖号选号
class WarmAccountPickTests(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool(["uid-1", "uid-2", "uid-3"])
        self.addCleanup(shutil.rmtree, self.pool.dir, ignore_errors=True)

    def test_cooling_front_falls_back_to_another_warm_account(self):
        self.pool.affinity.bind("s", "uid-1")
        self.pool.affinity.bind("s", "uid-2")      # 历史: [uid-2, uid-1]
        self.pool.get("uid-2").note_error("boom", cooldown=60)
        chosen = self.pool.pick(realm="cn", session_key="s")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.uid, "uid-1")      # 暖号顶上来，不是重选冷号
        self.assertEqual(self.pool.affinity.get("s"), "uid-1")

    def test_unusable_front_is_demoted_not_unbound(self):
        self.pool.affinity.bind("s", "uid-1")
        self.pool.affinity.bind("s", "uid-2")
        self.pool.get("uid-2").note_error("boom", cooldown=60)
        self.pool.pick(realm="cn", session_key="s")
        # uid-2 没有被丢掉：它还在队里，等冷却结束仍然是候选
        self.assertIn("uid-2", self.pool.affinity.recent("s"))

    def test_all_warm_accounts_unusable_falls_back_to_pool_and_binds_new(self):
        self.pool.affinity.bind("s", "uid-1")
        self.pool.get("uid-1").note_error("boom", cooldown=60)
        chosen = self.pool.pick(realm="cn", session_key="s")
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen.uid, "uid-1")
        self.assertEqual(self.pool.affinity.get("s"), chosen.uid)
        self.assertIn("uid-1", self.pool.affinity.recent("s"))

    def test_saturated_front_is_skipped_without_losing_history(self):
        self.pool.max_inflight = 1
        self.pool.affinity.bind("s", "uid-1")
        self.pool.get("uid-1").inflight = 1        # 粘性号已饱和
        chosen = self.pool.pick(realm="cn", session_key="s")
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen.uid, "uid-1")
        self.assertIn("uid-1", self.pool.affinity.recent("s"))


# ----------------------------------------------------------- 页轮转
class PageRotationTests(unittest.TestCase):
    def setUp(self):
        self.pool = make_pool(["uid-%d" % i for i in range(1, 7)])
        self.addCleanup(shutil.rmtree, self.pool.dir, ignore_errors=True)

    def test_page_slice_membership_is_stable_and_disjoint(self):
        snapshot = self.pool.by_realm("cn")
        first = [a.uid for a in self.pool._page_slice(snapshot, (0, 2))]
        second = [a.uid for a in self.pool._page_slice(snapshot, (1, 2))]
        self.assertEqual(first, ["uid-1", "uid-3", "uid-5"])
        self.assertEqual(second, ["uid-2", "uid-4", "uid-6"])
        self.assertEqual(set(first) & set(second), set())

    def test_page_covering_the_pool_is_not_a_page(self):
        snapshot = self.pool.by_realm("cn")
        self.assertEqual(len(self.pool._page_slice(snapshot, (0, 6))), 6)
        self.assertEqual(len(self.pool._page_slice(snapshot, (0, 1))), 6)

    def test_page_rotation_cycles_within_the_page(self):
        picked = []
        for _ in range(4):
            account = self.pool.pick(realm="cn", page=(0, 2))
            picked.append(account.uid)
        self.assertEqual(picked, ["uid-1", "uid-3", "uid-5", "uid-1"])

    def test_page_rotation_does_not_move_the_whole_pool_cursor(self):
        self.pool.pick(realm="cn", page=(0, 2))
        self.pool.pick(realm="cn", page=(0, 2))
        # 整池轮转仍从第一个可用号开始（页轮转用自己的一份状态）
        self.assertEqual(self.pool.pick(realm="cn").uid, "uid-1")

    def test_page_pick_does_not_bind_affinity(self):
        account = self.pool.pick(realm="cn", session_key="s", page=(0, 2))
        self.assertIsNotNone(account)
        self.assertIsNone(self.pool.affinity.get("s"))

    def test_page_pick_falls_back_to_whole_pool_when_page_is_unusable(self):
        for uid in ("uid-1", "uid-3", "uid-5"):
            self.pool.get(uid).note_error("boom", cooldown=60)
        account = self.pool.pick_for_session(realm="cn", session_key="s",
                                             page=(0, 2))
        self.assertIsNotNone(account)
        self.assertIn(account.uid, ("uid-2", "uid-4", "uid-6"))


# --------------------------------------------------------- 区域连坐暂停
class RestrictionTests(unittest.TestCase):
    def setUp(self):
        trae_restriction.reset()
        self.addCleanup(trae_restriction.reset)
        self.now = 1_700_000_000.0

    def note(self, uid, realm="cn", at=None):
        return trae_restriction.note_account_restriction(
            realm, uid, now=self.now if at is None else at)

    def test_wave_triggers_region_hold(self):
        self.assertEqual(self.note("a"), 0.0)
        self.assertEqual(self.note("b"), 0.0)
        until = self.note("c")
        self.assertGreater(until, self.now)
        self.assertAlmostEqual(
            trae_restriction.hold_remaining("cn", now=self.now), 1800.0, places=3)

    def test_same_account_repeated_does_not_trigger(self):
        for _ in range(5):
            self.assertEqual(self.note("a"), 0.0)
        self.assertEqual(trae_restriction.hold_remaining("cn", now=self.now), 0.0)

    def test_events_outside_the_window_do_not_count(self):
        self.note("a", at=self.now - 5000)
        self.note("b", at=self.now - 4000)
        self.assertEqual(self.note("c"), 0.0)
        self.assertEqual(trae_restriction.hold_remaining("cn", now=self.now), 0.0)

    def test_realms_are_independent(self):
        self.note("a", realm="cn")
        self.note("b", realm="cn")
        self.note("c", realm="cn")
        self.assertGreater(trae_restriction.hold_remaining("cn", now=self.now), 0)
        self.assertEqual(trae_restriction.hold_remaining("intl", now=self.now), 0.0)
        # intl 自己凑够一波才会停
        self.note("x", realm="intl")
        self.note("y", realm="intl")
        self.assertGreater(self.note("z", realm="intl"), 0.0)
        self.assertGreater(trae_restriction.hold_remaining("intl", now=self.now), 0)

    def test_hold_expires_on_its_own(self):
        self.note("a")
        self.note("b")
        self.note("c")
        later = self.now + 1801
        self.assertEqual(trae_restriction.hold_remaining("cn", now=later), 0.0)
        self.assertEqual(trae_restriction.all_holds(now=later), {})

    def test_clamp_shortens_and_zero_clears(self):
        self.note("a")
        self.note("b")
        self.note("c")
        self.assertAlmostEqual(
            trae_restriction.clamp_holds(60, now=self.now)["cn"], 60.0, places=3)
        self.assertEqual(trae_restriction.clamp_holds(0, now=self.now), {})
        self.assertEqual(trae_restriction.hold_remaining("cn", now=self.now), 0.0)

    def test_clear_forgets_the_wave_too(self):
        self.note("a")
        self.note("b")
        self.note("c")
        self.assertEqual(trae_restriction.clear("cn"), ["cn"])
        # 波次一起清掉：下一个 403 不会立刻把暂停重新挂上
        self.assertEqual(self.note("d"), 0.0)

    def test_hold_survives_a_restart(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        trae_restriction.configure(state_dir=state_dir)
        self.note("a")
        self.note("b")
        self.note("c")
        self.assertTrue(os.path.isfile(
            os.path.join(state_dir, trae_restriction.STATE_FILE_NAME)))
        # 模拟重启：清内存、按同一目录重新接线
        trae_restriction.reset()
        trae_restriction.configure(state_dir=state_dir)
        self.assertAlmostEqual(
            trae_restriction.hold_remaining("cn", now=self.now), 1800.0, places=3)

    def test_settings_and_env_override_the_defaults(self):
        with mock.patch.dict(os.environ,
                             {trae_restriction.ENV_HOLD: "120",
                              trae_restriction.ENV_WAVE_MIN: "2"}):
            trae_restriction.configure(
                settings_resolver=lambda: {"hold_seconds": 999,
                                           "wave_window": 300})
            self.note("a")
            until = self.note("b")
            self.assertAlmostEqual(until - self.now, 120.0, places=3)
            hold, wave_min, wave_window = trae_restriction.limits()
            self.assertEqual((hold, wave_min, wave_window), (120.0, 2, 300.0))

    def test_snapshot_reports_holds_and_live_events(self):
        self.note("a")
        self.note("b")
        self.note("c")
        snap = trae_restriction.snapshot(now=self.now)
        self.assertEqual(snap["holds"].get("cn"), 1800)
        self.assertEqual(snap["events"].get("cn"), ["a", "b", "c"])
        self.assertEqual(snap["wave_min"], 3)
        # 面板倒计时用截止时刻（epoch）自己数秒，不等后端刷新
        self.assertAlmostEqual(snap["hold_until"]["cn"], self.now + 1800, places=2)


# ------------------------------------------------- 派发侧接线（trae_proxy）
class RestrictionSettingsTests(unittest.TestCase):
    """settings.json 的 restriction 段读写往返（面板保存走的就是这条路）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_roundtrip_and_clear(self):
        self.assertEqual(trae_settings.restriction_settings(self.dir), {})
        stored = trae_settings.set_restriction_settings(
            self.dir, {"hold_seconds": "120", "wave_min": 2, "junk": 9})
        self.assertEqual(stored, {"hold_seconds": 120.0, "wave_min": 2})
        self.assertEqual(trae_settings.restriction_settings(self.dir), stored)
        self.assertEqual(trae_settings.set_restriction_settings(self.dir, None), {})
        self.assertEqual(trae_settings.restriction_settings(self.dir), {})

    def test_non_finite_thresholds_are_dropped(self):
        # nan/inf 过去会在 int(wave_min) 处抛 ValueError，面板保存直接 500
        stored = trae_settings.set_restriction_settings(
            self.dir, {"hold_seconds": "nan", "wave_min": "inf",
                       "wave_window": "1e999"})
        self.assertEqual(stored, {})
        self.assertEqual(trae_settings.restriction_settings(self.dir), {})


class DispatchWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import trae_proxy
        cls.P = trae_proxy

    def setUp(self):
        trae_restriction.reset()
        self.addCleanup(trae_restriction.reset)
        self.now = 1_700_000_000.0
        # 注入假时钟：暂停的判定与剩余时间都按这个时间走（不真等待、不依赖系统钟）
        trae_restriction.configure(clock=lambda: self.now)

    def trigger_wave(self, realm="cn"):
        for uid in ("a", "b", "c"):
            trae_restriction.note_account_restriction(realm, uid, now=self.now)

    def test_is_account_restriction_policy(self):
        P = self.P
        self.assertTrue(P._is_account_restriction(403, "forbidden"))
        self.assertTrue(P._is_account_restriction(401, "risk_control hit"))
        self.assertFalse(P._is_account_restriction(403, "TOKEN_EXPIRE", dead=True))
        self.assertFalse(P._is_account_restriction(
            429, "4008 exceeded the quota"))
        self.assertFalse(P._is_account_restriction(
            403, "DataInspectionFailed: inappropriate content"))
        # 上游排队（403 + 10605）是「忙」，不是账号被限制：不能算进连坐波次
        self.assertFalse(P._is_account_restriction(
            403, '{"code":"10605","isQueued":true,"retryAfterSeconds":30}'))
        # 客户端参数/权限/鉴权类：这次请求本身有问题，换号也一样被拒 —— 若算进
        # 波次，一个没给这些号开通的模型就能把整区停 30 分钟（且真因被 429 盖住）
        self.assertFalse(P._is_account_restriction(
            403, '{"error":{"type":"permission_error","message":"no access"}}'))
        self.assertFalse(P._is_account_restriction(403, "trae 1001 auth: expired"))
        # 裸正文（HTTPError.read 的原文）里的 1001 同样是鉴权，不是风控：
        # 上游用 403 投递它，但没有 "trae 1001" 那层信封包装
        self.assertFalse(P._is_account_restriction(
            403, '{"code":1001,"message":"auth failed"}'))
        self.assertFalse(P._is_account_restriction(
            403, '{"code":"1001","msg":"unauthorized"}'))
        self.assertFalse(P._is_account_restriction(500, "boom"))

    def test_hold_guard_raises_region_hold(self):
        self.trigger_wave("cn")
        with self.assertRaises(self.P.RegionHold) as ctx:
            self.P.realm_hold_guard("cn")
        # 仍然是一个 RateLimited（老的 except 分支照旧能接住）
        self.assertIsInstance(ctx.exception, self.P.RateLimited)

    def test_hold_guard_raises_rate_limited_with_retry_after(self):
        self.trigger_wave("cn")
        with self.assertRaises(self.P.RateLimited) as ctx:
            self.P.realm_hold_guard("cn")
        self.assertGreater(ctx.exception.wait, 1700)
        self.assertIn("连坐", ctx.exception.detail)
        # 没有暂停的区域照旧放行
        self.assertEqual(self.P.realm_hold_guard("intl"), 0.0)

    def test_open_upstream_refuses_before_touching_upstream(self):
        self.trigger_wave("cn")
        with self.assertRaises(self.P.RateLimited):
            self.P.open_upstream({"model": "deepseek-v4.1-flash"},
                                 target_realm="cn")

    def test_estimate_prompt_tokens(self):
        P = self.P
        small = {"messages": [{"role": "user", "content": "hi"}]}
        big = {"messages": [{"role": "user", "content": "x" * 400000}]}
        self.assertLess(P.estimate_prompt_tokens(small), 1000)
        self.assertGreaterEqual(P.estimate_prompt_tokens(big), 100000)
        self.assertEqual(P.estimate_prompt_tokens(None), 0)

    def test_affinity_page_for_thresholds(self):
        P = self.P
        pool = make_pool(["uid-%d" % i for i in range(1, 7)])
        self.addCleanup(shutil.rmtree, pool.dir, ignore_errors=True)
        old_pool = P.POOL
        P.POOL = pool
        try:
            with mock.patch.object(P, "AFFINITY_PAGE_TOKENS", 1000), \
                    mock.patch.object(P, "AFFINITY_PAGE_SIZE", 2):
                self.assertIsNone(P.affinity_page_for("", 5000, "cn"))
                self.assertIsNone(P.affinity_page_for("s", 999, "cn"))
                page = P.affinity_page_for("s", 5000, "cn")
                self.assertEqual(page, (page[0], 2))
                self.assertIn(page[0], (0, 1))
                # 同一段会话永远是同一页
                self.assertEqual(P.affinity_page_for("s", 5000, "cn"), page)
            with mock.patch.object(P, "AFFINITY_PAGE_TOKENS", 1000), \
                    mock.patch.object(P, "AFFINITY_PAGE_SIZE", 1):
                self.assertIsNone(P.affinity_page_for("s", 5000, "cn"))
            with mock.patch.object(P, "AFFINITY_PAGE_TOKENS", 1000), \
                    mock.patch.object(P, "AFFINITY_PAGE_SIZE", 64):
                self.assertIsNone(P.affinity_page_for("s", 5000, "cn"))
        finally:
            P.POOL = old_pool

    def test_pick_account_for_turn_tolerates_a_legacy_pool(self):
        """号池桩不认识 page 关键字时退回老签名，不把请求打崩。"""
        P = self.P
        legacy = FakePool([make_account("uid-1")])
        legacy.pick_for_session = lambda realm=None, session_key=None, \
            exclude=None, model=None: legacy.accounts[0]
        old_pool = P.POOL
        P.POOL = legacy
        try:
            account = P._pick_account_for_turn("cn", "s", set(), "m", (0, 2))
            self.assertEqual(account.uid, "uid-1")
        finally:
            P.POOL = old_pool

    # ---- 设置页两个动作（保存阈值 / 立即解除）的落点 ----
    def _accounts_dir(self):
        """临时账号目录 + 与生产一致的设置读取器接线（阈值从 settings.json 读）。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        trae_restriction.configure(
            settings_resolver=lambda: trae_settings.restriction_settings(tmp))
        return tmp

    def test_apply_restriction_payload_saves_thresholds(self):
        P = self.P
        tmp = self._accounts_dir()
        with mock.patch.object(P, "ACCOUNTS_DIR", tmp):
            reply, err = P.apply_restriction_payload(
                {"restriction": {"hold_seconds": 120, "wave_min": 2,
                                 "wave_window": 300}})
        self.assertEqual(err, "")
        self.assertEqual(reply["restriction"]["hold_seconds"], 120.0)
        self.assertEqual(reply["restriction"]["wave_min"], 2)
        self.assertEqual(trae_settings.restriction_settings(tmp),
                         {"hold_seconds": 120.0, "wave_min": 2,
                          "wave_window": 300.0})

    def test_apply_restriction_payload_rejects_non_object(self):
        P = self.P
        tmp = self._accounts_dir()
        with mock.patch.object(P, "ACCOUNTS_DIR", tmp):
            reply, err = P.apply_restriction_payload({"restriction": "nope"})
        self.assertEqual(reply, {})
        self.assertIn("restriction must be an object", err)

    def test_apply_restriction_payload_clear_releases_the_hold(self):
        P = self.P
        tmp = self._accounts_dir()
        self.trigger_wave("cn")
        self.assertGreater(trae_restriction.hold_remaining("cn"), 0)
        with mock.patch.object(P, "ACCOUNTS_DIR", tmp):
            reply, err = P.apply_restriction_payload({"restriction_clear": True})
        self.assertEqual(err, "")
        self.assertEqual(reply["restriction_cleared"], ["cn"])
        self.assertEqual(trae_restriction.hold_remaining("cn"), 0.0)
        self.assertEqual(reply["restriction"]["holds"], {})

    def test_apply_restriction_payload_clamps_existing_hold(self):
        """面板把暂停时长调小，现有暂停立刻跟着缩短（不用等上一轮走完）。"""
        P = self.P
        tmp = self._accounts_dir()
        self.trigger_wave("cn")
        self.assertAlmostEqual(trae_restriction.hold_remaining("cn"), 1800.0,
                               places=1)
        with mock.patch.object(P, "ACCOUNTS_DIR", tmp):
            reply, _err = P.apply_restriction_payload(
                {"restriction": {"hold_seconds": 60}})
        self.assertAlmostEqual(trae_restriction.hold_remaining("cn"), 60.0,
                               places=1)
        self.assertEqual(reply["restriction"]["holds"], {"cn": 60})

    # ---- settings.json 读不出来时不许覆盖（会丢面板密码与 API Key）----
    def _broken_settings_dir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"api_keys": [{"id": "k1", "key": "sk-keep-me"')
        return tmp, path

    def test_set_restriction_settings_refuses_a_broken_settings_file(self):
        tmp, path = self._broken_settings_dir()
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        with self.assertRaises(OSError):
            trae_settings.set_restriction_settings(tmp, {"hold_seconds": 60})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)   # 一个字节都没动

    def test_broken_settings_file_gives_400_and_keeps_the_hold(self):
        P = self.P
        tmp, _path = self._broken_settings_dir()
        self.trigger_wave("cn")
        with mock.patch.object(P, "ACCOUNTS_DIR", tmp):
            reply, err = P.apply_restriction_payload(
                {"restriction": {"hold_seconds": 60}, "restriction_clear": True})
        self.assertEqual(reply, {})
        self.assertIn("settings.json", err)
        # 落盘失败时不该已经「解除了一半」
        self.assertGreater(trae_restriction.hold_remaining("cn"), 0)

    # ---- 真 403 走到派发路径时确实记波次，并且当场停手 ----
    def _refuse_all(self, pool, body):
        """把整池的上游调用都换成同一个 403，并记下真的被打过的号。"""
        tried = []

        def refuse(account, *_args, **_kwargs):
            tried.append(account.uid)
            raise urllib.error.HTTPError(
                "http://upstream/api/ide/v1/llm_raw_chat", 403, "Forbidden",
                {}, io.BytesIO(body))

        old_pool = self.P.POOL
        self.P.POOL = pool
        self.addCleanup(setattr, self.P, "POOL", old_pool)
        return tried, mock.patch.object(self.P, "trae_open_chat", refuse)

    def test_403_wave_stops_rotating_inside_the_same_request(self):
        """波次成立的那一刻就停手：剩下的号不再被送进上游。

        只挡「下一个请求」等于没挡 —— 触发暂停的这一轮自己就把同批剩下的号
        全试探了一遍，正是这道闸门要防的放大器。
        """
        P = self.P
        pool = make_pool(["uid-%d" % i for i in range(1, 6)])
        self.addCleanup(shutil.rmtree, pool.dir, ignore_errors=True)
        tried, patched = self._refuse_all(pool, b'{"error":"forbidden"}')
        with patched:
            with self.assertRaises(P.RegionHold) as ctx:
                P.open_upstream({"model": "deepseek-v4.1-flash"},
                                target_realm="cn")
        self.assertEqual(len(tried), 3)                 # = wave_min，不是整池 5 个
        self.assertGreater(ctx.exception.wait, 1700)    # 带 Retry-After 的 429
        self.assertEqual(trae_restriction.snapshot()["events"].get("cn"),
                         sorted(tried))
        self.assertGreater(trae_restriction.hold_remaining("cn"), 0)

    def test_403_below_the_wave_keeps_rotating(self):
        """没凑够一波时照旧换号：闸门不能把普通 403 也变成停摆。"""
        P = self.P
        pool = make_pool(["uid-%d" % i for i in range(1, 6)])
        self.addCleanup(shutil.rmtree, pool.dir, ignore_errors=True)
        tried, patched = self._refuse_all(pool, b'{"error":"forbidden"}')
        with mock.patch.dict(os.environ, {trae_restriction.ENV_WAVE_MIN: "99"}):
            with patched:
                with self.assertRaises(P.UpstreamStatus) as ctx:
                    P.open_upstream({"model": "deepseek-v4.1-flash"},
                                    target_realm="cn")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(tried), 5)
        self.assertEqual(trae_restriction.hold_remaining("cn"), 0.0)

    def test_auth_403_body_never_pauses_the_region(self):
        """凭证类 403（裸正文 1001）打完全池也不该把整区停 30 分钟。"""
        P = self.P
        pool = make_pool(["uid-%d" % i for i in range(1, 6)])
        self.addCleanup(shutil.rmtree, pool.dir, ignore_errors=True)
        tried, patched = self._refuse_all(
            pool, b'{"code":1001,"message":"auth failed"}')
        with patched:
            with self.assertRaises(P.UpstreamStatus) as ctx:
                P.open_upstream({"model": "deepseek-v4.1-flash"},
                                target_realm="cn")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(tried), 5)
        self.assertEqual(trae_restriction.hold_remaining("cn"), 0.0)
        self.assertEqual(trae_restriction.snapshot()["events"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
