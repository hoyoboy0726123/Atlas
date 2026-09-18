import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL_MAIN   = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_MAIN = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEZONE           = os.getenv("TIMEZONE", "Asia/Taipei")
# OUTPUT_BASE_PATH 解析規則:
#   1. .env 顯式設絕對路徑 → 用該路徑
#   2. .env 設相對路徑 → 視為相對 repo_root(不是 backend cwd!)、解成 repo_root/<rel>
#   3. .env 沒設 → 預設 repo_root/ai_output(對齊 pipeline/runner.py 內 _workflow_output_dir
#      的 hardcoded 計算結果、避免 workflow runner 跟 chat_tools 兩條路徑解到不同地方)
# 強制 absolute 確保之後傳到 sandbox docker exec -w 不會炸「Cwd must be an absolute path」。
_REPO_ROOT = Path(__file__).parent.parent.resolve()   # backend/config.py → backend/ → repo_root/
_OUTPUT_ENV = os.getenv("OUTPUT_BASE_PATH", "").strip()
if _OUTPUT_ENV:
    _p = Path(_OUTPUT_ENV).expanduser()
    OUTPUT_BASE_PATH = _p if _p.is_absolute() else (_REPO_ROOT / _p).resolve()
else:
    OUTPUT_BASE_PATH = (_REPO_ROOT / "ai_output").resolve()
SCHEDULER_DB_PATH  = OUTPUT_BASE_PATH / "pipeline_scheduler.db"
PIPELINE_DIR       = Path(os.getenv("PIPELINE_DIR", "~/pipelines")).expanduser()

OUTPUT_BASE_PATH.mkdir(parents=True, exist_ok=True)
PIPELINE_DIR.mkdir(parents=True, exist_ok=True)

# external_projects:使用者放自己的 Python 專案 / 腳本的標準位置(AI 助手會引導放這、再給路徑)。
# 空資料夾 git 不追蹤、全新 clone 不會有 → 啟動時確保建立 + 放一份 README 說明用途。
EXTERNAL_PROJECTS_DIR = _REPO_ROOT / "external_projects"
EXTERNAL_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
_ext_readme = EXTERNAL_PROJECTS_DIR / "README.txt"
if not _ext_readme.exists():
    try:
        _ext_readme.write_text(
            "把你自己的 Python 專案 / 腳本放在這個資料夾底下\n"
            "(例:external_projects\\my_tool\\main.py),再告訴 AI 助手腳本的路徑或檔名,\n"
            "它就能把你的腳本接進工作流執行。\n\n"
            "說明:AI 生成的程式碼在隔離沙盒容器內執行;放在這裡的專案,AI 技能才讀寫得到、能接進流程。\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def check_config() -> list[str]:
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY（AI 驗證與 YAML 助手需要）")
    return missing


def check_host_tools() -> list[str]:
    """檢查 host 端必要系統工具(非 pip 套件、非 sandbox 可達)是否裝。

    回傳警告字串 list、空 list = 全裝齊。non-fatal、只 log 給使用者參考。
    """
    warnings: list[str] = []
    try:
        from pipeline.host_tools import get_host_tools
        for t in get_host_tools():
            if t.required and not t.installed:
                # 平台對應 install 指令
                import platform
                plat_key = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
                    platform.system(), "linux"
                )
                cmd = t.install_cmd.get(plat_key, "")
                warnings.append(
                    f"⚠ 缺少 {t.name}({t.why})。安裝指令:{cmd}"
                )
    except Exception as e:
        warnings.append(f"⚠ host 工具檢查失敗、略過:{e}")
    return warnings
