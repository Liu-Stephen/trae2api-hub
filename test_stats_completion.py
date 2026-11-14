#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_stats_completion.py —— 统计页收尾回归（热力图 / 本月 / 24h 桶 / 洞察端点）

覆盖此前「半成品」的几处：
  · /usage/stats 的 heatmap.max（否则前端所有非零格子同色）
  · 「本月」= 自然月 1 日，而不是最近 30 天
  · 近 24 小时按滚动小时桶聚合，不把昨天同一钟点并进今天的点
  · /usage/remaining、/accounts/credits/grants、/accounts/credits/usage、
    /pricing 不再是硬编码空数据，且字段与 dashboard.html 对齐
  · 额度包 composition 能从 entitlement 拆出来

跑法：python test_stats_completion.py    （全 PASS 退出码 0）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import trae_accounts as A          # noqa: E402
import trae_cache                  # noqa: E402
import trae_proxy as P             # noqa: E402
import trae_stats as S             # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, extra=""):
    CHECKS[0] += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  << " + str(extra)))
    if not cond:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="trae-stats-completion-")
LOG = os.path.join(TMP, "usage.jsonl")


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts


def make_account(uid="1001", realm="cn", name="acct-1"):
    now = time.time()
    packs = [
        {
            "display_desc": "每月登录奖励",
            "group_type": 3,
            "quota": {"credits_limit": 500},
            "usage": {"credits_amount": 43.61},
            "expire_time": int(now + 86400 * 20),
            "entitlement_base_info": {"entitlement_id": "monthly_bonus_x"},
        },
        {
            "display_desc": "签到奖励",
            "group_type": 1,
            "quota": {"credits_limit": 100},
            "usage": {},
            "expire_time": int(now + 86400 * 30),
            "entitlement_base_info": {"entitlement_id": "checkin_x"},
        },
    ]
    entitlement = {
        "ent_usage": {
            "usage_summary": {"total_amount": 600, "consumed_amount": 43.61},
            "user_entitlement_pack_list": packs,
        }
    }
    account = A.Account({
        "uid": uid,
        "realm": realm,
        "entitlement": entitlement,
        "extra": {"checkin": {"credits": 100, "checked_in": True,
                              "at": "2026-11-13"}},
    })
    account.display_name = name
    return account


def write_rows(rows):
    with open(LOG, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def configure(accounts):
    P.USAGE_DIR = TMP
    P.USAGE_LOG = LOG
    P.POOL = FakePool(accounts)
    P._AUX_CACHE = trae_cache.JsonResultCache()
    S.configure(usage_dir=TMP, log_path=LOG, pool=P.POOL,
                log=lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1) 「本月」是自然月，不是最近 30 天
# ---------------------------------------------------------------------------
with mock.patch.object(S.time, "time", return_value=time.mktime(
        (2026, 11, 13, 12, 30, 0, 0, 0, -1))):
    start, end = S.range_window("month")
check("1 本月窗口从 11-01 00:00 开始",
      time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
      == "2026-11-01 00:00", time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(start)))
check("1 本月窗口上界为空（到当前）", end is None, end)

# 30d 仍保持 30 天语义
with mock.patch.object(S.time, "time", return_value=time.mktime(
        (2026, 11, 13, 12, 30, 0, 0, 0, -1))):
    start30, _ = S.range_window("30d")
check("1 30d 仍是最近 30 天",
      time.strftime("%Y-%m-%d", time.localtime(start30)) == "2026-10-15",
      time.strftime("%Y-%m-%d", time.localtime(start30)))

# ---------------------------------------------------------------------------
# 2) heatmap.max 存在且等于当天峰值
# ---------------------------------------------------------------------------
fixed = time.mktime((2026, 11, 13, 12, 30, 0, 0, 0, -1))
write_rows([
    {"at": fixed - 10, "model": "m1", "prompt_tokens": 1000,
     "cached_tokens": 100, "total_tokens": 1200, "account": "1001",
     "realm": "cn"},
    {"at": fixed - 86400 * 2, "model": "m1", "prompt_tokens": 100,
     "cached_tokens": 0, "total_tokens": 100, "account": "1001",
     "realm": "cn"},
])
configure([make_account()])
with mock.patch.object(S.time, "time", return_value=fixed):
    st = S.compute_usage_stats(range_val="all")
heat = st.get("heatmap") or {}
check("2 heatmap.max 存在", "max" in heat, heat.keys())
check("2 heatmap.max 等于峰值 1200", heat.get("max") == 1200,
      heat.get("max"))
check("2 heatmap.max 与 days 最大值一致",
      heat.get("max") == max((d.get("tokens") or 0) for d in heat["days"]),
      (heat.get("max"), heat["days"][-2:]))

# ---------------------------------------------------------------------------
# 3) 近 24 小时按滚动小时桶：昨天同一钟点不再并进今天
# ---------------------------------------------------------------------------
write_rows([
    # 23h50m 前（昨天 12:40）与 10m 前（今天 12:20）同属时钟小时 12，
    # 旧实现会把它们加在同一个 12:00 点上。
    {"at": fixed - 23 * 3600 - 50 * 60, "model": "m1",
     "total_tokens": 20, "account": "1001", "realm": "cn"},
    {"at": fixed - 10 * 60, "model": "m1",
     "total_tokens": 10, "account": "1001", "realm": "cn"},
])
with mock.patch.object(S.time, "time", return_value=fixed):
    st = S.compute_usage_stats(range_val="all")
points = st["hourly"]["points"]
check("3 小时点仍是 24 个", len(points) == 24, len(points))
check("3 最新桶只有今天的 10 tokens", points[-1]["tokens"] == 10,
      points[-1])
check("3 最旧桶只有昨天的 20 tokens", points[0]["tokens"] == 20, points[0])
check("3 没有任何桶被合并成 30",
      all(p["tokens"] != 30 for p in points),
      [(p["label"], p["tokens"]) for p in points if p["tokens"]])

# ---------------------------------------------------------------------------
# 4) 额度包 composition / 积分包端点
# ---------------------------------------------------------------------------
write_rows([
    {"at": fixed - 86400 * 2, "model": "dfmodel", "total_tokens": 300,
     "credit": 5, "account": "1001", "realm": "cn"},
    {"at": fixed - 3700, "model": "unknown-x", "total_tokens": 200,
     "credit": 3.5, "account": "1001", "realm": "cn"},
    {"at": fixed - 60, "model": "dfmodel", "total_tokens": 100,
     "credit": 2.5, "account": "1001", "realm": "cn"},
])
acct = make_account()
configure([acct])

view = P.account_credit_view(acct)
check("4 composition 拆出 2 个额度包", len(view.get("composition") or []) == 2,
      view.get("composition"))
check("4 composition 含每月奖励 plan",
      any(c.get("kind") == "plan" and c.get("size") == 500
          for c in view["composition"]), view["composition"])
check("4 composition 含签到 pack",
      any(c.get("kind") == "pack" and c.get("size") == 100
          for c in view["composition"]), view["composition"])

grants = P.credits_grants(ttl=0)
check("4 grants 返回 2 行", grants.get("count") == 2, grants)
check("4 grants summary 面额 600", grants["summary"]["size"] == 600,
      grants["summary"])
check("4 grants summary 剩余 556.39", grants["summary"]["remain"] == 556.39,
      grants["summary"])

summary = P.credits_summary()
row = (summary.get("accounts") or [{}])[0]
check("4 credits_summary 带 composition", len(row.get("composition") or []) == 2,
      row.get("composition"))
check("4 credits_summary 带 size", row.get("credits_size") == 600,
      row.get("credits_size"))

# 面板「更新于」：public() 必须给出可直接展示的 ISO 串，而不是让前端猜字段。
acct.usage_updated_at = int(fixed)
_public = acct.public()
check("4 creditsUpdatedIso 是 19 位 ISO 串",
      isinstance(_public.get("creditsUpdatedIso"), str)
      and len(_public["creditsUpdatedIso"]) == 19,
      _public.get("creditsUpdatedIso"))

# ---------------------------------------------------------------------------
# 5) /usage/remaining：24h 消耗外推
# ---------------------------------------------------------------------------
# 时间窗口必须跟着夹具的 fixed 走，否则测试跑过 24h 后会自然过期。
with mock.patch.object(P.time, "time", return_value=fixed):
    remaining = P.usage_remaining(ttl=0)
r = (remaining.get("rows") or [{}])[0]
check("5 remaining 有 rows", bool(remaining.get("rows")), remaining)
check("5 24h 积分消耗 = 6.0", r.get("used_24h_credit") == 6.0, r)
check("5 24h tokens = 300", r.get("used_24h_tokens") == 300, r)
check("5 剩余 = 556.39", r.get("remain") == 556.39, r)
check("5 days_left = 556.39 / 6.0", r.get("days_left") == 92.7, r)
check("5 顶层 daily_rate = 6.0", remaining.get("daily_rate") == 6.0,
      remaining)

# ---------------------------------------------------------------------------
# 6) /accounts/credits/usage：逐日积分消耗
# ---------------------------------------------------------------------------
usage = P.credits_usage_daily(ttl=0, days=5)
check("6 逐日总消耗 = 11.0", usage.get("total") == 11.0, usage)
check("6 单日峰值 = 6.0", usage.get("peak") == 6.0, usage)
check("6 峰值日期 = 2026-11-13", usage.get("peak_date") == "2026-11-13",
      usage.get("peak_date"))
check("6 逐日序列连续 5 天", len(usage.get("daily") or []) == 5,
      usage.get("daily"))
check("6 accounts 行带 total/peak/ok",
      bool(usage.get("accounts"))
      and usage["accounts"][0].get("total") == 11.0
      and usage["accounts"][0].get("ok") is True,
      usage.get("accounts"))
check("6 source=usage_log", usage.get("source") == "usage_log",
      usage.get("source"))

# ---------------------------------------------------------------------------
# 7) /pricing：table + unpriced 与前端字段对齐
# ---------------------------------------------------------------------------
pricing = P.pricing_view(ttl=0)
check("7 pricing 带 table", bool(pricing.get("table")), pricing.keys())
check("7 pricing 带 unpriced", "unpriced" in pricing, pricing.keys())
check("7 未知模型进入 unpriced", "unknown-x" in pricing["unpriced"],
      pricing["unpriced"])
check("7 已定价模型不进 unpriced", "dfmodel" not in pricing["unpriced"],
      pricing["unpriced"])

# 7b) 回归：compute_row 曾经缺失 -> 所有行 cost_usd=0 / priced=false
cost = P.trae_pricing.compute_row({
    "model": "deepseek-v4.1-flash", "prompt_tokens": 1000000,
    "completion_tokens": 1000, "cached_tokens": 0,
})
check("7b 已定价模型 compute_row 返回正数",
      bool(cost.get("priced")) and cost.get("usd", 0) > 0, cost)
check("7b 生成价表命中 or_id",
      cost.get("or_id") == "deepseek/deepseek-v4.1-flash", cost)
unknown_cost = P.trae_pricing.compute_row({
    "model": "unknown-x", "prompt_tokens": 1000000,
    "completion_tokens": 1000, "cached_tokens": 0,
})
check("7b 未知模型仍不计价", unknown_cost.get("priced") is False,
      unknown_cost)

# ---------------------------------------------------------------------------
# 8) /usage/analytics：未知缓存命中率 / 推理占比不能伪造成 0%
# ---------------------------------------------------------------------------
write_rows([
    {"at": fixed - 10, "model": "m1", "prompt_tokens": 0,
     "cached_tokens": 0, "total_tokens": 0, "account": "1001",
     "realm": "cn"},
])
configure([make_account()])
analytics = P.compute_usage_analytics(range="all")
stat = analytics["summary"]["all_time"]
check("8 无输入时 cache_hit_pct=None", stat.get("cache_hit_pct") is None,
      stat.get("cache_hit_pct"))
check("8 无输出时 reasoning_ratio=None", stat.get("reasoning_ratio") is None,
      stat.get("reasoning_ratio"))

# ---------------------------------------------------------------------------
# 9) 真实启动：--usage-dir 必须让 /usage/stats 读同一份日志
#
# 回归的是「模块名 __main__ + 懒 import trae_proxy」把 stats.configure 覆盖回
# 默认目录的 bug；单测里直接调函数看不出来，必须走一次真实 HTTP。
# ---------------------------------------------------------------------------
def free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def check_usage_dir_wiring():
    root = tempfile.mkdtemp(prefix="trae-stats-wiring-")
    acc_dir = os.path.join(root, "accounts")
    usage_dir = os.path.join(root, "usage")
    os.makedirs(acc_dir)
    os.makedirs(usage_dir)
    now = time.time()
    with open(os.path.join(acc_dir, "1001.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "uid": "1001", "realm": "cn", "token": "x" * 32,
            "refresh_token": "y" * 32, "expired_at": int(now + 86400 * 30),
            "host": "https://api.trae.com.cn", "enabled": True, "weight": 1.0,
        }, fh)
    with open(os.path.join(usage_dir, "usage.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": now - 60, "model": "zzz-wiring", "total_tokens": 100,
            "credit": 2.5, "account": "1001", "realm": "cn",
        }) + "\n")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "trae_proxy.py"),
         "--host", "127.0.0.1", "--port", str(port),
         "--accounts-dir", acc_dir, "--usage-dir", usage_dir,
         "--panel-password", "test"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port

    def request(path, data=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Panel-Token"] = token
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            base + path, data=body, headers=headers,
            method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/ping", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            check("9 临时网关启动", False, "server did not start")
            return
        login = request("/panel/login", {"password": "test"})
        token = login.get("token") or ""
        stats = request("/usage/stats?range=all", token=token)
        check("9 --usage-dir 的统计读到同一份日志",
              stats.get("kpi", {}).get("tokens") == 100,
              stats.get("kpi"))
        check("9 统计模型来自临时日志",
              any(m.get("key") == "zzz-wiring"
                  for m in (stats.get("models") or [])),
              stats.get("models"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(root, ignore_errors=True)


check_usage_dir_wiring()


def main():
    print("")
    if FAILS:
        print("FAILED %d / %d" % (len(FAILS), CHECKS[0]))
        for name in FAILS:
            print("  - " + name)
        return 1
    print("ALL PASS (%d checks)" % CHECKS[0])
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
