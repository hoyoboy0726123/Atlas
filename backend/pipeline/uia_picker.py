"""UIA Live Picker — 滑鼠 hover 到的 UI 元素自動抓 + 紅框跟隨 + F8 確認。

設計參照 Microsoft Inspect.exe 的 Live Inspect 模式。

實作細節:
- 用 win32 GetCursorPos 讀滑鼠、GetAsyncKeyState 直接 poll F8/F9 狀態
  (一開始用 pynput.keyboard.Listener、實測 hook 起不來 / 跟其他 listener 衝突
  導致 F8/F9 沒反應、改 GetAsyncKeyState 在同一 loop 內 poll 最穩)
- 紅框靠 cu_highlight_overlay、每輪都 refresh TTL 避免靜止時消失
- 同 element handle 跳過 UIA 重 query 但仍 refresh 紅框、效能 + 視覺都顧到
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Optional

_log = logging.getLogger(__name__)

VK_F8 = 0x77
VK_F9 = 0x78


class _UiaPicker:
    """單例:只允許同時一個 picker session。"""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_element: Optional[dict] = None
        self._last_rect: Optional[tuple[int, int, int, int]] = None  # cache 給靜止時 refresh 紅框
        self._confirmed: Optional[dict] = None
        self._error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_element = None
            self._last_rect = None
            self._confirmed = None
            self._error = None
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="uia-picker")
            self._thread.start()
            return True

    def stop(self):
        was_running = self._running
        self._running = False
        try:
            from .cu_highlight_overlay import clear_highlight
            clear_highlight()
        except Exception:
            pass
        return was_running

    def confirm_current(self) -> Optional[dict]:
        """frontend 按「確認」按鈕呼這個、把當下 hover 元素鎖進 confirmed、停 picker。
        F8 hotkey 走另一條 GetAsyncKeyState 路徑、兩個都可用。"""
        with self._lock:
            if self._last_element:
                self._confirmed = dict(self._last_element)
                _log.info(f"[uia-picker] frontend 按鈕確認 {self._confirmed.get('type')}:{self._confirmed.get('name', '')[:60]}")
                self._running = False
                return dict(self._confirmed)
            return None

    def poll(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "hovered": dict(self._last_element) if self._last_element else None,
                "confirmed": dict(self._confirmed) if self._confirmed else None,
                "error": self._error,
            }

    def consume_confirmed(self) -> Optional[dict]:
        with self._lock:
            c = self._confirmed
            self._confirmed = None
            return c

    def _run_loop(self):
        """背景 loop:輪詢滑鼠 + key state、UIA query、紅框 refresh。"""
        try:
            import uiautomation as auto
            from ctypes import windll, Structure, c_long, c_short, byref

            class POINT(Structure):
                _fields_ = [("x", c_long), ("y", c_long)]

            # 明確設 GetAsyncKeyState 回傳型別為 c_short(原始 SHORT)、避免 ctypes 預設 c_int
            # 在某些 Win64 環境下解 SHORT 出怪值
            get_key = windll.user32.GetAsyncKeyState
            get_key.restype = c_short
            get_key.argtypes = [c_long]

            from .cu_highlight_overlay import highlight as draw_highlight, clear_highlight
        except Exception as e:
            self._error = f"picker 初始化失敗:{e}"
            self._running = False
            return

        last_pos = (-99999, -99999)
        last_handle_key = None
        consecutive_fail = 0
        # 上次 key state、用來偵測「從沒按到按下」的瞬間(避免 F8 一直按住觸發多次)
        f8_was_down = False
        f9_was_down = False
        loop_count = 0

        while self._running:
            try:
                # ── poll F8/F9 ──────────────────────────────────────
                # GetAsyncKeyState 回 c_short:bit 15 = 當前是否按下、bit 0 = 自上次 query 以來是否按過
                # 用 (raw & 0x8000) != 0 偵測現在是否按下、 raw & 1 偵測按過(更敏感、適合短按)
                f8_raw = get_key(VK_F8)
                f9_raw = get_key(VK_F9)
                f8_now = bool(f8_raw & 0x8000)
                f9_now = bool(f9_raw & 0x8000)
                # 第一次按過自上次 query 以來(catch fast tap)、避免 80ms poll 間隔錯失短按
                f8_pressed_since = bool(f8_raw & 1)
                f9_pressed_since = bool(f9_raw & 1)

                # 偵測「按下事件」:目前按著(was_down=False, now=True)、或自上次 query 起按過
                if (f8_now and not f8_was_down) or f8_pressed_since:
                    with self._lock:
                        if self._last_element:
                            self._confirmed = dict(self._last_element)
                            _log.info(f"[uia-picker] F8 確認 {self._confirmed.get('type')}:{self._confirmed.get('name', '')[:60]}")
                            self._running = False
                            break
                        else:
                            _log.info("[uia-picker] F8 按下但 _last_element=None、忽略(請先把滑鼠移到目標元素)")
                if (f9_now and not f9_was_down) or f9_pressed_since:
                    _log.info("[uia-picker] F9 取消")
                    self._running = False
                    break
                f8_was_down = f8_now
                f9_was_down = f9_now

                # 每 50 輪(~4 秒)log 一次 picker 還活著、debug 用
                loop_count += 1
                if loop_count % 50 == 0:
                    _log.debug(f"[uia-picker] alive iter={loop_count} f8_raw={f8_raw} hovered={'yes' if self._last_element else 'no'}")

                # ── poll 滑鼠位置 ───────────────────────────────────
                p = POINT()
                windll.user32.GetCursorPos(byref(p))
                pos = (p.x, p.y)

                if pos != last_pos:
                    # 滑鼠移動 → 重 query UIA element
                    try:
                        ctrl = auto.ControlFromPoint(pos[0], pos[1])
                    except Exception:
                        ctrl = None
                    if ctrl is not None:
                        try:
                            handle = getattr(ctrl, "NativeWindowHandle", 0) or 0
                            key = (handle, ctrl.ControlTypeName, ctrl.Name)
                            # 同 element 不重 update info(but rect 仍可能在外層 refresh 紅框)
                            if key != last_handle_key:
                                rect = ctrl.BoundingRectangle
                                rw = int(rect.right - rect.left)
                                rh = int(rect.bottom - rect.top)
                                el_info = {
                                    "type": str(ctrl.ControlTypeName or ""),
                                    "name": str(ctrl.Name or "")[:200],
                                    "auto_id": str(getattr(ctrl, "AutomationId", "") or ""),
                                    "rect": [int(rect.left), int(rect.top), rw, rh],
                                    "enabled": bool(getattr(ctrl, "IsEnabled", True)),
                                }
                                with self._lock:
                                    self._last_element = el_info
                                    self._last_rect = (int(rect.left), int(rect.top), rw, rh)
                                last_handle_key = key
                        except Exception:
                            pass
                    last_pos = pos

                # ── 每輪都 refresh 紅框(TTL 不會因為滑鼠靜止就消失)──────
                with self._lock:
                    rect = self._last_rect
                if rect and rect[2] > 10 and rect[3] > 10:
                    try:
                        draw_highlight(rect[0], rect[1], rect[2], rect[3], ttl_ms=600)
                    except Exception:
                        pass

                consecutive_fail = 0
                time.sleep(0.08)
            except Exception as e:
                consecutive_fail += 1
                if consecutive_fail > 5:
                    self._error = f"picker loop 異常 5 次:{e}"
                    self._running = False
                    return
                time.sleep(0.2)

        # 結束:清紅框
        try:
            clear_highlight()
        except Exception:
            pass


_picker_singleton: Optional[_UiaPicker] = None
_picker_lock = threading.Lock()


def get_picker() -> _UiaPicker:
    global _picker_singleton
    with _picker_lock:
        if _picker_singleton is None:
            _picker_singleton = _UiaPicker()
        return _picker_singleton
