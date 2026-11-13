"""签到 / 领取动作流水（跟进 wb 的「活动结构 + 历史」）。

为什么单独记：账号 JSON 里只有 lastCheckin（当天日期）和 campaignCodes（券码），
看不出「哪一天、哪个号、做了什么、成了没有」。这里追加一行一个事件到
usage/activity.jsonl，面板「积分获取历史」按时间倒序展示，只读不参与判定。
"""
import json
import os
import time

MAX_BYTES = 5 * 1024 * 1024
KEEP_DAYS = 30


def _path(usage_dir):
    return os.path.join(usage_dir or "", "activity.jsonl")


def record(usage_dir, kind, ok, uid="", nickname="", realm="", detail="",
           amount=0, state="", trigger=""):
    """追加一条事件；任何异常都吞掉（流水不能影响主流程）。

    state   成功 / 已签到 / 跳过 / 失败等更细的结论；旧行没有该字段时，
            前端按 ok 回退。
    trigger manual / startup / hour / schedule，面板据此区分手动与自动巡检。
    """
    try:
        path = _path(usage_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > MAX_BYTES:
                os.replace(path, path + ".1")
        except Exception:
            pass
        row = {"ts": time.time(), "kind": str(kind or ""),
               "ok": bool(ok), "uid": (uid or "")[:8],
               "nickname": nickname or "", "realm": realm or "",
               "detail": str(detail or "")[:200],
               "amount": amount or 0,
               "state": str(state or ""),
               "trigger": str(trigger or "")}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


AUTO_TRIGGERS = ("startup", "hour", "schedule")


def visible_rows(rows):
    """面板视图：隐藏启动 / 定时巡检的「已签到」状态检查。

    这些行只是「查了一下状态：今天已经签过」，不是一次领取动作；日志
    （activity.jsonl）仍然完整保留，需要审计时传 include_checks=1 即可。
    """
    return [row for row in (rows or [])
            if not (str(row.get("state") or "") == "already"
                    and str(row.get("trigger") or "") in AUTO_TRIGGERS)]


def summarize(rows):
    """把流水行汇总成看板卡片口径（跳过不算失败）。"""
    out = {"total": len(rows or []), "ok": 0, "failed": 0,
           "skipped": 0, "earned_credits": 0.0}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        if state == "skipped":
            out["skipped"] += 1
        elif row.get("ok"):
            out["ok"] += 1
        else:
            out["failed"] += 1
        try:
            out["earned_credits"] += float(row.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    out["earned_credits"] = round(out["earned_credits"], 2)
    return out


def recent(usage_dir, limit=200, days=KEEP_DAYS):
    """读最近事件（按时间倒序）；过期与坏行直接跳过。"""
    path = _path(usage_dir)
    cutoff = time.time() - days * 86400
    out = []
    seq = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if float(row.get("ts") or 0) < cutoff:
                    continue
                seq += 1
                row["_seq"] = seq
                out.append(row)
    except FileNotFoundError:
        return []
    except Exception:
        return out
    # 同一毫秒写入的事件（Windows 计时精度约 15ms）靠写入顺序定先后：
    # 后写的更新——否则「成功/失败」两条会因时间戳相同而顺序不定。
    out.sort(key=lambda r: (r.get("ts") or 0, r.get("_seq") or 0),
             reverse=True)
    for row in out:                 # _seq 只用于排序，不外泄到 API
        row.pop("_seq", None)
    try:
        limit = max(1, min(1000, int(limit)))
    except Exception:
        limit = 200
    return out[:limit]