"""
桌面自動化引擎（computer_use 節點專用）。

核心能力：
- L1 basic template matching（cv2.matchTemplate + TM_CCOEFF_NORMED）
- L2 multi-scale matching（對 template 做 ±15% 縮放，解決 DPI/視窗大小差異）
- 動作執行：click_image / click_at / type_text / hotkey / wait / wait_image / screenshot
- Emergency abort：pyautogui.FAILSAFE（滑鼠移到左上角 0,0 立即觸發）+ run_id 中止訊號

不與 skill / recipe 系統共用 — 純 pyautogui + opencv 執行，無 LLM 參與。
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


# ── Emergency abort signal（執行中可從外部 set，立即中斷）────────
_abort_flags: dict[str, bool] = {}

# vlm_mode='grounding' 的錨點局部驗證。
# 模型不會承認找不到目標（實測問它畫面上沒有的元素，它會指一個「最像的」位置，
# 提示詞只擋得住最離譜的那種），所以拿錄製時的錨點圖跟它指的位置比對來擋幻覺。
#
# 這兩個數字是量出來的，不是猜的（2026-08-03，10 個案例）：
#   固定位置比對 → 正確 0.17~1.00 / 幻覺 -0.00~0.07，兩群只差一點點。
#     原因是誤差 3px 相似度就從 1.00 掉到 0.31 —— 指標本身太脆。
#   改成 ±MARGIN 開窗搜尋後才拉得開（容許幾像素位移）。
# 門檻取 0.30：低於量到的正確下界、遠高於幻覺上界。
# 實測重新校準：0.30 太鬆。
# 舊註解引用的「0.171~0.308」其實是**位置錯誤**時的分數，不是正確結果的下限；
# 正確定位在 ±40px 開窗搜尋下拿到的是 0.9~1.0，幻覺是 0.02~0.45。
# 0.55 擋得住實測到的 0.447 幻覺，也不會誤殺正確結果。
VLM_GROUNDING_VERIFY_MIN = 0.55
VLM_GROUNDING_VERIFY_MARGIN = 40   # 局部搜尋窗半徑（px）

# 🪄 產生描述後的自我驗證門檻：拿產生的描述回去定位，離錄製點擊位置多遠算失敗。
# 40px 是量出來的 —— 描述正確時誤差都在 2~11px，講錯時是 70~259px，中間空得很開。
GROUNDING_DESC_VERIFY_PX = 40


# ── 模板圖 LRU 快取 ───────────────────────────────────────────────
# 對同一張錨點圖反覆 read_bytes + imdecode + cvtColor + Canny 是浪費；
# 典型一個 step 會對同一圖做 2~14 次（multi-scale × edge fallback × retry）。
# 以 (abs_path, mtime) 當 key，mtime 變動（使用者重錄）會自動失效。
# 記憶體成本：每個 ~5-50KB，上限 64 張 → < 4MB
_TPL_CACHE_MAX = 64
_tpl_cache: "OrderedDict[tuple[str, float], tuple[np.ndarray, np.ndarray]]" = OrderedDict()


# 錨點「有沒有特徵」的下限（灰階變異數）。
# 2026-08-06 實測：TM_CCOEFF_NORMED 對零變異模板是數學退化的（除以 0），
# 純色錨點跟**任何東西**比都拿 1.000 —— 連純雜訊都 1.000。
# 也就是純色錨點會讓 CV 隨便命中一塊平坦區域、讓幻覺守門形同虛設。
# 判別力實測：變異數 20 以下完全分不出來，100 以上正常（0.000~0.062）；
# 實際錄製的錨點是 888~5295，門檻取 100 有將近 9 倍餘裕。
ANCHOR_MIN_VARIANCE = 100.0


def _anchor_variance(img) -> float:
    """錨點的灰階變異數。太低 = 這張圖沒有特徵，比對結果不可信。"""
    import cv2
    import numpy as _np
    if img is None:
        return 0.0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(_np.var(g))


class _SkipVerify(Exception):
    """錨點沒有判別力 → 這次驗證跳過（已 warning）。"""


def _best_match_score(win, tpl) -> tuple[float, str]:
    """錨點局部驗證用：彩色 / 灰階 / 邊緣三種比對取最高分。回 (分數, 用了哪種)。

    為什麼不只用彩色（2026-08-03 實測）：
      錄製時滑鼠正停在按鈕上，錨點拍到的是 **hover 高亮**狀態；
      回放驗證時滑鼠還沒移過去，畫面是原始狀態。
      實測檔案總管的關閉鈕：錨點 BGR(40,54,199) 鮮紅、現況 BGR(224,203,209) 淺白，
      顏色完全相反 → 彩色比對只剩 0.389，門檻 0.3 差一點就誤殺。
      同一組圖：灰階 0.505、邊緣 0.777（形狀不受 hover 影響）。
      而幻覺那側三種模式幾乎一樣（0.219 / 0.219 / 0.234），
      所以取最高分只救正確案例、不會讓幻覺變好混。

    這個問題對**每個 click_image 都存在** —— 錄製時滑鼠必然停在目標上。
    多數按鈕 hover 效果較弱（淺灰）所以還沒被發現，關閉鈕最明顯。
    """
    import cv2
    if win.shape[0] < tpl.shape[0] or win.shape[1] < tpl.shape[1]:
        return (-1.0, "尺寸不足")
    best, best_mode = -1.0, ""
    wg = cv2.cvtColor(win, cv2.COLOR_BGR2GRAY)
    tg = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    for mode, a, b in (("彩色", win, tpl),
                       ("灰階", wg, tg),
                       ("邊緣", cv2.Canny(wg, 50, 150), cv2.Canny(tg, 50, 150))):
        try:
            s = float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())
        except cv2.error:
            continue
        if s > best:
            best, best_mode = s, mode
    return (best, best_mode)


def _imread_unicode(path: Path):
    """讀圖但吃得下非 ASCII 路徑。讀不到回 None。

    cv2.imread 在 Windows 無法開啟含中文的路徑（直接回 None，不報錯）。
    工作流名稱幾乎都是中文（例：ai_output\\新工作流\\桌面自動化_1_assets），
    所以整份程式碼一律走 read_bytes + imdecode —— 2026-08-03 我在新功能裡
    圖省事用了 cv2.imread，使用者一測就撞「錨點圖讀取失敗」。
    """
    import cv2
    try:
        buf = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _load_template(tpl_path: Path):
    """解碼錨點圖 → 回傳 (gray, edge) 灰階/Canny 邊緣陣列，兩者皆用於 find_template 的 mode 切換。
    命中快取直接回；未命中解碼一次存入。失敗回 (None, None, 錯誤訊息)。"""
    import cv2
    try:
        mtime = tpl_path.stat().st_mtime
    except OSError as e:
        return None, None, f"模板 stat 失敗：{e}"
    key = (str(tpl_path), mtime)
    cached = _tpl_cache.get(key)
    if cached is not None:
        _tpl_cache.move_to_end(key)
        return cached[0], cached[1], ""
    try:
        buf = np.frombuffer(tpl_path.read_bytes(), dtype=np.uint8)
        tpl_color = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        return None, None, f"模板讀取例外：{e}"
    if tpl_color is None:
        return None, None, f"模板解碼失敗（格式錯誤？）：{tpl_path}"
    tpl_gray = cv2.cvtColor(tpl_color, cv2.COLOR_BGR2GRAY)
    tpl_edge = cv2.Canny(tpl_gray, 50, 150)
    _tpl_cache[key] = (tpl_gray, tpl_edge)
    while len(_tpl_cache) > _TPL_CACHE_MAX:
        _tpl_cache.popitem(last=False)
    return tpl_gray, tpl_edge, ""


def clear_template_cache() -> None:
    """測試或使用者重錄大量錨點後手動清快取用"""
    _tpl_cache.clear()


def request_abort(run_id: str) -> None:
    """標記此 run 需立即中止;computer_use 引擎會在每個動作間檢查"""
    _abort_flags[run_id] = True


def clear_abort(run_id: str) -> None:
    _abort_flags.pop(run_id, None)


def _should_abort(run_id: Optional[str]) -> bool:
    return bool(run_id) and _abort_flags.get(run_id, False)


# ── ESC × 2 緊急停止 watcher(Windows only)────────────────────────────
# 比 pyautogui FAILSAFE (滑鼠甩到 0,0) 更直覺。
# 偵測邏輯:GetAsyncKeyState 輪詢 VK_ESCAPE,看到 rising edge(從沒按 → 按下)
# 兩次間隔 < 500ms 就觸發 abort。
#
# 設計:模組級單例 watcher、新 step 啟動時切換 run_id;不需要 try/finally 包整個
# step 函式 — 即使 step 函式有很多 return 路徑、watcher 也會 follow 最新 run_id。
_esc_watcher_state: dict = {"running": False, "run_id": "", "logger": None, "thread": None}


def _esc_watcher_loop():
    """單例 watcher loop:看 _esc_watcher_state['run_id'] 動態 follow 當前 run。"""
    import threading
    try:
        from ctypes import windll, c_short, c_long
        VK_ESCAPE = 0x1B
        get_key = windll.user32.GetAsyncKeyState
        get_key.restype = c_short
        get_key.argtypes = [c_long]
    except Exception:
        return  # 非 Windows、FAILSAFE 仍為備援

    was_down = False
    last_press_time = 0.0
    DOUBLE_PRESS_WINDOW_SEC = 0.5

    while _esc_watcher_state["running"]:
        try:
            raw = get_key(VK_ESCAPE)
            is_down = bool(raw & 0x8000)
            pressed_since = bool(raw & 1)
            if (is_down and not was_down) or pressed_since:
                now = time.time()
                if now - last_press_time < DOUBLE_PRESS_WINDOW_SEC:
                    rid = _esc_watcher_state.get("run_id") or ""
                    lg = _esc_watcher_state.get("logger")
                    if rid and lg:
                        lg.warning(f"[esc-watcher] ⚠ 偵測到 ESC × 2、觸發 abort run_id={rid}")
                        request_abort(rid)
                    # 不停 loop、繼續看下個 step 的 ESC × 2
                    last_press_time = 0.0
                else:
                    last_press_time = now
            was_down = is_down
            time.sleep(0.05)
        except Exception:
            time.sleep(0.1)


def _ensure_esc_watcher(run_id: str, logger: logging.Logger) -> None:
    """確保 watcher 已啟動;每次 step 開始時呼叫、會 update 當前 run_id"""
    import threading
    _esc_watcher_state["run_id"] = run_id
    _esc_watcher_state["logger"] = logger
    if _esc_watcher_state["running"]:
        return  # 已有 watcher 在跑、只 update run_id 即可
    _esc_watcher_state["running"] = True
    t = threading.Thread(target=_esc_watcher_loop, daemon=True, name="esc-watcher")
    _esc_watcher_state["thread"] = t
    t.start()


# ── 螢幕擷取與圖像比對 ──────────────────────────────────────────

def _capture_screen() -> tuple[np.ndarray, int, int]:
    """抓所有螢幕聯集的完整截圖，回傳 (BGR ndarray, 原點 x, 原點 y)。

    關鍵：用 monitors[0]（虛擬桌面聯集）而非 monitors[1]（主螢幕），
    讓 cv2 template matching 能在多螢幕環境下找到任意螢幕上的目標；
    多螢幕時主螢幕左上不一定是 (0,0)，回傳的 origin 用來把比對到的
    相對座標轉回絕對桌面座標（pyautogui.click 接受的就是絕對座標）。
    """
    import mss
    import cv2
    with mss.mss() as sct:
        mon = sct.monitors[0]      # 所有螢幕聯集
        img = np.array(sct.grab(mon))
    bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return bgr, mon["left"], mon["top"]


def _point_in_any_screen(x: int, y: int) -> tuple[bool, str]:
    """檢查 (x, y) 是否落在目前任一螢幕可見範圍內（支援多螢幕負座標）。
    用途：scroll / click 前避免把滑鼠拉到超出桌面範圍的座標。
    回傳 (是否在範圍內, 目前螢幕配置描述)。"""
    import mss
    try:
        with mss.mss() as sct:
            for mon in sct.monitors[1:]:
                left = mon["left"]
                top = mon["top"]
                if left <= x < left + mon["width"] and top <= y < top + mon["height"]:
                    return True, ""
            layout = "; ".join(
                f"{m['width']}×{m['height']} @ ({m['left']},{m['top']})"
                for m in sct.monitors[1:]
            )
            return False, f"目前螢幕：{layout}"
    except Exception:
        return True, ""  # 抓不到資訊就寬容處理


@dataclass
class MatchResult:
    found: bool
    center: tuple[int, int] = (0, 0)   # (x, y) 螢幕座標
    confidence: float = 0.0
    scale: float = 1.0                  # 命中的縮放比例
    reason: str = ""
    mode: str = "gray"                  # "gray" = 灰階匹配，"edge" = Canny 邊緣匹配


def find_template(
    template_path: str,
    threshold: float = 0.5,
    multi_scale: bool = True,
    near_xy: Optional[tuple[int, int]] = None,
    search_radius: int = 400,
    region: Optional[tuple[int, int, int, int]] = None,
    mode: str = "gray",
) -> MatchResult:
    """在當前螢幕找指定模板圖，回傳中心座標與相似度。

    L1: 單一尺度 matchTemplate（快，~5ms）
    L2: multi_scale=True 時額外跑 0.85/0.9/0.95/1.05/1.1/1.15 倍縮放，
        取最高相似度（~30ms，吸收 DPI 125%/150% 縮放差異）

    mode:
      - "gray"（預設）：灰階像素比對
      - "edge"：Canny 邊緣偵測後再比對 — 兩張圖都先跑 Canny 只留輪廓，
               對色彩/光線/hover 動畫等差異更容忍（conf 通常略低但更穩）

    搜尋範圍優先序（三選一）：
      region 給定 > near_xy 給定 > 全螢幕
      - region: (left, top, width, height) 虛擬桌面絕對座標，使用者明確指定的紅框
      - near_xy: 錄製座標附近 ±search_radius px 的方形範圍（自動退回舊行為）
      - 皆未給：整個虛擬桌面都找（速度最慢、誤匹配風險最高）
    """
    import cv2

    tpl_path = Path(template_path)
    if not tpl_path.is_file():
        return MatchResult(False, reason=f"模板不存在：{template_path}")

    # 從 LRU 快取拿灰階 + Canny 邊緣，避免每次呼叫都重做 decode+cvtColor+Canny
    tpl_gray, tpl_edge, err = _load_template(tpl_path)
    if err:
        return MatchResult(False, reason=err)

    screen_color, origin_x, origin_y = _capture_screen()
    screen_gray_full = cv2.cvtColor(screen_color, cv2.COLOR_BGR2GRAY)

    # Edge 模式：template 在 _load_template 已預算好 Canny；螢幕每次都要重算（畫面會變）。
    # 閾值 50/150 是常用的 hysteresis 組合，對 UI 元素邊緣偵測穩定
    if mode == "edge":
        tpl_proc_full = tpl_edge
        screen_proc_full = cv2.Canny(screen_gray_full, 50, 150)
    else:
        tpl_proc_full = tpl_gray
        screen_proc_full = screen_gray_full

    # 三選一裁切策略：region > near_xy > 全螢幕
    clip_offset_x, clip_offset_y = origin_x, origin_y
    if region is not None:
        # 使用者明確指定的搜尋矩形（絕對桌面座標）
        rl, rt, rw, rh = region
        rel_x = rl - origin_x
        rel_y = rt - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x)
        top = max(0, rel_y)
        right = min(W, rel_x + rw)
        bottom = min(H, rel_y + rh)
        if right - left < 20 or bottom - top < 20:
            return MatchResult(False, reason=f"search_region ({rl},{rt},{rw},{rh}) 與目前桌面範圍重疊不足")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    elif near_xy is not None:
        nx, ny = near_xy
        # 絕對座標 → 相對截圖的座標
        rel_x = nx - origin_x
        rel_y = ny - origin_y
        H, W = screen_proc_full.shape
        left = max(0, rel_x - search_radius)
        top = max(0, rel_y - search_radius)
        right = min(W, rel_x + search_radius)
        bottom = min(H, rel_y + search_radius)
        if right - left < 20 or bottom - top < 20:
            # 範圍超出螢幕太多（錄製座標根本不在目前桌面範圍內）
            return MatchResult(False, reason=f"錄製座標 ({nx},{ny}) 超出目前桌面範圍")
        screen_proc = screen_proc_full[top:bottom, left:right]
        clip_offset_x = origin_x + left
        clip_offset_y = origin_y + top
    else:
        screen_proc = screen_proc_full

    scales = [1.0]
    if multi_scale:
        # L2：涵蓋常見 DPI 差（100%/125%/150%）
        scales = [0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15]

    best = MatchResult(False, mode=mode)
    for s in scales:
        if abs(s - 1.0) < 1e-6:
            tpl_scaled = tpl_proc_full
        else:
            new_w = max(1, int(tpl_proc_full.shape[1] * s))
            new_h = max(1, int(tpl_proc_full.shape[0] * s))
            if new_w >= screen_proc.shape[1] or new_h >= screen_proc.shape[0]:
                continue
            tpl_scaled = cv2.resize(tpl_proc_full, (new_w, new_h), interpolation=cv2.INTER_AREA)
        try:
            res = cv2.matchTemplate(screen_proc, tpl_scaled, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            continue
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best.confidence:
            h, w = tpl_scaled.shape
            # 比對結果是相對於裁切區域的座標；加上裁切原點換算成桌面絕對座標
            cx = max_loc[0] + w // 2 + clip_offset_x
            cy = max_loc[1] + h // 2 + clip_offset_y
            best = MatchResult(
                found=max_val >= threshold,
                center=(cx, cy),
                confidence=float(max_val),
                scale=s,
                mode=mode,
            )
    if not best.found:
        area = "附近範圍" if near_xy else "整個桌面"
        best.reason = f"最佳相似度 {best.confidence:.3f} 低於門檻 {threshold}（搜尋{area}，{mode} 模式）"
    return best


# ── 動作執行 ────────────────────────────────────────────────────

@dataclass
class ActionResult:
    ok: bool
    action_index: int
    action_type: str
    message: str = ""
    duration_ms: int = 0
    # (變數名, 值) —— ocr_get_text 等「取值」動作用。對齊 uia_executor 的 save_as 機制,
    # 讓 pixel 路徑也能把讀到的東西存進 step_variables 給後續 {{變數}} 用。
    saved_var: "Optional[tuple[str, Any]]" = None


def _check_abort(run_id: Optional[str]) -> None:
    if _should_abort(run_id):
        raise RuntimeError("使用者中止（emergency abort）")


def _parse_search_region(action: dict) -> Optional[tuple[int, int, int, int]]:
    """解析 action['search_region'] = [left, top, width, height]（虛擬桌面絕對座標）。
    格式不對或尺寸 <= 0 回 None（代表不限制，走 near_xy / 全螢幕邏輯）。"""
    sr = action.get("search_region") or []
    if not isinstance(sr, (list, tuple)) or len(sr) != 4:
        return None
    try:
        l, t, w, h = int(sr[0]), int(sr[1]), int(sr[2]), int(sr[3])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (l, t, w, h)


def _vlm_judge_screen(prompt: str, region: Optional[tuple[int, int, int, int]],
                      logger: logging.Logger) -> tuple[bool, str]:
    """vlm_check 用：把當下螢幕送 Settings 主模型，回 (pass, reason)。
    region 給定就先把截圖裁成該矩形再送 VLM（省 token、聚焦關鍵區域）。
    模型不支援視覺時直接回 (False, 錯誤訊息) — 不靜默 fallback。"""
    screen, ox, oy = _capture_screen()
    if region is not None:
        l, t, w, h = region
        x = max(0, l - ox)
        y = max(0, t - oy)
        x_end = min(screen.shape[1], x + w)
        y_end = min(screen.shape[0], y + h)
        if x_end <= x or y_end <= y:
            return (False, f"裁切區域 {(l, t, w, h)} 與目前螢幕無交集")
        screen = screen[y:y_end, x:x_end]

    sys_msg = ("你是 UI 視覺判斷器，看到的圖是螢幕當下狀態。嚴格依使用者描述的條件回 JSON："
               '{"pass": true/false, "reason": "簡短中文說明"}。只回 JSON，不要 markdown、不要其他文字。')
    ok, raw, err = _vlm_call_with_image(f"判斷條件：{prompt}\n請看圖回 JSON。", sys_msg, screen)
    if not ok:
        return (False, err)
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        return (False, f"LLM 回應不是 JSON：{raw[:200]}")
    passed = bool(data.get("pass", False))
    reason = str(data.get("reason") or "").strip() or "(無原因說明)"
    logger.info(f"[vlm_check] pass={passed} reason={reason[:120]}")
    return (passed, reason)


def _vlm_call_with_images(messages_text: str, sys_prompt: str,
                          images_bgr: list) -> tuple[bool, str, str]:
    """多圖版本 —— 語意與 _vlm_call_with_image 相同，只是可帶多張圖。
    （產生 grounding 描述時要同時給「全螢幕上下文」與「元件特寫」。）"""
    return _vlm_call_with_image(messages_text, sys_prompt, images_bgr)


def _vlm_call_with_image(messages_text: str, sys_prompt: str,
                         screen_bgr) -> tuple[bool, str, str]:
    """共用的 VLM 帶圖呼叫。screen_bgr 可以是單張 ndarray 或 list[ndarray]。
    回 (ok, raw_response, error_msg)。"""
    import sys as _sys
    import base64
    import cv2

    backend_dir = str(Path(__file__).resolve().parent.parent)
    if backend_dir not in _sys.path:
        _sys.path.insert(0, backend_dir)
    from llm_factory import build_llm, provider_supports_vision
    from langchain_core.messages import HumanMessage, SystemMessage

    # 這條路徑「看圖」就是它存在的目的 —— 模型收不到圖等於整個判斷失去依據。
    # 訂閱 CLI 是純文字橋、圖片會被丟掉,比照 cu_vlm_verifier 自動改用另一個 role;
    # 兩邊都不支援就明確報錯,絕不默默硬跑(那會得到「沒看圖就下的判決」)。
    _eff_role = "primary"
    if not provider_supports_vision(_eff_role):
        if provider_supports_vision("secondary"):
            # 這支沒有 logger 參數(其他 VLM 函式是由呼叫端傳入),自行取模組 logger
            logging.getLogger("pipeline.computer_use").info(
                "[computer_use] 主模型無法輸入圖片(純文字橋)→ VLM 判斷自動改用副模型")
            _eff_role = "secondary"
        else:
            return (False, "",
                    "主/副模型都無法輸入圖片(純文字橋接),VLM 視覺判斷無法執行;"
                    "請把主或副模型其中一個設為支援 vision 的 API 模型(如 Gemini)")

    _imgs = screen_bgr if isinstance(screen_bgr, list) else [screen_bgr]
    user_content: list = [{"type": "text", "text": messages_text}]
    for _im in _imgs:
        ok, buf = cv2.imencode(".jpg", _im, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return (False, "", "JPEG encode 失敗")
        b64 = base64.b64encode(buf.tobytes()).decode()
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    try:
        llm = build_llm(temperature=0, role=_eff_role)
        result = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_content)])
        # 多模態模型（Gemini）回傳 content 是 list[dict]，純文字是 str — 統一抽 text
        raw_content = getattr(result, "content", None)
        if raw_content is None:
            raw = ""
        elif isinstance(raw_content, str):
            raw = raw_content
        elif isinstance(raw_content, list):
            parts: list[str] = []
            for block in raw_content:
                if isinstance(block, dict):
                    t = block.get("text") or ""
                    if t:
                        parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            raw = "".join(parts)
        else:
            raw = str(raw_content)
        raw = raw.strip()
    except Exception as e:
        return (False, "", f"LLM 呼叫失敗（請確認 Settings 主模型支援視覺）：{e.__class__.__name__}: {e}")
    if not raw:
        return (False, "", "LLM 回應為空")
    return (True, raw, "")


def _strip_json_fence(raw: str) -> str:
    """LLM 常用 ```json...``` 包 JSON。剝掉 fence 給 json.loads。"""
    if "```" not in raw:
        return raw.strip()
    parts = raw.split("```")
    if len(parts) >= 2:
        body = parts[1].strip()
        if body.startswith("json"):
            body = body[4:].strip()
        return body
    return raw.strip()


def _vlm_describe_to_text(prompt: str, region: Optional[tuple[int, int, int, int]],
                          logger: logging.Logger) -> tuple[bool, str, str]:
    """vlm_mode='description' 用：把螢幕送 VLM，請它告訴我們目標的「實際文字」。
    座標由後續 OCR 決定（VLM 不負責定位 → 不會點錯位置）。
    回 (found, target_text, reason)。"""
    screen, ox, oy = _capture_screen()
    if region is not None:
        l, t, w, h = region
        x = max(0, l - ox)
        y = max(0, t - oy)
        x_end = min(screen.shape[1], x + w)
        y_end = min(screen.shape[0], y + h)
        if x_end <= x or y_end <= y:
            return (False, "", f"裁切區域 {(l, t, w, h)} 與螢幕無交集")
        screen = screen[y:y_end, x:x_end]

    sys_msg = ("你是 UI 視覺助手。看到的圖是螢幕當下狀態。回 JSON："
               '{"found": true/false, "text": "目標的實際文字", "reason": "簡短說明"}。'
               'text 必須是螢幕上「實際看得到」的字串（含大小寫和標點），不是描述、不是同義詞。'
               "只回 JSON。")
    user_text = (f"使用者描述要點擊的目標：「{prompt}」\n"
                 "請看圖，找出符合此描述的 UI 元素，告訴我它身上實際顯示的文字（OCR 等下會用此文字定位）。"
                 "若畫面上找不到此目標，回 found=false。")

    ok, raw, err = _vlm_call_with_image(user_text, sys_msg, screen)
    if not ok:
        return (False, "", err)
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        return (False, "", f"VLM 回應不是 JSON：{raw[:200]}")
    found = bool(data.get("found", False))
    text = str(data.get("text") or "").strip()
    reason = str(data.get("reason") or "").strip() or "(無說明)"
    if found and not text:
        return (False, "", f"VLM 說 found=true 但 text 為空：{reason}")
    logger.info(f"[vlm_describe] found={found} text={text!r} reason={reason[:120]}")
    return (found, text, reason)


def describe_for_grounding(assets_dir: Path, action: dict) -> tuple[bool, str, str]:
    """給 vlm_mode='grounding' 產生「要點什麼」的描述草稿。回 (ok, 描述, 錯誤訊息)。

    為什麼用雲端主模型而不是那顆地端定位模型：
      地端模型是專門訓練來做「描述 → 座標」的。2026-08-03 實測反向任務
      （座標 → 描述）只有 3/5 命中，而且會把隔壁按鈕的名字講出來、
      同一句重複三四次。它是定位器，不是看圖助手。
      雲端模型剛好相反（讀畫面文字準、給座標不準），兩者互補。

    素材用錄製時就存好的：full_NNN.png（全螢幕上下文）+ img_NNN.png（元件特寫）。
    刻意**不在圖上畫框** —— 實測畫紅框會讓模型把框的顏色當成元件屬性
    （把黑字的「插入」描述成「紅色粗體文字」）。
    """
    import re
    import cv2
    anchor_name = (action.get("image") or "").strip()
    full_name = (action.get("full_image") or "").strip()
    if not anchor_name:
        return (False, "", "這個步驟沒有錨點圖，無法產生描述（請手動輸入）")

    anchor_p = assets_dir / anchor_name
    if not anchor_p.exists():
        return (False, "", f"找不到錨點圖 {anchor_name}")
    anchor = _imread_unicode(anchor_p)
    if anchor is None:
        return (False, "", f"錨點圖讀取失敗 {anchor_name}")

    # 錄製用的錨點是 240x80（刻意寬、抓左右鄰居當 CV 獨特性），
    # 但拿來「指認目標」就太模糊 —— 實測主模型會挑錯裡面哪一個才是目標
    # （「插入」那張同時框到常用/插入/繪圖，它回答「常用」）。
    # → 再從錨點正中央裁一塊更緊的特寫，明確指出目標本體。
    _ah, _aw = anchor.shape[:2]
    _tw, _th = min(_aw, 110), min(_ah, 46)
    tight = anchor[(_ah - _th) // 2:(_ah + _th) // 2, (_aw - _tw) // 2:(_aw + _tw) // 2]

    # 上下文用「點擊點周圍的局部區域」而不是整張全螢幕：
    # 全螢幕資訊太多，實測模型會從別處挑一個元件來回答。
    context = None
    if full_name and (assets_dir / full_name).exists():
        _full = _imread_unicode(assets_dir / full_name)
        if _full is not None:
            _cx = int(action.get("x", 0) or 0) - int(action.get("full_left", 0) or 0)
            _cy = int(action.get("y", 0) or 0) - int(action.get("full_top", 0) or 0)
            _H, _W = _full.shape[:2]
            if 0 <= _cx < _W and 0 <= _cy < _H:
                _rw, _rh = 700, 320
                _x0 = max(0, min(_W - _rw, _cx - _rw // 2))
                _y0 = max(0, min(_H - _rh, _cy - _rh // 2))
                context = _full[_y0:_y0 + _rh, _x0:_x0 + _rw]
            else:
                context = _full

    sys_msg = (
        "你是 UI 視覺助手，任務是替自動化腳本寫「點擊目標的描述」。"
        "這段描述之後會餵給另一個模型，讓它在螢幕上找出該元件的座標。\n"
        "規則：\n"
        "1. 只回一句話，不要解釋、不要重複、不要加標點以外的符號。\n"
        "2. 講出元件身上的文字標籤，加上它所在的區域"
        "（例：工具列上的「排序」按鈕、左側導覽窗格的「下載」）。\n"
        "3. **不要用否定句**。不可以寫「不是⋯⋯的那個」——"
        "實測下游模型會忽略否定詞、直接找最典型的匹配而點錯。\n"
        "4. 畫面上若有多個相似元件，改用正面且獨特的特徵區分"
        "（例：標題為「未命名」那個分頁上的叉叉）。\n"
        "5. 用繁體中文。"
    )
    if context is not None:
        user_text = (
            "我要你描述「一個特定的 UI 元件」，這個元件同時出現在下面三張圖裡：\n"
            "  第 1 張：目標元件的緊密特寫 —— **這張圖幾乎只有目標本身，以它為準**。\n"
            "  第 2 張：目標周圍的區域（目標在正中央），用來判斷它的鄰居是誰。\n"
            "  第 3 張：更大的畫面範圍，用來判斷它屬於哪個區域"
            "（工具列 / 側邊欄 / 標題列…）。\n"
            "請以第 1 張為準說出目標是什麼，再用第 2、3 張補上它在哪。\n"
            "注意：第 2、3 張裡有很多其他元件，**不要挑錯**。")
        imgs = [tight, anchor, context]
    else:
        user_text = ("第 1 張是目標元件的緊密特寫，第 2 張是它周圍的區域"
                     "（目標在正中央）。請以第 1 張為準描述目標是什麼。")
        imgs = [tight, anchor]

    ok, raw, err = _vlm_call_with_images(user_text, sys_msg, imgs)
    if not ok:
        return (False, "", err)
    text = re.sub(r"\s+", " ", raw.strip().strip('"「」')).strip()
    # 模型偶爾會囉嗦成好幾句 —— 只留第一句
    for sep in ("。", "\n"):
        if sep in text:
            text = text.split(sep)[0].strip()
            break
    if not text:
        return (False, "", "模型回了空描述")
    return (True, text[:120], "")


def verify_grounding_desc(assets_dir: Path, action: dict, desc: str) -> tuple[bool, float, str]:
    """把描述餵回地端定位模型，看它能不能回到錄製時的點擊位置。回 (通過, 誤差px, 說明)。

    為什麼要做這件事：
      產生描述的是雲端模型，2026-08-03 實測 5 題會講錯 1 題，
      而且錯的描述讀起來一樣通順（把右上角搜尋框說成「綠色的『好』儲存格樣式按鈕」）。
      使用者看到一句漂亮的中文不會逐字比對畫面，直接採用就點錯了。
      → 產生完立刻自己驗一次：描述若定位不回原點，就當場警告。
      成本是一次地端推論（約 2 秒、不花錢），很划算。
    """
    import cv2
    full_name = (action.get("full_image") or "").strip()
    if not full_name or not (assets_dir / full_name).exists():
        return (True, -1.0, "沒有全螢幕截圖可驗證（略過）")
    full_p = assets_dir / full_name
    img = _imread_unicode(full_p)
    if img is None:
        return (True, -1.0, "全螢幕截圖讀取失敗（略過）")
    cx = int(action.get("x", 0) or 0) - int(action.get("full_left", 0) or 0)
    cy = int(action.get("y", 0) or 0) - int(action.get("full_top", 0) or 0)
    H, W = img.shape[:2]
    if not (0 <= cx < W and 0 <= cy < H):
        return (True, -1.0, "點擊座標不在截圖範圍內（略過）")

    _tmp = None
    try:
        from pipeline import vlm_grounding as _vg
        # 錄製產物在 ai_output/<中文工作流名>/... 底下，路徑含中文。
        # 容器讀得到那個掛載，但檔名編碼在 docker exec 這段容易出事 ——
        # 複製一份到共用目錄、用純 ASCII 檔名，最保險。
        _tmp = _vg.shared_dir() / f"_verify_{os.getpid()}.png"
        _ok_w, _enc = cv2.imencode(".png", img)
        if not _ok_w:
            return (True, -1.0, "驗證跳過（截圖編碼失敗）")
        _tmp.write_bytes(_enc.tobytes())
        ok, gx, gy, why = _vg.locate(desc, str(_tmp), W, H,
                                     logging.getLogger("pipeline.computer_use"))
    except Exception as e:
        return (True, -1.0, f"驗證跳過（{e.__class__.__name__}）")
    finally:
        if _tmp is not None:
            try:
                _tmp.unlink()
            except OSError:
                pass
    if not ok:
        # 一定要分清楚「模型說找不到」和「驗證根本跑不起來」。
        # 兩者都回 False 的話，沙盒沒開就會跳紅字說「這段描述會點錯」——
        # 那是冤枉描述、也會讓使用者對警告麻痺（2026-08-03 自己踩到）。
        # ⚠ 2026-08-06 修正：「模型未給座標」是 finish() 型回應 ——
        #   那代表驗證跑不起來，不是模型說「畫面上沒有」。把它當否定會誤殺
        #   完全正確的描述（實測一句對的描述就因此被判成「會點錯」）。
        _model_said_no = "模型回報找不到目標" in why
        if _model_said_no:
            return (False, -1.0, f"定位模型在畫面上找不到這段描述講的東西：{why[:60]}")
        return (True, -1.0, f"驗證未執行（{why[:60]}）")
    d = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
    if d <= GROUNDING_DESC_VERIFY_PX:
        return (True, d, f"驗證通過（定位回錄製位置，誤差 {d:.0f}px）")
    return (False, d, f"這段描述會定位到別的地方（離錄製位置 {d:.0f}px）")


def _vlm_pick_anchor(prompt: str, anchor_names: list[str], assets_dir: Path,
                     region: Optional[tuple[int, int, int, int]],
                     logger: logging.Logger) -> tuple[bool, int, str]:
    """vlm_mode='anchor_pick' 用：給 VLM 看當下螢幕 + 數張候選錨點圖，請它選最匹配的索引。
    座標由後續 CV template matching 決定（用 VLM 選出來的那張）。
    回 (ok, picked_index, reason)。picked_index 是 anchor_names 的下標（0-based）。"""
    import cv2
    if not anchor_names:
        return (False, -1, "anchor_names 為空")

    screen, ox, oy = _capture_screen()
    if region is not None:
        l, t, w, h = region
        x = max(0, l - ox)
        y = max(0, t - oy)
        x_end = min(screen.shape[1], x + w)
        y_end = min(screen.shape[0], y + h)
        if x_end <= x or y_end <= y:
            return (False, -1, f"裁切區域 {(l, t, w, h)} 與螢幕無交集")
        screen = screen[y:y_end, x:x_end]

    # 把所有錨點圖橫向拼成一張條狀圖、上方標 [0]、[1]、... 讓 VLM 看圖選號碼
    # （比一次傳多張圖更穩；許多視覺模型對單一 image_url 反應比較可靠）
    anchor_imgs = []
    for name in anchor_names:
        p = assets_dir / name
        if not p.is_file():
            return (False, -1, f"錨點圖不存在：{name}")
        data = p.read_bytes()
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return (False, -1, f"錨點圖 decode 失敗：{name}")
        anchor_imgs.append(img)

    # 全部 padded 到同高度然後加 label 條
    LABEL_H = 28
    target_h = max(im.shape[0] for im in anchor_imgs)
    strip_pieces = []
    for i, im in enumerate(anchor_imgs):
        h, w = im.shape[:2]
        if h < target_h:
            pad = np.full((target_h - h, w, 3), 255, dtype=np.uint8)
            im = np.vstack([im, pad])
        label_bar = np.full((LABEL_H, im.shape[1], 3), 30, dtype=np.uint8)
        cv2.putText(label_bar, f"[{i}]", (8, 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 2, cv2.LINE_AA)
        strip_pieces.append(np.vstack([label_bar, im]))
        # 每張之間 8 px 白色分隔
        sep = np.full((target_h + LABEL_H, 8, 3), 200, dtype=np.uint8)
        strip_pieces.append(sep)
    strip_pieces.pop()  # 去掉最後一個分隔
    anchors_strip = np.hstack(strip_pieces)

    # 螢幕 + 錨點條垂直拼接，加上「SCREEN」「ANCHORS」標頭
    def _header(text: str, width: int) -> np.ndarray:
        bar = np.full((LABEL_H, width, 3), 60, dtype=np.uint8)
        cv2.putText(bar, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (255, 255, 255), 1, cv2.LINE_AA)
        return bar

    sw = screen.shape[1]
    aw = anchors_strip.shape[1]
    target_w = max(sw, aw)
    if sw < target_w:
        pad = np.full((screen.shape[0], target_w - sw, 3), 255, dtype=np.uint8)
        screen = np.hstack([screen, pad])
    if aw < target_w:
        pad = np.full((anchors_strip.shape[0], target_w - aw, 3), 255, dtype=np.uint8)
        anchors_strip = np.hstack([anchors_strip, pad])
    composite = np.vstack([
        _header("SCREEN", target_w), screen,
        _header("ANCHORS (pick one)", target_w), anchors_strip,
    ])

    sys_msg = ("你是 UI 視覺判斷器。圖分上下兩段：上段是螢幕當下，下段是數個候選錨點（按 [0][1][2]...編號）。"
               '請選一張最像螢幕上「使用者描述目標」當下狀態的錨點。回 JSON：'
               '{"index": 整數, "reason": "簡短說明"}。'
               "若沒有任何錨點符合（全都不像螢幕當下），回 index=-1。只回 JSON。")
    user_text = (f"使用者描述目標：「{prompt}」\n"
                 f"候選錨點數：{len(anchor_names)}（編號 0 到 {len(anchor_names) - 1}）\n"
                 "請選哪一個錨點最接近螢幕當下要點擊的目標。")

    ok, raw, err = _vlm_call_with_image(user_text, sys_msg, composite)
    if not ok:
        return (False, -1, err)
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        return (False, -1, f"VLM 回應不是 JSON：{raw[:200]}")
    try:
        idx = int(data.get("index", -1))
    except (TypeError, ValueError):
        return (False, -1, f"VLM 回的 index 不是整數：{data}")
    reason = str(data.get("reason") or "").strip() or "(無說明)"
    if idx < 0:
        return (False, -1, f"VLM 沒有選到任何錨點：{reason}")
    if idx >= len(anchor_names):
        return (False, -1, f"VLM 選的 index={idx} 超出範圍（只有 {len(anchor_names)} 張）")
    logger.info(f"[vlm_pick] picked [{idx}] {anchor_names[idx]} reason={reason[:120]}")
    return (True, idx, reason)


# ── 錨點獨特性分析 ──────────────────────────────────────────────────
# 「這張錨點回放時會不會挑錯」。錄製後算一次，只提醒、不改執行行為。
ANCHOR_RIVAL_THRESHOLD = 0.5

# 替身要「搶得走」才算數：CV 取的是搜尋範圍內**分數最高**的那一個，
# 所以替身必須先贏過真目標。分數差得遠 = 只有真目標整個消失時才會被誤點，
# 那不是「錨點不夠獨特」，報出來只會讓使用者學會忽略警告。
# 2026-08-06 實測（8 個真實動作）：真目標一律 1.000，最強替身 0.593~0.699，
# 差距 0.30~0.41 —— 全部搶不走。反例：視窗標題列三顆按鈕彼此 ~1.000。
ANCHOR_RIVAL_GAP = 0.15

# 全桌面 fallback 的最低 CV 門檻。analyze 要跟 execute_action 用同一個值，
# 否則會報出「執行時根本搆不到」的假警報。
_ANCHOR_FULLSCREEN_MIN = 0.80


def analyze_anchor_uniqueness(assets_dir, action: dict,
                              threshold: float = ANCHOR_RIVAL_THRESHOLD,
                              max_peaks: int = 8,
                              cv_search_radius: int = 400,
                              cv_threshold: float = 0.5,
                              cv_search_only_near: bool = False) -> dict:
    """算這張錨點在錄製畫面上有幾個替身，**而且執行時真的搆得到、搶得走**。

    用錄製時存的 full_NNN.png（那一刻的整個畫面），不是「現在」的畫面。

    ⚠ 判定要跟 find_template 的三階段一致，否則會狂報假警報：
        Phase 1  橘框內       門檻 cv_threshold  —— 沒拉橘框就不執行
        Phase 2  座標 ±radius  門檻 cv_threshold
        Phase 3  整個桌面      門檻 max(cv_threshold, 0.80)
                              —— 勾「只搜附近」或嚴格鎖橘框就不執行
      near_xy 是**方形**範圍（x/y 各 ±radius），不是圓形；用歐氏距離判斷會
      漏掉四個角落的替身（假陰性，比假警報危險）。

    回傳 checked / rivals / nearest_rival_px / scanned / phases /
        flat / variance / target_score / best_rival_score / reason
    """
    import cv2
    from pathlib import Path as _Path

    assets_dir = _Path(assets_dir)

    def _skip(why: str) -> dict:
        return {"checked": False, "rivals": 0, "nearest_rival_px": 0, "scanned": 0,
                "phases": {"box": 0, "near": 0, "fullscreen": 0},
                "flat": False, "variance": 0.0,
                "target_score": 0.0, "best_rival_score": 0.0, "reason": why}

    img_name = (action.get("image") or "").strip()
    full_name = (action.get("full_image") or "").strip()
    if not img_name:
        return _skip("這個動作沒有錨點圖")
    if not full_name:
        return _skip("沒有錄製當下的全螢幕截圖，無法判斷")

    tpl = _imread_unicode(assets_dir / img_name)
    full = _imread_unicode(assets_dir / full_name)
    if tpl is None or full is None:
        return _skip("錨點圖或全螢幕截圖讀取失敗")

    cx = int(action.get("x", 0) or 0) - int(action.get("full_left", 0) or 0)
    cy = int(action.get("y", 0) or 0) - int(action.get("full_top", 0) or 0)
    H, W = full.shape[:2]
    th, tw = tpl.shape[:2]
    if not (0 <= cx < W and 0 <= cy < H):
        return _skip("點擊座標不在全螢幕截圖範圍內")
    if th >= H or tw >= W:
        return _skip("錨點圖比全螢幕截圖還大")

    # 純色錨點比「有替身」更嚴重：它跟任何一塊平坦區域都是滿分。
    variance = _anchor_variance(tpl)
    if variance < ANCHOR_MIN_VARIANCE:
        return {"checked": True, "rivals": 0, "nearest_rival_px": 0, "scanned": 0,
                "phases": {"box": 0, "near": 0, "fullscreen": 0},
                "flat": True, "variance": round(variance, 1),
                "target_score": 0.0, "best_rival_score": 0.0,
                "reason": (f"這張錨點幾乎沒有特徵（灰階變異數 {variance:.1f}）——"
                           f"它跟畫面上任何一塊平坦區域都會是滿分，CV 可能命中"
                           f"完全無關的位置。請重圈一個含文字或邊框的範圍。")}

    g_full = cv2.cvtColor(full, cv2.COLOR_BGR2GRAY)
    g_tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    try:
        res = cv2.matchTemplate(g_full, g_tpl, cv2.TM_CCOEFF_NORMED)
    except cv2.error as e:
        return _skip(f"比對失敗：{e}")

    peaks = []
    work = res.copy()
    for _ in range(max_peaks):
        _, val, _, loc = cv2.minMaxLoc(work)
        if val < threshold:
            break
        peaks.append((float(val), loc[0] + tw // 2, loc[1] + th // 2))
        work[max(0, loc[1] - th):loc[1] + th, max(0, loc[0] - tw):loc[0] + tw] = -1.0
    if not peaks:
        return _skip("錨點在自己的錄製截圖上都對不到，可能是錄壞了")

    peaks.sort(key=lambda p: (p[1] - cx) ** 2 + (p[2] - cy) ** 2)
    target_score = peaks[0][0]
    all_rivals = peaks[1:]
    scanned = len(all_rivals)
    rivals = [r for r in all_rivals if r[0] >= target_score - ANCHOR_RIVAL_GAP]
    weak = scanned - len(rivals)

    off_l = int(action.get("full_left", 0) or 0)
    off_t = int(action.get("full_top", 0) or 0)
    box = action.get("search_region") or []
    has_box = isinstance(box, (list, tuple)) and len(box) == 4 and box[2] > 0 and box[3] > 0
    cv_strict = bool(action.get("cv_strict_region", False))
    fullscreen_on = not cv_search_only_near and not (cv_strict and has_box)
    full_min = max(cv_threshold, _ANCHOR_FULLSCREEN_MIN)

    counts = {"box": 0, "near": 0, "fullscreen": 0}
    reachable = []
    best_rival = 0.0
    for val, px, py in rivals:
        d = int(((px - cx) ** 2 + (py - cy) ** 2) ** 0.5)
        hit = False
        if has_box and val >= cv_threshold:
            l, t, w, h = box[0], box[1], box[2], box[3]
            if l <= px + off_l <= l + w and t <= py + off_t <= t + h:
                counts["box"] += 1
                hit = True
        in_near = abs(px - cx) <= cv_search_radius and abs(py - cy) <= cv_search_radius
        if not (cv_strict and has_box) and val >= cv_threshold and in_near:
            counts["near"] += 1
            hit = True
        if fullscreen_on and val >= full_min:
            counts["fullscreen"] += 1
            hit = True
        if hit:
            reachable.append(d)
            best_rival = max(best_rival, val)

    if not reachable:
        if weak and not rivals:
            why = (f"畫面上有 {weak} 個較像的地方，但分數都低於目標 "
                   f"{ANCHOR_RIVAL_GAP:.2f} 以上（目標 {target_score:.2f}），搶不走 —— "
                   f"只有真目標整個從畫面消失時才可能被誤點")
        elif scanned:
            why = f"畫面上有 {scanned} 個相似處，但執行時都搆不到"
        else:
            why = "錨點在錄製畫面上是獨一無二的"
        return {"checked": True, "rivals": 0, "nearest_rival_px": 0, "scanned": scanned,
                "phases": counts, "flat": False, "variance": round(variance, 1),
                "target_score": round(target_score, 3), "best_rival_score": 0.0,
                "reason": why}

    nearest = min(reachable)
    where = "、".join(n for n, ok in (("橘框內", counts["box"]),
                                      ("錄製座標附近", counts["near"]),
                                      ("退回全螢幕時", counts["fullscreen"])) if ok)
    return {
        "checked": True, "rivals": len(reachable), "nearest_rival_px": nearest,
        "scanned": scanned, "phases": counts, "flat": False,
        "variance": round(variance, 1),
        "target_score": round(target_score, 3),
        "best_rival_score": round(best_rival, 3),
        "reason": (f"有 {len(reachable)} 個地方分數逼近真目標（{where}）："
                   f"目標 {target_score:.2f} vs 替身 {best_rival:.2f}，"
                   f"最近的在 {nearest}px 外"
                   + (f"；另有 {scanned - len(reachable)} 個差太多或搆不到，不列入"
                      if scanned > len(reachable) else "")),
    }


def _pyautogui_with_failsafe():
    """lazy import pyautogui 並設好 failsafe / 節流"""
    import pyautogui
    pyautogui.FAILSAFE = True  # 滑鼠甩到左上角 (0,0) 立即 FailSafeException
    pyautogui.PAUSE = 0.15     # 每個 pyautogui 呼叫後自動等 150ms，防過快
    return pyautogui


def _do_click(pg, x: int, y: int, button: str, clicks: int, hold_sec: float, modifiers: list) -> None:
    """統一的點擊執行器：處理長按 + 修飾鍵。
    modifiers: ["ctrl"], ["ctrl","shift"] 等 — 按下→click→放開。

    跨螢幕瞬移防護：pyautogui.click(x=,y=) 預設是「瞬間 moveTo + click」，
    在多螢幕大跨度移動下（如從主螢幕跳到副螢幕）Windows 事件處理會 race —
    click 事件可能被路由到 cursor 飛過的中間位置或視窗動畫剛好的位置，造成
    使用者看到的「附近又多點一下」幽靈點擊。修法：先做帶 duration 的平滑
    moveTo、短暫等游標到位、再 click（不帶 x/y，用當下位置）。"""
    # 按下修飾鍵
    for mod in (modifiers or []):
        pg.keyDown(mod)
    try:
        # 先平滑移動到目標位置；duration=0.1 對單螢幕無感，跨螢幕避開瞬移 race
        # 拋例外（座標超出螢幕等）就略過 moveTo，讓 click 自己處理
        try:
            pg.moveTo(x, y, duration=0.1)
            time.sleep(0.05)   # 讓 OS 確認游標到位
        except Exception:
            pass
        if hold_sec > 0.1:
            pg.mouseDown(button=button)
            time.sleep(hold_sec)
            pg.mouseUp(button=button)
        else:
            # 不帶 x/y → click 在當下游標位置（就是上面 moveTo 過去的點）
            pg.click(button=button, clicks=clicks)
    finally:
        # 反序放開修飾鍵，即使 click 拋例外也確保按鍵會放
        for mod in reversed(modifiers or []):
            pg.keyUp(mod)


def execute_action(
    action: dict,
    assets_dir: Path,
    index: int,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    allow_coord_fallback: bool = True,
    cv_threshold: float = 0.5,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    uia_window: str = "",
    step_variables: "Optional[dict]" = None,
    _depth: int = 0,
) -> ActionResult:
    """執行單一 action。action 是 ComputerUseAction.model_dump() 結果的 dict。
    _depth: 遞迴深度（if_image_found / retry_until 巢狀時累加），防寫爛的 YAML 無限遞迴。"""
    t0 = time.time()
    # ⚠ 不能寫 `step_variables or {}` —— 空 dict 是 falsy,會**換成另一個物件**,
    #   巢狀動作(if_image_found / retry_until)存進去的變數就傳不回父層,而且是靜默的。
    if step_variables is None:
        step_variables = {}
    atype = action.get("type", "")
    desc = action.get("description") or atype
    indent = "  " * _depth if _depth > 0 else ""
    logger.info(f"[computer_use] {indent}動作 #{index + 1} ({atype})：{desc}")

    # 遞迴深度守衛：正常使用者寫不到 10 層，超過 10 層一定是 YAML 爛掉或 copy-paste 出錯
    if _depth > 10:
        return ActionResult(False, index, atype,
            f"動作巢狀超過深度 10（if_image_found / retry_until 遞迴過深），拒絕執行")

    _check_abort(run_id)

    # Bundle all the execution kwargs so nested dispatch (if_image_found / retry_until)
    # 不用手動 re-list 每個參數
    _exec_ctx = {
        "allow_coord_fallback": allow_coord_fallback,
        "cv_threshold": cv_threshold,
        "cv_search_only_near": cv_search_only_near,
        "cv_search_radius": cv_search_radius,
        "cv_trigger_hover": cv_trigger_hover,
        "cv_hover_wait_ms": cv_hover_wait_ms,
        "cv_coord_fallback": cv_coord_fallback,
        "ocr_threshold": ocr_threshold,
        "ocr_cv_fallback": ocr_cv_fallback,
        "uia_window": uia_window,
        "step_variables": step_variables,   # 巢狀子動作也要能用 {{變數}}
    }

    try:
        # uia_* 在這裡也要路由:最外層迴圈有自己的路由,但 if_image_found /
        # if_element_found / retry_until 的巢狀子動作走的是本函式 ——
        # 沒這段,巢狀裡的 uia 子動作會掉到「不認得的 action type」。
        if atype.startswith("uia_"):
            from .uia_executor import execute_uia_action
            uia_res = execute_uia_action(action, action.get("window") or uia_window,
                                         step_variables, logger)
            return ActionResult(uia_res.ok, index, atype, uia_res.message,
                                duration_ms=int((time.time() - t0) * 1000),
                                saved_var=uia_res.saved_var)
        pg = _pyautogui_with_failsafe()

        if atype == "click_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "click_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            # 多形態錨點：同一顆按鈕的不同樣子。每張都比一次取最高分。
            _variants = [v for v in (action.get("image_variants") or [])
                         if isinstance(v, str) and v.strip()]
            tpl_candidates = [(img_name, tpl_path)]
            for _v in _variants:
                _vp = assets_dir / _v
                if _vp.is_file():
                    tpl_candidates.append((_v, _vp))
                else:
                    logger.warning(f"[computer_use]   多形態錨點 {_v} 檔案不存在，略過")
            if len(tpl_candidates) > 1:
                _names = ", ".join(n for n, _ in tpl_candidates)
                logger.info(f"[computer_use]   多形態錨點：共 {len(tpl_candidates)} 張候選（{_names}）")
            # 門檻：action-level confidence 覆蓋 step 層級 cv_threshold，皆缺就用 0.5
            threshold = float(action.get("confidence") or cv_threshold)
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            fx = action.get("x")
            fy = action.get("y")
            has_coord = isinstance(fx, (int, float)) and isinstance(fy, (int, float))

            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""

            # 四種 primary mode（user-feedback 排序，VLM 永遠最高優先因為使用者明確開了它）：
            #   vlm_mode=description → VLM 看圖回「目標的實際文字」→ OCR 找文字 → 點中心
            #   vlm_mode=anchor_pick → VLM 從多張候選錨點挑一張最像螢幕當下的 → 用該張走 CV 比對
            #   use_ocr 勾起 + 有 ocr_text → 走 OCR
            #   use_ocr 沒勾 + use_coord=true → 走絕對座標
            #   use_ocr 沒勾 + use_coord=false → 走 CV 圖像比對
            # VLM 永遠不直接給座標 — 它只負責「決定要找的東西」，避開 VLM 點錯位置的問題
            vlm_mode = (action.get("vlm_mode") or "off").lower()
            use_ocr = bool(action.get("use_ocr", False))
            ocr_text = (action.get("ocr_text") or "").strip()
            ocr_will_run = use_ocr and bool(ocr_text)

            # ── 三層 fallback toggle (UIA / CV / 強制座標) ────────────────────
            # 預設三個 toggle 全 True、順序 UIA → CV → 強制座標。
            # 使用者可在 panel 取消某層、組合自定義(細節見 ComputerUseAction schema 註解)。
            # OCR / VLM 啟用時自帶 primary 邏輯、這三層不適用、UIA-first 跳過。
            use_uia_layer = bool(action.get("use_uia", True))
            use_cv_layer = bool(action.get("use_cv", True))
            use_coord_layer = bool(action.get("use_coord", True))
            user_chose_explicit = vlm_mode != "off" or use_ocr

            # 全關 + 沒 OCR/VLM = 無方法可用、直接 fail
            if (not use_uia_layer and not use_cv_layer and not use_coord_layer
                    and not user_chose_explicit):
                fail_msg = "UIA / CV / 強制座標 三個 toggle 全關、又沒啟用 OCR/VLM、無方法可用"
                logger.error(f"[computer_use]   ✗ {fail_msg}")
                return ActionResult(False, index, atype, fail_msg)

            # ── Phase 0:UIA-first ────────────────────────────────────────────
            ui_info = action.get("ui") if isinstance(action.get("ui"), dict) else None
            uia_first_attempted = False
            if ui_info and use_uia_layer and not user_chose_explicit:
                logger.info(f"[computer_use]   [UIA-first] 嘗試定位: name='{ui_info.get('name', '')[:40]}' type='{ui_info.get('control_type', '')}' auto_id='{ui_info.get('automation_id', '')[:30]}'")
                uia_first_attempted = True
                find_click_point = None
                try:
                    from pipeline.uia_lookup import find_click_point as _fcp
                    find_click_point = _fcp
                except Exception as _e1:
                    try:
                        from .uia_lookup import find_click_point as _fcp  # type: ignore
                        find_click_point = _fcp
                    except Exception as _e2:
                        logger.warning(f"[computer_use]   [UIA-first] 載入 uia_lookup 模組失敗:{type(_e2).__name__}: {_e2} (上游 {type(_e1).__name__}: {_e1})")
                if find_click_point is None:
                    logger.info(f"[computer_use]   [UIA-first] uia_lookup 不可用、退下層")
                else:
                    try:
                        point = find_click_point(ui_info, timeout=2.0)
                    except Exception as _e3:
                        logger.warning(f"[computer_use]   [UIA-first] find_click_point 例外:{type(_e3).__name__}: {_e3}")
                        point = None
                    if point:
                        cx, cy = point
                        _do_click(pg, cx, cy, button, clicks, hold_sec, modifiers)
                        hold_tag_u = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} UIA 命中 '{ui_info.get('name', '')[:40]}' "
                               f"({ui_info.get('control_type', '')}) @ ({cx},{cy}){hold_tag_u}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}(UIA-first、{duration}ms)")
                        return ActionResult(True, index, atype, msg, duration)
                    # UIA 沒中 — 看下層 toggle 決定 fall through 還是 strict fail
                    if not use_cv_layer and not use_coord_layer:
                        # 嚴格 UIA 模式(只勾 UIA, CV/座標都關)→ 找不到就死
                        fail_msg = (f"嚴格 UIA 模式: 元素 '{ui_info.get('name', '')[:40]}' 找不到 "
                                    f"(CV 與 強制座標 toggle 都關)")
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    _next = ("CV 模板比對" if use_cv_layer else "強制座標")
                    logger.info(f"[computer_use]   [UIA-first] 沒命中 '{ui_info.get('name', '')[:40]}' → 退 {_next}")
            elif ui_info and not use_uia_layer:
                logger.info(f"[computer_use]   [UIA-first] 跳過 — 使用者關了 UIA toggle")
            elif ui_info and user_chose_explicit:
                _chosen = "VLM" if vlm_mode != "off" else "OCR"
                logger.info(f"[computer_use]   [UIA-first] 跳過 — 使用者顯式選了 {_chosen} primary, 尊重使用者")

            # ── VLM 模式 1：description → OCR ──
            # bug 修補：之前讀錯欄位（讀紅框 search_region），這個模式既然走 OCR，
            # 區域就應該讀使用者在編輯器拉的「藍框」（ocr_box_*），跟純 OCR 路徑一致
            if vlm_mode == "description":
                vlm_prompt_click = (action.get("vlm_prompt") or action.get("description") or "").strip()
                if not vlm_prompt_click:
                    return ActionResult(False, index, atype,
                        "vlm_mode=description 但 vlm_prompt 為空（必填）")
                # 讀藍框（OCR 區域）— 跟下面純 OCR 路徑同邏輯
                _vlm_box_w = int(action.get("ocr_box_width", 0) or 0)
                _vlm_box_h = int(action.get("ocr_box_height", 0) or 0)
                vlm_region: Optional[tuple[int, int, int, int]] = None
                if _vlm_box_w > 0 and _vlm_box_h > 0:
                    vlm_region = (
                        int(action.get("ocr_box_left", 0) or 0),
                        int(action.get("ocr_box_top", 0) or 0),
                        _vlm_box_w,
                        _vlm_box_h,
                    )
                vlm_found, vlm_text, vlm_reason = _vlm_describe_to_text(vlm_prompt_click, vlm_region, logger)
                if not vlm_found:
                    return ActionResult(False, index, atype,
                        f"VLM 在螢幕找不到目標：{vlm_reason}")
                # 用 VLM 給的文字跑 OCR 找實際座標（VLM 不給座標 — 由 OCR 確定性決定）
                find_text_on_screen = None
                try:
                    from pipeline.ocr import find_text_on_screen
                except Exception:
                    try:
                        from .ocr import find_text_on_screen  # type: ignore
                    except Exception as _e:
                        return ActionResult(False, index, atype,
                            f"VLM 給了文字 '{vlm_text}' 但 OCR 模組載不進來：{_e}")
                screen_bgr_v, sxv, syv = _capture_screen()
                near_v = (int(fx), int(fy)) if has_coord else None
                ocr_res_v = find_text_on_screen(
                    screen_bgr_v, vlm_text, origin_x=sxv, origin_y=syv,
                    lang_tag="zh-Hant-TW", near_xy=near_v,
                    search_radius=cv_search_radius, threshold=ocr_threshold,
                    region=vlm_region,
                    strict_region=bool(action.get("ocr_strict_region", False)),
                )
                if not ocr_res_v.found:
                    return ActionResult(False, index, atype,
                        f"VLM 看到目標文字是 '{vlm_text}'（{vlm_reason}），但 OCR 在螢幕上找不到：{ocr_res_v.reason}")
                _do_click(pg, ocr_res_v.center[0], ocr_res_v.center[1],
                          button, clicks, hold_sec, modifiers)
                hold_tag_v = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                msg = (f"{mods_tag} VLM→OCR 點擊 '{vlm_text}' @ {ocr_res_v.center} "
                       f"(VLM: {vlm_reason[:60]}, OCR conf={ocr_res_v.confidence:.2f}){hold_tag_v}")
                duration = int((time.time() - t0) * 1000)
                logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                return ActionResult(True, index, atype, msg, duration)

            # ── VLM 模式 3：grounding → 地端 GUI 定位模型直接給座標 ──
            # 這是唯一一個「VLM 真的決定座標」的模式，前提是用**專門訓練過 GUI 定位**
            # 的地端模型（Mano-CUA 系）。2026-08-03 實測 14/14 命中、誤差中位數 4.5px。
            # 通用雲端模型不適用這條路（給座標不準，那正是 description/anchor_pick
            # 刻意繞開座標的原因）。
            # 失敗一律不讓整步掛掉 —— 往下掉回原本的 CV → 座標流程。
            if vlm_mode == "grounding":
                _g_prompt = (action.get("vlm_prompt") or action.get("description") or "").strip()
                if not _g_prompt:
                    return ActionResult(False, index, atype,
                        "vlm_mode=grounding 但 vlm_prompt 為空（必填，描述要點什麼）")
                _g_ok = False
                try:
                    from pipeline import vlm_grounding as _vg
                    _scr, _ox, _oy = _capture_screen()
                    _reg = _parse_search_region(action)
                    # ⚠ 2026-08-06：這裡原本只讀 search_region，而那個欄位只有使用者
                    #   「手動拖過」橘框才有值 —— 沒拖過就把整個螢幕丟給模型。
                    #   同一個橘框 CV 遵守、直接定位卻無視，使用者看到框卻不知道
                    #   模型其實在看整片桌面。畫面上候選越多，模型「一定要指一個
                    #   最像的」這個習性就越容易害人。沒拖過時改用編輯器畫的那個
                    #   預設框（錄製座標 ±cv_search_radius）。
                    if _reg is None and isinstance(action.get("x"), (int, float)):
                        _rr = max(1, int(cv_search_radius))
                        _reg = (int(action["x"]) - _rr, int(action["y"]) - _rr,
                                _rr * 2, _rr * 2)
                    if _reg is not None:
                        _l, _t, _w, _h = _reg
                        _x0, _y0 = max(0, _l - _ox), max(0, _t - _oy)
                        _x1 = min(_scr.shape[1], _x0 + _w)
                        _y1 = min(_scr.shape[0], _y0 + _h)
                        if _x1 > _x0 and _y1 > _y0:
                            _scr = _scr[_y0:_y1, _x0:_x1]
                            _ox, _oy = _ox + _x0, _oy + _y0
                    import cv2 as _cv2
                    # 一定要寫在共用交換目錄 —— Windows 的 %TEMP% 沒掛進容器，
                    # 容器會回「No such file or directory」（2026-08-03 實測）。
                    # 檔名也只用 ASCII，避免路徑編碼問題。
                    _png = str(_vg.shared_dir() / f"_shot_{os.getpid()}_{index}.png")
                    _ok_w, _enc = _cv2.imencode(".png", _scr)
                    if not _ok_w:
                        raise RuntimeError("截圖編碼失敗")
                    Path(_png).write_bytes(_enc.tobytes())
                    try:
                        _ok, _gx, _gy, _why = _vg.locate(
                            _g_prompt, _png, _scr.shape[1], _scr.shape[0], logger)
                    finally:
                        try:
                            os.unlink(_png)
                        except OSError:
                            pass
                    if _ok:
                        # ── 錨點局部驗證 ────────────────────────────────────
                        # 2026-08-03 實測:模型**不會承認找不到東西**。問它畫面上
                        # 沒有的元素(例:在 Excel 裡問「開始播放投影片」按鈕),
                        # 它會自信地指一個最像的位置。提示詞只擋得住最離譜的那種。
                        # → 步驟通常還留著錄製時的錨點圖,拿它跟「模型指的位置」
                        #   做局部比對:像 = 採用;完全不像 = 判定幻覺、退回 CV。
                        #   這裡是**局部**比對(只看那一小塊),跟 CV 全域搜尋不同 ——
                        #   CV 全域找不到正是使用者選這個模式的原因。
                        _verified = True
                        if tpl_path is not None and Path(tpl_path).exists():
                            try:
                                _tpl = _imread_unicode(Path(tpl_path))
                                if _tpl is not None:
                                    _th, _tw = _tpl.shape[:2]
                                    # 在座標周圍開一個小窗「搜尋」，而不是在座標上「對齊」。
                                    # 實測固定位置比對太脆:模型誤差 3px 相似度就從 1.00 掉到 0.31,
                                    # 誤差 11px 掉到 0.17 —— 跟幻覺的 0.02~0.07 只差一點點。
                                    # 開窗搜尋容許幾像素位移，正確結果的分數才拉得開。
                                    # 純色錨點跟任何東西比都 1.000（實測連雜訊都是），
                                    # 拿它「驗證」等於蓋橡皮圖章。守不住就要講出來。
                                    _av = _anchor_variance(_tpl)
                                    if _av < ANCHOR_MIN_VARIANCE:
                                        logger.warning(
                                            f"[computer_use]   ⚠ 錨點幾乎沒有特徵"
                                            f"（灰階變異數 {_av:.1f} < {ANCHOR_MIN_VARIANCE}）——"
                                            f"幻覺守門對這張圖無效，本次定位未經驗證。"
                                            f"建議重圈一個含文字或邊框的範圍。")
                                        raise _SkipVerify
                                    _mg = VLM_GROUNDING_VERIFY_MARGIN
                                    _sh, _sw = _scr.shape[:2]
                                    # ⚠ 目標在畫面邊緣時，直接依座標往兩邊截會讓窗比錨點
                                    #   還小，然後「比不了」靜默放行 —— 等於邊緣位置永遠
                                    #   免驗證。改成把窗整個推回畫面內再比。
                                    _ww, _wh = _tw + 2 * _mg, _th + 2 * _mg
                                    _x0 = min(max(0, _gx - _ww // 2), max(0, _sw - _ww))
                                    _y0 = min(max(0, _gy - _wh // 2), max(0, _sh - _wh))
                                    _win = _scr[_y0:_y0 + _wh, _x0:_x0 + _ww]
                                    if (_win.shape[0] >= _th and _win.shape[1] >= _tw):
                                        _score, _mode = _best_match_score(_win, _tpl)
                                        _verified = _score >= VLM_GROUNDING_VERIFY_MIN
                                        logger.info(
                                            f"[computer_use]   錨點局部驗證 相似度 {_score:.3f}"
                                            f"（{_mode}、門檻 {VLM_GROUNDING_VERIFY_MIN}、"
                                            f"搜尋窗 ±{_mg}px）"
                                            f"{'通過' if _verified else ' ✗ 疑似幻覺'}")
                            except _SkipVerify:
                                pass          # 已在上面 warning，維持 _verified=True
                            except Exception as _ve:
                                logger.debug(f"[computer_use]   局部驗證跳過：{_ve}")
                        if _verified:
                            _sx, _sy = _ox + _gx, _oy + _gy
                            logger.info(f"[computer_use]   VLM 定位 → 螢幕 ({_sx},{_sy})：{_why[:70]}")
                            _do_click(pg, _sx, _sy, button, clicks, hold_sec, modifiers)
                            return ActionResult(True, index, atype,
                                f"{mods_tag}VLM 定位點擊 ({_sx},{_sy})：{_g_prompt[:40]}")
                        logger.warning("[computer_use]   VLM 指的位置與錨點不符 → 退回 CV")
                    else:
                        logger.warning(f"[computer_use]   VLM 定位失敗（{_why}）→ 退回 CV")
                except Exception as _e:
                    logger.warning(
                        f"[computer_use]   VLM 定位例外（{_e.__class__.__name__}: {_e}）→ 退回 CV")
                # 沒 return = 往下走既有的 CV / 座標 fallback
                ocr_will_run = False

            # ── VLM 模式 2：anchor_pick → 用挑出的錨點走 CV ──
            if vlm_mode == "anchor_pick":
                anchor_names = list(action.get("vlm_anchors") or [])
                if not anchor_names:
                    return ActionResult(False, index, atype,
                        "vlm_mode=anchor_pick 但 vlm_anchors 為空（必填，至少 2 張變體錨點圖檔名）")
                vlm_prompt_pick = (action.get("vlm_prompt") or action.get("description") or "").strip()
                if not vlm_prompt_pick:
                    return ActionResult(False, index, atype,
                        "vlm_mode=anchor_pick 但 vlm_prompt 為空（必填，告訴 VLM 要點什麼）")
                region_rect_p = _parse_search_region(action)
                # ⚡ 先用純 CV 試一輪（免費、~50ms，實測比讓模型挑更準）。
                #    每張候選都比一次；最高分過門檻且明顯領先第二名就直接用它，
                #    完全不用打雲端 API。分不出來（分數接近 or 都太低）才退回 VLM。
                #    2026-08-06 實測：最大化↔還原兩態互測 1.000 vs 0.707，
                #    差距 0.29 —— 這種情況 CV 分得非常清楚。
                _CV_PICK_MIN = float(action.get("confidence") or cv_threshold)
                _CV_PICK_MARGIN = 0.10
                ok_pick, picked_idx, pick_reason = False, 0, ""
                try:
                    _scores = []
                    for _nm in anchor_names:
                        _p = assets_dir / _nm
                        if not _p.is_file():
                            _scores.append(-1.0)
                            continue
                        _r = find_template(str(_p), threshold=0.0, multi_scale=True,
                                           region=region_rect_p, mode="gray")
                        _scores.append(_r.confidence)
                    _order = sorted(range(len(_scores)), key=lambda i: _scores[i], reverse=True)
                    _top = _order[0]
                    _second = _scores[_order[1]] if len(_order) > 1 else -1.0
                    if _scores[_top] >= _CV_PICK_MIN and (_scores[_top] - _second) >= _CV_PICK_MARGIN:
                        ok_pick, picked_idx = True, _top
                        pick_reason = (f"純 CV 直接分辨（{anchor_names[_top]}={_scores[_top]:.2f}，"
                                       f"第二名 {_second:.2f}），未呼叫 VLM")
                        logger.info(f"[computer_use]   anchor_pick：{pick_reason}")
                except Exception as _ce:
                    logger.debug(f"[computer_use]   anchor_pick 純 CV 預篩跳過：{_ce}")
                if not ok_pick:
                    logger.info("[computer_use]   anchor_pick：純 CV 分不出來 → 交給 VLM 挑")
                    ok_pick, picked_idx, pick_reason = _vlm_pick_anchor(
                        vlm_prompt_pick, anchor_names, assets_dir, region_rect_p, logger)
                if not ok_pick:
                    return ActionResult(False, index, atype,
                        f"VLM 無法挑錨點：{pick_reason}")
                # 用挑出來的錨點走標準 CV template matching（重新指向 tpl_path）
                picked_name = anchor_names[picked_idx]
                tpl_path = assets_dir / picked_name
                logger.info(f"[computer_use]   VLM 挑了錨點 [{picked_idx}] {picked_name}（{pick_reason[:80]}）→ 走 CV 比對")
                # 不 return — 讓控制流繼續往下走 CV 路徑（vlm_mode 短路掉 OCR 模式）
                ocr_will_run = False  # 確定要走 CV 不走 OCR

            # 短路到「強制座標」: 使用者顯式關了 CV toggle 但保留座標 toggle = 直接點記錄座標
            # use_cv=False + use_coord=True 才走這裡, 預設(use_cv=True)永遠跑 CV 模板比對
            # OCR / vlm_mode=anchor_pick / vlm_mode=description 各有自己路徑、不走這裡
            if (vlm_mode != "anchor_pick"
                and not use_cv_layer
                and use_coord_layer
                and has_coord
                and not ocr_will_run):
                _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                _src = "UIA 沒中" if uia_first_attempted else "CV toggle 關"
                msg = f"[強制座標 ({_src})]{mods_tag} 點擊 ({fx},{fy}) button={button} clicks={clicks}{hold_tag}"
                duration = int((time.time() - t0) * 1000)
                logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                return ActionResult(True, index, atype, msg, duration)

            # Hover 預熱：錄製當下游標停在按鈕上、錨點擷取到 Windows hover highlight
            # 狀態；回放用 pyautogui 瞬移沒觸發 hover → 螢幕與錨點不一樣 conf 掉
            # 把游標移到錄製座標附近、等指定 ms 讓 hover 效果渲染後再比對
            # OCR 模式跳過 hover（純文字偵測不受 hover 影響，而且可能反而干擾游標位置）
            if cv_trigger_hover and has_coord and not ocr_will_run:
                try:
                    pg.moveTo(int(fx), int(fy))
                    time.sleep(max(50, int(cv_hover_wait_ms)) / 1000.0)
                except Exception:
                    pass  # 移動失敗就略過（例如座標超出螢幕），後面搜尋仍然照跑

            # ── OCR 模式分支 ──
            # 只有 use_ocr=True 且 ocr_text 有值才跑。OCR 失敗時的後續行為由 ocr_cv_fallback 控制：
            #   ocr_cv_fallback=False（預設）→ 失敗立即 FAIL（符合「選 OCR 就代表 CV 不適用」的直覺）
            #   ocr_cv_fallback=True         → 失敗繼續走下面的 CV 比對鏈（再受 cv_coord_fallback 接棒）
            if ocr_will_run:
                find_text_on_screen = None
                try:
                    from pipeline.ocr import find_text_on_screen
                except Exception:
                    try:
                        from .ocr import find_text_on_screen  # type: ignore
                    except Exception as _e:
                        logger.error(f"[computer_use]   ✗ 無法載入 OCR 模組：{_e}")
                if find_text_on_screen is not None:
                    screen_bgr, sx, sy = _capture_screen()
                    near = (int(fx), int(fy)) if has_coord else None
                    # 藍框：per-action 顯式 OCR 搜尋範圍（絕對桌面座標）
                    ocr_region = None
                    _box_w = int(action.get("ocr_box_width", 0) or 0)
                    _box_h = int(action.get("ocr_box_height", 0) or 0)
                    if _box_w > 0 and _box_h > 0:
                        ocr_region = (
                            int(action.get("ocr_box_left", 0) or 0),
                            int(action.get("ocr_box_top", 0) or 0),
                            _box_w,
                            _box_h,
                        )
                    ocr_res = find_text_on_screen(
                        screen_bgr, ocr_text, origin_x=sx, origin_y=sy,
                        lang_tag="zh-Hant-TW",
                        near_xy=near, search_radius=cv_search_radius,
                        threshold=ocr_threshold,
                        region=ocr_region,
                        strict_region=bool(action.get("ocr_strict_region", False)),
                    )
                    if ocr_res.found:
                        _do_click(pg, ocr_res.center[0], ocr_res.center[1],
                                  button, clicks, hold_sec, modifiers)
                        hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} 點擊 OCR 文字 '{ocr_text}' @ {ocr_res.center} "
                               f"(matched='{ocr_res.text[:30]}', conf={ocr_res.confidence:.2f}){hold_tag}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
                        return ActionResult(True, index, atype, msg, duration)
                    # OCR 失敗
                    if not ocr_cv_fallback:
                        fail_msg = f"{ocr_res.reason}（ocr_cv_fallback=off → 失敗直接 FAIL 不退回 CV/座標）"
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    logger.info(f"[computer_use]   {ocr_res.reason[:120]}，ocr_cv_fallback=on → 改試 CV 比對")
                elif not ocr_cv_fallback:
                    # OCR 模組載不進來且使用者沒開 fallback → 直接 FAIL（不偷偷走 CV）
                    return ActionResult(False, index, atype, "OCR 模組無法載入且 ocr_cv_fallback=off")

            # 搜尋策略：
            # 1. 有錄製座標 → 先在附近 ±cv_search_radius 範圍搜尋（防跨螢幕假陽性）
            #    首次 match 若 conf 低於門檻，等 150ms 再 retry 一次（最多 2 次）
            #    吸收 hover fade-in / transition 動畫未穩定造成的瞬時誤判
            #    典型 case：Windows 關閉鈕第一次 match 得 0.56、再等 150ms 變 0.97
            # 2. 仍找不到：若 cv_search_only_near=True → 直接 FAIL
            #              否則擴大到整個桌面
            # 3. 全螢幕也找不到 → 退回絕對座標 fallback（下方 else 分支）
            _SETTLE_RETRIES = 2          # 第一次 + 最多 1 次 retry
            _SETTLE_WAIT_MS = 150        # retry 前 sleep
            _FULLSCREEN_MIN_THRESHOLD = 0.80   # 全桌面 fallback 的最低 CV 門檻(避免大螢幕假匹配點錯)

            # 使用者明確指定的搜尋紅框（優先於錄製座標附近搜尋）
            region_rect = _parse_search_region(action)
            cv_strict = bool(action.get("cv_strict_region", False))

            def _search(nx_: Optional[int], ny_: Optional[int],
                        force_region: Optional[tuple[int, int, int, int]] = "use_outer",
                        threshold_override: Optional[float] = None) -> MatchResult:
                """先跑 gray 模式，若 conf < threshold 再跑 edge 模式，取較高 conf。
                edge 對 hover fade / 主題色差異更容忍，代價 +20ms。

                force_region:
                  "use_outer"（預設）→ 用外層 region_rect（紅框）
                  None              → 強制忽略 region_rect，用 nx_/ny_ 或全螢幕
                  tuple             → 強制用這個 region
                threshold_override: 全桌面搜尋時傳較高門檻,避免大螢幕假匹配點錯位置。
                """
                if force_region == "use_outer":
                    use_region = region_rect
                else:
                    use_region = force_region
                eff_threshold = threshold if threshold_override is None else threshold_override

                def _find_one(p: str, m: str) -> MatchResult:
                    if use_region is not None:
                        return find_template(p, threshold=eff_threshold, multi_scale=True,
                                             region=use_region, mode=m)
                    if nx_ is not None and ny_ is not None:
                        return find_template(p, threshold=eff_threshold, multi_scale=True,
                                             near_xy=(nx_, ny_), search_radius=cv_search_radius, mode=m)
                    return find_template(p, threshold=eff_threshold, multi_scale=True, mode=m)

                def _find(m: str) -> MatchResult:
                    """單張時就是一次比對；多形態時每張都比、取分數最高的。"""
                    best = _find_one(str(tpl_candidates[0][1]), m)
                    if len(tpl_candidates) == 1:
                        return best
                    best_name = tpl_candidates[0][0]
                    for _nm, _p in tpl_candidates[1:]:
                        r = _find_one(str(_p), m)
                        if r.confidence > best.confidence:
                            best, best_name = r, _nm
                    if best_name != img_name:
                        logger.info(f"[computer_use]   多形態錨點[{m}]：採用 {best_name}"
                                    f"（conf={best.confidence:.2f}）")
                    return best
                gray = _find("gray")
                if gray.found:
                    return gray
                # Gray 沒過門檻 → 試 edge 救一下
                edge = _find("edge")
                # 以 conf 做仲裁，但考量 edge 先天分數偏低，edge 要多給 0.05 才可以勝出
                # 避免 gray 比較接近但仍低、edge 亂抓到邊緣多的位置
                if edge.found or edge.confidence >= gray.confidence + 0.05:
                    logger.info(f"[computer_use]   edge fallback: gray={gray.confidence:.2f}, edge={edge.confidence:.2f} → 採用 edge")
                    return edge
                return gray

            if has_coord:
                # Phase 1：紅框內找（如果 region_rect 設了），或近錄製座標找
                m = MatchResult(False)
                for _attempt in range(_SETTLE_RETRIES):
                    m = _search(int(fx), int(fy))
                    if m.found:
                        break
                    if _attempt + 1 < _SETTLE_RETRIES:
                        logger.info(f"[computer_use]   首次比對 conf={m.confidence:.2f} < {threshold}，等 {_SETTLE_WAIT_MS}ms 讓動畫穩定後 retry")
                        time.sleep(_SETTLE_WAIT_MS / 1000.0)
                # Phase 2：紅框 miss、不嚴格 → 退回錄製座標附近 ±cv_search_radius 找
                if not m.found and region_rect is not None and not cv_strict:
                    logger.info(f"[computer_use]   紅框內找不到 → 退回錄製座標附近 ±{cv_search_radius}px 重試")
                    m = _search(int(fx), int(fy), force_region=None)
                # Phase 3：附近 miss、不嚴格、未鎖定附近 → 擴大全螢幕
                if not m.found and not cv_search_only_near and not cv_strict:
                    logger.info(f"[computer_use]   附近 ±{cv_search_radius}px 找不到（best={m.confidence:.2f}），擴大到整個桌面（門檻提高避免誤點）")
                    # 全桌面搜尋強制較高門檻:0.5 在 4K/多螢幕幾乎必有假陽性 → 點到無關位置卻當成功
                    m = _search(None, None, force_region=None,
                                threshold_override=max(threshold, _FULLSCREEN_MIN_THRESHOLD))
                # 嚴格模式 + 紅框 miss：明確標示原因
                if not m.found and cv_strict and region_rect is not None:
                    m.reason = f"嚴格鎖定範圍：紅框內找不到 {img_name}（{m.reason}）"
            else:
                # 無錄製座標 → 純全桌面模板搜尋,同樣用較高門檻避免誤點
                m = _search(None, None, threshold_override=max(threshold, _FULLSCREEN_MIN_THRESHOLD))

            if m.found:
                # 螢幕邊緣擷取時，點擊位置不在錨點影像中心，加上偏移校正
                off_x = int(action.get("anchor_off_x", 0) or 0)
                off_y = int(action.get("anchor_off_y", 0) or 0)
                click_x = m.center[0] + int(off_x * m.scale)
                click_y = m.center[1] + int(off_y * m.scale)
                _do_click(pg, click_x, click_y, button, clicks, hold_sec, modifiers)
                hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                off_tag = f" off=({off_x},{off_y})" if (off_x or off_y) else ""
                msg = f"{mods_tag} 點擊 {img_name} @ ({click_x},{click_y}) (conf={m.confidence:.2f} [{m.mode}], scale={m.scale}){off_tag}{hold_tag}"
            else:
                # 嚴格鎖定範圍：紅框 miss 時連座標 fallback 都禁掉
                if cv_strict and region_rect is not None:
                    fail_msg = m.reason
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                # Fallback 判斷(分三層 gate):
                #   1. has_coord:有錄製座標(沒有就根本退不了)
                #   2. allow_coord_fallback:系統層級信心(螢幕解析度跟錄製時相同)
                #   3. use_coord_layer:action 層級「📍 強制座標」 toggle(三層 fallback 的最後一層)
                #
                # step-level cv_coord_fallback 只在「使用者顯式進入進階模式」時生效:
                #   - OCR primary (use_ocr=True)
                #   - CV-only 自定義 (use_uia=False)
                # 預設三層 fallback (UIA ☑ + CV ☑ + 座標 ☑) 完全忽略 cv_coord_fallback、
                # 避免 step-level 跟 action-level 兩個 toggle 互相干擾、使用者全勾預設卻退不到座標的 bug。
                is_explicit_advanced = use_ocr or (not use_uia_layer)
                step_level_blocks_fallback = is_explicit_advanced and not (
                    cv_coord_fallback or bool(action.get("coord_fallback", False)))

                if (has_coord and allow_coord_fallback and use_coord_layer
                        and not step_level_blocks_fallback):
                    logger.warning(f"[computer_use]   ⚠ 圖像比對失敗({m.reason}),退回錄製座標 ({fx},{fy})")
                    _do_click(pg, int(fx), int(fy), button, clicks, hold_sec, modifiers)
                    hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                    msg = f"[fallback]{mods_tag} 點擊絕對座標 ({fx},{fy}){hold_tag}(原圖 {img_name} 找不到)"
                elif has_coord and not allow_coord_fallback:
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且目前螢幕解析度與錄製時不同,"
                        f"絕對座標 ({fx},{fy}) 不可信、請重錄或調整到原螢幕布局")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                elif has_coord and not use_coord_layer:
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且使用者關閉了 action 的「📍 強制座標」 toggle。"
                        f"若要容錯請到 panel 打開該 toggle。")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                elif has_coord and step_level_blocks_fallback:
                    _mode = "OCR 進階模式" if use_ocr else "CV-only 模式"
                    fail_msg = (f"找不到錨點圖 {img_name}({m.reason}),且在 {_mode} 下沒開「CV 失敗退回座標」。"
                        f"若要容錯請勾該動作的「📍 座標 (fallback)」,或到 CV 設定打開整個節點的 toggle。")
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)
                else:
                    fail_msg = f"找不到錨點圖 {img_name}（{m.reason}），且無 fallback 座標可用"
                    logger.error(f"[computer_use]   ✗ {fail_msg}")
                    return ActionResult(False, index, atype, fail_msg)

        elif atype == "click_at":
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            in_range, layout_info = _point_in_any_screen(x, y)
            if not in_range:
                return ActionResult(False, index, atype,
                    f"座標 ({x},{y}) 超出目前螢幕範圍（{layout_info}）")
            button = action.get("button", "left")
            clicks = int(action.get("clicks", 1))
            hold_sec = float(action.get("hold_sec", 0) or 0)
            modifiers = list(action.get("modifiers", []) or [])
            mods_tag = f"[{'+'.join(modifiers)}]" if modifiers else ""

            # 三層 fallback toggle — click_at 沒有 CV 模板 (它就是純座標 action),
            # 所以只有 UIA + 強制座標 兩層可用。CV toggle 在 click_at 沒效。
            use_uia_layer = bool(action.get("use_uia", True))
            use_coord_layer = bool(action.get("use_coord", True))
            if not use_uia_layer and not use_coord_layer:
                fail_msg = "click_at: UIA / 強制座標 兩個 toggle 都關、無方法可用"
                logger.error(f"[computer_use]   ✗ {fail_msg}")
                return ActionResult(False, index, atype, fail_msg)

            # Phase 0: UIA-first — 有錄到 ui + use_uia=True 才跑
            ui_info = action.get("ui") if isinstance(action.get("ui"), dict) else None
            if ui_info and use_uia_layer:
                try:
                    from pipeline.uia_lookup import find_click_point as _uia_find
                except Exception:
                    try:
                        from .uia_lookup import find_click_point as _uia_find  # type: ignore
                    except Exception:
                        _uia_find = None  # type: ignore
                if _uia_find is not None:
                    point = _uia_find(ui_info, timeout=2.0)
                    if point:
                        cx, cy = point
                        _do_click(pg, cx, cy, button, clicks, hold_sec, modifiers)
                        hold_tag_u = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
                        msg = (f"{mods_tag} UIA 命中 '{ui_info.get('name', '')[:40]}' "
                               f"@ ({cx},{cy}){hold_tag_u}")
                        duration = int((time.time() - t0) * 1000)
                        logger.info(f"[computer_use]   ✓ {msg}(UIA-first、{duration}ms)")
                        return ActionResult(True, index, atype, msg, duration)
                    if not use_coord_layer:
                        # 嚴格 UIA: 沒命中 + 座標 toggle 關 = fail
                        fail_msg = f"嚴格 UIA 模式: 元素 '{ui_info.get('name', '')[:40]}' 找不到 (強制座標 toggle 關)"
                        logger.error(f"[computer_use]   ✗ {fail_msg}")
                        return ActionResult(False, index, atype, fail_msg)
                    logger.info(f"[computer_use]   UIA 沒命中 → 退到強制座標")
            elif ui_info and not use_uia_layer:
                logger.info(f"[computer_use]   UIA 跳過 — 使用者關了 UIA toggle, 直接走強制座標")
            _do_click(pg, x, y, button, clicks, hold_sec, modifiers)
            hold_tag = f" hold={hold_sec}s" if hold_sec > 0.1 else ""
            msg = f"{mods_tag} 點擊絕對座標 ({x}, {y}){hold_tag}"

        elif atype == "type_text":
            text = action.get("text", "")
            if not text:
                return ActionResult(False, index, atype, "type_text 缺 text 欄位")
            # {{變數}} 替換 —— 沒有這層,前面 ocr_get_text / uia_get_text 取到的值填不進去,
            # 會把「{{發票金額}}」這幾個字原封不動打進欄位(看起來成功、內容全錯)。
            # uia_send_keys 一直有做,pixel 路徑的 type_text 之前漏了。
            if "{{" in text:
                try:
                    from .uia_executor import _substitute_vars
                    _before = text
                    text = _substitute_vars(text, step_variables)
                    if text != _before:
                        logger.info(f"[computer_use] type_text 變數替換：{_before[:40]!r} → {text[:40]!r}")
                except Exception as _e:
                    logger.warning(f"[computer_use] type_text 變數替換失敗:{_e}")
                # ⚠ _substitute_vars 找不到變數時**原樣保留**,不擋的話會把「{{金額}}」
                #   這幾個字面字元打進金額欄位,而且動作回報成功 —— 靜默填錯值。
                #   寧可響亮失敗。常見原因:取值動作排在後面、變數名打錯、
                #   取值放在 if_image_found 之類的巢狀動作裡。
                _left = re.findall(r"\{\{[^}]*\}\}", text)
                if _left:
                    return ActionResult(
                        False, index, atype,
                        f"變數未定義:{_left[:3]} —— 目前有的變數:{list(step_variables) or '(空)'}。"
                        f"拒絕把字面 {{{{}}}} 填進欄位。"
                        f"檢查:取值動作是否排在這一步之前、變數名是否一致")
            # ⚠ 一律走剪貼簿貼上,不要用 pg.write 逐字打。
            #   實測(中文 Windows):輸入法在啟用狀態時會攔截 pg.write 送出的按鍵 ——
            #   填「40,425」實際填進去變成「ˋ誒ㄓ」,而且**動作回報成功**。
            #   這對 RPA 填金額欄位是致命的:每次都填錯、還看不出來。
            #   含中文的字串本來就走剪貼簿(所以以前只有純 ASCII 的數字會中招),
            #   現在統一 —— 貼上是 IME 免疫的。
            #   要逐字打(某些欄位擋貼上、或需要觸發 keypress 事件)就設 type_method: "keys"。
            _method = (action.get("type_method") or "clipboard").lower()
            # ⚠ 含 \n / \t 一律逐字打。pyautogui 把它們當 Enter / Tab **按鍵**送出
            #   (錄製出來的 "帳號\t密碼\n" 靠這個跳欄與送出);走剪貼簿貼上的話
            #   單行欄位會吃掉或整段拒收 —— 表單沒送出、焦點沒跳欄,動作卻回報成功。
            if _method != "keys" and ("\n" in text or "\t" in text):
                _method = "keys"
                logger.info("[computer_use] type_text 含換行/Tab、改走逐字打(保留按鍵語意)")
            if _method == "keys":
                pg.write(text, interval=0.03)
                msg = f"輸入文字（逐字、注意輸入法可能攔截）：{text[:30]}"
            else:
                try:
                    import pyperclip
                    _prev = None
                    try:
                        _p = pyperclip.paste()      # 用完還原,別洗掉使用者的剪貼簿
                        # ⚠ 只在原本是「非空文字」時才還原。pyperclip 在 Windows 只讀
                        #   CF_UNICODETEXT,剪貼簿裡是圖片/檔案/Excel 區塊時回空字串;
                        #   拿空字串去 copy() 會先清空剪貼簿再什麼都不放 ——
                        #   等於把使用者複製好的圖片直接清掉(跟註解說的相反)。
                        if isinstance(_p, str) and _p:
                            _prev = _p
                    except Exception:
                        pass
                    pyperclip.copy(text)
                    pg.hotkey("ctrl", "v")
                    time.sleep(0.15)                # 等貼上完成再還原,否則會貼到舊內容
                    if _prev is not None:
                        try:
                            pyperclip.copy(_prev)
                        except Exception:
                            pass
                    msg = f"輸入文字（clipboard、IME 免疫）：{text[:30]}"
                except Exception as _e:
                    # 沒 pyperclip 只能退回逐字打 —— 明講風險,別讓人以為沒事
                    pg.write(text, interval=0.03)
                    msg = f"輸入文字（逐字、clipboard 不可用:{_e}；輸入法可能攔截）：{text[:30]}"

        elif atype == "hotkey":
            keys = action.get("keys", [])
            if not keys:
                return ActionResult(False, index, atype, "hotkey 缺 keys 欄位")
            # 單獨按修飾鍵（Shift / Ctrl / Alt / Win）要特別處理：
            # pyautogui.hotkey("shift") 底層用老 API keybd_event，Windows IME 的
            # 中英切換 hotkey 常常觸發不到。改用 pynput（SendInput）並明確拉長
            # press→release 間隔，讓 IME 有時間辨識為「獨立按 tap」。
            _MOD_TO_PYNPUT = {"shift": "shift", "ctrl": "ctrl", "alt": "alt",
                              "win": "cmd", "cmd": "cmd"}
            if len(keys) == 1 and keys[0].lower() in _MOD_TO_PYNPUT:
                from pynput.keyboard import Controller as _KC, Key as _K
                _kc = _KC()
                _pk = getattr(_K, _MOD_TO_PYNPUT[keys[0].lower()])
                _kc.press(_pk)
                time.sleep(0.12)
                _kc.release(_pk)
                msg = f"單按 {keys[0]}（pynput tap，IME-safe）"
            else:
                pg.hotkey(*keys)
                msg = f"熱鍵：{'+'.join(keys)}"

        elif atype == "wait":
            sec = float(action.get("seconds", 0.0))
            # 分段 sleep，中間可以 abort
            total, step = sec, 0.2
            while total > 0:
                _check_abort(run_id)
                time.sleep(min(step, total))
                total -= step
            msg = f"等待 {sec}s"

        elif atype == "wait_image":
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "wait_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 10.0))
            # action.confidence 沒設或為 0 → 退步驟層級 cv_threshold（跟 click_image 一致）
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            # until: disappear = 等錨點圖「消失」(查詢遮罩、loading 動畫收掉)
            _u = action.get("until")
            want_gone = isinstance(_u, str) and _u.strip() == "disappear"
            while time.time() < deadline:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if not want_gone and m.found:
                    msg = f"{img_name} 出現（conf={m.confidence:.2f}）"
                    break
                if want_gone and not m.found:
                    msg = f"{img_name} 已消失"
                    break
                last_conf = max(last_conf, m.confidence)
                time.sleep(0.3)
            else:
                if want_gone:
                    return ActionResult(False, index, atype,
                        f"等待 {timeout}s {img_name} 仍在畫面上（conf={last_conf:.2f}）")
                return ActionResult(False, index, atype,
                    f"等待 {timeout}s 仍未出現 {img_name}（最佳 {last_conf:.2f} < {threshold}）")

        elif atype == "drag":
            x1 = int(action.get("x", 0))
            y1 = int(action.get("y", 0))
            x2 = int(action.get("x2", 0))
            y2 = int(action.get("y2", 0))
            button = action.get("button", "left")
            # 起點：預設使用絕對座標；只有使用者切到圖像模式（use_coord=False）才嘗試圖像定位校正
            img_name = action.get("image", "")
            if img_name and action.get("use_coord", True) is False:
                tpl_path = assets_dir / img_name
                # drag 也吃 step 層級 cv_threshold / cv_search_radius
                threshold = float(action.get("confidence") or cv_threshold)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  near_xy=(x1, y1), search_radius=cv_search_radius)
                if m.found:
                    dx = m.center[0] - x1
                    dy_shift = m.center[1] - y1
                    x1, y1 = m.center[0], m.center[1]
                    # 終點同步偏移，保持相對位移
                    x2 += dx
                    y2 += dy_shift
                elif cv_search_only_near:
                    return ActionResult(False, index, atype,
                        f"【只搜附近模式】drag 起點在 ({x1},{y1}) ±{cv_search_radius}px 內找不到錨點 {img_name}")
            # 座標防護：超出螢幕就拒絕執行
            for cx, cy, label in [(x1, y1, "起點"), (x2, y2, "終點")]:
                in_range, layout_info = _point_in_any_screen(cx, cy)
                if not in_range:
                    return ActionResult(False, index, atype,
                        f"拖曳{label}座標 ({cx},{cy}) 超出目前螢幕（{layout_info}）")
            # Windows 的 DragDetect 要求 mouseDown 後第一個 move 必須**嚴格超過 SM_CXDRAG (~4px)**
            # 才觸發真正的 OLE Drag-Drop。pyautogui.dragTo + 平順 lerp 常常第一步 < 4px 就被當
            # 普通點擊。解法：press 前從偏移位置抵達產生「pre-move delta」，press 後立刻做一個
            # 6px 的明顯跳躍突破閾值，再開始平滑 lerp。
            # 參考：https://devblogs.microsoft.com/oldnewthing/20100304-00/?p=14733
            from pynput.mouse import Controller as _MC, Button as _Btn
            _mc = _MC()
            _btn_map = {"left": _Btn.left, "right": _Btn.right, "middle": _Btn.middle}
            _btn = _btn_map.get(button, _Btn.left)
            drag_mods = list(action.get("modifiers", []) or [])
            # 修飾鍵在整個拖曳期間都要按著（Shift+drag=移動、Ctrl+drag=複製）
            for mod in drag_mods:
                pg.keyDown(mod)
            try:
                # 計算單位方向（用來做 6px 初始跨閾值跳躍；若起終點距離 < 6px 就固定往右跳）
                dx = x2 - x1
                dy = y2 - y1
                dist = max(1, (dx * dx + dy * dy) ** 0.5)
                nx, ny = dx / dist, dy / dist

                # 1. 先從偏移位置抵達起點，產生真實的 pre-move event
                _mc.position = (int(x1 - nx * 3), int(y1 - ny * 3))
                time.sleep(0.05)
                _mc.position = (x1, y1)
                time.sleep(0.08)
                # 2. 按下
                _mc.press(_btn)
                time.sleep(0.10)
                # 3. 關鍵：press 後第一個 move 必須 > 4px 突破 SM_CXDRAG
                _mc.position = (int(x1 + nx * 6), int(y1 + ny * 6))
                time.sleep(0.06)
                # 4. 剩餘距離分段平滑移動到終點
                steps = 25
                total_move_sec = 0.6
                for i in range(1, steps + 1):
                    t = i / steps
                    mx = int(x1 + nx * 6 + (x2 - (x1 + nx * 6)) * t)
                    my = int(y1 + ny * 6 + (y2 - (y1 + ny * 6)) * t)
                    _mc.position = (mx, my)
                    time.sleep(total_move_sec / steps)
                # 5. 在終點停頓，讓 drop target highlight 起來再放手
                time.sleep(0.25)
                _mc.release(_btn)
            finally:
                # 即使過程拋例外也要放開修飾鍵，避免使用者鍵盤卡在按下狀態
                for mod in reversed(drag_mods):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(drag_mods)}] " if drag_mods else ""
            msg = f"{mods_tag}拖曳 ({x1},{y1}) → ({x2},{y2}) button={button}"

        elif atype == "scroll":
            x = int(action.get("x", 0))
            y = int(action.get("y", 0))
            dy = int(action.get("dy", 0))
            if dy == 0:
                logger.warning(f"[computer_use]   ⚠ scroll action dy=0，略過（action={action}）")
                return ActionResult(False, index, atype, "scroll 缺 dy 欄位或為 0")
            modifiers = list(action.get("modifiers", []) or [])
            # 座標防護：超出螢幕時不移動滑鼠直接在當前位置捲
            in_range, _ = _point_in_any_screen(x, y)
            if in_range:
                pg.moveTo(x, y)
                # Windows 上滑鼠移入新視窗需要短時間觸發 hover，否則後續 scroll 會被吞掉
                time.sleep(0.15)
            # 用 pynput 取代 pyautogui.scroll（pyautogui 在 Windows 有 known bug）
            from pynput.mouse import Controller as _MC
            _mc = _MC()
            # 按下修飾鍵（Ctrl+滾輪 = 縮放）→ scroll → 放開
            for mod in modifiers:
                pg.keyDown(mod)
            try:
                _mc.scroll(0, dy)
            finally:
                for mod in reversed(modifiers):
                    pg.keyUp(mod)
            mods_tag = f"[{'+'.join(modifiers)}] " if modifiers else ""
            msg = f"{mods_tag}在 ({x},{y}) 捲動 dy={dy}"

        elif atype == "activate_window":
            # 將指定標題的視窗帶到前景。解決錄製回放最常見的失敗原因：
            # 目標視窗在背景 → 點擊被其他視窗截去 or hover 作用在錯的視窗。
            # Linux 下 pygetwindow 支援很薄，用 try/except 吞例外並回 FAIL 讓使用者知情。
            title = (action.get("title") or "").strip()
            title_contains = (action.get("title_contains") or "").strip()
            if not title and not title_contains:
                return ActionResult(False, index, atype,
                    "activate_window 缺 title 或 title_contains 欄位")
            timeout = float(action.get("timeout_sec", 3.0))
            try:
                import pygetwindow as gw
            except Exception as e:
                return ActionResult(False, index, atype,
                    f"pygetwindow 無法載入（此平台可能不支援）：{e}")

            def _find_win():
                try:
                    all_wins = gw.getAllWindows()
                except Exception:
                    return []
                if title:
                    wins = [w for w in all_wins if (w.title or "") == title]
                else:
                    # 使用者常把 UIA 的萬用字元 pattern(「SCM Portal*」)原樣填進來;
                    # 字面比對 * 永遠不中。Edge 標題還藏零寬字元,一併去掉再比。
                    _inv = "\u200b\u200c\u200d\ufeff"
                    parts = [p for p in title_contains.lower().split("*") if p]

                    def _hit(t: str) -> bool:
                        t = "".join(ch for ch in t.lower() if ch not in _inv)
                        pos = 0
                        for p in parts:
                            i = t.find(p, pos)
                            if i < 0:
                                return False
                            pos = i + len(p)
                        return True

                    wins = [w for w in all_wins if parts and _hit(w.title or "")]
                return [w for w in wins if (w.title or "").strip()]

            deadline = time.time() + timeout
            target = None
            while True:
                _check_abort(run_id)
                matched = _find_win()
                if matched:
                    target = matched[0]
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.2)

            if target is None:
                needle = title or title_contains
                return ActionResult(False, index, atype,
                    f"{timeout}s 內找不到視窗標題 ~= '{needle}'")

            activated = False
            try:
                # 最小化的視窗必須先 restore 才能被 activate（pygetwindow 已實作這邏輯但不保證）
                if getattr(target, "isMinimized", False):
                    try:
                        target.restore()
                    except Exception:
                        pass
                target.activate()
                activated = True
            except Exception as _gw_err:
                # pygetwindow 在 foreground lock 等情境會拋 PyGetWindowException；改用 Win32 直接搶焦點
                try:
                    import ctypes  # type: ignore
                    hwnd = getattr(target, "_hWnd", None)
                    if hwnd:
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        activated = True
                except Exception:
                    pass
                if not activated:
                    return ActionResult(False, index, atype,
                        f"找到視窗 '{target.title[:60]}' 但無法 activate：{_gw_err}")
            # 給 Window Manager 時間切換焦點，避免下個動作時視窗還沒完全在前
            time.sleep(0.25)
            msg = f"已將視窗 '{(target.title or '')[:60]}' 切到前景"

        elif atype == "assert_image":
            # 驗證某張錨點圖「當下」必須可見（和 wait_image 相似但語意不同：
            # wait_image 等畫面載入、timeout 較長；assert_image 檢查當前狀態、timeout 較短）。
            # 失敗訊息也更精確，方便排查為什麼流程走到這一步畫面長得不對。
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "assert_image 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)
            deadline = time.time() + timeout
            last_conf = 0.0
            found_m: Optional[MatchResult] = None
            while True:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    found_m = m
                    break
                last_conf = max(last_conf, m.confidence)
                if time.time() >= deadline:
                    break
                time.sleep(0.2)
            if found_m is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內 {img_name} 未出現（最佳 {last_conf:.2f} < {threshold}）")
            msg = f"assert 通過：{img_name} 可見（conf={found_m.confidence:.2f}）"

        elif atype == "ocr_get_text":
            # 螢幕 OCR「取值」:找標籤、讀它旁邊的值、存進變數。
            # 跟 assert_text 的差別 —— assert 只回「在不在」,這個回「值是多少」。
            # ⚠ 若目標是可被 UIA 看到的控制項,**優先用 uia_get_text**:讀結構比讀像素準,
            #   也不受解析度/遮擋影響。這個動作是給 UIA 抓不到的畫面用的。
            # ⚠ 若值來自「上傳的檔案」(憑證/發票),用 /ocr/file 對檔案做 —— 原檔解析度
            #   更高、不必先把檔案開起來,比螢幕 OCR 穩定得多。
            label = (action.get("label") or action.get("text") or "").strip()
            save_as = (action.get("save_as") or "").strip()
            if not label:
                return ActionResult(False, index, atype, "ocr_get_text 缺 label 欄位（要找哪個標籤）")
            if not save_as:
                return ActionResult(False, index, atype, "ocr_get_text 缺 save_as 欄位（值要存進哪個變數）")
            try:
                from pipeline.ocr_file import read_field, AMOUNT_RE, IDENT_RE, TAXID_RE
            except Exception as _e:
                return ActionResult(False, index, atype, f"無法載入 OCR 取值模組：{_e}")
            try:
                from pipeline.ocr import _recognize as _ocr_recognize
            except Exception as _e:
                return ActionResult(False, index, atype, f"無法載入 OCR 模組：{_e}")

            # _capture_screen 回 (BGR, 原點x, 原點y) —— 多螢幕時主螢幕左上不一定是 (0,0)
            screen_bgr, sx, sy = _capture_screen()
            ox, oy = sx, sy
            region = _parse_search_region(action)
            if region:
                # search_region 是絕對桌面座標,先換算成截圖內的相對座標再裁
                l, t_, w_, h_ = region
                rl, rt = max(0, l - sx), max(0, t_ - sy)
                screen_bgr = screen_bgr[rt:rt + h_, rl:rl + w_]
                ox, oy = sx + rl, sy + rt
                if screen_bgr.size == 0:
                    return ActionResult(False, index, atype,
                                        f"search_region {region} 超出螢幕範圍、裁出空白影像")
            import asyncio as _aio
            try:
                _lang = action.get("lang_tag") or "zh-Hant-TW"
                try:
                    _words = _aio.run(_ocr_recognize(screen_bgr, _lang))
                except RuntimeError as _re:
                    # 已有 event loop 的 thread 不能用 asyncio.run(沿用 ocr.py 的處理)
                    if "running event loop" not in str(_re).lower():
                        raise
                    _lp = _aio.new_event_loop()
                    try:
                        _words = _lp.run_until_complete(_ocr_recognize(screen_bgr, _lang))
                    finally:
                        _lp.close()
            except Exception as _e:
                return ActionResult(False, index, atype, f"OCR 失敗：{_e}")
            # 換回絕對桌面座標(read_field 只用相對關係,但回傳的 box 要對得上畫面)
            for _w in _words:
                _w["x"] += ox
                _w["y"] += oy

            kind = (action.get("kind") or "amount").lower()
            vre = {"amount": AMOUNT_RE, "ident": IDENT_RE,
                   "taxid": TAXID_RE}.get(kind)  # any → None
            hit = read_field(_words, label,
                             direction=(action.get("direction") or "right"),
                             value_re=vre,
                             max_gap=int(action.get("max_gap", 600)))
            if not hit:
                # 抓不到就誠實失敗 —— 金額/單號填錯比填不到嚴重得多,不可猜
                return ActionResult(False, index, atype,
                                    f"找不到標籤「{label}」旁邊的值"
                                    f"（方向={action.get('direction') or 'right'}、格式={kind}、"
                                    f"OCR 共讀到 {len(_words)} 個詞）")
            msg = (f"讀到 {hit['value']!r} → 變數 {save_as}"
                   f"（標籤讀成「{hit['label_text']}」分數 {hit['label_score']}、"
                   f"方向 {hit['direction']}）")
            return ActionResult(True, index, atype, msg, saved_var=(save_as, hit["value"]))

        elif atype == "assert_text":
            # OCR 版本的 assert：驗證螢幕上應該有某段文字。
            # 常見用途：登入成功後檢查「歡迎回來」、錯誤訊息檢查、狀態列文字等。
            text = (action.get("text") or action.get("ocr_text") or "").strip()
            if not text:
                return ActionResult(False, index, atype, "assert_text 缺 text 欄位")
            find_text_on_screen = None
            try:
                from pipeline.ocr import find_text_on_screen
            except Exception:
                try:
                    from .ocr import find_text_on_screen  # type: ignore
                except Exception as _e:
                    return ActionResult(False, index, atype, f"無法載入 OCR 模組：{_e}")
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("ocr_threshold") or ocr_threshold)
            # 沿用既有 ocr_box_* 欄位做 OCR 搜尋範圍（藍框）
            _box_w = int(action.get("ocr_box_width", 0) or 0)
            _box_h = int(action.get("ocr_box_height", 0) or 0)
            region = None
            if _box_w > 0 and _box_h > 0:
                region = (
                    int(action.get("ocr_box_left", 0) or 0),
                    int(action.get("ocr_box_top", 0) or 0),
                    _box_w, _box_h,
                )
            deadline = time.time() + timeout
            last_reason = ""
            found_ocr = None
            while True:
                _check_abort(run_id)
                screen_bgr, sx, sy = _capture_screen()
                ocr_res = find_text_on_screen(
                    screen_bgr, text, origin_x=sx, origin_y=sy,
                    lang_tag="zh-Hant-TW",
                    threshold=threshold, region=region,
                    strict_region=bool(action.get("ocr_strict_region", False)),
                )
                if ocr_res.found:
                    found_ocr = ocr_res
                    break
                last_reason = ocr_res.reason
                if time.time() >= deadline:
                    break
                time.sleep(0.3)
            if found_ocr is None:
                return ActionResult(False, index, atype,
                    f"assert 失敗：{timeout}s 內未偵測到文字 '{text}'（{last_reason}）")
            msg = (f"assert 通過：文字 '{text}' 可見 @ {found_ocr.center} "
                   f"(matched='{found_ocr.text[:30]}', conf={found_ocr.confidence:.2f})")

        elif atype == "for_each":
            # 清單逐筆迴圈:每輪把當前項存進 save_as 變數、跑一遍 do[] 子動作。
            # 典型:5 個品規逐筆「填值→匯出→等查詢→分歧」,不用手動改五次。
            # items 支援:YAML 清單、逗號/換行分隔字串、{{變數}}(含從畫面讀下來的清單)。
            raw_items = action.get("items")
            sub = None
            if isinstance(raw_items, str):
                from .uia_executor import _substitute_vars as _sub2
                sub = _sub2(raw_items, step_variables)
                # 先按換行切,整段沒換行再按逗號/頓號/分號切
                parts = [ln for ln in sub.splitlines() if ln.strip()]
                if len(parts) <= 1:
                    import re as _re2
                    parts = _re2.split(r"[,、;，；]", sub)
                items = [p.strip() for p in parts if p.strip()]
            elif isinstance(raw_items, list):
                items = [str(x).strip() for x in raw_items if str(x).strip()]
            else:
                return ActionResult(False, index, atype,
                    "for_each 缺 items(清單、或逗號/換行分隔的字串、或 {{變數}})")
            if not items:
                return ActionResult(False, index, atype,
                    f"for_each 的 items 解析後是空清單(原值 {raw_items!r})")
            if len(items) > 500:
                return ActionResult(False, index, atype,
                    f"for_each 一次最多 500 筆(拿到 {len(items)} 筆),清單來源可能抓錯")
            var_name = (action.get("save_as") or "").strip()
            if not var_name:
                return ActionResult(False, index, atype,
                    "for_each 缺 save_as(每輪的當前值要存進哪個變數,子動作用 {{變數}} 取用)")
            # split_as:一筆多欄位。「2026-08」這種一筆兩值的清單(年+月、跨年時
            # 年份不同)按 split_sep 拆開、分別存進多個變數 —— 月份也能當外層迴圈,
            # 不用把整段動作複製兩份。名稱用 | 分隔;最後一個欄位吃剩餘部分
            # (品規等內容本身含分隔符時不會被切爛)。
            split_names = [s.strip() for s in str(action.get("split_as") or "").split("|") if s.strip()]
            split_sep = str(action.get("split_sep") or "-") or "-"
            do_list = action.get("do") or []
            if not do_list:
                return ActionResult(False, index, atype, "for_each 缺 do:(每輪要執行的子動作)")
            cont = bool(action.get("continue_on_error", False))
            done_n = 0
            fail_msgs = []
            for it_i, item in enumerate(items):
                _check_abort(run_id)
                step_variables[var_name] = item
                step_variables[var_name + "_序號"] = str(it_i + 1)
                if split_names:
                    parts = [p.strip() for p in item.split(split_sep, len(split_names) - 1)]
                    if len(parts) < len(split_names):
                        msg_bad = (f"第 {it_i+1} 筆({item})拆不出 {len(split_names)} 個欄位"
                                   f"(分隔符「{split_sep}」、拆到 {len(parts)} 段)")
                        if not cont:
                            return ActionResult(False, index, atype, "for_each 中斷於" + msg_bad)
                        fail_msgs.append(msg_bad)
                        logger.warning(f"[computer_use] {indent}  ⚠ {msg_bad}(跳下一筆)")
                        continue
                    for nm, val in zip(split_names, parts):
                        step_variables[nm] = val
                logger.info(f"[computer_use] {indent}  ── for_each 第 {it_i+1}/{len(items)} 筆:"
                            f"{var_name}={item!r} ──")
                item_failed = None
                for sub_i, sub_action in enumerate(do_list):
                    if not isinstance(sub_action, dict):
                        return ActionResult(False, index, atype,
                            f"do[{sub_i}] 不是 dict，YAML 格式錯誤")
                    sub_res = execute_action(
                        sub_action, assets_dir, sub_i, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    if getattr(sub_res, "saved_var", None):
                        step_variables[sub_res.saved_var[0]] = sub_res.saved_var[1]
                    if not sub_res.ok:
                        item_failed = (f"第 {it_i+1} 筆({item})的 do[{sub_i+1}] "
                                       f"({sub_res.action_type}) 失敗:{sub_res.message}")
                        break
                if item_failed:
                    if not cont:
                        return ActionResult(False, index, atype,
                            f"for_each 中斷於{item_failed}"
                            "(要失敗跳下一筆繼續,設 continue_on_error: true)")
                    fail_msgs.append(item_failed)
                    logger.warning(f"[computer_use] {indent}  ⚠ {item_failed}(continue_on_error,跳下一筆)")
                else:
                    done_n += 1
            if done_n == 0:
                # 全部筆都失敗 —— 就算 continue_on_error 也不能回報成功,
                # 不然「6/6 成功」底下藏著 0/3 完成(實測誤導過使用者)
                return ActionResult(False, index, atype,
                    f"for_each 全部 {len(items)} 筆都失敗:" +
                    "; ".join(m[:100] for m in fail_msgs[:3]))
            msg = f"for_each 完成 {done_n}/{len(items)} 筆"
            if fail_msgs:
                msg += f"(失敗 {len(fail_msgs)} 筆:" + "; ".join(m[:80] for m in fail_msgs[:3]) + ")"
                # 部分失敗且 continue_on_error:整體算成功、訊息誠實列出
            # 迴圈變數留著(最後一筆的值),後續動作還能引用

        elif atype == "wait_text":
            # OCR 版等待:等畫面出現/消失某段文字。UIA 讀不到的畫面(Canvas 繪製、
            # 遠端桌面、影像串流)才用這個 —— 能用 uia_wait 就用 uia_wait(快且準)。
            text = (action.get("text") or "").strip()
            if not text:
                return ActionResult(False, index, atype, "wait_text 缺 text 欄位")
            _u = action.get("until")
            until = _u.strip() if isinstance(_u, str) and _u.strip() else "appear"
            if until not in ("appear", "disappear"):
                return ActionResult(False, index, atype,
                    f"wait_text 不認得 until={until!r}(可用:appear/disappear)")
            try:
                from .ocr import find_text_on_screen
            except Exception as _e:
                return ActionResult(False, index, atype, f"無法載入 OCR 模組:{_e}")
            timeout = float(action.get("timeout_sec", 60))
            threshold = float(action.get("ocr_threshold") or ocr_threshold)
            region_rect = _parse_search_region(action)
            if region_rect is None:
                # 預設把 OCR 限縮在目標視窗範圍 —— 全螢幕掃會把終端機/編輯器裡
                # 剛好含同字樣的文字也算進去(實測:螢幕上開著含 YAML 原始碼的視窗,
                # 「資料處理中」永遠讀得到、等消失等到超時)。
                _wp = (action.get("window") or uia_window or "").strip()
                if _wp:
                    try:
                        from .uia_executor import _get_auto, _resolve_window
                        _a = _get_auto()
                        _w = _resolve_window(_a, {}, _wp)
                        if _w.Exists(1, 0.3):
                            _r = _w.BoundingRectangle
                            region_rect = (_r.left, _r.top,
                                           _r.right - _r.left, _r.bottom - _r.top)
                    except Exception:
                        pass
            deadline = time.time() + timeout
            t_w0 = time.time()
            last_reason = ""
            while time.time() < deadline:
                _check_abort(run_id)
                screen_bgr, sx, sy = _capture_screen()
                ocr_res = find_text_on_screen(
                    screen_bgr, text, origin_x=sx, origin_y=sy,
                    lang_tag="zh-Hant-TW", threshold=threshold, region=region_rect)
                if until == "appear" and ocr_res.found:
                    msg = f"文字「{text}」出現、等了 {time.time()-t_w0:.1f}s"
                    break
                if until == "disappear" and not ocr_res.found:
                    msg = f"文字「{text}」已消失、等了 {time.time()-t_w0:.1f}s"
                    break
                last_reason = getattr(ocr_res, "reason", "")
                time.sleep(0.5)
            else:
                verb = "仍未出現" if until == "appear" else "仍在畫面上"
                return ActionResult(False, index, atype,
                    f"等了 {timeout:.0f}s 文字「{text}」{verb}"
                    + (f"（{last_reason}）" if last_reason else ""))

        elif atype == "if_text_found":
            # OCR 版條件分支:畫面上讀得到某段文字 → then[]、讀不到 → else[]。
            # UIA 抓不到的對話框/畫面用這個;讀不到不算失敗、走 else。
            text = (action.get("text") or "").strip()
            if not text:
                return ActionResult(False, index, atype, "if_text_found 缺 text 欄位")
            try:
                from .ocr import find_text_on_screen
            except Exception as _e:
                return ActionResult(False, index, atype, f"無法載入 OCR 模組:{_e}")
            timeout = float(action.get("timeout_sec", 3.0))
            threshold = float(action.get("ocr_threshold") or ocr_threshold)
            region_rect = _parse_search_region(action)
            if region_rect is None:
                # 預設把 OCR 限縮在目標視窗範圍 —— 全螢幕掃會把終端機/編輯器裡
                # 剛好含同字樣的文字也算進去(實測:螢幕上開著含 YAML 原始碼的視窗,
                # 「資料處理中」永遠讀得到、等消失等到超時)。
                _wp = (action.get("window") or uia_window or "").strip()
                if _wp:
                    try:
                        from .uia_executor import _get_auto, _resolve_window
                        _a = _get_auto()
                        _w = _resolve_window(_a, {}, _wp)
                        if _w.Exists(1, 0.3):
                            _r = _w.BoundingRectangle
                            region_rect = (_r.left, _r.top,
                                           _r.right - _r.left, _r.bottom - _r.top)
                    except Exception:
                        pass
            deadline = time.time() + timeout
            found = False
            while True:
                _check_abort(run_id)
                screen_bgr, sx, sy = _capture_screen()
                ocr_res = find_text_on_screen(
                    screen_bgr, text, origin_x=sx, origin_y=sy,
                    lang_tag="zh-Hant-TW", threshold=threshold, region=region_rect)
                if ocr_res.found:
                    found = True
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.4)
            if found:
                time.sleep(0.5)   # 同 if_element_found:對話框開場動畫吃點擊
            branch = action.get("then", []) if found else action.get("else", [])
            branch = branch or []
            branch_label = "then" if found else "else"
            logger.info(f"[computer_use] {indent}  → 文字「{text}」"
                        f"{'讀得到' if found else '讀不到'} → 走 {branch_label} 分支"
                        f"（{len(branch)} 個子動作）")
            for sub_i, sub_action in enumerate(branch):
                if not isinstance(sub_action, dict):
                    return ActionResult(False, index, atype,
                        f"{branch_label}[{sub_i}] 不是 dict，YAML 格式錯誤")
                sub_res = execute_action(
                    sub_action, assets_dir, sub_i, logger, run_id,
                    _depth=_depth + 1, **_exec_ctx,
                )
                if getattr(sub_res, "saved_var", None):
                    step_variables[sub_res.saved_var[0]] = sub_res.saved_var[1]
                if not sub_res.ok:
                    return ActionResult(False, index, atype,
                        f"if_text_found/{branch_label}[{sub_i+1}] "
                        f"({sub_res.action_type}) 失敗：{sub_res.message}")
            msg = (f"if 文字「{text}」{'讀得到' if found else '讀不到'} → 執行 {branch_label}"
                   f"（{len(branch)} 個子動作皆 OK）")

        elif atype == "if_image_found":
            # 條件分支：根據錨點圖是否可見，選擇執行 then[] 或 else[]
            # 不叫 LLM、不燒 token — 純 CV template matching（跟 click_image 同一套）
            # 常見用途：
            #   1. 處理偶爾跳出的對話框（密碼過期、更新提示、網路錯誤）
            #   2. 登入狀態判斷（session 在 / 過期 兩種畫面）
            img_name = action.get("image", "")
            if not img_name:
                return ActionResult(False, index, atype, "if_image_found 缺 image 欄位")
            tpl_path = assets_dir / img_name
            timeout = float(action.get("timeout_sec", 2.0))
            threshold = float(action.get("confidence") or cv_threshold)
            region_rect = _parse_search_region(action)

            # 在 timeout 內等錨點出現；可能 0.3s 就找到、也可能等到 deadline
            deadline = time.time() + timeout
            found = False
            best_conf = 0.0
            while True:
                _check_abort(run_id)
                m = find_template(str(tpl_path), threshold=threshold, multi_scale=True,
                                  region=region_rect)
                if m.found:
                    found = True
                    best_conf = m.confidence
                    break
                best_conf = max(best_conf, m.confidence)
                if time.time() >= deadline:
                    break
                time.sleep(0.2)

            branch = action.get("then", []) if found else action.get("else", [])
            branch = branch or []
            branch_label = "then" if found else "else"
            logger.info(f"[computer_use] {indent}  → {img_name} "
                        f"{'found' if found else 'not found'} (conf={best_conf:.2f}) "
                        f"→ 走 {branch_label} 分支（{len(branch)} 個子動作）")

            for sub_i, sub_action in enumerate(branch):
                if not isinstance(sub_action, dict):
                    return ActionResult(False, index, atype,
                        f"{branch_label}[{sub_i}] 不是 dict，YAML 格式錯誤")
                sub_res = execute_action(
                    sub_action, assets_dir, sub_i, logger, run_id,
                    _depth=_depth + 1, **_exec_ctx,
                )
                # 巢狀動作(ocr_get_text 等)存的變數要收回父層 —— 不收的話後面
                # {{變數}} 找不到值,會把字面字元填進欄位(靜默錯誤)
                if getattr(sub_res, "saved_var", None):
                    step_variables[sub_res.saved_var[0]] = sub_res.saved_var[1]
                if not sub_res.ok:
                    return ActionResult(False, index, atype,
                        f"if_image_found/{branch_label}[{sub_i+1}] "
                        f"({sub_res.action_type}) 失敗：{sub_res.message}")
            msg = (f"if {img_name}: {'match' if found else 'no-match'} "
                   f"→ 執行 {branch_label}（{len(branch)} 個子動作皆 OK）")

        elif atype == "if_element_found":
            # UIA 版條件分支:目標元素在畫面上 → then[]、不在 → else[]。
            # 典型:按匯出後「查無資料」對話框跳出來就按確定,否則等下載完成。
            # 與 if_image_found 同框架,但比對 UIA 結構 —— 不用截錨點圖、漂移免疫。
            cdef = action.get("control") or {}
            if not cdef:
                return ActionResult(False, index, atype, "if_element_found 缺 control 欄位")
            timeout = float(action.get("timeout_sec", 3.0))
            from .uia_executor import element_exists
            found = element_exists(action.get("window") or uia_window, cdef,
                                   rect=action.get("rect"), timeout=timeout)
            if found:
                # 剛彈出的對話框有開場動畫,立刻點擊會被吃掉(回報成功卻沒生效,
                # 實測踩過) —— 偵測到就先讓它站穩再跑 then 分支。
                time.sleep(1.0)
            branch = action.get("then", []) if found else action.get("else", [])
            branch = branch or []
            branch_label = "then" if found else "else"
            logger.info(f"[computer_use] {indent}  → 元素 {cdef} "
                        f"{'在' if found else '不在'} → 走 {branch_label} 分支"
                        f"（{len(branch)} 個子動作）")
            def _run_branch():
                for sub_i, sub_action in enumerate(branch):
                    if not isinstance(sub_action, dict):
                        return (f"{branch_label}[{sub_i}] 不是 dict，YAML 格式錯誤")
                    sub_res = execute_action(
                        sub_action, assets_dir, sub_i, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    if getattr(sub_res, "saved_var", None):
                        step_variables[sub_res.saved_var[0]] = sub_res.saved_var[1]
                    if not sub_res.ok:
                        return (f"if_element_found/{branch_label}[{sub_i+1}] "
                                f"({sub_res.action_type}) 失敗：{sub_res.message}")
                return None

            fail_msg = _run_branch()
            if fail_msg and branch_label == "then":
                # 對話框剛彈出的那幾秒,Chromium 的子樹會抖:探測看得到、
                # 緊接著的點擊卻找不到(實測,單獨重跑同一動作又成功)。
                # then 分支失敗等 1 秒重試一次,吸收這種瞬時抖動。
                logger.warning(f"[computer_use] {indent}  then 分支失敗,等 1s 重試一次:{fail_msg[:80]}")
                time.sleep(1.0)
                fail_msg = _run_branch()
            if fail_msg:
                return ActionResult(False, index, atype, fail_msg)
            msg = (f"if 元素{'在' if found else '不在'} → 執行 {branch_label}"
                   f"（{len(branch)} 個子動作皆 OK）")

        elif atype == "wait_download":
            # 等瀏覽器下載完成:資料夾出現「新的、寫完的」檔案。
            # 完成的判斷:檔名對上 pattern、不是 .crdownload/.tmp/.partial 半成品、
            # 沒有同名半成品還在寫、且大小連續兩次輪詢沒變(還在長大 = 還在寫)。
            # save_as 存完整路徑,後續步驟可拿去搬檔/開檔/寄信。
            import glob as _glob
            from .uia_executor import _substitute_vars as _sub
            dl_dir = _sub(action.get("dir") or "", step_variables) or str(Path.home() / "Downloads")
            pattern = _sub(action.get("pattern") or "*", step_variables)
            timeout = float(action.get("timeout_sec", 300))
            if not Path(dl_dir).is_dir():
                return ActionResult(False, index, atype, f"下載資料夾不存在:{dl_dir}")
            t_begin = time.time()
            # 「新檔案」= 步驟開始之後出現的(mtime 判斷)。不能用本動作開始時間當
            # 基準:下載在「按匯出」當下就開始,等對話框探測完輪到本動作時,檔案
            # 可能已經寫完 —— 會被誤判成「本來就在」而空等到超時(實測踩過)。
            step_t0 = float(step_variables.get("__cu_step_t0__") or (t_begin - 30))
            claimed = step_variables.setdefault("__cu_downloads_claimed__", [])
            last_size: dict = {}
            while time.time() - t_begin < timeout:
                _check_abort(run_id)
                def _fresh(p):
                    try:
                        return Path(p).stat().st_mtime >= step_t0 - 2
                    except OSError:
                        return False
                cands = [p for p in _glob.glob(str(Path(dl_dir) / pattern))
                         if p not in claimed
                         and not p.endswith((".crdownload", ".tmp", ".partial"))
                         and _fresh(p)]
                if cands:
                    newest = max(cands, key=lambda p: Path(p).stat().st_mtime)
                    still_writing = Path(newest + ".crdownload").exists()
                    try:
                        sz = Path(newest).stat().st_size
                    except OSError:
                        sz = -1
                    if not still_writing and sz >= 0 and last_size.get(newest) == sz:
                        claimed.append(newest)   # 同步驟第二個 wait_download 不會重複認領
                        elapsed = time.time() - t_begin
                        # save_as 名稱也做變數替換 —— for_each 裡「下載檔{{品規_序號}}」
                        # 才能每輪存到不同變數
                        save_as = _sub((action.get("save_as") or ""), step_variables).strip()
                        _msg = f"下載完成:{Path(newest).name}({sz} bytes、等了 {elapsed:.1f}s)"
                        return ActionResult(True, index, atype, _msg,
                                            duration_ms=int((time.time() - t0) * 1000),
                                            saved_var=(save_as, newest) if save_as else None)
                    last_size[newest] = sz
                time.sleep(0.6)
            return ActionResult(False, index, atype,
                f"等了 {timeout:.0f}s 沒有符合「{pattern}」的新檔案下載完成"
                f"(資料夾:{dl_dir})。下載比預期久的話調大 timeout_sec")

        elif atype == "retry_until":
            # 重複動作直到條件滿足：按鈕沒反應再按一次、網路抖動後重試
            # do[]  = 每輪要執行的動作清單
            # until = 檢查是否完成的單一動作（建議 wait_image / assert_image / assert_text）
            do_list = action.get("do", []) or []
            until_action = action.get("until", None)
            if not do_list:
                return ActionResult(False, index, atype, "retry_until 缺 do: 動作清單")
            if until_action is None or not isinstance(until_action, dict):
                return ActionResult(False, index, atype,
                    "retry_until 缺 until: 檢查條件（必須是單一動作 dict）")
            max_attempts = int(action.get("max_attempts", 3) or 3)
            wait_between = float(action.get("wait_between_sec", 1.0) or 1.0)
            if max_attempts < 1:
                max_attempts = 1

            last_fail_reason = ""
            success = False
            for attempt in range(1, max_attempts + 1):
                _check_abort(run_id)
                logger.info(f"[computer_use] {indent}  retry_until 第 {attempt}/{max_attempts} 輪")
                # 1. 跑 do[] 裡所有動作
                attempt_do_ok = True
                for sub_i, sub_a in enumerate(do_list):
                    if not isinstance(sub_a, dict):
                        return ActionResult(False, index, atype,
                            f"do[{sub_i}] 不是 dict，YAML 格式錯誤")
                    sub_res = execute_action(
                        sub_a, assets_dir, sub_i, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    # 巢狀動作(ocr_get_text 等)存的變數要收回父層 —— 不收的話後面
                    # {{變數}} 找不到值,會把字面字元填進欄位(靜默錯誤)
                    if getattr(sub_res, "saved_var", None):
                        step_variables[sub_res.saved_var[0]] = sub_res.saved_var[1]
                    if not sub_res.ok:
                        attempt_do_ok = False
                        last_fail_reason = (f"第 {attempt} 輪 do[{sub_i+1}] "
                                            f"({sub_res.action_type}) 失敗：{sub_res.message}")
                        logger.info(f"[computer_use] {indent}    {last_fail_reason[:160]}")
                        break
                # 2. 跑 until 檢查
                if attempt_do_ok:
                    until_res = execute_action(
                        until_action, assets_dir, 0, logger, run_id,
                        _depth=_depth + 1, **_exec_ctx,
                    )
                    # 巢狀動作(ocr_get_text 等)存的變數要收回父層 —— 不收的話後面
                    # {{變數}} 找不到值,會把字面字元填進欄位(靜默錯誤)
                    if getattr(until_res, "saved_var", None):
                        step_variables[until_res.saved_var[0]] = until_res.saved_var[1]
                    if until_res.ok:
                        success = True
                        msg = f"retry_until 成功於第 {attempt}/{max_attempts} 輪（{until_res.message[:80]}）"
                        break
                    last_fail_reason = f"第 {attempt} 輪 until 未通過：{until_res.message}"
                    logger.info(f"[computer_use] {indent}    {last_fail_reason[:160]}")
                # 3. 還有輪次就等一下再重試
                if attempt < max_attempts:
                    # sleep 分段好讓 abort 能及時生效
                    remaining = wait_between
                    while remaining > 0:
                        _check_abort(run_id)
                        chunk = min(0.3, remaining)
                        time.sleep(chunk)
                        remaining -= chunk

            if not success:
                return ActionResult(False, index, atype,
                    f"retry_until {max_attempts} 輪仍未成功：{last_fail_reason}")

        elif atype == "vlm_check":
            # 純判斷不點擊：把當下畫面送 Settings 主模型（必須支援視覺）
            # 用途：登入後確認成功訊息、確認對話框出現、檢查表單填好等
            # pass=false 步驟即失敗，VLM 寫的 reason 會出現在錯誤訊息中
            prompt = (action.get("vlm_prompt") or action.get("description") or "").strip()
            if not prompt:
                return ActionResult(False, index, atype,
                    "vlm_check 缺 vlm_prompt（判斷條件必填）")
            region_rect = _parse_search_region(action)
            passed, reason = _vlm_judge_screen(prompt, region_rect, logger)
            if not passed:
                return ActionResult(False, index, atype,
                    f"VLM 判斷未通過：{reason}")
            msg = f"VLM 判斷通過：{reason[:120]}"

        elif atype == "screenshot":
            import cv2
            img, _ox, _oy = _capture_screen()
            ts = int(time.time())
            out = assets_dir / f"debug_screenshot_{ts}.png"
            # 用 imencode + write_bytes 避免中文路徑問題
            ok, buf = cv2.imencode(".png", img)
            if ok:
                out.write_bytes(buf.tobytes())
                msg = f"已存 screenshot：{out.name}"
            else:
                msg = "screenshot imencode 失敗"

        else:
            return ActionResult(False, index, atype, f"未知動作類型：{atype}")

        duration = int((time.time() - t0) * 1000)
        logger.info(f"[computer_use]   ✓ {msg}（{duration}ms）")
        return ActionResult(True, index, atype, msg, duration)

    except RuntimeError as e:
        # abort signal
        raise
    except Exception as e:
        # pyautogui.FailSafeException / 其他意外
        import traceback
        logger.error(f"[computer_use]   ✗ {atype} 失敗：{e}")
        logger.debug(traceback.format_exc())
        return ActionResult(False, index, atype, f"{type(e).__name__}: {e}",
                            int((time.time() - t0) * 1000))


# ── 對外入口：執行一整個 computer_use 步驟 ─────────────────────────

@dataclass
class StepResult:
    success: bool
    total_actions: int
    succeeded: int
    failed_at: int = -1        # 首次失敗的 index；-1 = 全部成功
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    # UIA / Computer Use 透過 save_as 累積的步驟變數(uia_get_text 等存入)。
    # runner 拿到後寫進 PipelineRun.step_results[i].step_vars,後續 step 可用
    # `{{ steps.<name>.output.<key> }}` 引用。
    step_variables: dict = field(default_factory=dict)


MAX_ACTIONS_PER_STEP = 500  # 單步動作數上限，防止失控腳本無限循環


def validate_action_assets(actions: list[dict], assets_dir: Path) -> list[str]:
    """Preflight：掃一遍 actions 裡引用到的所有錨點圖是否存在（含巢狀 then/else/do/until）。
    提早 FAIL 比回放跑到一半才發現圖不見好太多，也讓使用者錯誤訊息更集中。
    回傳缺失檔名 list（保留順序、去重）。"""
    missing: list[str] = []
    seen: set[str] = set()

    def _scan(acts: list) -> None:
        for a in acts:
            if not isinstance(a, dict):
                continue
            for key in ("image", "image2"):
                name = a.get(key) or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                if not (assets_dir / name).is_file():
                    missing.append(name)
            # vlm_mode=anchor_pick 用的多張候選錨點圖也要檢查
            for name in (list(a.get("image_variants") or [])
                         + list(a.get("vlm_anchors") or [])):
                if not name or name in seen:
                    continue
                seen.add(name)
                if not (assets_dir / name).is_file():
                    missing.append(name)
            # 遞迴掃 if_image_found / retry_until 的巢狀動作
            for sub_key in ("then", "else", "do"):
                sub = a.get(sub_key)
                if isinstance(sub, list):
                    _scan(sub)
            until_a = a.get("until")
            if isinstance(until_a, dict):
                _scan([until_a])

    _scan(actions)
    return missing


def _screen_layout_match(meta_path: Path, logger: logging.Logger) -> bool:
    """比對錄製時與回放時的螢幕解析度。
    True = 一致（絕對座標 fallback 仍可靠）；False = 已改變（座標 fallback 不可信，應禁用）"""
    if not meta_path.is_file():
        return True  # 沒 meta 就寬容處理
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rec_w, rec_h = meta.get("screen_width"), meta.get("screen_height")
        if not rec_w or not rec_h:
            return True
        import mss
        with mss.mss() as sct:
            cur = sct.monitors[1]
        if cur["width"] == rec_w and cur["height"] == rec_h:
            return True
        logger.warning(
            f"[computer_use] ⚠ 螢幕解析度變了："
            f"錄製 {rec_w}×{rec_h} → 目前 {cur['width']}×{cur['height']}；"
            f"將禁用絕對座標 fallback，強制圖像比對（常見於接/拔外接螢幕後）"
        )
        return False
    except Exception as e:
        logger.warning(f"[computer_use] 讀 meta.json 失敗：{e}")
        return True


def execute_computer_use_step(
    actions: list[dict],
    assets_dir: str,
    logger: logging.Logger,
    run_id: Optional[str] = None,
    fail_fast: bool = True,
    cv_threshold: float = 0.5,
    cv_search_only_near: bool = False,
    cv_search_radius: int = 400,
    cv_trigger_hover: bool = True,
    cv_hover_wait_ms: int = 200,
    cv_coord_fallback: bool = False,
    ocr_threshold: float = 0.6,
    ocr_cv_fallback: bool = False,
    # VLM 把關 Phase 1 參數(預設 off、不影響舊行為):
    cu_vlm_check_strategy: str = "off",     # off / after_each / critical_only
    cu_on_mismatch: str = "stop_notify",    # stop_notify / retry_once / skip_and_continue
    cu_vlm_max_retries: int = 1,
    # UIA 模式相關(action.type 是 uia_* 時用)
    uia_window: str = "",                   # 視窗 title pattern(可含 *)、空字串 = foreground
    llm_role: str = "primary",              # VLM 把關用主/副模型
) -> StepResult:
    """執行一整個 computer_use 步驟。

    - actions: ComputerUseAction 物件的 list of dict
    - assets_dir: 錨點圖片資料夾（絕對路徑，通常是 ai_output/<name>/ 下的子資料夾）
    - fail_fast: True 則遇到失敗立刻中止；False 則繼續但記錄失敗數
    - cv_threshold: CV 比對門檻（0.50 寬鬆 / 0.80 標準 / 0.90 嚴格）
    - cv_search_only_near: True = 只搜錄製座標附近、找不到直接 FAIL（不退回全螢幕也不退回座標）
    - cv_search_radius: 附近搜尋半徑（像素）；實際搜尋範圍 (2r × 2r)
    - cv_trigger_hover: True = 比對前先 moveTo(錄製座標) + 200ms 讓 Windows hover 效果出現
    """
    import json  # 供 _screen_layout_match 讀 meta.json
    clear_abort(run_id or "")
    # ── ESC × 2 緊急中止 watcher(Windows only)──
    # 比 FAILSAFE 滑鼠甩到 (0,0) 更直覺;非 Windows 自動 no-op、FAILSAFE 仍生效當備援
    if run_id:
        _ensure_esc_watcher(run_id, logger)
    if len(actions) > MAX_ACTIONS_PER_STEP:
        return StepResult(
            success=False,
            total_actions=len(actions),
            succeeded=0,
            failed_at=-1,
            stdout="",
            stderr=f"動作數 {len(actions)} 超過安全上限 {MAX_ACTIONS_PER_STEP}，拒絕執行",
            exit_code=2,
        )
    assets = Path(assets_dir)
    if not assets.is_dir():
        # 純 UIA 節點(Inspector 建的)本來就沒有錨點資料夾 —— 只在動作真的
        # 用到圖(image 欄位,含巢狀)時才警告,不然每次執行都喊一聲是噪音。
        def _uses_images(acts) -> bool:
            for a in acts or []:
                if not isinstance(a, dict):
                    continue
                if a.get("image") or a.get("image2") or a.get("full_image"):
                    return True
                for nf in ("do", "then", "else"):
                    if _uses_images(a.get(nf)):
                        return True
                u = a.get("until")
                if isinstance(u, dict) and _uses_images([u]):
                    return True
            return False
        if _uses_images(actions):
            logger.warning(f"[computer_use] assets 目錄不存在：{assets_dir}"
                           f"（動作裡有用到錨點圖,執行到那些動作會失敗）")
    else:
        # 錨點圖 preflight：避免跑到一半才發現圖不見
        missing_imgs = validate_action_assets(actions, assets)
        if missing_imgs:
            preview = ", ".join(missing_imgs[:5])
            more = f"...（共 {len(missing_imgs)} 張）" if len(missing_imgs) > 5 else ""
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=0,
                failed_at=0,
                stdout="",
                stderr=f"preflight 失敗：assets_dir 缺少錨點圖：{preview}{more}",
                exit_code=2,
            )

    # 螢幕解析度比對：若改變（接/拔外接螢幕）就禁用座標 fallback
    layout_ok = _screen_layout_match(assets / "meta.json", logger) if assets.is_dir() else True

    logger.info(f"[computer_use] ▶ 開始執行 {len(actions)} 個動作 "
                f"（assets: {assets_dir}, fail_fast={fail_fast}）")
    logger.info(f"[computer_use] 🛡 Safety: 滑鼠移到螢幕左上角 (0,0) 可立即中止")

    succeeded = 0
    failed_at = -1
    messages: list[str] = []
    # 跨 action 變數儲存(uia_get_text / uia_get_table_rowcount 用 save_as 存的)
    # 後續 action 內 {{var_name}} 替換靠這個
    step_variables: dict[str, Any] = {}
    # wait_download 的「新檔案」基準:下載在「按匯出」當下就開始,但 wait_download
    # 常排在對話框探測之後才輪到 —— 以動作自身的開始時間當基準會把已落地的檔案
    # 誤判成「本來就在」。所以基準用整個步驟的開始時間。
    step_variables["__cu_step_t0__"] = time.time()

    # VLM 把關 Phase 1 開關:strategy != off 才啟動驗證 loop;否則整段邏輯跳過、不影響舊行為
    _vlm_active = (cu_vlm_check_strategy or "off").lower() != "off"
    if _vlm_active:
        logger.info(f"[computer_use] 🛡 VLM 把關啟用 strategy={cu_vlm_check_strategy} on_mismatch={cu_on_mismatch}")

    def _should_verify(action: dict) -> bool:
        """依 strategy + action 欄位決定是否要送 VLM 驗。"""
        if not _vlm_active:
            return False
        expected = (action.get("expected") or "").strip()
        if not expected:
            return False
        if cu_vlm_check_strategy == "after_each":
            return True
        if cu_vlm_check_strategy == "critical_only":
            return bool(action.get("verify_critical", False))
        return False

    def _capture_to_png(prefix: str, idx: int) -> str:
        """抓螢幕、寫 PNG、回路徑;失敗回空字串。"""
        try:
            import cv2 as _cv
            img, _w, _h = _capture_screen()
            out_path = assets / f"_vlm_{prefix}_{idx:03d}.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ok, buf = _cv.imencode(".png", img)
            if not ok:
                return ""
            out_path.write_bytes(buf.tobytes())
            return str(out_path)
        except Exception as e:
            logger.warning(f"[computer_use] _capture_to_png 失敗 ({prefix}_{idx:03d}): {e}")
            return ""

    def _run_vlm_verify(before: str, after: str, expected: str) -> dict:
        """同步包裝 async verify_action_outcome、給 sync loop 內呼叫。"""
        from .cu_vlm_verifier import verify_action_outcome
        import asyncio as _aio
        try:
            return _aio.run(verify_action_outcome(
                before_path=before,
                after_path=after,
                expected=expected,
                logger=logger,
                timeout_sec=30.0,
                llm_role=llm_role,
            ))
        except RuntimeError as e:
            if "event loop" in str(e).lower():
                _loop = _aio.new_event_loop()
                try:
                    return _loop.run_until_complete(verify_action_outcome(
                        before_path=before, after_path=after,
                        expected=expected, logger=logger, timeout_sec=30.0,
                        llm_role=llm_role,
                    ))
                finally:
                    _loop.close()
            raise

    def _push_mismatch_to_tg(action_idx: int, action: dict, verdict: dict, after_path: str) -> None:
        """VLM 驗證失敗時 push TG 截圖 + verdict、不繞 chat agent(直接走 telegram Bot)。"""
        try:
            from pipeline.runner import _get_tg_token, _get_tg_chat_id
            from telegram import Bot
            import asyncio as _aio2
        except Exception as e:
            logger.debug(f"[computer_use] TG push import 失敗、跳過: {e}")
            return
        token = _get_tg_token()
        chat_id = _get_tg_chat_id()
        if not token or not chat_id:
            logger.debug("[computer_use] TG token / chat_id 未設、跳過 push")
            return

        action_type = action.get("type", "?")
        expected = action.get("expected", "")
        caption = (
            f"❌ Computer use 步驟 {action_idx+1}/{len(actions)} 偏離預期\n\n"
            f"動作: {action_type}\n"
            f"預期: {expected[:200]}\n\n"
            f"VLM 判定: 不符合 ({verdict.get('mismatch_type', '?')})\n"
            f"原因: {verdict.get('reason', '')[:300]}"
        )
        if verdict.get("unexpected"):
            caption += f"\n意外元素: {verdict['unexpected'][:200]}"

        async def _send():
            async with Bot(token=token) as bot:
                if after_path and Path(after_path).exists():
                    with open(after_path, "rb") as f:
                        await bot.send_photo(chat_id=int(chat_id), photo=f, caption=caption[:1024])
                else:
                    await bot.send_message(chat_id=int(chat_id), text=caption)

        try:
            _aio2.run(_send())
        except Exception as e:
            logger.warning(f"[computer_use] TG push 失敗(忽略): {e}")

    for i, action in enumerate(actions):
        do_verify = _should_verify(action)
        before_path = _capture_to_png("before", i) if do_verify else ""

        # 路由:uia_* type 走 uia_executor、其他 type 走原 pixel-based execute_action
        atype = action.get("type", "")
        if atype.startswith("uia_"):
            try:
                from .uia_executor import execute_uia_action
                uia_res = execute_uia_action(action, uia_window, step_variables, logger)
                # 把結果包成 ActionResult 兼容後續流程
                res = ActionResult(
                    ok=uia_res.ok,
                    action_index=i,
                    action_type=atype,
                    message=uia_res.message,
                    duration_ms=0,
                )
                # 收集 save_as 變數
                if uia_res.saved_var:
                    var_name, var_value = uia_res.saved_var
                    step_variables[var_name] = var_value
                    logger.info(f"[computer_use] 變數 {var_name} = {var_value!r:.80}")
            except Exception as e:
                logger.exception(f"[computer_use] uia action {atype} 例外")
                res = ActionResult(ok=False, action_index=i, action_type=atype,
                                   message=f"{type(e).__name__}: {e}")
            messages.append(f"#{i+1} [{res.action_type}] {'OK' if res.ok else 'FAIL'}: {res.message}")
            if res.ok:
                succeeded += 1
            else:
                if failed_at < 0:
                    failed_at = i
                if fail_fast:
                    return StepResult(
                        success=False, total_actions=len(actions),
                        succeeded=succeeded, failed_at=i,
                        stdout="\n".join(messages),
                        stderr=f"動作 #{i+1} ({atype}) 失敗:{res.message}",
                        exit_code=1,
                        step_variables=dict(step_variables),
                    )
            continue  # uia 動作不走 VLM 把關(它本來就讀結構、漂移免疫)

        try:
            res = execute_action(action, assets, i, logger, run_id,
                                 allow_coord_fallback=layout_ok,
                                 cv_threshold=cv_threshold,
                                 cv_search_only_near=cv_search_only_near,
                                 cv_search_radius=cv_search_radius,
                                 cv_trigger_hover=cv_trigger_hover,
                                 cv_hover_wait_ms=cv_hover_wait_ms,
                                 cv_coord_fallback=cv_coord_fallback,
                                 ocr_threshold=ocr_threshold,
                                 ocr_cv_fallback=ocr_cv_fallback,
                                 uia_window=uia_window,
                                 step_variables=step_variables)
        except RuntimeError as abort_err:
            logger.warning(f"[computer_use] {abort_err}")
            return StepResult(
                success=False,
                total_actions=len(actions),
                succeeded=succeeded,
                failed_at=i,
                stdout="\n".join(messages),
                stderr=str(abort_err),
                exit_code=130,  # SIGINT-ish
                step_variables=dict(step_variables),
            )
        messages.append(f"#{i+1} [{res.action_type}] {'OK' if res.ok else 'FAIL'}: {res.message}")

        # 收 save_as 變數(ocr_get_text 等取值動作)。對齊上面 uia 路徑的作法,
        # 讓 pixel 路徑取到的值也能被後續動作用 {{變數}} 引用。
        if getattr(res, "saved_var", None):
            _vn, _vv = res.saved_var
            step_variables[_vn] = _vv
            logger.info(f"[computer_use] 變數 {_vn} = {_vv!r:.80}")

        # VLM 把關:動作沒立即失敗(res.ok)且需驗 → 截後圖 + 送 VLM 比對 expected
        # 動作本身已 fail 走原有 fail 路徑、不浪費 VLM token
        if do_verify and res.ok:
            time.sleep(0.3)  # 給 UI render 時間、太快截圖會抓到動作前狀態
            after_path = _capture_to_png("after", i)
            if not before_path or not after_path:
                logger.warning(f"[computer_use] 動作 #{i+1} VLM 截圖不齊全(before={bool(before_path)}, after={bool(after_path)})、跳過驗證")
            else:
                expected = (action.get("expected") or "").strip()
                _retry_count = 0
                while True:
                    verdict = _run_vlm_verify(before_path, after_path, expected)
                    logger.info(f"[computer_use] 動作 #{i+1} VLM verdict: ok={verdict['ok']} reason={verdict['reason'][:100]}")
                    if verdict["ok"]:
                        break
                    # 不 ok、依 cu_on_mismatch 處理
                    if cu_on_mismatch == "skip_and_continue":
                        logger.warning(f"[computer_use] 動作 #{i+1} VLM 失敗、skip_and_continue 模式繼續: {verdict['reason'][:120]}")
                        messages.append(f"  ⚠ VLM mismatch ignored: {verdict['reason'][:120]}")
                        break
                    if cu_on_mismatch == "retry_once" and _retry_count < cu_vlm_max_retries:
                        _retry_count += 1
                        logger.info(f"[computer_use] 動作 #{i+1} VLM 失敗、retry {_retry_count}/{cu_vlm_max_retries}")
                        # 重執行同動作:不重新截 before、用同 before 比新 after
                        try:
                            res = execute_action(action, assets, i, logger, run_id,
                                                 allow_coord_fallback=layout_ok,
                                                 cv_threshold=cv_threshold,
                                                 cv_search_only_near=cv_search_only_near,
                                                 cv_search_radius=cv_search_radius,
                                                 cv_trigger_hover=cv_trigger_hover,
                                                 cv_hover_wait_ms=cv_hover_wait_ms,
                                                 cv_coord_fallback=cv_coord_fallback,
                                                 ocr_threshold=ocr_threshold,
                                                 ocr_cv_fallback=ocr_cv_fallback,
                                                 step_variables=step_variables)
                        except RuntimeError as abort_err:
                            return StepResult(
                                success=False, total_actions=len(actions),
                                succeeded=succeeded, failed_at=i,
                                stdout="\n".join(messages), stderr=str(abort_err),
                                exit_code=130,
                                step_variables=dict(step_variables),
                            )
                        if not res.ok:
                            messages.append(f"  ⚠ retry {_retry_count} 動作直接失敗: {res.message}")
                            break  # 動作 fail、跳出 retry loop、走下面 res.ok=False 邏輯
                        time.sleep(0.3)
                        after_path = _capture_to_png("after_retry", i)
                        continue
                    # stop_notify(預設)或 retry 用完仍失敗
                    logger.warning(f"[computer_use] 動作 #{i+1} VLM 失敗、stop_notify: {verdict['reason'][:200]}")
                    messages.append(f"  ❌ VLM mismatch (stop_notify): {verdict['reason'][:200]}")
                    _push_mismatch_to_tg(i, action, verdict, after_path)
                    return StepResult(
                        success=False,
                        total_actions=len(actions),
                        succeeded=succeeded,
                        failed_at=i,
                        stdout="\n".join(messages),
                        stderr=f"VLM 把關失敗(動作 #{i+1} {res.action_type}): {verdict['reason'][:200]}",
                        exit_code=2,  # 區別動作 fail (=1) 和 VLM mismatch (=2)
                        step_variables=dict(step_variables),
                    )

        if res.ok:
            succeeded += 1
        else:
            if failed_at < 0:
                failed_at = i
            if fail_fast:
                return StepResult(
                    success=False,
                    total_actions=len(actions),
                    succeeded=succeeded,
                    failed_at=i,
                    stdout="\n".join(messages),
                    stderr=f"動作 #{i + 1} ({res.action_type}) 失敗：{res.message}",
                    exit_code=1,
                    step_variables=dict(step_variables),
                )

    all_ok = (failed_at < 0)
    logger.info(f"[computer_use] ■ 結束：{succeeded}/{len(actions)} 成功")
    return StepResult(
        success=all_ok,
        total_actions=len(actions),
        succeeded=succeeded,
        failed_at=failed_at,
        stdout="\n".join(messages),
        stderr="" if all_ok else f"失敗動作數：{len(actions) - succeeded}",
        exit_code=0 if all_ok else 1,
        step_variables=dict(step_variables),
    )
