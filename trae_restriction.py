# -*- coding: utf-8 -*-
"""trae_restriction.py —— 区域级「连坐」暂停（借 workbuddy2api-hub 的做法）

上游对账号的限制很少是逐个发生的：同一台设备 / 同一个出口 IP 上的账号会被
**按批**处理。所以「一个号被 403 了，换下一个号重试」这个动作本身就是放大器 ——
每一次换号试探都在把同批剩下的号一个个送进去，最后整池全灭。

本模块只做一件事：把「一批账号在短时间内接连被限制」识别成区域级风控扫荡，
然后**整区暂停派发**一段时间，期间不打上游、不换号。暂停到期自动恢复，不需要
重启网关，也不改变任何账号的可用状态（恢复后照旧按各自冷却参与轮转）。

判定口径（与 wb 一致）：
  · 同一个 realm 内、wave_window（默认 10 分钟）里被限制的**不同 uid** 个数
    达到 wave_min（默认 3）→ 判定为扫荡，挂起 hold_seconds（默认 30 分钟）；
  · 按 uid 去重：同一个账号被反复探测 / 重试 / 刷新，不会把暂停越骗越长；
  · 暂停只延长不缩短（已有更晚的截止时间时取较晚者）。

三处刻意的取舍：
  * **不持久化账号状态**：本模块只记「区域暂停」这一件事，账号级的封禁 / 冷却
    仍由 trae_accounts.Account 自己负责，两边不重叠、不互相覆盖；
  * **暂停落盘**（usage/restriction.json，原子写）：进程重启不该等于「暂停
    白设」—— 重启后立刻恢复打号正是这套机制要防的事；落盘失败只丢持久化，
    内存里的暂停仍然生效；
  * **clock 可注入**：所有判定都接受 now 参数，测试不真等待，也不 sleep。

未配置 state_dir 时纯内存运行（测试 / 嵌入场景），行为与落盘版一致。
"""
import json
import os
import threading
import time

#: 区域暂停默认时长（秒）。30 分钟：够上游把这一批的判定跑完，又不至于整天停摆。
HOLD_SECONDS_DEFAULT = 1800.0
#: 触发扫荡判定的账号个数（不同 uid）。
WAVE_MIN_DEFAULT = 3
#: 扫荡判定窗口（秒）：这段时间内的限制算同一批。
WAVE_WINDOW_DEFAULT = 600.0

#: 上限：暂停最长 24 小时（面板 / 环境变量填错时兜底）。
HOLD_SECONDS_MAX = 24 * 3600.0
WAVE_WINDOW_MAX = 6 * 3600.0
WAVE_MIN_MAX = 64

ENV_HOLD = "TRAE_RESTRICTION_HOLD_SECONDS"
ENV_WAVE_MIN = "TRAE_RESTRICTION_WAVE_MIN"
ENV_WAVE_WINDOW = "TRAE_RESTRICTION_WAVE_WINDOW"

STATE_FILE_NAME = "restriction.json"
STATE_VERSION = 1

#: 面板上的区域称呼只保留在 trae_proxy.realm_label 一处（本模块是纯状态机，
#: 不持有任何给人看的文案）。

_lock = threading.RLock()
_holds = {}          # realm -> 暂停到的 epoch
_events = {}         # realm -> {uid: 最近一次被限制的 epoch}
_state_dir = None    # None = 不落盘
_loaded = False
_clock = time.time
_settings_resolver = None


# --------------------------------------------------------------------- 配置
def configure(state_dir=None, clock=None, settings_resolver=None):
    """接线：状态目录 / 时钟 / 设置读取器（都可选，缺省保持原值）。

    state_dir 一旦设定就会尝试把已有的暂停读回来（重启不清零）；settings_resolver
    是一个无参可调用对象，返回 {"hold_seconds":..,"wave_min":..,"wave_window":..}，
    由 trae_proxy 接到 trae_settings 上，避免本模块反向依赖设置模块。
    """
    global _state_dir, _clock, _settings_resolver, _loaded
    with _lock:
        if state_dir is not None:
            _state_dir = os.path.abspath(state_dir) if str(state_dir).strip() else None
            _loaded = False
        if clock is not None:
            _clock = clock
        if settings_resolver is not None:
            _settings_resolver = settings_resolver


def reset():
    """清空全部状态（测试 / 人工处置用）。不删盘上的文件。"""
    global _loaded, _state_dir, _clock, _settings_resolver
    with _lock:
        _holds.clear()
        _events.clear()
        _loaded = False
        _state_dir = None
        _clock = time.time
        _settings_resolver = None


def _now(now=None):
    if now is not None:
        try:
            return float(now)
        except (TypeError, ValueError):
            pass
    return float(_clock())


def _env_number(name):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(float(low), min(float(high), number))


def limits():
    """生效的三项阈值：(hold_seconds, wave_min, wave_window)。

    优先级：环境变量 > settings_resolver（settings.json 的 restriction 段）>
    模块默认值。非法值逐项回落到下一层，不因为一个坏值把整套机制关掉。
    """
    raw = {}
    if _settings_resolver is not None:
        try:
            value = _settings_resolver()
            if isinstance(value, dict):
                raw = value
        except Exception:
            raw = {}
    hold = _env_number(ENV_HOLD)
    if hold is None:
        hold = raw.get("hold_seconds")
    wave_min = _env_number(ENV_WAVE_MIN)
    if wave_min is None:
        wave_min = raw.get("wave_min")
    wave_window = _env_number(ENV_WAVE_WINDOW)
    if wave_window is None:
        wave_window = raw.get("wave_window")
    return (
        _clamp(hold, 0.0, HOLD_SECONDS_MAX, HOLD_SECONDS_DEFAULT),
        int(_clamp(wave_min, 1, WAVE_MIN_MAX, WAVE_MIN_DEFAULT)),
        _clamp(wave_window, 1.0, WAVE_WINDOW_MAX, WAVE_WINDOW_DEFAULT),
    )


# --------------------------------------------------------------------- 落盘
def _state_path():
    if not _state_dir:
        return ""
    return os.path.join(_state_dir, STATE_FILE_NAME)


def _load_locked():
    """懒加载盘上的暂停。调用方需持有 _lock。读失败按「没有暂停」处理。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _state_path()
    if not path:
        return
    try:
        # utf-8-sig：手工/脚本写进来的文件常带 BOM，读失败会被当成「没有暂停」，
        # 那等于重启后立刻恢复打号 —— 这条路径宁可能读就读出来。
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    holds = data.get("holds")
    if isinstance(holds, dict):
        for realm, until in holds.items():
            try:
                _holds[str(realm)] = float(until)
            except (TypeError, ValueError):
                continue
    events = data.get("events")
    if isinstance(events, dict):
        for realm, rows in events.items():
            if not isinstance(rows, dict):
                continue
            clean = {}
            for uid, at in rows.items():
                try:
                    clean[str(uid)] = float(at)
                except (TypeError, ValueError):
                    continue
            if clean:
                _events[str(realm)] = clean


def _save_locked():
    """原子落盘。调用方需持有 _lock；失败只丢持久化，不影响内存状态。"""
    path = _state_path()
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        payload = {
            "version": STATE_VERSION,
            "kind": "restriction_state",
            "holds": dict(_holds),
            "events": {realm: dict(rows) for realm, rows in _events.items()},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------- 判定
def note_account_restriction(realm, uid="", now=None):
    """记一次「账号被上游限制」，返回该区域暂停到的 epoch（0 = 未触发暂停）。

    只应该在**确实是账号级限制**时调用（403 / risk_control 一类）；凭证过期、
    额度用尽、内容审核都不算 —— 那几种情况换号没用或与账号无关，计入波次会
    把整区误停。
    """
    realm = str(realm or "").strip().lower()
    if not realm:
        return 0.0
    moment = _now(now)
    key = str(uid or "").strip() or "-"
    hold_seconds, wave_min, wave_window = limits()
    with _lock:
        _load_locked()
        rows = {}
        for row_uid, at in (_events.get(realm) or {}).items():
            try:
                if moment - float(at) <= wave_window:
                    rows[row_uid] = float(at)
            except (TypeError, ValueError):
                continue
        rows[key] = moment
        _events[realm] = rows
        until = 0.0
        if len(rows) >= wave_min and hold_seconds > 0:
            until = max(float(_holds.get(realm) or 0.0), moment + hold_seconds)
            _holds[realm] = until
        _save_locked()
    return until


def hold_remaining(realm, now=None):
    """该区域还剩多少秒暂停（0 = 没有暂停）。"""
    realm = str(realm or "").strip().lower()
    if not realm:
        return 0.0
    moment = _now(now)
    with _lock:
        _load_locked()
        try:
            until = float(_holds.get(realm) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if until <= moment:
            if realm in _holds:
                _holds.pop(realm, None)
                _save_locked()
            return 0.0
        return until - moment


def all_holds(now=None):
    """{realm: 剩余秒数}，只含仍在暂停中的区域（整数秒，面板用）。"""
    moment = _now(now)
    out = {}
    with _lock:
        _load_locked()
        for realm in list(_holds):
            remaining = hold_remaining(realm, now=moment)
            if remaining > 0:
                out[realm] = int(round(remaining))
    return out


def clamp_holds(max_seconds, now=None):
    """把现有暂停截断到不超过 max_seconds（面板调小时实时生效）。

    max_seconds <= 0 表示「立刻解除」。返回截断后的 {realm: 剩余秒数}。
    """
    moment = _now(now)
    try:
        limit = max(0.0, float(max_seconds))
    except (TypeError, ValueError):
        limit = 0.0
    with _lock:
        _load_locked()
        if limit <= 0:
            _holds.clear()
            _save_locked()
            return {}
        for realm, until in list(_holds.items()):
            if float(until) > moment + limit:
                _holds[realm] = moment + limit
        _save_locked()
    return all_holds(now=moment)


def clear(realm=None, now=None):
    """人工解除暂停：realm 为空 = 解除全部。返回被解除的区域列表。"""
    moment = _now(now)
    with _lock:
        _load_locked()
        if realm is None:
            removed = sorted(_holds)
            _holds.clear()
            _events.clear()
        else:
            key = str(realm or "").strip().lower()
            removed = [key] if key in _holds else []
            _holds.pop(key, None)
            # 人工处置后把波次一起清掉：否则下一个 403 会立刻把暂停重新挂上，
            # 操作者会以为「解除没生效」。
            _events.pop(key, None)
        _save_locked()
    return removed


def snapshot(now=None):
    """只读快照（面板 / 状态接口）：暂停、剩余秒数与阈值。

    波次里的账号只露 uid 前 8 位：面板与日志的其它地方都是这个口径，快照不该
    成为唯一回显完整 uid 的出口。
    """
    moment = _now(now)
    hold_seconds, wave_min, wave_window = limits()
    with _lock:
        _load_locked()
        holds = all_holds(now=moment)
        hold_until = {}
        for realm, remaining in holds.items():
            hold_until[realm] = round(moment + remaining, 3)
        events = {}
        for realm, rows in _events.items():
            live = [str(uid)[:8] for uid, at in rows.items()
                    if moment - float(at or 0) <= wave_window]
            if live:
                events[realm] = sorted(live)
    return {
        "holds": holds,
        # 面板倒计时要的是「截止时刻」而不是「剩余秒数」：拿 epoch 自己数秒，
        # 不必每秒轮询后端（也避免面板开着的这段时间里显示的数字停住）。
        "hold_until": hold_until,
        "events": events,
        "hold_seconds": hold_seconds,
        "wave_min": wave_min,
        "wave_window": wave_window,
        "state_file": _state_path(),
    }
