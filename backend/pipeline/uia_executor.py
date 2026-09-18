"""UIA(Microsoft UI Automation)action 分派 + element tree 檢視。

跟 pixel-based(computer_use.py)互補:
- pixel 走錄製座標 + CV/OCR/VLM 找位置;對 canvas / 自繪 UI / Citrix 才需要
- UIA 走 Windows 原生 GUI 結構樹;對 WPF/WinForms/Office/.NET app 直接讀寫、
  不靠像素、座標漂移免疫

兩者**共用 ComputerUseAction.type 命名空間**(actions[] 同一個 list、依 type 分派):
  uia_click           — 找控制項 click 中心
  uia_send_keys       — 對控制項打字 / 按鍵
  uia_get_text        — 讀控制項文字、save_as 變數
  uia_get_table_rowcount — 讀 DataGrid / ListView 列數、save_as 變數
  uia_click_cell      — 點 DataGrid 第 N 列第 M 欄 cell
  uia_wait_enabled    — 等控制項 enabled / 出現
  uia_assert_state    — 驗控制項狀態(enabled / checked / focused / exists)

"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import Optional, Any


# ── 共用:lazy import uiautomation(避免非 Windows / 沒裝時 import error 影響其他功能)──
def _get_auto():
    """lazy import、回 uiautomation module(失敗 raise 給上層處理)。"""
    import uiautomation
    return uiautomation


# ── 跨 step 變數儲存(uia_get_text / uia_get_table_rowcount 用 save_as 存的值)──
# scoped 到單個 step 的執行 session、執行完不保留。
# 變數替換語法:`{{var_name}}` 或 `{{var_name + 1}}`(支援簡單算術)
_VAR_PATTERN = None  # lazy compile


def _substitute_vars(text: str, variables: dict[str, Any]) -> str:
    """把 text 內的 {{var_name}} / {{var_name + N}} 替換為 variables 內的值。

    支援:
      {{x}}       → str(variables['x'])
      {{x + 1}}   → str(int(variables['x']) + 1)  (整數運算、便於 row_count + 1)
      {{x - 1}}   → str(int(variables['x']) - 1)

    找不到變數 → 原樣保留(不丟錯、避免炸掉)。
    """
    global _VAR_PATTERN
    if _VAR_PATTERN is None:
        import re
        _VAR_PATTERN = re.compile(r"\{\{\s*(\w+)\s*(?:([+-])\s*(\d+))?\s*\}\}")
    if not isinstance(text, str):
        return text

    def _repl(m):
        name = m.group(1)
        op = m.group(2)
        delta = m.group(3)
        if name not in variables:
            return m.group(0)  # 原樣
        v = variables[name]
        if op:
            try:
                vi = int(v)
                d = int(delta)
                v = vi + d if op == "+" else vi - d
            except (ValueError, TypeError):
                return str(v)  # 算術失敗就純字串替代
        return str(v)

    return _VAR_PATTERN.sub(_repl, text)


# ── window 定位 ─────────────────────────────────────────────────────────
def _resolve_window(auto, action: dict, step_window: str = ""):
    """把 action 或 step 層級的 window 設定解析成 uiautomation Window control。

    優先級:action.window > step_window > foreground 視窗。
    支援 wildcard(* 開頭 / 結尾 / 兩端)、底層走 RegexName。
    """
    win_pattern = (action.get("window") or step_window or "").strip()
    if not win_pattern:
        # 沒指定就用 foreground
        return auto.GetForegroundControl()

    # wildcard 處理
    import re
    has_star = "*" in win_pattern
    if has_star:
        # 轉 regex:* → .*
        regex = "^" + re.escape(win_pattern).replace(r"\*", ".*") + "$"
        win = auto.WindowControl(searchDepth=1, RegexName=regex)
    else:
        win = auto.WindowControl(searchDepth=1, Name=win_pattern)
    if win.Exists(1, 0.3):
        return win
    # 隱形字元後備比對:Edge 等視窗標題藏零寬字元(U+200B),使用者手打的
    # pattern 沒有它就永遠對不上,而且肉眼完全看不出差異(踩過三次)。
    # 比對前把 pattern 與標題的隱形字元都拆掉再配一次。
    _INVIS = "​‌‍﻿"
    def _strip(s):
        return re.sub(f"[{_INVIS}]", "", s or "")
    pat_s = _strip(win_pattern)
    regex_s = "^" + re.escape(pat_s).replace(r"\*", ".*") + "$" if "*" in pat_s         else "^" + re.escape(pat_s) + "$"
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                if re.match(regex_s, _strip(w.Name)):
                    return w
            except Exception:
                continue
    except Exception:
        pass
    return win   # 還是沒有就回原物件,讓呼叫端的 Exists 檢查誠實失敗


def _rescue_window_foreground(parent, logger=None) -> None:
    """把 parent 所屬的頂層視窗拉到前景 —— 睡眠分頁/冷凍視窗的最後救援。

    Edge 睡眠分頁會把整頁內容卸載,UIA 找得到視窗卻讀不到任何元素,
    且背景喚醒叫不醒(防搶焦點)。只在「不拉就必敗」時呼叫。
    """
    try:
        top = parent.GetTopLevelControl() or parent
    except Exception:
        top = parent
    try:
        title = top.Name or ""
    except Exception:
        return
    if not title:
        return
    try:
        import pygetwindow as gw
        for w in gw.getWindowsWithTitle(title):
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
                if logger:
                    logger.info(f"[uia] 救援:把「{title[:40]}」拉到前景(元素找不到、可能是睡眠分頁)")
                return
            except Exception:
                continue
    except Exception:
        pass
    try:
        top.SetActive()
    except Exception:
        pass


def _find_control(auto, parent, control_def: dict, fallback_rect: Optional[list] = None,
                  rescue: bool = False):
    """在 parent 控制項下找符合 control_def 的子控制項。

    control_def 例:
      {"type": "Button", "name": "儲存"}
      {"type": "DataGrid", "auto_id": "main-grid"}
      {"name": "OK"}    # 沒指定 type、找任何 control

    fallback_rect: 當 control_def 沒 Name 也沒 AutomationId(純 type 搜尋
    uiautomation 會拒、丟 LookupError "searchProperties must not be empty")、
    用 ControlFromPoint(rect 中心)當 fallback。是 picker 抓到的當下 rect、
    對 PaneControl / GroupControl 等沒 name 的 generic 容器特別有用。
    """
    if not isinstance(control_def, dict):
        return None
    ctype = (control_def.get("type") or "").strip()
    name = (control_def.get("name") or "").strip()
    auto_id = (control_def.get("auto_id") or control_def.get("automation_id") or "").strip()

    # 沒任何識別資訊 → 走 rect fallback
    if not name and not auto_id:
        if fallback_rect and len(fallback_rect) == 4 and fallback_rect[2] > 0 and fallback_rect[3] > 0:
            cx = int(fallback_rect[0] + fallback_rect[2] // 2)
            cy = int(fallback_rect[1] + fallback_rect[3] // 2)
            # ControlFromPoint 是「螢幕定點命中」:那個點被別的視窗蓋住時會
            # 抓到上層視窗的元素、接著點擊就打錯地方還回報成功(實測:點到
            # 使用者自己的瀏覽器)。先把目標視窗拉上來,讓這個點屬於它。
            try:
                if parent is not None:
                    _rescue_window_foreground(parent)
                    time.sleep(0.3)
            except Exception:
                pass
            try:
                return auto.ControlFromPoint(cx, cy)
            except Exception:
                return None
        return None

    # 組 kwargs
    # ⚠ 預設深度 10 對原生 app 夠,但**網頁表單一律搜不到** —— 瀏覽器把頁面內容
    #   埋在自己的外框底下,實測 Edge 上一個平常的表單欄位在深度 13-14(樹深 15)。
    #   深度不足的症狀是「inspect_window 看得到 auto_id、_find_control 卻回找不到」,
    #   很容易誤判成「這個 app 不支援 UIA」。BFS 找到就停,加深只在「真的找不到」
    #   時才多走,實測代價可忽略。
    kwargs = {"searchDepth": control_def.get("depth", 60)}
    if name:
        if "*" in name:
            # 萬用字元 → RegexName。真實系統的遮罩/狀態文字常沒有 auto_id、
            # 只有「資料處理中...」這種文字,結尾的刪節號還有 ... 與 … 兩種寫法,
            # 要求一字不差太脆 —— name: "資料處理中*" 就能對上。
            import re as _re
            kwargs["RegexName"] = "^" + _re.escape(name).replace(r"\*", ".*") + "$"
        else:
            kwargs["Name"] = name
    if auto_id:
        kwargs["AutomationId"] = auto_id

    # 依 type 走對應 method;沒指定就走通用 Control
    method_name = f"{ctype}Control" if ctype else "Control"
    if not hasattr(parent, method_name):
        # 不存在的 type、退回通用 Control
        method_name = "Control"
    ctrl = getattr(parent, method_name)(**kwargs)
    # Edge 冷樹自癒:視窗閒置後 a11y 子樹被放掉,FindFirst 找得到視窗卻找不到
    # 頁面元素(實測:重開的頁面閒置幾分鐘後 send_keys 全滅)。第一次找不到就
    # 實際列舉子元素逼 Chromium 重建樹、再試一次。Exists() 每次呼叫都會重搜,
    # 所以同一個 lazy control 物件可以直接重試。
    try:
        if ctrl.Exists(0.6, 0.2):
            return ctrl
    except Exception:
        return ctrl
    try:
        parent.GetChildren()
    except Exception:
        pass
    time.sleep(0.4)
    found = False
    try:
        found = ctrl.Exists(1, 0.3)
    except Exception:
        pass
    if found or not rescue:
        return ctrl
    # 終極救援(只有 rescue=True 的呼叫點,即「元素必須存在」的動作):
    # 視窗在、樹卻空 —— 睡眠分頁把頁面卸載了,背景喚醒叫不醒。
    # 拉到前景讓內容重載、再找最後一次。等待/探測類動作不走這裡
    # (元素「不在」對它們是合法狀態,拉前景會誤觸發)。
    _rescue_window_foreground(parent)
    time.sleep(1.5)
    try:
        ctrl.Exists(2, 0.5)
    except Exception:
        pass
    return ctrl


def element_exists(window_pattern: str, control_def: dict,
                   rect: Optional[list] = None, timeout: float = 3.0) -> bool:
    """探測「元素現在在不在畫面上」(if_element_found 條件分支用)。

    找不到不是錯誤、就是 False —— 跟 uia_wait 不同,這裡的「不在」是合法分支。
    """
    try:
        auto = _get_auto()
    except ImportError:
        return False
    try:
        win = _resolve_window(auto, {}, window_pattern)
        if not win.Exists(1, 0.3):
            return False
        deadline = time.time() + max(0.5, timeout)
        while True:
            try:
                ctrl = _find_control(auto, win, control_def, fallback_rect=rect)
                if ctrl is not None and ctrl.Exists(0.5, 0.2):
                    return True
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            try:
                win.GetChildren()   # Edge 冷樹敲醒(同 uia_wait 的作法)
            except Exception:
                pass
            time.sleep(0.3)
    except Exception:
        return False


# ── inspect:回傳 element tree(給 frontend tree picker 用)──────────────
def inspect_window(window_pattern: str = "", max_depth: int = 6,
                   max_children_per_node: int = 50,
                   logger: Optional[logging.Logger] = None,
                   max_nodes: int = 8000) -> dict:
    """檢視指定視窗的 UIA element tree、回 JSON 結構。

    Args:
        window_pattern: 視窗 title(支援 wildcard *)、空字串 = foreground
        max_depth: tree 深度上限(避免某些 app 有上千層)
        max_children_per_node: 每節點子元素上限(避免大表格 1 萬列展開)

    Returns 結構:
        {
            "ok": bool,
            "window": {"name": "...", "class": "...", "rect": [l,t,r,b]} 或 None,
            "tree": {
                "type": "Button",
                "name": "儲存",
                "auto_id": "save-btn",
                "rect": [x, y, w, h],
                "enabled": true,
                "children": [...],
            },
            "error": str (失敗時),
        }
    """
    log = logger or logging.getLogger(__name__)
    try:
        auto = _get_auto()
    except ImportError as e:
        return {"ok": False, "error": f"uiautomation 套件未安裝:{e}"}

    try:
        import re
        # 取得 window control
        if not window_pattern.strip():
            win = auto.GetForegroundControl()
        else:
            pat = window_pattern.strip()
            # 第一線:RegexName 比對(支援 * wildcard、預設視為 substring)
            if "*" in pat:
                regex = "^" + re.escape(pat).replace(r"\*", ".*") + "$"
            else:
                regex = "^.*" + re.escape(pat) + ".*$"
            win = auto.WindowControl(searchDepth=1, RegexName=regex)

            # 第二線:RegexName 萬一沒命中(uiautomation 對某些 shell window 如
            # Program Manager 的 RegexName 比對不穩)、改用 root.GetChildren() 在 Python 層手動比對
            if not win.Exists(2, 0.5):
                pat_lower = pat.lower().replace("*", "")
                root = auto.GetRootControl()
                matched = None
                for child in root.GetChildren():
                    try:
                        cname = (child.Name or "").strip()
                        if not cname:
                            continue
                        if pat_lower in cname.lower():
                            matched = child
                            break
                    except Exception:
                        continue
                if matched is not None:
                    win = matched
                else:
                    return {"ok": False, "error": f"找不到視窗:{window_pattern or '(foreground)'}"}

        # 元數據
        rect = win.BoundingRectangle
        win_meta = {
            "name": str(win.Name or ""),
            "class": str(getattr(win, "ClassName", "") or ""),
            "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            "process_id": int(getattr(win, "ProcessId", 0) or 0),
        }

        # 遞迴抓 tree。截斷一定要回報:公司入口網站把表單包在 iframe + 層層框架裡,
        # 深度不夠時欄位整片消失、畫面只剩頁尾連結,使用者完全看不出是被截掉的。
        cut = {"depth": 0, "budget": False}
        seen = [0]

        def _walk(ctrl, depth: int) -> dict:
            seen[0] += 1
            try:
                rect = ctrl.BoundingRectangle
                node = {
                    "type": str(ctrl.ControlTypeName or ""),
                    "name": str(ctrl.Name or "")[:200],   # 防超長 description
                    "auto_id": str(getattr(ctrl, "AutomationId", "") or ""),
                    "rect": [int(rect.left), int(rect.top),
                             int(rect.right - rect.left), int(rect.bottom - rect.top)],
                    "enabled": bool(getattr(ctrl, "IsEnabled", True)),
                    "offscreen": bool(getattr(ctrl, "IsOffscreen", False)),
                    "children": [],
                }
            except Exception as e:
                return {"type": "?", "name": f"(讀取失敗:{e!s:.80})", "children": []}

            if depth >= max_depth:
                try:
                    if ctrl.GetFirstChildControl():
                        cut["depth"] += 1
                except Exception:
                    pass
                return node

            try:
                kids = ctrl.GetChildren()[:max_children_per_node]
                for k in kids:
                    if seen[0] >= max_nodes:
                        cut["budget"] = True
                        break
                    node["children"].append(_walk(k, depth + 1))
            except Exception:
                pass
            return node

        tree = _walk(win, depth=0)
        return {"ok": True, "window": win_meta, "tree": tree,
                "truncated": {"depth_cut": cut["depth"], "budget_hit": cut["budget"],
                              "nodes": seen[0], "max_depth": max_depth}}

    except Exception as e:
        log.exception("inspect_window 失敗")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── action 分派 ──────────────────────────────────────────────────────────
@dataclass
class UiaActionResult:
    ok: bool
    message: str = ""
    saved_var: Optional[tuple[str, Any]] = None   # (var_name, value);step 層級的 variables 會收集


def execute_uia_action(action: dict, step_window: str,
                       variables: dict[str, Any],
                       logger: logging.Logger) -> UiaActionResult:
    """執行單個 uia_* action。

    variables 是 step 層級累積的(uia_get_text 等 save_as 進去的、
    後續 uia_send_keys.text 內 {{var}} 替換靠這個)。
    """
    try:
        auto = _get_auto()
    except ImportError as e:
        return UiaActionResult(False, f"uiautomation 未安裝:{e}")

    atype = action.get("type", "")
    try:
        # 剪貼簿動作不需要視窗 —— 別讓「目標視窗剛好不在前景/被關」害一個
        # 跟視窗無關的動作失敗(實測:讀剪貼簿排在切去別的工具之後,步驟視窗
        # 被判找不到、整步陪葬)。
        _windowless = atype in ("uia_get_clipboard", "uia_set_clipboard")
        win = None
        if not _windowless:
            win = _resolve_window(auto, action, step_window)
            if not win.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到視窗(action={atype})")

        if atype == "uia_click":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            # 優先用 UIA pattern 互動(不必把視窗拉到前景、真正背景操作);
            # pattern 都不支援才退回 ctrl.Click()(那個會 SetActive 把視窗叫到前景再用滑鼠點)
            # click_method: "mouse" = 跳過 pattern、直接滑鼠真點。
            # 為什麼需要:Tk 這類 MSAA 橋接的匿名按鈕會「假接受」InvokePattern
            # (回報成功、實際沒觸發),uia_click 以為成功就不退滑鼠 —— 實測
            # 複製鈕沒按到、剪貼簿還是舊值。
            if (action.get("click_method") or "").strip().lower() == "mouse":
                # 滑鼠點之前必須把目標視窗拉到最上層 —— 被別的視窗蓋住時,
                # 滑鼠會打在上層視窗上、還回報成功(實測:連按 6 次 alert 都沒關到)。
                _rescue_window_foreground(ctrl, logger)
                time.sleep(0.3)
                ctrl.Click()
                return UiaActionResult(True,
                    f"已點擊 {action.get('control', {}).get('name', '?')} via Click(mouse, 指定強制滑鼠)")
            method_used = ""
            try:
                ip = ctrl.GetInvokePattern()
                if ip:
                    ip.Invoke()
                    method_used = "InvokePattern"
            except Exception:
                pass
            if not method_used:
                try:
                    tp = ctrl.GetTogglePattern()
                    if tp:
                        tp.Toggle()
                        method_used = "TogglePattern"
                except Exception:
                    pass
            if not method_used:
                try:
                    sp = ctrl.GetSelectionItemPattern()
                    if sp:
                        sp.Select()
                        method_used = "SelectionItemPattern"
                except Exception:
                    pass
            if not method_used:
                try:
                    ep = ctrl.GetExpandCollapsePattern()
                    if ep:
                        # 展開 / 收合切換(menu / tree node 用)
                        if ep.ExpandCollapseState in (0, 2):  # collapsed / partial
                            ep.Expand()
                        else:
                            ep.Collapse()
                        method_used = "ExpandCollapsePattern"
                except Exception:
                    pass
            if not method_used:
                # fallback:退回滑鼠點。先明確把目標視窗拉到最上層 ——
                # uiautomation 的 Click() 不保證 activate,視窗被蓋住時滑鼠
                # 打在上層視窗、還回報成功(實測)。
                _rescue_window_foreground(ctrl, logger)
                time.sleep(0.3)
                ctrl.Click()
                method_used = "Click(mouse, 視窗會被拉到前景)"
            return UiaActionResult(True, f"已點擊 {action.get('control', {}).get('name', '?')} via {method_used}")

        elif atype == "uia_send_keys":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            text = action.get("text", "")
            keys = action.get("keys", [])
            if text:
                # variable 替換
                text = _substitute_vars(text, variables)
                # 優先 ValuePattern.SetValue(背景 work、瞬時、不用敲鍵盤)
                # ⚠ 不能用 ctrl.GetValuePattern()：_find_control 沒指定 type 時回的是
                #   通用 Control 物件,**沒有這個方法** → AttributeError 被吞掉 →
                #   永遠默默退回 SendKeys(拉前景、逐字打、輸入法可能攔截)。
                #   通用 Control 要用 GetPattern(PatternId.ValuePattern)。
                via_pattern = False
                try:
                    _pid = getattr(getattr(auto, "PatternId", None), "ValuePattern", None)
                    vp = ctrl.GetPattern(_pid) if _pid is not None else None
                    if vp and not getattr(vp, "IsReadOnly", True):
                        vp.SetValue(text)
                        via_pattern = True
                except Exception as _e:
                    logger.debug(f"[uia] ValuePattern 填值失敗、退 SendKeys:{_e}")
                if not via_pattern:
                    # fallback:模擬鍵盤打字、需要 focus(可能拉前景)
                    ctrl.SendKeys(text)
                return UiaActionResult(True, f"已輸入 {text[:60]!r} via {'ValuePattern' if via_pattern else 'SendKeys(keyboard sim)'}")
            elif keys:
                # 把 ["ctrl","s"] 之類轉 uiautomation 格式 "{Ctrl}s"
                # 簡化版:單一鍵(含特殊)直接 SendKeys、組合用 {Ctrl} 之類
                if len(keys) == 1:
                    ctrl.SendKeys("{" + keys[0].capitalize() + "}" if len(keys[0]) > 1 else keys[0])
                else:
                    # 修飾鍵 + 一般鍵組合
                    mod_map = {"ctrl": "{Ctrl}", "shift": "{Shift}", "alt": "{Alt}", "win": "{Win}"}
                    parts = []
                    for k in keys[:-1]:
                        parts.append(mod_map.get(k.lower(), "{" + k.capitalize() + "}"))
                    last = keys[-1]
                    parts.append("{" + last.capitalize() + "}" if len(last) > 1 else last)
                    ctrl.SendKeys("".join(parts))
                return UiaActionResult(True, f"已按鍵 {'+'.join(keys)}")
            else:
                return UiaActionResult(False, "uia_send_keys 缺 text 或 keys")

        elif atype == "uia_get_text":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到控制項:{action.get('control')}")
            # 取欄位「值」——不是標籤。三段式,每段都記錄走哪條,別再靜默退回 Name。
            # ⚠ 舊寫法用 ctrl.GetValuePattern(),但 _find_control 在沒指定 type 時回的是
            #   **通用 Control 物件,它沒有 GetValuePattern 方法** → AttributeError 被
            #   except 吞掉 → 悄悄退回讀 Name。症狀是「讀 EAP 欄位讀回一堆欄位標題」,
            #   看起來像成功、其實全錯。通用 Control 要用 GetPattern(PatternId.xxx)。
            text, via = "", ""
            for _pid_name in ("ValuePattern", "LegacyIAccessiblePattern"):
                _pid = getattr(getattr(auto, "PatternId", None), _pid_name, None)
                if _pid is None:
                    continue
                try:
                    _p = ctrl.GetPattern(_pid)
                    _v = getattr(_p, "Value", None) if _p else None
                    if _v:
                        text, via = str(_v), _pid_name
                        break
                except Exception as _e:
                    logger.debug(f"[uia] {_pid_name} 取值失敗:{_e}")
            if not text:
                # 走到這代表控制項沒有 value(例:純標籤、按鈕),Name 才是它的內容。
                # 但對輸入框而言 Name 是**標籤**不是值 —— 明講出來,別讓人誤判成成功。
                text, via = (ctrl.Name or ""), "Name(注意:輸入框的 Name 是標籤、不是值)"
            # save_as 名稱做變數替換 —— for_each 裡「結果{{品規_序號}}」每輪存不同變數
            save_as = _substitute_vars((action.get("save_as") or ""), variables).strip()
            if save_as:
                return UiaActionResult(True, f"讀到 {text[:60]!r}(via {via})、存到 {save_as}",
                                       saved_var=(save_as, text))
            return UiaActionResult(True, f"讀到 {text[:60]!r}(via {via}、沒設 save_as)")

        elif atype == "uia_get_table_rowcount":
            grid = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not grid or not grid.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到表格:{action.get('control')}")
            # GridPattern 是標準介面;沒實作就退到子元素數
            n = 0
            try:
                gp = grid.GetGridPattern()
                if gp:
                    n = int(gp.RowCount)
            except Exception:
                pass
            if n == 0:
                # fallback:數 DataItem 子元素
                try:
                    n = sum(1 for c in grid.GetChildren()
                            if str(c.ControlTypeName or "") in ("DataItem", "ListItem", "TreeItem"))
                except Exception:
                    pass
            save_as = (action.get("save_as") or "").strip()
            if save_as:
                return UiaActionResult(True, f"表格列數 {n}、存到 {save_as}",
                                       saved_var=(save_as, n))
            return UiaActionResult(True, f"表格列數 {n}(沒設 save_as)")

        elif atype == "uia_click_cell":
            grid = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not grid or not grid.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到表格:{action.get('control')}")
            row = action.get("row", 0)
            col = action.get("column", 0)
            # variable 替換(row 可能是 "{{row_count + 1}}")
            if isinstance(row, str):
                row = int(_substitute_vars(row, variables))
            if isinstance(col, str):
                col = int(_substitute_vars(col, variables))
            row, col = int(row), int(col)

            cell = None
            try:
                gp = grid.GetGridPattern()
                if gp:
                    cell = gp.GetItem(row, col)
            except Exception:
                pass
            if cell is None:
                # fallback:用 DataItem 子層、第 row 個的第 col 個 child
                try:
                    items = [c for c in grid.GetChildren()
                             if str(c.ControlTypeName or "") in ("DataItem", "ListItem", "TreeItem")]
                    if 0 <= row < len(items):
                        cells = items[row].GetChildren()
                        if 0 <= col < len(cells):
                            cell = cells[col]
                except Exception:
                    pass
            if cell is None:
                return UiaActionResult(False, f"找不到 cell ({row}, {col})")
            # 優先 SelectionItemPattern.Select(cell 通常是可選元素、不必滑鼠點)
            method_used = ""
            try:
                sp = cell.GetSelectionItemPattern()
                if sp:
                    sp.Select()
                    method_used = "SelectionItemPattern"
            except Exception:
                pass
            if not method_used:
                try:
                    ip = cell.GetInvokePattern()
                    if ip:
                        ip.Invoke()
                        method_used = "InvokePattern"
                except Exception:
                    pass
            if not method_used:
                cell.Click()
                method_used = "Click(mouse、視窗會拉前景)"
            return UiaActionResult(True, f"已點 cell ({row}, {col}) via {method_used}")

        elif atype == "uia_wait_enabled":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            timeout = float(action.get("timeout_sec", 10))
            t_start = time.time()
            while time.time() - t_start < timeout:
                if ctrl and ctrl.Exists(0, 0) and ctrl.IsEnabled:
                    return UiaActionResult(True, f"控制項已 enabled、耗時 {time.time()-t_start:.1f}s")
                time.sleep(0.3)
            return UiaActionResult(False, f"等 {timeout}s 控制項仍未 enabled")

        elif atype == "uia_select":
            # 下拉選單（ComboBox / HTML <select>）選指定選項。
            # 為什麼要專用動作：點開下拉再點選項的錄製回放對「彈出清單」很脆
            # （清單是暫時元素、位置會漂）；走 UIA pattern 是背景操作、不動滑鼠。
            # 選項文字支援 {{變數}} —— 搭配 {{ input.月份 }} / {{ now.month }}
            # 就能「這次選 08、下次選 09」或「永遠選當月」。
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not ctrl or not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, f"找不到下拉選單:{action.get('control')}")
            raw_opt = action.get("text", "")
            option = _substitute_vars(raw_opt, variables) if isinstance(raw_opt, str) else str(raw_opt)
            option = option.strip()
            if not option:
                return UiaActionResult(False, "uia_select 缺 text（要選的選項文字，例:08）")

            def _get_pat(c, pat_name):
                try:
                    pid = getattr(getattr(auto, "PatternId", None), pat_name, None)
                    return c.GetPattern(pid) if pid is not None else None
                except Exception:
                    return None

            # 策略 1:不展開直接找選項（Edge 的 HTML <select> 收合時
            # 子 ListItem 也在 accessibility tree 裡）→ SelectionItemPattern.Select()
            # 全程背景、不彈清單、不動滑鼠。
            # 策略 2:ExpandCollapse 展開後再找（原生 app 的 ComboBox 常要展開才生子項）。
            item = None
            via = ""
            expanded = False
            for round_no in (1, 2, 3):
                for attempt in ("collapsed", "expanded"):
                    if attempt == "expanded":
                        ecp = _get_pat(ctrl, "ExpandCollapsePattern")
                        if not ecp:
                            break
                        try:
                            ecp.Expand()
                        except Exception as _e:
                            # Chromium(Edge) 的 UIA 常在動作「已生效」時照樣丟
                            # COMError「事件無法啟動任何訂閱者」—— 不能當失敗中止，
                            # 展開成沒成交給後面的搜尋結果決定。
                            logger.debug(f"[uia] Expand 丟例外(可能已生效):{_e}")
                        expanded = True
                        time.sleep(0.3 * round_no + 0.1)   # 給清單 render 時間,逐輪加長
                    try:
                        cand = ctrl.ListItemControl(Name=option, searchDepth=8)
                        if cand.Exists(1, 0.2):
                            item = cand
                            break
                    except Exception:
                        pass
                    # 展開後的清單可能掛在視窗層而不是 combo 底下（原生 app 常見）
                    if attempt == "expanded" and win is not None:
                        try:
                            cand = win.ListItemControl(Name=option, searchDepth=30)
                            if cand.Exists(1, 0.2):
                                item = cand
                                break
                        except Exception:
                            pass
                if item is not None:
                    break
                # Edge 的 a11y 樹會「冷掉」:視窗一陣子沒被輔助工具查詢,
                # 收合下拉的子項從樹上消失、連 Expand 都生不出清單。
                # 實際列舉子元素會逼 Chromium 重建樹,醒了之後下一輪就找得到。
                logger.debug(f"[uia] 第 {round_no} 輪沒找到「{option}」,敲醒 a11y 樹重試")
                try:
                    ctrl.GetChildren()
                    if win is not None:
                        win.GetChildren()
                except Exception:
                    pass
                time.sleep(0.6)
            if item is None:
                if expanded:
                    try:
                        _get_pat(ctrl, "ExpandCollapsePattern").Collapse()
                    except Exception:
                        pass
                return UiaActionResult(
                    False,
                    f"下拉選單裡找不到選項「{option}」"
                    + (f"（原文 {raw_opt!r}、變數替換後）" if raw_opt != option else "")
                    + "。檢查:選項文字要一字不差（含前導零，08 不是 8）")

            sel = _get_pat(item, "SelectionItemPattern")
            select_err = None
            if sel is not None:
                try:
                    sel.Select()
                except Exception as _e:
                    select_err = _e   # 同 Expand 的 Chromium quirk:回讀驗證定成敗
                via = "SelectionItemPattern"
            else:
                item.Click()
                via = "Click(mouse fallback)"
            if expanded:
                try:
                    _get_pat(ctrl, "ExpandCollapsePattern").Collapse()
                except Exception:
                    pass

            # 回讀驗證 —— 「選了」不等於「選對」。ComboBox 的 ValuePattern
            # 會回當前選中的選項文字，讀得到就核對；讀不到就誠實標註未驗證。
            time.sleep(0.25)
            vp = _get_pat(ctrl, "ValuePattern")
            got = ""
            try:
                got = str(getattr(vp, "Value", "") or "") if vp else ""
            except Exception:
                got = ""
            if not got and select_err is not None:
                return UiaActionResult(
                    False,
                    f"Select 丟例外且無法回讀驗證是否生效:{select_err}")
            if got and got.strip() != option:
                return UiaActionResult(
                    False,
                    f"選了「{option}」但下拉當前值是「{got.strip()}」—— 沒選上"
                    f"（via {via}）。可能同名選項在別層、或清單需要更長 render 時間")
            note = f"、回讀驗證 {got.strip()!r}" if got else "、無法回讀驗證(控制項沒有 ValuePattern)"
            return UiaActionResult(True, f"已選「{option}」via {via}{note}")

        elif atype == "uia_wait":
            # 等「畫面出現某個東西」才繼續 —— 查詢時間不固定時取代寫死的 wait 秒數。
            # until:
            #   appear        目標元素出現(預設)
            #   disappear     目標元素消失(等「查詢中…」遮罩收掉最常用)
            #   text_contains 目標元素的文字包含 text(等結果區出現「已匯出」等關鍵字)
            #   text_equals   目標元素的文字等於 text
            until = (action.get("until") or "appear").strip()
            timeout = float(action.get("timeout_sec", 60))
            raw_txt = action.get("text", "")
            want = _substitute_vars(raw_txt, variables) if isinstance(raw_txt, str) else str(raw_txt)
            want = want.strip()
            if until in ("text_contains", "text_equals") and not want:
                return UiaActionResult(False, f"uia_wait until={until} 需要 text(要等的關鍵字)")
            if until not in ("appear", "disappear", "text_contains", "text_equals"):
                return UiaActionResult(False, f"uia_wait 不認得 until={until!r}"
                                              "(可用:appear/disappear/text_contains/text_equals)")
            cdef = action.get("control") or {}

            def _read_text(c):
                # 與 uia_get_text 同邏輯:Value → LegacyIAccessible → Name
                for _pid_name in ("ValuePattern", "LegacyIAccessiblePattern"):
                    _pid = getattr(getattr(auto, "PatternId", None), _pid_name, None)
                    if _pid is None:
                        continue
                    try:
                        _p = c.GetPattern(_pid)
                        _v = getattr(_p, "Value", None) if _p else None
                        if _v:
                            return str(_v)
                    except Exception:
                        pass
                return c.Name or ""

            t_start = time.time()
            last_txt = None
            poll_n = 0
            while time.time() - t_start < timeout:
                # 每輪重找 —— 元素會出現/消失,拿舊 reference 會 stale
                ctrl = _find_control(auto, win, cdef, fallback_rect=action.get("rect"))
                exists = bool(ctrl) and ctrl.Exists(0, 0)
                elapsed = time.time() - t_start
                if until == "appear" and exists:
                    return UiaActionResult(True, f"目標已出現、等了 {elapsed:.1f}s")
                if until == "disappear" and not exists:
                    return UiaActionResult(True, f"目標已消失、等了 {elapsed:.1f}s")
                if until in ("text_contains", "text_equals") and exists:
                    cur = _read_text(ctrl)
                    last_txt = cur
                    hit = (want in cur) if until == "text_contains" else (cur.strip() == want)
                    if hit:
                        return UiaActionResult(True,
                            f"文字符合「{want}」、等了 {elapsed:.1f}s(實際:{cur[:60]!r})")
                poll_n += 1
                if poll_n % 8 == 0:
                    # Edge a11y 樹冷掉時元素「看起來」不存在 —— 定期實際列舉
                    # 子元素逼它重建,避免結果早就出來了卻等到超時。
                    try:
                        win.GetChildren()
                    except Exception:
                        pass
                time.sleep(0.4)
            detail = f"、最後讀到 {last_txt[:60]!r}" if last_txt is not None else ""
            return UiaActionResult(False,
                f"等了 {timeout:.0f}s 條件仍未成立(until={until}"
                + (f"、text=「{want}」" if want else "") + f"){detail}。"
                "查詢比預期久的話調大 timeout_sec")

        elif atype == "uia_get_clipboard":
            # 讀剪貼簿存變數 —— UIA 讀不到內容的工具(Tkinter 沒有 UIA provider、
            # 整個視窗是空白 Pane)用「工具端複製 → 這裡讀」的剪貼簿交接。
            # 典型:Tk 工具按「複製勾選清單」→ uia_get_clipboard → for_each 逐筆跑。
            save_as = (action.get("save_as") or "").strip()
            if not save_as:
                return UiaActionResult(False, "uia_get_clipboard 缺 save_as(存到哪個變數)")
            try:
                import pyperclip
                txt = pyperclip.paste()
            except Exception as e:
                return UiaActionResult(False, f"讀剪貼簿失敗:{e}")
            if not (txt or "").strip():
                return UiaActionResult(False,
                    "剪貼簿是空的 —— 來源工具還沒按「複製」?或前面少了點按複製鈕的動作")
            return UiaActionResult(True,
                f"讀到剪貼簿 {len(txt)} 字:{txt[:60]!r}、存到 {save_as}",
                saved_var=(save_as, txt))

        elif atype == "uia_set_clipboard":
            # 把文字塞進 Windows 剪貼簿、給後續 Ctrl+V 用、跨步驟 / 跨節點傳值用
            # 文字支援 {{變數}} 替換(get_text / get_table_rowcount 存的變數都能用)
            raw = action.get("text", "")
            text = _substitute_vars(raw, variables) if isinstance(raw, str) else str(raw)
            try:
                # pyperclip 而不是 pywin32：pyperclip 本來就是必要相依
                # （type_text 的剪貼簿貼上走它），效果相同（CF_UNICODETEXT），
                # 省掉 30MB 的 pywin32 —— 它從未進 requirements.txt，
                # 另一台機器乾淨安裝時這個動作直接硬失敗。
                import pyperclip
                pyperclip.copy(text)
            except ImportError:
                return UiaActionResult(False, "pyperclip 未安裝、無法寫剪貼簿（pip install pyperclip）")
            except Exception as e:
                return UiaActionResult(False, f"寫剪貼簿失敗:{type(e).__name__}: {e}")
            return UiaActionResult(True, f"已寫剪貼簿 {text[:60]!r}({len(text)} 字)")

        elif atype == "uia_close_window":
            # 關閉視窗的「正確」方式:走 WindowPattern.Close()、不靠 title/X 點擊、
            # 不必把視窗拉前景。控制項可以是視窗本身或視窗內任何元素(會往上找 Window)
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            if not ctrl:
                # 沒指定 control 時用 step 的 window
                ctrl = win
            if not ctrl.Exists(2, 0.5):
                return UiaActionResult(False, "找不到要關的視窗")
            # 往上找 Window control(WindowPattern 在 WindowControl 才有)
            target = ctrl
            for _ in range(20):  # 最多往上 20 層、防 infinite loop
                try:
                    if str(target.ControlTypeName or "") == "WindowControl":
                        break
                    parent = target.GetParentControl()
                    if not parent:
                        break
                    target = parent
                except Exception:
                    break
            try:
                wp = target.GetWindowPattern()
                if wp:
                    wp.Close()
                    return UiaActionResult(True, f"已關閉視窗 {target.Name!r:.60} via WindowPattern")
            except Exception as e:
                # ⚠ Close() 丟例外不等於沒關掉。2026-08-06 實測 Win11 記事本：
                #   視窗確實關了，但 COM 回 -2147220991「事件無法啟動任何訂閱者」。
                #   直接回失敗會讓整個工作流誤判中止，所以先確認視窗是不是真的還在。
                _gone = False
                try:
                    for _ in range(6):          # 最多等 1.5s，關閉是非同步的
                        time.sleep(0.25)
                        if not target.Exists(0, 0):
                            _gone = True
                            break
                except Exception:
                    _gone = True
                if _gone:
                    return UiaActionResult(
                        True, f"已關閉視窗（Close() 回了 {type(e).__name__}，"
                              f"但確認視窗已消失，視為成功）")
                return UiaActionResult(False, f"WindowPattern.Close() 失敗且視窗還在:{e}")
            return UiaActionResult(False, "目標控制項或父鏈沒 WindowPattern、不能關")

        elif atype == "uia_assert_state":
            ctrl = _find_control(auto, win, action.get("control") or {}, fallback_rect=action.get("rect"), rescue=True)
            check = (action.get("check") or "exists").strip()
            if check == "exists":
                ok = bool(ctrl and ctrl.Exists(2, 0.5))
            elif check == "enabled":
                ok = bool(ctrl and ctrl.Exists(2, 0.5) and ctrl.IsEnabled)
            elif check == "focused":
                ok = bool(ctrl and ctrl.Exists(2, 0.5) and getattr(ctrl, "HasKeyboardFocus", False))
            elif check == "checked":
                ok = False
                try:
                    tp = ctrl.GetTogglePattern()
                    ok = bool(tp and tp.ToggleState == 1)   # ToggleState_On
                except Exception:
                    pass
            else:
                return UiaActionResult(False, f"未知 check 類型:{check}")
            return UiaActionResult(ok, f"assert {check} → {ok}")

        else:
            return UiaActionResult(False, f"未知 uia action type:{atype}")

    except Exception as e:
        logger.exception(f"uia action {atype} 失敗")
        return UiaActionResult(False, f"{type(e).__name__}: {e}")
