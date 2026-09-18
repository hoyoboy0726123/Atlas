"""根據使用者設定建立 LLM client（支援 Groq / Gemini / Ollama / OpenRouter）。"""
import asyncio
import logging
import time
from typing import Any, Optional

from config import GROQ_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY
from settings import get_settings


# Gemini 2.5 系列使用 thinking_budget，3.x 系列使用 thinking_level
_GEMINI_3X_PREFIXES = ("gemini-3-", "gemini-3.", "gemini-3.1")


def _is_gemini_3x(model: str) -> bool:
    return any(model.startswith(p) for p in _GEMINI_3X_PREFIXES)


def _resolve_role_settings(s: dict, role: str) -> dict:
    """根據 role 從 settings 抽出該角色的 (provider, model, thinking 設定)。

    role='primary' → 用 provider/model
    role='secondary' → 用 secondary_provider/secondary_model;空時 fallback 回 primary

    回傳統一 key:provider/model/ollama_thinking/ollama_num_ctx/gemini_thinking/anthropic_thinking/ollama_base_url
    """
    if (role == "secondary"
            and (s.get("secondary_provider") or "").strip()
            and (s.get("secondary_model") or "").strip()):   # 兩者都要有,否則 fallback primary(避免只設 provider 沒設 model → KeyError)
        return {
            "provider": s.get("secondary_provider") or "",
            "model": s.get("secondary_model") or "",
            "ollama_thinking": s.get("secondary_ollama_thinking", "off"),
            "ollama_num_ctx": s.get("secondary_ollama_num_ctx", 16384),
            "gemini_thinking": s.get("secondary_gemini_thinking", "off"),
            "anthropic_thinking": s.get("secondary_anthropic_thinking", "off"),
            "ollama_base_url": s.get("ollama_base_url", "http://localhost:11434"),  # 共用
        }
    # primary 或 secondary 沒設定時 fallback
    return {
        "provider": s.get("provider") or "",
        "model": s.get("model") or "",
        "ollama_thinking": s.get("ollama_thinking", "off"),
        "ollama_num_ctx": s.get("ollama_num_ctx", 16384),
        "gemini_thinking": s.get("gemini_thinking", "off"),
        "anthropic_thinking": s.get("anthropic_thinking", "off"),
        "ollama_base_url": s.get("ollama_base_url", "http://localhost:11434"),
    }


def compute_retry_wait(err_msg: str, attempt: int) -> Optional[float]:
    """LLM 暫時錯誤的退避秒數;回 None = 不該重試(每日額度 RPD 用盡,等到明天才會恢復)。

    429(RESOURCE_EXHAUSTED)幾乎都是「每分鐘」限制(RPM/TPM):滑動窗最多 ~60s 就釋放,
    但等 1-2 秒必然還在同一窗內白白燒掉重試次數(實測 gemma-4 免費層 2026-07 頻繁踩到)
    → 改用 20/40 秒跨窗;Google 錯誤 body 常帶 retryDelay 官方建議秒數,優先採用。
    其他暫時性錯誤(5xx / overloaded)維持短退避。
    """
    m = err_msg or ""
    if "429" in m or "RESOURCE_EXHAUSTED" in m or "ResourceExhausted" in m:
        low = m.lower()
        if "perday" in low.replace(" ", "") or "per day" in low:
            return None  # RPD 用盡:重試無意義,直接報錯讓使用者換模型
        import re as _re
        rd = _re.search(r"retry[_ ]?delay[\"':{\s]+(?:seconds[\"':\s]+)?(\d+)", m, _re.IGNORECASE) \
            or _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", m, _re.IGNORECASE)
        if rd:
            return min(float(rd.group(1)) + 2.0, 90.0)
        return [20.0, 40.0][min(attempt, 1)]
    return float(2 ** attempt)


def tpm_overflow_hint(err_msg: str, est_input_tokens: int) -> Optional[str]:
    """429 且「單一請求的輸入就超過該模型每分鐘 token 上限(TPM)」→ 重試永遠無效。

    背景:hero 系統提示注入約 40-55K tokens,而 gemma-4 免費層 2026-07 被下修到
    16K TPM —— 單次就超標 2.5 倍以上,滑動窗永遠塞不進,20/40 秒退避只是白等。

    判斷條件(全部成立才觸發、避免誤殺):
    1. 錯誤是 429/RESOURCE_EXHAUSTED
    2. 配額指標是「輸入 token × 每分鐘」(Google quotaId 如
       GenerateContentInputTokensPerModelPerMinute-FreeTier)
    3. 錯誤 body 帶 quotaValue,且我們的輸入「低估值」仍超過它
       (呼叫端用 字元數//2 低估,寧可漏報、不可誤報)

    回 None = 非此情況(照常走退避重試);回字串 = 給使用者的說明,呼叫端應直接
    fail-fast、不再重試。
    """
    m = err_msg or ""
    if not ("429" in m or "RESOURCE_EXHAUSTED" in m or "ResourceExhausted" in m):
        return None
    low = m.lower().replace("_", "").replace("-", "").replace(" ", "")
    if "inputtoken" not in low or "perminute" not in low:
        return None
    import re as _re
    qv = _re.search(r"quota[_ ]?value[\"':\s]+(\d+)", m, _re.IGNORECASE)
    if not qv:
        return None
    limit = int(qv.group(1))
    if est_input_tokens <= limit:
        return None
    return (
        f"這次要送給模型的內容約 {est_input_tokens:,}+ tokens,已超過此模型的每分鐘 token 上限"
        f"(TPM ≈ {limit:,})。單一請求就塞不進限流窗口,重試等待也永遠不會成功。"
        f"建議:到 Settings 把主模型換成 TPM 較高的模型(如 Gemini Flash 系列或付費層),"
        f"弱模型留給工作流步驟(單步輸入小、不會踩到這個限制)。"
    )


def build_llm(temperature: float = 0.0, role: str = "primary") -> Any:
    """依當前設定回傳一個 LangChain chat model 實例。

    role:'primary'(預設、走主模型)或 'secondary'(走副模型;副模型未設則 fallback 主)
    """
    s = get_settings()
    cfg = _resolve_role_settings(s, role)
    provider = cfg["provider"]
    model = cfg["model"]

    if provider == "groq":
        from langchain_groq import ChatGroq
        # max_tokens 不設 = LangChain default 1024-4096、大段 Python(含三引號 heredoc)
        # 會被截斷成 unterminated string,LLM 寫 self-check py 一直 rc=1 syntax error 循環。
        # 8192 對 Groq 系列(llama / kimi / qwen)夠用、只按實際 token 收費,設高不多花錢。
        return ChatGroq(
            api_key=GROQ_API_KEY, model=model, temperature=temperature,
            max_tokens=8192,
        )

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        gem_thinking = cfg.get("gemini_thinking", "off")
        kwargs: dict[str, Any] = {
            "model": model,
            "google_api_key": GEMINI_API_KEY,
            "temperature": temperature,
            # 16384:對齊其他 provider、寫大 Python code(三引號 heredoc 寫長 md/json 報告)
            # 不被截斷。舊值 8192 對中文長報告會邊緣。Gemini 3 / Gemma 4 都支援這個上限。
            "max_output_tokens": 16384,
        }
        # 思考模式支援表(Gemini API):
        # - gemini-2.5-* : thinking_budget(integer)
        # - gemini-3.x   : thinking_level(low/medium/high)
        # - gemma-4-*    : thinking_level only,且不支援 thinking_budget(API 會回錯)
        #                  預設 thinking ON、要明確設 "minimal" 才會關(否則 reasoning 吃掉 tool_calls、native FC 失效)
        # - 其他(gemini-2.0, gemma-3 等):不支援思考、靜默略過
        is_gemma_4 = model.startswith("gemma-4-")
        supports_thinking = model.startswith("gemini-2.5-") or _is_gemini_3x(model) or is_gemma_4
        if is_gemma_4:
            # Gemma 4 native FC 必須關 thinking(否則 LLM 回應全跑 reasoning field、tool_calls 永遠空)
            # 即使 user 設 "off",對 Gemma 4 也要主動傳 "minimal" 才會真關(不傳 = 預設 thinking ON)
            kwargs["thinking_level"] = (
                gem_thinking if gem_thinking in ("low", "medium", "high") else "minimal"
            )
        elif gem_thinking != "off" and supports_thinking:
            if _is_gemini_3x(model):
                kwargs["thinking_level"] = gem_thinking if gem_thinking != "auto" else "medium"
            else:
                budget_map = {"auto": -1, "low": 1024, "medium": 4096, "high": 16384}
                kwargs["thinking_budget"] = budget_map.get(gem_thinking, -1)
        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        # max_tokens 不設 = OpenAI default(gpt-4o-mini 可能僅 4096)、大段 Python heredoc
        # 寫長 markdown/json 一定被截斷 → unterminated string → LLM 重寫 → 又截斷死循環。
        # 16384 對主流 gpt-5 / gpt-4o / o1 系列都支援、按實際生成收費、設高不多花錢。
        return ChatOpenAI(
            model=model,
            api_key=OPENAI_API_KEY,
            temperature=temperature,
            max_tokens=16384,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        # Anthropic 預設沒設 max_tokens 會吃 LangChain 的 1024 default、Claude 4 系列容易截斷。
        # 8192 對寫大段 Python(含三引號 heredoc)會在中途被斷流、收不到結尾的 """,
        # 解析時就會抛 unterminated triple-quoted string literal。設 32768 給足夠空間。
        # 只按實際生成 token 收費、不會因為設高了多花錢。
        #
        # Prompt caching (#153):啟用 prompt-caching 1h-TTL beta、跨輪 system prompt 命中 cache。
        # 多輪 workflow 第 2 輪起 cached input 只 0.1x 計價、實測省 70-85%。
        # 用法:caller(subagent_runner / executor)對 SystemMessage 加 cache_control:
        #   SystemMessage(content=..., additional_kwargs={"cache_control": {"type": "ephemeral"}})
        # ephemeral 預設 5 分 TTL,需要 1 小時 TTL 用 {"type":"ephemeral","ttl":"1h"} 並要 1h-cache beta header。
        return ChatAnthropic(
            model=model,
            api_key=ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=32768,
            default_headers={
                "anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
            },
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        thinking = cfg.get("ollama_thinking", "off")
        num_ctx = cfg.get("ollama_num_ctx", 32768)
        kwargs = {
            "model": model,
            "base_url": cfg.get("ollama_base_url") or "http://localhost:11434",
            "temperature": temperature,
            "num_ctx": num_ctx,
        }
        # auto → 不傳、讓模型自行決定;on/off → 明確開關(對 qwen3 等思考型模型有效)
        if thinking == "on":
            kwargs["reasoning"] = True
        elif thinking == "off":
            kwargs["reasoning"] = False
        return ChatOllama(**kwargs)

    if provider == "claude_cli":
        # 訂閱帳號 CLI 當大腦(headless `claude -p`,吃 Pro/Max 額度、非 API 計費)。
        # 從 Atlas 內部走 → 吃到完整系統提示注入 + app 上下文(MCP 外掛做不到)。
        from claude_cli_provider import ClaudeCliChat
        return ClaudeCliChat(model=model or "sonnet", temperature=temperature)

    raise ValueError(f"unknown provider: {provider}")


# ── CLI 大腦沒有原生 function calling → 這些 provider 要走「文字協議」loop ──
_CLI_PROVIDERS = {"claude_cli"}


def provider_has_native_fc(role: str = "primary") -> bool:
    """該 role 目前的 provider 是否支援原生 function calling。
    CLI 訂閱大腦(claude_cli)沒有 → 回 False,呼叫端據此改走 <tool> 文字協議。"""
    try:
        cfg = _resolve_role_settings(get_settings(), role)
        return (cfg.get("provider") or "") not in _CLI_PROVIDERS
    except Exception:
        return True


# Ollama 模型 vision 能力查詢結果快取（key = (base_url, model)）。
# /api/show 是本機呼叫、很快，但這支會被 VLM 路徑頻繁呼叫，還是快取一下。
_OLLAMA_VISION_CACHE: dict = {}


def _ollama_has_vision(base_url: str, model: str) -> bool:
    """問 Ollama 這顆模型到底看不看得到圖。

    ⚠ 2026-08-13 修正：舊版只看 provider 不看 model，所以
    `provider=ollama, model=qwen3:8b`（純文字）會被判定成「支援看圖」，
    於是圖片照送、Ollama 收下後**默默忽略**，模型基於「沒看到畫面」給出
    很有自信的判斷 —— 正是這支函式的 docstring 自己警告的那種靜默錯誤，
    但守門條件只擋得住 CLI provider。

    /api/show 的 capabilities 會明確列出 vision（實測 qwen3.6:27b 有、
    qwen3:8b 沒有）。查不到一律**保守回 False** —— 寧可讓呼叫端改用
    另一個 role 或明確報錯，也不要送圖給看不到的模型。
    """
    key = (base_url, model)
    if key in _OLLAMA_VISION_CACHE:
        return _OLLAMA_VISION_CACHE[key]
    ok = False
    try:
        import httpx
        r = httpx.post(f"{base_url.rstrip('/')}/api/show",
                       json={"model": model}, timeout=8.0)
        if r.status_code == 200:
            caps = r.json().get("capabilities") or []
            ok = "vision" in [str(c).lower() for c in caps]
    except Exception:
        ok = False
    _OLLAMA_VISION_CACHE[key] = ok
    return ok


def provider_supports_vision(role: str = "primary") -> bool:
    """該 role 目前的「provider + model」組合能不能吃圖片。

    CLI 訂閱大腦走純文字橋(image blocks 被丟掉) → False。
    Ollama → 實際查該模型的 capabilities（純文字模型很常見）。
    其他雲端 provider → 沿用舊行為視為 True。

    呼叫端（cu_vlm_verifier / visual_validator / computer_use）會據此改用
    另一個 role 或明確報錯 —— 絕不能讓模型「沒看到圖就下判決」。
    """
    try:
        cfg = _resolve_role_settings(get_settings(), role)
        provider = (cfg.get("provider") or "").strip()
        if provider in _CLI_PROVIDERS:
            return False
        if provider == "ollama":
            return _ollama_has_vision(
                cfg.get("ollama_base_url") or "http://localhost:11434",
                (cfg.get("model") or "").strip())
        return True
    except Exception:
        return True

def resolve_loop_mode(env_mode: str, role: str = "primary") -> str:
    """把 SUBAGENT_LOOP_MODE(native/text)依 provider 修正:
    CLI provider 一律 text(它吐 <tool> 純文字、沒有 native tool_calls)。"""
    if not provider_has_native_fc(role):
        return "text"
    return (env_mode or "native").strip().lower()


async def invoke_with_streaming(
    llm: Any,
    messages: list,
    *,
    label: str = "LLM",
    timeout: float = 300.0,
    logger: Optional[logging.Logger] = None,
    return_usage: bool = False,
) -> Any:
    """以串流方式呼叫 LLM，附帶進度記錄與硬性超時。

    - 用串流避免 Ollama thinking 模式整包 buffer 造成「看起來卡住」
    - 每 15 秒打一行 log 顯示目前進度（reasoning / content 累積字數）
    - 超過 timeout 秒直接拋 asyncio.TimeoutError
    - 回傳最終 content 字串（reasoning_content 僅消耗，不回傳）

    return_usage=True 時改回 dict {content, usage_metadata, model}，
    供需要算 token / 成本的 caller（subagent_runner）使用。
    向後相容：預設 False 仍回純字串。
    """
    log = logger or logging.getLogger(__name__)
    start = time.time()
    last_log = start
    content_parts: list[str] = []
    reasoning_len = 0
    chunk_count = 0
    final_chunk = None
    # streaming 中 usage 可能 attach 在最後一個 chunk、也可能跨 chunk 增量；累計保險
    # cache_read / cache_creation 用來驗 Anthropic Prompt Caching 是否真的命中
    acc_um = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }

    try:
        input_chars = sum(len(str(getattr(m, "content", "") or "")) for m in messages)
        log.info(f"[{label}] 🤖 LLM 開始處理（input {input_chars:,} 字）…")
    except Exception:
        log.info(f"[{label}] 🤖 LLM 開始處理…")

    async def _stream():
        nonlocal last_log, reasoning_len, chunk_count, final_chunk
        async for chunk in llm.astream(messages):
            chunk_count += 1
            final_chunk = chunk
            # 累計 usage_metadata（langchain 0.2+ 標準）— 多數 provider 只在 final chunk 給，累加安全
            um = getattr(chunk, "usage_metadata", None)
            if isinstance(um, dict) and um:
                for k in ("input_tokens", "output_tokens", "total_tokens"):
                    v = um.get(k)
                    if v:
                        acc_um[k] = acc_um.get(k, 0) + int(v)
                # langchain 0.3+ Anthropic: input_token_details = {"cache_read": N, "cache_creation": N}
                # 部分 provider 改放頂層 cache_read_tokens → 兩種形狀都收,漏收會低估成本
                itd = um.get("input_token_details") or {}
                if not isinstance(itd, dict):
                    itd = {}
                cr = itd.get("cache_read") or um.get("cache_read_tokens")
                if cr:
                    acc_um["cache_read_tokens"] += int(cr)
                cc = itd.get("cache_creation") or um.get("cache_creation_tokens")
                if cc:
                    acc_um["cache_creation_tokens"] += int(cc)
            c = getattr(chunk, "content", None)
            if c:
                if isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict):
                            t = block.get("text") or ""
                            if t:
                                content_parts.append(t)
                        elif isinstance(block, str):
                            content_parts.append(block)
                else:
                    content_parts.append(str(c))
            extra = getattr(chunk, "additional_kwargs", None) or {}
            rc = extra.get("reasoning_content") or extra.get("reasoning") or ""
            if rc:
                reasoning_len += len(rc) if isinstance(rc, str) else 0
            now = time.time()
            if now - last_log >= 15.0:
                elapsed = now - start
                total = sum(len(p) for p in content_parts)
                if reasoning_len:
                    log.info(
                        f"[{label}] 🧠 思考中… {elapsed:.0f}s（reasoning {reasoning_len} 字, content {total} 字）"
                    )
                else:
                    log.info(
                        f"[{label}] ✍️ 產生中… {elapsed:.0f}s（content {total} 字）"
                    )
                last_log = now

    # ── 暫時性 API 錯誤自動重試（500/502/503/504/429、overloaded 等）──
    # 這類錯誤是 provider 服務端打嗝、不是任務本身的問題;靜默重試幾次
    # 比把 step 打成失敗、再丟給使用者按「重試」務實得多。
    _TRANSIENT_MARKERS = (
        "500", "502", "503", "504", "429",
        "INTERNAL", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "ResourceExhausted",
        "overloaded", "ServiceUnavailable", "Service Unavailable",
        "deadline", "DEADLINE", "Internal error", "try again",
    )
    _API_RETRY_BACKOFF = [3.0, 8.0, 20.0]  # 最多自動重試 3 次（共 4 次嘗試）

    for _attempt in range(len(_API_RETRY_BACKOFF) + 1):
        # 每次嘗試前重設串流累積狀態（避免上次 partial 殘留疊加）
        content_parts.clear()
        reasoning_len = 0
        chunk_count = 0
        final_chunk = None
        for _k in list(acc_um.keys()):
            acc_um[_k] = 0
        start = time.time()
        last_log = start
        try:
            await asyncio.wait_for(_stream(), timeout=timeout)
            break  # 串流成功、跳出重試圈
        except asyncio.TimeoutError:
            total = sum(len(p) for p in content_parts)
            log.error(
                f"[{label}] LLM 串流逾時（>{timeout:.0f}s），已收集 reasoning {reasoning_len} 字 / content {total} 字"
            )
            raise
        except Exception as _api_exc:
            _msg = str(_api_exc)
            _is_transient = any(m in _msg for m in _TRANSIENT_MARKERS)
            if _is_transient and _attempt < len(_API_RETRY_BACKOFF):
                _wait = compute_retry_wait(_msg, _attempt)
                if _wait is None:
                    log.warning(f"[{label}] 每日額度(RPD)用盡、不重試 — 請切換模型或等額度重置")
                    raise
                _wait = max(_wait, _API_RETRY_BACKOFF[_attempt])
                log.warning(
                    f"[{label}] ⚠ LLM API 暫時性錯誤（{_msg[:140]}）"
                    f"→ {_wait:.0f}s 後自動重試 {_attempt + 1}/{len(_API_RETRY_BACKOFF)}"
                )
                await asyncio.sleep(_wait)
                continue
            # 非暫時性錯誤 or 重試已用盡 → 往上拋
            raise

    elapsed = time.time() - start
    total = sum(len(p) for p in content_parts)
    content = "".join(content_parts)

    # Usage：優先用 stream loop 累計的；空時 fallback 到 final_chunk 各種 metadata 位置
    # 1) langchain 0.2+ standard usage_metadata
    # 2) response_metadata.token_usage (OpenAI / Groq 風格 prompt_tokens/completion_tokens)
    # 3) additional_kwargs.usage (個別 provider)
    usage = dict(acc_um) if acc_um.get("total_tokens", 0) > 0 else {}
    model_name = ""
    if final_chunk is not None:
        if not usage:
            rm = getattr(final_chunk, "response_metadata", None) or {}
            tk = rm.get("token_usage") or rm.get("usage") or {}
            if tk:
                usage = {
                    "input_tokens": int(tk.get("prompt_tokens") or tk.get("input_tokens") or 0),
                    "output_tokens": int(tk.get("completion_tokens") or tk.get("output_tokens") or 0),
                    "total_tokens": int(tk.get("total_tokens") or 0),
                    # Anthropic native shape: cache_read_input_tokens / cache_creation_input_tokens
                    "cache_read_tokens": int(tk.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(tk.get("cache_creation_input_tokens") or 0),
                }
        if not usage:
            ak = getattr(final_chunk, "additional_kwargs", None) or {}
            u = ak.get("usage") or {}
            if u:
                usage = {
                    "input_tokens": int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
                    "output_tokens": int(u.get("completion_tokens") or u.get("output_tokens") or 0),
                    "total_tokens": int(u.get("total_tokens") or 0),
                    "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
                }
        # streaming 已抓到 input/output/total 但 cache 沒抓到時、補抓一次
        if usage and usage.get("cache_read_tokens", 0) == 0 and usage.get("cache_creation_tokens", 0) == 0:
            rm = getattr(final_chunk, "response_metadata", None) or {}
            tk = rm.get("token_usage") or rm.get("usage") or {}
            if tk:
                cr = int(tk.get("cache_read_input_tokens") or 0)
                cc = int(tk.get("cache_creation_input_tokens") or 0)
                if cr or cc:
                    usage["cache_read_tokens"] = cr
                    usage["cache_creation_tokens"] = cc
        # 仍空時把 acc_um 也丟回去（即使全 0、結構完整方便前端判斷 'stream 沒給 usage'）
        if not usage:
            usage = dict(acc_um)
        # provider model name（給成本對照表用）
        rm = getattr(final_chunk, "response_metadata", None) or {}
        model_name = rm.get("model_name") or rm.get("model") or ""
    # streaming chunk 沒 attach model 名稱時(常見:Groq / 部分 Gemini)，用設定頁的 model 當 fallback
    if not model_name:
        try:
            from settings import get_settings
            model_name = (get_settings().get("model") or "").strip()
        except Exception:
            pass

    # cache hit 摘要（只有 cache_read > 0 才顯示、避免噪音）
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_tokens", 0) or 0
    input_tok = usage.get("input_tokens", 0) or 0
    if cache_read or cache_creation:
        # cache hit 比例:cache_read / (cache_read + 非 cached input)
        # Anthropic spec:input_tokens 已扣掉 cache_read,所以總 prompt = input_tokens + cache_read + cache_creation
        total_prompt = input_tok + cache_read + cache_creation
        hit_pct = (cache_read / total_prompt * 100) if total_prompt > 0 else 0
        cache_str = f", cache_read {cache_read:,} ({hit_pct:.0f}%), cache_write {cache_creation:,}"
    else:
        cache_str = ""

    if reasoning_len:
        log.info(
            f"[{label}] ✅ LLM 完成（{elapsed:.0f}s, reasoning {reasoning_len} 字, content {total} 字, "
            f"tokens {usage.get('total_tokens', '?')}{cache_str}）"
        )
    else:
        log.info(
            f"[{label}] ✅ LLM 完成（{elapsed:.0f}s, content {total} 字, "
            f"tokens {usage.get('total_tokens', '?')}{cache_str}）"
        )

    if return_usage:
        # provider 直接問這次實際用的 llm 物件（不是查設定）——
        # 設定可能在 run 途中被改，物件本身才是這次呼叫的真相。
        # 訂閱路徑(claude_cli)要標出來，讓前端顯示「訂閱不計費、此為 API 等值成本」。
        try:
            _prov = str(getattr(llm, "_llm_type", "") or "")
        except Exception:
            _prov = ""
        if not _prov:
            _prov = type(llm).__name__
        return {"content": content, "usage_metadata": usage,
                "model": model_name, "provider": _prov}
    return content
