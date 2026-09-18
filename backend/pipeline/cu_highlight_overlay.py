"""透明 topmost 紅框 overlay — 給 UIA inspector hover element 時、
螢幕對應位置畫紅框、讓使用者看清楚 tree 節點對應實際 UI 哪個 control。

實作:tkinter 在 daemon thread 跑 mainloop、HTTP endpoint 透過 queue 送指令。
- show(x, y, w, h, ttl_ms):移動 + 顯示紅框、ttl_ms 後自動消失
- clear():立即隱藏

Windows 細節:
- WS_EX_LAYERED + WS_EX_TRANSPARENT 讓 click 穿透(畫上的紅框不擋滑鼠)
- overrideredirect(True) 拿掉視窗 chrome、純框
- alpha=0.0 fully transparent body、邊框畫 4px 紅
"""
from __future__ import annotations
import logging
import queue
import threading
import time
from typing import Optional

_log = logging.getLogger(__name__)

_BORDER_PX = 4
_BORDER_COLOR = "#ff2d2d"
_DEFAULT_TTL_MS = 1500


class _HighlightOverlay:
    """單例:跑 tkinter mainloop 在背景 thread、外部丟 command 進 queue 控制。"""

    def __init__(self):
        self._cmds: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._started = False

    def _ensure_thread(self):
        with self._lock:
            if self._started:
                return
            t = threading.Thread(target=self._run, daemon=True, name="cu-highlight-overlay")
            t.start()
            self._thread = t
            self._started = True

    def show(self, x: int, y: int, w: int, h: int, ttl_ms: int = _DEFAULT_TTL_MS):
        self._ensure_thread()
        # 守底線:太小的 rect 自動加大、確保看得見
        if w < 10:
            w = 10
        if h < 10:
            h = 10
        self._cmds.put(("show", int(x), int(y), int(w), int(h), int(ttl_ms)))

    def clear(self):
        if not self._started:
            return
        self._cmds.put(("hide",))

    def _run(self):
        try:
            import tkinter as tk
        except Exception as e:
            _log.warning(f"highlight overlay tkinter import 失敗、disabled: {e}")
            return

        try:
            root = tk.Tk()
        except Exception as e:
            _log.warning(f"highlight overlay Tk 初始化失敗(可能在 headless / 服務模式): {e}")
            return

        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        # 全黑底 + alpha=0 → 完全透明、再用邊框畫紅
        # 但 alpha 太低 border 也會透、所以用 transparentcolor 把中間部分鏤空
        root.config(bg="black", highlightthickness=_BORDER_PX,
                    highlightbackground=_BORDER_COLOR, highlightcolor=_BORDER_COLOR)
        # 中間填一個跟主題色不同的顏色、整塊作 transparentcolor → 中間透
        # Win32 Layered Window 的 transparentcolor 要 chroma 比較精確、用一個少見色
        TRANSP = "magenta"
        try:
            root.attributes("-transparentcolor", TRANSP)
        except Exception:
            pass
        canvas = tk.Canvas(root, bg=TRANSP, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        # 點擊穿透:WS_EX_LAYERED | WS_EX_TRANSPARENT
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x80000
            WS_EX_TRANSPARENT = 0x20
            ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED | WS_EX_TRANSPARENT
            )
        except Exception as e:
            _log.debug(f"highlight overlay click-through 設定失敗(忽略): {e}")

        state = {"hide_at": 0.0, "shown": False}

        def poll():
            try:
                while True:
                    cmd = self._cmds.get_nowait()
                    if cmd[0] == "show":
                        _, x, y, w, h, ttl = cmd
                        root.geometry(f"{w}x{h}+{x}+{y}")
                        if not state["shown"]:
                            root.deiconify()
                            state["shown"] = True
                        state["hide_at"] = time.time() + ttl / 1000.0
                    elif cmd[0] == "hide":
                        if state["shown"]:
                            root.withdraw()
                            state["shown"] = False
                        state["hide_at"] = 0.0
            except queue.Empty:
                pass

            # auto-hide after ttl
            if state["hide_at"] and time.time() > state["hide_at"]:
                if state["shown"]:
                    root.withdraw()
                    state["shown"] = False
                state["hide_at"] = 0.0

            try:
                root.after(50, poll)
            except Exception:
                pass

        try:
            root.after(50, poll)
            root.mainloop()
        except Exception as e:
            _log.warning(f"highlight overlay mainloop 失敗: {e}")


_singleton: Optional[_HighlightOverlay] = None
_singleton_lock = threading.Lock()


def _get():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = _HighlightOverlay()
        return _singleton


def highlight(x: int, y: int, w: int, h: int, ttl_ms: int = _DEFAULT_TTL_MS):
    """畫紅框、ttl_ms 後自動消失。"""
    _get().show(x, y, w, h, ttl_ms)


def clear_highlight():
    """立即清除紅框。"""
    _get().clear()
