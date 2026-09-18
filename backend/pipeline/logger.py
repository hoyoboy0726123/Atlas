"""
Pipeline 專用 file logger。

每次 run 建立獨立的 .log 檔，記錄完整 subprocess 輸出與驗證結果。
Telegram 只推送摘要，詳細過程全在 log 檔。
"""
import logging
from datetime import datetime
from pathlib import Path

from config import OUTPUT_BASE_PATH

LOG_DIR = OUTPUT_BASE_PATH / "pipeline_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def resolve_log_dirs() -> list[Path]:
    """回傳要搜尋 run log 的目錄候選(只回存在的、去重)。

    讀 log 的各處(AI 助手 get_run_log、TG /log、TG 最近 run 摘要)必須用這個、
    不可自己寫死路徑 —— 之前多處寫死 `backend/ai_output/pipeline_logs`,但 logger
    實際寫到 OUTPUT_BASE_PATH/pipeline_logs(搬遷後 = 專案根/ai_output),導致新 run
    一律找不到、甚至「log 目錄不存在」。

    優先序:
    1. LOG_DIR(= OUTPUT_BASE_PATH/pipeline_logs,logger 真正寫入處)
    2. backend/ai_output/pipeline_logs(搬遷前舊 run 的 fallback)
    """
    cands = [
        LOG_DIR,
        Path(__file__).resolve().parent.parent / "ai_output" / "pipeline_logs",  # backend/ai_output(舊)
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for d in cands:
        try:
            key = str(d.resolve())
        except Exception:
            key = str(d)
        if key not in seen and d.exists():
            seen.add(key)
            out.append(d)
    return out


def find_run_log(run_id: str):
    """依 run_id(完整或前 8 字前綴)跨所有 log 目錄找最新符合的 log 檔。
    回 Path 或 None(目錄不存在/找不到都回 None,由 caller 區分訊息)。"""
    dirs = resolve_log_dirs()
    if not dirs or not run_id or not run_id.strip():
        return None
    rid_short = run_id.strip().split("-")[0][:8]
    matches = []
    for d in dirs:
        matches.extend(d.glob(f"*{rid_short}*.log"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def create_run_logger(run_id: str, pipeline_name: str) -> tuple[logging.Logger, str]:
    """
    建立此 run 的 file logger。

    Returns:
        (logger, log_path_str)
    """
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in pipeline_name)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{ts}_{safe_name}_{run_id[:8]}.log"

    logger = logging.getLogger(f"pipeline.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # 徹底清除舊的 handlers，避免在 Windows spawn 模式或恢復執行時重複輸出
    if logger.handlers:
        for h in logger.handlers[:]:
            logger.removeHandler(h)

    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    return logger, str(log_path)


def get_run_logger(run_id: str) -> logging.Logger:
    """回傳已存在的 logger（不保證有 handler，恢復 run 時用）"""
    return logging.getLogger(f"pipeline.{run_id}")


def resume_run_logger(run_id: str, log_path: str) -> logging.Logger:
    """
    恢復執行時，附加到現有的 log 檔案（不建立新檔）。
    用於 resume_pipeline 與 run_pipeline 恢復路徑，確保前端讀到的
    log_path 始終是同一個檔案。
    """
    logger = logging.getLogger(f"pipeline.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        for h in logger.handlers[:]:
            logger.removeHandler(h)
    fh = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger
