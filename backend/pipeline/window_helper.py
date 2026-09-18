"""Atlas 前景視窗縮小/還原 — 給 computer_use workflow 啟動時用。

設定 `auto_minimize_for_computer_use` 開啟時:
- workflow 開跑前、把當下前景視窗(通常是 Atlas frontend 瀏覽器)縮到最小
- workflow 結束(成功 / 失敗 / 暫停)後、把那個視窗還原回前景

採 reference counting:支援並發 workflow。第一個觸發時 minimize、
最後一個結束時才 restore;中間 workflow 進出不會反覆 minimize/restore。

純 ctypes 呼叫 user32.dll、不加任何套件依賴。
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

log = logging.getLogger(__name__)


# 只在 Windows 平台上載入 user32,其他平台所有 helper 都會 no-op
_USER32 = None
_AVAILABLE = False
if sys.platform == "win32":
    try:
        import ctypes
        _USER32 = ctypes.windll.user32
        _AVAILABLE = True
    except Exception as e:
        log.warning(f"[window_helper] 載入 ctypes user32 失敗:{e}")

# Win32 ShowWindow 常數
_SW_MINIMIZE = 6
_SW_RESTORE = 9

# Reference counting 狀態 + lock
_lock = threading.Lock()
_ref_count: int = 0
_saved_hwnd: Optional[int] = None


def request_minimize() -> bool:
    """請求縮小前景視窗。第一個呼叫者實際縮、後續呼叫者只 +1 ref count。

    回傳:這次有沒有真的 minimize(False = 已經有別人縮過、或不可用)。
    """
    global _ref_count, _saved_hwnd
    if not _AVAILABLE:
        return False
    with _lock:
        _ref_count += 1
        if _ref_count > 1:
            # 已經有別的 workflow 縮過、不重複動作
            return False
        try:
            hwnd = _USER32.GetForegroundWindow()
            if not hwnd:
                log.info("[window_helper] GetForegroundWindow 回 0、跳過 minimize")
                # ref_count 已經 +1、撤回
                _ref_count -= 1
                return False
            _USER32.ShowWindow(hwnd, _SW_MINIMIZE)
            _saved_hwnd = hwnd
            log.info(f"[window_helper] 縮小前景視窗 hwnd={hwnd}、ref_count={_ref_count}")
            return True
        except Exception as e:
            log.warning(f"[window_helper] minimize 失敗:{type(e).__name__}: {e}")
            _ref_count -= 1
            return False


def request_restore() -> bool:
    """釋放一個 minimize 請求。最後一個呼叫者實際還原。

    回傳:這次有沒有真的 restore(False = 還有別人在用、不該還原)。
    """
    global _ref_count, _saved_hwnd
    if not _AVAILABLE:
        return False
    with _lock:
        if _ref_count <= 0:
            # 防呆:沒人 request_minimize 過、不該被 request_restore
            log.debug("[window_helper] request_restore 但 ref_count 已 = 0、忽略")
            return False
        _ref_count -= 1
        if _ref_count > 0:
            # 還有別的 workflow 在用、不要還原
            return False
        # 最後一個了、實際還原
        hwnd = _saved_hwnd
        _saved_hwnd = None
        if not hwnd:
            return False
        try:
            _USER32.ShowWindow(hwnd, _SW_RESTORE)
            _USER32.SetForegroundWindow(hwnd)
            log.info(f"[window_helper] 還原前景視窗 hwnd={hwnd}")
            return True
        except Exception as e:
            log.warning(f"[window_helper] restore 失敗:{type(e).__name__}: {e}")
            return False


def config_has_computer_use(config_dict: dict) -> bool:
    """快速檢查 config 內任一 step 是不是 computer_use 節點。"""
    steps = (config_dict or {}).get("steps") or []
    for s in steps:
        if isinstance(s, dict) and s.get("computer_use") is True:
            return True
    return False
