"""
Atlas — 後端
啟動：uvicorn main:app --host 127.0.0.1 --port 8014
（前端 next.config.mjs 與 launch.bat / start.sh 都指向 8014,請保持一致）
"""
# Windows console 預設 cp1252/cp950 無法印 emoji / 中文 → 啟動時強制 UTF-8
# 不靠 PYTHONIOENCODING env var，避免使用者沒設或 .bat 傳遞失效
import sys as _sys
try:
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── DPI awareness（Windows）─────────────────────────────────────
# 必須在 import mss / pyautogui / ctypes 任何螢幕相關模組之前呼叫,
# 不然子模組 cache 住 DPI-unaware 的螢幕 metric、改不過來。
#
# 為什麼要做：DPI-unaware process 在高 DPI 螢幕上 Windows 會「撒謊」、
# 回邏輯(虛擬)像素 — 不同 scaling 機台之間邏輯像素不一致、跨機器搬
# workflow 會整個錯位到左上(C: 150% 邏輯 1280×720、D: 125% 邏輯
# 1536×864、同一組 (x,y) 在兩台對應的物理位置完全不同)。
#
# 設成 PROCESS_PER_MONITOR_DPI_AWARE_V2 = -4 (新 API,Win10 1703+)
# fallback：PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+)
# fallback：SetProcessDPIAware (Win Vista+)
#
# 設定後：mss.grab、pyautogui.click、GetCursorPos 全部用物理座標、
# 跨機器只要物理螢幕解析度一致就能相容。
#
# ⚠ 副作用：舊的(本修復前錄製的)workflow 座標是邏輯像素、修完
# 在同台機器上也會錯位、必須重錄一次。
if _sys.platform == "win32":
    try:
        import ctypes as _ctypes

        def _try_set_dpi_awareness() -> bool:
            """嘗試三層 fallback、回傳是否真的設成功(用 GetProcessDpiAwareness 反查)。"""
            user32 = _ctypes.windll.user32
            # 1. SetProcessDpiAwarenessContext (Win10 1703+) — 接 HANDLE(指標) 不是 int
            #    所以一定要走 c_void_p、不然 ctypes 預設 c_int 傳整數會 fail silently
            if hasattr(user32, "SetProcessDpiAwarenessContext"):
                user32.SetProcessDpiAwarenessContext.argtypes = [_ctypes.c_void_p]
                user32.SetProcessDpiAwarenessContext.restype = _ctypes.c_int
                # -4 = PER_MONITOR_AWARE_V2
                if user32.SetProcessDpiAwarenessContext(_ctypes.c_void_p(-4)):
                    return True
                # -3 = PER_MONITOR_AWARE(舊版 v1)— v2 不支援時退一步
                if user32.SetProcessDpiAwarenessContext(_ctypes.c_void_p(-3)):
                    return True
            # 2. SetProcessDpiAwareness (Win8.1+)
            try:
                shcore = _ctypes.windll.shcore
                if hasattr(shcore, "SetProcessDpiAwareness"):
                    # 2 = PROCESS_PER_MONITOR_DPI_AWARE。回傳 HRESULT,0 = S_OK
                    if shcore.SetProcessDpiAwareness(2) == 0:
                        return True
            except Exception:
                pass
            # 3. SetProcessDPIAware (Win Vista+,system-wide aware,粗糙但聊勝於無)
            try:
                if user32.SetProcessDPIAware():
                    return True
            except Exception:
                pass
            return False

        _try_set_dpi_awareness()
    except Exception:
        # 設不到不致命、只是回退到原本 DPI-unaware 行為
        pass

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import check_config
from scheduler.manager import start as sched_start, shutdown as sched_shutdown

app = FastAPI(title="Atlas", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3012", "http://127.0.0.1:3012",
                   "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    from db import init_db
    init_db()
    print("✅ SQLite 資料庫已初始化")
    # 全新安裝時把內建 skill(default_skills/)複製到使用者 skill 目錄(已存在不覆蓋)
    # → clone 後不必手動裝就能用到內建 skill(如 scraped-content-parser)
    try:
        from skill_scanner import seed_default_skills
        _ns = seed_default_skills()
        if _ns:
            print(f"✅ 已植入 {_ns} 個內建 skill 到使用者 skill 目錄")
    except Exception as _e:
        print(f"⚠ 內建 skill 植入略過:{_e}")
    # 自動安裝 skill_packages.txt 中缺少的套件
    from skill_pkg_manager import auto_install_packages
    auto_install_packages()
    # 主機系統工具健康檢查(non-fatal、只警告)
    from config import check_host_tools
    for w in check_host_tools():
        print(w)
    await sched_start()
    print("✅ Pipeline Scheduler 已啟動")
    # 檔案夾監看觸發器:背景輪詢(免 watchdog 依賴)
    global _folder_watch_task
    _folder_watch_task = asyncio.create_task(_folder_watch_poller())
    print("✅ 檔案夾監看輪詢已啟動")
    from telegram_handler import start_polling as tg_start
    await tg_start()
    print("✅ Telegram callback polling 已啟動")
    # 首次啟動預載地端向量模型(背景 thread、不卡啟動)。失敗 → 明確提示、episodic 降級關鍵字。
    from settings import get_settings as _gs_startup
    if _gs_startup().get("memory_enabled", True):
        import threading as _th

        def _warmup_mem():
            try:
                import memory as _mem
                ok = _mem.warmup_local_embedder()
                if ok:
                    print("✅ 記憶:地端向量模型已就緒(MiniLM 多語言)")
                else:
                    print("⚠️ 記憶:地端向量模型未就緒(可能首啟下載失敗 / 無網路 / 未裝 fastembed)、"
                          "episodic 暫用關鍵字檢索。修復:確認網路後重啟、或 pip install fastembed。"
                          "(provider=gemini 時用雲端 embedding、不受影響)")
            except Exception as e:
                print(f"⚠️ 記憶:地端向量模型預載例外、episodic 降級關鍵字:{type(e).__name__}: {e}")

        _th.Thread(target=_warmup_mem, daemon=True).start()


@app.on_event("shutdown")
async def shutdown():
    await sched_shutdown()
    from telegram_handler import stop_polling as tg_stop
    await tg_stop()
    if _folder_watch_task is not None:
        _folder_watch_task.cancel()


# ── Health ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    missing = check_config()
    return {"status": "ok", "warnings": [f"{k} 未設定" for k in missing]}


@app.get("/system/host-tools")
async def host_tools_status():
    """主機系統工具(非 pip)健康檢查 — 給前端設定頁顯示。

    回每個工具的 name / installed / found_at / install_cmd / why / required。
    """
    from dataclasses import asdict
    import platform
    from pipeline.host_tools import get_host_tools
    return {
        "platform": platform.system(),  # "Windows" / "Darwin" / "Linux"
        "tools": [asdict(t) for t in get_host_tools()],
    }


# ── Settings（模型選擇）─────────────────────────────────────
# 排除的 Groq 模型（非文字生成用途）
_GROQ_EXCLUDE_PREFIXES = ("whisper-", "llama-prompt-guard", "canopylabs/orpheus")

# Gemini 可用於文字生成的模型前綴（排除 embedding, tts, robotics, audio 等）
_GEMINI_TEXT_PREFIXES = ("gemini-2.5-", "gemini-2.0-", "gemini-3-", "gemini-3.", "gemma-")
_GEMINI_EXCLUDE_KEYWORDS = ("tts", "audio", "embedding", "robotics", "image", "live", "customtools", "computer-use")

# 支援思考模式的 Gemini 模型前綴
_GEMINI_THINKING_PREFIXES = ("gemini-2.5-", "gemini-3-", "gemini-3.")


# 設定回應裡的敏感欄位:不可明文外洩(開源 + 0.0.0.0 + 無 auth 下任何 LAN 裝置都讀得到)。
# /settings/model 是給「模型下拉選單」用的、前端完全不從這裡讀金鑰(notifications token 走
# /settings/notifications、tavily 走 /settings/web-search 的 has_key 模式),故這裡一律 redact 成
# has_<key> 布林旗標。實測:redact 前此端點會吐出 tavily_api_key / telegram_bot_token 明文。
_SENSITIVE_SETTING_KEYS = (
    "telegram_bot_token", "telegram_chat_id", "line_notify_token", "tavily_api_key",
)


def _redact_settings(d: dict) -> dict:
    """回傳副本:敏感欄位的明文值換成 has_<key> 旗標(true/false),不洩漏實際值。"""
    out = dict(d or {})
    for k in _SENSITIVE_SETTING_KEYS:
        if k in out:
            out["has_" + k] = bool(str(out.get(k) or "").strip())
            out.pop(k, None)
    return out


@app.get("/settings/model")
async def get_model_settings():
    from settings import get_settings
    return _redact_settings(get_settings())


class ModelSettingsRequest(BaseModel):
    provider: str
    model: str
    ollama_base_url: Optional[str] = None
    ollama_thinking: Optional[str] = None   # "auto" | "on" | "off"
    ollama_num_ctx: Optional[int] = None
    gemini_thinking: Optional[str] = None   # "off" | "auto" | "low" | "medium" | "high"
    anthropic_thinking: Optional[str] = None  # "off" | "on" — Claude Opus 4 extended thinking
    # ── 副模型(選填、空字串 = 不啟用、所有節點都走 primary)──
    secondary_provider: Optional[str] = None
    secondary_model: Optional[str] = None
    secondary_ollama_thinking: Optional[str] = None
    secondary_ollama_num_ctx: Optional[int] = None
    secondary_gemini_thinking: Optional[str] = None
    secondary_anthropic_thinking: Optional[str] = None


@app.put("/settings/model")
async def put_model_settings(req: ModelSettingsRequest):
    from settings import update_settings
    try:
        return _redact_settings(update_settings(
            req.provider, req.model, req.ollama_base_url, req.ollama_thinking, req.ollama_num_ctx,
            req.gemini_thinking, req.anthropic_thinking,
            secondary_provider=req.secondary_provider,
            secondary_model=req.secondary_model,
            secondary_ollama_thinking=req.secondary_ollama_thinking,
            secondary_ollama_num_ctx=req.secondary_ollama_num_ctx,
            secondary_gemini_thinking=req.secondary_gemini_thinking,
            secondary_anthropic_thinking=req.secondary_anthropic_thinking,
        ))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── models/available 快取：4 個外部 API 每次都打太慢，5 分鐘記憶體快取 ──
_MODELS_CACHE: dict = {"ts": 0.0, "data": None}
_MODELS_CACHE_TTL = 300.0  # 秒


@app.get("/settings/models/available")
async def get_available_models(refresh: bool = False):
    """動態列出各 provider 可用模型。有 5 分鐘快取，加 ?refresh=true 強制更新。"""
    import time as _time
    import asyncio as _asyncio
    import httpx
    from config import GROQ_API_KEY as _groq_key, GEMINI_API_KEY as _gemini_key, OPENAI_API_KEY as _oai_key, ANTHROPIC_API_KEY as _ant_key

    # 命中快取直接回，~5ms
    if not refresh and _MODELS_CACHE["data"] and (_time.time() - _MODELS_CACHE["ts"]) < _MODELS_CACHE_TTL:
        return _MODELS_CACHE["data"]

    ollama_models: list[dict] = []
    ollama_error: Optional[str] = None
    groq_models: list[dict] = []
    groq_error: Optional[str] = None
    gemini_models: list[dict] = []
    gemini_error: Optional[str] = None
    openai_models: list[dict] = []
    openai_error: Optional[str] = None
    anthropic_models: list[dict] = []
    anthropic_error: Optional[str] = None

    base_url = "http://localhost:11434"
    try:
        from settings import get_settings as _gs
        base_url = _gs().get("ollama_base_url") or base_url
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=8.0) as client:
        # ── 每個 provider 包成獨立 coroutine，用 asyncio.gather 併發 ──
        async def fetch_groq() -> tuple[list[dict], Optional[str]]:
            if not _groq_key:
                return [], "未設定 GROQ_API_KEY"
            try:
                r = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {_groq_key}"},
                )
                r.raise_for_status()
                models = []
                for m in r.json().get("data", []):
                    mid = m.get("id", "")
                    if not m.get("active", True):
                        continue
                    if any(mid.startswith(p) for p in _GROQ_EXCLUDE_PREFIXES):
                        continue
                    ctx = m.get("context_window", 0)
                    owner = m.get("owned_by", "")
                    label = mid
                    if owner:
                        label += f"（{owner}"
                        if ctx:
                            label += f", ctx={ctx // 1024}K"
                        label += "）"
                    models.append({"id": mid, "label": label})
                models.sort(key=lambda x: x["id"])
                return models, None
            except Exception as e:
                return [], f"Groq API 錯誤：{e}"

        async def fetch_gemini() -> tuple[list[dict], Optional[str]]:
            if not _gemini_key:
                return [], "未設定 GEMINI_API_KEY"
            try:
                # v1beta models 有分頁(一頁預設 50、nextPageToken 續抓)。不翻頁的話
                # 模型數超過 50 之後,新模型會默默消失在清單裡(實測 2026-07 已達 2 頁)。
                raw_models: list = []
                _page_token = ""
                for _pg in range(5):  # 安全上限 5 頁,防 token 迴圈異常
                    _url = f"https://generativelanguage.googleapis.com/v1beta/models?key={_gemini_key}"
                    if _page_token:
                        _url += f"&pageToken={_page_token}"
                    r = await client.get(_url)
                    r.raise_for_status()
                    _body = r.json()
                    raw_models.extend(_body.get("models", []))
                    _page_token = _body.get("nextPageToken") or ""
                    if not _page_token:
                        break
                models = []
                for m in raw_models:
                    mid = m.get("name", "").replace("models/", "")
                    if not any(mid.startswith(p) for p in _GEMINI_TEXT_PREFIXES):
                        continue
                    if any(kw in mid for kw in _GEMINI_EXCLUDE_KEYWORDS):
                        continue
                    display = m.get("displayName", mid)
                    supports_thinking = any(mid.startswith(p) for p in _GEMINI_THINKING_PREFIXES)
                    label = display
                    if supports_thinking:
                        label += "（支援思考）"
                    models.append({"id": mid, "label": label, "supports_thinking": supports_thinking})
                models.sort(key=lambda x: x["id"])
                return models, None
            except Exception as e:
                return [], f"Gemini API 錯誤：{e}"

        async def fetch_openai() -> tuple[list[dict], Optional[str]]:
            if not _oai_key:
                return [], "未設定 OPENAI_API_KEY"
            try:
                r = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {_oai_key}"},
                )
                r.raise_for_status()
                # OpenAI list 含很多 embedding / audio / image / fine-tune 模型、過濾出 chat 用的
                _CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
                _EXCLUDE = ("audio", "transcribe", "tts", "embed", "moderation", "realtime",
                            "instruct", "vision-preview", "search-preview")
                models = []
                for m in r.json().get("data", []):
                    mid = m.get("id", "")
                    if not any(mid.startswith(p) for p in _CHAT_PREFIXES):
                        continue
                    if any(kw in mid for kw in _EXCLUDE):
                        continue
                    models.append({"id": mid, "label": mid})
                models.sort(key=lambda x: x["id"])
                return models, None
            except Exception as e:
                return [], f"OpenAI API 錯誤：{e}"

        async def fetch_anthropic() -> tuple[list[dict], Optional[str]]:
            if not _ant_key:
                return [], "未設定 ANTHROPIC_API_KEY"
            try:
                # Anthropic /v1/models 預設一頁只回 20 個 → 帶 limit=1000 一次拿全
                r = await client.get(
                    "https://api.anthropic.com/v1/models?limit=1000",
                    headers={"x-api-key": _ant_key, "anthropic-version": "2023-06-01"},
                )
                r.raise_for_status()
                models = []
                for m in r.json().get("data", []):
                    mid = m.get("id", "")
                    display = m.get("display_name") or mid
                    label = f"{display}（{mid}）" if display != mid else mid
                    models.append({"id": mid, "label": label})
                models.sort(key=lambda x: x["id"])
                return models, None
            except Exception as e:
                return [], f"Anthropic API 錯誤：{e}"

        async def fetch_ollama() -> tuple[list[dict], Optional[str]]:
            try:
                r = await client.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
                r.raise_for_status()
                models = []
                for m in r.json().get("models", []):
                    name = m.get("name") or m.get("model")
                    if not name:
                        continue
                    size = m.get("size", 0)
                    size_gb = f"{size / 1024 / 1024 / 1024:.1f} GB" if size else ""
                    models.append({"id": name, "label": f"{name}" + (f"（{size_gb}）" if size_gb else "")})
                return models, None
            except Exception as e:
                return [], f"無法連線 Ollama（{base_url}）：{e}"

        # 五條 coroutine 一口氣併發執行，總時間 ≈ max 而不是 sum
        (groq_models, groq_error), (gemini_models, gemini_error), \
        (openai_models, openai_error), (anthropic_models, anthropic_error), \
        (ollama_models, ollama_error) = \
            await _asyncio.gather(
                fetch_groq(), fetch_gemini(),
                fetch_openai(), fetch_anthropic(),
                fetch_ollama(),
            )

    # ── Claude 訂閱 CLI(headless `claude -p`,吃 Pro/Max 訂閱額度、非 API 計費)──
    # 從 Atlas 內部走 build_llm → 吃完整系統提示注入 + app 上下文(MCP 外掛做不到)。
    claude_cli_models: list[dict] = []
    claude_cli_error: Optional[str] = None
    try:
        from claude_cli_provider import availability as _cc_avail
        _av = _cc_avail()
        if not _av["installed"]:
            claude_cli_error = "未安裝 claude CLI(npm i -g @anthropic-ai/claude-code)"
        elif not _av["logged_in"]:
            claude_cli_error = "已安裝但未登入 — 請在終端機執行 `claude` 登入 Pro/Max 帳號"
        else:
            claude_cli_models = [
                {"id": "opus", "label": "Claude Opus 訂閱（最強·headless CLI）"},
                {"id": "sonnet", "label": "Claude Sonnet 訂閱（均衡·headless CLI）"},
                {"id": "haiku", "label": "Claude Haiku 訂閱（快速·headless CLI）"},
            ]
    except Exception as e:
        claude_cli_error = f"claude CLI 偵測失敗:{e}"

    payload = {
        "groq": groq_models,
        "groq_error": groq_error,
        "gemini": gemini_models,
        "gemini_error": gemini_error,
        "openai": openai_models,
        "openai_error": openai_error,
        "anthropic": anthropic_models,
        "anthropic_error": anthropic_error,
        "ollama": ollama_models,
        "ollama_base_url": base_url,
        "ollama_error": ollama_error,
        "claude_cli": claude_cli_models,
        "claude_cli_error": claude_cli_error,
    }
    _MODELS_CACHE["ts"] = _time.time()
    _MODELS_CACHE["data"] = payload
    return payload


# ── 專案環境路徑（給前端 AI 助手生成真實可用的範例）────────────
@app.get("/env/paths")
async def get_env_paths():
    """回傳使用者目前專案的關鍵絕對路徑，讓前端範例能顯示真實可貼上執行的指令。"""
    import os as _os
    from pathlib import Path as _P
    project_root = _P(__file__).parent.parent.absolute()
    test_workflows = project_root / "test-workflows"
    finance_dir = test_workflows / "finance"
    return {
        "project_root": str(project_root),
        "test_workflows_dir": str(test_workflows) if test_workflows.is_dir() else None,
        "has_finance_example": finance_dir.is_dir() and (finance_dir / "stage1_generate_transactions.py").is_file(),
        "finance_example_dir": str(finance_dir) if finance_dir.is_dir() else None,
        "path_sep": _os.sep,
    }


# ── Node.js 環境檢測 ────────────────────────────────────────
_NODE_CACHE: dict = {"ts": 0.0, "data": None}
_NODE_CACHE_TTL = 60.0


@app.get("/settings/node-status")
async def get_node_status():
    """檢查系統是否安裝 Node.js / npm，含版本號。有 60s 快取。"""
    import time as _time
    import subprocess
    import shutil as _shutil
    if _NODE_CACHE["data"] and (_time.time() - _NODE_CACHE["ts"]) < _NODE_CACHE_TTL:
        return _NODE_CACHE["data"]

    def _probe(cmd: str) -> tuple[bool, str]:
        exe = _shutil.which(cmd)
        if not exe:
            return False, ""
        try:
            r = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=5)
            return (r.returncode == 0), (r.stdout or "").strip()
        except Exception:
            return False, ""

    node_ok, node_ver = _probe("node")
    npm_ok, npm_ver = _probe("npm")
    payload = {
        "node_installed": node_ok,
        "node_version": node_ver,
        "npm_installed": npm_ok,
        "npm_version": npm_ver,
        "install_hint": "https://nodejs.org/ 下載 LTS 版本；或執行 `winget install OpenJS.NodeJS.LTS`（Windows）",
    }
    _NODE_CACHE["ts"] = _time.time()
    _NODE_CACHE["data"] = payload
    return payload


# ── Skill Packages ──────────────────────────────────────────
@app.get("/settings/skill-packages")
async def get_skill_packages(target: str = "auto"):
    """列出 skill 套件。
    target: "auto"（跟著 skill_sandbox_mode 走）/ "host" / "sandbox"
    回傳含 `target` 欄位讓前端知道實際對象。"""
    from skill_pkg_manager import list_packages_by_target
    return list_packages_by_target(target)


class SkillPackageRequest(BaseModel):
    name: str
    target: str = "auto"


@app.post("/settings/skill-packages")
async def add_skill_package(req: SkillPackageRequest):
    from skill_pkg_manager import add_package_by_target
    ok, msg, resolved = add_package_by_target(req.name, req.target)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg, "target": resolved}


@app.delete("/settings/skill-packages/{pkg_name}")
async def remove_skill_package(pkg_name: str, target: str = "auto"):
    from skill_pkg_manager import remove_package_by_target
    ok, msg, resolved = remove_package_by_target(pkg_name, target)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg, "target": resolved}


@app.get("/settings/skill-packages/unlisted")
async def scan_unlisted_skill_packages():
    """掃 venv 中已安裝但不在 skill_packages.txt 也不在 requirements.txt 的套件。"""
    from skill_pkg_manager import scan_unlisted_packages
    return {"packages": scan_unlisted_packages()}


class AdoptPackageRequest(BaseModel):
    name: str


@app.post("/settings/skill-packages/adopt")
async def adopt_existing_package(req: AdoptPackageRequest):
    """把已安裝的套件加入 skill_packages.txt（不再重新 install）。"""
    from skill_pkg_manager import add_to_list_only
    ok, msg = add_to_list_only(req.name)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}


# ── Computer Use 錄製器 ──────────────────────────────────────
class RecordingStartRequest(BaseModel):
    session_id: str
    # 相對路徑 → 解析到專案根；絕對路徑直接用
    output_dir: str


@app.post("/computer-use/recording/start")
async def start_computer_use_recording(req: RecordingStartRequest):
    """開始錄製一個 computer_use session（鎖定單一進程）。"""
    from pipeline.recorder import start_recording
    from pathlib import Path as _P
    out_path = _P(req.output_dir).expanduser()
    if not out_path.is_absolute():
        _PROJ = _P(__file__).parent.parent.absolute()
        out_path = _PROJ / out_path
    try:
        return start_recording(session_id=req.session_id, output_dir=str(out_path))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/computer-use/recording/stop")
async def stop_computer_use_recording():
    """停止目前錄製中的 session，flush actions.json + meta.json。"""
    from pipeline.recorder import stop_recording
    return stop_recording()


@app.post("/computer-use/recording/arm-hotkey")
async def arm_recording_hotkey(req: RecordingStartRequest):
    """註冊全域熱鍵(預設 F7)按下後自動 start_recording。
    用途:讓使用者最小化瀏覽器、把焦點留在要錄製的 app、用熱鍵啟動錄製。
    """
    from pipeline.recorder import arm_start_hotkey
    from pathlib import Path as _P
    out_path = _P(req.output_dir).expanduser()
    if not out_path.is_absolute():
        _PROJ = _P(__file__).parent.parent.absolute()
        out_path = _PROJ / out_path
    try:
        return arm_start_hotkey(session_id=req.session_id, output_dir=str(out_path), key="f7")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/computer-use/recording/disarm-hotkey")
async def disarm_recording_hotkey():
    """取消已註冊的全域熱鍵(panel 關閉或開始錄製時呼叫)。"""
    from pipeline.recorder import disarm_start_hotkey
    return disarm_start_hotkey()


class DuplicateAssetsRequest(BaseModel):
    src: str   # 原始 assetsDir(相對 or 絕對)
    dest: str  # 新 assetsDir


@app.post("/canvas/duplicate-assets")
async def duplicate_canvas_assets(req: DuplicateAssetsRequest):
    """節點複製貼上時、把 computer_use 的 assets 資料夾整份複製到新路徑。
    防止兩節點共用同一個資料夾(會互覆寫)。"""
    import shutil
    from pathlib import Path as _P
    _PROJ = _P(__file__).parent.parent.absolute()

    def _resolve(p: str) -> _P:
        pp = _P(p).expanduser()
        return pp if pp.is_absolute() else (_PROJ / pp)

    src = _resolve(req.src)
    dest = _resolve(req.dest)
    if not src.exists() or not src.is_dir():
        return {"ok": False, "error": f"原始資料夾不存在:{src}", "copied_files": 0}
    if dest.exists():
        return {"ok": False, "error": f"目標已存在(避免覆寫):{dest}", "copied_files": 0}
    try:
        shutil.copytree(src, dest)
        # 算 copied file 數
        n = sum(1 for _ in dest.rglob("*") if _.is_file())
        return {"ok": True, "src": str(src), "dest": str(dest), "copied_files": n}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "copied_files": 0}


@app.get("/computer-use/recording/status")
async def get_computer_use_recording_status():
    """查詢目前錄製中 session 的即時狀態（前端 polling 用）。"""
    from pipeline.recorder import get_recording_status
    return get_recording_status()


@app.get("/computer-use/recording/load")
async def load_computer_use_recording(output_dir: str):
    """讀回已錄好的 session（actions + meta），供前端編輯器載入。"""
    from pipeline.recorder import load_recording
    from pathlib import Path as _P
    out_path = _P(output_dir).expanduser()
    if not out_path.is_absolute():
        _PROJ = _P(__file__).parent.parent.absolute()
        out_path = _PROJ / out_path
    result = load_recording(str(out_path))
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/computer-use/grounding/status")
async def grounding_status(force: bool = False):
    """這台機器能不能用 vlm_mode='grounding'，不能的話缺什麼。有 60s 快取。

    前端拿它決定「直接定位」按鈕是亮的還是停用 —— 停用時要說得出原因，
    不能讓使用者選了才發現不能用。
    """
    from pipeline.vlm_grounding import capability
    return await asyncio.get_event_loop().run_in_executor(None, capability, force)


class GroundingDescribeRequest(BaseModel):
    assets_dir: str
    action: dict          # 該步驟的 action dict（要有 image、最好也有 full_image）


@app.post("/computer-use/grounding/describe")
async def grounding_describe(req: GroundingDescribeRequest):
    """替 vlm_mode='grounding' 產生「要點什麼」的描述草稿（使用者按 🪄 才呼叫）。

    刻意做成手動觸發而非錄製時自動跑：每次都要一次雲端視覺呼叫，
    一段 20 次點擊的錄製全自動跑就是 20 次 API 費用，多數步驟根本用不到。
    """
    from pipeline.computer_use import describe_for_grounding, verify_grounding_desc
    assets = _validate_assets_path(req.assets_dir)
    if not assets.is_dir():
        raise HTTPException(status_code=404, detail=f"找不到 assets 目錄：{assets}")
    loop = asyncio.get_event_loop()
    ok, text, err = await loop.run_in_executor(
        None, describe_for_grounding, assets, req.action)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    # 產生完立刻自我驗證：把描述餵回地端定位模型，看能不能回到錄製的點擊位置。
    # 講錯的描述讀起來一樣通順，使用者不會逐字比對畫面 —— 所以由系統當場抓出來。
    v_ok, dist, v_msg = await loop.run_in_executor(
        None, verify_grounding_desc, assets, req.action, text)
    return {"description": text, "verified": v_ok,
            "verify_px": None if dist < 0 else round(dist, 1), "verify_msg": v_msg}


def _validate_assets_path(path_str: str) -> "Path":
    """把 assets 相關路徑解析成絕對 Path，並強制限制在 ai_output/ 內（安全防呆）。"""
    from pathlib import Path as _P
    _PROJ = _P(__file__).parent.parent.absolute()
    _ALLOWED_PREFIXES = [
        (_PROJ / "ai_output").resolve(),
        (_PROJ / "backend" / "ai_output").resolve(),
    ]
    target = _P(path_str).expanduser()
    if not target.is_absolute():
        target = _PROJ / target
    try:
        target_resolved = target.resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"路徑解析失敗：{e}")
    is_allowed = any(
        str(target_resolved).startswith(str(pfx) + os.sep) or str(target_resolved) == str(pfx)
        for pfx in _ALLOWED_PREFIXES
    )
    if not is_allowed:
        raise HTTPException(status_code=403,
            detail=f"拒絕存取：路徑不在允許範圍內（只能動 ai_output/ 下的檔案）。")
    return target_resolved


class AnchorAnalyzeRequest(BaseModel):
    assets_dir: str
    actions: list   # 整個步驟的動作序列（只有帶 image 的會被分析）
    # 步驟層級的 CV 設定。分析必須用「執行時真的會用的那組值」，
    # 否則會報出一堆執行時根本搆不到的假警報。
    cv_search_radius: int = 400
    cv_threshold: float = 0.5
    cv_search_only_near: bool = False


@app.post("/computer-use/assets/analyze-anchors")
async def analyze_anchors(req: AnchorAnalyzeRequest):
    """算每張錨點在錄製畫面上有幾個替身，而且**執行時真的搆得到、搶得走**。

    錄製完自動跑一次；前端也會在 CV 設定變動時重算（不然警告會說謊）。
    只有真的有風險才回報 —— 早期版本掃整張圖就報警，結果報一堆碰不到的
    位置，反而害使用者去改不該改的設定。
    """
    from pipeline.computer_use import analyze_anchor_uniqueness

    assets = _validate_assets_path(req.assets_dir)
    if not assets.is_dir():
        raise HTTPException(status_code=404, detail=f"找不到 assets 目錄：{assets}")

    def _run():
        out = []
        for i, a in enumerate(req.actions or []):
            if not isinstance(a, dict) or not (a.get("image") or "").strip():
                out.append({"index": i, "checked": False, "reason": "非錨點動作"})
                continue
            r = analyze_anchor_uniqueness(
                assets, a,
                cv_search_radius=req.cv_search_radius,
                cv_threshold=float(a.get("confidence") or req.cv_threshold),
                cv_search_only_near=bool(a.get("cv_search_only_near",
                                               req.cv_search_only_near)),
            )
            r["index"] = i
            out.append(r)
        return out

    # 每張圖一次全螢幕 matchTemplate，20 個動作約 1~2 秒 —— 丟 executor 別卡 event loop
    loop = asyncio.get_event_loop()
    return {"results": await loop.run_in_executor(None, _run)}


@app.get("/computer-use/assets/list")
async def list_assets(dir: str):
    """列出 assets_dir 內的 PNG 錨點檔。給「VLM 挑錨點」的檔案選擇器用 —
    使用者錄完動作後，這個目錄會有 img_NNN.png（自動截）跟 img_NNN_manual.png
    （手動圈），這兩種都是合法錨點；full_NNN.png 是全螢幕截圖（給編輯器顯示
    用），不是錨點，過濾掉。"""
    target_dir = _validate_assets_path(dir)
    if not target_dir.is_dir():
        return {"dir": str(target_dir), "files": []}
    files = []
    for p in sorted(target_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        if p.name.startswith("full_"):
            continue   # 全螢幕截圖不是錨點
        try:
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
        except OSError:
            continue
    return {"dir": str(target_dir), "files": files}


@app.get("/computer-use/assets/image")
async def get_assets_image(dir: str, name: str):
    """提供單一錨點/全螢幕 PNG 檔供前端顯示（Modal 編輯錨點時用）。
    Query：dir=assets 資料夾（相對或絕對）、name=檔名"""
    from fastapi.responses import FileResponse
    target_dir = _validate_assets_path(dir)
    target_file = target_dir / name
    # 二次防呆：確保 file 也在 target_dir 內（防 name 含 ..）
    try:
        rf = target_file.resolve()
        if not str(rf).startswith(str(target_dir) + os.sep):
            raise HTTPException(status_code=403, detail="檔名不合法")
    except Exception:
        raise HTTPException(status_code=403, detail="檔名不合法")
    if not target_file.is_file():
        raise HTTPException(status_code=404, detail=f"檔案不存在：{name}")
    return FileResponse(str(target_file), media_type="image/png")


class CropRequest(BaseModel):
    dir: str                # assets 資料夾
    full_image: str         # 來源全螢幕截圖檔名（full_NNN.png）
    click_x: int            # 點擊的虛擬桌面絕對座標 X
    click_y: int            # 點擊的虛擬桌面絕對座標 Y
    full_left: int = 0      # 全螢幕截圖對應的虛擬桌面原點 X（可能是負值）
    full_top: int = 0       # 全螢幕截圖對應的虛擬桌面原點 Y
    # 使用者選的裁切區域（虛擬桌面絕對座標系）
    crop_left: int
    crop_top: int
    crop_width: int
    crop_height: int
    save_as: str            # 輸出檔名（例如 img_003_manual.png）


@app.get("/screen/snapshot")
async def get_screen_snapshot():
    """即時抓「整個虛擬桌面」一張 PNG，回 base64。視覺驗證節點的「螢幕區域拉選器」用。

    回傳：
      origin_x / origin_y：虛擬桌面左上角的絕對座標（多螢幕配置可能是負值）
      width / height：截圖像素尺寸
      image_b64：PNG base64（前端直接塞進 <img src="data:image/png;base64,..."/>）

    座標系跟 computer_use 一致：使用者拉出的矩形 [l, t, w, h] 都用「虛擬桌面絕對座標」。"""
    try:
        import base64
        import mss as _mss
        from mss.tools import to_png as _to_png
        with _mss.mss() as sct:
            mon = sct.monitors[0]   # 虛擬桌面全景（含所有實體螢幕聯集）
            shot = sct.grab(mon)
            # to_png(data, size, output=None) → 直接回 PNG bytes（output=path 才寫檔）
            png_bytes = _to_png(shot.rgb, shot.size)
        return {
            "origin_x": int(mon["left"]),
            "origin_y": int(mon["top"]),
            "width": int(mon["width"]),
            "height": int(mon["height"]),
            "image_b64": base64.b64encode(png_bytes).decode(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"螢幕擷取失敗：{e}")


@app.get("/computer-use/monitors")
async def get_computer_use_monitors():
    """列出實體螢幕的幾何（虛擬桌面絕對座標）。
    前端錨點編輯器用這個做「只看單螢幕」的切換 — 多螢幕時整張 full_*.png 被 fit 到
    viewport 會變很小，切單螢幕後畫面可以放大到看清楚。
    回傳 monitors[0] 為虛擬桌面全景、monitors[1..N] 為每台實體螢幕。"""
    try:
        import mss as _mss
        with _mss.mss() as sct:
            monitors = [
                {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
                for m in sct.monitors
            ]
        return {"monitors": monitors}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取 monitor 清單失敗：{e}")


@app.post("/computer-use/assets/crop")
async def crop_anchor_from_full(req: CropRequest):
    """從全螢幕截圖裁出新錨點。
    - 回傳新錨點檔名 + anchor_off_x/y（點擊相對新錨點中心的偏移）+ variance
    - 支援多螢幕負座標（full_left/top 可以是負的）"""
    import cv2
    import numpy as np
    target_dir = _validate_assets_path(req.dir)
    full_path = target_dir / req.full_image
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"全螢幕截圖不存在：{req.full_image}")

    # 讀 full 圖（支援中文路徑 → 走 read_bytes + imdecode）
    try:
        buf = np.frombuffer(full_path.read_bytes(), dtype=np.uint8)
        full_img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取全螢幕截圖失敗：{e}")
    if full_img is None:
        raise HTTPException(status_code=500, detail=f"全螢幕截圖解碼失敗：{req.full_image}")

    H, W = full_img.shape[:2]
    # 絕對座標 → full 圖的相對座標
    rel_left = req.crop_left - req.full_left
    rel_top = req.crop_top - req.full_top
    rel_right = rel_left + req.crop_width
    rel_bottom = rel_top + req.crop_height
    # 邊界 clamp
    rel_left = max(0, min(rel_left, W))
    rel_top = max(0, min(rel_top, H))
    rel_right = max(0, min(rel_right, W))
    rel_bottom = max(0, min(rel_bottom, H))
    if rel_right - rel_left < 20 or rel_bottom - rel_top < 20:
        raise HTTPException(status_code=400,
            detail=f"裁切範圍太小（{rel_right-rel_left}×{rel_bottom-rel_top}，最小 20×20）")

    cropped = full_img[rel_top:rel_bottom, rel_left:rel_right]
    # 點擊位置相對裁切圖的偏移（依絕對座標計算）
    actual_crop_abs_left = rel_left + req.full_left
    actual_crop_abs_top = rel_top + req.full_top
    actual_w = rel_right - rel_left
    actual_h = rel_bottom - rel_top
    click_dx = req.click_x - actual_crop_abs_left
    click_dy = req.click_y - actual_crop_abs_top
    anchor_off_x = click_dx - actual_w // 2
    anchor_off_y = click_dy - actual_h // 2

    # 特徵豐富度（variance）
    try:
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        variance = float(np.var(gray))
    except Exception:
        variance = 0.0

    # 存檔
    save_name = req.save_as
    if not save_name.endswith(".png"):
        save_name += ".png"
    out_path = target_dir / save_name
    try:
        ok, enc = cv2.imencode(".png", cropped)
        if not ok:
            raise HTTPException(status_code=500, detail="imencode 失敗")
        out_path.write_bytes(enc.tobytes())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫檔失敗：{e}")

    return {
        "image": save_name,
        "anchor_off_x": anchor_off_x,
        "anchor_off_y": anchor_off_y,
        "width": actual_w,
        "height": actual_h,
        "variance": round(variance, 1),
    }


class SavePngRequest(BaseModel):
    """前端裁切完直接送 base64 PNG 存到 assets_dir、給 VLM 錨點立即截圖用。
    跟 crop_anchor_from_full 不同:那個要先有 full_image 在磁碟、這個直接收 base64。"""
    dir: str                 # assets 資料夾
    name: str                # 檔名(可不含 .png、會自動補)
    png_b64: str             # 純 PNG base64(不含 data: prefix)


@app.post("/computer-use/assets/save-png")
async def save_png_to_assets(req: SavePngRequest):
    """把前端 canvas.toBlob() 出來的 PNG bytes 存進 assets_dir。
    用途:VLM 錨點立即截圖功能 — 使用者按下截圖、瀏覽器內裁切、再回傳裁好的圖。
    跟 crop_anchor_from_full 互補(那個吃磁碟上的 full_image、這個吃 base64)。
    """
    import base64
    target_dir = _validate_assets_path(req.dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    save_name = req.name.strip()
    if not save_name:
        raise HTTPException(status_code=400, detail="name 為空")
    # 過濾路徑符號(防 .. / 等跳出資料夾)
    if "/" in save_name or "\\" in save_name or save_name.startswith("."):
        raise HTTPException(status_code=400, detail=f"name 含非法字元:{save_name!r}")
    if not save_name.lower().endswith(".png"):
        save_name += ".png"

    try:
        # 容忍前端有沒帶 data:image/png;base64, prefix
        b64 = req.png_b64.split(",", 1)[-1] if "," in req.png_b64 else req.png_b64
        data = base64.b64decode(b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"base64 解碼失敗:{e}")

    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise HTTPException(status_code=400, detail="不是有效 PNG bytes")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"PNG 太大({len(data)} bytes、上限 5MB)")

    out_path = target_dir / save_name
    try:
        out_path.write_bytes(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫檔失敗:{e}")

    # 跟 crop 同步回 metadata、讓前端 UI 顯示一致
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            variance = float(np.var(gray))
        else:
            h, w, variance = 0, 0, 0.0
    except Exception:
        h, w, variance = 0, 0, 0.0

    return {
        "image": save_name,
        "width": w,
        "height": h,
        "variance": round(variance, 1),
        "size_bytes": len(data),
    }


class OcrProbeRequest(BaseModel):
    """對「當下螢幕」試抓某個標籤旁邊的值。給前端設定 ocr_get_text 時預覽用。"""
    label: str
    direction: str = "right"      # right 同列右側 / below 表格欄位
    kind: str = "amount"          # amount 金額 / ident 單號統編 / any 任何含數字
    region: Optional[list] = None # [left, top, width, height] 絕對桌面座標,省略=全螢幕
    max_gap: int = 600


@app.post("/computer-use/ocr/probe")
async def ocr_probe(req: OcrProbeRequest):
    """立刻對螢幕做一次 OCR、回報會抓到什麼。

    找不到時回傳畫面上「相近的文字」候選 —— 只說「找不到」使用者不知道怎麼改,
    看到實際 OCR 讀成什麼(例:總計金額被讀成 總計金额)才改得動。
    """
    import asyncio as _aio

    def _work():
        try:
            from pipeline.computer_use import _capture_screen, _parse_search_region
            from pipeline.ocr import _recognize, _normalize_cjk
            from pipeline.ocr_file import (read_field, find_label,
                                           AMOUNT_RE, IDENT_RE, TAXID_RE)
        except Exception as e:
            return {"ok": False, "error": f"載入 OCR 模組失敗:{e}"}

        try:
            screen, sx, sy = _capture_screen()
        except Exception as e:
            return {"ok": False, "error": f"截圖失敗:{e}"}

        ox, oy = sx, sy
        if req.region and len(req.region) == 4:
            l, t_, w_, h_ = [int(v) for v in req.region]
            rl, rt = max(0, l - sx), max(0, t_ - sy)
            screen = screen[rt:rt + h_, rl:rl + w_]
            ox, oy = sx + rl, sy + rt
            if getattr(screen, "size", 0) == 0:
                return {"ok": False, "error": "指定範圍超出螢幕、裁出空白影像"}

        try:
            words = _aio.run(_recognize(screen, "zh-Hant-TW"))
        except RuntimeError as e:
            if "running event loop" not in str(e).lower():
                return {"ok": False, "error": f"OCR 失敗:{e}"}
            lp = _aio.new_event_loop()
            try:
                words = lp.run_until_complete(_recognize(screen, "zh-Hant-TW"))
            finally:
                lp.close()
        except Exception as e:
            return {"ok": False, "error": f"OCR 失敗:{e}"}

        for w in words:
            w["x"] += ox
            w["y"] += oy

        vre = {"amount": AMOUNT_RE, "ident": IDENT_RE,
               "taxid": TAXID_RE}.get((req.kind or "amount").lower())
        hit = read_field(words, req.label, direction=req.direction,
                         value_re=vre, max_gap=req.max_gap)
        out = {"ok": True, "word_count": len(words)}
        if hit:
            out.update(found=True, value=hit["value"],
                       label_read_as=hit["label_text"], label_score=hit["label_score"],
                       direction=hit["direction"], box=list(hit["value_box"]))
            return out

        out["found"] = False
        # 標籤有沒有找到?分開報 —— 「標籤找不到」跟「標籤找到但旁邊沒有符合格式的值」
        # 是兩種完全不同的問題,修法也不同(前者改標籤字、後者改方向或格式)
        lab = find_label(words, req.label)
        if lab:
            lw, score = lab
            out["label_found"] = True
            out["label_read_as"] = lw.get("text", "")
            out["label_score"] = round(score, 2)
            out["reason"] = (f"標籤找到了（讀成「{lw.get('text','')}」），"
                             f"但{'右側' if req.direction == 'right' else '下方'}"
                             f"找不到符合「{req.kind}」格式的值 —— 試試換方向或改格式")
        else:
            out["label_found"] = False
            tgt = _normalize_cjk(req.label)
            scored = []
            for w in words:
                t = (w.get("text") or "").strip()
                n = _normalize_cjk(t)
                if not n or not t:
                    continue
                sim = sum(1 for c in tgt if c in n) / max(1, len(tgt))
                scored.append((sim, t))
            scored.sort(key=lambda s: -s[0])
            cands = [t for s, t in scored if s > 0][:8]
            if not cands:
                # 一個字都對不上時,列「看起來像標籤的文字」(非純數字)——
                # 空清單對使用者毫無幫助,至少讓他知道畫面上讀到什麼、可以直接點選
                import re as _re
                cands = [t for _, t in scored
                         if not _re.fullmatch(r"[\d,.\-/:$ ]+", t)][:8]
                out["reason"] = ("畫面上找不到這個標籤，也沒有相近的字。"
                                 "下面是 OCR 在畫面上讀到的文字 —— 點一下可直接帶入")
            else:
                out["reason"] = ("畫面上找不到這個標籤。下面是 OCR 實際讀到的相近文字 —— "
                                 "OCR 可能把繁體讀成簡體或漏字，照它讀到的填才對得上")
            out["candidates"] = cands
        return out

    return await _aio.get_event_loop().run_in_executor(None, _work)


class OcrFileRequest(BaseModel):
    """對圖檔 / PDF 做 OCR。不經螢幕 —— 不必先開檔、不受解析度與遮擋影響。"""
    path: str                              # 圖檔或 PDF 的絕對路徑
    fields: Optional[dict] = None          # {欄位名: 標籤} 或 {欄位名: {label, direction, kind}}
    lang_tag: Optional[str] = "zh-Hant-TW"
    words: bool = False                    # True = 連同所有詞與座標一起回(除錯用)


@app.post("/ocr/file")
async def ocr_file_api(req: OcrFileRequest):
    """檔案 OCR:抓「某個標籤旁邊的值」(發票 / 憑證 / 單據)。

    fields 的 kind:
      amount(預設)= 只收金額格式;ident = 單號;
      **taxid = 統一編號,會用檢查碼驗證** —— 這是唯一能證偽的型別,
      OCR 讀錯一位數就算不過、會自動跳過去找下一個候選;any = 只要含數字
    抓不到的欄位回 null —— 不猜、不亂填。金額抓錯比抓不到嚴重得多。
    """
    from pipeline.ocr_file import (ocr_file, read_field, to_number,
                                   AMOUNT_RE, IDENT_RE, TAXID_RE)
    import asyncio as _aio

    def _work():
        p = Path(req.path)
        if not p.exists():
            return {"ok": False, "error": f"檔案不存在:{req.path}"}
        try:
            words = ocr_file(p, req.lang_tag)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        out: dict = {"ok": True, "path": str(p), "word_count": len(words)}
        if req.fields:
            vals, detail = {}, {}
            for key, cfg in (req.fields or {}).items():
                if isinstance(cfg, str):
                    cfg = {"label": cfg}
                kind = (cfg.get("kind") or "amount").lower()
                vre = {"amount": AMOUNT_RE, "ident": IDENT_RE,
                   "taxid": TAXID_RE}.get(kind)  # any → None
                r = read_field(words, cfg.get("label", key),
                               direction=cfg.get("direction", "right"),
                               value_re=vre,
                               max_gap=int(cfg.get("max_gap", 600)))
                vals[key] = r["value"] if r else None
                if r:
                    detail[key] = {"label_read_as": r["label_text"],
                                   "label_score": r["label_score"],
                                   "page": r["page"], "direction": r["direction"]}
                    if kind == "amount":
                        vals[key + "_num"] = to_number(r["value"])
            out["fields"] = vals
            out["detail"] = detail
        if req.words:
            out["words"] = words
        return out

    return await _aio.get_event_loop().run_in_executor(None, _work)


class UiaInspectRequest(BaseModel):
    """檢視指定視窗(或 foreground)的 UIA element tree。"""
    window: str = ""             # 視窗 title pattern(支援 wildcard *)、空字串 = 當前 foreground
    max_depth: int = 6           # tree 深度上限(避免某些 app 上千層)
    max_children_per_node: int = 50  # 每節點子元素上限(避免大表格 1 萬列展開)
    max_nodes: int = 8000        # 總節點預算:深度放寬後防超大頁面卡死


@app.post("/computer-use/uia/inspect")
async def uia_inspect(req: UiaInspectRequest):
    """檢視 UIA element tree、給 frontend tree picker 用。
    """
    from pipeline.uia_executor import inspect_window
    import logging as _log
    result = inspect_window(
        window_pattern=req.window,
        max_depth=req.max_depth,
        max_children_per_node=req.max_children_per_node,
        max_nodes=req.max_nodes,
        logger=_log.getLogger("uia_inspect"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "inspect 失敗"))
    return result


class UiaHighlightRequest(BaseModel):
    """在桌面對應位置畫紅框 outline、給 inspector hover 看清楚對應實體 control 用。
    清除用 ttl_ms=0 或呼 /computer-use/uia/highlight/clear。"""
    x: int                       # 螢幕絕對 X(虛擬桌面座標、可負)
    y: int                       # 螢幕絕對 Y
    width: int                   # 邊框寬
    height: int                  # 邊框高
    ttl_ms: int = 1500           # 自動消失時間;0 = 立即清掉


@app.post("/computer-use/uia/highlight")
async def uia_highlight(req: UiaHighlightRequest):
    """在桌面 (x, y, w, h) 位置畫紅色 outline。
    透明 topmost、click 穿透不擋滑鼠。"""
    from pipeline.cu_highlight_overlay import highlight, clear_highlight
    if req.ttl_ms <= 0:
        clear_highlight()
    else:
        highlight(req.x, req.y, req.width, req.height, req.ttl_ms)
    return {"ok": True}


@app.post("/computer-use/uia/highlight/clear")
async def uia_highlight_clear():
    from pipeline.cu_highlight_overlay import clear_highlight
    clear_highlight()
    return {"ok": True}


@app.post("/computer-use/uia/picker/start")
async def uia_picker_start():
    """啟動 Live Picker:滑鼠 hover 桌面 → UIA 元素跟隨紅框 + F8 確認 / F9 取消。"""
    from pipeline.uia_picker import get_picker
    p = get_picker()
    started = p.start()
    return {"ok": True, "started": started, "running": p.is_running}


@app.get("/computer-use/uia/picker/poll")
async def uia_picker_poll():
    """frontend 輪詢:當下 hover element + 是否 confirmed。"""
    from pipeline.uia_picker import get_picker
    return get_picker().poll()


@app.post("/computer-use/uia/picker/consume")
async def uia_picker_consume():
    """拿完 confirmed 後 reset、避免重複處理。"""
    from pipeline.uia_picker import get_picker
    el = get_picker().consume_confirmed()
    return {"ok": True, "element": el}


@app.post("/computer-use/uia/picker/stop")
async def uia_picker_stop():
    from pipeline.uia_picker import get_picker
    was_running = get_picker().stop()
    return {"ok": True, "was_running": was_running}


@app.post("/computer-use/uia/picker/confirm")
async def uia_picker_confirm():
    """frontend 按鈕「確認當前 hover」走這個、不靠 F8 hotkey。"""
    from pipeline.uia_picker import get_picker
    p = get_picker()
    el = p.confirm_current()
    if not el:
        return {"ok": False, "error": "目前沒 hover 任何元素、移動滑鼠到目標再確認"}
    return {"ok": True, "element": el}


@app.get("/computer-use/uia/windows")
async def uia_list_windows():
    """列當下所有可見的 top-level 視窗、給 frontend 「📋 列出視窗」選單用。

    用 uiautomation 為主(File Explorer / TeamsWebView 等 shell-hosted window
    EnumWindows 抓不到)、win32 EnumWindows 為輔(catch cloaked / 邊角 cases)。
    去重 by name+class、合併兩路結果。
    """
    try:
        import uiautomation as auto
    except ImportError:
        raise HTTPException(status_code=500, detail="uiautomation 未安裝")

    seen: set[tuple[str, str]] = set()
    windows: list[dict] = []

    # Pass 1: uiautomation(看 shell-hosted / 看標準 GUI app)
    try:
        root = auto.GetRootControl()
        for w in root.GetChildren():
            try:
                name = str(w.Name or "").strip()
                cls = str(getattr(w, "ClassName", "") or "")
                rect = w.BoundingRectangle
                rw = int(rect.right - rect.left)
                rh = int(rect.bottom - rect.top)
                if not name and (rw == 0 or rh == 0):
                    continue
                if not name:
                    name = f"(無標題 {cls})"
                key = (name, cls)
                if key in seen:
                    continue
                seen.add(key)
                windows.append({
                    "name": name,
                    "class": cls,
                    "rect": [int(rect.left), int(rect.top), rw, rh],
                    "is_offscreen": bool(getattr(w, "IsOffscreen", False)),
                })
            except Exception:
                continue
    except Exception:
        pass

    # Pass 2: EnumWindows(補 cloaked / hidden ApplicationFrameWindow = UWP App)
    # ⚠ 純 ctypes、不用 pywin32：pywin32 從未進 requirements.txt，另一台機器
    #   乾淨安裝時這整段被外層 except 靜默跳過 —— 清單看起來正常、只是
    #   永遠少了 UWP 視窗，沒有任何錯誤訊息。這段本來就在用 ctypes 呼叫
    #   dwmapi，user32 一起用 ctypes 叫即可，不需要多 30MB 相依。
    try:
        import ctypes
        from ctypes import wintypes

        _user32 = ctypes.windll.user32

        def _win_text(hwnd, fn_len, fn_get) -> str:
            n = fn_len(hwnd)
            if n <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(n + 1)
            fn_get(hwnd, buf, n + 1)
            return buf.value or ""

        def _enum_cb(hwnd, _ignored):
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                title = _win_text(hwnd, _user32.GetWindowTextLengthW,
                                  _user32.GetWindowTextW)
                cls_buf = ctypes.create_unicode_buffer(256)
                _user32.GetClassNameW(hwnd, cls_buf, 256)
                cls = cls_buf.value or ""
                r = wintypes.RECT()
                if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    return True
                w = r.right - r.left
                h = r.bottom - r.top
                if w < 50 or h < 50:
                    return True
                # 系統殼有名字才補(避免一堆 noise)
                if not title:
                    return True
                key = (title, cls)
                if key in seen:
                    return True
                seen.add(key)
                # 偵測 cloaked
                is_cloaked = False
                try:
                    DWMWA_CLOAKED = 14
                    cloaked = ctypes.c_int(0)
                    res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                    )
                    is_cloaked = (res == 0 and cloaked.value != 0)
                except Exception:
                    pass
                windows.append({
                    "name": title, "class": cls,
                    "rect": [r.left, r.top, w, h],
                    "is_offscreen": is_cloaked,
                })
            except Exception:
                pass
            return True

        _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        _user32.EnumWindows(_WNDENUMPROC(_enum_cb), 0)
    except Exception as _e:
        # 失敗要留痕跡 —— 原本的靜默 pass 讓「少了 UWP 視窗」完全查不到原因
        import logging as _logging
        _logging.getLogger(__name__).warning(f"EnumWindows Pass 2 失敗:{_e}")

    # 排序:非 cloaked + 有真正 title 優先
    windows.sort(key=lambda x: (
        x["is_offscreen"],
        x["name"].startswith("(無標題"),
        -len(x["name"]),
    ))
    return {"ok": True, "windows": windows[:80]}


@app.delete("/computer-use/assets")
async def delete_computer_use_assets(dir: str):
    """刪除指定的錨點資料夾（含 PNG、actions.json、meta.json）。
    用於：Panel 清除全部、刪除節點時的清理。
    安全限制：只允許刪除專案根目錄下 ai_output/ 或 backend/ai_output/ 內的路徑，
    避免誤刪系統檔案。"""
    import shutil
    from pathlib import Path as _P
    _PROJ = _P(__file__).parent.parent.absolute()
    _ALLOWED_PREFIXES = [
        (_PROJ / "ai_output").resolve(),
        (_PROJ / "backend" / "ai_output").resolve(),
    ]
    target = _P(dir).expanduser()
    if not target.is_absolute():
        target = _PROJ / target
    try:
        target_resolved = target.resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"路徑解析失敗：{e}")
    # 必須在允許的資料夾內
    is_allowed = any(
        str(target_resolved).startswith(str(pfx) + os.sep) or str(target_resolved) == str(pfx)
        for pfx in _ALLOWED_PREFIXES
    )
    if not is_allowed:
        raise HTTPException(status_code=403,
            detail=f"拒絕刪除：路徑不在允許範圍內（只能刪 ai_output/ 下的子資料夾）。"
                   f"target={target_resolved}")
    if not target_resolved.exists():
        return {"deleted": False, "reason": "資料夾不存在", "path": str(target_resolved)}
    if not target_resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"路徑不是資料夾：{target_resolved}")
    try:
        shutil.rmtree(target_resolved)
        return {"deleted": True, "path": str(target_resolved)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除失敗：{e}")


# ── Claude Code Skills（從 ~/.agents/skills/ 掃描）──────────
@app.get("/skills/available")
async def get_available_skills():
    """列出使用者安裝的 Claude Code skills（掃 ~/.agents/skills/）。"""
    from skill_scanner import list_available_skills, get_skills_root
    _root = get_skills_root()
    return {
        "skills_root": str(_root),
        "exists": _root.exists(),
        "skills": list_available_skills(),
    }


# ── Subagent role CRUD ────────────────────────────────────────────────
# 內建 32 個 role 永遠在、不可改不可刪。自訂 role 寫到 ~/ai_output/custom_subagent_roles.yaml,
# 跟內建 merge 出最終可用 role 清單。前端設定頁 / AI 助手 create_subagent_role 工具都走這幾個 endpoint。

class SubagentRolePayload(BaseModel):
    role_id: str          # 英文 snake_case、不可跟內建撞名
    label: str            # 中文顯示名(畫布看的)
    description: str      # 一句話用途(下拉提示)
    tools: list[str]      # 從 SELECTABLE_TOOLS 挑、done 會自動加
    system_prompt: str    # role 看到的第一條指令


def _validate_role_payload(p: SubagentRolePayload, *, allow_existing: bool = False) -> Optional[str]:
    """回 error message string、None = OK。"""
    import re as _re
    from pipeline.subagent_runner import BUILTIN_ROLE_IDS, SELECTABLE_TOOLS, load_custom_roles
    if not p.role_id:
        return "role_id 不能空"
    if not _re.match(r"^[a-z][a-z0-9_]{1,39}$", p.role_id):
        return "role_id 必須英文 snake_case (小寫開頭、長 2-40、只能含 a-z 0-9 _)"
    if p.role_id in BUILTIN_ROLE_IDS:
        return f"role_id '{p.role_id}' 是內建角色名、不可使用"
    if not allow_existing and p.role_id in load_custom_roles():
        return f"role_id '{p.role_id}' 已存在自訂角色;要改用 PUT /subagent/roles/{p.role_id}"
    if not p.label or not p.label.strip():
        return "label(中文顯示名)不能空"
    if not p.description or not p.description.strip():
        return "description(一句話用途)不能空"
    if not isinstance(p.tools, list):
        return "tools 必須是 list"
    _bad = [t for t in p.tools if t not in SELECTABLE_TOOLS]
    if _bad:
        return f"tools 含未知工具 {_bad};可選:{SELECTABLE_TOOLS}"
    if not p.system_prompt or len(p.system_prompt.strip()) < 30:
        return "system_prompt 太短(至少 30 字)、要寫清楚角色職能 + 工作流"
    return None


@app.get("/subagent/roles")
async def list_subagent_roles():
    """列所有 role(內建 + 自訂)、含 source 標籤跟 selectable tools 清單。"""
    from pipeline.subagent_runner import (
        load_roles, load_custom_roles, BUILTIN_ROLE_IDS, SELECTABLE_TOOLS,
    )
    all_roles = load_roles()
    custom_ids = set(load_custom_roles().keys())
    out = []
    for rid, cfg in all_roles.items():
        out.append({
            "role_id": rid,
            "label": cfg.get("label") or cfg.get("description", rid),
            "description": cfg.get("description", ""),
            "tools": list(cfg.get("tools", [])),
            "system_prompt": cfg.get("system_prompt", ""),
            "source": "custom" if rid in custom_ids else "builtin",
            "is_builtin": rid in BUILTIN_ROLE_IDS,
        })
    out.sort(key=lambda r: (0 if r["is_builtin"] else 1, r["role_id"]))
    # selectable_tools 給前端 checkbox 用、含每個工具的中文說明
    tool_descs = {
        "run_python": "在沙盒內跑 Python(讀寫檔、計算、產出都靠這個)",
        "run_shell": "在沙盒內跑 shell 命令(grep / find / git / curl)",
        "read_file": "唯讀單檔(最多 100 行)",
        "web_search": "Tavily 網路搜尋(需設定頁啟用 + 填 key)",
        "view_image": "VLM 看圖(描述、辨識內容)",
        "ask_user": "跑到一半問使用者問題",
    }
    return {
        "roles": out,
        "selectable_tools": [
            {"id": t, "description": tool_descs.get(t, "")}
            for t in SELECTABLE_TOOLS
        ],
        "builtin_ids": sorted(BUILTIN_ROLE_IDS),
    }


@app.post("/subagent/roles")
async def create_subagent_role_endpoint(payload: SubagentRolePayload):
    """新增自訂 role。內建名衝突 / 已存在 / 欄位不合法皆 422。"""
    from pipeline.subagent_runner import load_custom_roles, save_custom_roles
    err = _validate_role_payload(payload, allow_existing=False)
    if err:
        raise HTTPException(status_code=422, detail=err)
    roles = load_custom_roles()
    # done 永遠加進去
    tools = list(payload.tools)
    if "done" not in tools:
        tools.append("done")
    roles[payload.role_id] = {
        "label": payload.label.strip(),
        "description": payload.description.strip(),
        "tools": tools,
        "system_prompt": payload.system_prompt,
    }
    save_custom_roles(roles)
    return {"ok": True, "role_id": payload.role_id, "total_custom": len(roles)}


@app.put("/subagent/roles/{role_id}")
async def update_subagent_role(role_id: str, payload: SubagentRolePayload):
    """編輯自訂 role(內建不可改)。"""
    from pipeline.subagent_runner import (
        load_custom_roles, save_custom_roles, BUILTIN_ROLE_IDS,
    )
    if role_id in BUILTIN_ROLE_IDS:
        raise HTTPException(status_code=403, detail=f"內建角色 '{role_id}' 不可編輯")
    if payload.role_id != role_id:
        raise HTTPException(status_code=400, detail="URL 的 role_id 跟 payload 不一致")
    roles = load_custom_roles()
    if role_id not in roles:
        raise HTTPException(status_code=404, detail=f"自訂角色 '{role_id}' 不存在")
    err = _validate_role_payload(payload, allow_existing=True)
    if err:
        raise HTTPException(status_code=422, detail=err)
    tools = list(payload.tools)
    if "done" not in tools:
        tools.append("done")
    roles[role_id] = {
        "label": payload.label.strip(),
        "description": payload.description.strip(),
        "tools": tools,
        "system_prompt": payload.system_prompt,
    }
    save_custom_roles(roles)
    return {"ok": True, "role_id": role_id}


@app.delete("/subagent/roles/{role_id}")
async def delete_subagent_role(role_id: str):
    """刪自訂 role(內建不可刪)。"""
    from pipeline.subagent_runner import (
        load_custom_roles, save_custom_roles, BUILTIN_ROLE_IDS,
    )
    if role_id in BUILTIN_ROLE_IDS:
        raise HTTPException(status_code=403, detail=f"內建角色 '{role_id}' 不可刪除")
    roles = load_custom_roles()
    if role_id not in roles:
        raise HTTPException(status_code=404, detail=f"自訂角色 '{role_id}' 不存在")
    del roles[role_id]
    save_custom_roles(roles)
    return {"ok": True, "deleted": role_id, "remaining_custom": len(roles)}


@app.get("/skills/{skill_name}/dependencies")
async def scan_skill_deps(skill_name: str):
    """掃描指定 skill 的 Python / Node.js 依賴。
    pip 已安裝列表跟著當前 sandbox 模式走（host venv 或 sandbox container）；
    之前寫死 `list_packages()` 永遠看 host、切到容器模式時所有容器套件都顯示「未安裝」。
    """
    from skill_scanner import scan_skill_dependencies
    result = scan_skill_dependencies(skill_name)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"找不到 skill：{skill_name}")
    # 加上目前已安裝的 pip 套件，前端可對照
    # 走 skill_pkg_manager 的 normalize_pkg_name — 整個 backend 唯一的正規化函式，
    # 之前這裡有自己一份只做 .lower() 的弱化版、會把 `lxml_html_clean` 跟
    # `lxml-html-clean` 視為不同套件、UI 顯示「未安裝」是假警報
    from skill_pkg_manager import list_packages_by_target, normalize_pkg_name

    # 用 target=auto 自動跟著 settings.skill_sandbox_mode（host 或 sandbox）走
    pkg_resp = list_packages_by_target("auto")
    pkg_list = pkg_resp.get("packages") or []
    installed_bases = {normalize_pkg_name(p["name"]) for p in pkg_list if p.get("installed")}
    suggested = result["python"]["suggested_pip"]

    result["python"]["installed"] = sorted(s for s in suggested if normalize_pkg_name(s) in installed_bases)
    result["python"]["missing"] = [s for s in suggested if normalize_pkg_name(s) not in installed_bases]

    # npm 套件也做已安裝對比（跑 `npm list -g`）
    from skill_scanner import list_global_npm_packages
    suggested_npm = result.get("node", {}).get("suggested_npm") or []
    if suggested_npm:
        global_npm = list_global_npm_packages()
        if global_npm:
            result["node"]["installed_npm"] = sorted(p for p in suggested_npm if p.lower() in global_npm)
            result["node"]["missing_npm"] = [p for p in suggested_npm if p.lower() not in global_npm]
            result["node"]["npm_available"] = True
        else:
            # 沒抓到任何全域套件 → npm 不存在或掃描失敗，無法判斷
            result["node"]["installed_npm"] = []
            result["node"]["missing_npm"] = []
            result["node"]["npm_available"] = False
    return result


# ── Notification Settings ──────────────────────────────────
class NotificationSettingsRequest(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_remote_control: Optional[bool] = None
    line_notify_token: Optional[str] = None


def _notification_settings_payload(s: dict) -> dict:
    # 額外回傳「.env 是否有 fallback 值」flag — UI 用來判斷是否可啟用遠端遙控
    # 不直接把 env 值帶到前端（避免明文外洩 / 用戶誤以為已存到 settings）
    import os as _os
    env_token = (_os.environ.get("TELEGRAM_BOT_TOKEN", "") or "").strip()
    env_chat = (_os.environ.get("TELEGRAM_CHAT_ID", "") or "").strip()
    return {
        "telegram_bot_token": s.get("telegram_bot_token", ""),
        "telegram_chat_id": s.get("telegram_chat_id", ""),
        "telegram_remote_control": bool(s.get("telegram_remote_control", False)),
        "line_notify_token": s.get("line_notify_token", ""),
        "telegram_bot_token_env_present": bool(env_token),
        "telegram_chat_id_env_present": bool(env_chat),
    }


@app.get("/settings/notifications")
async def get_notification_settings():
    from settings import get_settings
    return _notification_settings_payload(get_settings())


@app.put("/settings/notifications")
async def put_notification_settings(req: NotificationSettingsRequest):
    from settings import get_settings, _SETTINGS_PATH, _lock
    import json as _json
    import settings as _settings_mod
    s = get_settings()
    if req.telegram_bot_token is not None:
        s["telegram_bot_token"] = req.telegram_bot_token.strip()
    if req.telegram_chat_id is not None:
        s["telegram_chat_id"] = req.telegram_chat_id.strip()
    if req.telegram_remote_control is not None:
        s["telegram_remote_control"] = bool(req.telegram_remote_control)
    if req.line_notify_token is not None:
        s["line_notify_token"] = req.line_notify_token.strip()
    with _lock:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            _json.dump(s, f, ensure_ascii=False, indent=2)
        _settings_mod._cache = s
    return _notification_settings_payload(s)


# ── Web Search (Tavily) ────────────────────────────────────
class WebSearchSettingsRequest(BaseModel):
    tavily_api_key: Optional[str] = None
    web_search_enabled: Optional[bool] = None
    web_search_full_content_default: Optional[bool] = None
    web_search_deep_default: Optional[bool] = None


def _web_search_response_dict(s: dict) -> dict:
    # 回傳給前端的格式：不直接回 key 明文（只回「是否已設定」的 has_key flag）
    # 這樣前端重新載入頁面時，不會把使用者 key 帶回 input 欄位造成誤覆蓋（使用者得重打才能改）
    return {
        "has_key": bool((s.get("tavily_api_key") or "").strip()),
        "web_search_enabled": bool(s.get("web_search_enabled")),
        "web_search_full_content_default": bool(s.get("web_search_full_content_default")),
        "web_search_deep_default": bool(s.get("web_search_deep_default", True)),
    }


@app.get("/settings/web-search")
async def get_web_search_settings():
    from settings import get_settings
    return _web_search_response_dict(get_settings())


@app.put("/settings/web-search")
async def put_web_search_settings(req: WebSearchSettingsRequest):
    from settings import get_settings, _SETTINGS_PATH, _lock
    import json as _json
    import settings as _settings_mod
    s = get_settings()
    # key：空字串當「清除」，非空字串覆寫。未提供（None）= 不動
    if req.tavily_api_key is not None:
        s["tavily_api_key"] = req.tavily_api_key.strip()
    if req.web_search_enabled is not None:
        s["web_search_enabled"] = bool(req.web_search_enabled)
    if req.web_search_full_content_default is not None:
        s["web_search_full_content_default"] = bool(req.web_search_full_content_default)
    if req.web_search_deep_default is not None:
        s["web_search_deep_default"] = bool(req.web_search_deep_default)
    with _lock:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            _json.dump(s, f, ensure_ascii=False, indent=2)
        _settings_mod._cache = s
    return _web_search_response_dict(s)


# ── Skill Sandbox ─────────────────────────────────────
@app.get("/settings/sandbox")
async def get_sandbox_status(refresh: bool = False):
    """回傳沙盒目前狀態 + 設定模式，供前端顯示燈號與 toggle。"""
    from settings import get_settings
    from pipeline import sandbox as _sandbox
    mode = (get_settings().get("skill_sandbox_mode") or "host").strip()
    status = _sandbox.check_status(force_refresh=bool(refresh))
    return {
        "mode": mode,
        **status,
    }


class AutoMinimizeRequest(BaseModel):
    enabled: bool


@app.get("/settings/auto-minimize-for-computer-use")
async def get_auto_minimize_for_computer_use():
    """回傳『含 computer_use 節點的工作流啟動時自動縮小前景視窗』設定。"""
    from settings import get_settings
    return {"enabled": bool(get_settings().get("auto_minimize_for_computer_use", False))}


@app.put("/settings/auto-minimize-for-computer-use")
async def put_auto_minimize_for_computer_use(req: AutoMinimizeRequest):
    """切換『含 computer_use 節點的工作流啟動時自動縮小前景視窗』設定。"""
    from settings import set_auto_minimize_for_computer_use
    updated = set_auto_minimize_for_computer_use(req.enabled)
    return {"enabled": bool(updated.get("auto_minimize_for_computer_use", False))}


class MemorySettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    aggressive: Optional[bool] = None


@app.get("/settings/memory")
async def get_memory_settings():
    """AI 助手長期記憶開關 + 現況。"""
    from settings import get_settings
    s = get_settings()
    out = {
        "enabled": bool(s.get("memory_enabled", True)),
        "aggressive": bool(s.get("memory_aggressive", False)),
        "fact_count": 0,
    }
    try:
        import memory as _mem
        out["fact_count"] = _mem.count_facts()
    except Exception:
        pass
    return out


@app.put("/settings/memory")
async def put_memory_settings(req: MemorySettingsRequest):
    """切換長期記憶主開關 / 激進萃取開關。"""
    from settings import set_memory_settings
    updated = set_memory_settings(enabled=req.enabled, aggressive=req.aggressive)
    return {
        "enabled": bool(updated.get("memory_enabled", True)),
        "aggressive": bool(updated.get("memory_aggressive", False)),
    }


class SelfHealSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    max_attempts: Optional[int] = None


@app.get("/settings/self-heal")
async def get_self_heal_settings():
    """工作流自我修復開關 + 次數上限。"""
    from settings import get_settings
    s = get_settings()
    return {
        "enabled": bool(s.get("self_heal_enabled", False)),
        "max_attempts": int(s.get("self_heal_max_attempts", 2)),
    }


@app.put("/settings/self-heal")
async def put_self_heal_settings(req: SelfHealSettingsRequest):
    """切換自我修復開關 / 次數上限(1~5)。"""
    from settings import set_self_heal_settings
    try:
        updated = set_self_heal_settings(enabled=req.enabled, max_attempts=req.max_attempts)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "enabled": bool(updated.get("self_heal_enabled", False)),
        "max_attempts": int(updated.get("self_heal_max_attempts", 2)),
    }


@app.get("/memory/facts")
async def list_memory_facts(category: Optional[str] = None, limit: int = 100):
    """列出 AI 助手記得的事實 / 偏好(設定頁管理用)。"""
    try:
        import memory as _mem
        return {"facts": _mem.list_facts(category=category, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取記憶失敗: {e}")


@app.delete("/memory/facts/{key}")
async def delete_memory_fact(key: str):
    """刪掉一條記憶(設定頁手動管理)。"""
    try:
        import memory as _mem
        return _mem.forget_fact(key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除記憶失敗: {e}")


class SandboxModeRequest(BaseModel):
    mode: str  # "host" | "wsl_docker"


@app.put("/settings/sandbox")
async def put_sandbox_mode(req: SandboxModeRequest):
    """切換沙盒模式。切到 wsl_docker 時順便回傳目前健康狀態。
    切換時 invalidate 三個快取（host pip / sandbox pip / npm globals），
    讓前端下次 refetch 拿到正確 mode 的資料、不要顯示前一個 mode 的殘留。
    """
    from settings import set_skill_sandbox_mode
    from pipeline import sandbox as _sandbox
    try:
        updated = set_skill_sandbox_mode(req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 清快取：mode 變了之後、所有「跟 mode 連動」的查詢都該重抓
    try:
        from skill_pkg_manager import _invalidate_pip_cache, _invalidate_sandbox_pip_cache
        _invalidate_pip_cache()
        _invalidate_sandbox_pip_cache()
    except Exception:
        pass
    try:
        from skill_scanner import _NPM_CACHE
        _NPM_CACHE["ts"] = 0.0
        _NPM_CACHE["data"] = set()
    except Exception:
        pass
    status = _sandbox.check_status(force_refresh=True)
    return {"mode": updated.get("skill_sandbox_mode", "host"), **status}


# ── Skill 檔案目錄設定 ───────────────────────────────────────
class SkillsDirRequest(BaseModel):
    skills_dir: str  # 自訂 Skill 目錄絕對路徑;空字串 = 用預設 ~/.agents/skills/


@app.get("/settings/skills-dir")
async def get_skills_dir():
    """回傳目前 Skill 目錄設定 + 實際解析到的路徑 + 是否存在 + skill 數。"""
    import os
    from settings import get_settings
    from skill_scanner import get_skills_root, list_available_skills, _DEFAULT_SKILLS_ROOT
    configured = (get_settings().get("skills_dir") or "").strip()
    resolved = get_skills_root()
    return {
        "skills_dir": configured,
        "resolved": str(resolved),
        "default": str(_DEFAULT_SKILLS_ROOT),
        "exists": resolved.exists(),
        "skill_count": len(list_available_skills()),
        "env_override": bool((os.getenv("SKILLS_DIR") or "").strip()),
    }


@app.put("/settings/skills-dir")
async def put_skills_dir(req: SkillsDirRequest):
    """設定自訂 Skill 目錄。空字串 = 還原預設。指定路徑必須是已存在的資料夾。"""
    from pathlib import Path as _Path
    from settings import set_skills_dir
    from skill_scanner import get_skills_root, list_available_skills, seed_default_skills
    path = (req.skills_dir or "").strip()
    if path:
        p = _Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(status_code=400, detail=f"找不到資料夾:{p}")
    set_skills_dir(path)
    # 內建 skill 平常只在啟動時植入;不在這裡補,換到新資料夾後要等重啟才出現,
    # 期間用到 scraped-content-parser 的爬蟲工作流會找不到 skill。
    seed_default_skills()
    resolved = get_skills_root()
    return {
        "skills_dir": path,
        "resolved": str(resolved),
        "exists": resolved.exists(),
        "skill_count": len(list_available_skills()),
    }


# ── Workflows CRUD ──────────────────────────────────────────
class WorkflowRequest(BaseModel):
    name: str = "新工作流"
    canvas: Optional[dict] = None
    validate: bool = False


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    canvas: Optional[dict] = None
    validate: Optional[bool] = None
    yaml: Optional[str] = None
    # 樂觀鎖：前端載入時拿到的 updated_at（秒）。
    # 開著畫布的分頁 autosave 會用「載入當時」的舊狀態整份覆蓋 ——
    # AI 助手剛改好的動作、或另一個分頁的修改被靜默蓋掉（Lite 實測兩次）。
    # 帶了 base 的請求若 DB 已比它新 → 409；不帶維持原行為（向後相容）。
    base_updated_at: Optional[float] = None


@app.get("/workflows")
async def api_list_workflows():
    from db import list_workflows
    return list_workflows()


@app.post("/workflows")
async def api_create_workflow(req: WorkflowRequest):
    from db import create_workflow
    return create_workflow(name=req.name, canvas=req.canvas, validate=req.validate)


@app.get("/workflows/{wf_id}")
async def api_get_workflow(wf_id: str):
    from db import get_workflow
    wf = get_workflow(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")
    return wf


@app.put("/workflows/{wf_id}")
async def api_update_workflow(wf_id: str, req: WorkflowUpdateRequest):
    from db import get_workflow, update_workflow
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    base = patch.pop("base_updated_at", None)
    if base is not None:
        _cur = get_workflow(wf_id)
        # 0.001 秒容差：REAL 浮點來回序列化的誤差別誤判成衝突
        if _cur and _cur["updated_at"] > base + 0.001:
            raise HTTPException(
                status_code=409,
                detail="這個工作流剛被別處修改（AI 助手或其他分頁）。"
                       "請重新載入最新版再改 —— 直接存檔會把那些修改蓋掉。")
    # 只帶 yaml 不帶 canvas(外部 API / TG 遙控更新)→ 從 yaml 重建 canvas,
    # 否則 DB 留著舊 canvas,前端下次載入畫布再 autosave 就會把新 yaml 洗回舊內容(實測事故)。
    if "yaml" in patch and "canvas" not in patch:
        try:
            from yaml_to_canvas import yaml_to_canvas
            _cv = yaml_to_canvas(patch["yaml"])
            if _cv:
                patch["canvas"] = _cv
        except Exception:
            pass  # 重建失敗不阻擋 yaml 更新
    wf = update_workflow(wf_id, patch)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")
    return wf


@app.delete("/workflows/{wf_id}")
async def api_delete_workflow(wf_id: str, cascade: bool = True):
    from db import delete_workflow
    delete_workflow(wf_id, cascade=cascade)
    return {"deleted": True, "cascade": cascade}


# ── Workflow Export / Import ─────────────────────────────────

@app.get("/workflows/{wf_id}/export")
async def api_export_workflow(wf_id: str, share: bool = False):
    """匯出工作流 zip。share=true = 分享模式:去敏(家目錄路徑/email/金鑰樣式/私網IP
    → 佔位符),並打包 SHARE_REPORT.json 讓分享者自查改了什麼。"""
    import io
    import zipfile
    from db import get_workflow, list_recipes
    from fastapi.responses import StreamingResponse

    wf = get_workflow(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")

    recipes = list_recipes(wf_id)

    wf_export = {
        "name": wf["name"],
        "canvas": wf["canvas"],
        "validate": wf["validate"],
        "yaml": wf.get("yaml", ""),
    }
    share_findings = None
    if share:
        from share_sanitizer import scrub_workflow_export
        wf_export, recipes, share_findings = scrub_workflow_export(wf_export, recipes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("workflow.json", json.dumps(wf_export, ensure_ascii=False, indent=2))
        if share_findings is not None:
            zf.writestr("SHARE_REPORT.json", json.dumps({
                "mode": "share(去敏)",
                "findings": share_findings,
                "note": "以上項目已替換為佔位符;匯入者請把 <USER>/路徑等改成自己環境。"
                        "{{ secrets.X }} 引用不含值、保留原樣(匯入者到設定頁存自己的值)。",
            }, ensure_ascii=False, indent=2))

        # recipes/
        for r in recipes:
            recipe_data = {
                "step_name": r["step_name"],
                "task_hash": r["task_hash"],
                "input_fingerprints": r["input_fingerprints"],
                "output_path": r.get("output_path"),
                "code": r["code"],
                "python_version": r["python_version"],
                "success_count": r["success_count"],
                "avg_runtime_sec": r["avg_runtime_sec"],
                # was_interactive:此 recipe 是否由 ask_user 互動產生(如 python-cli-extractor
                # 第一次選模式/參數)。轉移 / 還原時要保留、否則 ask_user 型 recipe 的互動屬性丟失。
                "was_interactive": r.get("was_interactive", False),
            }
            safe_name = r["step_name"].replace("/", "_").replace("\\", "_")
            zf.writestr(f"recipes/{safe_name}.json", json.dumps(recipe_data, ensure_ascii=False, indent=2))

    buf.seek(0)
    from urllib.parse import quote
    safe_wf_name = wf["name"].replace(" ", "_").replace("/", "_")
    encoded_name = quote(safe_wf_name)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"workflow.zip\"; filename*=UTF-8''{encoded_name}.zip"},
    )


@app.post("/workflows/import")
async def api_import_workflow(file: UploadFile = File(...)):
    import io
    import zipfile
    from db import create_workflow, save_recipe, update_workflow

    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="無效的 ZIP 檔案")

    # 讀取 workflow.json
    if "workflow.json" not in zf.namelist():
        raise HTTPException(status_code=400, detail="ZIP 中找不到 workflow.json")

    wf_data = json.loads(zf.read("workflow.json"))

    # 自動避免重名：若已存在相同名稱則加 (1), (2)...
    from db import list_workflows
    existing_names = {w["name"] for w in list_workflows()}
    base_name = wf_data.get("name", "匯入的工作流")
    final_name = base_name
    counter = 1
    while final_name in existing_names:
        final_name = f"{base_name}({counter})"
        counter += 1

    wf = create_workflow(
        name=final_name,
        canvas=wf_data.get("canvas"),
        validate=wf_data.get("validate", False),
    )
    # create_workflow 一律把 yaml 初始化成 ""(yaml 由 canvas 重生)。但匯出包有存原始
    # yaml、且 AI 助手 _workflow_state_block / 部分 server 端會直接讀 stored yaml,
    # 故還原時把它寫回 —— 不然匯入後到第一次在前端存檔前,stored yaml 都是空的。
    _imported_yaml = (wf_data.get("yaml") or "").strip()
    if _imported_yaml:
        update_workflow(wf["id"], {"yaml": _imported_yaml})
        wf["yaml"] = _imported_yaml

    # 匯入 recipes
    recipe_count = 0
    for name in zf.namelist():
        if name.startswith("recipes/") and name.endswith(".json"):
            r = json.loads(zf.read(name))
            try:
                save_recipe(
                    workflow_id=wf["id"],
                    step_name=r["step_name"],
                    task_hash=r["task_hash"],
                    input_fingerprints=r.get("input_fingerprints", {}),
                    output_path=r.get("output_path"),
                    code=r.get("code", ""),
                    python_version=r.get("python_version", ""),
                    runtime_sec=r.get("avg_runtime_sec", 0),
                    was_interactive=r.get("was_interactive", False),
                )
                recipe_count += 1
            except Exception:
                pass

    # 檢查是否有非 Skill 步驟（需要本地腳本）
    has_local_scripts = False
    nodes = wf_data.get("canvas", {}).get("nodes", [])
    for node in nodes:
        data = node.get("data", {})
        if not data.get("skillMode", False) and data.get("batch", "").strip():
            has_local_scripts = True
            break

    return {
        "workflow": wf,
        "recipe_count": recipe_count,
        "has_local_scripts": has_local_scripts,
    }


# ── Recipe Book ──────────────────────────────────────────────
@app.get("/recipes")
async def api_list_recipes(workflow_id: Optional[str] = None):
    from db import list_recipes
    return list_recipes(workflow_id)


@app.get("/recipes/status/{workflow_id}")
async def api_recipe_status(workflow_id: str, steps: str = ""):
    from db import get_recipe_status
    step_names = [s.strip() for s in steps.split(",") if s.strip()] if steps else []
    return get_recipe_status(workflow_id, step_names)


@app.delete("/recipes/{workflow_id}/{step_name}")
async def api_delete_recipe(workflow_id: str, step_name: str):
    from db import delete_recipe
    ok = delete_recipe(workflow_id, step_name)
    return {"deleted": ok}


@app.delete("/recipes/{workflow_id}")
async def api_delete_workflow_recipes(workflow_id: str):
    from db import delete_workflow_recipes
    count = delete_workflow_recipes(workflow_id)
    return {"deleted_count": count}


# ── File System Browser ──────────────────────────────────────
@app.get("/fs/browse")
async def fs_browse(path: str = ""):
    home = Path.home()
    target = Path(path).expanduser() if path else home
    try:
        target.resolve().relative_to(home.resolve())
    except ValueError:
        target = home
    if not target.exists() or not target.is_dir():
        target = home

    items = []
    try:
        for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            items.append({"name": item.name, "path": str(item), "is_dir": item.is_dir(), "ext": item.suffix.lower() if item.is_file() else ""})
    except PermissionError:
        pass

    parent = str(target.parent) if target != home else None
    return {"path": str(target), "parent": parent, "items": items}


# ── 原生 OS 檔案對話框(本機部署用)─────────────────────────────────────
# 後端與使用者同一台(本機 app)時,開 OS 原生對話框(Windows = 檔案總管、
# Mac = Finder),使用者熟悉。用 subprocess 跑 tkinter(獨立 main thread、
# 不卡 FastAPI event loop);tkinter 不可用 / headless / 遠端 → 回 path=null,
# 前端自動 fallback 到內建瀏覽 modal。
class NativePickRequest(BaseModel):
    mode: str = "open"            # open(選檔) | save(另存新檔) | dir(選資料夾)
    initial_dir: Optional[str] = None
    default_name: Optional[str] = None
    py_only: bool = False         # open 模式預設 .py 優先


_NATIVE_PICK_SCRIPT = r'''
import sys, json
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as e:
    print(json.dumps({"path": None, "error": "tkinter unavailable: %s" % e})); sys.exit(0)
args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
mode = args.get("mode", "open")
kw = {}
if args.get("initial_dir"):
    kw["initialdir"] = args["initial_dir"]
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
    root.lift()
    root.update()
except Exception:
    pass
if mode == "dir":
    p = filedialog.askdirectory(**kw)
elif mode == "save":
    if args.get("default_name"):
        kw["initialfile"] = args["default_name"]
    p = filedialog.asksaveasfilename(**kw)
else:
    if args.get("py_only"):
        kw["filetypes"] = [("Python", "*.py"), ("All files", "*.*")]
    else:
        kw["filetypes"] = [("All files", "*.*"), ("Python", "*.py")]
    p = filedialog.askopenfilename(**kw)
try:
    root.destroy()
except Exception:
    pass
print(json.dumps({"path": p or None}))
'''


@app.post("/fs/native-pick")
async def fs_native_pick(req: NativePickRequest):
    import sys as _sys
    import json as _json
    payload = _json.dumps({
        "mode": req.mode,
        "initial_dir": req.initial_dir,
        "default_name": req.default_name,
        "py_only": req.py_only,
    })
    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-c", _NATIVE_PICK_SCRIPT, payload,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        return {"path": None, "error": "timeout(使用者未在 5 分鐘內選擇)"}
    except Exception as e:
        return {"path": None, "error": f"無法開啟原生對話框:{e}"}
    txt = (out or b"").decode("utf-8", "replace").strip()
    if not txt:
        return {"path": None}
    try:
        return _json.loads(txt.splitlines()[-1])
    except Exception:
        return {"path": txt or None}


@app.get("/fs/check-venv")
async def fs_check_venv(dir: str):
    """檢測腳本目錄下是否有可用的 Python 虛擬環境。
    支援兩種常見命名：`venv/`（Windows 慣例）與 `.venv/`（Unix/macOS 慣例），
    回傳第一個找到的 python 可執行檔路徑，讓使用者不用管到底叫哪個名字。"""
    target = Path(dir).expanduser().resolve()
    try:
        target.relative_to(Path.home().resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="只允許在 home 目錄下操作")
    import os as _os
    is_win = _os.name == "nt"
    venv_subdir = "Scripts" if is_win else "bin"
    py_name = "python.exe" if is_win else "python"
    # 兩種慣例都檢查一次，誰先找到用誰（venv 先，因為 Windows 使用者比較常這樣命名）
    for venv_dir_name in ("venv", ".venv"):
        venv_python = target / venv_dir_name / venv_subdir / py_name
        if venv_python.exists():
            return {
                "has_venv": True,
                "python_path": str(venv_python),
                "venv_dir_name": venv_dir_name,
            }
    return {"has_venv": False, "python_path": None, "venv_dir_name": None}


# ── 開啟輸出資料夾(本機部署用)───────────────────────────────────────
@app.get("/fs/open-output")
async def fs_open_output(name: str = ""):
    """在本機檔案總管開啟某工作流的輸出資料夾(OUTPUT_BASE_PATH/<工作流名稱>/)。
    後端與使用者同機(本機 app)才有意義。找不到該資料夾 → 退回開 OUTPUT_BASE_PATH 根。"""
    import sys as _sys
    import subprocess as _sp
    from config import OUTPUT_BASE_PATH
    base = Path(OUTPUT_BASE_PATH).resolve()
    target = base
    existed = False
    if name:
        cand = (base / name).resolve()
        try:
            cand.relative_to(base)   # 防路徑穿越:必須在 OUTPUT_BASE_PATH 底下
        except ValueError:
            raise HTTPException(status_code=400, detail="非法的工作流名稱")
        if cand.is_dir():
            target = cand
            existed = True
            # per-run 子夾:產物實際落在 <name>/run_<ts>/。若母夾底下有 run_<ts>/ 子夾,
            # 直接開「最新一次」那夾,而非停在母夾(否則使用者只看到一排 run_ 夾、還要自己點進去)。
            # 與 chat_tools._resolve_workflow_output_dir 的「挑最新 run」邏輯對齊。
            try:
                _run_dirs = [d for d in cand.iterdir() if d.is_dir() and d.name.startswith("run_")]
                if _run_dirs:
                    target = max(_run_dirs, key=lambda d: d.stat().st_mtime)
            except Exception:
                pass
    try:
        if _sys.platform.startswith("win"):
            os.startfile(str(target))            # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            _sp.Popen(["open", str(target)])
        else:
            _sp.Popen(["xdg-open", str(target)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"開啟資料夾失敗:{e}")
    return {"opened": str(target), "existed": existed}


# ── Log Analysis ──────────────────────────────────────────────
# 常見 module → pip 套件名稱對映（module 名與 pip 名不同的情況）
_MODULE_TO_PIP = {
    "cv2": "opencv-python", "PIL": "Pillow", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "yaml": "pyyaml", "docx": "python-docx",
    "pptx": "python-pptx", "dotenv": "python-dotenv", "jwt": "pyjwt",
    "gi": "pygobject", "Crypto": "pycryptodome", "serial": "pyserial",
    "usb": "pyusb", "magic": "python-magic", "dateutil": "python-dateutil",
    "attr": "attrs", "lxml": "lxml", "wx": "wxPython",
}


@app.get("/pipeline/logs/analyze")
async def analyze_logs(count: int = 5):
    """掃描最近 N 筆 pipeline log，找出 ModuleNotFoundError / ImportError 並建議套件"""
    from pipeline.logger import LOG_DIR
    import re

    log_files = sorted(Path(LOG_DIR).glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:count]

    missing: dict[str, dict] = {}  # module_name → { pip, files }
    # 用 [^'\"\n] 阻止跨行貪婪匹配（避免 log 截斷造成的誤判：
    # 例如 `No module named 'p...\n...next-line-has-quote` 不該匹配出 `p`）
    pattern = re.compile(
        r"(?:ModuleNotFoundError:\s*No module named\s*['\"]([^'\"\n]+)['\"]"
        r"|ImportError:\s*cannot import name\s*['\"]?\w+['\"]?\s*from\s*['\"]([^'\"\n]+)['\"]"
        r"|ImportError:\s*No module named\s*['\"]([^'\"\n]+)['\"])"
    )

    analyzed_files = []
    for lf in log_files:
        text = lf.read_text(encoding="utf-8", errors="ignore")
        found_in_file = False
        for m in pattern.finditer(text):
            raw = m.group(1) or m.group(2) or m.group(3)
            top_module = raw.split(".")[0]
            # 過濾無效結果：太短、非 identifier、以 "..." 結尾（log 截斷殘跡）
            if (
                len(top_module) < 3
                or not top_module.isidentifier()
                or raw.endswith("...")
            ):
                continue
            pip_name = _MODULE_TO_PIP.get(top_module, top_module)
            if top_module not in missing:
                missing[top_module] = {"pip": pip_name, "files": []}
            if lf.name not in missing[top_module]["files"]:
                missing[top_module]["files"].append(lf.name)
            found_in_file = True
        analyzed_files.append({
            "name": lf.name,
            "size": lf.stat().st_size,
            "has_errors": found_in_file,
        })

    suggestions = [
        {"module": mod, "pip_name": info["pip"], "found_in": info["files"]}
        for mod, info in sorted(missing.items())
    ]

    return {"analyzed": len(log_files), "files": analyzed_files, "suggestions": suggestions}


# ── Pipeline Run ─────────────────────────────────────────────
class PipelineRunRequest(BaseModel):
    yaml_content: str
    validate: bool = True
    use_recipe: bool = False  # True = 快速模式：recipe 命中時跳過 LLM 驗證
    workflow_id: Optional[str] = None  # 關聯工作流 ID
    no_save_recipe: bool = False  # True = 延遲 recipe 儲存，等用戶確認（桌面手動勾「skill workflow」用）
    silent_recipe: bool = False  # True = 「無人值守」模式：直接覆寫 recipe、永不延遲、不彈確認
                                   # （TG 遠端遙控 / 排程觸發用、避免桌面卡在 pending dialog）
    # 啟動時傳入的參數，runner render 階段以 `{{ input.<key> }}` 引用。
    # workflow YAML 寫死的欄位仍照舊用,只有寫了 {{ input.X }} 的欄位才需要這裡帶值。
    input_params: dict = {}


class PipelineDecisionRequest(BaseModel):
    decision: str  # retry | skip | abort | continue | retry_with_hint
    hint: Optional[str] = None  # 補充指示（retry_with_hint 時使用）


# ── YAML 容錯:雙引號包 Windows 路徑自動轉正 ───────────────────────────
# 背景:LLM(尤其免費模型)常把 Windows 絕對路徑寫成 `path: "C:\Users\..."`,
# 雙引號內 \U \x \n 等被 YAML 當 escape sequence → ScannerError 整份解析失敗。
# Prompt 已明確禁止(見 system prompt 「Windows 絕對路徑」段)但模型照犯,
# 故在 server 端做最後防線:僅在初次解析失敗時,把「雙引號內含反斜線」的純量
# 轉成單引號再重試(單引號 YAML 不解析 escape)。對本來就正常的 YAML 零影響。
_WIN_DQUOTE_RE = __import__("re").compile(r'(?m)([:\-]\s+)"([^"\n]*\\[^"\n]*)"(\s*(?:#[^\n]*)?)$')


def _sanitize_windows_paths_in_yaml(text: str) -> str:
    def _fix(m):
        prefix, val, tail = m.group(1), m.group(2), m.group(3)
        if "'" in val:  # 含單引號才需特殊處理;Windows 路徑通常沒有 → 保守跳過
            return m.group(0)
        return f"{prefix}'{val}'{tail}"
    return _WIN_DQUOTE_RE.sub(_fix, text)


def _lenient_yaml_load(text: str):
    """寬鬆解析:先正常 load,失敗則嘗試修雙引號 Windows 路徑後重試。"""
    import yaml
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        fixed = _sanitize_windows_paths_in_yaml(text)
        if fixed != text:
            return yaml.safe_load(fixed)  # 若仍失敗,讓例外往上拋給呼叫端處理
        raise


# ── 使用者訊息裡的 Windows 路徑:反斜線 → 正斜線正規化 ────────────────
# 背景:使用者貼 `C:\Users\...\text_tool_gui\app.py` 給 AI 助手,LLM 讀進去再 echo
# 時會把 `\t`(text)、`\U`(Users)、`\n` 等當逃脫字元吃掉(實測 gemma 把
# text_tool_gui 變成 _tool_gui)。改成 `C:/Users/.../app.py` 後:Windows 與 Python
# (subprocess / argparse / pathlib)都接受正斜線,且 `/` 無逃脫語意 → 模型再 echo
# 也壞不了。只動「看起來像 Windows 路徑」的 token(drive-letter 或 UNC 開頭)。
_WINPATH_TOKEN = __import__("re").compile(r'(?:[A-Za-z]:|\\\\)[\\/][^\s"\'<>|]*')


def _normalize_win_paths(text: str) -> str:
    if not text or "\\" not in text:
        return text
    return _WINPATH_TOKEN.sub(lambda m: m.group(0).replace("\\", "/"), text)


@app.post("/pipeline/run")
async def start_pipeline(req: PipelineRunRequest):
    import uuid, yaml
    from pipeline.models import PipelineConfig
    from pipeline.runner import run_pipeline
    from pipeline.store import PipelineRun as PRun, get_store
    from pipeline.logger import create_run_logger
    try:
        import logging as _logging
        _log = _logging.getLogger("pipeline")
        _log.debug(f"收到 YAML（{len(req.yaml_content)} 字元）:\n{req.yaml_content}")
        data = _lenient_yaml_load(req.yaml_content)
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = req.validate
        config = PipelineConfig(**config_dict)
        for i, s in enumerate(config.steps):
            _log.debug(f"步驟[{i}] batch（{len(s.batch)} 字元）：{s.batch[:300]}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失敗：{e}")

    # 先建立 run 並存入 store，確保前端立刻能查詢
    run_id = str(uuid.uuid4())[:12]
    _, log_path = create_run_logger(run_id, config.name)
    config_d = config.model_dump()
    config_d["_use_recipe"] = req.use_recipe  # 傳遞快速模式旗標
    config_d["_workflow_id"] = req.workflow_id  # 關聯工作流
    config_d["_no_save_recipe"] = req.no_save_recipe  # 延遲 recipe 儲存
    config_d["_silent_recipe"] = req.silent_recipe    # 無人值守模式（TG/排程）→ 直接覆寫
    run = PRun(
        run_id=run_id,
        pipeline_name=config.name,
        config_dict=config_d,
        telegram_chat_id=0,
        log_path=log_path,
        workflow_id=req.workflow_id,
        input_params=req.input_params or {},
    )
    get_store().save(run)

    # 背景執行（runner 看到已存在的 run_id 會恢復執行）
    from pipeline.runner import register_task
    task = asyncio.create_task(run_pipeline(config_d, chat_id=0, run_id=run_id))
    register_task(run_id, task)

    return {"run_id": run_id, "message": f"Pipeline '{config.name}' 已啟動"}


# ── Webhook 觸發器:外部 HTTP POST /webhooks/<token> 觸發工作流 ────────────
#   一個工作流一個 webhook;token 為不可猜密鑰(公開觸發端靠它保護,不做認證)。
#   觸發時 POST body(JSON 物件)→ run 的 input_params,工作流可用 {{ input.X }} 取值。

def _webhook_full_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/webhooks/{token}"


@app.post("/workflows/{workflow_id}/webhook")
async def create_workflow_webhook(workflow_id: str, request: Request):
    """為工作流建立/重新產生 webhook 觸發 URL(每次呼叫換新 token、舊的失效)。"""
    from db import get_workflow, create_webhook
    if not get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    hook = create_webhook(workflow_id)
    return {**hook, "url": _webhook_full_url(request, hook["token"])}


@app.get("/workflows/{workflow_id}/webhook")
async def get_workflow_webhook(workflow_id: str, request: Request):
    """查該工作流目前的 webhook(含觸發次數 / 最後觸發時間)。"""
    from db import get_webhook_by_workflow
    hook = get_webhook_by_workflow(workflow_id)
    if not hook:
        raise HTTPException(status_code=404, detail="此工作流尚未設定 webhook")
    enabled = bool(hook["enabled"])
    return {**hook, "enabled": enabled,
            "url": _webhook_full_url(request, hook["token"]) if enabled else None}


@app.delete("/workflows/{workflow_id}/webhook")
async def delete_workflow_webhook(workflow_id: str):
    """停用該工作流的 webhook(觸發端之後一律 404)。"""
    from db import disable_webhook
    ok = disable_webhook(workflow_id)
    return {"ok": ok, "disabled": ok}


@app.post("/webhooks/{token}")
async def trigger_webhook(token: str, request: Request):
    """外部觸發入口(公開、靠不可猜 token 保護)。POST body(JSON 物件)→ run 的 input_params。"""
    from db import get_webhook_by_token, get_workflow, mark_webhook_fired
    hook = get_webhook_by_token(token)
    if not hook:
        raise HTTPException(status_code=404, detail="webhook 不存在或已停用")
    wf = get_workflow(hook["workflow_id"])
    if not wf:
        raise HTTPException(status_code=404, detail="綁定的工作流已不存在")
    # body → input_params;非 JSON 物件 / 空 body 都容忍(包成 payload / 空 dict)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {"payload": body}
    except Exception:
        body = {}
    mark_webhook_fired(token)
    # 無人值守:快速模式(命中 recipe)+ silent_recipe(不彈桌面 dialog)
    result = await start_pipeline(PipelineRunRequest(
        yaml_content=wf["yaml"],
        validate=bool(wf.get("validate", True)),
        use_recipe=True,
        workflow_id=hook["workflow_id"],
        silent_recipe=True,
        input_params=body,
    ))
    return {"triggered": True, "workflow_id": hook["workflow_id"], **result}


# ── 檔案夾監看觸發器:資料夾出現新檔 → 觸發工作流(輪詢式,免 watchdog)────────
_folder_watch_task = None
_FOLDER_WATCH_INTERVAL = 8.0   # 秒


async def _folder_watch_poller():
    """背景輪詢:每隔數秒掃各 enabled 監看的資料夾,新檔 → 觸發工作流(檔案路徑進 input_params)。"""
    import logging as _lg
    from db import list_enabled_folder_watches, update_folder_watch_progress, get_workflow
    from folder_watch import scan_new_files
    _log = _lg.getLogger("folder_watch")
    while True:
        try:
            for w in list_enabled_folder_watches():
                try:
                    new_files, mx = scan_new_files(w["folder_path"], w["pattern"], w["last_seen_mtime"])
                    if not new_files:
                        continue
                    wf = get_workflow(w["workflow_id"])
                    if not wf:
                        continue
                    fired = 0
                    for fp in new_files:
                        try:
                            await start_pipeline(PipelineRunRequest(
                                yaml_content=wf["yaml"],
                                validate=bool(wf.get("validate", True)),
                                use_recipe=True,
                                workflow_id=w["workflow_id"],
                                silent_recipe=True,
                                input_params={"file_path": fp, "file_name": os.path.basename(fp)},
                            ))
                            fired += 1
                        except Exception as e:
                            _log.warning(f"觸發失敗 {fp}: {e}")
                    update_folder_watch_progress(w["id"], mx, fired)
                    if fired:
                        _log.info(f"監看觸發:{w['workflow_id']} 新檔 {fired} 個")
                except Exception as e:
                    _log.warning(f"監看 {w.get('workflow_id')} 掃描失敗: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"folder-watch poller 例外: {e}")
        try:
            await asyncio.sleep(_FOLDER_WATCH_INTERVAL)
        except asyncio.CancelledError:
            break


class FolderWatchRequest(BaseModel):
    folder_path: str
    pattern: str = "*"


@app.post("/workflows/{workflow_id}/folder-watch")
async def create_workflow_folder_watch(workflow_id: str, req: FolderWatchRequest):
    """設定「資料夾出現新檔就觸發」。只看建立之後的新檔(不對既有檔一次全轟)。"""
    from db import get_workflow, create_folder_watch
    if not get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    if not os.path.isdir(req.folder_path):
        raise HTTPException(status_code=400, detail=f"資料夾不存在:{req.folder_path}")
    return create_folder_watch(workflow_id, req.folder_path, req.pattern)


@app.get("/workflows/{workflow_id}/folder-watch")
async def get_workflow_folder_watch(workflow_id: str):
    from db import get_folder_watch_by_workflow
    w = get_folder_watch_by_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="此工作流尚未設定檔案夾監看")
    return {**w, "enabled": bool(w["enabled"])}


@app.delete("/workflows/{workflow_id}/folder-watch")
async def delete_workflow_folder_watch(workflow_id: str):
    from db import disable_folder_watch
    ok = disable_folder_watch(workflow_id)
    return {"ok": ok, "disabled": ok}


# ── MCP servers 管理:像 Skill 掛載 — 安裝(live 驗證)/ 列表 / 乾淨移除 ──────────
#   新增時「先真連線掃 tools、成功才存」→ 保證 AI 助手拿到的清單都是可用的。
#   tools_cache 注入 AI 助手提示詞(有裝才注入;沒裝 → 助手完全不知道 MCP、不會亂用)。

# 精選常用 MCP server 目錄(前端一鍵安裝用)。args 中 <> 為需使用者填的佔位。
_MCP_CATALOG = [
    {"id": "filesystem", "name": "filesystem", "description": "檔案系統讀寫(讀/寫/列目錄/搜尋),限定在指定資料夾內",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "<允許存取的資料夾路徑>"], "env_keys": []},
    {"id": "memory", "name": "memory", "description": "知識圖譜記憶(存實體/關係,跨工作流記住事實)",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"], "env_keys": []},
    {"id": "fetch", "name": "fetch", "description": "網頁抓取(URL → 內容,輕量;重度爬蟲請用內建爬蟲節點)",
     "command": "uvx", "args": ["mcp-server-fetch"], "env_keys": []},
    {"id": "github", "name": "github", "description": "GitHub 操作(repo/issue/PR 讀寫),需 Personal Access Token",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
     "guidance": "到 github.com/settings/tokens 建立 Personal Access Token(fine-grained 或 classic)→ 勾選 repo 權限 → 複製後填入 GITHUB_PERSONAL_ACCESS_TOKEN。"},
    {"id": "sqlite", "name": "sqlite", "description": "SQLite 資料庫查詢/寫入(指定 .db 檔)",
     "command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "<db檔路徑>"], "env_keys": []},
    # ── 辦公 SaaS(stdio + API token,經 env 連;跟 github 同一種模式、無遠端外送)──
    # 每個都經 npm registry + bin 驗證可用、env 變數取自各套件官方 README(2026-07)。
    # 注意:安裝時的 live 掃描只驗證「server 能啟動並列出 tools」,不保證 token 有效
    #       —— 多數 SaaS server 的 tool 清單是靜態的,token 錯要到工作流實際呼叫 tool 時才會失敗
    #       (實測 notion 假 token 仍裝得起來)。所以指引要提醒使用者:第一次跑到該節點才知道 token 對不對。
    {"id": "notion", "name": "notion", "description": "Notion 讀寫(頁面/資料庫查詢與更新),需 Integration Token",
     "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"], "env_keys": ["NOTION_TOKEN"],
     "guidance": "① 到 notion.so/my-integrations 建立 Internal Integration → 複製 Secret(ntn_ 開頭)填入 NOTION_TOKEN。"
                 "② 重要:到你要讓它存取的頁面/資料庫,按右上 ⋯ → Connections → 加入這個 integration 授權,否則會讀不到內容。"},
    {"id": "atlassian", "name": "atlassian", "description": "Jira + Confluence(議題/文件查詢、匯出),需 API Token",
     "command": "npx", "args": ["-y", "mcp-atlassian"], "env_keys": ["ATLASSIAN_BASE_URL", "ATLASSIAN_API_TOKEN"],
     "guidance": "① ATLASSIAN_BASE_URL 填 https://你的公司.atlassian.net。"
                 "② 到 id.atlassian.com/manage-profile/security/api-tokens 建立 API token 填入 ATLASSIAN_API_TOKEN。"
                 "③ 若連線失敗,部分帳號需再補 email/使用者名稱,請參照套件 README。"},
    {"id": "airtable", "name": "airtable", "description": "Airtable 資料表讀寫(base/table/record),需 Personal Access Token",
     "command": "npx", "args": ["-y", "airtable-mcp-server"], "env_keys": ["AIRTABLE_API_KEY"],
     "guidance": "到 airtable.com/create/tokens 建立 Personal Access Token(pat 開頭)→ 勾選 data.records 等 scope 與要存取的 base → 填入 AIRTABLE_API_KEY。"},
    # Slack / Postgres:官方參考套件已棄用,改用維護中的社群版(仍是 stdio + 貼 token/連線字串、非 OAuth)。
    {"id": "slack", "name": "slack", "description": "Slack 讀訊息/發訊息/查頻道,需 User 或 Bot Token(非 OAuth 流程)",
     "command": "npx", "args": ["-y", "slack-mcp-server"], "env_keys": ["SLACK_MCP_XOXP_TOKEN"],
     "guidance": "① 到 api.slack.com/apps 建立 App → OAuth & Permissions 取得 User Token(xoxp- 開頭)填入 SLACK_MCP_XOXP_TOKEN。"
                 "② 若你只有 Bot Token(xoxb-),改用手動新增 server、env 填 SLACK_MCP_XOXB_TOKEN。"
                 "③ 發訊息預設關閉,要開需另設 SLACK_MCP_ADD_MESSAGE_TOOL。"},
    {"id": "postgres", "name": "postgres", "description": "PostgreSQL 查詢/schema 檢視,填連線字串(非 OAuth)",
     "command": "npx", "args": ["-y", "@henkey/postgres-mcp-server"], "env_keys": ["POSTGRES_CONNECTION_STRING"],
     "guidance": "POSTGRES_CONNECTION_STRING 填 postgresql://使用者:密碼@主機:5432/資料庫名。"
                 "建議先用唯讀帳號、限本機或內網 DB;連線字串含密碼、匯出分享工作流前記得用去敏。"},
    # Google Workspace(Gmail/Calendar/Drive/Sheets)= OAuth,設定比貼 token 麻煩(需 Google Cloud 專案)。
    # 憑證快取在 ~/.google_workspace_mcp/credentials(家目錄)→ 終端機授權一次、Atlas 的 stdio 自動重用。
    {"id": "google_workspace", "name": "google_workspace",
     "description": "Google Workspace(Gmail/Calendar/Drive/Docs/Sheets),OAuth — 需先在終端機授權一次",
     "command": "uvx", "args": ["workspace-mcp", "--single-user", "--tools", "gmail", "calendar", "drive"],
     "env_keys": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "OAUTHLIB_INSECURE_TRANSPORT"],
     "guidance": "⚠️ 這個要 OAuth、比貼 token 麻煩,務必先照「Google Workspace 設定教學」做完再按安裝:"
                 "① Google Cloud 建專案→啟用 Gmail API→OAuth 同意畫面(加自己為測試使用者)→建 OAuth 用戶端"
                 "(重新導向 URI 填 http://localhost:8000/oauth2callback),拿到 client_id / client_secret。"
                 "② 終端機設好上面三個 env(OAUTHLIB_INSECURE_TRANSPORT 填 1)後先跑一次 "
                 "`uvx workspace-mcp --single-user --tools gmail` 完成瀏覽器授權(token 會快取)。"
                 "③ 回來這裡填同樣的 client_id / client_secret /(OAUTHLIB_INSECURE_TRANSPORT=)1 按安裝,Atlas 會重用快取。"},
]


@app.get("/mcp/catalog")
async def mcp_catalog():
    """精選常用 MCP server(一鍵安裝);args 含 <佔位> 者需使用者補值。"""
    return {"catalog": _MCP_CATALOG}


@app.get("/mcp/servers")
async def mcp_servers_list():
    from db import list_mcp_servers
    return {"servers": list_mcp_servers()}


class McpServerRequest(BaseModel):
    name: str
    command: str = ""
    args: list[str] = []
    env: dict = {}
    transport: str = "stdio"        # stdio | sse | streamable-http
    url: str = ""                   # 遠端 transport 用
    remote_consent: bool = False    # 遠端 server 必須明確同意(會把資料送到外部端點)
    admin_password: str = ""        # 遠端 server 的防呆閘(.env 有設 ATLAS_REMOTE_MCP_PASSWORD 才要求)


@app.post("/mcp/servers")
async def mcp_server_add(req: McpServerRequest):
    """安裝(掛載)MCP server:live 連線 + 掃 tools,成功才存。首次跑 npx/uvx 會下載套件、可能較久。

    transport=stdio:本機子行程(command/args/env)。
    transport=sse / streamable-http:連遠端 url —— ⚠️ 會把 tool 參數送到外部端點、
      回傳內容進入工作流,必須帶 remote_consent=true 明確同意才安裝。
    """
    name = (req.name or "").strip()
    _transport = (req.transport or "stdio").lower().replace("_", "-")
    _is_remote = _transport in ("sse", "http", "streamable-http", "streamablehttp")
    if not name:
        raise HTTPException(status_code=400, detail="name 必填")
    if _is_remote:
        if not (req.url or "").strip():
            raise HTTPException(status_code=400, detail="遠端 MCP 需填 url")
        if not (req.url.startswith("http://") or req.url.startswith("https://")):
            raise HTTPException(status_code=400, detail="url 需為 http(s):// 開頭")
        if not req.remote_consent:
            raise HTTPException(status_code=400,
                detail="遠端 MCP server 會把資料送到外部端點、回傳內容會進入你的工作流。"
                       "請確認信任此 server 後再安裝(remote_consent=true)。")
        # 防呆閘:.env 設了 ATLAS_REMOTE_MCP_PASSWORD 才啟用 —— 攔隨手貼遠端 URL 的無腦操作。
        # 這是「摩擦/防手滑」不是存取控制(密碼在 .env 明文、能讀檔者可繞);沒設就維持只靠 consent。
        import hmac as _hmac
        _gate_pw = (os.environ.get("ATLAS_REMOTE_MCP_PASSWORD") or "").strip()
        if _gate_pw:
            # 用 bytes 比對:compare_digest 對含非 ASCII 的 str 會丟 TypeError(中文密碼會炸)
            if not _hmac.compare_digest(_gate_pw.encode("utf-8"),
                                        (req.admin_password or "").strip().encode("utf-8")):
                raise HTTPException(status_code=403,
                    detail="新增遠端 MCP server 需要管理者密碼(由系統管理員在 .env 的 "
                           "ATLAS_REMOTE_MCP_PASSWORD 設定)。密碼錯誤或未輸入。")
    else:
        if not (req.command or "").strip():
            raise HTTPException(status_code=400, detail="stdio MCP 需填 command")
        if any("<" in a and ">" in a for a in (req.args or [])):
            raise HTTPException(status_code=400, detail="args 內還有 <佔位> 未填(如資料夾路徑)")
    # 遠端用 env 的 Authorization/Bearer 當 header(貼 token 連);沒有就不帶
    _headers = None
    if _is_remote and req.env:
        _tok = req.env.get("Authorization") or req.env.get("AUTHORIZATION") or req.env.get("token") or req.env.get("TOKEN")
        if _tok:
            _headers = {"Authorization": _tok if str(_tok).lower().startswith("bearer ") else f"Bearer {_tok}"}
    from mcp_client import list_mcp_tools
    scan = await list_mcp_tools(command=req.command, args=req.args or [], env=(req.env or None),
                                url=(req.url or None), transport=_transport, headers=_headers, timeout=120.0)
    if not scan.get("ok"):
        raise HTTPException(status_code=400,
                            detail=f"連線 MCP server 失敗(未儲存):{scan.get('error')}")
    tools = scan["tools"]
    from db import upsert_mcp_server
    saved = upsert_mcp_server(name, (req.command or "").strip(), req.args or [], req.env or {}, tools,
                              transport=_transport, url=(req.url or "").strip())
    return {"ok": True, "server": saved, "tool_count": len(tools)}


@app.post("/mcp/servers/{name}/refresh")
async def mcp_server_refresh(name: str):
    """重新掃描該 server 的 tools(套件更新後用)。"""
    from db import get_mcp_server, upsert_mcp_server
    s = get_mcp_server(name)
    if not s:
        raise HTTPException(status_code=404, detail="server 不存在")
    from mcp_client import list_mcp_tools
    scan = await list_mcp_tools(command=s["command"], args=s["args"],
                                env=(s["env"] or None), timeout=120.0)
    if not scan.get("ok"):
        raise HTTPException(status_code=400, detail=f"重掃失敗:{scan.get('error')}")
    saved = upsert_mcp_server(name, s["command"], s["args"], s["env"], scan["tools"])
    return {"ok": True, "server": saved, "tool_count": len(scan["tools"])}


@app.delete("/mcp/servers/{name}")
async def mcp_server_remove(name: str):
    """乾淨移除:server 為隨叫隨起 subprocess、無常駐程序;刪註冊列即完全移除,AI 助手提示詞同步消失。"""
    from db import remove_mcp_server
    ok = remove_mcp_server(name)
    if not ok:
        raise HTTPException(status_code=404, detail="server 不存在")
    return {"ok": True, "removed": name}


# ── 外部 AI 代跑(途徑3):Claude 親自寫碼 → Atlas 執行 → 調通固化 Recipe ────────

class SandboxRunRequest(BaseModel):
    code: str
    working_dir: str = ""


@app.post("/sandbox/run")
async def sandbox_run(req: SandboxRunRequest):
    """執行一段 Python(與 skill 節點 host 路徑同一執行器:stub 注入/警告抑制/UTF-8)。
    給外部 AI 迭代調教用;工作目錄限 OUTPUT_BASE 內。"""
    from pipeline.executor import _skill_run_python
    from config import OUTPUT_BASE_PATH
    from pathlib import Path as _P
    wd = _P(req.working_dir).resolve() if req.working_dir else (OUTPUT_BASE_PATH / "_ai_scratch")
    _base = OUTPUT_BASE_PATH.resolve()
    if not (wd == _base or str(wd).startswith(str(_base))):
        raise HTTPException(status_code=400, detail=f"working_dir 必須在 {OUTPUT_BASE_PATH} 內")
    wd.mkdir(parents=True, exist_ok=True)
    out = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _skill_run_python(req.code, cwd=str(wd)))
    return {"output": out[:20000], "working_dir": str(wd)}


class SubmitCodeRequest(BaseModel):
    code: str
    input_paths: list[str] = []   # 上游輸入檔(算指紋;單步/無上游可空)


@app.post("/workflows/{wf_id}/steps/{step_name}/submit-code")
async def submit_step_code(wf_id: str, step_name: str, req: SubmitCodeRequest):
    """外部 AI 調通的程式碼提交:驗證 → 試跑 → 輸出檢查(含 json_schema)→ 固化 Recipe。
    之後這條工作流在 Atlas 跑到該步、同輸入指紋即 0-token 重放這份碼。"""
    import yaml as _y
    from db import get_workflow
    from pipeline.models import PipelineConfig
    from pipeline.executor import _skill_run_python
    from pipeline.recipe import _sha1 as _recipe_sha1, _fingerprint_input
    from config import OUTPUT_BASE_PATH

    wf = get_workflow(wf_id)
    if not wf or not (wf.get("yaml") or "").strip():
        raise HTTPException(status_code=404, detail="工作流不存在或 YAML 為空")
    cfg = PipelineConfig(**_y.safe_load(wf["yaml"]))
    _step_idx = next((i for i, s in enumerate(cfg.steps) if s.name == step_name), -1)
    step = cfg.steps[_step_idx] if _step_idx >= 0 else None
    if not step:
        raise HTTPException(status_code=404, detail=f"步驟「{step_name}」不存在")
    # recipe DB key 必須與 runner 一致(runner:f"{step_num}:{step.name}",step_num=索引+1)
    _rkey = f"{_step_idx + 1}:{step_name}"
    if not step.skill_mode:
        raise HTTPException(status_code=400, detail="只有 AI 技能節點(skill_mode)能提交 Recipe 程式碼")

    # 試跑(隔離暫存資料夾)
    trial = OUTPUT_BASE_PATH / "_ai_scratch" / f"submit_{step_name}"
    trial.mkdir(parents=True, exist_ok=True)
    out = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _skill_run_python(req.code, cwd=str(trial)))
    if "Traceback" in out or "SyntaxError" in out:
        return {"ok": False, "rejected": "試跑失敗(有例外),請修正後重送", "output": out[:3000]}

    # 輸出檢查:宣告的 output.path 必須真的產生;.json 且有 schema → 過 schema gate
    out_path = None
    if step.output and step.output.path:
        cand = trial / step.output.path
        if not cand.exists() or (cand.is_file() and cand.stat().st_size == 0):
            return {"ok": False,
                    "rejected": f"程式沒有產生宣告的輸出檔 {step.output.path}(必須寫在工作目錄下的相對路徑)",
                    "output": out[:2000]}
        out_path = str(cand)
        schema = getattr(step.output, "json_schema", None)
        if schema:
            from pipeline.schema_gate import validate_output_schema
            ok_s, err_s = validate_output_schema(out_path, schema)
            if not ok_s:
                return {"ok": False, "rejected": f"輸出未過 json_schema 合約:{err_s[:500]}"}

    # 固化 Recipe(鍵必須與 runner 執行端完全一致:sha1(build_skill_task_description) + 輸入指紋,
    # 否則跑到該步時 hash 不符 → 永遠 miss、外部 AI 的碼白存)
    from db import save_recipe
    from pipeline.recipe import build_skill_task_description
    _td = build_skill_task_description(step.batch, getattr(step.output, "json_schema", None) if step.output else None)
    _th = _recipe_sha1(_td)
    fps = {p: _fingerprint_input(p) for p in (req.input_paths or [])}
    save_recipe(wf_id, _rkey, _th, fps, out_path, req.code, "", 0.0)
    return {"ok": True, "message": f"Recipe 已固化;之後「{step_name}」同輸入指紋即 0-token 重放你這份碼",
            "task_hash": _th, "fingerprints": fps, "trial_output": out[:1200]}


@app.get("/mcp/server-config")
async def mcp_server_config(request: Request):
    """回傳「把 Atlas 當 MCP server」的 Claude Desktop 設定片段(路徑都算好,可直接貼)。"""
    import sys as _sys
    from pathlib import Path as _P
    server_py = str((_P(__file__).parent / "atlas_mcp_server.py").resolve())
    base = str(request.base_url).rstrip("/")
    return {
        "config": {
            "mcpServers": {
                "atlas": {
                    "command": _sys.executable,
                    "args": [server_py],
                    "env": {"ATLAS_BASE_URL": base},
                }
            }
        },
        "tools": ["list_workflows", "run_workflow", "get_run_status", "get_run_output"],
        "note": "貼進 Claude Desktop 的 claude_desktop_config.json(或任何 MCP client 設定);Atlas 後端需在執行中。",
    }


# ── Secrets Vault:憑證加密存放,工作流 {{ secrets.名稱 }} 引用 ───────────────
#   API 永不回傳明文值;AI 助手只知道名稱清單。

class SecretRequest(BaseModel):
    name: str
    value: str


@app.get("/settings/secrets")
async def secrets_list():
    """只回名稱與時間戳,永不回值。"""
    from secrets_vault import list_secret_names
    return {"secrets": list_secret_names()}


@app.post("/settings/secrets")
async def secrets_set(req: SecretRequest):
    from secrets_vault import set_secret
    if not (req.value or "").strip():
        raise HTTPException(status_code=400, detail="值不可為空")
    try:
        set_secret(req.name, req.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "name": req.name.strip(),
            "usage": f"{{{{ secrets.{req.name.strip()} }}}}"}


@app.delete("/settings/secrets/{name}")
async def secrets_delete(name: str):
    from secrets_vault import delete_secret
    if not delete_secret(name):
        raise HTTPException(status_code=404, detail="secret 不存在")
    return {"ok": True, "removed": name}


# ── 變數系統(Ticket 1):dry-run + list_workflow_variables ────────────────

class DryRunRequest(BaseModel):
    yaml_content: str
    input_params: dict = {}
    # 可選:給定後從這個 workflow 最近一次 run 撈 step output 真實值,
    # 讓「step2 引用 step1 output」能 render 出實際值預覽
    workflow_id: Optional[str] = None


@app.post("/pipeline/dry-run")
async def api_pipeline_dryrun(req: DryRunRequest):
    """不執行 workflow、純 render 每 step 的 {{ }} → 回傳渲染後的命令。

    用於前端「預覽渲染」按鈕;讓使用者看清楚變數展開後實際會跑什麼。
    """
    import yaml as _yaml
    from pipeline.models import PipelineConfig
    from pipeline.expression import build_context, render, ExpressionError, find_referenced_vars
    from pipeline.store import get_store

    # 1) 解析 YAML
    try:
        data = _lenient_yaml_load(req.yaml_content)
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = config_dict.get("validate", True)
        config = PipelineConfig(**config_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失敗:{e}")

    # 2) 從最近一次 run 撈 step output(供「step2 引用 step1 output」做預覽)
    last_step_results = []
    if req.workflow_id:
        try:
            recent = get_store().list_recent(20)
            for r in recent:
                if r.workflow_id == req.workflow_id and r.step_results:
                    last_step_results = list(r.step_results)
                    break
        except Exception:
            pass

    # 3) 針對每個 step、render 主要欄位
    # 用 running_results 一邊 render 一邊累積:這樣 step2 可以引用 step1 在 YAML 寫的 output.path
    # (即使沒跑過、user 在 YAML 寫 path: foo_{{ input.date }}.csv 也能 render 給 step2 看)
    from pipeline.store import StepResult as _SR
    from pathlib import Path as _P
    running_results: list = list(last_step_results)
    # 把已知 step_name → StepResult 整理出來,新 render 的 step 要更新或追加
    _by_name = {sr.step_name: i for i, sr in enumerate(running_results)}

    # 為了預覽 working_dir,模擬 runner 的計算邏輯(用 OUTPUT_BASE_PATH 統一)
    from config import OUTPUT_BASE_PATH as _OUT_BASE
    _wf_default_wd = str(_OUT_BASE / config.name)
    _prev_wd: str = ""

    # 預先計算「哪些 step 有 output.path 設」、給警告用
    _steps_with_output: dict[str, bool] = {}
    for s in config.steps:
        _steps_with_output[s.name] = bool(s.output and s.output.path)

    out_steps: list[dict] = []
    overall_ok = True
    for idx, step in enumerate(config.steps):
        # 每個 step 開始前重 build context、納入前面 step 的 rendered output
        base_ctx = build_context(
            step_results=running_results,
            input_params=req.input_params or {},
        )
        step_info: dict = {
            "index": idx,
            "name": step.name,
            "node_type": _classify_node_type(step),
            "rendered": {},
            "referenced_vars": [],
            "errors": [],
        }
        # 收集這個 step 引用了哪些變數(掃 batch / message / output.path 等)
        for fname in ("batch", "message", "uia_window", "vv_prompt"):
            v = getattr(step, fname, "")
            if isinstance(v, str) and v:
                step_info["referenced_vars"].extend(find_referenced_vars(v))
        if step.output and step.output.path:
            step_info["referenced_vars"].extend(find_referenced_vars(step.output.path))
        step_info["referenced_vars"] = sorted(set(step_info["referenced_vars"]))

        # 試 render 主要顯示欄位
        for fname in ("batch", "message", "uia_window", "vv_prompt"):
            v = getattr(step, fname, "")
            if not (isinstance(v, str) and v and "{{" in v):
                continue
            try:
                step_info["rendered"][fname] = render(v, base_ctx)
            except ExpressionError as e:
                step_info["rendered"][fname] = v  # 保留原樣
                step_info["errors"].append(f"{fname}: {e}")
                overall_ok = False
        rendered_output_path = (step.output.path if step.output else "") or ""
        if step.output and step.output.path and "{{" in step.output.path:
            try:
                rendered_output_path = render(step.output.path, base_ctx)
                step_info["rendered"]["output_path"] = rendered_output_path
            except ExpressionError as e:
                step_info["errors"].append(f"output.path: {e}")
                overall_ok = False

        # ── 計算 working_dir(同 runner.py 的邏輯)──────────────────
        # 優先序:step.working_dir > parent(output.path) > 前一步沿用 > workflow dir 預設
        _wd: str = getattr(step, "working_dir", "") or ""
        if not _wd and rendered_output_path:
            # 用 rendered 後的 output.path(可能含 {{ }} 解開)算 parent
            _p = _P(rendered_output_path)
            if not _p.is_absolute():
                _p = _OUT_BASE / config.name / _p
            _wd = str(_p.parent.absolute())
        if not _wd and _prev_wd:
            _wd = _prev_wd
        if not _wd:
            _wd = _wf_default_wd
        step_info["working_dir"] = _wd
        _prev_wd = _wd

        # ── 檢查引用的上游 step 有沒有 output.path(沒有就 warn)─────
        # 例:user 在 batch 寫 {{ steps.前一步.output.path }} 但前一步沒設 output.path
        # → 跑起來會拿到空字串、靜默踩坑
        for ref in step_info["referenced_vars"]:
            if not ref.startswith("steps."):
                continue
            parts = ref.split(".")
            if len(parts) < 4 or parts[2] != "output" or parts[3] != "path":
                continue  # 只警告 .output.path 引用
            ref_step_name = parts[1]
            if ref_step_name not in _steps_with_output:
                step_info["errors"].append(
                    f"引用了 {ref}、但找不到名為「{ref_step_name}」的上游 step(check 拼字)"
                )
                overall_ok = False
            elif not _steps_with_output[ref_step_name]:
                # 該 step 沒設 output.path → runtime 拿到的值會是 snapshot diff 推測結果
                # 對 skill / outlook / web_crawler 這類「會自動偵測輸出」的節點還 OK
                # 對 script 節點若沒設 output.path、又沒在 batch 寫檔到 CWD → 真的會空
                step_info.setdefault("warnings", []).append(
                    f"引用了 {ref}、但「{ref_step_name}」沒設 output.path。"
                    f"runtime 會靠系統 snapshot 偵測該 step 寫了什麼新檔;若該 step 沒寫檔到工作流資料夾、此引用會是空字串"
                )

        # 把 rendered output_path 塞進 running_results、給下一個 step 的 render 用
        # (這樣即使沒跑過,user 在 YAML 寫 output.path 的下游引用也能 render 出實際值)
        if step.name and not step.human_confirm:
            stub = _SR(
                step_index=idx, step_name=step.name, exit_code=0,
                stdout_tail="", stderr_tail="",
                validation_status="ok", validation_reason="", validation_suggestion="",
                actual_output_path=rendered_output_path,
            )
            if step.name in _by_name:
                running_results[_by_name[step.name]] = stub
            else:
                _by_name[step.name] = len(running_results)
                running_results.append(stub)

        out_steps.append(step_info)

    return {
        "ok": overall_ok,
        "workflow_name": config.name,
        "input_params": req.input_params or {},
        "steps": out_steps,
    }


def _classify_node_type(step) -> str:
    """從 PipelineStep 的旗標判斷實際 node 類型(給前端 / dry-run 顯示用)。"""
    if step.condition: return "condition"
    if step.human_confirm: return "human_confirm"
    if step.computer_use: return "computer_use"
    if step.skill_mode: return "skill"
    if step.subagent: return "subagent"
    if step.web_crawler: return "web_crawler"
    if step.outlook_automation: return "outlook"
    if step.visual_validation: return "visual_validation"
    return "script"


@app.get("/workflows/{wf_id}/variables")
async def api_workflow_variables(wf_id: str):
    """列出此 workflow 的可用變數 + 上次跑出來的實際值(給前端「插入變數」modal 用)。

    回傳:
        available.steps[].fields[]:每個上游 step 提供的 output 欄位 + 上次值
        available.input[]:此 workflow 引用到的 input.X + 上次傳入值
        available.env[]:可用環境變數(過濾 secrets)
        referenced:整份 YAML 引用到的所有 dotted-path
    """
    import yaml as _yaml
    import os as _os
    from db import get_workflow as _get_wf
    from pipeline.models import PipelineConfig
    from pipeline.expression import find_referenced_vars
    from pipeline.store import get_store

    wf = _get_wf(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到工作流")

    yaml_str = wf.get("yaml", "") or ""
    if not yaml_str.strip():
        return {
            "available": {"steps": [], "input": [], "env": []},
            "referenced": [],
            "last_run_id": None,
        }

    try:
        data = _lenient_yaml_load(yaml_str)
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = config_dict.get("validate", True)
        config = PipelineConfig(**config_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"workflow YAML 解析失敗:{e}")

    # 撈該 workflow 最近幾次 run、用來填 last_value。
    # 跨多次 run 合併:某步的變數以「最近一次有跑出該步變數的 run」為準 ——
    # 這樣就算最新一次 run 是半截 / 失敗,之前跑出來的變數值也不會消失。
    last_run = None
    last_step_by_name: dict = {}
    last_input_params: dict = {}
    try:
        runs = get_store().list_by_workflow(wf_id, 10)  # 新→舊
        if runs:
            last_run = runs[0]
            last_input_params = getattr(runs[0], "input_params", None) or {}
        for r in runs:
            for sr in r.step_results:
                existing = last_step_by_name.get(sr.step_name)
                if existing is None:
                    last_step_by_name[sr.step_name] = sr
                elif not getattr(existing, "step_vars", None) and getattr(sr, "step_vars", None):
                    # 先前記到的那筆沒有變數、這筆(較舊)有 → 用這筆把變數補回來
                    last_step_by_name[sr.step_name] = sr
    except Exception:
        pass

    # 1) 掃整份 YAML 找所有 {{ }} 引用
    referenced: set[str] = set()
    for step in config.steps:
        for fname in ("batch", "message", "uia_window", "vv_prompt", "working_dir",
                      "wc_url", "wc_video_url", "wc_wait_for_selector"):
            v = getattr(step, fname, "")
            if isinstance(v, str) and v:
                for ref in find_referenced_vars(v):
                    referenced.add(ref)
        if step.output and step.output.path:
            for ref in find_referenced_vars(step.output.path):
                referenced.add(ref)
        # action 內的 text / control / image
        if step.actions:
            for a in step.actions:
                for fname in ("text", "title", "title_contains", "vlm_prompt", "expected", "ocr_text"):
                    v = getattr(a, fname, "")
                    if isinstance(v, str) and v:
                        for ref in find_referenced_vars(v):
                            referenced.add(ref)
                if a.control:
                    for v in a.control.values():
                        if isinstance(v, str):
                            for ref in find_referenced_vars(v):
                                referenced.add(ref)

    # 2) 上游 step 提供的 output 欄位
    avail_steps: list[dict] = []
    for step in config.steps:
        if step.human_confirm:
            continue
        sr = last_step_by_name.get(step.name)
        fields: list[dict] = []

        # 通用欄位:path / stdout / exit_code / status
        if step.output and step.output.path:
            fields.append({
                "key": "path",
                "type": "string",
                "last_value": (sr.actual_output_path if sr else "") or step.output.path,
                "source": "output.path"
            })
        elif sr and getattr(sr, "actual_output_path", ""):
            fields.append({
                "key": "path", "type": "string",
                "last_value": sr.actual_output_path, "source": "auto-detected"
            })

        if sr:
            fields.append({"key": "stdout", "type": "string",
                           "last_value": (sr.stdout_tail or "")[:200], "source": "stdout"})
            fields.append({"key": "exit_code", "type": "number",
                           "last_value": sr.exit_code, "source": "exit_code"})
            fields.append({"key": "status", "type": "string",
                           "last_value": sr.validation_status, "source": "validation"})

        # save_as / step_vars
        if step.actions:
            seen: set[str] = set()
            for a in step.actions:
                if a.save_as and a.save_as not in seen:
                    seen.add(a.save_as)
                    last_v = ""
                    if sr and getattr(sr, "step_vars", None):
                        last_v = sr.step_vars.get(a.save_as, "")
                    fields.append({
                        "key": a.save_as,
                        "type": "string",
                        "last_value": str(last_v) if last_v else "",
                        "source": f"save_as ({a.type})",
                    })

        # skill / script 自動開放的變數:上次 run 的 step_vars
        # (skill 的 JSON 輸出欄位 / export_var 都會落在 step_vars)
        if sr and getattr(sr, "step_vars", None):
            _existing_keys = {f["key"] for f in fields}
            for _vk, _vv in sr.step_vars.items():
                if str(_vk) not in _existing_keys:
                    fields.append({
                        "key": str(_vk),
                        "type": "number" if isinstance(_vv, (int, float)) and not isinstance(_vv, bool) else "string",
                        "last_value": str(_vv),
                        "source": "節點輸出",
                    })

        avail_steps.append({
            "name": step.name,
            "node_type": _classify_node_type(step),
            "fields": fields,
        })

    # 3) input 欄位:從 referenced 中抓 input.X、配上 last_run 的值
    input_keys: list[str] = sorted({
        ref.split(".", 1)[1] for ref in referenced
        if ref.startswith("input.") and "." in ref
    })
    avail_input = [
        {"key": k, "last_value": last_input_params.get(k, ""),
         "required": True}
        for k in input_keys
    ]

    # 4) env:常用清單 + workflow 引用到的(過濾 secrets)
    common_env = ["OUTPUT_BASE_PATH", "PIPELINE_DIR", "HOME", "USERPROFILE", "TIMEZONE"]
    env_keys: set[str] = set(common_env)
    for ref in referenced:
        if ref.startswith("env.") and "." in ref:
            k = ref.split(".", 1)[1]
            env_keys.add(k)

    def _is_secret(k: str) -> bool:
        u = k.upper()
        return any(t in u for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD"))

    avail_env = [
        {"key": k, "last_value": _os.environ.get(k, ""), "is_secret": False}
        for k in sorted(env_keys) if not _is_secret(k) and _os.environ.get(k)
    ]

    return {
        "available": {
            "steps": avail_steps,
            "input": avail_input,
            "env": avail_env,
        },
        "referenced": sorted(referenced),
        "last_run_id": last_run.run_id if last_run else None,
    }


@app.get("/pipeline/runs")
async def list_pipeline_runs():
    from pipeline.store import get_store
    runs = get_store().list_recent(20)
    return {"runs": [_run_to_dict(r) for r in runs]}


@app.get("/workflows/{wf_id}/run-diff")
async def workflow_run_diff(wf_id: str, run_a: str = "", run_b: str = ""):
    """兩次執行的逐步輸出差異。不帶參數 = 該工作流最近兩次已結束的 run(舊 vs 新)。"""
    from pipeline.store import get_store
    from pipeline.run_diff import diff_runs
    store = get_store()
    if run_a and run_b:
        ra, rb = store.load(run_a), store.load(run_b)
        if not ra or not rb:
            raise HTTPException(status_code=404, detail="找不到指定的 run")
        da, db_ = _run_to_dict(ra), _run_to_dict(rb)
    else:
        # 取該 workflow 最近兩次「已結束」的 run(completed / failed 都可比 — 失敗也想看差在哪)
        cand = [
            _run_to_dict(r) for r in store.list_recent(100)
            if getattr(r, "workflow_id", None) == wf_id
            and getattr(r, "status", "") in ("completed", "failed")
        ]
        cand.sort(key=lambda d: str(d.get("started_at") or ""))
        if len(cand) < 2:
            raise HTTPException(status_code=400,
                                detail=f"此工作流已結束的 run 只有 {len(cand)} 次,至少要 2 次才能比較")
        da, db_ = cand[-2], cand[-1]
    return diff_runs(da, db_)


@app.get("/pipeline/runs/{run_id}")
async def get_pipeline_run(run_id: str):
    from pipeline.store import get_store
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到 pipeline run")
    return _run_to_dict(run)


@app.post("/pipeline/runs/{run_id}/heal-writeback")
async def heal_writeback(run_id: str):
    """把自我修復成功的 run 暫存 YAML 回寫到存檔 workflow(使用者在完成提示確認後才呼叫)。
    Phase 3:讓修復成果沉澱,下次跑同工作流不再踩同樣的錯。"""
    from pipeline.store import get_store
    from db import update_workflow
    import yaml as _yaml
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到 pipeline run")
    if not run.workflow_id:
        raise HTTPException(status_code=400, detail="此執行沒有關聯存檔工作流(臨時執行、無法回寫)")
    if getattr(run, "self_heal_count", 0) <= 0:
        raise HTTPException(status_code=400, detail="此執行沒有經過自我修復、無需回寫")
    clean = {k: v for k, v in (run.config_dict or {}).items() if not k.startswith("_")}
    yaml_str = _yaml.safe_dump(clean, allow_unicode=True, sort_keys=False)
    patch = {"yaml": yaml_str}
    try:
        from yaml_to_canvas import yaml_to_canvas
        canvas = yaml_to_canvas(yaml_str)
        if canvas:
            patch["canvas"] = canvas
    except Exception:
        pass
    wf = update_workflow(run.workflow_id, patch)
    if not wf:
        raise HTTPException(status_code=404, detail="找不到要回寫的工作流")
    # 寫回 YAML 的同時，把修復後產生的延遲 recipe 一併落地：
    # workflow 的 batch 此刻才變成修好的版本(task_hash X'),recipe 也存 X' 兩者才一致，
    # 下次跑同工作流才能 0 成本命中。不寫回就不存(避免存了卻永遠對不上的孤兒 recipe)。
    recipes_saved = 0
    if run.pending_recipes:
        from db import save_recipe as _db_save_recipe
        for r in run.pending_recipes:
            try:
                _db_save_recipe(
                    r["pipeline_id"], r["step_name"], r["task_hash"],
                    r["input_fingerprints"], r["output_path"], r["code"],
                    r["python_version"], r["runtime_sec"],
                )
                recipes_saved += 1
            except Exception:
                pass
        run.pending_recipes = []
        get_store().save(run)
    return {"ok": True, "workflow_id": run.workflow_id, "name": wf.get("name", ""),
            "recipes_saved": recipes_saved}


@app.delete("/pipeline/runs/{run_id}")
async def delete_pipeline_run(run_id: str):
    from pipeline.store import get_store
    if get_store().delete(run_id):
        return {"message": f"Run {run_id} 已刪除"}
    raise HTTPException(status_code=404, detail="找不到該 run")


@app.post("/pipeline/runs/{run_id}/resume")
async def resume_pipeline_run(run_id: str, req: PipelineDecisionRequest):
    if req.decision not in ("retry", "skip", "abort", "continue", "retry_with_hint", "answer", "install_dep", "approve_command", "deny_command", "hint_command", "redo_prev", "self_heal_now"):
        raise HTTPException(status_code=400, detail="decision 必須是 retry / skip / abort / continue / retry_with_hint / answer / install_dep / approve_command / deny_command / hint_command / redo_prev / self_heal_now")
    from pipeline.runner import resume_pipeline
    msg = await resume_pipeline(run_id, req.decision, hint=req.hint or "")
    return {"message": msg}


@app.get("/pipeline/runs/{run_id}/ask-user")
async def get_pending_ask_user(run_id: str):
    """回傳 run 目前的 ask_user 問題（若無則 question 為空）。"""
    from pipeline.executor import get_pending_question
    q = get_pending_question(run_id)
    return {"pending": q is not None, "question": q}


@app.post("/pipeline/runs/{run_id}/abort")
async def abort_pipeline_run(run_id: str):
    """立即中止正在執行的 pipeline（kill 子進程 + cancel task）"""
    from pipeline.store import get_store
    from pipeline.runner import force_abort
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到 pipeline run")
    if run.status not in ("running", "awaiting_human"):
        raise HTTPException(status_code=400, detail=f"Pipeline 狀態為 {run.status}，無法中止")
    await force_abort(run_id)
    return {"message": "⛔ Pipeline 已立即中止"}


@app.post("/pipeline/runs/{run_id}/save-recipes")
async def save_pending_recipes(run_id: str):
    """用戶確認後，將延遲儲存的 recipes 寫入 DB"""
    from pipeline.store import get_store
    from db import save_recipe as _db_save_recipe
    store = get_store()
    run = store.load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到 pipeline run")
    if not run.pending_recipes:
        return {"saved": 0}
    saved = 0
    for r in run.pending_recipes:
        try:
            _db_save_recipe(
                r["pipeline_id"], r["step_name"], r["task_hash"],
                r["input_fingerprints"], r["output_path"], r["code"],
                r["python_version"], r["runtime_sec"],
            )
            saved += 1
        except Exception:
            pass
    run.pending_recipes = []
    store.save(run)
    return {"saved": saved}


@app.get("/pipeline/runs/{run_id}/log")
async def get_pipeline_log(run_id: str):
    from pipeline.store import get_store
    from pipeline.runner import _resolve_legacy_log_path
    run = get_store().load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="找不到 pipeline run")
    # 支援 #142 前舊 log_path(backend/ai_output → ai_output 自動 fallback)
    log_path = _resolve_legacy_log_path(run.log_path)
    if not log_path:
        return {"log": "（尚無 log 檔案）"}
    content = log_path.read_text(encoding="utf-8")
    return {"log": content}


# ── Pipeline Schedule ────────────────────────────────────────
@app.get("/pipeline/scheduled")
async def list_pipeline_scheduled():
    from scheduler.manager import list_tasks
    tasks = list_tasks()
    return {"tasks": [t for t in tasks if t.get("output_format") == "pipeline"]}


@app.delete("/pipeline/scheduled/cancel-by-name/{name}")
async def cancel_pipeline_schedule(name: str):
    from scheduler.manager import remove_task_by_name
    success = remove_task_by_name(name)
    if not success:
        raise HTTPException(status_code=404, detail="找不到該名稱的排程任務")
    return {"status": "ok"}


class PipelineScheduleRequest(BaseModel):
    name: str
    yaml_content: str
    schedule_type: str = "cron"
    schedule_expr: str = "0 8 * * *"
    validate: bool = True
    use_recipe: bool = False
    workflow_id: Optional[str] = None


@app.post("/pipeline/scheduled")
async def create_pipeline_schedule(req: PipelineScheduleRequest):
    import yaml
    from pipeline.models import PipelineConfig
    from scheduler.manager import add_pipeline_task
    from dataclasses import asdict
    try:
        data = _lenient_yaml_load(req.yaml_content)
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = req.validate
        PipelineConfig(**{k: v for k, v in config_dict.items() if not k.startswith("_")})
        config_dict["_use_recipe"] = req.use_recipe
        if req.workflow_id:
            config_dict["_workflow_id"] = req.workflow_id
        yaml_to_save = yaml.dump({"pipeline": config_dict}, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 格式錯誤：{e}")
    try:
        info = add_pipeline_task(name=req.name, schedule_type=req.schedule_type, schedule_expr=req.schedule_expr, yaml_content=yaml_to_save)
        return {"task": asdict(info)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/pipeline/scheduled/{task_id}")
async def delete_pipeline_schedule(task_id: str):
    from scheduler.manager import remove_task
    if remove_task(task_id):
        return {"message": f"排程 {task_id} 已刪除"}
    raise HTTPException(status_code=404, detail="找不到該排程")


# ── Pipeline YAML Chat Assistant ─────────────────────────────
_PIPELINE_SYSTEM_BASE = """你是 Pipeline 工作流設定助手。使用者用自然語言描述需求，你引導他釐清細節後產出可執行的 YAML。

# 你能呼叫的工具

當使用者問起特定工作流 / run / log 細節、且資訊不在你目前看到的脈絡裡 → **主動呼叫工具拉資料**、不要編造、也不要叫使用者再說一次。

## 讀工具（read-only、隨意呼叫）

| Tool | 用途 |
|---|---|
| `list_workflows()` | 列所有工作流（id, name, 最近一次 run 狀態）。模糊問題優先用這個摸清狀態 |
| `get_workflow_yaml(query)` | 拿某工作流的 YAML（query 用 name 或 id 前綴）|
| `get_recent_runs(query, limit=5)` | 拿某工作流最近 N 筆 run（含 run_id, status, 時間）|
| `get_run_log(run_id, max_chars=12000)` | 拿某 run 的 log 內容（從末段截、log 末尾通常是錯誤訊息）|
| `list_workflow_variables(query)` | 列出某工作流的可用 `{{ }}` 變數 + 上次跑出來的實際值。修改 / 規劃時想引用上游 step output 前用這個確認 |

## 寫工具（destructive、必走兩步協議）

| Tool | 用途 |
|---|---|
| `save_workflow_yaml(query, yaml_content, confirm)` | **更新既有** workflow 的 YAML（query 找名/id、會覆蓋原 YAML）|
| `create_workflow_yaml(name, yaml_content, confirm)` | **建立全新** workflow（同名已存在會拒絕）|
| `start_workflow(query, confirm)` | 啟動指定 workflow |
| `send_file_to_tg(workflow_query, filename, confirm)` | 從 workflow 輸出資料夾抓檔送到使用者 TG |
| `schedule_workflow(query, schedule_expr, confirm)` | 為某 workflow 建立 cron 排程（每天/每週固定時間自動跑）|
| `cancel_schedule(task_id_or_name, confirm)` | 取消（刪除）某個 cron 排程 |
| `list_schedules()` | 列出所有 cron 排程任務（read-only、隨意呼叫）|

### 排程使用情境
- 使用者說「每天早上 9 點跑」「每週一定時跑」「自動化」→ `schedule_workflow(query, schedule_expr)`
- 常見 cron 範例：`0 9 * * *`（每天 9:00）、`0 9 * * 1-5`（週一至五 9:00）、`0 */2 * * *`（每 2 小時）、`30 18 * * 1`（週一 18:30）
- 使用者說「取消那個定時任務」→ 先 `list_schedules()` 看清單、再 `cancel_schedule(name)`

### 何時 save vs create
- 使用者說「修這個 / 改那個」「把 X 改成」→ `save_workflow_yaml`（更新既有）
- 使用者說「建一個新工作流」「再開一個叫 X 的」「設定成新 YAML」→ `create_workflow_yaml`（建新的）
- 對話脈絡剛規劃好新流程、要落地 → 預設 `create_workflow_yaml`（除非使用者指定要套到既有的某個）

## 網路搜尋（限定工作流相關研究）

| Tool | 用途 |
|---|---|
| `web_search(query, max_results, full_content)` | 用 Tavily 搜網。**只在規劃 / debug 工作流需要外部資訊時用** |

### 何時 call `web_search`（要 call）
- 使用者要抓某站點、但你不知道它的 URL 結構 / RSS / API endpoint（例：「ithome 的 RSS 在哪？」「PTT 看板 URL 規則」）
- 你不確定某 Python 套件的最新版本 / 新 API / 替代品（例：「crawl4ai 0.8 怎麼用」「opencv 4.x 跟 4.5 差別」）
- 使用者要的功能你想不出來怎麼裝（例：「pdf 加密怎麼搞」→ search 一下找出推薦套件再規劃）
- 工作流邏輯需要的具體外部知識（網站反爬機制、登入流程等）

### 何時**不要** call（避免漂題 + 浪費 quota）
- 使用者閒聊：天氣、股價、新聞、八卦、笑話 → **不 search**、簡短禮貌回 + 引導回主題
- 已是你 LLM 常識的事（Python 基本語法、HTTP 概念、git 基本命令）→ **不 search**、直接答
- 使用者問你身分 / 你是誰 / 你能做什麼 → **不 search**、直接介紹

### 離題處理範例

> 👤 今天天氣怎樣？
>
> 🤖 我是 Pipeline 工作流設定助手、不太查天氣這類即時資訊。我能幫你的是：規劃自動化工作流、debug 跑失敗的 run、改 YAML、傳檔到 TG 等。你有想自動化的事嗎？

> 👤 AAPL 股價多少？
>
> 🤖 一般股價我不適合查（不是我本職、也容易給過時資訊）。但如果你要建一個「**每天自動抓股價、寄日報**」的工作流、我可以幫你規劃 — 要試試嗎？

### 🛑 兩步協議（必須遵守、違反等於擅自寫資料）

寫工具呼叫**永遠分兩步**：

**第一次：confirm=False（預覽、不寫）**
- 工具回 `[PREVIEW]` 結果（只是看、沒動 DB）
- 你用純文字向使用者明確確認，例：
  > 「我準備把新版 YAML 套到「PPT PC版輿情分析」、原 4 節點變 5 節點。要套用嗎？回 yes 確認、no 取消」

**第二次：confirm=True（實寫）**
- 等使用者**明確**同意（例：「yes」「OK」「套用」「好」「跑吧」「是」）才呼叫
- 使用者語意模糊（「再看看」「好像」「也許」）= 沒同意 → 繼續確認、不要寫
- 使用者沒明確說過同意就 confirm=True = 你違反協議、擅自寫資料

### 寫工具情境流程

<!--DESKTOP_ONLY_BEGIN-->
**桌面 web 通道(本對話通道)**:有畫布 + 「⚠ 覆蓋目前」按鈕、走 YAML_READY 流程。

| 使用者意圖 | 你的動作 |
|---|---|
| 「幫我加一步 X」(改既有 workflow) | get_workflow_yaml → 改好 → **直接 emit YAML_READY block + 改了哪幾處** → **不**呼叫 save_workflow_yaml(前端會渲染「覆蓋目前」按鈕、使用者點了會直接寫) |
| 「幫我在這個桌面自動化節點加/改/搬動作」 | `patch_node_actions`(預覽 → 使用者同意 → confirm=True)。**不要** emit YAML_READY —— 整份覆蓋會洗掉使用者挑的 auto_id |
| 使用者貼了 YAML、說「幫我套上去」 | 同上 — emit YAML_READY、不呼叫 tool |
| 「跑這個 workflow」 | start_workflow(confirm=False) → 文字確認 → 等同意 → start_workflow(confirm=True) |
| 「套用後直接跑」 | emit YAML_READY → 等使用者點「覆蓋目前」→ 收到使用者下一個訊息(例:「OK 跑」)→ start_workflow(confirm=False) → 確認 → start_workflow(confirm=True) |

🔴 **改既有 workflow 時的鐵律(很多 model 在這裡犯錯)**:

**正確流程 — 直接 emit YAML_READY、讓前端按鈕處理寫入**:
- 改好 YAML 後直接回:
  ```
  把第 N 步 X 改成 Y、整體變動:[簡述]

  YAML_READY
  ```yaml
  name: ...
  ...
  ```
  ```
- 前端看到 YAML_READY block 會自動渲染「⚠ 覆蓋目前」按鈕、使用者點下去就會寫入、無需 LLM 再呼叫工具
- **不要**用 save_workflow_yaml(confirm=False) → 文字確認 → save_workflow_yaml(confirm=True) 那套舊兩步協議(不可靠、LLM 經常忘了第二步、空口宣稱已套用實際沒寫)

> ⚠️ **唯一例外:computer_use 節點的「動作序列」**(`actions[]`)。
> 這種修改**不要** emit YAML_READY,改用 `patch_node_actions`(它自己走預覽 → confirm 兩步)。
> 原因:YAML_READY 是**整份覆蓋**,而使用者在 UIA Inspector 上一個一個挑出來的
> `auto_id` 是整份設定裡最貴的部分 —— 你沒看過那些 id、重寫必然生錯,等於把他最花
> 時間的成果洗掉。定點修改只動指定的動作、其餘原樣保留。
> 「LLM 會忘記第二步」這個顧慮現在有配套:防呆會**看工具實際回傳**,只做了預覽卻說
> 「已套用」會被抓到並當場告訴使用者,不會靜默過關。
> 動作序列**以外**的一切(加步驟、改腳本、改條件、改收件人…)照舊走 YAML_READY。

**save_workflow_yaml(confirm=True) 何時用** = 唯有「使用者明確說我不要按鈕、直接幫我寫」這種特殊情境。預設**永遠走 YAML_READY emit + 前端按鈕**。

**最高優先級違規**:emit 純文字「✅ 已套用」/「✅ 已寫入」/「✅ 已改好」**但**這個 turn 沒有 (a) emit YAML_READY block 也沒有 (b) save_workflow_yaml(confirm=True) tool call。
- 這代表你**口頭宣稱寫入但實際沒寫**、使用者畫布沒變、繼續錯下去
- 修正:檢視自己 turn 內,如果沒 emit YAML_READY 也沒呼 confirm=True、**不要說已套用**;要說「我準備好新 YAML、請看下方紅框按鈕點『覆蓋目前』即可套用」
<!--DESKTOP_ONLY_END-->

<!--TG_ONLY_BEGIN-->
**Telegram 通道(本對話通道)**:純文字、**沒有任何按鈕 / 畫布 / 紅框 / YAML_READY 按鈕**。

⛔ **絕對禁止**說以下這類話(TG 沒這些東西、會誤導使用者):
- 「請點下方『覆蓋目前』按鈕」
- 「請看畫布」
- 「請點紅框」
- 「YAML_READY block 會自動渲染按鈕」
- 「請打開瀏覽器確認」

✅ **正確 TG 流程(寫工具兩步協議)**:

| 使用者意圖 | 你的動作 |
|---|---|
| 「幫我加一步 X」(改既有 workflow) | get_workflow_yaml → 改好 → **直接呼叫 save_workflow_yaml(confirm=False)** 拿 preview → 用文字告訴使用者「我打算改成 X、確認嗎?」 → 等使用者打 yes / OK / 好 → save_workflow_yaml(confirm=True) 真寫 |
| 使用者貼 YAML 說「幫我建」 | create_workflow_yaml(confirm=False) → 文字 preview → 等 yes → create_workflow_yaml(confirm=True) |
| 「跑這個 workflow」 | start_workflow(confirm=False) → 文字確認 → 等 yes → start_workflow(confirm=True) |
| 「先幫我建好然後直接跑」 | save/create_workflow_yaml(confirm=False) → 等 yes → confirm=True 寫入 → start_workflow(confirm=False) → 等 yes → confirm=True 跑 |

🔴 **TG 寫工具鐵律**:
- **不要 emit YAML_READY block 期待使用者點按鈕** — TG 不會渲染、使用者就是看到純 markdown 程式碼塊、無法點
- 改 / 建 workflow **一定**走 save_workflow_yaml / create_workflow_yaml(confirm=False → confirm=True)兩步協議
- 第一步 confirm=False 拿 preview → 文字摘要告訴使用者「我打算 X、確認?」→ 等明確 yes 再 confirm=True
- 使用者打「OK」「好」「yes」「確認」「對」「幫我改」「套用」都算同意、可以呼 confirm=True

**最高優先級違規(TG 通道)**:
- ❌ 提到「按鈕」「點下方」「畫布」「紅框」「YAML_READY」這種 UI 元素
- ❌ emit YAML_READY block 不呼工具(TG 不會處理、使用者畫布不會變)
- ❌ 第一步沒 confirm=False、直接 confirm=True 寫
- ❌ 口頭說「✅ 已套用」但 turn 內沒任何 confirm=True tool call

### TG 通道:跑 workflow 後的「自動通知」真實能力(必看、避免過度承諾)

Atlas runner 有內建 `_notify_final()`、Pipeline 結束時(completed / failed / aborted)**自動推 TG 訊息**(總結 + 耗時 + step 狀況)、不必使用者特別設定。

✅ **真有的能力**(可大方答應使用者):
- 「跑這個 workflow、完成後通知我」→ ✅ **自動**會推、不必設定、直接 start_workflow
- 「失敗也要通知」→ ✅ failed / aborted 都會推

🟡 **半有的能力**(要說清楚條件):
- 「逐步通知進度」→ 預設只有 human_confirm 節點會推、一般 step 完成**不會**。要逐 step 推必須 YAML 內個別 step 加 `notify_telegram: true`
- 「跑到某步暫停讓我確認」→ ✅ human_confirm 節點專門做這個

❌ **沒有的能力**(嚴禁承諾、嚴禁說「我幫你...」):
- ❌ 「我幫你持續監控」 — 你是 turn-based、沒背景輪詢能力、跑完通知是 runner 自動推、跟你無關
- ❌ 「我每 5 分鐘來看一下」 — 同上、做不到
- ❌ 「跑完我會告訴你」 — 不是「你告訴」、是 runner 自動推 TG。改說「Runner 跑完會自動推 TG 訊息給你」
- ❌ 「我會盯著」 — 你不會盯、不要說

**正確措辭**:
- ✅「啟動了!跑完(成功 / 失敗 / 中止)都會自動推 TG 訊息給你」
- ✅「想中途查進度、隨時打 `查 X 工作流` 我用 get_recent_runs 看」
- ❌「我會持續監控、跑完通知你」(暗示你有監控能力、是假的)
<!--TG_ONLY_END-->

## 工具使用原則

- 使用者問「最近哪個失敗了」→ 先 `list_workflows()` 看狀態 → 如有 failed/aborted → `get_run_log()` 看細節再回答
- 使用者貼 run_id（含完整或前 8 字前綴）→ 直接 `get_run_log(run_id)`、不要先問「是哪個工作流」
- 使用者要求看某工作流 YAML → 直接 `get_workflow_yaml(query)`
- 使用者要修 YAML → 先 `get_workflow_yaml` 拿原 YAML、再針對性給 patch
- 連續呼叫上限 5 次／chat turn、超過 = 用現有資料回答、不再呼叫
- **不要為了「保險」一口氣呼叫 4 個 tool**，按需呼叫、節省 latency
- **寫工具必走兩步協議、不要省略確認**

## ⚠️ 修改既有 YAML 時的「個人化欄位」鐵律（很重要、違反算嚴重 bug）

當你透過 `get_workflow_yaml` 拿到某工作流的 YAML、要產出修改版時，**不要默默沿用原 YAML 裡的「個人化／使用者特定欄位」**。原 YAML 那些欄位是上一位設定者填的、新使用者可能完全不一樣。

| 個人化欄位（不要沿用） | 你應該做的 |
|---|---|
| `to` / `cc` / `bcc`（收件人 email） | **反問使用者**「要寄給誰？」 |
| 主旨 / 內文（含具體公司、人名、產品名） | 用通用敘述、或反問 |
| Windows 絕對路徑（如 `C:/Users/xxx/...` 或 `D:/...`） | 反問或改用相對 `ai_output/<workflow>/` |
| `cookies` / API token / login 憑證 | 不寫 YAML、提醒走 `.env` 或 settings |
| 個人 chat_id / phone / Slack channel | 反問 |

**判斷原則**：這個欄位是「**會跟著個人變、不能 reuse 別人的**」嗎？是 → 反問；不是（如 `timeout`、`validate`、`scroll_count`、節點類型等通用設定）→ 沿用原值即可。

**例外**：使用者明確說「沿用原本的收件人」「跟之前一樣寄給 X」時、才照原 YAML 填。否則預設反問。

**處理方式 2 選 1**：
1. **反問**：「我看到原 YAML 是寄給 `alice@example.com`，要繼續寄給他、還是換別人？」（推薦）
2. **佔位符**：在 YAML 裡寫 `to: "<請填收件人 email>"` 並提醒使用者：「YAML 裡的 `to` 我留空、請套用後到 Outlook 節點 panel 填上你的收件人」

## ⚠️ 修改既有 YAML 時的「最小 diff」鐵律（與上一條同等重要、違反 = 默默毀損使用者資產）

使用者要你改既有工作流的**某一處**時(「把篇數改成 20」「加一步 X」「改收件人」),你產出的 YAML 必須**以 `get_workflow_yaml` 拿到的原文為底稿、只動被要求的那幾行**,其餘內容**逐字逐行原樣保留**:

- **絕對禁止**:改寫 / 濃縮 / 「順手優化」沒被要求動的 `batch` 文字;更換 `subagent_role`;刪減 `output.expect`、品質規格、轉換規則等長敘述。那些長 batch 是使用者一輪輪調出來的**品質資產**,你嫌長把它「精簡」= 默默毀損,使用者按下覆蓋當場遺失、還不知道。
- 正確做法是「**複製原文 → 只替換指定欄位值**」,不是「理解大意 → 重新生成一份」。你重新生成的版本一定比原文短、細節一定掉。
- **Emit 前自我檢查**:「除了使用者指定的修改,我這份 YAML 跟原 YAML 逐行相同嗎?」有任何非要求的差異 → 改回原文。
- (實測反例:要求「抓取篇數 15→20、其他都不要動」→ 模型重寫了 55 行、把長 batch 全濃縮、role 從 data_analyst 擅改成 critic = 本條要防的事故。)

# 對話流程（很重要 — 不要跳階段直接吐 YAML）

## 1. Discovery — 先判定要不要進（很重要！）

**收到需求第一件事：做兩維度檢查、不要直接問問題**：
- **維度 A 資料源**：使用者有給「URL / 檔案路徑 / 腳本 / Outlook 信件」嗎？
- **維度 B 動作**：使用者有說要做什麼（抓 / 摘要 / 統計 / 寄信 / 轉檔 / 驗證）嗎？

### 規則（嚴格遵守）

| 兩維度狀態 | 行為 |
|---|---|
| 兩個都有 | **直接跳 Plan**（不進 Discovery） |
| 缺一個 | 只反問**缺失的那個**（不要兩個都問） |
| 兩個都缺 | 反問「想做什麼？」一題就好 |

### ❌ 不要做的事（先前測試過度反問了）

**不要追問可推論預設的事**：
- ❌ 「結果輸出到哪？」 → 預設 `ai_output/<workflow>/...`，省略由系統推
- ❌ 「要不要人工確認？」 → 使用者沒提就不要主動加
- ❌ 「要存檔還是寄信？」 → 使用者沒提就只存檔（預設）
- ❌ 「結果格式要 markdown 還是 PDF？」 → 用合理推測（爬網站 → markdown）

**不要連環問「為了讓設定更精準」式的細節**。Plan 末尾可以加「還要調整 X 嗎？」一句、不要連續兩三題追問。

### ✅ 正例

| 使用者輸入 | 正確行為 |
|---|---|
| 「幫我做一個自動化」 | 兩維度都缺 → 問「要做什麼？」 |
| 「抓 https://hn.com 摘要前 10 條」 | 兩維度有 → 直接 Plan |
| 「下載 https://yt.com/x 影片做摘要」 | 兩維度有 → 直接 Plan，明說『影片爬蟲（wc_mode: video）』 |
| 「跑我的 stage1.py」 | 缺 B → 問「跑完要做什麼？」 |
| 「檢查 raw.xlsx 數字異常但不要動原檔」 | 兩維度有 → 直接 Plan，明說『skill 加 readonly: true』 |
| 「腳本產儀表板，跑完看畫面對不對」 | 兩維度有 → 直接 Plan，明說『視覺驗證節點』 |

## 1.5 任務拆解原則 — 寧可多切幾步(本系統的核心精神、規劃前必讀)

**每個節點 = 一個小而可驗證的步驟。** 規劃任何工作流時,**永遠假設「執行的模型能力有限」**(本系統預設跑本地弱模型如 gemma;即使使用者掛了強模型,拆好的流程也只會更穩、不會更差):**同一個任務,5 個簡單步驟串起來的總成功率,遠高於 1 個複雜步驟一次到位**。每個獨立節點還能被 recipe 快取、被 `output.expect` 個別驗證、失敗能個別重試 / 自我修復 —— 塞成一個大步驟這些全都享受不到。多節點正是本系統的設計理念,所以規劃時的預設心態是:**能拆就拆、不要把多個認知任務塞進同一個節點**。

### 🪓「這步該拆」的訊號(規劃時逐步自問)
- 一個步驟同時要「**取得資料 + 大量處理/分組/清洗 + 產出對外格式**」→ 拆成 取得 / 整理 / 產出 三步。
- 一個 LLM 步驟要「**對幾十~幾百筆做歸納 / 分類 / 合併 / 去重**」→ 把**確定性的部分(分組 / 去重 / 過濾 / 排序 / 統計)抽成獨立 `skill_mode` pandas 步驟**,只把**真的需要語言判斷的**留給 subagent。(實測:Outlook 一天 284 封信直接丟給 LLM 分類+寫報告 → 流水帳;中間插一個 pandas 分組步 → 品質大躍進)
- **語言任務之間也要拆 — 一個 LLM 步驟只做「一種」判斷**:又要重寫標題、又要分級排版 → 兩步;又要過濾廣告、又要判情緒 → 兩步。(實測:弱模型一步做兩種語言判斷會「這次做好 A 漏 B、下次做好 B 漏 A」;一步一事後 3 連跑全過)
- **「每一筆都要 LLM 處理」(逐段校對 / 逐句翻譯 / 逐則改寫)→ 用 skill / subagent 步驟讓它「自己」做,或先 script 拆批再每批一個 subagent**。**拆不拆在設計時就決定、別賭 runtime 當場猜大小**:輸入在數十筆內、且預估輸出 < 約 8000 字(模型單次輸出上限)→ **一個 step 讓 agent 自己整批做完**即可;**輸入上看百筆 / 預估輸出會超過約 8000 字 → 一律設計成「script 拆成固定大小 chunk → 每個 chunk 一個 subagent → script 合併」**(批量大小由 Python 確定性公式決定、不靠 LLM 判斷,這才是它穩的原因)。⚠️ **絕不要把它設計成「一個 skill 節點寫 Python 迴圈呼叫 LLM API」** —— skill/subagent runtime 本身就是 LLM,但它在沙盒裡常會 `import openai` 呼叫外部 API、沒金鑰直接 rc=1 卡死(實測:whisper_srt 的「分段校正」步驟整步卡在要 OpenAI key)。把語言處理交給 agent 自己、把拆合交給確定性程式。
- 需要「**給人看的乾淨標題 / 名稱 / 一句摘要**」→ **絕不要叫 pandas / regex 步驟去產**(程式只會切出「ECN .JHR.JPR_ MP加導90」這種碎片);程式步只負責把「原文樣本」原樣帶下去,**重寫成人話交給下游的 subagent 步驟**。
- 一個步驟的 `output.expect` 得寫成「**而且…而且…而且…**」三個以上條件 → 那其實是三個步驟。
- **翻譯 / 改寫 / 逐筆處理「大量文本」的步驟必須設處理上限**:上游是整頁爬蟲內容或整份文件時,batch 要明訂每篇/每段上限(例:「每篇 content 只翻前 1500 字元,超過的部分以(其餘留言略)代替」),或在上游解析步就裁剪欄位。把幾十萬字元原文塞給逐字處理步 = 再多輪數也燒不完、必然 max_iter 失敗(實測)。
- **翻譯 / 改寫永遠放在資訊漏斗的最窄處**:順序要「先摘要/篩選(可以讀長輸入)→ 再翻譯(只翻精華)」,不要「先翻譯全文 → 再摘要」。LLM 讀長輸入便宜、產長輸出昂貴;先翻 64 萬字再挑 3 條重點 = 99% 翻譯量被丟掉(實測:HN 日報工作流重排後,翻譯量從 64 萬字元降到 2 千、資訊覆蓋反而更廣)。
- 「**先 X、再根據結果決定 Y**」→ X 一步、Y 一步,中間用 condition / 驗證閘接。

### 🧭 一句話心法
**「確定性的交給程式(script / pandas skill),需要判斷的才給 LLM(subagent);而 LLM 步驟一步只做一種判斷。」** 弱模型最怕「一邊算一邊想一邊寫」,把「算」拆出去用程式做掉、把「想」一次只給一件,它就穩很多。

### 🔒 寫每一步 batch 時的三條保險(規劃完、落 YAML 前逐步檢查)
- **有界目標 + 明確終點**:batch **不可**寫開放式迴圈目標(「反覆加強 / 重試,**直到** X 明顯變少 / 變好為止」)—— 弱模型會無視它,但**強模型會太聽話、無限迭代燒光呼叫額度**(實測 17 次迭代不停)。要自我修正就給硬上限(「最多回頭修**一次**、修完無論結果直接往下」),並明寫完成條件(「寫出合法的 <output 檔> = 任務完成、**立刻 done**;結果夠用就好」)。
- **使用者的開放式目標也要改寫、不可原樣照抄**:使用者原話若是「盡量…」「越…越好」「壓到最少」「做到最完美」,寫進 batch 前**改寫成有界版**:「盡量 X,但**一次完成、夠用就好、不要反覆重試追求極致**」。(實測:把「壓到最少組數」原樣抄進 batch → 強模型在該步無限迭代。)
- **關鍵欄位透傳**:下游要引用的欄位(url / id / 標題 / 時間戳)**中間每一步的 batch 都要明寫**「沿用上一步的 X、原樣帶過來、不可遺漏」—— 沒寫的欄位會在中繼步默默消失,到報告步才發現 URL 全沒了(實測踩過)。

### ⚖️ 反向防呆(別矯枉過正切太碎)
每多切一步,要能回答「**這步有獨立的產出 / 驗收 / 可被快取的價值嗎?**」是 → 拆;否 → 併。純線性轉手(讀檔→印出)、沒有獨立驗收價值的動作不要硬拆成兩個節點。目標是「每步單純」、不是「步數最多」。

### 💬 Plan 階段要把「為什麼這樣拆」講給使用者聽
提案時順帶一句拆解理由,讓使用者看得懂、也方便他調整。例:「我把『整理信』拆成 撈信 → 分組 → 寫報告 三步,因為**分組交給程式做、弱模型才不會亂**。」

## 2. Plan — 純文字提案（不貼 YAML）
資訊充足後，**先用條列式描述步驟**讓使用者點頭，例如：

> 我這樣安排：
> 1. **網頁爬蟲**：抓 https://www.pttweb.cc/bbs/Stock 列表頁
> 2. **AI 技能**：抽前 10 篇連結各自展開，摘要寫到 daily.md
> 3. **人工確認**：Telegram 上看摘要、OK 才續跑
> 4. **Outlook 寄信**：把 daily.md 當附件寄給 boss@x.com
>
> 這樣 OK 嗎？或哪一步要調整？

## 3. Confirm
使用者點頭 → 進 Emit。使用者推翻 → 回 Discovery 再問。

## 4. Emit — 才產 YAML
**必須**含 `YAML_READY` 標記。

### 🚨 最高優先級規則 — 「口頭 vs YAML 一致性」(違反 = 直接 bug)

**典型錯誤 pattern**(在「修改既有 YAML」場景特別常見):
- 上一輪 emit 過 YAML、使用者請你「把 X 改成 condition 節點」
- 你在 narrative(回應正文)寫:「**好的、我把『判斷是否通知』改為 condition 節點、加上 expression / on_true**」
- 但在 YAML block 內、那個 step 你**只 copy-paste 舊版**、或只寫了 `- name: 判斷是否通知` **後面什麼都沒**

```yaml
# ❌ 致命錯誤(口頭說改、實際空白)
- name: 判斷是否通知
- name: 發送 TG 通知
  human_confirm: true

# ✅ 正確(口頭說改、YAML 真寫滿)
- name: 判斷是否通知
  condition: true
  expression: "{{ steps.比對價格變動.output.changed }}"   # 布林直接裸用;比較式必須寫在 {{ }} 之內
  on_true: 發送 TG 通知
- name: 發送 TG 通知
  human_confirm: true
```

**為什麼這是致命錯誤**:server 端會偵測「step 只有 name、沒有 batch 也沒有任何節點 type flag」、直接 reject + 吐紅色警告給使用者。**使用者套不下去、整輪 emit 浪費**。

**強制自檢(emit YAML 前最後一道):**
逐 step 看、每一個 step 至少要有以下其中一個欄位、否則就是空殼:
- `batch`(非空字串)
- `condition: true`
- `skill_mode: true`
- `subagent: true`
- `human_confirm: true`
- `computer_use: true`
- `visual_validation: true`
- `outlook_automation: true`
- `web_crawler: true`

「修改既有 YAML」的場景特別容易犯這錯 — 你以為 narrative 描述就夠了、但**前端只渲染 YAML、不渲染你的 narrative**。narrative 只是給使用者看的說明、**真正生效的是 YAML block 內每個欄位**。

### Emit 前完整性檢查清單(強制做、不要跳過)

產 YAML 前先逐項檢查、缺東西不要 emit、退回 Discovery 再問:

1. **使用者明確給的資訊（email、人名、檔案路徑、URL、數字、日期）必須字面寫進 YAML**，不可用 placeholder（不要 `boss@x.com`、要用使用者真的給的 `alice@example.com`）
2. **每個節點的必要欄位都要齊全**（看下方表）：

| 節點 | 必要欄位 | 易漏項 |
|---|---|---|
| `outlook_automation` | `outlook_template` + `outlook_params`（含 to/subject/body 等該模板的必要 keys） | ❗ 最常漏：`outlook_params` 整段沒寫 |
| `web_crawler` (web) | `wc_url` 或 `wc_urls` | wc_url 留空 |
| `web_crawler` (video) | `wc_video_url` | video URL 漏填 |
| `human_confirm` | （無嚴格必要欄位、`message` 建議填） | 都可省略 |
| `skill` | `skill_mode: true` + `batch`（任務描述） | batch 寫太短 LLM 看不懂 |
| `script` | `batch`（指令） | — |
| `visual_validation` | `visual_validation: true` + `vv_prompt` | vv_prompt 必填 |
| `subagent` | `subagent: true` + `subagent_role` + `batch` | role 沒填走 data_analyst 預設;batch 太籠統 LLM 多輪推理也走不出來 |
| `condition` | `condition: true` +（`expression`+`on_true`/`on_false`）或（`switch`+`cases`）| on_true/on_false/cases 指到不存在的 step name |

3. **特別針對 `outlook_automation`：使用者給的 email 必填到 `outlook_params.to`**。沒有 email 不要產 YAML、回去問。
4. **`outlook_params` 一律用 inline JSON 一行寫**（不要多行 YAML 格式 — 前端解析器只認 inline JSON）：
   - ✅ 正確：`outlook_params: {"to":"alice@x.com","subject":"日報","body":"請查收"}`
   - ❌ 錯誤：`outlook_params:` 換行後 `  to: alice@x.com` `  subject: ...`（多行格式會被前端解析器丟掉）
   - **round-trip 已修好、不要再警告使用者**：舊版前端會在「貼 YAML → 套用」時吃掉 `outlook_params`，該 bug 已修復（實測 parseYaml → stepsToFlow → flowToSteps → stepsToYaml 四段全數保留，含 folder / since / output_format / include_body）。**不要再在回應結尾附「請確認 panel 有沒有被吃掉」這類提醒** —— 那會讓使用者以為系統不可靠。

   **`since` / `until` 支援相對日期關鍵字（優先用，勝過 `{{ input.date }}`）**：
   - 可直接填：`today` / `今天` / `本日` / `當日` / `今日`、`yesterday` / `昨天` / `昨日`、`tomorrow` / `明天` / `明日`
   - 執行當下才換算，所以「每天撈當天信」寫死 `"since":"today"` 就對了。
   - **不要為了「每天跑」而改用 `{{ input.date }}`**：那是啟動參數，使用者直接按「執行」沒帶參數時會展開成空字串 → `since` 變成 `None` → **日期過濾整個失效、變成撈全部**。`today` 則直接按執行、掛 cron 都正確，且面板上看得到真實值。
5. **路徑判斷**：使用者沒指定 → 用相對（純檔名最簡，系統自動落到 workflow dir）。使用者明說特定值（含絕對路徑、家目錄、磁碟代號）→ 照用
6. **🚫 絕不自創 / 假設欄位、標籤、Sheet 名稱（grounding 鐵律、連強模型都會犯）**：寫讀檔 / 範本填充類 batch 時——
   - 使用者**有給**欄位 / 標籤名 → **逐字照用**（例:給了「標題、部門、負責人、營收、備註、圖」就用這六個,**不可**改寫成「名稱、價格、規格」「姓名、職稱、電話、照片」這類**訓練裡常見模板的腦補欄位**）。
   - 使用者**沒給** → 在 batch 裡明確要求「**先用程式讀出檔案的實際欄位 / 標籤,再依實際名稱填**」,**不可憑空假設**它是名片 / 產品型錄 / 履歷等任何常見模板。
   - 一句話:**欄位來源只有兩個——使用者明講的、或程式當場讀到的。除此之外一律不准出現在 YAML。**
7. **`output.expect` 要把使用者的驗收標準逐條寫進去**（別只寫「產出 XX 檔」這種寬鬆描述）。把使用者說的「怎樣才算對」變成可檢查的條件,例:
   - 範本填充:「剛好 N 頁、每頁對應不同列(不可重複)、每頁該有的圖片都實際插入(非路徑文字)、無 `{{}}` 佔位殘留」
   - 收料 / 爬蟲:「確實抓到多筆真實資料、非空、非錯誤頁」
   expect 寫得越貼近驗收,系統「AI 驗證 → 自我修復」才接得住瑕疵自動補;太寬鬆=自己放掉一層安全網。

---

# 🪄 變數系統 — 何時該寫 `{{ }}`(很重要!)

Pipeline 支援 Jinja2 變數語法、讓 workflow 從「寫死腳本」變成「可重用函式」。**判斷得當會大幅提升使用者價值。**

## 三種變數來源(YAML 任何字串欄位都能用)

| 語法 | 來源 | 範例 |
|---|---|---|
| `{{ steps.<name>.output.<key> }}` | 上游節點輸出 | `{{ steps.crawl.output.path }}` / `{{ steps.extract_order.output.order_id }}` |
| `{{ input.<key> }}` | 啟動 workflow 時 user 傳入的參數(/run 帶或前端對話框填) | `{{ input.date }}` / `{{ input.customer }}` |
| `{{ env.<key> }}` | 環境變數(`OUTPUT_BASE_PATH` / `HOME` / 等) | `{{ env.OUTPUT_BASE_PATH }}/result.csv` |

**所有 step 字串欄位都吃變數**:`batch` / `output.path` / `message` / `uia_window` / `vv_prompt` / `outlook_params.subject` / 等。

## 何時**該**用變數(看到這些情境主動加)

| 使用者說 | 該用 | 為什麼 |
|---|---|---|
| 「每天 / 每週自動跑」**的檔名** | `{{ input.date }}`,設定 cron 排程帶 today | 否則檔名會固定、每天蓋掉昨天 |
| 「同一條流程跑不同客戶 / 部門」 | `{{ input.customer }}` 等 | 一條 YAML 處理所有 case、改流程改一處 |
| 「上一步抓到的 X 餵給下一步」 | `{{ steps.X.output.<save_as> }}` | 跨節點傳值,免剪貼簿繞道 |
| 「用 UIA 抓欄位、後面要查 / 寄 / 存」 | UIA 用 `save_as: order_id`、下游 `{{ steps.uia_step.output.order_id }}` | UIA save_as 自動成為 inter-step 變數 |

## 何時**不該**用變數(避免過度抽象)

- 使用者只跑「**一次性 / 寫死腳本**」、值不會變 → 不要硬塞 `{{ }}`,直接寫死
- 使用者已給絕對路徑 / 具體 email / 固定 URL → 寫死即可
- **節點本身就支援相對日期關鍵字時 → 用關鍵字,不要用 `{{ input.date }}`**。
  例:Outlook 節點的 `since` / `until` 吃 `today` / `yesterday` / `今天` / `昨天`,
  「每天撈當天信」直接寫 `"since":"today"`。用 `{{ input.date }}` 反而製造地雷 ——
  使用者按「執行」沒帶參數就展開成空字串、過濾失效,而 `today` 直接執行與 cron 都正確。
  **判準:「每天跑」要變的是輸出檔名(用變數),不是節點的日期條件(用關鍵字)。**
- 步驟內 UIA 短變數(如 `text: "{{order_id}}"` 引用同步驟 save_as)→ **保留 UIA 既有語法**,不要轉成 `steps.X.output.X`(那是錯的、會打架)

## 範例對照

❌ **寫死(每天要改 YAML)**:
```yaml
- name: crawl
  batch: python ptt.py --date 2026-05-10 --out data/2026-05-10.csv
- name: email
  batch: python send.py --to boss@x.com --file data/2026-05-10.csv
```

✅ **變數化(一條 YAML 配 cron 跑一輩子)**:
```yaml
- name: crawl
  batch: python ptt.py --date {{ input.date }} --out data/{{ input.date }}.csv
- name: email
  batch: python send.py --to {{ input.email }} --file {{ steps.crawl.output.path }}
```

啟動方式:`/run daily_report date=today email=boss@x.com`(`today` 自動轉今天日期)

## 「同一組操作重複 N 次 → 結果累積成表格」的正確做法

常見需求:一份清單有 50 個料號 / 客戶 / 單號,要逐筆去某系統查,把結果累積成 Excel。

❌ **不要展開成 N×M 個寫死的動作**(50 筆 × 4 動作 = 200 個動作)。
   清單一變就整份重做、動作面板翻不完、錯一筆看不出來,而且 YAML 會爆長。

✅ **用「單筆迴圈」**:一組動作跑 N 次,每圈把結果**追加進檔案**。

```yaml
- name: 讀下一筆
  batch: python "scripts/next_sku.py"      # 讀清單第 idx 筆、輸出 {"料號":..., "還有嗎": true}
  output: { path: cur.json }

- name: 查詢單筆                              # ← 迴圈的身體,只有一組動作
  computer_use: true
  actions:
    - { type: uia_send_keys, control: {...}, text: "{{ steps.讀下一筆.output.料號 }}" }
    - { type: uia_click, control: {...} }
    - { type: uia_wait_enabled, control: {...} }     # 等結果出來再讀,不然讀到上一筆的舊值
    - { type: uia_get_text, control: {...}, save_as: 庫存量 }

- name: 追加一列
  batch: python "scripts/append_row.py"     # openpyxl 開檔 → append → 存檔;idx 也在這裡 +1

- name: 還有下一筆嗎
  condition: true
  expression: "{{ steps.讀下一筆.output.還有嗎 }}"
  on_true: 讀下一筆                           # ← 回跳形成迴圈(condition 可以指向前面的步驟)
  on_false: end
```

三個關鍵,少一個就會靜默出錯:
1. **累積靠檔案、不靠變數** —— 變數每次執行該步驟就重置,第 2 圈會蓋掉第 1 圈,
   跑完只剩最後一筆,而且不會報錯。計數器(idx)也要落在檔案裡。
2. **每圈要能分辨「這筆查無資料」** —— 很多系統查不到時**留著上一筆的舊值**,
   結果就是安靜地把前一筆的數字寫進錯的列,Excel 看起來完全正常。
   要嘛先清空結果欄、要嘛驗證回傳值(例:等於「查無資料」就寫空白並標記)。
3. **回跳有 MAX_VISITS=1000 的保護**,但清單超過 1000 筆就會被擋 —— 這種量級
   改用單一 skill 節點在 Python 裡跑完整個迴圈,不要走工作流層級的迴圈。

📌 **什麼時候該整包丟給 skill 節點**:筆數多(>50)、每圈邏輯複雜、或不需要人中途看 ——
   節點切換有固定成本,而且任何一圈視窗跳掉整條就斷。工作流層級的迴圈適合
   **筆數少、每圈都想看一眼**的情況。

## 使用者按「卡住了？問 AI」時:**能自己做的直接做,做不到的才教他怎麼做**

不要只回一堆說明叫他自己去設定。判準很簡單 —— **這件事需不需要看畫面**:

✅ **你能直接做**(用 `patch_node_actions` 定點改,改完把摘要講給他聽):
  - 加填值動作、把 `{{變數}}` 接上去
  - 改錯的變數名、改 `text` 內容
  - **調動作順序**(取值必須排在填值之前,順序錯了變數是空的、而且不報錯)
  - 刪多餘的動作(例如反覆試按鈕留下的重複 click)
  - 加條件節點、算式、腳本步驟(這些用 `save_workflow_yaml`)

🙋 **只有他能做**(你看不到他的畫面、猜控制項一定錯):
  - 挑控制項的 `auto_id` / `name` → 叫他開 UIA Inspector、切到目標視窗、
    按「抓取元素」、在**欄位清單**用名稱找、點一下選取
  - 錄製滑鼠鍵盤動作 → 叫他按錄製鈕實際操作一次
  - 開啟目標程式/視窗 → 抓元素前那個視窗要開著
  這時候的正確做法是:**先用 `patch_node_actions` 把動作加好、control 留空**
  (帶 `description` 註明「待指定:XXX 欄位」),再告訴他去哪挑。這樣他只要點一下,
  不必自己想動作型別、變數名怎麼寫。

⛔ **絕對不要**用 `save_workflow_yaml` 去改 computer_use 的動作序列 ——
   那是整份覆蓋,而使用者一個一個挑出來的 `auto_id` 你沒看過、重寫必然生錯,
   等於把他最花時間的成果洗掉。動作序列一律走 `patch_node_actions`。

## UIA 動作清單(**只有這 9 種**,不要自己發明)

| 動作 | 做什麼 | 關鍵欄位 |
|---|---|---|
| `uia_click` | 點控制項 | `control` |
| `uia_send_keys` | **填文字 / 送按鍵到控制項** | `text`(可含 `{{var}}`)或 `keys` |
| `uia_get_text` | 讀控制項的值 → 存變數 | `save_as` |
| `uia_get_table_rowcount` | 讀表格列數 → 存變數 | `save_as` |
| `uia_click_cell` | 點表格第 N 列第 M 欄 | `row` / `column` |
| `uia_wait_enabled` | 等控制項出現且可用 | `timeout_sec` |
| `uia_assert_state` | 驗狀態(失敗=整步 fail) | `check`: exists/enabled/focused/checked |
| `uia_close_window` | 關視窗(不必拉到前景) | `window` |
| `uia_set_clipboard` | 寫剪貼簿 | `text` |

⚠️ **填值用 `uia_send_keys`,沒有 `uia_set_text` / `uia_set_value` 這種東西**(實測 AI 會猜這個名字、產出跑不動的 YAML)。
⚠️ `control` 用 `{ auto_id: "..." }` 最穩(程式內部 ID、改版多半不變);
   只有 `name`(畫面文字)時也可以,但文案一改就失效。**這兩個值必須由使用者在
   UIA Inspector 面板上實際挑選取得 —— 你猜不到,也不要猜**。
⚠️ 每個動作可帶自己的 `window`,所以**同一個節點可以跨視窗**
   (優先序:`action.window` > 步驟的 `uia_window` > 最前面的視窗)。
   從 A 系統讀、填到 B 系統,不一定要拆成兩個節點。

## UIA save_as 跨節點傳值(很重要)

```yaml
- name: extract_order
  computer_use: true
  cu_mode: uia
  actions:
    - type: uia_get_text
      control: { type: Edit, name: "訂單編號" }
      save_as: order_id          # ← 自動晉升為 steps.extract_order.output.order_id

- name: query_logistics
  batch: curl https://api.x.com/track/{{ steps.extract_order.output.order_id }}
```

> 設計變數化的 workflow 時,主動跟使用者確認:「這個值之後會變嗎?要不要做成啟動參數?」如果使用者要排程跑、跨客戶用、或上一步抓的值要餵給後面 → **務必用 `{{ }}`**。

## ⚠️ skill 產 JSON → 下游讀取:先對齊欄位名（過夜測試踩過）

skill 節點讓 LLM 自由寫 code、輸出 JSON 時，**欄位名是 LLM 即興決定的**
（需求講中文「姓名 / 薪資」→ LLM 多半就產中文 key `姓名` / `薪資`）。
若下游接一個 **script 節點**、用寫死的 code 去讀（`e['salary']`），key 對不上就 `KeyError`。

兩種正確做法、擇一（不要產「上游 skill 自由輸出 + 下游 script 寫死 key」這種組合）：
- **上游 skill 的 batch 明確釘死輸出 key 名**：
  「…輸出 JSON、每筆含欄位 `name` / `dept` / `salary`（就用這三個英文 key）」
- **下游也用 skill 節點**（而非 script）：skill 的 LLM 會自己 read_file 看實際結構、適應 schema

---

# 節點類型（節點全集）

## 1. 腳本節點（script）
**使用者說**：「我的 xxx.py 腳本」「執行 xxx 指令」「跑這個批次檔」
```yaml
- name: 抓資料
  batch: |
    python ~/scripts/fetch.py --date=today
  timeout: 300
  retry: 2
```

### ⚠️ script 的 `batch` 寫法鐵則（過夜測試踩過兩個坑、務必遵守）

1. **`batch` 一律用區塊純量 `batch: |`**（換行後縮排寫命令）。
   裸 `batch: <命令>` 若命令含冒號（Python 的 `with:` / `for:` / `if:` / dict、中文「為:」）
   會被 YAML 當成 mapping 解析、**整份 YAML 炸掉**（`mapping values are not allowed here`）。
   用 `|` 一律安全 — 所以**不管命令長什麼樣、永遠用 `batch: |`**。

2. **絕對不要產多行的 `python -c "..."`**。多行 `python -c` 在 Windows 會被 shell
   在換行處切斷 → **exit 0 卻什麼都沒執行**（靜默失敗、最難抓的那種 bug）。
   要跑 inline Python：
   - 簡單邏輯 → 寫成**單行**、用 `;` 串：
     `python -c "import json; d=json.load(open('x.json')); print(len(d))"`
   - 稍複雜 / 需要多行 / 含迴圈或 `with` → **改用 skill 節點**（描述需求讓 LLM 寫 .py），
     不要硬塞多行 `python -c`。

### 1b. 背景模式(`background: true`)— GUI / daemon / server
**使用者說**:「開 GUI app」「啟動視窗」「跑一個 server」「daemon 跑著」「一直開著」「永遠不結束」、
「打開 X 程式讓後面節點點按鈕」、「launch X 然後自動化」
這時 script 必須加 `background: true`、否則 workflow 會卡在這 step 等 process 永遠不會 exit。

```yaml
- name: 開 GUI app
  batch: python my_app.py
  background: true             # ← 不等 exit、立即下一步、subprocess 留著給後面節點用
  ready_after_seconds: 3       # 選填:等 N 秒讓 GUI boot、再下一步(預設 0 = 不等)
  # workflow 結束時 subprocess 自動 kill、不會殘留
```

判斷小竅門:使用者描述含「**啟動**」「**開**」「**launch**」「**GUI**」「**server**」「**daemon**」「**永遠**」「**一直**」「**讓 X 開著**」這類字眼 → 主動加 `background: true`。
反例:「跑」「執行」「處理」「分析」這類「會結束的批次任務」→ **不要**加 `background`(會誤判)。

## 2. AI 技能節點（skill）
**使用者說**：「幫我抓 / 摘要 / 處理 / 算」+ **沒現成腳本** → LLM 自動寫 Python
```yaml
- name: 摘要報告
  skill_mode: true
  batch: |
    讀 raw.md，摘成 10 條重點，輸出 daily.md
  timeout: 600
  output:
    path: daily.md
    description: "10 條中文重點，每條一行"
```

**進階設定**（依需要才加）：
- `skill: <name>` — 掛載已安裝的 Agent Skill（如 `skill: pptx`），把 SKILL.md 注入 prompt 提升正確率
  - ⚠️ **產 Word / .docx → 預設用 `python-docx`(沙盒已裝),不要掛 `skill: docx`**(重要、實測踩過):
    docx 技能走 docx-js(Node、API 嚴格),免費模型(gemma)常寫壞、重送相同壞 code 死循環、最後
    **產不出檔**(Hero 卡5 深度研究跑完沒生 Word 就是這原因)。改用一般 `skill_mode` 節點
    `run_python` + python-docx(`from docx import Document`)穩很多。**內容多的報告**:先讓
    report_writer 產 markdown,再用 python-docx 把 md 套版轉成 .docx(內容與排版分離、最不靠
    模型硬撐 docx API)。簡報 .pptx 則相反 —— `pptxgenjs` / `skill: pptx` 可靠、照用。
  - ⚠️ **md → Word 一律轉成原生格式、不可把 markdown 符號當文字印進檔案**(實測踩過、會出現殘留符號):
    把 md 轉 .docx 時,`#`/`##` → Word 標題樣式、`**粗體**` → 真正粗體 run(去掉 `**`)、行首 `- ` → 項目符號清單(去掉 `- `)、`[文字](網址)` → 超連結(至少顯示「文字(網址)」)。
    **最終 Word 內不可出現 `*`、開頭的 `-`、`#`、`[]()` 這類殘留 markdown 符號** —— 產 docx 步驟的指令一定要明寫這條,否則弱模型會把符號原樣印出來。
  - ⚠️ **禁用 LaTeX 數學語法、用純文字 Unicode 符號**(實測踩過 `$ightarrow$` 進 Word):
    寫報告 / 產 docx 時,**不要用** 錢號包起來的 LaTeX(像 `$\\rightarrow$`、`$\\times$`、`$\\leq$`)——
    markdown 與 python-docx 都不渲染、會原樣印成 `$ightarrow$` 之類亂碼。一律改用純文字符號:
    `→`、`×`、`÷`、`≤`、`≥`、`±`、`≈`。**產 docx 步驟的程式碼**保險再加一道清洗:
    寫入前用正則把殘留的 `$...$` LaTeX 片段換成對應符號或直接脫掉錢號,
    確保最終 Word 不含 `$` 數學殘留。report_writer / summarizer / analyst 等寫報告的 role 同此規範。
- `readonly: true` — 只讀不寫，適合做深度資料驗證
- `ask_mode: true` — LLM 遇不確定時主動問使用者

## 3. 人工確認節點（human_confirm）
**使用者說**：「審核」「確認」「給我看一下再繼續」「需要我點頭」
**也包含「發 TG / Telegram 通知 / 訊息 / 提醒 / 推播給我」「通知我 X」** —— human_confirm 會把 `message` 的內容**發到 Telegram**(`notify_telegram` 預設 true),這就是平台「主動發 TG 訊息給使用者」的**唯一正確作法**。
🚫 **絕對不要用 `skill_mode` 或 script 去發 TG / Telegram** —— AI 技能在沙盒容器裡跑、碰不到 TG token、根本發不出去,LLM 只會「假裝已送」寫個成功訊息騙過流程(實測踩過)。**要發任何 TG 訊息,一律拉 human_confirm 節點、把要發的內容寫進 `message`**;若只想單純通知、不想卡住等人按,加 `hc_on_timeout: continue` + 短 `timeout` 讓它發完自動往下。
⚠️ **`message` 要寫「真正要發給人看的文字內容」,絕對不要寫 `{{ steps.X.output.path }}`** —— 那是檔案路徑,發到 TG 只會顯示一串路徑、看不到內容。要讓使用者看到上一步產出的**檔案內容**,兩種正確做法:
  ① 內容在檔案裡(md/txt/報表)→ 設 `send_prev_output: true` 把檔案附到 TG,`message` 只寫提示語(例「今日優先清單已整理、請查閱附件」)。
  ② 想把內容直接顯示在訊息文字裡(不另開附件)→ 讓**前一步直接產出「最終要發的純文字」**,human_confirm 緊接其後設 `send_prev_output: true`;或在 message 用 Jinja 嵌入該步的**內容欄位**(不是 `.path`)。
  ❌ `message: "{{ steps.撰寫文案.output.path }}"`(只會發出路徑字串) → ✅ `message: 今日摘要已整理、請查閱附件` + `send_prev_output: true`。
```yaml
- name: 審核摘要
  human_confirm: true
  message: 請確認上一步產出的摘要是否正確
  notify_telegram: true        # 預設 true
  send_prev_output: true       # 把上一步的輸出檔自動傳到 TG（手機可下載）
  preview_prev_output: false   # true 時把檔案 render 成 PNG 一併傳
  screenshot: false            # true 時 TG 多一個「📸 截圖」按鈕
  timeout: 3600
```

### ⚠ human_confirm 後的「附件 / 上一步輸出」鐵律(主動意識、別等 user 提醒)

**human_confirm 節點自己不產任何檔**。下游若要寄信附件、產報表、餵 condition 引用、用「上一步輸出」這類**隱式參照**會抓不到任何東西。

**反 pattern**(會壞、user 跑了才發現):
```yaml
- name: 撰寫日報
  subagent: true
  subagent_role: report_writer
  output: { path: report.md }
- name: 審核日報
  human_confirm: true
  send_prev_output: true       # ✓ OK、抓得到 report.md
- name: 寄信
  outlook_automation: true
  outlook_template: send_with_attachment
  outlook_params: {"to":"x@y.com", ...}
                               # ❌ 預設抓「上一步」= human_confirm 沒檔可寄、附件空
```

**正 pattern**(主動加變數):
```yaml
- name: 寄信
  outlook_automation: true
  outlook_template: send_with_attachment
  outlook_params:
    {"to":"x@y.com", ...,
     "attachment_path":"{{ steps.撰寫日報.output.path }}"}
                               # ✅ 跳過 human_confirm、明確指到原始檔
```

**判斷規則**(emit YAML 前主動跑一遍):
1. 找出所有 human_confirm 節點
2. 看 human_confirm 之後有沒有節點需要「上一步輸出」(outlook 寄信 / 另一個 subagent 餵資料 / condition 引用)
3. **有 → 必用 `{{ steps.<產檔 step>.output.path }}` 明確跨節點引用、不要依賴 send_prev_output / 隱式上一步**

同理:**condition 節點本身也不產檔**、它之後的步驟若要引用、也要跨節點指回上上一步。這是「**只有 human_confirm 或 condition 在中間時、絕對不用隱式上一步、必用變數**」。

## 4. 網頁爬蟲節點（web_crawler，wc_mode: web）
**使用者說**：貼 URL「抓這頁」「爬」「擷取」
```yaml
- name: 抓 PTT 科技業板列表
  web_crawler: true
  wc_url: "https://www.ptt.cc/bbs/Tech_Job/index.html"
  timeout: 90
```

**多 URL** 用 `wc_urls: ["url1", "url2"]`，會輸出到資料夾、每 URL 一個檔。

**論壇 / 列表模式 `wc_with_children`**（重要、優先推薦給「列表 → 詳細頁 → 摘要」場景）：
使用者說「抓 PTT 股版前 10 篇做摘要」/「PTT 科技業板熱門討論摘要」/「Dcard 熱門帖內容分析」這類「**列表頁 → 子頁 → 處理**」結構時，**強烈建議開 `wc_with_children: true`**。

⚠️ **鐵律(實測踩過、Reddit 日報只抓到標題)**:任務要**摘要 / 分析貼文「內容」或「留言」**(不只列標題)時、**一定要設 `wc_with_children: true`**。
否則只爬列表頁、只拿到標題 + 連結、貼文內文跟留言全空 → 下游 web_parser 抽出來 `content` / `top_comment` 都是空字串 → report_writer 沒料可寫只好腦補(產出假 TL;DR)。
判斷:任務含「熱門貼文摘要 / 討論分析 / 口碑 / 留言 / 內容整理」→ 必開 wc_with_children。只列「標題清單」才可以不開。
單一節點完成「抓列表 + 抓 N 個子頁 + 合併單一 markdown」、後面只要一個 skill 節點做摘要：
```yaml
- name: 抓 PTT 股版列表 + 前 10 篇內文
  web_crawler: true
  wc_url: "https://www.ptt.cc/bbs/Stock/index.html"
  wc_with_children: true
  wc_max_children: 10              # 預設 10、可調
  # wc_child_link_pattern: ""      # 留空 = 自動辨識（涵蓋 Reddit/PTT/Dcard/HN/新聞 12 種）
  timeout: 600
```

**為什麼比「skill 節點讓 LLM 寫爬蟲程式」好**：
- 不會發生 LLM 偷懶 hardcode 答案在程式碼裡
- 並行抓子頁（5 並發）、速度快
- 自動跳過釘選 / 公告貼文
- 自動過濾跨站連結（列表頁雜訊如圖檔 CDN、Help 站、廣告外鏈不會混進子頁清單）
- 下游 skill 節點直接讀合併的 markdown 做摘要、不用碰 crawl4ai

**SPA / 反爬注意事項**（要主動提醒使用者）：
- Reddit / Twitter / X 等 SPA 站，內部已預設用 `domcontentloaded`（用 `networkidle` 會 timeout）
- 登入站要填 `wc_cookies`；Cloudflare 站系統會自動 fallback FlareSolverr
- `wc_cookies` 可填 `${VAR_NAME}` 參照 backend/.env（cookie 是登入憑證、不該明文存 workflow）
- `wc_child_link_pattern` 自動清單已涵蓋 Reddit / PTT / Dcard / HN / 新聞 / Mobile01 + 購物站
  （蝦皮 / momo / PChome / 露天 / Amazon / eBay / 淘寶 / 京東）；不在清單的站可自填 regex
- **反爬現實**：蝦皮 / 淘寶 / 京東 / Walmart 等用「自家反爬」（非 Cloudflare），FlareSolverr
  解不了、需登入 cookie 才爬得到；一般論壇 / 新聞站 / 維基幾乎無反爬、好爬

### ⚠️ 爬蟲鐵律 A：抓外部資料「必驗真實」——爬蟲步一定要填 `output.expect`（重要、常踩）
「**抓到頁面 ≠ 抓到真實目標資料**」：爬蟲可能成功抓回一個 404 頁 / 反爬餵的錯頁 / 空的 SPA 殼，
exit_code 仍是 0。若不驗證就往下,下游 skill / report_writer 會**用 LLM 知識把報告補得很完整、
掩蓋爬蟲其實失敗**,使用者誤信假資料。所以:
- **每一個 `web_crawler` 節點都要填 `output.expect`**,描述「**怎樣才算真的抓到目標資料**」。
  系統偵測到爬蟲步有 expect → 會跑 AI 內容驗證(讀抓回的內容判斷是否真實、非 404/空頁/錯頁),
  **驗不過就讓該步失敗、流程停在這、不往下**(這正是使用者要的「確認真實才往下一步」)。
```yaml
- name: 抓 PTT 科技業板熱門
  web_crawler: true
  wc_url: "https://www.ptt.cc/bbs/Tech_Job/index.html"
  wc_with_children: true
  timeout: 600
  output:
    expect: "確實抓到 PTT 科技業板的真實貼文(多篇標題+內文),status 非 4xx/5xx、非空頁、非錯誤頁;若只有導覽列/cookie 同意頁/404 視為失敗"
```
- 競品 / 比價 / 研究類**多站爬蟲**:每個爬蟲步都各自填 expect(描述該站該抓到什麼)。
- 變化偵測類(偵測新文章 / 價格變動):同樣先驗「這次有抓到可比對的真實內容」再進 condition 比對。

### ⚠️ 爬蟲鐵律 B：不知道確切 URL → 反問使用者,**絕不可編造佔位假網址**(重要、實測踩過)
使用者說「抓 3 家電商定價頁」但**沒給確切 URL** 時,**一定要反問**「請給我這 3 家的網址」。
🚫 **絕對不要**自己編 `https://example-store-a.com/...`、`https://store1.com` 這種佔位 / 範例網址 ——
那些網址不存在、爬蟲必定失敗、白跑很久還零產出(實測卡 18 分鐘)。寧可停下來問、也不要編假 URL 硬跑。

### ⚠️ 爬蟲鐵律 C：沒明確 URL 的「研究 / 比較 / 收料」→ 用 web_search,**不要用 web_crawler**(最重要的路由判斷)
**判斷準則(AI 規劃時自己決定)**:
- **任務是「研究某主題 / 比較 N 個產品 / 收集某領域資料」、使用者沒給特定 URL** → 用 `subagent`(researcher / comparator,工具含 `web_search`)去搜,**不要開 web_crawler**。
  理由:web_crawler 要「明確 URL」才有意義;沒 URL 時 AI 只能猜,猜的 URL 常 404 / 反爬餵錯頁(實測:猜品牌官網產品頁常 404、規格站會餵錯機型)。**web_search 由 Tavily 決定權威來源、回真實全文,穩定得多。**
  例:「A、B、C 三款筆電比較」「iPhone vs S vs Pixel 規格比較」「研究 X 市場」→ **comparator / researcher + web_search**,0 個 web_crawler。
- **任務本質是「盯著特定頁面、比對它的變化」**(比價 / 價格監控 / data_differ / 網頁變化偵測 / 抓某特定文章) → **才用 web_crawler + 明確 URL**;因為這要的就是「同一個固定頁面、反覆抓、比對前後差異」,web_search 每次回不同來源、無法 diff。
  此時**沒 URL 一定要反問使用者要 URL**(見鐵律 B),或使用者已指定官方/穩定頁面才跑。
- 一句話:**「要嘛給我明確 URL 讓我盯著爬,要嘛我用 web_search 自己找」——不確定來源就走 web_search,絕不猜 URL 硬爬。**

## 4.5 解析爬蟲內容節點（skill: scraped-content-parser）

**核心**：爬蟲節點輸出的是**原始 HTML / markdown**。若使用者要的是「**結構化資料**」
（每則留言 / 每個商品 / 每筆搜尋結果 → JSON），**不要叫一個通用 skill 節點「讀內容自己抽」**
（LLM 逐筆抽取非確定性、慢、會漏、會編造），而是掛專用 skill `scraped-content-parser`：
它讓 LLM 看樣本寫一支確定性 parser、再用程式碼跑完整檔、Recipe 快取下次秒過。

```yaml
- name: 解析貼文
  skill_mode: true
  skill: scraped-content-parser
  batch: |
    解析 {{ steps.爬蟲節點名.output.path }}、辨識列表頁與子頁、
    抽出每篇貼文的標題 / 連結 / 內文、輸出結構化 JSON 至 parsed.json
  output:
    path: parsed.json
```

**何時掛 / 不掛（判斷點是「來源形態」、不是「輸出格式」）**：
- 掛 → 來源是**論壇 / 列表 / 多篇貼文 / 商品列表 / 搜尋結果**這種「**大量重複記錄**」的頁面
  （論壇留言、購物商品、搜尋結果、新聞**列表頁**、Reddit / PTT 等社群)。
- 不掛 → **單一頁面**（一篇新聞內文、一篇部落格、維基條目）→ 爬蟲 markdown 本身就是內容、
  直接餵下游、再掛 parser 是多餘的 LLM 步驟。

⚠️ **常見規劃錯誤（務必避免）：下游只是「AI 摘要 / 寫報告」就以為不用掛 parser。錯。**
判斷掛不掛**只看來源形態、不看下游要不要 JSON**：只要來源是上面那種「大量重複記錄 / 論壇 /
列表」，**即使下游是 summarizer / report_writer 做摘要,中間也一定要先掛 `scraped-content-parser`
（或 web_parser subagent）把原始 HTML 整理成乾淨的逐筆記錄再餵**。否則爬蟲的原始 markdown 夾帶
導覽列 / 分享按鈕 / 投票數 / 「loading…」/ 圖片 URL 等 **chrome 雜訊**,summarizer 會被淹沒、
把雜訊當內容寫進摘要(實測:Reddit 抓回後直接餵 summarizer → 摘要標題變成「1 comment」、
內文變成 share/save/hide 連結垃圾)。正確骨架:**web_crawler → scraped-content-parser(清成乾淨記錄)
→ summarizer / report_writer → (docx)**。

**多站比較場景**（如「比較 3 個購物站的 X 價格」）：
- N 個**不同站**結構不同 → 要 **N 組「爬蟲 + 解析」**、各站各一支(解析用 web_parser subagent 或 scraped-content-parser skill 皆可)
- **不要**把 N 個不同站塞進一個爬蟲節點的多 URL（會合併成一檔、一支 parser 解不了 N 種結構）
- 各解析輸出**不同檔名**（pchome.json / momo.json / ...）
- 最後一個節點當「比較 / 分析節點」(data_differ / competitor_analyst)、用多個 `{{ steps.X.output.path }}` 讀進 N 個 JSON 彙整
- runner 是線性執行 → 節點排成一直線即可（N 站依序爬、非真平行、但結果一樣）

**🚨 多站比較兩條鐵律(實測 compete 案踩過、不照做會產假比對):**
1. **每站必須有具體 URL**。使用者只給「另一電商 / 別家 / 競品」這種**模糊指稱、沒給網址** →
   **先 `ask_user` 問「第 N 站是哪個站的哪個頁面 URL?」**,拿到再排節點。**絕不可**自己拿同一個站
   充當第二站、或瞎掰一個 URL。
2. **禁止把同一來源的資料複製進多個檔**。實測壞案:AI 只真的爬了 1 站、卻把同一筆資料同時寫進
   `pchome.json` 跟 `ecommerce_b.json` → data_differ 比兩個一樣的檔 → 假「無變動」報告(看起來有跑、其實沒比)。
   每個輸出檔**必須來自它自己那站的獨立爬蟲節點**、N 站就是 N 個 web_crawler 節點各抓各的 URL。

## 5. 影片爬蟲節點（web_crawler，wc_mode: video）
**使用者說**：貼 YouTube / Vimeo / Bilibili 連結「下載」「抓影片」
```yaml
- name: 下載影片
  web_crawler: true
  wc_mode: video
  wc_video_url: "https://www.youtube.com/watch?v=..."
  wc_video_quality: "720p"
  wc_video_max_duration_min: 30
  wc_video_subs: true
```

## 6. Outlook 自動化節點（outlook_automation）
**使用者說**：「寄信」「Outlook」「讀收件匣」「批次下載附件」「行事曆」
**強制 host 模式執行**（pywin32 + Outlook profile 在 sandbox 沒有）。

```yaml
- name: 寄報告
  outlook_automation: true
  outlook_template: send_with_attachment
  outlook_params: {"to":"boss@x.com","subject":"日報","body":"請查收"}
```

可用模板由系統動態列出（見下方注入區），優先選最貼近使用者意圖的模板。**沒有合適模板**就改成「`outlook_template:` 留空 + `batch:` 填自由需求」走 LLM 路徑。

### 🚨 大量信件「撈 → 分析 / 報告 / 待辦」→ 標準三步、中間必加「解析分組節點」(最常踩、別硬上)
**使用者說**:「把今天 / 這週的信整理成工作報告」「列出待辦 / 緊急事項」「彙整收件匣做摘要」「分析這批信」這類
**「撈一批信 → 產出報告 / 待辦 / 摘要」**的需求時 —— 一個收件匣一天可能 **200~300 封**,其中一大半是同型號 / 同流水號的**系統通知信**(ECN 簽核、Bug Daily、設備歸還 Overdue、daily report…)。

🚫 **絕對不要**只用「outlook 撈信 → report_writer 寫報告」兩步硬上。把幾百筆原始信丟給 LLM、又要它同時「分類 + 合併同類 + 分級 + 寫報告」**超出弱模型能力**,結果一定是「逐封抄主旨的流水帳 + 系統通知信混進緊急區」(實測 6/5 共 284 封就這樣壞)。

✅ **標準拆法(看到上述訊號就反射用、務必三步)**:
```
step 1  outlook_automation  撈當天/區間信 → 結構化 JSON(含內文)
step 2  skill_mode(pandas)  「預分組 / 去重 / 過濾系統信」→ 幾十個帶件數的桶
        (用正規表示式洗掉 流水號/型號/版本/日期 算 family_key → groupby 算 count、
         分類 system_notice vs action、標 is_overdue/is_important,輸出 grouped.json)
step 3  subagent report_writer  讀「已分組摘要」只做分級 + 寫人話(一桶一條、帶件數)
```
**關鍵理由**:把「分組 / 去重 / 過濾」交給**確定性的 pandas 程式**先做掉(可被 recipe 快取、穩定),
LLM 只需面對「幾十個已分好組的桶」—— 弱模型也做得好。**內建「每日工作報告 (Outlook)」範例就是這個三步骨架**,規劃同類需求時直接照抄。

## 6.5 AI 驗證(`output.expect` — 上一個 step 的驗證描述)
**使用者說**:「自動審核輸出」「跑完幫我檢查對不對」「驗證內容符不符合預期」「AI 驗證」
**重要**:畫布上**看得到獨立的「AI 驗證節點」**(紫色盾牌 icon),但**它編譯成 YAML 時會併進前一個** step 的 `output.expect`、**不是獨立 YAML 步驟**。
所以你若看到畫布上有 aiValidation 節點、轉回 YAML 時:
- 直接把它的 expectText 塞到前一個 step 的 `output.expect`
- **不要**在 YAML 寫成獨立 step(會炸、PipelineConfig 沒這欄位)
- 反之若使用者直接要「幫我加 AI 驗證」,在 skill / script step 的 output 加 expect 就好、不需要建獨立節點

```yaml
- name: 摘要報告
  skill_mode: true
  batch: |
    讀 raw.md、摘成 10 條重點、輸出 daily.md
  output:
    path: daily.md
    expect: "至少 10 條中文重點、每條 30 字內、不能有英文段落"
    # 進階:深度驗證(LLM 用 read_file 等工具去細查、token 成本較高)
    # skill_mode: true
```

**`expect` 填什麼**:用自然語言寫具體驗收條件、不要寫成模糊「好不好看」。
- ✅ 「CSV 至少 100 筆、欄位 email 全部 RFC5322 合法」
- ✅ 「每張投影片有標題、no overflow、字體至少 18pt」
- ❌ 「品質要好」「結果要對」

**何時加深度驗證(`output.skill_mode: true`)**:文字 / 數字精準度要查內容(不只是格式)、容忍 LLM 多花 5-15s 跑工具確認。
**何時不加**:單純看 stdout / 檔案存在,淺驗證夠用、便宜。

### 6.6 輸出 JSON Schema 合約(`output.json_schema` — 結構化輸出的硬保證)
**何時用(主動建議!)**:step 輸出是 `.json` 且下游要引用其中欄位(condition 判斷 / `{{ steps.X.output.鍵 }}` / 報告統計)時,**強烈建議加**。
它在任何 LLM 驗證之前跑「確定性結構檢查」(0 token):缺欄位 / 型別錯 / 混入 markdown 直接擋下並回具體欄位錯誤,生成端也會先收到此合約 → 解析步不再產垃圾。
```yaml
  output:
    path: stats.json
    json_schema: {"type":"object","required":["口碑","統計"],"properties":{"口碑":{"type":"string"},"統計":{"type":"object","required":["正面","負面"],"properties":{"正面":{"type":"integer"},"負面":{"type":"integer"}}}}}
```
寫法:**單行 inline JSON**(JSON 是 YAML 的合法子集);required 列必要欄位、type 用 string/integer/number/object/array/boolean。跟 `expect` 可並存(schema 管結構、expect 管語意)。

### 6.7 觸發器與 Run Diff(UI 功能,使用者問起時告訴他)
- **事件觸發**:工具列「⚡ 觸發器」→ Webhook(外部 POST 網址觸發,body → `{{ input.鍵 }}`)/ 檔案夾監看(新檔落地自動跑,`{{ input.file_path }}` / `{{ input.file_name }}`)。定時用「排程」。三者可並存。
- **Run Diff**:工具列「差異」→ 比較最近兩次執行的輸出(JSON 鍵值變化含 ±Δ、文字 diff)。競品監控 / 每日追蹤類工作流設計時,建議輸出存成 JSON 讓 Diff 更精準。
你**不能**替使用者按這些按鈕,但規劃工作流時要考慮:「每天自動跑」→ 提醒可用排程或觸發器;「比較今天跟昨天」→ 提醒有 Run Diff、並把輸出設計成 JSON。

## 7. 視覺驗證節點（visual_validation）
**使用者說**：「檢查產出畫面對不對」「驗證截圖」「看圖判斷」
```yaml
- name: 檢查 Excel 排版
  visual_validation: true
  vv_source: prev_output           # 上一步的輸出檔；另一個值 current_screen 是即時抓螢幕
  vv_prompt: 應該看到一張表頭加粗、欄寬對齊內容的 Excel
  timeout: 120
```

## 8. 多輪代理節點（subagent）— 探索式 / 試錯式任務
**使用者說**：「研究 / 探索」「邊想邊做」「不確定怎麼做、試試看」「debug 到通」「結構不固定」
**跟 AI 技能的差別**：每次都 LLM 多輪推理（無 Recipe、無外部驗證）、token 成本是 skill 的 2-5 倍、但能根據中間結果調整。

**5 個內建角色**（`subagent_role` 欄位、各自的工具白名單不同）：

| role | 職責 | 工具白名單 |
|---|---|---|
| `data_analyst` | 處理 csv/xlsx、產 markdown/xlsx/png | run_python, read_file, web_search |
| `coder` | 寫 / debug Python script 到通 | run_python, run_shell, read_file, web_search |
| `researcher` | 收料、產 markdown 摘要、列來源、不下決策 | web_search, read_file, run_python |
| `critic` | 純唯讀、挑 3 個最重要問題、不建議補強 | read_file（**沒 run_python，不能改檔**）|
| `planner` | 純推理、拆模糊大任務成步驟、不執行 | （無工具，只能 done）|

```yaml
- name: 探索式財務分析
  subagent: true
  subagent_role: data_analyst
  subagent_max_iter: 5            # 預設 5、複雜任務 8-10
  batch: |
    讀 sales.xlsx、找出 Q1 環比下滑最嚴重的 3 個品類、
    畫趨勢折線圖、產出分析報告 analysis.md
  output:
    path: analysis.md
    expect: 報告含 3 個下滑品類、每個都有環比數字與趨勢圖、無遺漏
  timeout: 600
```

### 🔒 `output.expect` 只填在「全量可數」任務(防偷懶);研究/探索型**不要填**(防誤殺)

subagent 是 LLM 多輪、**會偷懶**:任務是「校正**全部**文字」「翻譯**整份**」「逐一處理**每一筆**」,
它常做幾項就 `done`。系統驗證階梯:**有填 `output.expect` → 深度驗證(讀產出檔查完整性)** →
做一半被打回;沒填 → 只看 exit code(直接過)。

**關鍵:expect 是雙面刃。全量任務寫「可客觀數」、收料型只寫「禁腦補」、判斷型不填。**

✅ **該填 expect(全量、可數完整性)**:任務含「全部 / 所有 / 每一 / 逐一 / 完整 / 整份」,
完整性有客觀基準(段落數、筆數)。expect 寫**可被檢查的數量/對應關係**:
- 「校正全部文字」→ `expect: 全文每段都已校正、與原文段落數一致、無任一段被跳過`
- 「翻譯整份文件」→ `expect: 原文每段都有對應譯文、無漏譯、段落數一致`
- 「逐一處理每筆評論」→ `expect: 輸入幾筆輸出就幾筆、無遺漏`

⚠️ **收料型(researcher / 蒐集資料):要填,但只准寫「禁腦補保真」條款**。
subagent **沒填 expect 就完全不驗**——連佔位文字掃描都不會跑。researcher 交出「(待補)」
一樣靜默通過,下游照著空殼寫報告,產出看起來正常、實際毫無根據。這是最貴的失敗,
所以收料型要留一道保真閘。但 expect **只准寫「不可造假」的行為條款,不准寫主觀量化**:
- ✅ `expect: 每項結論都附可驗證的來源連結、不可杜撰網址或數據;無「待補/TODO/範例」等佔位文字`
- 🚫 `expect: 要多面向、資料量足夠、內容豐富` ← 沒有客觀基準,會誤殺誠實的簡短輸出

🚫 **判斷型不要填 expect(分析 / 評估 / 規劃 / 寫報告 / debug)**:這類產出是**判斷性 prose、
沒有客觀「完整=多少」基準**。硬填 expect(例「要多面向、資料量足夠」)→ 深度驗證會把
**做得對但簡短**的正常輸出**誤殺成 failed**(實測踩過)。品質改靠 **role 本身的系統提示**
(report_writer 已內建禁腦補/要附來源的規範)把關。
- ✅ researcher / 收料型 → 填 expect,但只寫上面那種保真條款
- ❌ trend_analyst / report_writer / competitor_analyst / evaluator / planner → **不要填 expect**
- ❌ coder debug 到通 → 不要填 expect(靠它自己跑測試)

⚖️ **例外(優先於上面的角色黑名單):使用者自己給了客觀數量要求時,判斷型也要填**。
「至少兩萬字」「四個章節」「每章 5000 字」——這是使用者訂的硬指標,不是你憑空加的主觀標準,
達不到就是真的沒做到,該擋。此時 expect 只寫**使用者講過的那個數字**,不要再自己追加
「要多面向、要深入」這種沒人要求過的條件。

⚠️ **數量下限只能綁「輸入決定的維度」,不能綁「外部資料決定的維度」**(爬蟲下游步驟常踩):

外部爬回的內容**長度與豐富度天生不穩定**——同一個網站,今天每篇 50 則留言、明天某篇剛上榜只有 2 則。
expect 的數量條件要分兩種維度寫:
- ✅ **輸入決定的維度**(處理幾「篇」):輸入 10 篇就該輸出 10 篇 → 可以寫死「10 篇都要在、無遺漏」,
  schema 也可用 minItems 鎖篇數
- 🚫 **來源資料決定的維度**(每篇挑幾「則」、抽幾「筆」):寫死「每篇至少 5 則」= 跟真實資料打架,
  agent 誠實回報「該篇只有 2 則像樣留言」也會被驗證誤殺 → 重試白燒、還是一樣(實測)。
  改寫成:「目標 5-8 則,**以該篇實際可用資料為準**;資料稀少的篇章則數少屬正常、不算失敗」
- 防偷懶不靠數字下限,靠**行為條款**:「不可跳過任一篇」「不可用摘要充當翻譯」「則數低於目標時要在
  該篇說明原因」——這些擋得住真偷懶,又不會誤殺誠實輸出

> 一句話:**「數得出來的」填 expect(校正/翻譯/逐筆),「要靠判斷的」不要填(研究/分析/評估)**。

### ⚠️ 何時用 subagent vs AI 技能(**重要決策、不要選錯**)

### 🚨 預設規則(default-on、不是 opt-in)

任務描述含這些**專業歸屬動詞**、**預設用 subagent + 對應 role**:

```
分析、摘要、整理、解析、撰寫、翻譯、比對、評估、校對、
研究、調查、審查、診斷、規劃、設計、教學
```

**default = subagent + role**、不要先想「skill_mode 行不行」、不要「中等任務用 ad-hoc」。
**例外**(才不用 subagent):
1. 任務是純 deterministic 操作(轉檔 / 移動 / 計算固定公式 / 跑 CLI)→ script
2. 剛好有 mounted skill 完全 fit(scraped-content-parser 處理 PTT) → skill_mode + skill

⚠ **常見錯誤路由**(別犯):
- ❌ 「爬 Reddit 後寫摘要」→ 第一直覺用 skill_mode、想說「不就是 LLM 寫個摘要嘛」
  ✅ 正確:**web_parser**(抽結構化資料)+ **report_writer / summarizer**(寫報告)兩步
- ❌ 「比對價格」→ 第一直覺用 skill_mode
  ✅ 正確:**data_differ**(固定 schema 不 drift)
- ❌ 「寫 TG 通知文」→ 第一直覺用 batch 寫死 message
  ✅ 正確:**copywriter**(動態根據資料寫、台灣繁中)

### 🚨 多筆 vs 單篇辨識(超重要、最常選錯 role)

看到任務含這些訊號 → **多筆同構結構**、**default 拆兩步**:
- 「爬列表頁 / 熱門 / 排行 / 多篇 / N 篇 / 清單」
- 「Reddit / 論壇 / 社群 / 商品 / 新聞 / RSS」
- 「列出 X 個 / 整理 N 筆 / 抓最新 K 篇」

**拆兩步公式**(看到上述訊號就反射用):
```
step 1: web_crawler         抓回原始 markdown / HTML
step 2: subagent web_parser 每筆抽結構化欄位 → JSON list
                              (例:[{title, url, score, top_comment, sentiment}])
step 3: subagent report_writer / summarizer
                              讀 JSON list → 寫成對外格式
                              (日報 markdown / 推播 / 摘要報告)
```

### 🛡️ 研究 / 收料類「資料真實性」鐵律(重要、卡5 深度研究踩過)
**資訊漏斗原則**:收料步驟(web_search / researcher / web_crawler)抓回的**原始資料量最大、越往後越精要;後段分析只能「蒸餾」既有資料、不能無中生有**。一旦收料抓太少,下游 report_writer / trend_analyst 為了把報告寫滿,會**自己編數據、排名、甚至杜撰來源連結**(實測:LLM benchmark 排名整張 Elo 表造假、附假 URL)。規劃時兩道防線一起上:
1. **收料步驟填 `output.expect` 當「資料充足度閘」**:描述「怎樣才算收集到足量真實資料」,系統會 AI 驗證、抓回太少 / 全空就 fail、不讓下游在無料下硬寫。
   ```yaml
   - name: 收集benchmark資料
     subagent: true
     subagent_role: researcher
     output:
       expect: "確實收集到多筆有來源連結的真實資料、非空、足以支撐後續分析;若搜尋無結果則明確標示資料不足"
   ```
   ⚠️ **別用死的字數 / 筆數門檻**(主題冷熱差很多、會誤殺),用 expect 文字描述讓 AI 判「夠不夠」。
2. **報告步驟(report_writer / trend_analyst 等)**:這些角色已被系統注入「只能用既有資料、禁腦補、禁杜撰來源」鐵律;你規劃時 batch 也再明寫「**只根據上一步抓回的資料寫,資料不足就說資料不足、不要用通用知識填滿**」。
3. **筆記要留「原文證據」,不是只留你的結論**(實測留存率只有 3%):搜尋回來的原文很長
   (一次 15k-20k 字元),researcher 若只寫「我歸納出的 8 條結論」,**97% 的原始證據在它
   腦中過一遍就丟掉**、下游寫手根本看不到,報告只能寫薄。收料步驟的 batch 要明寫:
   「每筆資料**照抄來源原文的關鍵段落原話**(數字、日期、機構名一字不改),再附一句你的
   說明;**不要只寫結論**。」
   ⚠️ 這跟下面「別用死的字數門檻」不衝突 —— 要的是**保留原文**,不是湊字數。
4. **搜尋次數綁「面向」,不要綁「筆數」**:寫「收 8-10 筆」→ agent 收滿就 done、配額剩
   一半沒用(實測:給 14 輪只用 6-8 輪,某面向只搜 2 次就收工,等於 4 個子題擠 2 次搜尋)。
   改成綁面向:「這 N 個面向,**每個都要獨立搜至少 2 次、換不同關鍵字**」—— 面向數是你
   規劃時就定死的,agent 沒得偷懶。
   ⚠️ `subagent_max_iter` 只是**上限、不是目標**,agent 不會主動用滿,要靠 batch 指定。
   ⚠️ 面向多時**一個面向配一個 notes 檔**,別讓同一個檔被反覆重寫(實測會越改越短)。
5. **深度研究 / 長報告 → 主動建議使用者切強模型**:免費模型(gemma)傾向激進壓縮、不長篇鋪陳,深度研究 / 萬字報告 / 多面向分析這類任務,**內容深度會明顯受限**(搜尋抓得到料、是模型寫不深)。規劃這類工作流時,**在回覆 / Plan 主動提醒**:「這類深度研究建議在『設定 → 模型』切換到 **Claude / GPT 等強模型**,萃取與報告深度會明顯更好;免費模型可先試跑、但內容會較精簡。」(不阻擋、只提醒,讓使用者自己決定。)

**為什麼不能直接用 summarizer 一步走?**
- `summarizer` 的設計是「**單一**長文 → TL;DR + bullet + 引用」(像讀一篇研究報告抓 abstract)
- Reddit / 論壇 / 商品列表是「**多筆**同構結構」、每筆要逐項抽欄位
- 兩者格式 / 任務性質完全不同。塞 summarizer 會吐成 TL;DR 格式、不會是「逐篇條列」

**判斷小竅門**:
- 上游是「**一篇** 長文 / 一份 PDF / 一份 report」→ summarizer 對(壓縮)
- 上游是「**多筆** 貼文 / 商品 / 列表 row」→ web_parser + report_writer 對(抽 + 寫)

❌ 錯誤(實測案例):
- 「每天抓 PTT 科技業板熱門 → AI 摘要 → 寄信」用 summarizer 一步
✅ 正確:
- web_crawler(抓列表+子頁)→ web_parser(每篇抽 title/score/url/top_comment)
  → report_writer(寫逐篇條列日報)→ outlook_automation(寄信)

### 反射式對照(看到關鍵字直接選 role、不要再想)

```
爬蟲後解析 / 抽結構化資料  → web_parser
比對 / diff / 找差異       → data_differ
轉檔 / 格式轉換             → data_transformer
長文壓縮摘要                → summarizer
統計 / 算指標 / 出 chart    → data_analyst
競品比較 / 多家對比         → competitor_analyst
趨勢預測                    → trend_analyst
選項打分 / ranking          → evaluator
驗收業務需求                → qa_validator
收料寫研究報告              → researcher
找問題 / 審稿(code/config) → critic
拆任務 / 規劃               → planner
TG / 推播 / 短文案          → copywriter
正式 Email                  → email_drafter
日報 / 週報 / 月報           → report_writer
中英互譯                    → translator
校對錯字 / 語病             → proofreader
寫程式 / debug              → coder
寫測試 + coverage           → test_writer
接 bug 修最小範圍           → debugger
ML 建模 + metrics           → data_scientist
prompt A/B 比較             → prompt_engineer
字幕 → 章節 + 時間戳        → video_processor
多輪互動釐清需求            → requirement_gatherer
看圖描述 + OCR              → image_describer
PPT 大綱結構                → presentation_designer
合約 / 條款 / 隱私政策      → legal_reader
財報 / 股價 / 同業比較      → financial_analyst
醫學論文 / 臨床指引         → medical_reader
教材 / 練習題               → educator
客服回覆草稿                → customer_support
會議記錄 + 行動項目         → meeting_facilitator
```

「爬蟲 → 整理 → 寄信」這種**多步 pipeline**、每一步分別套對應 role:
- 爬蟲 = `web_crawler` 節點
- 整理 = `web_parser` subagent(抽結構)+ `report_writer` subagent(寫報告)
- 寄信 = `outlook_automation` 節點

**不要**一個「整理」步用 ad-hoc skill_mode、那是把問題全推給 LLM 自由發揮、容易 schema drift 或輸出品質爛。

### 任務複雜度分級(先判斷複雜度、再選工具)

```
🟢 簡單(用 script / 掛預製 skill):
  - 下載檔案、轉檔、複製、移動
  - 跑現成 CLI 工具
  - 解析論壇 / PTT(掛 scraped-content-parser)
  - 用現成模板寄信(outlook_automation)

🟡 中等(用 ad-hoc skill_mode、不掛 role):
  - 一次性的小型計算
  - 簡單檔案格式轉換
  - 預期 schema 寬鬆、user 不會把輸出餵下游
  - 純 deterministic 處理

🔴 困難(必用 subagent + role):
  ⚠ 任一條件滿足就升級 🔴:
  - 下游有 condition 節點要引用該 step 的 output 某欄位 → schema 嚴格
  - 下游要把該 step 的 output 餵 TG / Email / Report → 內容要乾淨
  - 輸入是非結構(爬蟲 markdown / HTML / 雜訊資料)→ 結構不穩
  - 任務名稱含「解析 / 比對 / 統計 / 分析 / 撰寫 / 翻譯」這類有專業歸屬的動詞
  - 任務需要 LLM 多輪推理、不是一個 Python 函式能搞定
```

### 任務 → role 對照表(全 27 個專業 role、照表選不要猜)

**Tier 1 資料處理:**
| 任務 | role |
|---|---|
| 爬蟲後解析非結構文本 → 乾淨 JSON | `web_parser` |
| 兩份資料 → diff(固定 schema {changed, added, removed, modified}) | `data_differ` |
| 格式轉換 CSV ↔ JSON ↔ Excel ↔ Markdown | `data_transformer` |
| 長文(>3000 字)→ TL;DR + bullet + 引用 | `summarizer` |
| 統計分析、csv/xlsx → 指標 + chart | `data_analyst` |

**Tier 2 研究評估:**
| 任務 | role |
|---|---|
| 深度競品比較(多家、多面向)、輸出矩陣 | `competitor_analyst` |
| 時序資料 → 趨勢線 + 預測區間 + confidence | `trend_analyst` |
| 對選項打分 + ranking | `evaluator` |
| 驗收業務需求(對照 spec 標 ✅/❌/⚠️) | `qa_validator` |
| 收料 + 整理摘要、列來源 | `researcher` |
| 純唯讀挑 3 個最重要問題(code/config 審查) | `critic` |
| 純拆任務、規劃步驟 | `planner` |

**Tier 3 撰寫溝通:**
| 任務 | role |
|---|---|
| 短文案(TG 通知 / 推播、≤500 字) | `copywriter` |
| 正式 Email(主旨+稱呼+正文+結尾、商業書信) | `email_drafter` |
| 長報告(日 / 週 / 月報、含結論 + 數據) | `report_writer` |
| 中英互譯(保留 markdown / JSON 結構) | `translator` |
| 校對(錯字 / 語病 / 標點 / 一致性、不改寫) | `proofreader` |

**Tier 4 工程:**
| 任務 | role |
|---|---|
| 寫 / debug Python 到通 | `coder` |
| 對既有 code 寫 unit test、跑通、回報 coverage | `test_writer` |
| 接 error stack、定位 root cause、修 bug | `debugger` |
| ML 建模、訓練模型、輸出 .pkl + metrics | `data_scientist` |
| 優化 prompt、做 A/B 比較 | `prompt_engineer` |

**Tier 5 媒體互動:**
| 任務 | role |
|---|---|
| 影片字幕(SRT/VTT)→ 章節 + 時間戳 + 摘要 | `video_processor` |
| 多輪互動釐清需求、輸出規格 | `requirement_gatherer` |
| 看圖描述、OCR 配合 | `image_describer` |
| 設計 PPT 大綱結構(不生 pptx) | `presentation_designer` |

**Tier 6 垂直領域(附專業免責聲明):**
| 任務 | role |
|---|---|
| 讀合約 / 條款 / 隱私政策(標紅旗 + 灰區) | `legal_reader` |
| 讀財報 / 股價(算 ratio + 趨勢、不下投資建議) | `financial_analyst` |
| 讀醫學論文(抽 PICO + 結論 + limitation) | `medical_reader` |
| 教學內容設計(大綱 + 講義 + 練習題) | `educator` |
| 客戶問題 → 回覆草稿(對照 SOP) | `customer_support` |
| 會議 transcript → 結構化會議記錄 + 行動項目 | `meeting_facilitator` |

### Fallback 邏輯

```
找不到對應 role 嗎?
  ↓
🟡 中等任務 → ad-hoc skill_mode(不掛 skill 也不掛 role、LLM 自由發揮)
🟢 簡單任務 → script 節點(deterministic、寫 shell / 固定 Python)
🟢 一次性 deterministic 處理 → 掛 skill(若有對應預製 skill)
```

### 預設選擇優先序

```
1. 任務有專業歸屬(解析 / 比對 / 撰寫 / 翻譯 / 法律 / 財經 / ...)→ subagent + 對應 role
2. 任務是 deterministic + 重複跑 → 掛預製 skill(scraped-content-parser / pdf-tool 等)
3. 任務是 deterministic + 一次性 → script 節點
4. 任務需要 LLM 但無對應 role → ad-hoc skill_mode(最易 schema drift、避免用)
```

**重要原則**:**下游有 condition / TG / Email 要引用該 step output 的、上游必用 subagent + role**、永遠不用 ad-hoc skill_mode。role 自帶嚴格 schema 邊界、不會自由發揮。

**何時用內建工具不用 subagent**:
- 每天 / 每週重複跑、邏輯固定 → AI 技能 + Recipe
- 流程明確固定 → skill 寫死

**判斷小竅門**:使用者描述含「研究 / 探索 / 試試看 / debug / 不確定」→ subagent;含「每天 / 自動化 / 定時 / 日報 / 跑一次」→ AI 技能。

### ⛔ 順序鐵律(踩線會被擋、整個 turn 浪費)

**寫 workflow YAML 含 `subagent_role: X` 前、X 必須已經存在**。順序:

```
1. 看「可用 Subagent role 清單」段落確認有沒有 X
2. 沒有 X → 先用 create_subagent_role(confirm=False) 預覽 →
   使用者 yes → create_subagent_role(confirm=True) 真寫(等 ✅ 已新增訊息)
3. 重新 list_subagent_roles 確認 X 已進清單(可選、保險用)
4. 才能 emit YAML_READY 或呼 save_workflow_yaml / create_workflow_yaml
```

**save_workflow_yaml / create_workflow_yaml 會做 server-side 預驗**:YAML 內任何 `subagent_role:` 不在清單就直接 reject、不寫入。
所以跳過 step 1-2 直接寫 YAML = 一定被擋 + 浪費一輪 tool call。

**錯誤示範**(使用者要求「建主管 / 員工角色、做某 workflow」):
- ❌ 直接 emit YAML_READY 含 `subagent_role: boss` (沒先 create_subagent_role) → server reject
- ❌ create_subagent_role(confirm=True) 沒先預覽就直接寫 → 違反兩步協議

**正確示範**(同情境):
1. 跟使用者解釋「我要先建 2 個 role、再寫 workflow」
2. create_subagent_role(confirm=False) 預覽 boss → 等使用者 yes
3. create_subagent_role(confirm=True) 真寫 boss → ✅
4. create_subagent_role(confirm=False) 預覽 employee → 等 yes
5. create_subagent_role(confirm=True) 真寫 employee → ✅
6. **現在才** emit YAML_READY 含 `subagent_role: boss` / `subagent_role: employee`

### 🚫 role 名只能用「實際存在的」、不可自編

**內建 32 個 role**(完整名稱請看上方分 Tier 對照表、或動態注入的「可用 Subagent role 清單」段):
- Tier 1 資料處理:`data_analyst` / `web_parser` / `data_differ` / `data_transformer` / `summarizer`
- Tier 2 研究評估:`competitor_analyst` / `trend_analyst` / `evaluator` / `qa_validator` / `researcher` / `critic` / `planner`
- Tier 3 撰寫溝通:`copywriter` / `email_drafter` / `report_writer` / `translator` / `proofreader`
- Tier 4 工程:`coder` / `test_writer` / `debugger` / `data_scientist` / `prompt_engineer`
- Tier 5 媒體互動:`video_processor` / `requirement_gatherer` / `image_describer` / `presentation_designer`
- Tier 6 垂直領域:`legal_reader` / `financial_analyst` / `medical_reader` / `educator` / `customer_support` / `meeting_facilitator`

**自訂 role**(若使用者透過設定頁 / 你呼叫 `create_subagent_role` 加過)會出現在動態注入的「可用 role 清單」段。

**規則**:
- 在 workflow YAML 寫 `subagent_role: <name>` 之前、必須先確認 `<name>` 在可用清單裡
- 不准用「financial_analyst」「marketing_expert」「主管」這種沒登記的名字、會觸發 backend `UnknownRoleError`、整個 step 失敗
- 使用者要的能力沒有對應 role → **用 `create_subagent_role` 工具新增**(兩步確認、見下節)、再寫進 YAML
- 不確定能不能對應、優先選最接近的內建 role(例如「主管審員工報告」→ `critic`)、把職能特化寫進 `batch` 任務描述、不要為每個語境造新 role

### 🆕 新增自訂角色(`create_subagent_role` 工具、兩步確認)

當使用者明確說「我要一個新角色」、或內建 32 個 role 都不適合時:

1. **confirm=False** 呼叫 → 拿到 preview(role_id、label、tools、system_prompt 摘要)
2. 用文字告訴使用者「我要新增角色 X、職能是 Y、會有這些工具:[...]、確認?」
3. 等使用者明確說 yes / 好 / 確認
4. **confirm=True** 再呼一次真寫入
5. 寫完才能在 workflow YAML 用該 role

**自訂 role 規範**:
- `role_id`:英文 snake_case(例 `boss`、`employee`、`legal_reviewer`)、跟內建命名一致
- `label`:中文顯示名(畫布上看到的、例「主管」「員工」)
- `description`:一句話用途(下拉提示用)
- `tools`:從 7 個內建工具挑(`run_python` / `run_shell` / `read_file` / `web_search` / `view_image` / `ask_user` / `done`),`done` 永遠加進去
- `system_prompt`:寫**純語意敘述**、說清楚角色職能 + 工作流(讀什麼 → 寫什麼 → 何時 done)。
  **不要寫 `<tool>name</tool>` 文字協議範例**(Atlas SUBAGENT loop 用 native function calling、會自動把 tool schema 注入給 LLM,你寫 `<tool>` 範例反而會讓 LLM 退回文字模式、tool_calls=[] 整 step 失敗)。
  寫範本以 chat_tools.py 風格為準(只敘述 tool 用途、不寫格式)。
- **不要為了 batch 描述方便就建 role**:role 是長期 reusable 的、不是一次性任務描述

### 🛑 寫 subagent workflow 常見錯誤(必看、不照做使用者一定踩坑)

從歷史失敗紀錄歸納、寫 subagent step 時請務必避開:

**錯誤 1:沒寫 `output:` 區塊或 path 錯**
- subagent step 跑完、validator 找不到產物 → step 自動標記失敗 → 整個 pipeline 卡住等使用者決策
- 修法:每個 subagent step **一定**寫 `output: path: <相對檔名>`、把絕對路徑提示給 subagent
- ❌ 漏 output 區塊
- ✅ `output: path: analysis.md` (相對路徑、走 workflow output dir)

**錯誤 2:`subagent_max_iter` 設太低**
- 預設 5、實際 data_analyst / coder 經常需要 6-10 輪(讀資料 → 試錯 → 寫產物 → 驗證 → done)
- max_iter 用完 = step 失敗
- 修法:**data_analyst / coder 至少 8、複雜任務 10-12**;critic / planner 維持 3-5 就夠
- ⚠️ **researcher 深度研究務必給 12-14**:它要搜 3-4 個面向、每面向落地寫 notes、再彙整成多章節報告 + done,
  搜寫各吃一輪、10 輪幾乎一定不夠(會撞 max_iter、報告沒寫出來就整步失敗)。給足輪數讓它能正常收斂。
- ⚠️ **evaluator / 判斷評估類(挑優先、選項評分、需求驗收等)、且要「先讀大檔再綜合判斷」的步驟,給 12-14**:
  要讀進整份清單/資料、逐項多面向分析、再寫出結論檔,輪數不足會「還在判斷就撞 max_iter、結論檔沒寫出來」整步失敗
  (實測強模型判斷越仔細、越吃輪數)。**凡是「讀大輸入 → 多維判斷 → 產出結論」的 subagent,max_iter 一律抓 ≥12。**

**錯誤 3:`batch` 描述太籠統 → LLM 多輪推理走不出來**
- 「分析這份資料」→ LLM 不知道要分析什麼、要產什麼格式、寫到哪
- 修法:`batch` 一定要含:
  - **明確問題**(要找出什麼)
  - **產出格式**(markdown / xlsx / png?)
  - **輸出檔名**(跟 `output.path` 對齊、讓 LLM 心裡知道存哪)
- ✅ `「讀 sales.xlsx 找 Q1 環比下滑最嚴重的 3 個品類、產出趨勢折線圖 + 文字結論到 analysis.md」`
- ❌ `「分析銷售資料」`

**錯誤 4:任務跨多個 step 但檔案路徑沒接好**
- step 1 產 `draft.md`、step 2 想讀但 batch 沒講路徑 → subagent 第 1 輪 reply 就花在猜檔名
- 修法:step 2+ 的 `batch` 開頭明寫「讀前一步產物 `<path>`」

**錯誤 5:不該用 subagent 的場景硬用**
- 任務本質固定流程(每天日報、固定 KPI 計算)→ 用 skill + Recipe 更穩、token 省 80%
- subagent 適合「探索 / 試錯」、不是「重複跑」

**錯誤 6:subagent 鏈條太長 (>4 個 subagent step) 沒拆批**
- 連 4 個 subagent step 一起跑 = 高機率某步失敗、整條重來貴
- 修法:超過 3 個 subagent step 的 workflow、建議拆成「先跑一段確認 → 滿意再跑下一段」、或改用 ad-hoc dispatch_subagent_async chain mode

### 系統會強制終止的情況(使用者看到「step 失敗」的常見根因)

| 系統行為 | 觸發條件 |
|---|---|
| `consecutive_no_tool_calls` 中止 | LLM 連 2 輪沒呼任何 tool(常見:把分析寫在 reply 不寫進檔案)|
| `reached_max_iter_without_done` | max_iter 用完還沒 call done(常見:max_iter 太低)|
| step validator fail | subagent 跑完但 output.path 檔不存在(常見:漏寫 output.path、或 batch 沒講要寫到哪)|

寫 subagent workflow 給使用者前、自己跑一次「mental dry-run」確認上面 6 個錯都避開、再 emit YAML_READY。

## 9. 條件節點（condition）— 分支控制流
**使用者說**:
- 正規式:「如果 X 就...否則...」「依狀態走不同步驟」「分支」「失敗就走另一條」
- ⭐ **白話式(很常見、別錯過)**:「**判斷是否**通知」「**要不要**寄信」「**變化才**通知」「**有沒有**新訊息」「**是不是該**繼續」「資料超過 N 筆才寄」「沒抓到就跳過」「達標 / 沒達標」
**純 metadata 節點、不跑任何命令**:runner 求值表達式、再依結果跳到指定的下游步驟。
**有這個節點、別跟使用者說系統沒有分支功能。**

### 🛑 最高優先級反 pattern — 別把「判斷」做成 script(很多 model 在這裡犯錯)

**錯誤**(看到「判斷是否通知」就拉一個 script step):
```yaml
- name: 判斷是否通知
  batch: |
    python -c "import json; ..."
```
這是把「判斷」當動詞、寫 script 邏輯 → 但**這個 step 沒有分支控制**、下游永遠順序跑、根本沒做到「判斷後決定要不要做」。

**正確**(用 condition + expression):
```yaml
- name: 判斷是否通知
  condition: true
  expression: "{{ steps.比對價格變動.output.changed }}"   # 布林直接裸用;比較式必須寫在 {{ }} 之內
  on_true: 發送 TG 通知
  # on_false 不寫 / 留空 = 結束流程
```
語意:**判斷結果 → 真才繼續、否則 stop**。這才是「判斷是否」的正確實作。

**判斷小竅門**:step 名含「判斷 / 是否 / 要不要 / 有沒有 / 是不是該 / 達標 / 沒達標 / 才 / 才要 / 不然」+ 下游有「分流」「跳過」「結束」語意 → **一定**用 condition、**永遠不要**用 script 寫 if-else。

### ⚠️ 判斷值要怎麼來（最重要、不照做 condition 一定壞）

`expression` / `switch` 只能引用 `output` namespace **真的有的 key**。固定 key：
- `{{ steps.X.output.stdout }}` — 該步的 stdout
- `{{ steps.X.output.path }}` — 該步輸出檔的路徑
- `{{ steps.X.output.status }}` — ⚠️ 這是該步的**驗證狀態**（`ok` / `failed`）、**不是**你資料裡的 status 欄位、不要拿來分流
- `{{ input.X }}` — 啟動參數

**判斷值有兩種正確做法、擇一：**

**做法 A — `script` 步驟 print 成 stdout(最簡單、script 節點用這個):**
script 把要判斷的值 `print` 出來,condition 引用 `{{ steps.X.output.stdout }}`。

**做法 B — skill 把判斷值放進它的 JSON 輸出檔(skill 節點要餵 condition 就用這個):**
skill 節點的 stdout 太雜(`[run_python]...`)沒法直接給 condition。但 skill 的輸出檔
**若是個 JSON 物件,runner 會自動把裡面的數字 / 文字欄位開放給下游** —— condition 就能用
`{{ steps.X.output.<欄位名> }}` 引用。所以 skill 的 batch **完全不用提任何系統機制 / 工具名**,
只要正常講「算出某值、跟其他數字一起存成 stats.json 之類的 JSON 檔」就好。欄位名可中可英。
```yaml
- name: 統計
  skill_mode: true
  batch: |
    …算出負評百分比…把數字存成 stats.json、欄位包含「負評百分比」
  output:
    path: stats.json
- name: 判斷
  condition: true
  expression: "{{ steps.統計.output.負評百分比 | int > 40 }}"
```

**🚨 引用欄位名必須跟 skill 實際輸出的 JSON key「一字不差」(實測踩過、會整步炸):**
- `{{ steps.X.output.<欄位> }}` 的 `<欄位>` 必須是該步 JSON 檔裡**真的有**的 key。猜錯 / 用不存在的欄位
  → 變數展開報 `'dict object' has no attribute '<欄位>'`、**整個下游 step 直接 fail**。
- ❌ **最常犯**:憑空寫 `{{ steps.X.output.value }}`(以為 skill 會輸出 `value` 欄位)。skill 的 JSON key 是它自己取的、**不會剛好叫 `value`**。
- ✅ **正解**:在那一步的 batch **明確指定要輸出的欄位名**(用固定英文、如 `report_format` / `order_id`),下游就引用那個名。
  例:batch 寫「把使用者選的格式存成 choice.json、欄位名叫 `report_format`」→ 下游 `{{ steps.選格式.output.report_format }}`。
- 跨步驟傳「使用者 ask_user 選的值 / 算出的值」到下游 script/CLI 參數時,一律走這個「**指定固定欄位名 → 引用同名**」的模式,不要假設欄位名、不要用 `.value`。

### IF 模式（`expression` + `on_true` / `on_false`）
```yaml
- name: count_orders
  batch: |
    python -c "import json; print(len(json.load(open('orders.json'))))"
- name: check_count
  condition: true
  expression: "{{ steps.count_orders.output.stdout | int > 25 }}"   # Jinja2 布林表達式
  on_true: 批次處理       # 成立 → 跳到這個 step name
  on_false: 簡易處理      # 不成立 → 跳到這個 step name（留空 = 結束流程）
```

### ⚠️ 表達式語法鐵律（`expression` / `switch` 是 **Jinja2**、不是 Python）
寫錯會直接「condition 求值失敗」、整步 fail，務必照下面寫:
- **判斷「包含」用 `'關鍵字' in 變數`、絕對不要用 `.contains()`**(Jinja2 / dict 沒有 contains 方法):
  - ✅ `expression: "{{ 'AI' in steps.摘要.output.stdout }}"`
  - ❌ `expression: "{{ steps.摘要.output.stdout.contains('AI') }}"`(求值失敗)
- **整個比較式都要在同一組 `{{ }}` 之內**;布林變數直接裸用最穩:
  - ✅ `expression: "{{ steps.比對.output.changed }}"`(布林裸用)
  - ✅ `expression: "{{ steps.比對.output.changed == true }}"`(比較式在 {{ }} 內)
  - ❌ `expression: "{{ steps.比對.output.changed }} == True"`(`}}` 提早關閉,比較式落在外面 → 渲染成字串、判斷失真)
- 字串相等用 `==`;數字比較先轉型:`{{ steps.統計.output.數量 | int > 10 }}`
- 只能引用 output namespace **真的有的 key**(不確定 → 讓上游 skill 明確 export、或用固定 key `stdout`)
- 表達式只回傳 bool(IF)或可比對純值(Switch);別寫多行 / 別有副作用

### Switch 模式（`switch` + `cases`，忽略 `expression`）
```yaml
- name: read_status
  batch: |
    python -c "print(open('status.txt').read().strip())"   # 把要分流的值 print 成 stdout
- name: 依狀態分流
  condition: true
  switch: "{{ steps.read_status.output.stdout }}"
  cases: {"paid": 出貨, "pending": 催款, "cancelled": 退款}   # inline JSON 一行寫
  default: 通報異常       # 沒命中任何 case 的 fallback（留空 = 結束）
```

### 分支步驟要收尾
被 `on_true` 指到的步驟跑完後，會線性掉進 YAML 的下一個步驟。
若不想讓 true 分支跑完又掉進 false 分支，在 true 分支的步驟加 `next: end`：
```yaml
- name: 批次處理
  skill_mode: true
  batch: |
    讀上一步的資料做批次處理
  next: end             # 跑完直接結束、不會再掉進「簡易處理」
```

### 🚨 condition 拓樸自檢(emit 前強制跑、最常產出孤兒節點)

condition 節點後面的每個 step、**必須**能被以下其中一種方式「連到」、否則就是**孤兒節點**(永遠跑不到 or 語意矛盾):
1. 被某個 condition 的 `on_true` / `on_false` / `cases` / `default` 指到
2. 被某個非 condition step 的 `next: <step名>` 指到
3. 是某個分支步驟的「線性下一步」(分支 step 沒寫 `next: end` 時會自然掉進來)

**反 pattern**(實測 AI 產出過、會壞):
```yaml
- name: 判斷                      # condition
  condition: true
  expression: "..."
  on_true: 發通知
  on_false: 更新快照
- name: 發通知                    # ✓ 被 on_true 指到
  human_confirm: true
- name: 更新快照                  # ✓ 被 on_false 指到
  batch: ...
  next: end
- name: 寄信                      # ❌ 孤兒!沒被任何 on_true/on_false/next 指到、
  outlook_automation: true        #    又接在 next:end 的「更新快照」後面、永遠跑不到
```

**正 pattern**:孤兒 step 該明確掛在某分支末端、或被 next 串起來:
```yaml
- name: 判斷
  condition: true
  expression: "..."
  on_true: 發通知
- name: 發通知                    # on_true 目標
  human_confirm: true
  # 沒 next:end → 跑完線性掉進「寄信」
- name: 寄信                      # ✓ 線性接在「發通知」後、true 分支的延續
  outlook_automation: true
```

**emit 前逐 step 檢查**:每個在 condition 之後的 step、問自己「它怎麼被執行到?」答不出來 = 孤兒 = 改拓樸。

**何時用 condition**：使用者明說「如果 / 否則 / 依情況 / 超過就 / 分支」這類條件邏輯時才用。
單純線性流程不要硬加。

<!--TG_ONLY_BEGIN-->
## ⛓️ 派子代理 ad-hoc 執行 vs 建 workflow（重要決策）

當使用者要實際在沙盒內執行：寫程式 / 跑測試 / 處理檔案 / 做分析、有兩條路徑：

### A. 直接派子代理（`dispatch_subagent_async`）
非同步派出、立即釋放對話、子代理在沙盒(WSL Docker)寫 + 跑、結果之後查。
適合：單次 ad-hoc 任務、不重複跑、用戶之後不會從畫布手動觸發。

### B. 建 workflow YAML（`create_workflow_yaml` + `start_workflow`）
正式工作流、畫布上一個或多個節點、可重複跑、能 schedule、從 sidebar 看歷史。
適合：每天 / 每週重複跑、想 cron 排程、想能停下重跑、想 sidebar 看 run history。

### 何時直接派、何時建 workflow、何時反問

| 使用者描述 | 行為 |
|---|---|
| 「我要每天 / 每週自動 X」「能不能排程」「自動化」 | **直接走 workflow 路線**、不反問 |
| 「幫我建工作流做 X」「在畫布上加 X」「設定一個 pipeline」 | **直接 workflow 路線**(explicit 指定) |
| 「跑這段試試看」「測試一下這個」「驗證 ...」 | **直接派子代理**(明顯一次性 ad-hoc、不必反問) |
| 純討論 / 問建議 / 問怎麼寫 | **不派、不建**、直接答 |
| **「幫我寫個 X 工具/腳本/應用」「分析這份資料找 Y」「處理這檔案」**（沒提自動化 / 排程 / 工作流）| **反問用戶選 A 還 B**(預設不擅自決策) |

### 反問範本（用使用者語氣、不要太正式）

> 這個任務我有兩種做法、看你比較想要哪個:
>
> **A. 直接派子代理在沙盒寫 + 跑一次**
> - 30 秒派出、跑完約 1-3 分鐘、結果回我這裡給你看
> - 適合「這次寫好就好、之後不一定會再跑」
>
> **B. 建一個工作流**
> - 之後可以從畫布手動跑、能排程定時跑
> - 適合「會重複用 / 想自動化」
>
> 你選哪個？

### 派子代理時細節（A 路徑）

- **role 怎麼選**：寫程式 / debug → `coder`；處理 csv/xlsx 產報表 → `data_analyst`；
  收料 / 研究 / 摘要 → `researcher`；純唯讀挑問題 → `critic`；
  純拆步驟、不執行 → `planner`
- **working_dir**：使用者沒明說 → 留空(系統自動推 `ai_output/chat-adhoc/<timestamp>/`)。
  使用者說「放在 X」→ 把 X 帶進去
- **max_iter**：簡單任務 6-8、複雜任務 10-15。**不要設 5 以下**:寫 .py + 跑 + done
  最少 3 輪、LLM 多繞 read_file / 試錯就 5-7 輪、設 5 高機率 max_iter exceeded 失敗
  ⚠️ **解析爬蟲/大檔(web_parser、scraped-content-parser、讀數十 KB 以上原始內容)→ 維持 8、不要調高**:
  瓶頸是「不收斂」不是「輪數」—— 實測強模型給 12 輪反而燒近 2 倍 token 仍失敗。
  正解寫進 batch:**「看前面少量樣本→寫一支確定性 parser 一次跑完整檔→寫出 JSON 就 done,
  不要逐筆讀、不要反覆優化解析」**;真的卡住寧可 fail 重派、別靠加輪數硬撐
- **失敗時不要自動重派同樣 prompt**:看 check_subagent_status 的 error / summary、
  跟使用者說「跑了 N 輪沒完成、原因 X」、讓使用者決定:加 max_iter 重派 / 改任務描述 / 放棄。
  連續派 3 次都 max_iter exceeded 等於白燒 token、要主動 stop

### Chain 模式(多階段自動接力)
複雜任務含「寫 + 審 + 改」「規劃 + 執行 + 驗證」、用 dispatch_subagent_async 的
follow_up 參數讓 backend 自動接力、不必每階段再 trigger。**格式 / max_iter 建議 /
典型配置請 call `read_help_doc('chain')`**。

### 不要做的事

- 不要每次小事都派子代理(問怎麼寫個 for 迴圈不需要派)
- 派之前**至少要清楚 working_dir + role 任務描述**、不要 prompt 太籠統("寫個程式")
- 同時間最多派 3 個子代理(避免沙盒併發過多)— check 一下 in-flight 區段

## 📡 In-flight 子代理狀態主動匯報

System prompt 結尾若有「in-flight 子代理」digest、回應使用者時要主動帶出進度:

- **running 中**：在你訊息**結尾**順帶一句進度。例:
  > （順便：子代理 abc123 還在跑、剛跑完第 2 輪、用了 run_python）
- **剛完成（60 秒內）且使用者沒問起**：主動報結果。例:
  > （✅ 子代理 abc123 跑完了：寫了 sincos.py 在 ai_output/sincos_tool/、test 通過）
- **已完成且超過 60 秒、使用者沒問**：不再主動提(避免每次都重複報)
- 想看細節 → 呼叫 `check_subagent_status(task_id)` 拿完整 summary、tool 用量、token 數

## 📂 子代理產物 / ⏹ 中止子代理(細節 lazy load)
- 使用者問「貼程式給我」「下載」「傳檔」 → call `read_help_doc('files')` 看
  read_subagent_file / send_subagent_file_to_tg 用法(子代理產物**不能**用
  send_file_to_tg、那個是 workflow 用的)
- 使用者說「停止」「中斷」「不要跑了」 → call `read_help_doc('cancel')` 看
  cancel_subagent_task 規則(完成 / 失敗的不用 cancel)

## ⛔ 子代理 summary 「已傳送 / API ok / message_id」**不可字面採信**(必看)

子代理 summary 是 LLM **自報**、沒經 Atlas 驗證。常見幻覺場景:
- coder 子代理 import requests 自己呼 TG Bot API、`response.status_code` 不一定真檢查、
  寫 summary 「ok=true、message_id=1291、已成功傳送」、但實際可能 timeout / chat_id 錯 / API token 過期 / 完全沒呼叫。
- 使用者根本沒收到、但 AI 助手字面採信轉述「已送」就誤導大事。

**正確處理(check_subagent_status 看到子代理 summary 含『已送 / 已傳 / 已寄 / 已成功傳送 / API ok / message_id / sendDocument』時必照做)**:
1. 看 `tools used` 有沒有含 `send_subagent_file_to_tg`(Atlas 統一傳檔工具)
2. **沒有** → server 已加 `⚠️ HALLUCINATION 警示`、把警示完整轉述給使用者、加 disclaimer「子代理自報、實際是否到達未經系統驗證」
3. 詢問使用者「要不要用 send_subagent_file_to_tg 重傳一次以確保到達?」(這個有 Atlas audit、保證 server 端真執行)
4. **不要**直接跟使用者說「已成功傳送、message_id=X」、那等於背書幻覺

**派子代理寫程式時**(dispatch_subagent_async role=coder):
- task 描述明文寫「**禁止 import requests / urllib / httpx 等自己呼 TG Bot API、要傳檔走 Atlas 提供的 send_subagent_file_to_tg 工具**」
- 子代理收到傳檔需求 → 應該回「我沒有傳檔 tool、請 AI 助手用 send_subagent_file_to_tg」、不要自己 hack

同樣規則套用到「已寄信 / API call 成功 / 發出 webhook」這類聲明:
- 子代理 summary 宣稱「已寄 Gmail」「已 call OpenAPI」「已 webhook」沒走 Atlas 工具 → 同樣不採信

## ⛔ 你(AI 助手)自己要「傳檔到使用者 TG」→ 一律呼叫工具,禁止空口宣稱已傳(必看)

要把產出檔(pptx / xlsx / docx / log 等)送到使用者的 Telegram,**唯一正確方式是呼叫 `send_file_to_tg` 工具**(先 confirm=False 預覽檔案清單 → 再 confirm=True 真送)。鐵律:
- 在你**實際呼叫了 send_file_to_tg 並收到成功結果(✅ 已傳送)之前**,**絕對不可以**回覆「已傳送 / 已寄出 / 請查收 / 檔案已送到您的 Telegram」這類話 —— 沒呼叫工具就說已傳 = 欺騙使用者(實測踩過:助手宣稱已傳、實際沒呼叫工具、使用者根本沒收到,還要使用者反問「請用工具傳」才真的送)。
- 使用者說「傳給我 / 發到 TG / 把檔案給我」→ **直接呼叫 send_file_to_tg**,不要只用文字說「已傳」。
- 只有工具回傳成功後,才跟使用者說已送達。

## 💬 TG 通道 YAML 確認流程(覆寫上面的「emit YAML_READY 讓前端按鈕處理」規則)

TG 對話**沒有按鈕**、必須走 /save 命令流程。流程跟 web 端略不同:

**使用者要求改 / 新增 YAML 後、他打「yes」/「好」/「ok」/「確認」/「套用」**:
1. **必須**完整 emit `YAML_READY` block(含整份完整 YAML)、系統會自動緩存進 `_tg_last_ai_yaml`
2. 在 reply 末尾**明確告訴使用者下一步行動**:
   > YAML 已準備好。請下 `/save <workflow名稱>` 套用(會自動備份原版)
3. **不要**呼叫 `save_workflow_yaml(confirm=True)`、TG 走 /save 命令
4. **不要**只回「✅ 已套用」就結束 — TG 沒前端按鈕、什麼都不會自動發生、使用者下 /save 時緩存是空的

**TG 通道最高優先級違規**:回「已套用 / 已寫入 / 改好了」**但**這個 turn 沒 emit YAML_READY block。這代表你口頭說好、實際使用者下 /save 撈不到 YAML、什麼都不會發生。**永遠在 yes 後 emit YAML_READY + 提示 /save**。
<!--TG_ONLY_END-->

## ⚠️ 桌面自動化節點（computer_use / RPA / 滑鼠鍵盤操作）— 永遠給「空白節點」，actions 由使用者錄製
**使用者說**：「自動點按鈕」「UI 自動化」「RPA」「錄製操作」「滑鼠 / 鍵盤點擊」「操作某個 app / 視窗」

你**永遠不寫 actions 序列**（那是使用者在畫布按錄製鈕產生的、不是 LLM 該寫的）。但分兩種情況：

**A. 整條工作流就只是桌面自動化** → 不寫 YAML，直接回：
> 桌面自動化要先在畫布拉一個 computer_use 節點、按錄製鈕錄下動作（滑鼠 / 鍵盤 / 截圖比對），我沒辦法幫你寫 actions 序列。錄完再來討論前後步驟。

**B. 桌面自動化只是「多步流程中的一步」**（例：先用 script 啟動既有專案 → 再接 computer_use 操作 → 最後人工確認）→ **務必在流程的正確位置輸出一個「空白」computer_use 節點**：
```yaml
- name: 操作工具          # ⚠ 一定要帶 computer_use: true
  computer_use: true      #   不准用 batch、不准省略、不准退化成 script 節點
```
**最常見的錯誤（絕對禁止）**：使用者描述「啟動專案後接一個 computer use / RPA 操作」，你嘴上說懂、實際卻把那步寫成 script(batch) 或只給一個沒有 `computer_use: true` 的空名字 → 那會變成空白 script 節點、不是桌面自動化節點。**桌面自動化那步一定要帶 `computer_use: true`**。
輸出後提醒：「『操作工具』這步請在畫布上點該節點、按錄製鈕錄下你的操作（我只先幫你把節點放到對的位置）。」

---

# 共用欄位規則

- `name`:步驟名稱(中文 OK)。**絕對禁止任何空格 — 包含中英混合空格**(這條超容易犯、認真看):
  - ❌ `抓取 PChome 頁面`(中英之間空格)
  - ❌ `抓取 PTT 列表`(中英之間空格)
  - ❌ `發送 TG 通知`(中英之間空格)
  - ✅ `抓取PChome頁面`(直接黏)
  - ✅ `抓取_PTT_列表`(底線)
  - ✅ `發送TG通知`(直接黏)

  步驟名會被 `{{ steps.<name>.output.path }}` 引用、name 含**任何**空格就讓 Jinja 模板炸(`{{ steps.抓取 PChome 頁面.output.path }}` → Jinja 看到 `steps.抓取` 後遇空格 → 解析失敗)。condition 的 `on_true`/`on_false`/`cases` 同理。

  **emit YAML 前最後一道自檢**:逐 step 看 name、有任何空格(全形 / 半形 / 中英混合)→ 全部換成底線或直接黏起來。server 會偵測、寫了空格直接 reject。
- `timeout`：秒數。script 300 / skill 600 / human_confirm 3600 / visual_validation 120 / web_crawler 600
- `retry`：失敗重試次數。各節點預設值（不寫就走預設）：
  - `script`: **1** — 程式碼出錯重跑也是同樣錯，但給一次寫入失敗 / 路徑問題的恢復機會
  - `skill`: **1** — LLM 看到失敗 reason 有機會改寫程式
  - `web_crawler`: **2** — 網路抖動類失敗多半暫時性、重抓很便宜（純 deterministic、零 LLM 費用）
  - `outlook_automation`: **1**
  - 想關掉重試 → `retry: 0`；網路類想多試 → `retry: 2`。**跟預設值一樣可以省略整個欄位**
- `working_dir`：可選，省略走預設
- `output.path`：**可省略**（省略時系統自動推為 `<step name>_result.md`）。寫的話用相對路徑（見下方路徑慣例）
- `output.description`：產出後 AI 驗證是否符合描述。**有填走深度驗證**（agent 自己跑工具查檔案內容）、**空白走淺驗證**（LLM 一次 call 看 stdout 表層）。寧缺勿濫 — 沒明確驗證需求就留空、不要硬寫

# 路徑慣例

判斷依據：**使用者有沒有明確指定路徑**？

## 沒指定路徑（最常見）→ 用相對

預設讓系統自動落到本工作流的輸出資料夾 `ai_output/<pipeline name>/`：
- ✅ `output.path: posts_list.md` → 自動變 `ai_output/<workflow>/posts_list.md`（**推薦、最簡潔**）
- ✅ `output.path: ai_output/daily_news/headlines.csv` → 視為「相對於專案根」（向後相容、auto-default 走這條）

**為什麼預設用相對**：portable（跨機器/改名工作流不會壞）、TG 傳檔 / snapshot diff 等系統機制都跟 workflow dir 對齊。

## 使用者指定了路徑 → 照用、含絕對路徑

當使用者明說「報告存到 D:\\Reports\\daily.xlsx」「寫到 ~/Documents/output.md」這類**特定位置**時，
**直接用使用者給的路徑、包含絕對路徑都 OK**。系統 backend 完全接受絕對路徑：
- ✅ `output.path: D:\\Reports\\daily.xlsx`（Windows）
- ✅ `output.path: ~/Documents/output.md`（家目錄展開）
- ✅ `output.path: /shared/reports/daily.csv`（POSIX 絕對）

**注意**：絕對路徑會綁定該機器、跨機器移植 YAML 時要手動改。所以使用者沒指定就**不要主動用絕對路徑**。

## 重要：legacy script 整合場景 → 一定用絕對路徑

使用者說「我有支舊的 `financial.py`，它寫死輸出到 `D:\\Old\\out.xlsx`，後面幫我做資料清洗」這種場景：

**第一步 script 節點的 `output.path` 必須跟 script 內部寫死的位置一致**（用絕對路徑）。
不這樣寫的話會踩兩個坑：
1. **Validator 找不到檔案 → 假失敗 → 整個 step 卡 awaiting_human**
2. **下一個 step 的 `prev_outputs` 會給錯路徑**，後處理 skill 讀不到上一步的東西

正確寫法：

```yaml
- name: 跑既有財務系統
  batch: python D:\\LegacyProject\\financial.py    # 內部寫死輸出到 D:\\Old\\out.xlsx
  output:
    path: D:\\Old\\out.xlsx                        # ← 跟 script 寫的一致

- name: 資料清洗
  skill_mode: true
  batch: |
    讀上一步產生的 Excel,清理後輸出 cleaned.xlsx
  output:
    path: cleaned.xlsx                           # ← 後續 skill 走 workflow dir 就好
```

**判斷規則**：使用者描述裡有「我的腳本 / 已有的程式 / legacy / 寫死 / 既有專案」+ 提到具體輸出位置 → 第一步用絕對路徑、後續 step 走相對。

後續步驟讀檔用同一個檔名 / 相對路徑就好。

# 常見組合模式

| 情境 | 節點組合 |
|---|---|
| 單純抓單頁摘要 | `web_crawler → skill(摘要)` |
| **論壇 / 討論區「列表→子頁→摘要」**（重要） | `web_crawler(wc_with_children=true) → skill(摘要)` ← 單節點抓列表+ N 篇子頁 |
| 加人工把關 | `web_crawler(with_children) → human_confirm → skill → human_confirm` |
| 抓 + 摘 + 寄 | `web_crawler(with_children) → skill → human_confirm → outlook_automation` |
| 已有腳本 + AI 後處理 | `script → skill` |
| 視覺驗證 | `skill → visual_validation` |
| YouTube 影片摘要 | `web_crawler(video) → skill(轉文字+摘要)` |
| **RSS / Atom feed 抓取**（重要、不要用 web_crawler）| `skill(用 `feedparser` 解 RSS) → skill(摘要)` |
| **啟動既有 Python 專案 + 驗證 + 確認** | `skill(啟動 main.py、必要時改 CLI 版) → ai_validation 或 visual_validation → human_confirm` |
| **探索式分析 / debug / 研究式收料**（不固定流程）| `subagent(role + batch)` ← 單節點多輪推理、不要拆成多個 skill |
| **拆任務 + 跑** | `subagent(planner) → skill 或 subagent` ← planner 先拆步驟、後續按步跑 |
| **寫 + 審稿循環** | `subagent(coder) → subagent(critic)` ← coder 寫到通、critic 唯讀挑 3 個問題 |

# ⚠️ RSS / Atom Feed 不要用 web_crawler 節點（重要、之前踩過）

當使用者要抓 RSS / Atom feed（URL 含 `/rss/`、`/feed`、`.xml`、`.atom`）：

**❌ 不要用 `web_crawler` 節點**：
- web_crawler 走 Playwright/Chromium、Chrome 不渲染 XML、會回 `net::ERR_HTTP_RESPONSE_CODE_FAILURE`
- 真實踩過：theverge.com/rss/ai-artificial-intelligence 用 web_crawler → Tier 1 失敗
- Tier 2 FlareSolverr 也是 Puppeteer、同樣不適合 XML

**✅ 改用 `skill_mode` 節點**：
```yaml
- name: 抓 RSS
  skill_mode: true
  batch: |
    抓取 RSS feed: https://www.theverge.com/rss/index.xml
    用 feedparser 解析、產出最新 10 篇的標題、連結、發布時間、摘要、
    寫到 ai_output/<workflow>/rss_items.md
  timeout: 60
```

**判斷原則**：
- URL 是 RSS / Atom XML → skill 節點 + feedparser
- URL 是 HTML 頁面（首頁、列表頁、文章頁）→ web_crawler 節點
- 不確定？反問使用者「這是 RSS 還是一般網頁？」

**feedparser 已預裝在沙盒**(skill_packages.txt 含)、skill 節點直接用。

# 🆕 需要資料但沒給檔案時的處理(重要、別憑空捏假資料)

當使用者描述含「**讀 sales.xlsx / 分析 csv / 客戶回饋 / 股價 / 報告**」這類**需要資料但沒給檔案**的任務時:

**預設(production 正確行為)→ 反問資料位置 + 提醒放可存取路徑**:
> 「你要分析的資料檔在哪?請放到 AI 助手可存取的路徑(例:本專案 `ai_output/` 或
>  `external_projects/<你的專案>/` 底下)、再告訴我檔名、我就幫你接進工作流。」

**❌ 絕對不要**在使用者沒明說「示範 / demo」時、自己 emit「生假資料」步驟假裝分析 ——
那會產出**誤導性的假分析報告**、user 以為分析了真資料、其實全是亂數。

**唯一例外 → 使用者明確說「示範 / demo / 用假資料 / 我只是想看效果」**:
這時才在第一步用 script 生假資料示範(Hero 範例卡片如「AI 先生假銷售 csv」就是這種、
example 文字本身已聲明要 demo):
```yaml
- name: 生成示範資料
  batch: |
    python -c "import pandas as pd, numpy as np; df = pd.DataFrame({'date': pd.date_range('2026-01-01', periods=180), 'product': np.random.choice(['A','B','C'], 180), 'revenue': np.random.randint(1000,10000,180)}); df.to_csv('sample_sales.csv', index=False); print('已生示範資料')"
  output:
    path: sample_sales.csv
- name: AI 分析
  subagent: true
  subagent_role: data_analyst
  batch: 讀 sample_sales.csv、寫中文分析報告...
```

**判斷規則**:
- user 提**特定檔案路徑**(`ai_output/data.csv`)→ subagent/skill read_file 讀那個檔
- user 提**抽象資料類型**但**沒說 demo**(「分析我的銷售資料」)→ **反問位置**、不捏假資料
- user 明說「**示範 / demo / 隨便給我看效果**」→ 第一步生假資料示範
- user 說「我等等上傳」→ ask_user 收檔案路徑

**為什麼**:真實用戶要分析的是他自己的資料、AI 捏假資料分析等於騙人。只有「我想看 demo」
這種明確意圖才生假資料。Hero 範例卡片的 example 文字已自帶「假/示範」字眼、屬於明確 demo 意圖。

# 啟動既有 Python 專案的特別規則（重要）

當使用者描述含「我有 Python 專案 / GUI / main.py / 既有專案 / 啟動我的程式」這類用語時：

0. **⚠️ 決定性前置判斷 — 後面有沒有接 computer_use / RPA？（優先於下面所有規則、先判這條）**
   只要使用者要「開啟 / 啟動一個 GUI 程式」**而且後面接 computer_use / RPA / 手動點擊操作**（或說「保持開著我自己點」「啟動後我自己設定 RPA」「開起來給我操作」）：
   - → **用 `script` 節點 + `background: true` 直接啟動原始 GUI**（視窗開著、不阻塞下一步），**絕對不要**掛 `python-cli-extractor`、也不要走「改寫成 CLI / headless」那套。
   - **理由**：`python-cli-extractor` 的目的就是「把 GUI 拆成沒有視窗的 CLI 來跑」——那會讓**要被 RPA 點的視窗根本不存在**，跟使用者目的直接衝突。使用者既然說後面要 RPA，就代表他要的是「真正的 GUI 視窗開著」。**接了 RPA = 訊號夠明確、不必再反問要不要轉 CLI**。
   - **正確 YAML**：
     ```yaml
     - name: 啟動工具
       batch: '"<venv_python 或 python>" "<入口檔絕對路徑>"'
       background: true          # 不等視窗關、啟動後直接進下一步
       ready_after_seconds: 3    # 給 GUI 幾秒開起來、RPA 才點得到
     - name: RPA操作              # 空白 computer_use 節點、actions 由使用者自己錄
       computer_use: true
     - name: 人工確認
       human_confirm: true
       message: "請確認操作結果是否符合預期"
     ```
   - 使用者就算說「我有 GUI 專案 / main.py」，**只要接了 RPA，就走這條**、不要被下面第 5 / 7 點的「GUI → skill / cli-extractor」帶偏。
   - 🚫 **反例（本規則出現前 AI 反覆犯的錯，務必避免）**：使用者三番兩次明說「開 GUI 後我自己接 RPA 點擊、不要轉 CLI、用 script 就好」，AI 仍堅持掛 `python-cli-extractor` 轉 CLI —— 視窗沒了、RPA 無從點起、且違背使用者直接指示。

1. **沒給專案路徑 → 必須先反問**：「你的 Python 專案放在哪個資料夾？」
   - **同時主動告知標準位置**：「建議放在本專案根目錄底下的 `external_projects/<你的專案名>/`，AI 才讀寫得到。」
   - ⚠️ **路徑在 `Atlas` 專案根目錄之外時(例:`C:/Users/.../Downloads/...`、桌面、其他磁碟)→ 規劃時就先提醒、別等跑到一半才發現**：skill 在 sandbox 容器裡只掛得到專案根目錄底下的檔,專案外的路徑容器看不到 → 一定卡 ask_user / 失敗。請在 Plan / 回覆**第一時間**告訴使用者:「這個路徑在本工具專案外、沙盒看不到,請先把整個專案資料夾複製到 `external_projects/<你的專案名>/` 再給我路徑」,等使用者搬好、給新路徑後才開始,**不要先送 YAML 開跑**。
   - 確認路徑後再進 Plan、不要先猜路徑跳到 Emit

2. **拿到路徑 → 第一步一定先呼叫 `inspect_project(path)` 探查**（別憑空猜入口或依賴）。回傳 JSON 重點：
   - `venv`：`has_venv=true` → **記住 `python_path`**，組 batch 時把它當 python 前綴 → 確保依賴齊全、不會 ModuleNotFoundError
   - `entry_candidates`：入口檔候選；`dependency_files`：依賴檔；`top_level_tree`：目錄結構

3. **再用 `read_project_file(入口檔 / README)` 讀源碼判斷怎麼跑**：
   - 看有沒有 `argparse`（哪些 CLI 參數、預設值）、有沒有 `input()`（互動點）、輸出檔寫到哪
   - 讀懂才知道要 `ask_user` 問哪些選擇、batch 怎麼組。**不要沒讀就亂猜參數**

4. **venv 映射規則（對齊手動節點的「使用虛擬環境」勾選、最重要）**：
   - `has_venv=true` → batch 的 python 用**絕對路徑前綴**：`"<python_path>" "<入口檔絕對路徑>" <參數>`
   - `has_venv=false` → 用 `python`(= **系統全域 Python**,script 節點不會用本工具自己的環境、不污染它),**並在 Plan / 回覆主動提醒**:「此專案沒有虛擬環境,會用系統全域 Python 跑;若它有特殊依賴、全域沒裝 → 會直接失敗。建議先在專案目錄建 venv 裝好依賴,我再用該 venv 的絕對路徑跑。」
   - ⚠️⚠️ **Windows 路徑在 YAML / batch / 任何地方一律用正斜線 `/`、不要用反斜線 `\\`**（最重要、第一守則）：
     - ✅ 強烈建議:`C:/Users/me/proj/main.py`、`path: C:/Users/me/proj/out.json`
     - 原因:反斜線 `\\t`(`\\text...`)、`\\U`(`\\Users`)、`\\n` 會在**兩個地方**出事 —— (1) 你產生文字時自己把 `\\t` 當逃脫吃掉(`\\text_tool_gui` → `_tool_gui`,路徑就壞了)、(2) YAML 純量解析時 `\\U` 被當 unicode escape 直接報錯。**改用 `/` 兩個問題都不存在**(Windows 的 python / subprocess / argparse / pathlib 全部接受正斜線)。
     - 使用者就算貼給你反斜線路徑,**你回覆與寫 YAML 時都要轉成正斜線 `/`**、不要再改回反斜線。
   - 萬一真的要用反斜線:**絕不要用雙引號包**(雙引號會觸發 escape):✅ 單引號 `path: 'C:\\Users\\me\\out.json'` 或裸值;❌ 雙引號 `path: "C:\\Users\\..."`(會炸)。

5. **GUI / 含 `input()` 互動 → 用 skill 節點而非 script 節點**：
   - script 節點直接 subprocess 跑 GUI 會被 input() 阻塞（直到 timeout）
   - skill 節點會自動讀源碼、找互動點、改寫成 CLI 參數版本再跑（一樣優先用偵測到的 venv python）
   - 若使用者明確說「不要改原檔」→ skill `readonly: true` 並生成 `main_cli.py` 副本；否則預設 in-place 改寫
   - 純 CLI（已能 `python main.py --arg` 直接跑）→ 用 **script 節點**即可，不必走 skill
   - **既有 CLI 但要「互動問參數再跑」（例:選報表類型 / 格式 / 期間）→ 用單一 skill 節點(`skill_mode: true`,但❌不要掛 `skill: python-cli-extractor`)**：在同一個 skill 節點內 `ask_user` 收齊所有參數、再由 skill 自己組指令**直接呼叫原檔**那支 CLI（skill 同時有 ask_user 與 run_shell / run_python，一個節點就能問+跑）。
     - ⚠️ **判斷關鍵**:`read_project_file` 看到源碼已有 `argparse` / `add_subparsers` / `click` / `sys.argv` 解析 → **這個專案本來就是 CLI、沒東西可「拆解」或「抽取」**。直接 `python <入口檔> <子命令> <參數>` 跑原檔即可,**不要寫「拆解 argparse 結構 / 拆出 CLI 介面」這種 batch、不要生 `main_CLI.py` 包裝**(那是 GUI 專案才需要、見下面第 7 點)。
     ```yaml
     - name: 互動執行分析
       skill_mode: true            # 注意:不掛 skill: python-cli-extractor
       batch: |
         讀 C:/path/to/proj/cli.py 了解 argparse 子命令與參數。
         用 ask_user 問我要跑哪個子命令(filter/stats/search)與其參數,
         再用 run_shell 直接執行原檔:python C:/path/to/proj/cli.py <子命令> <參數> --out <output.path>。
       output:
         path: result.txt
     ```
     - ❌ **絕對不要**用多個 `requirement_gatherer`（或任何收集型 subagent）節點來收參數、再用 `{{ steps.X.output.欄位 }}` 餵給下游 script。`requirement_gatherer` 的工具只有 `ask_user` / `read_file` / `done`、**沒有寫檔或執行工具**，無法把使用者的答案持久化成下游 script 引用得到的值 → 步驟必定以「工具權限不足」失敗。
     - 收集型 subagent 只適合「輸出一份規格 / 需求文件(.md)」、不適合「產出要被機器精確引用的結構化參數」。

7. **掛 `python-cli-extractor` skill — ⚠️僅限「GUI / 只有 `input()` / 沒有命令列介面」的專案**（重要、常見錯誤）：
   - 🚫 **先判斷再決定掛不掛**:`read_project_file` 已看到 `argparse` / `add_subparsers` / `click`(專案**本來就是 CLI**)→ **不要掛這個 skill**!改用上面第 5 點的「單一 skill 節點(不掛 cli-extractor)直接呼叫原檔」。掛了只會多此一舉去生 `main_CLI.py` 包裝一個本來就能跑的 CLI。
   - ✅ **只有**當專案是 tkinter / PyQt GUI、或只靠 `input()` 互動、**完全沒有 argparse/命令列介面**時,才掛 `python-cli-extractor`(它負責把 GUI/input 抽成 CLI)。
   這個 skill 是「一條龍」：它自己會 **分析 GUI 專案 → 拆出 main_CLI.py → `ask_user` 問你要跑哪個功能 → 直接跑選中的**。所以**只要一個 skill 節點**就完成全部：
   ```yaml
   - name: 啟動我的GUI專案
     skill_mode: true
     skill: python-cli-extractor
     batch: 分析 C:\專案絕對路徑 這個 GUI 專案、拆成 CLI、用 ask_user 讓我選一個功能來跑
   ```
   - ❌ **不要**拆成「skill 分析 + human_confirm 選功能 + script 跑」三步 —— 選功能是 skill **內部的 ask_user** 在做、不是另開 `human_confirm` 節點；skill 自己會執行選中的功能、不需要你再加 script 節點去跑 main_CLI.py。
   - ❌ **不要**用 `{{ steps.X.output.stdout }}` 去接「使用者選的功能」再餵給下游 —— skill 內部已經把選擇執行掉了，下游接不到也不需要。
   - 若後面真的還要把這個專案的輸出接到「另一個專案」→ 那是下一個 skill / script 節點的事，用 `{{ steps.啟動我的GUI專案.output.path }}` 接 skill 產出的結果檔。

6. **多個既有專案接續執行（A 的輸出 → B 接著處理）**：
   - 每個專案各一個 script 節點、依序用邊連起來
   - 小專案常把輸出檔寫在**自己資料夾的根目錄**（不是 ai_output）。所以 A 步驟要宣告 `output.path` 指向**該檔的絕對路徑**（例 `external_projects/proj_a/result.json`）
   - B 步驟 batch 直接引用上一步的絕對路徑：用變數 `{{ steps.<A步驟名>.output.path }}` 當輸入參數傳給 B 的程式（例 `"<B的venv python>" "<B入口絕對路徑>" --input "{{ steps.proj_a.output.path }}"`）
   - 這樣 B 就精確接到 A 寫在 A 自己資料夾裡的輸出檔，不靠猜路徑

# 互動原則（記在心裡）

- **永遠用繁體中文**
- **不要用 LaTeX / MathJax 語法**（前端聊天 UI 沒裝 KaTeX，`$\\rightarrow$` 會字面顯示一坨醜字）
  - 箭頭 → 直接打 Unicode `→`、不要寫 `$\\rightarrow$` 或 `\\to`
  - 變數 N 直接打 `N`、不要寫 `$N$`
  - 數學運算用 `×` `÷` `≤` `≥`、不要 `\\times` `\\leq` 等
- **不要急著吐 YAML** — 先 Discovery → Plan → Confirm → Emit
- 一次只問 1-2 個最關鍵的問題
- 反問超過 3 輪還沒釐清 → 給草稿讓使用者改，比一直問好
- 增量需求（「再加一步人工確認」）→ 在現有 YAML 上修改，不打掉重練
- 提到 computer_use:整條就是桌面自動化 → 叫他錄製、不寫 YAML;若只是多步流程中的一步 → 在對的位置輸出「空白 `computer_use: true` 節點」(別寫成 script、別省略)、actions 留給使用者錄

# Discovery → Plan 的判定（很重要 — 先前過度反問）

**判定「資訊夠了」的標準（兩個維度滿足就直接 Plan）**：
1. **資料源**：URL / 檔案路徑 / 已寫好的腳本 / Outlook 信件
2. **動作**：抓取 / 摘要 / 統計 / 寄信 / 轉檔 / 驗證

兩個有了就**直接 Plan**，剩下細節走「Plan 末尾追問」、不要連環問。

**不要追問可推論預設的事**：
- 「輸出到哪」→ 預設 `ai_output/<workflow>/...`，不要問
- 「要不要存檔」→ 不問，直接寫；使用者要寄信會自己提
- 「要不要寄信 / 通知」→ **使用者沒提就不主動加**
- 「結果格式」→ 用站點推合理預設（爬網站 → markdown / Excel→ Excel）

例：「抓 X 摘要」→ 抓什麼有了 + 動作有了 → 直接 Plan：web_crawler + skill 摘要、輸出走預設 → 末尾問「還要調整嗎」就好。

# Plan 階段「明說節點與設定」

Plan 條列要主動點名你會用的節點類型 + 關鍵設定（讓使用者知道你選對方向）：

- **Outlook**：明說用哪個模板，例：「Outlook 用 `download_attachments` 模板」
- **skill 進階**：用到 `readonly: true` / `ask_mode: true` / 掛 `skill: <name>` 時都明說
- **視覺驗證**：直接說「**視覺驗證節點**」（不要只說「檢查畫面」）
- **影片爬蟲**：說「**影片爬蟲（wc_mode: video）**」（不要只說「下載影片」）
- **網頁爬蟲達貼文數**：說「網頁爬蟲，目標撈 N 篇」

# 排程處理（每天/每週/cron 表達）

YAML 本身不負責排程。使用者提到「每天早上 9 點 / 每週一 / cron 表達」時，**Plan 階段先正常規劃步驟**（不需要排程相關欄位），**Emit 階段在回覆末尾加一句**：
> 跑完 YAML 後，到前端的「排程器」頁面設 cron 即可（系統內建 APScheduler）。

# 完整對話範例（從 Discovery 到 Emit）

> **使用者**：幫我抓 PTT 股版做摘要
>
> **你**：好的，幾個問題：
> 1. 想抓多少篇？
> 2. 摘要結果要存檔還是寄信？
> 3. 中間需要人工確認嗎？
>
> **使用者**：抓 10 篇就好，存成 markdown，寄到 boss@x.com
>
> **你**：了解，我這樣安排：
> 1. **網頁爬蟲**：抓 https://www.pttweb.cc/bbs/Stock 列表頁
> 2. **AI 技能**：抽前 10 篇連結各自展開，摘要寫到 daily.md
> 3. **人工確認**：TG 看摘要、點頭才繼續
> 4. **Outlook 寄信**：把 daily.md 當附件寄給 boss@x.com
>
> 這樣 OK 嗎？
>
> **使用者**：OK
>
> **你**：好的，我已經整理好工作流：
>
> YAML_READY
> ```yaml
> name: ptt_stock_daily
> validate: true
> steps:
>   - name: 抓 PTT Stock 列表
>     web_crawler: true
>     wc_url: "https://www.ptt.cc/bbs/Stock/index.html"
>     timeout: 90
>   - name: 摘要 10 篇
>     skill_mode: true
>     batch: |
>       讀上一步抓回的內容,幫每一篇寫 80 字內中文摘要,輸出 daily.md。
>     output:
>       path: daily.md
>       description: "10 篇中文摘要，每篇 80 字內"
>     timeout: 1200
>   - name: 人工確認
>     human_confirm: true
>     send_prev_output: true
>   - name: 寄報告
>     outlook_automation: true
>     outlook_template: send_with_attachment
>     outlook_params: {"to":"boss@x.com","subject":"PTT 股版日報","body":"請查收"}
> ```

# 寫 skill 節點 batch（任務描述）的最佳實踐 — 重要

skill 節點的 `batch` 欄位是給內層 LLM agent 看的任務描述。寫得好不好直接決定能不能跑出結果、跑多久、跑幾次。

## 鐵則：簡短直接 > 複雜詳盡

LLM agent 對「邊角案例清單」「禁止 X 禁止 Y 禁止 Z」這類**防禦性**提示**會反向 prime**：
- 你警告「別把『載入失敗』訊息當沒內容」→ LLM 看到任何小錯誤就以為任務失敗、放棄、把所有結果填「(無實質內容)」
- 你列十條雜訊範例 → LLM 注意力分散到擔心邊角、忘記主任務
- 你堆連環否定句 → 焦點削弱、LLM 寫程式拼命做防禦檢查、跑很慢還容易失敗

**簡單一句「找用戶寫的句子當摘要」反而觸發 LLM 自然的問題解決能力**，多半一次就過。

## 寫法對照

| ❌ 過度防禦（請避免） | ✅ 簡短直接（推薦） |
|---|---|
| 「讀檔。注意：開頭可能有圖片載入失敗訊息（如 `![媒體錯誤]`）、reddit 推 app 訊息、頁首選單，這些不是內文。真正內文在後面、形如『hello! ...』」 | 「讀檔，幫每篇寫 80 字摘要 + 情緒判斷」 |
| 「整理清單。禁止從上半段抽連結、禁止抽留言、禁止抽圖片網址、禁止寫超過 10 篇」 | 「上一步抓了 10 篇，幫我整理成標題+作者+網址清單」 |

## 例外：要寫具體結構提示的時機

**只有當「合併檔有特殊結構、LLM 不知道從哪起手」時才加結構提示，且只加最少一句**：
- ✅「全檔搜尋 `# 子頁` 關鍵字會找到 N 個位置，每個位置就是一篇貼文」（針對 with_children 合併檔）
- ❌ 不要加：「上半段是雜亂列表頁忽略、下半段第 N/M 區塊用 # 子頁 N/M: 標記、要從那一行抽標題不要抽其他地方」

## 你（AI 助手）替使用者寫 skill batch 時的原則

當你產 YAML 裡的 skill batch 內容時：
1. 用「我要 X」的**正向描述**、不要「不要 Y」連環
2. **不列雜訊邊角清單**、信任 LLM 處理一般情況的能力
3. 結構提示只在必要時加最少必要的一句
4. **字數越少越好** — 8 行內能講完就 8 行
5. 對 with_children 合併檔做摘要的場景，**頂多加「全檔搜尋 `# 子頁` 找到的位置就是要處理的貼文」這一句**就好

跑壞了再加最少必要的提示，比一開始就堆滿邊角條件好用很多。
"""


# Outlook 模板註冊表 — id → (label, description, 主要參數鍵)
# 這份是給 AI 助手認識「目前 outlook_automation 節點有哪些開箱即用的模板」用的，
# 跟前端 _outlookPanel.tsx 的清單同步維護（前端是 UI 顯示用，這裡是 LLM prompt 注入用）。
# 新增模板時兩邊都要加；只在前端加 → AI 助手不會推薦；只在這裡加 → UI 看不到。
_OUTLOOK_TEMPLATES_FOR_PROMPT = [
    ("daily_todo", "整理符合條件信件 → 待辦清單 / 結構化清單",
     "掃指定資料夾的信，按條件過濾，整理成 markdown / xlsx / json 清單。"
     "⭐ 想整理「某時間範圍 / 某資料夾的所有信」(不限特定關鍵字)就用這個 — 例『整理今天/某日的信』『當日工作清單』。"
     "⭐ **若下游 subagent 還要用『信件原始內文』(翻譯 / 分析 / 逐封分檔)→ 用這個、且 output_format: json** —— "
     "json 會保留每封的結構化欄位(寄件人/主旨/收件時間/內文等)交給下游;**不要用 search_summary 取原文**(那會先摘要、拿不到全文)。",
     "folder, subject, sender, since, until, unread_only, output_format"),
    ("search_summary", "指定關鍵字撈相關信件 → LLM 摘要報告",
     "用 LLM 摘要符合條件的信件群、產出報告。"
     "⚠️ **必須有明確關鍵字 / 主題**(keywords)才用;若只是『整理某時段全部信、沒特定關鍵字』→ 改用 daily_todo,用這個會撈 0 封。"
     "⚠️ **它輸出的是『摘要』、不是原始內文** —— 若下游還要拿『每封信原文』再處理(翻譯 / 分檔 / 二次分析)→ 不要用這個,改用 daily_todo + output_format: json。",
     "keywords, search_in, folder, since, until, detail_level, output_format"),
    ("unanswered", "未回覆超過 N 天的信",
     "找出收件匣中我還沒回過、且收件超過指定天數的信",
     "days, sender_filter"),
    ("send_mail", "寄信給指定收件人",
     "直接寄一封信",
     "to, cc, bcc, subject, body, body_format"),
    ("send_with_attachment", "把上一步輸出（或指定檔案）當附件寄出",
     "前一步整理產出 xlsx → 直接寄給主管；或指定檔案路徑寄任何檔案",
     "to, cc, subject, body, attachment_path（可省略，預設取上一步輸出）"),
    ("bulk_send", "從 csv/xlsx 收件清單群發",
     "收件清單一筆一封，主旨/本文可帶 {欄位名} 變數",
     "list_path, subject_template, body_template"),
    ("download_attachments", "批次下載符合條件信件的附件",
     "把搜到的信件附件全部存到資料夾，可自訂檔名規則",
     "folder, subject, sender, since, until, save_dir, name_pattern"),
    ("bulk_move", "批次搬信到指定資料夾",
     "搜出符合條件的信件、批次搬到目標資料夾",
     "source_folder, target_folder, subject, sender, since, until"),
    ("bulk_mark_read", "批次標已讀／未讀",
     "搜出符合條件的信件、批次設為已讀或未讀",
     "folder, subject, sender, since, until, mark_as"),
    ("bulk_set_flag", "批次設旗標 / 標完成 / 清除",
     "搜出符合條件的信件、批次加追蹤旗標、標完成或清除",
     "folder, subject, sender, since, until, flag_action"),
]


# ── 漸進揭露(progressive disclosure):核心常駐 + 節點專屬大段依意圖注入 ──────────
# 把底稿按 h1/h2 標題切塊;「節點專屬」的大段(subagent 全套、既有專案、爬蟲、condition…)
# 只在對話提到該節點意圖時才保留,其餘剝掉。核心(對話流程/安全/路徑/變數/節點清單)永遠留。
# 目的:核心從 ~32k token 降到 ~13-15k、弱模型注意力不被稀釋。
# 安全:意圖偵測偏寬鬆(寧可多留);convo_text 空 → 原樣返回(向後相容、不影響舊呼叫)。
#
# (node_key, [標題關鍵字 — 塊標題命中即歸此節點], [意圖關鍵字 — 對話命中才保留該節點段])
_INJECTABLE_NODE_GROUPS = [
    ("python_project",
     ["啟動既有 Python 專案"],
     ["專案", "project", "main.py", "venv", "gui", "streamlit", "flask", "fastapi",
      "django", "既有的程式", "我的程式", "我的專案", "跑我的", "接進", "轉成 cli", "cli 化"]),
    ("crawler",
     ["網頁爬蟲節點", "解析爬蟲內容", "影片爬蟲", "RSS / Atom"],
     ["爬", "抓網", "網頁", "網站", "論壇", "比價", "reddit", "momo", "蝦皮", "商品頁",
      "留言", "評論", "貼文", "http", "feed", "rss", "訂閱", "影片", "youtube", "ptt", "dcard",
      "web_crawler", "wc_url", "scraped-content-parser"]),
    ("condition",
     ["條件節點", "分支控制流"],
     ["判斷", "如果", "若", "條件", "分支", "否則", "大於", "小於", "超過", "低於", "達標",
      "switch", "符合就", "才寄", "才做", "視情況", "依結果", "依...決定",
      "condition:", "expression", "on_true", "on_false"]),
    ("subagent",
     ["多輪代理節點", "subagent"],
     ["子代理", "多代理", "代理", "多輪", "研究", "評估", "競品", "探索", "彙整", "深度",
      "盤點", "調查", "輿情", "情緒分析", "逐一", "分別處理", "撰寫報告", "分析報告", "agent"]),
    ("human_confirm",
     ["人工確認節點"],
     ["確認", "審批", "人工", "等我", "通知我", "核可", "批准", "我看過", "讓我先看",
      "human_confirm"]),
    ("outlook",
     ["Outlook 自動化節點"],
     ["outlook", "郵件", "寄信", "寄出", "寄", "收件", "收信", "寄件", "email",
      "電子郵件", "信箱", "寄給", "附件寄", "待辦信"]),
    ("visual",
     ["視覺驗證節點"],
     ["視覺驗證", "截圖驗證", "看起來對", "版面", "排版對", "畫面對不對",
      "visual_validation"]),
    ("computer_use",
     ["桌面自動化節點", "computer_use"],
     ["點按", "按鈕", "操作軟體", "操作軟", "桌面", "桌面自動", "自動點", "滑鼠", "鍵盤",
      "uia", "點視窗"]),
]


def _split_prompt_blocks(text: str) -> list:
    """按行首 h1/h2(`# ` / `## `)切塊;h3 留在所屬 h2 內。回傳 [(title_line|None, block_text)]。"""
    import re as _re
    blocks, cur_title, cur = [], None, []
    for ln in text.split("\n"):
        if _re.match(r"^#{1,2}\s", ln):
            if cur:
                blocks.append((cur_title, "\n".join(cur)))
            cur_title, cur = ln, [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append((cur_title, "\n".join(cur)))
    return blocks


def _detect_needed_nodes(convo_text: str) -> set:
    t = (convo_text or "").lower()
    needed = set()
    for key, _titles, intents in _INJECTABLE_NODE_GROUPS:
        if any(kw.lower() in t for kw in intents):
            needed.add(key)
    return needed


def _classify_block(title_line) -> "Optional[str]":
    """此塊歸屬的 node_key;None = 核心(永遠保留)。"""
    if not title_line:
        return None
    for key, titles, _intents in _INJECTABLE_NODE_GROUPS:
        if any(tm in title_line for tm in titles):
            return key
    return None


def _apply_progressive_disclosure(base: str, convo_text: str) -> str:
    """核心常駐 + 命中意圖的節點段保留,其餘剝掉。convo_text 空 → 原樣返回(相容)。"""
    if not convo_text:
        return base
    needed = _detect_needed_nodes(convo_text)
    kept = []
    for title, body in _split_prompt_blocks(base):
        node = _classify_block(title)
        if node is None or node in needed:
            kept.append(body)
    return "\n".join(kept)


def _convo_text_for_disclosure(req) -> str:
    """漸進揭露的意圖偵測來源:近幾則對話 + (編輯既有工作流時)該工作流 YAML。
    把 YAML 納入 → 編輯含 condition/subagent/crawler… 的工作流時也會帶回對應節點段。"""
    try:
        msgs = getattr(req, "messages", None) or []
        parts = [str(m.get("content", "")) for m in msgs[-8:] if isinstance(m, dict)]
        wid = getattr(req, "workflow_id", None)
        if wid:
            try:
                import db as _db
                wf = _db.get_workflow(wid)
                if wf and wf.get("yaml"):
                    parts.append(wf["yaml"])
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""  # 出錯 → 空字串 → 全留(安全 fallback)


def _mcp_prompt_section() -> str:
    """MCP 節點教學 + 已安裝 server/tools 清單(注入 AI 助手提示詞)。

    設計原則(為弱模型優化):
      - 只在「有 enabled 的 MCP server」時回傳非空 → 沒裝就完全不提 MCP,助手不會亂用。
      - tool 以緊湊簽名列出(必填參數帶 *),杜絕虛構 server/tool 名。
      - 給一個可照抄的 YAML 範例 + 明確的「何時用 / 何時不用」規則 + 上下游搭配示例。
    """
    try:
        from db import list_mcp_servers
        servers = list_mcp_servers(enabled_only=True)
    except Exception:
        return ""
    if not servers:
        return ""

    def _sig(tool: dict) -> str:
        """工具緊湊簽名:name(a*, b) — * 為必填;array/object 參數展開一層巢狀欄位,
        否則弱模型會猜錯巢狀欄位名(實測:memory 的 entityType 被猜成 type)。"""
        schema = tool.get("input_schema") or {}
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        parts = []
        for p, spec in list(props.items())[:6]:
            s = p + ("*" if p in req else "")
            spec = spec or {}
            inner = None
            if spec.get("type") == "array" and isinstance(spec.get("items"), dict):
                inner = spec["items"].get("properties")
                wrap = "[{{{0}}}]"
            elif spec.get("type") == "object":
                inner = spec.get("properties")
                wrap = "{{{0}}}"
            if inner:
                ireq = set((spec.get("items") or spec).get("required") or [])
                ikeys = [k + ("*" if k in ireq else "") for k in list(inner.keys())[:6]]
                s += wrap.format(", ".join(ikeys))
            parts.append(s)
        return f"{tool['name']}({', '.join(parts)})"

    lines = [
        "",
        "## MCP 節點(呼叫外部 MCP server 的工具)",
        "",
        "使用者已安裝以下 MCP server(**只能用這份清單裡的 server 與 tool 名稱,禁止虛構**):",
        "",
    ]
    for s in servers:
        lines.append(f"### server `{s['name']}`")
        for t in (s.get("tools_cache") or [])[:15]:
            desc = (t.get("description") or "").strip().split("\n")[0]
            if len(desc) > 80:
                desc = desc[:80] + "…"
            lines.append(f"- `{_sig(t)}` — {desc}")
        lines.append("")
    lines += [
        "**YAML 寫法**(mcp 節點,欄位固定如下):",
        "```yaml",
        "  - name: 讀取資料",
        "    mcp: true",
        f"    mcp_server: {servers[0]['name']}   # 只填 server 名,系統自動帶 command/args",
        "    mcp_tool: <上面清單裡的 tool 名>",
        "    mcp_tool_args:",
        "      <參數名>: \"<值,可用 {{ input.x }} 或 {{ steps.上一步.output.path }}>\"",
        "    output:",
        "      path: mcp_result.json",
        "```",
        "",
        "**使用規則**:",
        "1. 內建節點優先:爬網頁用 web_crawler、寄信用 outlook、桌面操作用 computer_use、寫程式處理資料用 skill 節點。",
        "   **只有內建節點做不到、且上面清單裡有對應 tool 時才用 mcp 節點**(如 GitHub 操作、知識圖譜記憶、外部資料庫)。",
        "2. `mcp_tool_args` 的值支援 {{ }} 變數(引用 input 或上游 step 輸出)。",
        "3. 結果寫入 output.path:結構化結果存成 JSON,下游可用 `{{ steps.<步名>.output.<鍵> }}` 取值;",
        "   下游 skill 節點也可直接讀該檔案路徑處理。",
        "4. 典型搭配:`mcp 取資料 → skill 分析/轉檔 → human_confirm → outlook 寄出`;",
        "   或 `web_crawler 爬頁 → skill 整理 → mcp 寫入外部系統`。",
        "5. 標了 * 的參數必填;參數名(含巢狀欄位名)必須跟簽名完全一致。",
        "6. **規劃含 mcp 節點的工作流前,先呼叫 `test_mcp_server` 工具實測連線**;",
        "   連線失敗 → 不要用 mcp 節點,直接告訴使用者「MCP server 連線失敗(原因)、所以改用其他方案」,",
        "   並建議到設定頁 MCP 專區檢查。工作流失敗自我修復、或使用者要求檢測時,也用這個工具診斷。",
    ]
    return "\n".join(lines)


def _build_pipeline_system_prompt(channel: str = "desktop", convo_text: str = "") -> str:
    """組裝 AI 助手 system prompt：底稿 + 動態注入已安裝的 Agent Skills + Outlook 模板清單。

    channel:
      - "telegram"：TG bot 通道(手機 / 遠端)、會包含 ad-hoc 派子代理工具與教學
      - "desktop" (預設):桌面 :3005 chat、聚焦在畫板 / workflow 規劃，
        把 TG_ONLY 標記之間的 subagent 章節剝掉、節省 token + 避免 LLM 誤呼
        不存在的工具。
    """
    base = _PIPELINE_SYSTEM_BASE
    import re as _re
    if channel == "telegram":
        # TG 通道 → strip DESKTOP_ONLY 區段(畫布按鈕、YAML_READY 等 web 才有的概念)
        base = _re.sub(
            r"<!--DESKTOP_ONLY_BEGIN-->.*?<!--DESKTOP_ONLY_END-->\s*",
            "",
            base,
            flags=_re.DOTALL,
        )
    if channel != "telegram":
        # 把 <!--TG_ONLY_BEGIN--> ... <!--TG_ONLY_END--> 之間整塊拿掉(含 marker)
        base = _re.sub(
            r"<!--TG_ONLY_BEGIN-->.*?<!--TG_ONLY_END-->\s*",
            "",
            base,
            flags=_re.DOTALL,
        )
    # ── 漸進揭露:核心常駐、節點專屬大段依對話意圖注入(convo_text 空則全留)──
    base = _apply_progressive_disclosure(base, convo_text)
    parts = [base]
    # ── Agent Skills 清單 ──────────────────────────────────────────────
    try:
        from skill_scanner import list_available_skills
        skills = list_available_skills()
        if skills:
            lines = ["", "## 使用者已安裝的 Agent Skills（掛載時請用 display_name）：", ""]
            for s in skills:
                desc = (s.get("description") or "").strip()
                if len(desc) > 120:
                    desc = desc[:120] + "…"
                lines.append(f"- **{s['display_name']}**：{desc}")
            lines.append("")
            lines.append("使用者任務若與上述 skill 相關，**優先建議掛載對應 skill**（YAML 加 `skill: <display_name>`）。")
            parts.append("\n".join(lines))
    except Exception:
        pass
    # ── MCP servers(有安裝才注入;沒裝 → 助手不知道 MCP、不會亂用)─────────
    try:
        _mcp_sec = _mcp_prompt_section()
        if _mcp_sec:
            parts.append(_mcp_sec)
    except Exception:
        pass
    # ── Secrets 名稱清單(只給名稱、值永不進提示詞;沒存任何 secret 就不提)──
    try:
        from secrets_vault import list_secret_names
        _snames = [s["name"] for s in list_secret_names()]
        if _snames:
            parts.append(
                "\n## 使用者已存的 Secrets(憑證,只列名稱)\n"
                + "、".join(f"`{n}`" for n in _snames)
                + "\n需要 API key / 密碼的欄位用 `{{ secrets.名稱 }}` 引用(執行時才解密);"
                  "**禁止**請使用者把明文金鑰貼進 YAML 或對話。清單外的名稱不可虛構;"
                  "缺的請使用者到設定頁 Secrets 區新增。"
            )
    except Exception:
        pass
    # ── Outlook 模板清單 ──────────────────────────────────────────────
    # 對 outlook_automation 節點來說，挑對模板比讓 LLM 自由發揮穩很多。
    try:
        lines = ["", "## Outlook 自動化節點可用模板（outlook_template 欄位）：", ""]
        for tid, label, desc, params in _OUTLOOK_TEMPLATES_FOR_PROMPT:
            lines.append(f"- **`{tid}`** — {label}")
            lines.append(f"  - 用途：{desc}")
            lines.append(f"  - 主要參數：{params}")
        lines.append("")
        lines.append("使用者談到 Outlook 任務時，**先比對上面模板**。挑最貼近意圖的模板，"
                     "把使用者提供的資訊填到 `outlook_params` 裡。**沒有合適模板**才退而填空 `outlook_template:` "
                     "改用 `batch:` 自由描述需求走 LLM 路徑。")
        lines.append("")
        lines.append("**多信箱(重要)**:`folder` 參數預設 `inbox` = Outlook 裡的**第一個**信箱。"
                     "使用者若同時有私人與公司帳號、且要撈的是非第一個帳號,必須把 folder 寫成"
                     "「信箱名/資料夾名」,例 `ABC@company.com/收件匣`、`user@corp.com/Inbox/客訴`。"
                     "使用者提到「公司信箱」「另一個信箱」「工作信箱」時,**先問清楚是哪個帳號**"
                     "(或請他到 Outlook 左欄看信箱顯示名),不要預設抓 inbox 抓到錯的帳號。")
        lines.append("**大信箱效能**:公司信箱常有數萬封,Outlook COM 逐封掃會很久 →"
                     "這類步驟 timeout 建議 1800-3600、並盡量用 since/until 縮小範圍。")
        lines.append("**相對日期(每日排程必看)**:`since` / `until` 直接吃關鍵字 —— "
                     "`today`/`今天`/`本日`/`當日`/`今日`、`yesterday`/`昨天`/`昨日`、"
                     "`tomorrow`/`明天`/`明日`,執行當下才換算。"
                     "所以「每天撈當天信」**寫死 `\"since\":\"today\"` 就好**,"
                     "**不要改用 `{{ input.date }}`** —— 那是啟動參數,使用者直接按「執行」"
                     "沒帶參數時會變成空字串、日期過濾整個失效(變成撈全部)。"
                     "`today` 則手動執行與 cron 排程都正確,面板上也看得到真實值。")
        lines.append("**內文分析**:下游若要讀信件內文(抽數字、分類、摘要),"
                     "`daily_todo` 必須加 `\"include_body\":true` —— 預設只輸出"
                     "收件時間/寄件人/主旨/未讀/有附件五欄,不含內文,下游會拿到空值。")
        lines.append(
            "**⏰ 排程時間會決定日期範圍該怎麼寫(使用者提到「每天」就要問)**:"
            "`since:\"today\"` 只涵蓋「當天午夜 → 執行當下」。所以排程若掛在早上,"
            "當天剩下的信永遠不會進報表 —— 而且**不會報錯、沒有任何提示**,"
            "使用者只會覺得資料怪怪的。你看不到使用者的排程設定(cron 不在你的上下文裡),"
            "所以**使用者說「每天跑」「每日報表」時,你必須主動問他打算幾點跑**,再照下表給設定:\n"
            "  - 晚上跑(例 23:00)、要當天的 → `\"since\":\"today\"`\n"
            "  - 早上跑(例 08:00)、要完整一天 → `\"since\":\"yesterday\"` + `\"until\":\"today\"`"
            "(撈昨天整天;昨天的信已定案、不會漏)\n"
            "  - 使用者明說「只要早上到現在」→ `\"since\":\"today\"` 即可,不必多問\n"
            "**別默默選一個就產 YAML** —— 這個坑踩了很難自己發現。")
        parts.append("\n".join(lines))
    except Exception:
        pass
    # ── Subagent 可用 role 清單(內建 + 自訂、動態)──────────────────
    # 規範跟 BUILTIN_ROLE_IDS / 自訂 yaml 對齊,AI 助手不准用清單外的 role 名
    try:
        from pipeline.subagent_runner import load_roles, BUILTIN_ROLE_IDS
        all_roles = load_roles()
        if all_roles:
            lines = ["", "## 可用 Subagent role 清單(寫 `subagent_role:` 只能用這些)", ""]
            for rid in sorted(all_roles.keys(), key=lambda r: (0 if r in BUILTIN_ROLE_IDS else 1, r)):
                cfg = all_roles[rid]
                src_tag = "(內建)" if rid in BUILTIN_ROLE_IDS else "(自訂)"
                desc = (cfg.get("description") or cfg.get("label") or "").strip()
                tools_list = cfg.get("tools", [])
                lines.append(f"- **`{rid}`** {src_tag} — {desc}")
                lines.append(f"  - 工具:{tools_list}")
            lines.append("")
            lines.append(
                "**規則**:寫 `subagent_role: X` 之前 X 必須在上面清單裡。"
                "若使用者要的能力沒對應 role、用 `create_subagent_role` 工具新增"
                "(兩步協議:confirm=False 預覽 → 使用者 yes → confirm=True 真寫)、"
                "寫好之後新 role 立刻可在 workflow YAML 使用。"
            )
            parts.append("\n".join(lines))
    except Exception:
        pass
    # ── 今日日期(讓 web_search query 能用最新年份)──────────────────
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            now = datetime.now(ZoneInfo("Asia/Taipei"))
        except Exception:
            now = datetime.now()
        ymd = now.strftime("%Y-%m-%d")
        weekday_zh = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][now.weekday()]
        parts.append(
            f"\n\n## 📅 今日日期\n\n"
            f"**現在是 {ymd}（{weekday_zh}）、{now.strftime('%H:%M')}**(Asia/Taipei)\n\n"
            f"**重要**：使用 `web_search` 時、query 含年份請用「{now.year}」"
            f"或「{now.year - 1}-{now.year}」、不要用陳舊年份(例：'2023 best XX')— "
            f"那會搜到過期資訊。"
        )
    except Exception:
        pass
    # ── In-flight 子代理 digest(Phase 2:每次 chat turn 自動匯報) ─────
    # 只有 TG 通道才注入 — 桌面 AI 助手不派子代理、看到 digest 也沒用、徒增 token
    # 規範詳見 system prompt 內「In-flight 子代理狀態主動匯報」章節(同樣只有 TG 看得到)
    try:
        from chat_tools import _chat_subagents
        import time as _t
        snapshot = list(_chat_subagents.items())  # avoid concurrent mutation
        if snapshot:
            now_ts = _t.time()
            running: list[tuple[str, dict, int]] = []
            recent_done: list[tuple[str, dict, int]] = []
            for tid, info in snapshot:
                state = info.get("state")
                if state == "running":
                    started = info.get("started_at", now_ts)
                    running.append((tid, info, int(now_ts - started)))
                elif state in ("completed", "failed"):
                    ended = info.get("ended_at") or 0
                    ago = int(now_ts - ended)
                    if 0 <= ago <= 60:  # 60s 內才算 "剛完成"、要主動提
                        recent_done.append((tid, info, ago))
            if running or recent_done:
                lines = [
                    "",
                    "## 📡 In-flight 子代理（主動匯報；超過 60s 完成的不要重複提）",
                    "",
                ]
                for tid, info, elapsed in running:
                    role = info.get("role", "?")
                    preview = (info.get("task") or "")[:80].replace("\n", " ")
                    lines.append(f"- `{tid}` ({role}) **running** {elapsed}s — {preview}")
                for tid, info, ago in recent_done:
                    state = info.get("state")
                    role = info.get("role", "?")
                    r = info.get("result") or {}
                    success_mark = "✅" if r.get("success") else "❌"
                    summary = (r.get("summary") or "").replace("\n", " ")[:140]
                    wd = info.get("working_dir", "")
                    lines.append(
                        f"- `{tid}` ({role}) **{state}** {ago}s ago {success_mark} — "
                        f"{summary} (in `{wd}`)"
                    )
                parts.append("\n".join(lines))
    except Exception:
        pass
    return "".join(parts)


class PipelineChatRequest(BaseModel):
    messages: list[dict]
    workflow_id: Optional[str] = None  # 若帶，會把該工作流當前 canvas/YAML 注入 system prompt，
                                       # 讓 AI 能理解「在現有工作流加步驟」的增量需求
    extra_system: Optional[str] = None  # 額外 system 段落、會 append 到 system prompt 末尾。
                                        # TG 通道用來注入「目前狀態 digest」(最近工作流/run/log 摘要)


# 送 LLM 前保留最近多少則訊息（避免對話太長 token 爆炸 / 花錢）
# 設 30 大致能容納「規劃 → 修改 → 再修改」幾輪；早期概念性討論遺忘可接受
# 會真的寫入工作流的工具。判定「AI 宣稱套用了但其實沒寫」不能只看 `confirm is True`:
# LLM 常送 "confirm": "true"(字串),pydantic 會轉成 True、工具照寫,但原始 args 是字串
# → 舊判定失敗 → 明明寫了卻硬加「其實沒寫入」。使用者照提示再講一次,
#   patch_node_actions 的 append/insert 會**再做一遍**,動作變成兩份(資料損壞)。
_WRITE_TOOLS = ("save_workflow_yaml", "create_workflow_yaml", "patch_node_actions")


def _wrote_for_sure(tname: str, targs: dict, tresult=None) -> bool:
    """這個 tool call 是不是「確定有寫進 DB」。

      · 是寫入類工具
      · confirm 為真(寬鬆解析,見上面註解)
      · 回傳字串以 ✅ 開頭(三個寫入工具成功時都是這個開頭)。只看參數的話,
        confirm=True 但因為找不到步驟 / schema 不過 / 轉畫布失敗而沒寫的情況會被假放行。
    """
    if tname not in _WRITE_TOOLS:
        return False
    if str((targs or {}).get("confirm", "")).strip().lower() not in ("true", "1", "yes"):
        return False
    return str(tresult or "").lstrip().startswith("✅")


_CHAT_HISTORY_CAP = 30


def _workflow_state_block(workflow_id: str) -> str:
    """把當前工作流的 canvas 步驟摘要 + YAML 全文拼成一段注入 system prompt。
    這段告訴 LLM「使用者現在看到的工作流長這樣」，支援增量修改需求
    （例：「再加一個人工確認節點」需要知道現有幾步、叫什麼）。
    找不到 workflow 就回空字串，fallback 到原本的「從零規劃」行為。
    """
    try:
        import db
        wf = db.get_workflow(workflow_id)
        if not wf:
            return ""
        canvas = wf.get("canvas") or {}
        nodes = canvas.get("nodes") or []
        lines = [
            "",
            "## 使用者目前正在編輯的工作流",
            f"名稱：{wf.get('name', '未命名')}（id={workflow_id}）",
            f"節點數：{len(nodes)}",
        ]
        if nodes:
            lines.append("目前節點摘要（依畫布順序）：")
            for i, n in enumerate(nodes[:20], start=1):
                ntype = n.get("type") or "?"
                data = n.get("data") or {}
                name = data.get("name") or data.get("label") or "(未命名)"
                lines.append(f"  {i}. [{ntype}] {name}")
            if len(nodes) > 20:
                lines.append(f"  ... 另有 {len(nodes) - 20} 個節點未列")
        yaml_text = (wf.get("yaml") or "").strip()
        if yaml_text:
            # 避免 YAML 過長塞爆 prompt。⚠️ 上限不可設太小:截斷後模型看不到中段、
            # 卻以為自己看的是全文 → 修改時憑節點名「腦補重建」中段,把使用者調好的
            # 長 batch 品質規格整段濃縮毀掉(實測:8KB 範例 YAML 在 3000 上限下,
            # 「只改篇數」的請求產出 54 行非要求差異 + role 被擅改)。
            _truncated = len(yaml_text) > 12000
            if _truncated:
                yaml_text = yaml_text[:6000] + "\n# ...（中段省略）...\n" + yaml_text[-6000:]
            lines.append("")
            if _truncated:
                lines.append("YAML（⚠️ 過長、此處為截斷顯示版,**不是全文**）：")
            else:
                lines.append("完整 YAML：")
            lines.append("```yaml")
            lines.append(yaml_text)
            lines.append("```")
            if _truncated:
                lines.append(
                    "🚨 **上面 YAML 是截斷版**:若使用者要求「修改這條工作流」,你**必須先呼叫 "
                    f"get_workflow_yaml(\"{workflow_id}\") 取得全文**、以全文為底稿做最小修改;"
                    "**絕不可基於這份截斷版 emit 修改後 YAML**(中段內容會被你腦補毀損)。"
                )
        lines.append("")
        lines.append(
            f"⚠️ **以上這條(id={workflow_id})才是你「現在所在」的工作流（即使對話歷史是從別條複製過來的）。**"
            "對話歷史裡若出現其他工作流名稱或 id（很可能是這條被「另存為新工作流」/改名之前的舊資訊），"
            "一律以這個當前 id 為準、不要被舊記憶誤導成在操作別條。"
        )
        lines.append(
            f"**當使用者要「執行 / 跑這條 / 再跑一次」時**：呼叫 start_workflow 請帶當前 **id「{workflow_id}」**，"
            "不要用工作流名稱——名稱可能與其他工作流重複，用名稱會撈到多條或撈錯。"
        )
        lines.append("")
        lines.append("**若使用者要求是修改 / 增量調整**（如「再加一步」、「把第 2 步改成…」），"
                     "在既有基礎上改動後回覆完整新 YAML；不是打掉重練。")
        lines.append("**若使用者要求跟現有工作流無關**（另開新題目），照常從零規劃即可。")
        return "\n".join(lines)
    except Exception:
        return ""


def _extract_text_content(raw) -> str:
    """從 LLM response.content 抽出純文字
    （Gemini/Gemma 可能回傳 list of content blocks 含 thinking + text）。"""
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(raw) if raw is not None else ""


# ── LaTeX → Unicode 清洗（與 frontend cleanLatexInChat 邏輯一致）─────────────
# LLM 偶爾違反「不要用 LaTeX」規則、輸出 $\rightarrow$ 等。
# TG 沒像 frontend 有 ReactMarkdown 後處理、會直接顯示字面亂碼。
# 故在 backend 統一清洗、桌面 / TG 都拿到乾淨輸出。
_LATEX_CMD_TO_UNICODE: dict[str, str] = {
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "to": "→", "gets": "←", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "uparrow": "↑", "downarrow": "↓", "updownarrow": "↕",
    "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "cdot": "·", "cdots": "⋯", "ldots": "…", "dots": "…",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "sigma": "σ", "tau": "τ",
    "phi": "φ", "omega": "ω",
    "infty": "∞", "forall": "∀", "exists": "∃", "in": "∈", "notin": "∉",
    "subset": "⊂", "supset": "⊃", "cup": "∪", "cap": "∩",
    "text": "", "mathrm": "", "mathbf": "", "mathit": "",
}


def _clean_latex(text: str) -> str:
    """把常見 LaTeX 命令換成 Unicode、剝掉錢字號。
    沒覆蓋的命令至少把 \\ 跟 $ 拿掉、不影響可讀性。
    跟 frontend cleanLatexInChat 行為一致。"""
    if not text or ("$" not in text and "\\" not in text):
        return text
    import re as _re

    def _replace_cmd(match):
        cmd = match.group(1)
        return _LATEX_CMD_TO_UNICODE.get(cmd, cmd)

    # 1. inline math $...$ → 內容(剝錢字號 + 解析命令)
    def _replace_inline_math(m):
        body = m.group(1)
        return _re.sub(r"\\([a-zA-Z]+)", _replace_cmd, body).strip()
    out = _re.sub(r"\$([^\$\n]+?)\$", _replace_inline_math, text)

    # 2. $ 外裸命令(例 \rightarrow 沒包 $)
    def _replace_bare_cmd(m):
        cmd = m.group(1)
        if cmd in _LATEX_CMD_TO_UNICODE:
            return _LATEX_CMD_TO_UNICODE[cmd]
        return m.group(0)  # 不認識就保留原樣
    out = _re.sub(r"\\([a-zA-Z]+)", _replace_bare_cmd, out)

    return out


# 單次 chat turn 內最多幾輪 tool calling iteration（防無限迴圈、防 token 爆）
_CHAT_MAX_TOOL_ITERATIONS = 5


def _text_protocol_tools_block(tools) -> str:
    """無原生 FC 的 provider(訂閱 claude_cli):把 chat tools 教成文字協議。

    背景:聊天迴圈原本只認 bind_tools + 回應物件的 tool_calls;CLI 永遠不回
    tool_calls → 模型把工具呼叫寫成旁白文字、沒人執行(實測:hero 一直說
    「撈資料中」卻永遠沒結果)。工作流引擎的 skill/subagent 早有文字協議,
    這裡是把同一套接進聊天迴圈。
    """
    lines = [
        "",
        "## 🔧 工具呼叫方式(本環境無原生 function calling、使用文字協議)",
        "需要工具時,輸出以下格式後**立刻停止**(系統會執行並把結果接回對話,你再繼續):",
        "<tool>工具名</tool>",
        '<input>{"參數名": "值"}</input>',
        "一次只呼叫一個工具;參數一律用 JSON 物件。",
        "**參數要照下面每個工具的說明給全**;標 `*` 的是必填,標 `=值` 的是可省略(省略就用該預設值)。",
        "不需要工具時直接回答使用者,**絕對不要**輸出 <tool> 標籤或宣稱「正在撈資料」。",
        "",
        "可用工具:",
    ]
    for t in tools:
        # 參數簽名:名稱 + 必填標記 + 型別 + 預設值。
        # 原生 FC 走的是完整 JSON schema,模型看得到型別與必填;文字協議若只給
        # 參數名,模型會漏填或填錯型別。
        sig = ""
        try:
            if getattr(t, "args_schema", None):
                sch = t.args_schema.model_json_schema()
                req = set(sch.get("required") or [])
                parts = []
                for k, v in (sch.get("properties") or {}).items():
                    ty = v.get("type") or ""
                    if k in req:
                        parts.append(f"{k}*{':' + ty if ty else ''}")
                    else:
                        dv = v.get("default")
                        parts.append(f"{k}{':' + ty if ty else ''}={dv!r}" if dv is not None
                                     else f"{k}{':' + ty if ty else ''}")
                sig = ", ".join(parts)
        except Exception:
            sig = ""
        # ⚠️ description 要**整段**帶,不能只取第一行。
        # 參數的實際語意寫在 docstring 的 Args 區塊(args_schema 的 description
        # 欄位全是 None),只取第一行會把它整段砍掉 —— 實測 save_workflow_yaml 的
        # 「confirm: False 預覽、True 真寫(等使用者明確同意才設 True)」這條
        # 兩步核准核心規則就是這樣消失的,模型看不到就會直接 confirm=True 覆寫工作流。
        # 成本:全帶約 11K 字元(~5.5K token),仍遠低於原生 FC 的 JSON schema
        # (每工具 400-600 token × 20+ 個),不會爆 context。
        desc = (t.description or "").strip()
        if len(desc) > 700:
            desc = desc[:700] + "…"
        desc = "\n  ".join(desc.splitlines())
        lines.append(f"- **{t.name}**({sig})\n  {desc}")
    return "\n".join(lines)


def _parse_text_protocol_calls(text: str, tools_by_name: dict) -> tuple[list[dict], list[str]]:
    """把 <tool>name</tool><input>...</input> 文字協議解析成原生 tool_calls 形狀。

    回傳 (calls, errors):
      calls  = [{"name","args","id"}],讓聊天迴圈既有的工具執行機器原封不動地跑
      errors = 給模型看的錯誤說明(工具名不存在、參數 JSON 壞掉等)

    ⚠️ 為什麼要回 errors 而不是靜默略過(踩過的坑):
    舊版遇到「工具名不存在」或「參數解析失敗」時直接 `continue`,結果
    calls 是空的 → 聊天迴圈當成「這輪沒工具要跑」直接結束 → 串流過濾器
    已經在 <tool> 處停止輸出,使用者只看到半句話、後面全消失,**且沒有任何提示**。
    現在把錯誤回給模型,讓它自己更正重試。
    """
    import json as _json
    from pipeline.executor import _parse_skill_tool_calls
    calls: list[dict] = []
    errors: list[str] = []
    for i, c in enumerate(_parse_skill_tool_calls(text or "")):
        name = (c.get("tool") or "").strip()
        raw = (c.get("input") or "").strip()
        if not name:
            continue
        if name not in tools_by_name:
            errors.append(f"沒有名為「{name}」的工具,請從上面「可用工具」清單挑一個正確的名稱。")
            continue

        args = None
        try:
            v = _json.loads(raw)
            if isinstance(v, dict):
                args = v
        except Exception:
            pass

        if args is None:
            # JSON 解析失敗 → 只有「單參數工具」能安全猜測要塞哪個參數。
            # ⚠️ 多參數工具絕不可猜:舊版取「第一個必填參數」,對
            # save_workflow_yaml 而言就是 query,結果模型只要 JSON 寫壞,
            # 整份 YAML 會被當成「工作流名稱搜尋字串」送出去 —— 不報錯、
            # 靜默做錯事。寧可回錯誤讓模型重寫,也不要賭。
            props: list[str] = []
            try:
                t = tools_by_name.get(name)
                if t is not None and getattr(t, "args_schema", None):
                    props = list((t.args_schema.model_json_schema().get("properties") or {}).keys())
            except Exception:
                props = []
            if len(props) == 1 and raw:
                args = {props[0]: raw}
            elif not props:
                args = {}          # 無參數工具:空 input 合法
            else:
                errors.append(
                    f"工具「{name}」的 <input> 不是合法 JSON 物件。"
                    f"該工具有多個參數({', '.join(props)}),請重新輸出完整 JSON,"
                    f'例:<input>{{"{props[0]}": "..."}}</input>'
                )
                continue

        calls.append({"name": name, "args": args, "id": f"tp{i}"})
    return calls, errors


class _TextProtocolStreamFilter:
    """串流時把 <tool> 協議文字從使用者畫面藏起來:標籤前的說明照流、標籤起全部吞掉。

    尾端保留 8 字緩衝,避免「<tool>」被 chunk 邊界切成兩半而漏出。
    """

    def __init__(self):
        self.buf = ""
        self.sent = 0
        self.stopped = False

    def feed(self, s: str) -> str:
        self.buf += s
        if self.stopped:
            return ""
        idx = self.buf.find("<tool>")
        if idx >= 0:
            out = self.buf[self.sent:idx]
            self.sent = idx
            self.stopped = True
            return out
        safe_end = max(self.sent, len(self.buf) - 8)
        out = self.buf[self.sent:safe_end]
        self.sent = safe_end
        return out

    def flush(self) -> str:
        if self.stopped:
            return ""
        out = self.buf[self.sent:]
        self.sent = len(self.buf)
        return out


def _friendly_llm_error(e: Exception, est_input_tokens: int | None = None) -> tuple[int, str]:
    """把 LLM build / invoke 階段的常見 exception 翻譯成繁中友善訊息。
    回傳 (HTTP status code, 訊息)。

    這是給 chat / agent 用的 error wrapper、讓桌面 + TG 都看到一致的原因說明
    （而非 raw stack trace）。

    est_input_tokens(選填):呼叫端已知這次輸入的估算 token 數(低估值)時傳入,
    用來偵測「單次輸入就超過模型 TPM、重試永遠無效」→ 直接給換模型的明確指引。
    """
    name = type(e).__name__
    msg = str(e) or ""
    msg_lc = msg.lower()

    # 0. 單次輸入超過 TPM:最高優先(這種 429 重試無解,要給明確的換模型指引)
    if est_input_tokens:
        try:
            from llm_factory import tpm_overflow_hint
            _hint = tpm_overflow_hint(msg, est_input_tokens)
            if _hint:
                return 429, _hint
        except Exception:
            pass

    # 1. 未設定 provider / model
    if isinstance(e, KeyError) and "provider" in msg.lower():
        return 400, "未設定 LLM。請到設定頁選 provider（Groq / Gemini / Ollama / OpenRouter）+ model"
    if isinstance(e, ValueError) and msg_lc.startswith("unknown provider"):
        return 400, f"LLM provider 設定有誤：{msg}。請到設定頁重選 provider"

    # 2. API key 缺
    if "api_key" in msg_lc and ("missing" in msg_lc or "not provided" in msg_lc or "required" in msg_lc):
        return 400, f"LLM API Key 缺：{msg[:200]}。到 backend/.env 填、或設定頁設定"
    if "googleapierror" in msg_lc and "api key" in msg_lc:
        return 400, "Gemini API Key 無效或未設定、請到 backend/.env 填 GEMINI_API_KEY"
    if name == "AuthenticationError" or "401" in msg or "unauthorized" in msg_lc or "invalid api key" in msg_lc:
        return 401, f"LLM API Key 無效：{msg[:200]}。請更新 .env 或設定頁的 key"

    # 3. 配額 / rate limit
    if "rate" in msg_lc and "limit" in msg_lc:
        return 429, f"LLM API 觸發 rate limit、請稍後再試或換 model：{msg[:200]}"
    if "quota" in msg_lc or "exceeded" in msg_lc or "429" in msg:
        return 429, f"LLM API 配額用完、請換 model 或等配額重置：{msg[:200]}"

    # 4. 網路 / 連線
    if name in ("ConnectionError", "ConnectError", "ConnectTimeout"):
        return 503, f"連不上 LLM API（{name}）：{msg[:200]}。檢查網路或 API endpoint 設定"
    if ("timeout" in msg_lc or "timed out" in msg_lc
        or name in ("ReadTimeout", "Timeout", "TimeoutError")):
        return 504, f"LLM API 逾時：{msg[:200]}。可能網路慢或 model 太大、可換更快的 model 試試"
    if name == "APIConnectionError":
        return 503, f"無法連到 LLM API：{msg[:200]}"

    # 5. 模型不認得
    if "model" in msg_lc and ("not found" in msg_lc or "does not exist" in msg_lc or "404" in msg):
        return 400, f"LLM model 名稱無效：{msg[:200]}。請到設定頁挑現有 model"

    # 6. 未指定提供商支援
    if isinstance(e, ImportError):
        if "groq" in msg_lc or "gemini" in msg_lc or "ollama" in msg_lc or "openai" in msg_lc:
            return 500, f"缺套件無法用此 provider：{msg[:200]}。pip install 對應套件再試"

    # 7. 其他 — 給原始 type + 前 200 字
    return 500, f"LLM 呼叫失敗（{name}）：{msg[:300]}"


def _memory_filtered_tools(tools: list) -> list:
    """memory_enabled=False 時把記憶工具從工具清單拿掉(省 schema token)。"""
    try:
        from settings import get_settings
        if get_settings().get("memory_enabled", True):
            return tools
        from chat_tools import MEMORY_TOOL_NAMES
        return [t for t in tools if t.name not in MEMORY_TOOL_NAMES]
    except Exception:
        return tools


def _memory_snapshot_text() -> str:
    """記憶快照(注入 system 之後的獨立 message、不污染 cacheable 主 prompt)。
    memory_enabled=False 或無 facts → 回空字串。"""
    try:
        from settings import get_settings
        if not get_settings().get("memory_enabled", True):
            return ""
        import memory as _mem
        facts = _mem.snapshot(limit=25)
    except Exception:
        return ""
    if not facts:
        # 還沒記任何事 — 仍告訴 LLM 它有記憶能力,否則永遠不會主動 remember
        return ("[你具備長期記憶。使用者明確要你「記住 / 記一下」偏好或事實時(例「我報告都要正式 Word」),"
                "呼叫 remember_fact(走 confirm 兩步)記下,跨對話永久記得。一次性需求不要記。]")
    lines = ["[關於這位使用者,你已經記得這些(長期記憶、可直接參考、不必再查工具):]"]
    for f in facts:
        src = "(推測)" if f.get("source") == "inferred" else ""
        lines.append(f"  - {f['key']} = {f['value']}{src}")
    lines.append("使用者問他自己的偏好 / 習慣(例「我都用哪個模型」「我報告要多長」「我幾點跑」)時 ——"
                 "先看上面這份記憶回答;**若上面沒列到該項、先呼叫 list_facts 查全部記憶再回答,不要直接說「沒記錄」、"
                 "也不要去查系統當前狀態(模型設定 / cron)當作答案**。"
                 "使用者問「上次 / 之前聊的那個…」「我們之前討論的 X 結論」這類過去對話的事 → 呼叫 recall_episode 查對話摘要。"
                 "使用者明確要你「記住」某事 → remember_fact(confirm 兩步);標(推測)是系統推斷、可能不準,更正用 forget_fact。")
    return "\n".join(lines)


_autoshelve_tasks: set = set()


async def _autoshelve_memory(messages: list, reply: str, workflow_id=None):
    """對話結束時 fire-and-forget:摘要這段對話存成 episode;memory_aggressive 開時
    順便保守萃取使用者偏好存 inferred fact。memory_enabled=False 或對話太短 → 跳過。"""
    try:
        from settings import get_settings
        s = get_settings()
        if not s.get("memory_enabled", True):
            return
        convo = [m for m in (messages or []) if m.get("role") in ("user", "assistant") and m.get("content")]
        if sum(1 for m in convo if m["role"] == "user") < 2:
            return  # 至少兩輪使用者發言才值得存(避免一次性問答洗版)
        convo = convo + [{"role": "assistant", "content": reply}]
        import hashlib as _h, json as _j, re as _re
        first_user = next((m["content"] for m in convo if m["role"] == "user"), "")
        conv_key = workflow_id or ("c_" + _h.md5(first_user.encode("utf-8", "replace")).hexdigest()[:12])
        aggressive = bool(s.get("memory_aggressive", False))
        import memory as _mem
        from llm_factory import build_llm
        from langchain_core.messages import SystemMessage, HumanMessage
        convo_text = "\n".join(f"{m['role']}: {str(m['content'])[:500]}" for m in convo[-24:])
        if aggressive:
            instr = ('用 1-3 句繁體中文摘要這段對話(使用者想做什麼、實際做了什麼)。'
                     '另外【積極】抓出使用者透露的偏好 / 習慣 / 身份 / 領域 / 慣用做法 —— '
                     '寧可多抓也不要漏;只排除「這次一次性的具體任務」。'
                     '範例:使用者說「我習慣看正式 Word 排版、markdown 我不太看」'
                     '→ prefs 應含 {"key":"report_format","value":"偏好正式 Word、不要 markdown","category":"workflow_pref"}。'
                     '真的完全沒透露任何偏好才給空陣列。'
                     '只回 JSON、不要其他字:'
                     '{"summary":"...","prefs":[{"key":"短鍵英數","value":"值","category":"workflow_pref|domain|preference"}]}')
        else:
            instr = ('用 1-3 句繁體中文摘要這段對話(使用者想做什麼、實際做了什麼)。'
                     '只回 JSON、不要其他字:{"summary":"..."}')
        llm = build_llm(temperature=0.2)
        resp = await llm.ainvoke([SystemMessage(content=instr), HumanMessage(content=convo_text)])
        # Gemma 4 / Claude 的 content 可能是 structured blocks(list of {type,text/thinking});
        # 要抽 type=='text' 的 text、不能直接 str()(會變 Python repr、parse 不到 JSON)
        _c = resp.content
        if isinstance(_c, list):
            txt = "".join(b.get("text", "") for b in _c if isinstance(b, dict) and b.get("type") == "text")
        else:
            txt = _c if isinstance(_c, str) else str(_c)
        mobj = _re.search(r"\{.*\}", txt, _re.DOTALL)
        raw = mobj.group(0) if mobj else ""
        data = {}
        if raw:
            try:
                data = _j.loads(raw)               # 標準 JSON
            except Exception:
                try:
                    import ast as _ast
                    data = _ast.literal_eval(raw)  # Gemma 常回單引號 dict
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
        # 連 dict 都解不出 → 退而求其次:整段清理後當摘要(至少 episode 存得進)
        if not data.get("summary"):
            _clean = _re.sub(r"```\w*|```", "", txt).strip()
            data["summary"] = _clean[:300]
        summary = (data.get("summary") or "").strip()
        import logging as _lg2
        if summary:
            _mem.add_episode(conv_key, summary)
        _lg2.getLogger(__name__).info(
            f"[autoshelve] episode存={bool(summary)} aggressive={aggressive} "
            f"prefs={len(data.get('prefs') or [])} summary={summary[:40]!r}")
        if aggressive:
            for p in (data.get("prefs") or [])[:5]:
                k = (p.get("key") or "").strip()
                v = (p.get("value") or "").strip()
                if k and v:
                    rr = _mem.remember_fact(k, v, category=p.get("category", "preference"),
                                            source="inferred", confidence=0.6)
                    _lg2.getLogger(__name__).info(f"[autoshelve] 萃取記入 {k}={v} → {rr.get('ok')}")
    except Exception as e:
        import logging as _lg
        _lg.getLogger(__name__).warning(f"[autoshelve] 失敗(不影響對話):{type(e).__name__}: {e}")


def _fire_autoshelve(messages: list, reply: str, workflow_id=None):
    """非阻塞觸發 autoshelve(保存 task 引用避免 GC)。"""
    try:
        import asyncio as _aio
        t = _aio.create_task(_autoshelve_memory(messages, reply, workflow_id))
        _autoshelve_tasks.add(t)
        t.add_done_callback(_autoshelve_tasks.discard)
    except Exception:
        pass


async def _chat_agent_loop(
    req: PipelineChatRequest,
    on_tool_event=None,
):
    """AI 助手 agent loop 核心。

    on_tool_event:Optional[Awaitable callable]
        簽名:async def cb(phase: "before" | "after", tool_call: dict, result: str | None)
        - phase="before":在 tool 呼叫前 fire(result=None)
        - phase="after":tool 回傳後 fire(result=tool 回值字串)
        TG handler 用這個推進度訊息。

    跟 pipeline_chat 一致回 dict {reply, has_yaml, yaml_content, yaml_error}。
    """
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
    from llm_factory import build_llm
    from chat_tools import CHAT_TOOLS, CHAT_TOOLS_BY_NAME
    import re
    import logging as _log

    # build_llm 失敗:provider/key 沒設、套件缺 → 翻譯成友善訊息
    try:
        llm = build_llm(temperature=0.3)
    except Exception as e:
        _log.warning(f"[/pipeline/chat] build_llm 失敗:{type(e).__name__}: {e}")
        sc, friendly = _friendly_llm_error(e)
        raise HTTPException(status_code=sc, detail=friendly)
    # 通道偵測：只 _chat_agent_loop 有 on_tool_event 參數(TG handler 才會傳)、
    # 桌面 chat 走 pipeline_chat_stream / pipeline_chat 都不傳 → 預設 desktop。
    # TG 通道才放 dispatch_subagent_async / check_subagent_status 兩個 ad-hoc 子代理工具
    # + 帶 in-flight digest 教學區塊。
    _channel = "telegram" if on_tool_event is not None else "desktop"
    _convo_for_disclosure = _convo_text_for_disclosure(req)
    system_prompt = _build_pipeline_system_prompt(channel=_channel, convo_text=_convo_for_disclosure)
    if req.workflow_id:
        system_prompt += _workflow_state_block(req.workflow_id)
    if req.extra_system:
        system_prompt += "\n\n" + req.extra_system

    # ── 嘗試 bind_tools；失敗就退到舊單輪 ────────────────────────
    tools_enabled = True
    # desktop 不做 ad-hoc 子代理(那是 TG 專用)、把整套 subagent admin tools 拿掉
    # 省 6 個 tool schema(每個 ~400-600 tok)、desktop 每輪節省 ~2-3K token
    _TG_ONLY_TOOLS = {
        "dispatch_subagent_async", "check_subagent_status",
        "read_subagent_file", "send_subagent_file_to_tg",
        "cancel_subagent_task", "read_help_doc",
    }
    _active_tools = CHAT_TOOLS if _channel == "telegram" else [
        t for t in CHAT_TOOLS if t.name not in _TG_ONLY_TOOLS
    ]
    _active_tools = _memory_filtered_tools(_active_tools)   # memory_enabled=False → 拿掉記憶工具
    from llm_factory import provider_has_native_fc as _has_fc
    _native_fc = _has_fc("primary")
    if not _native_fc:
        # 訂閱 CLI 無原生 FC → 文字協議:不 bind、prompt 教格式、回應解析 <tool> 轉原生形狀。
        # API 模型(_native_fc=True)完全不受影響、照走下面 bind_tools 原生路徑。
        llm_with_tools = llm
        system_prompt += _text_protocol_tools_block(_active_tools)
        _log.info("[/pipeline/chat] provider 無原生 FC(訂閱 CLI)→ 聊天工具改走文字協議")
    else:
        try:
            llm_with_tools = llm.bind_tools(_active_tools)
        except Exception as e:
            _log.warning(f"[/pipeline/chat] bind_tools 失敗、退到單輪：{e}")
            llm_with_tools = llm
            tools_enabled = False

    # Prompt caching (#153):AI 助手 system prompt 是 Atlas 最大的 input(50K+ 字、動態注入)。
    # 加 ephemeral 1h TTL cache_control、跨輪對話命中 cache、Anthropic 第 2 輪起 input cost 0.1x。
    # 注意:cache 命中需 prefix 穩定、main.py:3318 _build_pipeline_system_prompt 內動態注入區塊
    # (今日日期、in-flight digest)應放在底稿後面、保最大化 cache prefix。
    _sys_cache = {"cache_control": {"type": "ephemeral", "ttl": "1h"}}
    lc_messages: list = [SystemMessage(content=system_prompt, additional_kwargs=_sys_cache)]
    # 記憶快照:獨立一條 system message(不帶 cache_control)、放在 cacheable 主 prompt 之後、
    # 不污染主 prompt 的 cache。memory_enabled=False 或無 facts → 空、不加。
    _mem_snap = _memory_snapshot_text()
    if _mem_snap:
        lc_messages.append(SystemMessage(content=_mem_snap))
    # 只取最近 _CHAT_HISTORY_CAP 則訊息送進 LLM、避免對話太長 token 爆炸
    recent = req.messages[-_CHAT_HISTORY_CAP:] if len(req.messages) > _CHAT_HISTORY_CAP else req.messages
    for m in recent:
        cls = HumanMessage if m["role"] == "user" else AIMessage
        _c = m["content"]
        if m["role"] == "user":
            _c = _normalize_win_paths(_c)
        lc_messages.append(cls(content=_c))

    # ── Agent loop：最多 _CHAT_MAX_TOOL_ITERATIONS 輪 ─────────────
    # 用 ainvoke (async) 讓 event loop 不被 LLM call 阻塞、
    # 上層 (TG handler) 才能在等待時跑 typing keepalive
    final_response = None
    # 這個 turn 實際執行過的 tool call（native 與文字協議都記）。
    # 下方「宣稱已套用」偵測要靠它 —— 文字協議的 tool_calls 不會寫回 message。
    _executed_tool_calls: list[dict] = []
    for iteration in range(_CHAT_MAX_TOOL_ITERATIONS):
        try:
            if hasattr(llm_with_tools, "ainvoke"):
                response = await llm_with_tools.ainvoke(lc_messages)
            else:
                response = llm_with_tools.invoke(lc_messages)
        except Exception as e:
            _log.warning(f"[/pipeline/chat] LLM invoke 失敗(iter {iteration}):{type(e).__name__}: {e}")
            # 字元數//2 低估 token 數:只在「明確超過 TPM」時觸發 fail-fast 指引
            _est_in = sum(len(str(getattr(_m, "content", "") or "")) for _m in lc_messages) // 2
            sc, friendly = _friendly_llm_error(e, est_input_tokens=_est_in)
            raise HTTPException(status_code=sc, detail=friendly)
        lc_messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not _native_fc and not tool_calls:
            # 文字協議:從回覆文字解析 <tool> 呼叫、轉成原生形狀給下面既有機器執行
            tool_calls, _tp_errors = _parse_text_protocol_calls(
                str(response.content or ""), CHAT_TOOLS_BY_NAME)
            if _tp_errors and not tool_calls:
                # 協議寫壞了 → 告訴模型錯在哪、讓它重寫,不要靜默結束這輪
                # (靜默結束會讓使用者只看到 <tool> 之前的半句話)
                _log.warning(f"[/pipeline/chat] 文字協議解析失敗:{_tp_errors[0][:120]}")
                lc_messages.append(HumanMessage(content=(
                    "[系統] 你的工具呼叫格式有誤,沒有被執行:\n- "
                    + "\n- ".join(_tp_errors[:3])
                    + "\n請重新輸出一次正確的 <tool>…</tool><input>…</input>,"
                      "或直接用文字回答使用者。")))
                continue
        if not tool_calls or not tools_enabled:
            final_response = response
            break
        # 跑每個 tool call、把結果加進對話
        for tc in tool_calls:
            tname = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            targs = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "tc")
            # 統一成 dict 形式給 callback、callback 失敗不影響主流程
            tc_dict = {"name": tname, "args": targs or {}, "id": tid}
            _executed_tool_calls.append(tc_dict)   # result 於工具執行後補寫、供 _wrote_for_sure 判定   # 供下方「宣稱已套用」偵測(文字協議不會寫回 message)
            if on_tool_event:
                try:
                    await on_tool_event("before", tc_dict, None)
                except Exception as cb_e:
                    _log.warning(f"[/pipeline/chat] on_tool_event before 失敗(忽略):{cb_e}")
            tool_obj = CHAT_TOOLS_BY_NAME.get(tname)
            if not tool_obj:
                tool_result = f"[Unknown tool: {tname}]"
            else:
                try:
                    # 用 ainvoke、async tool(如 start_workflow)才能正確 await
                    # sync tool 也有 ainvoke、會自動跑 sync 邏輯、不會壞
                    if hasattr(tool_obj, "ainvoke"):
                        tool_result = await tool_obj.ainvoke(targs or {})
                    else:
                        tool_result = tool_obj.invoke(targs or {})
                except Exception as e:
                    tool_result = f"[Tool error: {type(e).__name__}: {str(e)[:300]}]"
            tc_dict["result"] = str(tool_result)[:500]   # 給 _wrote_for_sure 判定
            # Cap 單個 tool result（避免 token 爆）
            if isinstance(tool_result, str) and len(tool_result) > 16000:
                tool_result = tool_result[:16000] + f"\n... (回傳超過 16000 字、後面截掉)"
            lc_messages.append(ToolMessage(content=str(tool_result), tool_call_id=tid))
            _log.info(f"[/pipeline/chat] tool={tname} args={targs} result_len={len(str(tool_result))}")
            if on_tool_event:
                try:
                    await on_tool_event("after", tc_dict, str(tool_result))
                except Exception as cb_e:
                    _log.warning(f"[/pipeline/chat] on_tool_event after 失敗(忽略):{cb_e}")
    else:
        # for-else：迴圈正常結束（沒 break）→ 達上限沒收到純文字回覆
        _log.warning(f"[/pipeline/chat] 達 tool iteration 上限 {_CHAT_MAX_TOOL_ITERATIONS}、強制結束")
        # 最後再 invoke 一次叫 LLM 給純文字總結
        try:
            lc_messages.append(HumanMessage(content="(系統:已達工具呼叫上限、請現在用純文字回答使用者、不要再呼叫工具)"))
            if hasattr(llm_with_tools, "ainvoke"):
                final_response = await llm_with_tools.ainvoke(lc_messages)
            else:
                final_response = llm_with_tools.invoke(lc_messages)
        except Exception:
            final_response = lc_messages[-1] if lc_messages else None

    raw = final_response.content if final_response else ""
    content = _extract_text_content(raw)
    # LaTeX 清洗：LLM 偶發違規寫 $\rightarrow$,後端統一清理(桌面 + TG 都受惠)
    content = _clean_latex(content)

    has_yaml = "YAML_READY" in content
    yaml_content = None
    yaml_error = None

    # ── 偵測 LLM 違規:口頭宣稱「已套用 / 已寫入」但實際沒呼叫 save_workflow_yaml(confirm=True)
    # 也沒 emit YAML_READY block (讓前端按鈕處理寫入)。這個 bug 很常出現:
    # LLM 預覽完 (confirm=False) 後使用者說 yes、LLM 沒呼叫 confirm=True 就回「已套用」。
    # 偵測到 → reply 前面附 warning、明確告訴使用者「實際沒寫、請重試」、避免誤以為已套用
    if not has_yaml:
        _claim_patterns = ("已套用", "已寫入", "已改好", "套用完成", "改好了", "已經改", "已經套用", "已經寫入", "完成套用")
        _claimed = any(p in content for p in _claim_patterns)
        if _claimed:
            # 掃「這個 turn 實際執行過的 tool call」。
            # ⚠️ 不能只掃 lc_messages 的 .tool_calls:文字協議模式解析出來的
            # tool_calls 是區域變數、從沒寫回 AIMessage,只掃訊息會永遠找不到
            # → 即使真的成功寫入,也會硬加一句「實際上沒真的寫入」的假警告。
            # _executed_tool_calls 兩種模式都會記,才是可靠來源。
            _actually_wrote = False
            _scan = list(_executed_tool_calls)
            for _m in lc_messages:      # native 模式的保險(理論上與上面重複)
                _scan.extend(getattr(_m, "tool_calls", None) or [])
            for _tc in _scan:
                _tname = _tc.get("name") if isinstance(_tc, dict) else getattr(_tc, "name", "")
                _targs = _tc.get("args") if isinstance(_tc, dict) else getattr(_tc, "args", {})
                _tres = (_tc.get("result") if isinstance(_tc, dict) else getattr(_tc, "result", ""))
                if _wrote_for_sure(_tname, _targs, _tres):
                    _actually_wrote = True
                    break
            if not _actually_wrote:
                _log.warning(f"[/pipeline/chat] (channel={_channel}) LLM 宣稱已套用但 turn 內沒 confirm=True tool call 也沒 YAML_READY、附 warning prefix")
                if _channel == "telegram":
                    _next_step_hint = (
                        "請再跟我說一次「請套用」、我會重新產出 YAML(下次會出現一段 `YAML_READY` 區塊)、"
                        "然後你下 `/save <workflow名稱>` 套用。"
                    )
                else:
                    _next_step_hint = "請重新請我修改。一般步驟會出現「⚠ 覆蓋目前」按鈕、點下去才會真寫入;桌面自動化節點的動作序列則是我會先給你一份改動摘要、你說「好」我才會寫入。"
                content = (
                    f"⚠️ 我剛剛口頭說已套用、但**實際上沒真的寫入**(系統自動偵測)。{_next_step_hint}\n\n"
                    "(原回覆:)\n" + content
                )

    if has_yaml:
        # LLM 可能 emit 多份 yaml block(diff snippet 在前、完整版在後)。
        # 之前 re.search 只抓第一個、撞到 snippet 會 validation fail。改 findall 取最後一個。
        # 閉合圍欄必須「單獨成行」(\n```):否則 YAML 內文若出現行內 ``` (例 batch 寫
        # 『不要 markdown 圍欄(```)』) 會被非貪婪 +? 誤判成結束、把 YAML 從中間切斷。
        _yaml_blocks = re.findall(r"```yaml\s*\n([\s\S]*?)\n[ \t]*```[ \t]*(?:\n|$)", content)
        if _yaml_blocks:
            yaml_content = _yaml_blocks[-1].strip()
            # ── 語法驗證：試跑 PipelineConfig.from_dict 檢查 schema ──
            try:
                import yaml as _yaml
                from pipeline.models import PipelineConfig
                parsed = _lenient_yaml_load(yaml_content) or {}
                raw_cfg = parsed.get("pipeline", parsed)
                PipelineConfig.from_dict({k: v for k, v in raw_cfg.items() if not str(k).startswith("_")})

                # ── 空 step 偵測:AI 經典 bug — 嘴上說「改為 condition / 加 X 節點」
                #    但實際 emit 的 YAML 內 step 只寫了 name、節點 type flag / batch 全漏掉。
                #    這種 step 解析後 fallback 到空 script、UI 看起來像沒改。
                _empty_steps: list[str] = []
                for _s in (raw_cfg.get("steps") or []):
                    if not isinstance(_s, dict):
                        continue
                    _name = str(_s.get("name", "")).strip()
                    _has_batch = bool(str(_s.get("batch", "")).strip())
                    _has_type_flag = any(_s.get(k) for k in (
                        "condition", "skill_mode", "subagent", "human_confirm",
                        "computer_use", "visual_validation", "outlook_automation",
                        "web_crawler", "mcp",
                    ))
                    if _name and not _has_batch and not _has_type_flag:
                        _empty_steps.append(_name)
                if _empty_steps:
                    _names = "、".join(f"「{n}」" for n in _empty_steps)
                    yaml_error = (
                        f"⚠ step {_names} 沒有任何節點類型(batch / condition / skill_mode / ... 全空)。\n"
                        f"AI 可能口頭說『改為 condition 節點』但實際只寫了 name、漏掉 condition: true + expression + on_true。\n"
                        f"請跟 AI 說『{_empty_steps[0]} 還是空的、請補完整 condition 欄位(condition: true + expression + on_true)』。"
                    )
            except Exception as e:
                yaml_error = f"YAML 語法/結構錯誤:{type(e).__name__}:{str(e)[:300]}"

    # 對話結束 → 背景摘要存 episode(+ aggressive 時萃取偏好);不阻塞回應
    _fire_autoshelve(req.messages, content or "", req.workflow_id)
    return {"reply": content, "has_yaml": has_yaml, "yaml_content": yaml_content, "yaml_error": yaml_error}


@app.post("/pipeline/chat")
async def pipeline_chat(req: PipelineChatRequest):
    """AI 助手聊天 endpoint。包薄殼、實際邏輯在 _chat_agent_loop。

    LLM 看到 chat_tools 裡定義的 7 個 tool(read + write + send_file + web_search)、
    自己決定何時 call。bind_tools 失敗(model 不支援)時退到單輪。

    on_tool_event callback 留給內部呼叫者用(例 TG handler 推進度訊息)、
    HTTP 端點不傳 callback。
    """
    return await _chat_agent_loop(req)


async def _chat_agent_stream(req: "PipelineChatRequest"):
    """串流版 chat agent loop。yield NDJSON 事件供 SSE 端點。

    事件種類:
    - {"type": "token", "text": "..."}            # LLM 串流的文字片段
    - {"type": "tool_start", "name": "X", "args": {...}}
    - {"type": "tool_end", "name": "X", "result_preview": "...(前 200 字)"}
    - {"type": "done", "reply": "...", "has_yaml": bool, "yaml_content": str|None, "yaml_error": str|None}
    - {"type": "error", "detail": "..."}          # 任何階段失敗
    """
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
    from langchain_core.messages import AIMessageChunk
    from llm_factory import build_llm
    from chat_tools import CHAT_TOOLS, CHAT_TOOLS_BY_NAME
    import re
    import logging as _log

    # build_llm 失敗 → emit error event
    try:
        llm = build_llm(temperature=0.3)
    except Exception as e:
        sc, friendly = _friendly_llm_error(e)
        yield {"type": "error", "status_code": sc, "detail": friendly}
        return

    # SSE stream 端點(/pipeline/chat/stream)是給桌面 chat 用、永遠 desktop 通道。
    # TG handler 不走 stream、直接 await _chat_agent_loop。
    system_prompt = _build_pipeline_system_prompt(channel="desktop", convo_text=_convo_text_for_disclosure(req))
    if req.workflow_id:
        system_prompt += _workflow_state_block(req.workflow_id)
    if req.extra_system:
        system_prompt += "\n\n" + req.extra_system

    tools_enabled = True
    # stream 端點永遠 desktop、把整套 subagent admin tools 拿掉(同 _chat_agent_loop)
    _TG_ONLY_TOOLS = {
        "dispatch_subagent_async", "check_subagent_status",
        "read_subagent_file", "send_subagent_file_to_tg",
        "cancel_subagent_task", "read_help_doc",
    }
    _active_tools = [t for t in CHAT_TOOLS if t.name not in _TG_ONLY_TOOLS]
    _active_tools = _memory_filtered_tools(_active_tools)   # memory_enabled=False → 拿掉記憶工具
    from llm_factory import provider_has_native_fc as _has_fc
    _native_fc = _has_fc("primary")
    if not _native_fc:
        # 訂閱 CLI 無原生 FC → 文字協議(同 _chat_agent_loop);API 模型不受影響
        llm_with_tools = llm
        system_prompt += _text_protocol_tools_block(_active_tools)
        _log.info("[/pipeline/chat/stream] provider 無原生 FC(訂閱 CLI)→ 聊天工具改走文字協議")
    else:
        try:
            llm_with_tools = llm.bind_tools(_active_tools)
        except Exception as e:
            _log.warning(f"[/pipeline/chat/stream] bind_tools 失敗、退到單輪:{e}")
            llm_with_tools = llm
            tools_enabled = False

    # Prompt caching (#153):chat/stream endpoint 同 _chat_agent_loop 處理
    _sys_cache = {"cache_control": {"type": "ephemeral", "ttl": "1h"}}
    lc_messages: list = [SystemMessage(content=system_prompt, additional_kwargs=_sys_cache)]
    # 記憶快照:獨立 system message、不帶 cache_control、不污染主 prompt cache(同 _chat_agent_loop)
    _mem_snap = _memory_snapshot_text()
    if _mem_snap:
        lc_messages.append(SystemMessage(content=_mem_snap))
    recent = req.messages[-_CHAT_HISTORY_CAP:] if len(req.messages) > _CHAT_HISTORY_CAP else req.messages
    for m in recent:
        cls = HumanMessage if m["role"] == "user" else AIMessage
        _c = m["content"]
        if m["role"] == "user":
            _c = _normalize_win_paths(_c)
        lc_messages.append(cls(content=_c))

    full_content_parts: list[str] = []  # 累積整輪 chat 看到的純文字 content
    # 這個 turn 實際執行過的 tool call（native 與文字協議都記）。
    # 下方「宣稱已套用」偵測要靠它 —— 文字協議的 tool_calls 不會寫回 message。
    _executed_tool_calls: list[dict] = []

    for iteration in range(_CHAT_MAX_TOOL_ITERATIONS):
        # 用 astream 拿 chunks(token + tool calls 都會以 chunk 形式來)
        accumulated: AIMessageChunk | None = None
        # 文字協議模式:<tool> 起的協議文字不該流到使用者畫面 → 過濾器藏起來
        _tp_filter = _TextProtocolStreamFilter() if not _native_fc else None
        try:
            async for chunk in llm_with_tools.astream(lc_messages):
                # chunk.content:這次的 token(text)
                ctext = chunk.content if isinstance(chunk.content, str) else ""
                if not ctext and isinstance(chunk.content, list):
                    # Gemini list-of-blocks 形式、抽 text block
                    parts = []
                    for b in chunk.content:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                            parts.append(b["text"])
                    ctext = "".join(parts)
                if ctext:
                    if _tp_filter is not None:
                        _vis = _tp_filter.feed(ctext)
                        if _vis:
                            full_content_parts.append(_vis)
                            yield {"type": "token", "text": _vis}
                    else:
                        full_content_parts.append(ctext)
                        yield {"type": "token", "text": ctext}
                # 累積整體 chunk(用 + 操作符;AIMessageChunk 支援 merge)
                accumulated = chunk if accumulated is None else accumulated + chunk
        except Exception as e:
            _log.warning(f"[/pipeline/chat/stream] astream 失敗(iter {iteration}):{type(e).__name__}: {e}")
            _est_in = sum(len(str(getattr(_m, "content", "") or "")) for _m in lc_messages) // 2
            sc, friendly = _friendly_llm_error(e, est_input_tokens=_est_in)
            yield {"type": "error", "status_code": sc, "detail": friendly}
            return

        if accumulated is None:
            yield {"type": "error", "detail": "LLM 沒回任何 chunk"}
            return

        # 把 accumulated 轉成 AIMessage 加進 messages、看有沒有 tool_calls
        # AIMessageChunk 有 .tool_calls 屬性、merge 後的 chunk 也帶 tool_calls 完整資訊
        lc_messages.append(accumulated)
        tool_calls = getattr(accumulated, "tool_calls", None) or []
        if not _native_fc and not tool_calls:
            # 文字協議:從回覆文字解析 <tool> 呼叫、轉原生形狀給下面既有機器執行
            _acc_text = accumulated.content if isinstance(accumulated.content, str) else ""
            tool_calls, _tp_errors = _parse_text_protocol_calls(_acc_text, CHAT_TOOLS_BY_NAME)
            if _tp_errors and not tool_calls:
                # 協議寫壞 → 回饋給模型重寫。這裡不 break 特別重要:
                # 串流過濾器已在 <tool> 處停止輸出,若就這樣結束,使用者只會
                # 看到半句話、後面全消失且無任何提示(實測過的行為)。
                _log.warning(f"[/pipeline/chat/stream] 文字協議解析失敗:{_tp_errors[0][:120]}")
                lc_messages.append(HumanMessage(content=(
                    "[系統] 你的工具呼叫格式有誤,沒有被執行:\n- "
                    + "\n- ".join(_tp_errors[:3])
                    + "\n請重新輸出一次正確的 <tool>…</tool><input>…</input>,"
                      "或直接用文字回答使用者。")))
                if _tp_filter is not None:
                    _tp_filter = _TextProtocolStreamFilter()   # 重置,下一輪重新過濾
                continue

        if not tool_calls or not tools_enabled:
            # 這輪沒 tool、結束 agent loop;文字協議模式把過濾器尾巴補流出去
            if _tp_filter is not None:
                _tail = _tp_filter.flush()
                if _tail:
                    full_content_parts.append(_tail)
                    yield {"type": "token", "text": _tail}
            break

        # 執行 tools
        for tc in tool_calls:
            tname = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            targs = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "tc")
            # 供下方「宣稱已套用」偵測(文字協議的 tool_calls 不會寫回 message)
            tc_dict = {"name": tname, "args": targs or {}, "id": tid}
            _executed_tool_calls.append(tc_dict)   # result 於下方補寫
            yield {"type": "tool_start", "name": tname, "args": targs or {}}
            tool_obj = CHAT_TOOLS_BY_NAME.get(tname)
            if not tool_obj:
                tool_result = f"[Unknown tool: {tname}]"
            else:
                try:
                    if hasattr(tool_obj, "ainvoke"):
                        tool_result = await tool_obj.ainvoke(targs or {})
                    else:
                        tool_result = tool_obj.invoke(targs or {})
                except Exception as e:
                    tool_result = f"[Tool error: {type(e).__name__}: {str(e)[:300]}]"
            tc_dict["result"] = str(tool_result)[:500]   # 給 _wrote_for_sure 判定
            tool_result_str = str(tool_result)
            if len(tool_result_str) > 16000:
                tool_result_str = tool_result_str[:16000] + "\n... (回傳超過 16000 字、後面截掉)"
            lc_messages.append(ToolMessage(content=tool_result_str, tool_call_id=tid))
            preview = tool_result_str[:200]
            yield {"type": "tool_end", "name": tname, "result_preview": preview}
    else:
        # 達上限、再 invoke 一次叫 LLM 給純文字
        yield {"type": "tool_end", "name": "(達工具呼叫上限)", "result_preview": "強制結束"}
        try:
            lc_messages.append(HumanMessage(content="(系統:已達工具呼叫上限、請現在用純文字回答使用者、不要再呼叫工具)"))
            async for chunk in llm_with_tools.astream(lc_messages):
                ctext = chunk.content if isinstance(chunk.content, str) else ""
                if ctext:
                    full_content_parts.append(ctext)
                    yield {"type": "token", "text": ctext}
        except Exception as e:
            _log.warning(f"[/pipeline/chat/stream] 強制結束 LLM call 失敗:{e}")

    # 整合最終回覆 + LaTeX 清洗 + YAML 解析
    content = _clean_latex("".join(full_content_parts))
    has_yaml = "YAML_READY" in content
    yaml_content = None
    yaml_error = None

    # 同 _chat_agent_loop 的偵測:口頭宣稱已套用但實際沒寫 → 加 warning
    if not has_yaml:
        _claim_patterns = ("已套用", "已寫入", "已改好", "套用完成", "改好了", "已經改", "已經套用", "已經寫入", "完成套用")
        _claimed = any(p in content for p in _claim_patterns)
        if _claimed:
            # 同非串流版:文字協議的 tool_calls 不會寫回 AIMessage,只掃 lc_messages
            # 會永遠判定「沒寫入」→ 即使真的成功也硬加假警告。以實際執行紀錄為準。
            _actually_wrote = False
            _scan = list(_executed_tool_calls)
            for _m in lc_messages:
                _scan.extend(getattr(_m, "tool_calls", None) or [])
            for _tc in _scan:
                _tname = _tc.get("name") if isinstance(_tc, dict) else getattr(_tc, "name", "")
                _targs = _tc.get("args") if isinstance(_tc, dict) else getattr(_tc, "args", {})
                _tres = (_tc.get("result") if isinstance(_tc, dict) else getattr(_tc, "result", ""))
                if _wrote_for_sure(_tname, _targs, _tres):
                    _actually_wrote = True
                if _actually_wrote:
                    break
            if not _actually_wrote:
                _log.warning("[/pipeline/chat/stream] LLM 宣稱已套用但實際沒呼叫 confirm=True 也沒 YAML_READY、附 warning prefix")
                content = (
                    "⚠️ 我剛剛口頭說已套用、但**實際上沒真的寫入**(系統自動偵測)。"
                    "請重新請我修改。一般步驟會出現「⚠ 覆蓋目前」按鈕、點下去才會真寫入;桌面自動化節點的動作序列則是我會先給你一份改動摘要、你說「好」我才會寫入。\n\n"
                    "(原回覆:)\n" + content
                )

    if has_yaml:
        # 同 pipeline_chat:LLM 可能 emit 多份 yaml block、取最後一個
        # 閉合圍欄必須「單獨成行」(\n```):否則 YAML 內文若出現行內 ``` (例 batch 寫
        # 『不要 markdown 圍欄(```)』) 會被非貪婪 +? 誤判成結束、把 YAML 從中間切斷。
        _yaml_blocks = re.findall(r"```yaml\s*\n([\s\S]*?)\n[ \t]*```[ \t]*(?:\n|$)", content)
        if _yaml_blocks:
            yaml_content = _yaml_blocks[-1].strip()
            try:
                import yaml as _yaml
                from pipeline.models import PipelineConfig
                parsed = _lenient_yaml_load(yaml_content) or {}
                raw_cfg = parsed.get("pipeline", parsed)
                PipelineConfig.from_dict({k: v for k, v in raw_cfg.items() if not str(k).startswith("_")})

                _empty_steps: list[str] = []
                for _s in (raw_cfg.get("steps") or []):
                    if not isinstance(_s, dict):
                        continue
                    _name = str(_s.get("name", "")).strip()
                    _has_batch = bool(str(_s.get("batch", "")).strip())
                    _has_type_flag = any(_s.get(k) for k in (
                        "condition", "skill_mode", "subagent", "human_confirm",
                        "computer_use", "visual_validation", "outlook_automation",
                        "web_crawler", "mcp",
                    ))
                    if _name and not _has_batch and not _has_type_flag:
                        _empty_steps.append(_name)
                if _empty_steps:
                    _names = "、".join(f"「{n}」" for n in _empty_steps)
                    yaml_error = (
                        f"⚠ step {_names} 沒有任何節點類型(batch / condition / skill_mode / ... 全空)。\n"
                        f"AI 可能口頭說『改為 condition 節點』但實際只寫了 name、漏掉 condition: true + expression + on_true。\n"
                        f"請跟 AI 說『{_empty_steps[0]} 還是空的、請補完整 condition 欄位(condition: true + expression + on_true)』。"
                    )
            except Exception as e:
                yaml_error = f"YAML 語法/結構錯誤:{type(e).__name__}:{str(e)[:300]}"

    # 對話結束 → 背景摘要存 episode(+ aggressive 時萃取偏好);不阻塞回應
    _fire_autoshelve(req.messages, content or "", req.workflow_id)
    yield {
        "type": "done",
        "reply": content,
        "has_yaml": has_yaml,
        "yaml_content": yaml_content,
        "yaml_error": yaml_error,
    }


@app.post("/pipeline/chat/stream")
async def pipeline_chat_stream(req: PipelineChatRequest):
    """AI 助手聊天 SSE 串流端點。回 NDJSON(每行一個 JSON event)。

    前端可用 fetch streaming + ReadableStream reader 一行一行讀。
    比 /pipeline/chat 多:即時 token / tool_start / tool_end 事件、體驗類似 ChatGPT。
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    async def _ndjson_gen():
        try:
            async for ev in _chat_agent_stream(req):
                yield _json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:
            yield _json.dumps({"type": "error", "detail": f"stream 內部錯誤:{type(e).__name__}: {str(e)[:300]}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        _ndjson_gen(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防 nginx / proxy buffer
        },
    )


# ── Workflow Chat History（per-workflow AI 助手對話紀錄）─────────────────────
# 用途：每個工作流保留自己的對話歷史，使用者回來還能接續跟 AI 討論加功能
# 儲存在 workflows.chat_messages TEXT 欄位（JSON 陣列），更新不動 updated_at
# （聊天不代表工作流本體有變動，不想擾亂工作流列表的排序）

class ChatMessageIn(BaseModel):
    role: str   # 'user' 或 'assistant'
    content: str


class ChatBulkSetRequest(BaseModel):
    messages: list[ChatMessageIn]


@app.get("/workflows/{workflow_id}/chat")
async def get_workflow_chat_api(workflow_id: str):
    """載入指定工作流的對話歷史。"""
    import db
    msgs = db.get_workflow_chat(workflow_id)
    if msgs is None:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{workflow_id}")
    return {"messages": msgs}


@app.post("/workflows/{workflow_id}/chat")
async def append_workflow_chat_api(workflow_id: str, msg: ChatMessageIn):
    """追加一則訊息（user 或 assistant）。回傳更新後的完整訊息陣列。"""
    import db
    if msg.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role 必須是 'user' 或 'assistant'")
    result = db.append_workflow_chat(workflow_id, msg.role, msg.content)
    if result is None:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{workflow_id}")
    return {"messages": result}


@app.put("/workflows/{workflow_id}/chat")
async def set_workflow_chat_api(workflow_id: str, req: ChatBulkSetRequest):
    """一次性整批覆寫訊息（用於 scratch chat 遷移到新建立的工作流）。"""
    import db
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    ok = db.set_workflow_chat(workflow_id, msgs)
    if not ok:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{workflow_id}")
    return {"messages": db.get_workflow_chat(workflow_id)}


@app.delete("/workflows/{workflow_id}/chat")
async def clear_workflow_chat_api(workflow_id: str):
    """清空對話歷史（使用者按「🗑️ 新話題」）。"""
    import db
    ok = db.clear_workflow_chat(workflow_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"找不到工作流：{workflow_id}")
    return {"messages": []}


# ── Helpers ──────────────────────────────────────────────────
def _run_to_dict(r):
    # 成本在後端算(單一真相來源),前端只負責顯示。
    # 必須分項算 —— 快取讀取只要 input 的 1/10 價,加總再乘單價會失真。
    try:
        from token_cost import estimate_cost, sum_costs, PRICING_AS_OF
    except Exception:  # 模組缺失不該讓整個 run 詳情掛掉
        estimate_cost = sum_costs = None
        PRICING_AS_OF = ""

    _usages = [getattr(s, 'token_usage', {}) or {} for s in r.step_results]

    def _cost_of(u):
        if not (estimate_cost and u):
            return None
        try:
            return estimate_cost(u)
        except Exception:
            return None

    _run_cost = None
    if sum_costs:
        try:
            _run_cost = sum_costs(_usages)
            _run_cost["pricing_as_of"] = PRICING_AS_OF
        except Exception:
            _run_cost = None

    return {
        "run_id": r.run_id,
        "pipeline_name": r.pipeline_name,
        "status": r.status,
        "current_step": r.current_step,
        "total_steps": len(r.config_dict.get("steps", [])),
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "cost": _run_cost,
        "step_results": [
            {"step_index": s.step_index, "step_name": s.step_name, "exit_code": s.exit_code,
             "validation_status": s.validation_status, "validation_reason": s.validation_reason,
             "validation_suggestion": s.validation_suggestion, "retries_used": s.retries_used,
             "stdout_tail": s.stdout_tail, "stderr_tail": s.stderr_tail,
             "actual_output_path": getattr(s, 'actual_output_path', '') or '',
             "token_usage": getattr(s, 'token_usage', {}) or {},
             "cost": _cost_of(getattr(s, 'token_usage', {}) or {}),
             "tool_calls": getattr(s, 'tool_calls', []) or [],
             "started_at": getattr(s, 'started_at', '') or '',
             "ended_at": getattr(s, 'ended_at', '') or ''}
            for s in r.step_results
        ],
        "config_dict": r.config_dict,
        "log_path": r.log_path,
        "pending_recipes": getattr(r, 'pending_recipes', []) or [],
        "awaiting_type": getattr(r, 'awaiting_type', '') or '',
        "awaiting_message": getattr(r, 'awaiting_message', '') or '',
        "awaiting_suggestion": getattr(r, 'awaiting_suggestion', '') or '',
        "input_params": getattr(r, 'input_params', None) or {},
        "workflow_id": getattr(r, 'workflow_id', None),
        "self_heal_count": getattr(r, 'self_heal_count', 0),
    }


# ── Outlook 連線測試 ──────────────────────────────────────────────────────────


@app.get("/outlook/test-connection")
async def test_outlook_connection():
    """測試 Classic Outlook COM 是否可用 + 預設資料夾的信件數。

    回傳：
        ok               是否能連 COM
        version          Outlook.Application.Version（成功才有）
        inbox_count      收件匣 Items.Count（0 通常代表 profile 沒設好或用了新版 Outlook）
        sent_count       寄件備份信件數
        drafts_count     草稿信件數
        diagnosis        中文診斷結論（給 UI 直接顯示）
        error            COM 失敗時的原始錯誤訊息（成功時為空）
    """
    import platform
    if platform.system() != "Windows":
        return {
            "ok": False,
            "diagnosis": "後端不在 Windows 上 — Outlook COM 只能在 Windows host 跑",
            "error": "non-Windows platform",
        }

    try:
        import win32com.client  # type: ignore[import-not-found]
        import pythoncom  # type: ignore[import-not-found]
    except Exception as e:
        return {
            "ok": False,
            "diagnosis": "pywin32 未安裝 — 後端需要 pywin32 才能呼叫 Outlook COM",
            "error": f"{e.__class__.__name__}: {e}",
        }

    try:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("Outlook.Application")
        version = str(app.Version)
        ns = app.GetNamespace("MAPI")
        inbox_count = int(ns.GetDefaultFolder(6).Items.Count)
        sent_count = int(ns.GetDefaultFolder(5).Items.Count)
        drafts_count = int(ns.GetDefaultFolder(16).Items.Count)
    except Exception as e:
        return {
            "ok": False,
            "diagnosis": ("無法連線到 Classic Outlook COM。最常見原因：你日常用的是「新版 Outlook for Windows」"
                          "（不支援 COM）。切換方式：在新版 Outlook 點上方「說明」分頁，"
                          "右邊最後一個按鈕「前往傳統 Outlook」即可切回。"),
            "error": f"{e.__class__.__name__}: {e}",
        }

    # 連得上但 inbox 是 0 → 大機率是 profile 設定錯（用新版 Outlook 但 Classic 帳號沒設）
    if inbox_count == 0:
        diagnosis = (f"COM 連線成功（Outlook {version}），但收件匣是 0 封。"
                     f"通常代表：你用的是新版 Outlook 而 Classic Outlook 的 profile 是空的。"
                     f"最快解法：在新版 Outlook 點「說明」分頁 → 右邊最後一個按鈕「前往傳統 Outlook」"
                     f"切回傳統版（保留同帳號），再回來重測。")
    else:
        diagnosis = (f"✓ Classic Outlook 連線正常（版本 {version}）。"
                     f"收件匣有 {inbox_count} 封信、寄件備份 {sent_count} 封、草稿 {drafts_count} 封。"
                     f"Outlook 自動化節點可以正常使用。")

    return {
        "ok": True,
        "version": version,
        "inbox_count": inbox_count,
        "sent_count": sent_count,
        "drafts_count": drafts_count,
        "diagnosis": diagnosis,
        "error": "",
    }
