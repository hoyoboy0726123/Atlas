# -*- coding: utf-8 -*-
"""集中提供 skill_llm helper 的執行環境(SKILL_LLM_* 環境變數 + helper 目錄路徑)。

host run_python(executor._skill_run_python)與 sandbox run_python(sandbox.run_python)
共用這份,避免兩邊各寫一份 provider/金鑰 對應邏輯而漂移。

可用環境變數 SKILL_LLM_HELPER=0 關閉注入(預設開)。關閉後沙盒內 `from skill_llm import llm`
會 ModuleNotFoundError / 未設定錯誤,agent 應改成自己逐段處理。
"""
import os
from pathlib import Path

# sandbox_helpers/skill_llm.py 所在目錄(已在 repo bind-mount 範圍內、容器用 /mnt/c 看得到同一份)
HELPERS_DIR_WIN = Path(__file__).resolve().parent / "sandbox_helpers"


def _enabled() -> bool:
    return (os.environ.get("SKILL_LLM_HELPER", "1").strip().lower()
            not in ("0", "false", "off", "no", ""))


def runtime_env() -> dict:
    """回 skill_llm helper 要的 SKILL_LLM_* 環境變數(不含 PYTHONPATH;PYTHONPATH 由各
    執行路徑自行用對的格式 Windows / WSL 補上)。關閉或抓不到有效設定時回 {}。

    讀「當前 settings 的 provider/model」+「config 已載入的金鑰」——不直接讀 .env。
    """
    if not _enabled():
        return {}
    try:
        from settings import get_settings
        import config as _cfg
        s = get_settings()
        provider = (s.get("provider") or "").strip().lower()
        model = (s.get("model") or "").strip()

        # ⚠️ 訂閱 CLI 大腦(claude_cli)在沙盒內用不了 —— `claude` 執行檔裝在
        # Windows host,容器/WSL 沙盒看不到,也不該把使用者的登入憑證帶進去。
        # 舊行為:照樣把 SKILL_LLM_PROVIDER=claude_cli 傳進沙盒,技能一呼叫就
        # `RuntimeError: skill_llm 不支援 provider='claude_cli'`,訊息還看不出原因。
        # 改成自動改用副模型(有設且非 CLI 才用);都不行就不注入,讓 helper
        # 回「未設定」的明確訊息。
        _CLI_ONLY = ("claude_cli",)
        if provider in _CLI_ONLY:
            sec_p = (s.get("secondary_provider") or "").strip().lower()
            sec_m = (s.get("secondary_model") or "").strip()
            if sec_p and sec_m and sec_p not in _CLI_ONLY:
                provider, model = sec_p, sec_m
            else:
                return {}   # 沒有可用的沙盒內 provider → 不注入

        if not provider or not model:
            return {}
        key = ""
        if provider in ("gemini", "google", "gemma"):
            key = getattr(_cfg, "GEMINI_API_KEY", "") or ""
        elif provider == "groq":
            key = getattr(_cfg, "GROQ_API_KEY", "") or ""
        elif provider == "openai":
            key = getattr(_cfg, "OPENAI_API_KEY", "") or ""
        elif provider == "anthropic":
            key = getattr(_cfg, "ANTHROPIC_API_KEY", "") or ""
        env = {"SKILL_LLM_PROVIDER": provider, "SKILL_LLM_MODEL": model}
        if key:
            env["SKILL_LLM_KEY"] = key
        if provider == "ollama":
            base = (s.get("ollama_base_url") or "").strip()
            if base:
                env["SKILL_LLM_BASE_URL"] = base
        return env
    except Exception:
        return {}
