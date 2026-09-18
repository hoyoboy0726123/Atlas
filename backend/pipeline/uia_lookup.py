"""UIA 元素定位 helper — 給 computer_use click_image / click_at 的「UIA-first 三層 fallback」用。

錄製時 recorder.py 在 mouse-down 同時抓 UIA element info(name / control_type /
automation_id / window_title)、跟 CV anchor 一起存進 action。回放時這個模組
依錄製資訊在當前螢幕找對應元素、回傳 bounding rect、上層 click 中心點。

策略(由穩到模糊、任一中即回):
  Stage A:視窗標題完全匹配 + AutomationId 找(自家程式有設 ID = 最穩)
          視窗標題完全匹配 + Name + ControlType 找(雙重驗證)
          視窗標題完全匹配 + Name 找(常見情境)
  Stage B:視窗標題模糊匹配(處理動態標題、例 "Notepad - foo.txt" → "Notepad - bar.txt")
          重複 A 的三層搜尋
  Stage C:沒指定視窗標題、用前景視窗
  Stage D:全域 root 搜尋(給 Windows 殼層元件用 — taskbar Start button、
          系統匣、通知中心這些「沒 window title 的 Pane 元素」)
          **只用 AutomationId 全域搜**(Name 全域搜會誤中其他視窗的同名鈕)
          過濾 IsOffscreen=True、避免點到隱藏視窗的元素

任何步驟例外(無 UIA / 視窗關了 / 元素消失)都吞掉、回 None,
讓上層 fall through 到 CV / OCR / 座標下一層。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger(__name__)


def _split_window_variants(title: str) -> list[str]:
    """把視窗標題拆成可能的「穩定子字串」、用來做模糊 regex 匹配。
    例:'Notepad - foo.txt' → ['Notepad', 'foo.txt']
       'Atlas — Microsoft Edge' → ['Atlas', 'Microsoft Edge']
    """
    if not title:
        return []
    # 常見分隔符:- / | — — (en-dash / em-dash)
    parts = re.split(r"\s*[-—|·]\s*", title)
    parts = [p.strip() for p in parts if p.strip()]
    return parts[:3]  # 最多 3 段、避免亂搞


def _try_find(win, ui: dict):
    """在指定 window control 下、用三種策略(automation_id / name+type / name)找元素。
    回傳 uiautomation Control(找到)或 None(沒找到)。"""
    target_name = (ui.get("name") or "").strip()
    target_type = (ui.get("control_type") or "").strip()
    target_auto_id = (ui.get("automation_id") or "").strip()

    # Tier 1: AutomationId
    if target_auto_id:
        try:
            elem = win.Control(AutomationId=target_auto_id)
            if elem.Exists(maxSearchSeconds=0.5):
                return elem
        except Exception:
            pass

    # Tier 2: Name + ControlType(雙重驗證)
    if target_name and target_type:
        try:
            elem = win.Control(Name=target_name)
            if elem.Exists(maxSearchSeconds=0.5):
                if (elem.ControlTypeName or "") == target_type:
                    return elem
        except Exception:
            pass

    # Tier 3: 只用 Name
    if target_name:
        try:
            elem = win.Control(Name=target_name)
            if elem.Exists(maxSearchSeconds=0.5):
                return elem
        except Exception:
            pass

    return None


def find_element_rect(ui_info: dict, timeout: float = 2.0) -> Optional[tuple[int, int, int, int]]:
    """根據錄製時抓的 UIA 資訊、在當前螢幕找對應元素。

    回傳 (left, top, right, bottom) 物理像素座標、找不到回 None。
    timeout 秒內輪詢重試、對動態載入的 UI 給時間出現。
    """
    if not ui_info or not isinstance(ui_info, dict):
        return None
    try:
        import uiautomation as uia
    except ImportError:
        log.debug("[uia_lookup] uiautomation 沒裝、跳過")
        return None

    target_window = (ui_info.get("window_title") or "").strip()
    window_variants = _split_window_variants(target_window)

    end = time.time() + timeout
    while time.time() < end:
        win = None
        try:
            # Stage A: 視窗標題完全匹配
            if target_window:
                try:
                    cand = uia.WindowControl(searchDepth=1, Name=target_window)
                    if cand.Exists(maxSearchSeconds=0.3):
                        win = cand
                except Exception:
                    pass

            # 嘗試在當前 win 下找
            if win:
                elem = _try_find(win, ui_info)
                if elem:
                    r = elem.BoundingRectangle
                    rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                    log.info(f"[uia_lookup] 命中(完全匹配視窗):{rect} name='{ui_info.get('name')}' type='{ui_info.get('control_type')}'")
                    return rect

            # Stage B: 視窗標題模糊匹配(動態標題)
            for variant in window_variants:
                if len(variant) < 3:  # 太短的子串會亂中(例如 "-")
                    continue
                try:
                    regex = f".*{re.escape(variant)}.*"
                    cand = uia.WindowControl(searchDepth=1, RegexName=regex)
                    if not cand.Exists(maxSearchSeconds=0.3):
                        continue
                    elem = _try_find(cand, ui_info)
                    if elem:
                        r = elem.BoundingRectangle
                        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                        log.info(f"[uia_lookup] 命中(模糊匹配視窗 '{variant}'):{rect} name='{ui_info.get('name')}'")
                        return rect
                except Exception:
                    continue

            # Stage C: 沒指定視窗、找前景控制項全域搜
            if not target_window:
                try:
                    fore = uia.GetForegroundControl()
                    if fore:
                        elem = _try_find(fore, ui_info)
                        if elem:
                            r = elem.BoundingRectangle
                            rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                            log.info(f"[uia_lookup] 命中(前景):{rect}")
                            return rect
                except Exception:
                    pass

            # Stage D: 全域 root 搜尋(只用 AutomationId、過濾 offscreen)
            # 給 Windows 殼層元件用:taskbar Start button、釘選 app、系統匣、通知中心
            # 這些元素的 top window 是 `Shell_TrayWnd` Pane(沒 Name)、Stage A/B 找不到
            # 只用 AutomationId 避免誤中其他視窗的同名 Name 鈕
            target_auto_id_d = (ui_info.get("automation_id") or "").strip()
            if target_auto_id_d:
                try:
                    root = uia.GetRootControl()
                    if root:
                        elem = root.Control(AutomationId=target_auto_id_d)
                        if elem.Exists(maxSearchSeconds=0.5):
                            # 過濾隱藏 / offscreen 元素(否則會點到背景視窗的元素位置)
                            is_offscreen = False
                            try:
                                is_offscreen = bool(getattr(elem, "IsOffscreen", False))
                            except Exception:
                                is_offscreen = False
                            if is_offscreen:
                                log.debug(f"[uia_lookup] Stage D 找到但 IsOffscreen=True、忽略")
                            else:
                                r = elem.BoundingRectangle
                                # 也擋掉 BoundingRectangle 是 0×0(元素存在但沒實際範圍)
                                if (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                                    rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                                    log.info(f"[uia_lookup] 命中(Stage D 全域 root 搜尋 auto_id='{target_auto_id_d[:40]}'):{rect}")
                                    return rect
                except Exception as e:
                    log.debug(f"[uia_lookup] Stage D 例外(忽略):{type(e).__name__}: {e}")
        except Exception as e:
            log.debug(f"[uia_lookup] 搜尋迴圈例外(忽略):{type(e).__name__}: {e}")

        time.sleep(0.2)

    log.info(f"[uia_lookup] 在 {timeout}s 內找不到元素 name='{ui_info.get('name')}' type='{ui_info.get('control_type')}' window='{target_window}'")
    return None


def find_click_point(ui_info: dict, timeout: float = 2.0) -> Optional[tuple[int, int]]:
    """根據 UIA 資訊找元素、回傳該元素的中心點 (x, y)、找不到回 None。"""
    rect = find_element_rect(ui_info, timeout=timeout)
    if not rect:
        return None
    left, top, right, bottom = rect
    return (int((left + right) // 2), int((top + bottom) // 2))
