"""使用者可調設定（模型選擇等）— 持久化到 JSON 檔案。"""
import json
import threading
from pathlib import Path
from typing import Optional

from config import OUTPUT_BASE_PATH, GEMINI_MODEL_MAIN

_SETTINGS_PATH = OUTPUT_BASE_PATH / "pipeline_settings.json"
_lock = threading.Lock()

# 預設:Gemini(只要貼上 API Key 就能用;模型見 config.GEMINI_MODEL_MAIN)
_DEFAULT = {
    # ── 主模型(預設、所有節點不另設 llm_role 時用這個)──
    "provider": "gemini",         # "gemini" | "groq" | "ollama" | "openai" | "anthropic" | "claude_cli"
    "model": GEMINI_MODEL_MAIN,    # e.g. "gemini-3.5-flash-lite" or "qwen3:8b"
    "ollama_base_url": "http://localhost:11434",
    "ollama_thinking": "off",      # "auto" | "on" | "off" — 預設關閉，避免 thinking 模式 rambling 卡住
    "ollama_num_ctx": 65536,       # Ollama context window tokens（僅 Ollama）。AI 助手一輪 = 提示詞 1.4~4 萬 + 工具 5.5k
                                   # + 對話 + 回答;32768 實測會被 Ollama 靜默截掉前半(模型只看到後 16k)
    "gemini_thinking": "off",      # "off" | "auto" | "low" | "medium" | "high"
    "anthropic_thinking": "off",   # "off" | "on" — Claude Opus 4 系列 extended thinking
    # ── 副模型(節點可在 panel 選 llm_role: secondary 切到這個)──
    # 預設全空、=不啟用副模型;任一 node 設 llm_role=secondary 而副模型未設 → fallback 主
    "secondary_provider": "",
    "secondary_model": "",
    "secondary_ollama_thinking": "off",
    "secondary_ollama_num_ctx": 16384,
    "secondary_gemini_thinking": "off",
    "secondary_anthropic_thinking": "off",
    # 通知設定
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # Telegram 遠端遙控：開啟後 bot 會處理你發的指令訊息（/menu 列工作流、按按鈕啟動）
    # 預設 OFF — 安全起見、要使用時手動開
    # 開啟前提：telegram_bot_token + telegram_chat_id 都要填好
    "telegram_remote_control": False,
    "line_notify_token": "",       # LINE Notify（預留）
    # Skill 沙盒：run_python / run_shell 執行位置
    #   "host"       — Windows 原生 subprocess（快）
    #   "wsl_docker" — 透過 WSL 內的 atlas-sandbox 容器執行（隔離、需先跑 sandbox/setup_sandbox.bat）
    "skill_sandbox_mode": "host",
    # Skill 檔案目錄:留空 = 預設 ~/.agents/skills/;可填自訂絕對路徑,
    # 讓專案使用者把 skill 檔放在自己選的位置。亦可用環境變數 SKILLS_DIR 覆蓋。
    "skills_dir": "",
    # 網路搜尋（Tavily）：skill agent 需要即時 / 外部資訊時使用
    # key 可先填但預設關閉，避免誤觸扣費
    "tavily_api_key": "",
    "web_search_enabled": False,
    # 完整內容模式：ON 時 Tavily 直接回文章原文（Agent 不用寫爬蟲）
    # 代價：一次回傳 ~15000 字，需要雲端大 context 模型；本地 Ollama 8B 小 context 會爆
    "web_search_full_content_default": False,
    # 搜尋深度預設:advanced 模式比 basic 貴 2x、但研究品質好 5-10x。
    # Atlas 定位深度研究 → 預設 ON。chat 助手 / skill / subagent 全部受影響。
    # 帳單失控時可從設定頁關掉、改成 LLM 個別 input 才走 advanced。
    "web_search_deep_default": True,
    # 含 computer_use 節點的工作流啟動時自動縮小前景視窗(通常是 Atlas 瀏覽器)、
    # 結束後自動還原。避免 Atlas 視窗擋住要自動化的目標 app。
    # 預設 OFF — 使用者自己決定要不要打擾桌面。並發 workflow 用 ref-count 處理。
    "auto_minimize_for_computer_use": False,
    # AI 助手長期記憶(facts 語意記憶):記得使用者偏好 / 事實、跨對話「越用越懂」。
    # 開 → 提供 remember/recall 等記憶工具 + 每輪注入記憶快照進 system prompt。
    # 關 → 完全不載入記憶工具、不注入快照(已存的記憶保留、不刪)。預設 ON。
    "memory_enabled": True,
    # 激進學習:對話結束自動「推斷」使用者偏好存成低信心 inferred fact(不必明說「記下」)。
    # 開 → 越用越懂最快,但 AI 可能過度推斷 / 記錯(都標(推測)、可改可刪)。
    # 關 → 只記使用者明確說「記下」的事(保守、零誤記)。預設 OFF — 要積極才開。
    # 依賴 memory_enabled;memory_enabled 關時此項無效。
    "memory_aggressive": False,
    # 工作流自我修復:某步驟失敗(重試耗盡)時,讓 AI 助手讀 log + 比對自己寫的 YAML、
    # 找出錯誤、改 YAML、從失敗步重跑,循環到成功或達上限才轉人工決策。
    # 開 → 失敗自動背景修復(前端/TG 看得到「AI 修復中 N/M」)。
    # 關(預設)→ 維持現狀:失敗即轉 awaiting_human 等人工。預設 OFF — 自動改 YAML 有風險、要明確開。
    "self_heal_enabled": False,
    # 單次 run 最多自動修復幾次(每次 = 一輪「讀 log → 改 YAML → 重跑」)。
    # 2 是安全預設:夠救「路徑錯/參數錯/缺前置步驟」這類一兩次能修好的、又不會無限燒 token。硬 cap 5。
    "self_heal_max_attempts": 2,
}

_cache: Optional[dict] = None


def _load_from_disk() -> dict:
    if _SETTINGS_PATH.exists():
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(_DEFAULT)
            merged.update({k: v for k, v in data.items() if k in _DEFAULT})
            return merged
        except Exception:
            pass
    return dict(_DEFAULT)


def get_settings() -> dict:
    """取得當前設定（含快取）。"""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_disk()
        return dict(_cache)


def update_settings(
    provider: str,
    model: str,
    ollama_base_url: Optional[str] = None,
    ollama_thinking: Optional[str] = None,
    ollama_num_ctx: Optional[int] = None,
    gemini_thinking: Optional[str] = None,
    anthropic_thinking: Optional[str] = None,
    # ── 副模型(選填、空字串 = 不啟用)──
    secondary_provider: Optional[str] = None,
    secondary_model: Optional[str] = None,
    secondary_ollama_thinking: Optional[str] = None,
    secondary_ollama_num_ctx: Optional[int] = None,
    secondary_gemini_thinking: Optional[str] = None,
    secondary_anthropic_thinking: Optional[str] = None,
) -> dict:
    """更新並寫入磁碟。"""
    global _cache
    if provider not in ("groq", "ollama", "gemini", "openai", "anthropic", "claude_cli"):
        raise ValueError(f"unsupported provider: {provider}")
    if not model or not isinstance(model, str):
        raise ValueError("model is required")
    thinking = (ollama_thinking or "off").strip()
    if thinking not in ("auto", "on", "off"):
        raise ValueError(f"invalid ollama_thinking: {thinking}")
    gem_thinking = (gemini_thinking or "off").strip()
    if gem_thinking not in ("off", "auto", "low", "medium", "high"):
        raise ValueError(f"invalid gemini_thinking: {gem_thinking}")
    ant_thinking = (anthropic_thinking or "off").strip()
    if ant_thinking not in ("off", "on"):
        raise ValueError(f"invalid anthropic_thinking: {ant_thinking}")
    num_ctx = ollama_num_ctx if ollama_num_ctx is not None else _DEFAULT["ollama_num_ctx"]
    if not isinstance(num_ctx, int) or num_ctx < 2048 or num_ctx > 262144:
        raise ValueError(f"invalid ollama_num_ctx: {num_ctx}(需介於 2048~262144)")
    if provider == "ollama" and num_ctx < 32768:
        raise ValueError(f"ollama_num_ctx={num_ctx} 太小:AI 助手一輪至少 2 萬 tokens,Ollama 塞不下會靜默截掉提示詞前半。請設 32768 以上(建議 65536)")

    # ── Secondary 驗證(可全空表示停用)──
    sec_provider = (secondary_provider or "").strip()
    sec_model = (secondary_model or "").strip()
    if sec_provider and sec_provider not in ("groq", "ollama", "gemini", "openai", "anthropic", "claude_cli"):
        raise ValueError(f"unsupported secondary_provider: {sec_provider}")
    if sec_provider and not sec_model:
        raise ValueError("secondary_model is required when secondary_provider is set")
    sec_oll_thinking = (secondary_ollama_thinking or "off").strip()
    if sec_oll_thinking not in ("auto", "on", "off"):
        raise ValueError(f"invalid secondary_ollama_thinking: {sec_oll_thinking}")
    sec_gem_thinking = (secondary_gemini_thinking or "off").strip()
    if sec_gem_thinking not in ("off", "auto", "low", "medium", "high"):
        raise ValueError(f"invalid secondary_gemini_thinking: {sec_gem_thinking}")
    sec_ant_thinking = (secondary_anthropic_thinking or "off").strip()
    if sec_ant_thinking not in ("off", "on"):
        raise ValueError(f"invalid secondary_anthropic_thinking: {sec_ant_thinking}")
    sec_num_ctx = secondary_ollama_num_ctx if secondary_ollama_num_ctx is not None else _DEFAULT["secondary_ollama_num_ctx"]
    if not isinstance(sec_num_ctx, int) or sec_num_ctx < 2048 or sec_num_ctx > 262144:
        raise ValueError(f"invalid secondary_ollama_num_ctx: {sec_num_ctx}")

    with _lock:
        # 先讀取現有設定(保留通知等其他欄位)
        existing = _cache if _cache else _load_from_disk()
        existing.update({
            "provider": provider,
            "model": model.strip(),
            "ollama_base_url": (ollama_base_url or _DEFAULT["ollama_base_url"]).strip(),
            "ollama_thinking": thinking,
            "ollama_num_ctx": num_ctx,
            "gemini_thinking": gem_thinking,
            "anthropic_thinking": ant_thinking,
            "secondary_provider": sec_provider,
            "secondary_model": sec_model,
            "secondary_ollama_thinking": sec_oll_thinking,
            "secondary_ollama_num_ctx": sec_num_ctx,
            "secondary_gemini_thinking": sec_gem_thinking,
            "secondary_anthropic_thinking": sec_ant_thinking,
        })
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


def settings_signature() -> str:
    """回傳一個代表當前設定的簡易字串，用於 LLM 快取失效判斷。"""
    s = get_settings()
    return f"{s['provider']}::{s['model']}::{s['ollama_base_url']}::{s.get('ollama_thinking', 'off')}::{s.get('ollama_num_ctx', 16384)}"


def set_primary_model(provider: str, model: str) -> dict:
    """只換主模型的供應商與模型,其他欄位(副模型、thinking、num_ctx)原樣保留。
    update_settings 會把沒帶的欄位重設成預設值,首次設定精靈不能用它。"""
    global _cache
    provider = (provider or "").strip()
    if provider not in ("groq", "ollama", "gemini", "openai", "anthropic", "claude_cli"):
        raise ValueError(f"invalid provider: {provider}")
    if not (model or "").strip():
        raise ValueError("model is required")
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        existing["provider"] = provider
        existing["model"] = model.strip()
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


# ── Sandbox mode（獨立 setter，不混進 model 更新流程） ─────────────
def set_skill_sandbox_mode(mode: str) -> dict:
    """切換 skill 執行模式。mode ∈ {"host", "wsl_docker"}。"""
    global _cache
    mode = (mode or "host").strip()
    if mode not in ("host", "wsl_docker"):
        raise ValueError(f"invalid skill_sandbox_mode: {mode}")
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        existing["skill_sandbox_mode"] = mode
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


# ── computer_use 啟動時自動縮視窗 (獨立 setter) ───────────────────
def set_auto_minimize_for_computer_use(enabled: bool) -> dict:
    """切換 computer_use workflow 啟動時、自動縮小前景視窗。"""
    global _cache
    enabled = bool(enabled)
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        existing["auto_minimize_for_computer_use"] = enabled
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


# ── AI 助手記憶開關（獨立 setter） ────────────────────────────────
def set_memory_settings(enabled: Optional[bool] = None,
                        aggressive: Optional[bool] = None) -> dict:
    """切換長期記憶開關。enabled=主開關;aggressive=激進自動萃取(依賴 enabled)。
    傳 None 的欄位保持原值。"""
    global _cache
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        if enabled is not None:
            existing["memory_enabled"] = bool(enabled)
        if aggressive is not None:
            existing["memory_aggressive"] = bool(aggressive)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


# ── Skill 檔案目錄（獨立 setter） ──────────────────────────────────
def set_skills_dir(path: str) -> dict:
    """設定 Skill 檔案目錄。空字串 = 用預設 ~/.agents/skills/。"""
    global _cache
    path = (path or "").strip()
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        existing["skills_dir"] = path
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)


# ── 工作流自我修復開關（獨立 setter） ─────────────────────────────
def set_self_heal_settings(enabled: Optional[bool] = None,
                           max_attempts: Optional[int] = None) -> dict:
    """切換自我修復開關 / 次數上限。傳 None 的欄位保持原值。max_attempts 限 1~5。"""
    global _cache
    with _lock:
        existing = _cache if _cache else _load_from_disk()
        if enabled is not None:
            existing["self_heal_enabled"] = bool(enabled)
        if max_attempts is not None:
            n = int(max_attempts)
            if n < 1 or n > 5:
                raise ValueError("self_heal_max_attempts 需介於 1~5")
            existing["self_heal_max_attempts"] = n
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _cache = existing
    return dict(existing)
