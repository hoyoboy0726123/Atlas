"""模型單價表 + 分項成本計算。

## 為什麼一定要「分項」算

Anthropic 規格中 `input_tokens` **不包含**快取讀取
(`cache_read_input_tokens` / `cache_creation_input_tokens` 是獨立欄位),
而三種 input 的單價差到 20 倍:

    一般 input   1.00x
    快取讀取     0.10x   ← 便宜 10 倍
    快取寫入     1.25x(5 分鐘 TTL)/ 2.00x(1 小時 TTL)

所以「把 token 加總再乘一個單價」會嚴重失真:
  - 快取命中率高時會**高估**(大部分其實只要 1/10 價)
  - 只看 input_tokens 而漏掉快取欄位時會**低估**(實測某 run 顯示 input=36,
    真實 prompt 是十幾萬 token —— 全跑到 cache_read 去了)

OpenAI / Gemini 的慣例不同(`prompt_tokens` 已含快取),因此各 provider
的欄位語意由 `estimate_cost()` 統一處理,呼叫端不需要知道差異。

## 單價來源與時效

- Anthropic:官方定價表(2026-06 版)
- Gemini:ai.google.dev/gemini-api/docs/pricing(2026-07-27 實查)
- OpenAI:developers.openai.com/api/docs/pricing(2026-07-27 實查)

價格會變。要覆寫或補新模型,在 backend/ 放 `model_pricing.json`:

    { "my-model": {"input": 1.0, "output": 5.0, "cache_read": 0.1} }

**未知模型不會假造成本** —— 回傳 priced=False,只顯示 token 數。
寧可不顯示,也不顯示錯的數字。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("token_cost")

PRICING_AS_OF = "2026-07-27"

# 每百萬 token 的美元單價。
# cache_read / cache_write 省略時,Anthropic 系列用倍率推導(見 _fill_anthropic)。
_PRICING: dict[str, dict[str, float]] = {}


def _anthropic(input_price: float, output_price: float) -> dict[str, float]:
    """Anthropic 系列:快取單價由 input 單價依官方倍率推導。

    cache_read 0.1x、cache_write 1.25x(5m)、2x(1h)。
    分開列出 5m/1h,因為本專案 AI 助手走 1h TTL(main.py 有 ephemeral 1h
    cache_control),pipeline 步驟則多為預設 5m —— 兩者價差 1.6 倍。
    """
    return {
        "input": input_price,
        "output": output_price,
        "cache_read": round(input_price * 0.10, 4),
        "cache_write": round(input_price * 1.25, 4),      # 5 分鐘 TTL(預設)
        "cache_write_1h": round(input_price * 2.00, 4),   # 1 小時 TTL
    }


_PRICING.update({
    # ── Anthropic ──────────────────────────────────────────────
    "claude-fable-5":    _anthropic(10.0, 50.0),
    "claude-mythos-5":   _anthropic(10.0, 50.0),
    "claude-opus-5":     _anthropic(5.0, 25.0),
    "claude-opus-4-8":   _anthropic(5.0, 25.0),
    "claude-opus-4-7":   _anthropic(5.0, 25.0),
    "claude-opus-4-6":   _anthropic(5.0, 25.0),
    "claude-opus-4-5":   _anthropic(5.0, 25.0),
    "claude-opus-4-1":   _anthropic(15.0, 75.0),
    "claude-sonnet-5":   _anthropic(3.0, 15.0),   # 2026-08-31 前有 $2/$10 導入價
    "claude-sonnet-4-6": _anthropic(3.0, 15.0),
    "claude-sonnet-4-5": _anthropic(3.0, 15.0),
    "claude-haiku-4-5":  _anthropic(1.0, 5.0),

    # ── Google Gemini(ai.google.dev 實查)────────────────────
    # 注意:Pro 系列 >200k context 會跳價、音訊輸入另計,這裡取
    # 「≤200k + 文字/圖片/影片」檔(絕大多數情境)。
    # normalize_model 走最長前綴,所以 -lite 一定會蓋過同族的非 lite。
    "gemini-2.5-pro":         {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "gemini-2.5-flash":       {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-2.5-flash-lite":  {"input": 0.10, "output": 0.40, "cache_read": 0.01},
    "gemini-3-flash":         {"input": 0.50, "output": 3.00, "cache_read": 0.05},
    "gemini-3-pro":           {"input": 2.00, "output": 12.0, "cache_read": 0.20},
    "gemini-3.1-pro":         {"input": 2.00, "output": 12.0, "cache_read": 0.20},
    "gemini-3.1-flash-lite":  {"input": 0.25, "output": 1.50, "cache_read": 0.025},
    "gemini-3.5-flash":       {"input": 1.50, "output": 9.00, "cache_read": 0.15},
    "gemini-3.5-flash-lite":  {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-3.6-flash":       {"input": 1.50, "output": 7.50, "cache_read": 0.15},

    # ── OpenAI(developers.openai.com 實查)────────────────────
    "gpt-5.6-sol":   {"input": 5.00, "output": 30.0, "cache_read": 0.50},
    "gpt-5.6-terra": {"input": 2.50, "output": 15.0, "cache_read": 0.25},
    "gpt-5.6-luna":  {"input": 1.00, "output": 6.00, "cache_read": 0.10},
    "gpt-5.5":       {"input": 5.00, "output": 30.0, "cache_read": 0.50},
    "gpt-5.5-pro":   {"input": 30.0, "output": 180.0},
    "gpt-5.4":       {"input": 2.50, "output": 15.0, "cache_read": 0.25},
    "gpt-5.4-mini":  {"input": 0.75, "output": 4.50, "cache_read": 0.075},
    "gpt-5.4-nano":  {"input": 0.20, "output": 1.25, "cache_read": 0.02},
})

# 不計費的模型:本機跑的(ollama 等)與 Google AI Studio 免費層的 gemma。
# 注意:llama/qwen/mistral 走託管 API 時其實要錢 —— 這裡預設當本機自架,
# 若你是走付費託管,請用 model_pricing.json 覆寫成實際單價。
_FREE_PREFIXES = ("ollama/", "llama", "qwen", "mistral", "gemma", "phi", "deepseek-r1")

# 短名 → 正式 key。claude_cli 只回報 "opus"/"sonnet" 這種短名。
_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}


def _load_overrides() -> None:
    """讀 backend/model_pricing.json 覆寫或補充單價(使用者可自行維護)。"""
    p = Path(__file__).resolve().parent / "model_pricing.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and "input" in v and "output" in v:
                    _PRICING[str(k).strip().lower()] = {
                        kk: float(vv) for kk, vv in v.items()
                        if isinstance(vv, (int, float))
                    }
            logger.info(f"[token_cost] 已套用 model_pricing.json 覆寫({len(data)} 筆)")
    except Exception as e:
        logger.warning(f"[token_cost] model_pricing.json 讀取失敗、忽略:{e}")


_load_overrides()


def normalize_model(raw: Any) -> Optional[str]:
    """把各家回報的 model 字串正規化成單價表的 key。

    處理:大小寫、日期後綴(claude-opus-4-5-20251101)、provider 前綴
    (anthropic/claude-opus-5、models/gemini-2.5-flash)、短名別名。
    找不到就回 None —— 呼叫端據此決定「不顯示成本」而非亂猜。
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None

    # 去掉 provider 前綴:anthropic/xxx、models/xxx、openai/xxx
    if "/" in s:
        s = s.rsplit("/", 1)[-1]

    if s in _ALIASES:
        s = _ALIASES[s]
    if s in _PRICING:
        return s

    # 去掉尾端日期後綴(-20251101 / @20251101)
    s2 = re.sub(r"[-@]\d{8}$", "", s)
    if s2 in _PRICING:
        return s2

    # 本機自架 / 免費層 → 不計費
    if any(s2.startswith(p) or p in s2 for p in _FREE_PREFIXES):
        return "__local__"

    # 最長前綴匹配(涵蓋 claude-opus-5-something、gemini-2.5-flash-preview 等)
    best = None
    for key in _PRICING:
        if s2.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def estimate_cost(usage: dict, *, cache_ttl: str = "5m") -> dict:
    """依 token 用量分項計算美元成本。

    Args:
        usage: 含 input_tokens / output_tokens / cache_read_tokens /
               cache_creation_tokens / model 的 dict。
        cache_ttl: "5m"(預設)或 "1h" —— 決定快取寫入單價倍率。

    Returns:
        dict:
          priced        是否算得出成本(未知模型 = False,不假造數字)
          model_key     對應到的單價表 key
          input_usd / cache_read_usd / cache_write_usd / output_usd / total_usd
          saved_usd     因快取而省下的錢(相對於全額 input 計價)
          prompt_tokens 真實 prompt 總量(input + cache_read + cache_write)
          note          給前端顯示的說明
    """
    u = usage or {}

    def _n(key: str) -> int:
        try:
            return max(0, int(u.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    in_tok = _n("input_tokens")
    out_tok = _n("output_tokens")
    cr_tok = _n("cache_read_tokens")
    cw_tok = _n("cache_creation_tokens")
    prompt_tok = in_tok + cr_tok + cw_tok

    key = normalize_model(u.get("model"))
    # 訂閱路徑(claude_cli)實際不從 API 扣款 —— 金額仍要算,但必須標示清楚,
    # 否則使用者會以為這筆錢真的被扣了。
    _prov = str(u.get("provider") or "").strip().lower()
    if _prov:
        is_sub = _prov == "claude_cli"
    else:
        # 舊 run 沒存 provider(欄位是後來才加的)→ 用 model 名稱回推。
        # claude CLI 只回報 "opus"/"sonnet"/"haiku" 這種**裸短名**;
        # Anthropic API 一律回完整 ID(claude-opus-5、claude-opus-4-5-20251101),
        # 其他家也都是完整名 —— 所以裸短名等同 claude_cli 的指紋。
        is_sub = str(u.get("model") or "").strip().lower() in _ALIASES
    base = {
        "priced": False,
        "model_key": key or "",
        "billing": "subscription" if is_sub else "api",
        "input_usd": 0.0, "cache_read_usd": 0.0,
        "cache_write_usd": 0.0, "output_usd": 0.0,
        "total_usd": 0.0, "saved_usd": 0.0,
        "prompt_tokens": prompt_tok,
        "total_tokens": prompt_tok + out_tok,
        "note": "",
    }

    if key == "__local__":
        base["priced"] = True
        base["note"] = "本機自架或免費層、不計費(若走付費託管請用 model_pricing.json 覆寫)"
        return base

    price = _PRICING.get(key or "")
    if not price:
        base["note"] = (
            f"未知模型「{u.get('model') or '(未記錄)'}」— 無單價資料,只顯示 token。"
            "可在 backend/model_pricing.json 補上單價。"
        )
        return base

    p_in = float(price.get("input", 0.0))
    # 缺 cache_read 單價時保守用 input 全額(不低報)
    p_cr = float(price.get("cache_read", p_in))
    if cache_ttl == "1h" and "cache_write_1h" in price:
        p_cw = float(price["cache_write_1h"])
    else:
        p_cw = float(price.get("cache_write", p_in))
    p_out = float(price.get("output", 0.0))

    M = 1_000_000.0
    input_usd = in_tok / M * p_in
    cr_usd = cr_tok / M * p_cr
    cw_usd = cw_tok / M * p_cw
    out_usd = out_tok / M * p_out
    # 省下的錢:快取讀取若按全額 input 計價會多花多少
    saved = cr_tok / M * (p_in - p_cr)

    base.update({
        "priced": True,
        "input_usd": round(input_usd, 6),
        "cache_read_usd": round(cr_usd, 6),
        "cache_write_usd": round(cw_usd, 6),
        "output_usd": round(out_usd, 6),
        "total_usd": round(input_usd + cr_usd + cw_usd + out_usd, 6),
        "saved_usd": round(saved, 6),
    })
    if is_sub:
        base["note"] = "訂閱不計費,此為 API 等值成本(供評估是否轉 API 用)"
    return base


def sum_costs(usages: list[dict], *, cache_ttl: str = "5m") -> dict:
    """多個步驟的成本加總。任一步驟算不出價就標記 partial。"""
    agg = {
        "priced": True, "partial": False, "billing": "api",
        "input_usd": 0.0, "cache_read_usd": 0.0, "cache_write_usd": 0.0,
        "output_usd": 0.0, "total_usd": 0.0, "saved_usd": 0.0,
        "prompt_tokens": 0, "total_tokens": 0, "note": "",
    }
    seen_any = False
    any_sub = False
    for usg in usages or []:
        if not usg:
            continue
        c = estimate_cost(usg, cache_ttl=cache_ttl)
        agg["prompt_tokens"] += c["prompt_tokens"]
        agg["total_tokens"] += c["total_tokens"]
        if c.get("billing") == "subscription":
            any_sub = True
        if c["priced"]:
            seen_any = True
            for k in ("input_usd", "cache_read_usd", "cache_write_usd",
                      "output_usd", "total_usd", "saved_usd"):
                agg[k] = round(agg[k] + c[k], 6)
        elif c["total_tokens"] > 0:
            agg["partial"] = True
    agg["priced"] = seen_any
    # 只要有任一步驟走訂閱,整筆就標訂閱(避免使用者誤以為全額被扣款)
    if any_sub:
        agg["billing"] = "subscription"
        agg["note"] = "訂閱不計費,此為 API 等值成本(供評估是否轉 API 用)"
    return agg


def format_usd(v: float) -> str:
    """成本字串。金額很小時保留更多位數,避免一律顯示 $0.00。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$0"
    if v <= 0:
        return "$0"
    if v < 0.0001:
        return "<$0.0001"   # 不用科學記號:$5.0e-5 在成本欄會被誤讀成亂碼
    if v < 0.01:
        return f"${v:.4f}"
    if v < 1:
        return f"${v:.3f}"
    return f"${v:.2f}"
