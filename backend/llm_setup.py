"""AI 模型就緒檢查與首次設定(給「還沒有可用模型」的新使用者用)。

- readiness():不呼叫模型,只檢查目前設定的主模型能不能用,並偵測本機現成的替代選項
  (Ollama 有沒有在跑、有哪些模型;Claude Code 有沒有安裝與登入;.env 裡有哪些金鑰)。
- save_api_key():把使用者在介面貼上的金鑰寫進 backend/.env,並讓執行中的後端立即生效。
- use_model():只切換主模型的供應商與模型,不動其他設定。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"

KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
LABEL = {
    "gemini": "Google Gemini", "groq": "Groq", "openai": "OpenAI", "anthropic": "Anthropic",
    "ollama": "Ollama(本機)", "claude_cli": "Claude 訂閱(Claude Code)",
}
# 使用者只填金鑰、沒挑模型時用的預設模型
DEFAULT_MODEL = {
    "gemini": "gemini-3.5-flash-lite",
    "claude_cli": "sonnet",
}
# 啟動時把金鑰讀成模組變數的地方;金鑰更新後要一起改,否則要重啟才生效
_KEY_HOLDERS = ("config", "llm_factory", "pipeline.executor", "pipeline.validator")
# 只做向量檢索、不能對話的 Ollama 模型
_EMBED_HINTS = ("embed", "bge", "minilm", "nomic")


def _ollama_models(base_url: str, timeout: float = 1.5) -> tuple[bool, list[str]]:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return False, []
    names = [m.get("name", "") for m in data.get("models", [])]
    return True, [n for n in names if n and not any(h in n.lower() for h in _EMBED_HINTS)]


def _ollama_has(models: list[str], model: str) -> bool:
    m = (model or "").strip()
    return bool(m) and (m in models or f"{m}:latest" in models)


def _key(provider: str) -> str:
    v = os.getenv(KEY_ENV[provider], "").strip()
    # 範本留下的佔位字(your_xxx_here)不算有填,否則會誤判成可用、送出後才報金鑰錯誤
    if v.lower().startswith("your_") or v.lower().endswith("_here"):
        return ""
    return v


def readiness() -> dict:
    from settings import get_settings
    s = get_settings()
    provider = (s.get("provider") or "").strip()
    model = (s.get("model") or "").strip()
    base_url = s.get("ollama_base_url") or "http://localhost:11434"

    ollama_running, ollama_models = _ollama_models(base_url)
    try:
        from claude_cli_provider import availability
        cc = availability()
    except Exception:
        cc = {"installed": False, "logged_in": False}
    keys = {p: bool(_key(p)) for p in KEY_ENV}

    ready, problem = False, ""
    label = LABEL.get(provider, provider or "(未選擇)")
    if provider in KEY_ENV:
        if not keys[provider]:
            problem = f"目前選的是 {label},但還沒有填 API Key。"
        elif not model:
            problem = f"目前選的是 {label},但還沒有選模型。"
        else:
            ready = True
    elif provider == "ollama":
        if not ollama_running:
            problem = "目前選的是本機 Ollama,但連不到 Ollama(可能還沒安裝,或沒有開啟)。"
        elif not _ollama_has(ollama_models, model):
            problem = f"Ollama 裡還沒有模型 {model or '(未選擇)'},要先下載。"
        else:
            ready = True
    elif provider == "claude_cli":
        if not cc.get("installed"):
            problem = "目前選的是 Claude 訂閱,但這台電腦還沒安裝 Claude Code。"
        elif not cc.get("logged_in"):
            problem = "目前選的是 Claude 訂閱,但 Claude Code 還沒登入。"
        else:
            ready = True
    else:
        problem = "還沒有選擇 AI 模型。"

    return {
        "ready": ready,
        "provider": provider,
        "provider_label": label,
        "model": model,
        "problem": problem,
        "detected": {
            "ollama": {"running": ollama_running, "models": ollama_models},
            "claude_cli": {"installed": bool(cc.get("installed")), "logged_in": bool(cc.get("logged_in"))},
            "keys": keys,
        },
        "defaults": DEFAULT_MODEL,
    }


def verify_gemini_key(key: str, model: str) -> tuple[bool, str]:
    """向 Google 查一次模型資訊確認金鑰有效(不產生任何費用)。"""
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
        headers={"x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True, ""
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return False, "Google 回報這把 API Key 無效,請確認有完整複製(通常是 AIza 開頭)。"
        if e.code == 404:
            return False, f"API Key 有效,但找不到模型 {model}。"
        return False, f"驗證 API Key 時 Google 回應 HTTP {e.code},請稍後再試。"
    except Exception as e:
        return False, f"連不到 Google 驗證 API Key({type(e).__name__}),請檢查網路。"


def _write_env(name: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pat = re.compile(rf"^\s*#?\s*{re.escape(name)}\s*=")
    out, done = [], False
    for line in lines:
        if not done and pat.match(line):
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def save_api_key(provider: str, key: str) -> tuple[bool, str]:
    provider = (provider or "").strip()
    key = (key or "").strip()
    if provider not in KEY_ENV:
        return False, f"不支援的供應商:{provider}"
    if not key or len(key) > 300 or any(c.isspace() for c in key) or "=" in key:
        return False, "API Key 格式不正確,請重新複製貼上。"
    if provider == "gemini":
        ok, msg = verify_gemini_key(key, DEFAULT_MODEL["gemini"])
        if not ok:
            return False, msg
    name = KEY_ENV[provider]
    _write_env(name, key)
    os.environ[name] = key
    for mod_name in _KEY_HOLDERS:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, name):
            setattr(mod, name, key)
    return True, ""


def use_model(provider: str, model: str) -> dict:
    from settings import set_primary_model
    return set_primary_model(provider, model)
