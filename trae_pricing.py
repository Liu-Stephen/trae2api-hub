"""费用估算（参考 OpenRouter 公布价，issue: 跟进 wb 的「等价 token 花费」）。

口径说明（与官方积分不是一回事）：
  · 输入按「缓存命中 / 未命中」两档单价拆分，输出单独单价；
  · 单价单位为 USD / 1M tokens；
  · 只用于「这些 token 放在 OpenRouter 上大概值多少钱」的参考估算，**不能用于结算**。

价格来源优先级：
  1. usage/pricing-overrides.json（用户手填，键为模型 key 或显示名）
  2. pricing/trae_pricing.json（_refresh_pricing.py 生成的 OpenRouter 表，
     支持长上下文 bands / 精确 or_id）
  3. 本文件 DEFAULT_PRICES / ALIASES（离线兜底）
  4. 未知模型 -> 不计价（priced=False，面板显示 —，绝不猜数字）
"""
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PRICING_PATH = os.path.join(HERE, "pricing", "trae_pricing.json")
_DEFAULT_USD_CNY = 7.1

# USD / 1M tokens: (输入未命中, 输入缓存命中, 输出)
DEFAULT_PRICES = {
    "dfmodel": (0.28, 0.028, 0.42),        # DeepSeek-V4-Flash
    "dmodel": (0.55, 0.14, 2.19),          # DeepSeek-V4-Pro
    "gmodel": (0.60, 0.15, 2.20),          # GLM-5.3
    "gfmodel": (0.06, 0.015, 0.40),        # GLM-5.3-Flash
    "kmodel": (0.60, 0.15, 2.50),          # Kimi-K2.8
    "kmodel_latest": (0.60, 0.15, 2.50),   # Kimi-K3（同族，先按此档）
    "qmodel": (0.40, 0.10, 1.20),          # Qwen3.7-Plus
    "qmodel_latest": (1.20, 0.30, 6.00),   # Qwen3.7-Max
    "qmodel_38max": (1.20, 0.30, 6.00),    # Qwen3.8-Max
    "qfmodel": (0.05, 0.012, 0.20),        # Qwen3.8-Flash
    "mmodel": (0.30, 0.075, 1.20),         # MiniMax-M3
    "cmodel": (3.00, 0.75, 15.00),         # Cantus
}

# 目录里的别名键 -> 计价键（agent 页/模型卡可能用这些名字）
ALIASES = {
    "deepseek": "dfmodel", "deepseek-flash": "dfmodel",
    "deepseek-v4-flash": "dfmodel", "deepseek-v4-pro": "dmodel",
    "glm": "gmodel", "glm-5.3": "gmodel", "glm-5.3-flash": "gfmodel",
    "kimi": "kmodel", "kimi-k2.8-preview": "kmodel", "kimi-k3": "kmodel_latest",
    "qwen3.7-plus": "qmodel", "qwen3.7-max": "qmodel_latest",
    "qwen3.8-max": "qmodel_38max", "qwen3.8-flash": "qfmodel",
    "minimax-m3": "mmodel", "cantus": "cmodel",
}

_price_cache = {"mtime": None, "data": None}
_overrides_cache = {}
_price_lock = threading.RLock()


def _norm(name):
    return str(name or "").strip().lower()


def _norm_key(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _as_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_pricing(force=False):
    """读 pricing/trae_pricing.json；按 mtime 缓存，损坏时退化为空表。"""
    try:
        mtime = os.path.getmtime(PRICING_PATH)
    except OSError:
        mtime = None
    with _price_lock:
        if not force and _price_cache["data"] is not None \
                and _price_cache["mtime"] == mtime:
            return _price_cache["data"]
        try:
            with open(PRICING_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = {"meta": {}, "models": {}}
        if not isinstance(data, dict):
            data = {"meta": {}, "models": {}}
        data.setdefault("models", {})
        data.setdefault("meta", {})
        _price_cache.update({"mtime": mtime, "data": data})
        return data


def usd_cny(pricing=None):
    pricing = pricing or load_pricing()
    try:
        value = float((pricing.get("meta") or {}).get("usd_cny")
                      or _DEFAULT_USD_CNY)
    except (TypeError, ValueError):
        value = _DEFAULT_USD_CNY
    return value if value > 0 else _DEFAULT_USD_CNY


def _norm_index(pricing):
    """规范化名称 -> 价格条目。索引挂在 pricing 字典上，避免每行遍历整表。"""
    index = pricing.get("_norm_index")
    if isinstance(index, dict):
        return index
    index = {}
    for key, entry in (pricing.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        candidates = [key, entry.get("display"), entry.get("or_id")]
        candidates.extend(entry.get("aliases") or [])
        for candidate in candidates:
            norm = _norm_key(candidate)
            if norm and norm not in index:
                index[norm] = entry
    try:
        pricing["_norm_index"] = index
    except Exception:
        pass
    return index


def entry_for(model, pricing=None):
    """按 Trae 模型名查价；支持 key / 显示名 / or_id / 去标点归一。"""
    pricing = pricing or load_pricing()
    models = pricing.get("models") or {}
    if not model:
        return None
    name = str(model)
    for key in (name, name.lower()):
        if isinstance(models.get(key), dict):
            return models[key]
    return _norm_index(pricing).get(_norm_key(name))


def _rate(entry, prompt_tokens):
    flat = entry.get("flat") or {}
    rate = dict(flat)
    bands = entry.get("bands") or []
    chosen = None
    for band in bands:
        try:
            minimum = int(band.get("min_prompt_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if prompt_tokens >= minimum and (chosen is None or minimum >= chosen[0]):
            chosen = (minimum, band)
    if chosen:
        rate.update(chosen[1].get("flat") or {})
        return rate, chosen[0]
    return rate, None


def _cost_from_rates(prompt_tokens, completion_tokens, cached_tokens,
                     pin, pcached, pout):
    prompt = max(0, _as_float(prompt_tokens))
    cached = min(prompt, max(0, _as_float(cached_tokens)))
    out = max(0, _as_float(completion_tokens))
    return ((prompt - cached) * pin + cached * pcached + out * pout) / 1e6


def compute(model, prompt_tokens=0, completion_tokens=0, cached_tokens=0,
            pricing=None):
    """单次调用参考花费（USD / CNY）。返回 dict，未知模型 priced=False。"""
    pricing = pricing or load_pricing()
    entry = entry_for(model, pricing)
    prompt = max(0, _as_float(prompt_tokens))
    completion = max(0, _as_float(completion_tokens))
    cached = min(max(0, _as_float(cached_tokens)), prompt)
    if entry:
        rate, band = _rate(entry, prompt)
        miss = _as_float(rate.get("input_cache_miss"),
                         _as_float(rate.get("prompt")))
        hit = _as_float(rate.get("input_cache_hit"), miss)
        out = _as_float(rate.get("output"), _as_float(rate.get("completion")))
        unit = _as_float(entry.get("unit"), 1000000.0) or 1000000.0
        usd = _cost_from_rates(prompt, completion, cached, miss, hit, out) \
            * 1000000.0 / unit
        return {"usd": round(usd, 8),
                "cny": round(usd * usd_cny(pricing), 6),
                "priced": True, "band": band, "rates": rate,
                "or_id": entry.get("or_id"),
                "exact": entry.get("exact", True)}
    price = _legacy_price(model)
    if price is not None:
        usd = _cost_from_rates(prompt, completion, cached, *price)
        return {"usd": round(usd, 8),
                "cny": round(usd * usd_cny(pricing), 6),
                "priced": True, "band": None,
                "rates": {"input_cache_miss": price[0],
                          "input_cache_hit": price[1],
                          "output": price[2]},
                "or_id": None, "exact": True}
    return {"usd": 0.0, "cny": 0.0, "priced": False,
            "band": None, "rates": None, "or_id": None, "exact": False}


def compute_segments(model, prompt_tokens=0, completion_tokens=0,
                     cached_tokens=0, pricing=None, overrides=None):
    """三段等价花费（USD）：非缓存输入 / 缓存输入 / 输出。

    用于把「账户实际消耗积分」按三段成本占比分摊。未知模型 priced=False。
    """
    pricing = pricing or load_pricing()
    prompt = max(0, _as_float(prompt_tokens))
    completion = max(0, _as_float(completion_tokens))
    cached = min(prompt, max(0, _as_float(cached_tokens)))
    uncached = prompt - cached
    entry = entry_for(model, pricing)
    if entry:
        rate, band = _rate(entry, prompt)
        miss = _as_float(rate.get("input_cache_miss"),
                         _as_float(rate.get("prompt")))
        hit = _as_float(rate.get("input_cache_hit"), miss)
        out = _as_float(rate.get("output"), _as_float(rate.get("completion")))
        unit = _as_float(entry.get("unit"), 1000000.0) or 1000000.0
        scale = 1.0 / unit
        seg = {"input": uncached * miss * scale,
               "cached": cached * hit * scale,
               "output": completion * out * scale}
        seg["usd"] = seg["input"] + seg["cached"] + seg["output"]
        seg.update({"priced": True, "band": band,
                    "or_id": entry.get("or_id")})
        return seg
    price = (overrides or {}).get(_norm(model))
    if price is None:
        price = _legacy_price(model)
    if price is None:
        return {"input": 0.0, "cached": 0.0, "output": 0.0,
                "usd": 0.0, "priced": False, "band": None, "or_id": None}
    pin, pcached, pout = price
    seg = {"input": uncached * pin / 1e6,
           "cached": cached * pcached / 1e6,
           "output": completion * pout / 1e6}
    seg["usd"] = seg["input"] + seg["cached"] + seg["output"]
    seg.update({"priced": True, "band": None, "or_id": None})
    return seg


def compute_row(row, pricing=None, overrides=None):
    """给单条 usage 行算参考花费；支持用户覆盖表。"""
    if not isinstance(row, dict):
        return {"usd": 0.0, "cny": 0.0, "priced": False, "band": None,
                "rates": None, "or_id": None, "exact": False}
    model = row.get("model")
    prompt = row.get("prompt_tokens")
    completion = row.get("completion_tokens")
    cached = row.get("cached_tokens")
    price = (overrides or {}).get(_norm(model))
    if price is not None:
        usd = _cost_from_rates(prompt, completion, cached, *price)
        return {"usd": round(usd, 8),
                "cny": round(usd * usd_cny(pricing), 6),
                "priced": True, "band": None,
                "rates": {"input_cache_miss": price[0],
                          "input_cache_hit": price[1],
                          "output": price[2]},
                "or_id": None, "exact": True}
    return compute(model, prompt, completion, cached, pricing=pricing)


def annotate_row(row, pricing=None):
    """给 usage 行补 cost_usd / cost_cny / priced / price_band（原地+返回）。"""
    cost = compute_row(row, pricing=pricing)
    row["cost_usd"] = cost["usd"]
    row["cost_cny"] = cost["cny"]
    row["priced"] = bool(cost["priced"])
    if cost.get("band") is not None:
        row["price_band"] = cost["band"]
    return row


def summarize(rows, pricing=None):
    """批量汇总：USD/CNY、已定价/未定价行数与未定价模型集合。"""
    total_usd = 0.0
    priced = 0
    missing = {}
    for row in rows or []:
        cost = compute_row(row, pricing=pricing)
        total_usd += cost["usd"]
        if cost["priced"]:
            priced += 1
        else:
            model = str((row or {}).get("model") or "?")
            missing[model] = missing.get(model, 0) + 1
    return {"usd": round(total_usd, 8),
            "cny": round(total_usd * usd_cny(pricing), 6),
            "priced_rows": priced,
            "total_rows": len(rows or []),
            "missing": missing}


def load_overrides(usage_dir):
    """读用户覆盖表（usage/pricing-overrides.json）；按 mtime 缓存，损坏时 {}。"""
    path = os.path.join(usage_dir or "", "pricing-overrides.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _price_lock:
        hit = _overrides_cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key, val in data.items():
            try:
                if isinstance(val, dict):
                    out[_norm(key)] = (float(val.get("in", 0)),
                                       float(val.get("cached",
                                                    val.get("in", 0))),
                                       float(val.get("out", 0)))
                elif isinstance(val, (list, tuple)) and len(val) == 3:
                    out[_norm(key)] = tuple(float(x) for x in val)
            except Exception:
                continue
    with _price_lock:
        _overrides_cache[path] = (mtime, out)
    return out


def _legacy_price(name):
    """离线兜底表：返回 (in, cached_in, out) 或 None。"""
    key = _norm(name)
    if not key:
        return None
    if key in DEFAULT_PRICES:
        return DEFAULT_PRICES[key]
    if key in ALIASES:
        return DEFAULT_PRICES.get(ALIASES[key])
    # 前缀/后缀继承：deepseek-v4-flash-0731 -> deepseek-v4-flash
    for alias, target in ALIASES.items():
        if key.startswith(alias + "-") or key.startswith(alias + "_"):
            return DEFAULT_PRICES.get(target)
    return None


def price_for(name, overrides=None):
    """返回 (in, cached_in, out) 或 None（未知模型不计价）。"""
    key = _norm(name)
    if not key:
        return None
    ov = overrides or {}
    if key in ov:
        return ov[key]
    entry = entry_for(name)
    if entry:
        rate, _ = _rate(entry, 0)
        miss = _as_float(rate.get("input_cache_miss"),
                         _as_float(rate.get("prompt")))
        hit = _as_float(rate.get("input_cache_hit"), miss)
        out = _as_float(rate.get("output"), _as_float(rate.get("completion")))
        unit = _as_float(entry.get("unit"), 1000000.0) or 1000000.0
        scale = 1000000.0 / unit
        return (miss * scale, hit * scale, out * scale)
    return _legacy_price(name)


def cost_of(name, prompt_tokens=0, cached_tokens=0, completion_tokens=0,
            overrides=None):
    """按 tokens 折算 USD；未知模型返回 None。"""
    key = _norm(name)
    price = (overrides or {}).get(key)
    if price is not None:
        return _cost_from_rates(prompt_tokens, completion_tokens,
                                cached_tokens, *price)
    result = compute(name, prompt_tokens, completion_tokens, cached_tokens)
    return result["usd"] if result["priced"] else None


def table(usage_dir=None):
    """对外输出：生效价格表 + 来源（含用户覆盖标记）。"""
    ov = load_overrides(usage_dir) if usage_dir else {}
    rows = {}
    for key, (pin, pcached, pout) in DEFAULT_PRICES.items():
        rows[key] = {"in": pin, "cached": pcached, "out": pout,
                     "source": "builtin"}
    for key, entry in (load_pricing().get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        rate, _ = _rate(entry, 0)
        miss = _as_float(rate.get("input_cache_miss"),
                         _as_float(rate.get("prompt")))
        hit = _as_float(rate.get("input_cache_hit"), miss)
        out = _as_float(rate.get("output"), _as_float(rate.get("completion")))
        unit = _as_float(entry.get("unit"), 1000000.0) or 1000000.0
        scale = 1000000.0 / unit
        rows[key] = {"in": miss * scale, "cached": hit * scale,
                     "out": out * scale, "source": "openrouter",
                     "display": entry.get("display") or "",
                     "or_id": entry.get("or_id") or ""}
    for key, (pin, pcached, pout) in ov.items():
        rows[key] = {"in": pin, "cached": pcached, "out": pout,
                     "source": "override"}
    return rows
