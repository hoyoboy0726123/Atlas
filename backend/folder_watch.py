"""檔案夾監看觸發器 — 掃描邏輯(純函式,不觸發 run)。

輪詢式(不依賴 watchdog):每次掃描回傳「mtime 比上次新」的檔案。
用 mtime cutoff 而非記住每個檔名,狀態極小(只存一個 last_seen_mtime),
且重啟不會重跑舊檔(建立監看時 last_seen_mtime 就設為當下)。
"""
from __future__ import annotations

import glob
import os
from typing import Tuple


def scan_new_files(folder_path: str, pattern: str, last_seen_mtime: float) -> Tuple[list[str], float]:
    """掃 folder_path 下符合 pattern、且 mtime > last_seen_mtime 的檔案。

    回傳 (依 mtime 由舊到新排序的新檔清單, 這批的最大 mtime)。
    資料夾不存在 / 無新檔 → ([], last_seen_mtime)。
    只回「檔案」(跳過子目錄);pattern 為 glob(如 *.csv、invoice_*.pdf)。
    """
    if not folder_path or not os.path.isdir(folder_path):
        return [], last_seen_mtime
    try:
        matches = glob.glob(os.path.join(folder_path, pattern or "*"))
    except Exception:
        return [], last_seen_mtime
    found: list[tuple[float, str]] = []
    max_mtime = last_seen_mtime
    for p in matches:
        try:
            if not os.path.isfile(p):
                continue
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > last_seen_mtime:
            found.append((m, p))
            if m > max_mtime:
                max_mtime = m
    found.sort(key=lambda t: t[0])   # 由舊到新,觸發順序符合直覺
    return [p for _, p in found], max_mtime
