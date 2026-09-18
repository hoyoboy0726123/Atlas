"""Jinja2-based template render for pipeline step string fields.

設計原則:
- 只 render 含 {{ }} / {% %} 的字串、其餘原樣回傳(零成本)
- 用 StrictUndefined:未定義變數明確報錯、不靜默 ""
- 保護 UIA 既有的「步驟內 {{var}} / {{var + 1}}」短變數語法、不被 Jinja2 吃掉
  (那段在 uia_executor._substitute_vars 處理、執行 action 時才解析)

Context 結構:
    {
        "steps": {
            "<step_name>": {
                "output": {
                    "path": "...",        # actual_output_path
                    "stdout": "...",      # stdout_tail
                    "stderr": "...",
                    "exit_code": 0,
                    "status": "ok",
                    "<save_as_var>": ...,  # step_vars promote 到 output
                }
            },
            ...
        },
        "input": {<input_params>},
        "env": {<os.environ>},
    }
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from jinja2 import Environment, StrictUndefined, UndefinedError, TemplateSyntaxError


class ExpressionError(Exception):
    """render 失敗(未定義變數 / 語法錯誤)。錯誤訊息已包含原因。"""


# Jinja2 環境、lazy init
_env: Optional[Environment] = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            undefined=StrictUndefined,
            autoescape=False,        # 不是 render HTML
            keep_trailing_newline=True,
        )
        # 常用 filter
        import json as _json
        _env.filters["json"] = lambda v: _json.dumps(v, ensure_ascii=False)
    return _env


# UIA 既有的「步驟內」短變數語法:{{var}} 或 {{var + 1}} / {{var - 2}}
# 規則:整個 {{ }} 內只有單一識別字、可選 [+-] 整數;不含 dot / pipe / filter
# 這類 pattern 由 uia_executor._substitute_vars 在執行 action 時解析、Jinja2 不能碰
_INTRA_STEP_VAR_RE = re.compile(
    r"\{\{\s*\w+\s*(?:[+-]\s*\d+)?\s*\}\}"
)


def _protect_intra_step_vars(s: str) -> str:
    """把 UIA 步驟內短變數包進 {% raw %},Jinja2 render 時原樣輸出、不嘗試解析。

    {{var}}     → {% raw %}{{var}}{% endraw %}
    {{steps.X}} → 原樣(會被 Jinja2 處理)
    """
    def _wrap(m: re.Match) -> str:
        return "{% raw %}" + m.group(0) + "{% endraw %}"
    return _INTRA_STEP_VAR_RE.sub(_wrap, s)


def render(s: Any, context: dict) -> Any:
    """Render 單一字串。非字串 / 不含 {{ }} 直接回傳。

    Raises:
        ExpressionError: 變數未定義或 Jinja2 語法錯
    """
    if not isinstance(s, str):
        return s
    if "{{" not in s and "{%" not in s:
        return s
    protected = _protect_intra_step_vars(s)
    try:
        tmpl = _get_env().from_string(protected)
        return tmpl.render(**context)
    except UndefinedError as e:
        raise ExpressionError(f"未定義變數: {e}") from e
    except TemplateSyntaxError as e:
        raise ExpressionError(f"模板語法錯誤: {e}") from e


# PipelineStep top-level 字串欄位,實際會用到變數的清單
# (timeout / retry / 等數值欄位不 render)
_STEP_STR_FIELDS = (
    "batch",
    "working_dir",
    "message",
    "uia_window",
    "vv_prompt",
    "wc_url",
    "wc_video_url",
    "wc_wait_for_selector",
    "wc_cookies",
    "wc_child_link_pattern",
    "wc_video_subs_langs",
    "skill",
    "mcp_command",
    "mcp_tool",
)


# ComputerUseAction 字串欄位中、會吃變數的清單
_ACTION_STR_FIELDS = (
    "text", "title", "title_contains", "vlm_prompt", "expected",
    "ocr_text", "window", "image", "image2",
    # ocr_get_text 的標籤 —— 加 ocr_get_text 時漏了這裡。不 render 的話
    # 「用上一步拿到的欄位名當標籤」寫了 {{ }} 也不會被替換，會拿字面字元
    # 去找標籤 → 一定找不到，而且看起來像 OCR 讀不到。
    "label",
)


def _render_action_dict(d: dict, context: dict) -> None:
    """In-place render 巢狀動作 dict(for_each.do / then / else 裡的子動作)。

    巢狀動作沒經過 pydantic 轉物件、是原始 dict,欄位覆蓋範圍與
    _ACTION_STR_FIELDS 對齊,並繼續往更深層的 do/then/else 遞迴
    (if_element_found 包在 for_each 裡是實際使用的形態)。
    """
    for fname in _ACTION_STR_FIELDS + ("items", "save_as", "pattern", "dir"):
        v = d.get(fname)
        if isinstance(v, str) and v and "{{" in v:
            new = render(v, context)
            if new != v:
                d[fname] = new
    ctrl = d.get("control")
    if isinstance(ctrl, dict):
        d["control"] = {
            k: (render(v, context) if isinstance(v, str) and "{{" in v else v)
            for k, v in ctrl.items()
        }
    ks = d.get("keys")
    if isinstance(ks, list):
        d["keys"] = [render(k, context) if isinstance(k, str) and "{{" in k else k for k in ks]
    for nf in ("do", "then", "else"):
        lst = d.get(nf)
        if isinstance(lst, list):
            for sub in lst:
                if isinstance(sub, dict):
                    _render_action_dict(sub, context)
    u = d.get("until")
    if isinstance(u, dict):
        _render_action_dict(u, context)


def render_step(step, context: dict):
    """In-place render 一個 PipelineStep 的所有字串欄位。

    沒寫 {{ }} 的欄位完全跳過(零 render 成本、舊行為不變)。
    回傳同一個 step 物件(方便鏈式使用)。

    覆蓋範圍:
      - PipelineStep top-level str 欄位(batch / working_dir / message / etc.)
      - StepOutput.path / .expect
      - ComputerUseAction list 內每個 action 的 str 欄位 + control dict + keys list + row/column
      - outlook_params dict 的 str values
      - wc_urls list 的 str items
    """
    # 1) top-level 字串欄位
    for fname in _STEP_STR_FIELDS:
        v = getattr(step, fname, None)
        if isinstance(v, str) and v:
            new = render(v, context)
            if new != v:
                setattr(step, fname, new)

    # 2) StepOutput
    if step.output is not None:
        if isinstance(step.output.path, str) and step.output.path:
            step.output.path = render(step.output.path, context)
        if isinstance(step.output.expect, str) and step.output.expect:
            step.output.expect = render(step.output.expect, context)
        if isinstance(step.output.description, str) and step.output.description:
            step.output.description = render(step.output.description, context)

    # 3) ComputerUseAction list
    if step.actions:
        for action in step.actions:
            for fname in _ACTION_STR_FIELDS:
                v = getattr(action, fname, None)
                if isinstance(v, str) and v:
                    new = render(v, context)
                    if new != v:
                        setattr(action, fname, new)
            # control dict 的 str values
            if action.control:
                action.control = {
                    k: (render(v, context) if isinstance(v, str) else v)
                    for k, v in action.control.items()
                }
            # row / column 可填字串(intra-step var 或 inter-step)
            for fname in ("row", "column"):
                v = getattr(action, fname, None)
                if isinstance(v, str) and v:
                    new = render(v, context)
                    if new != v:
                        setattr(action, fname, new)
            # keys list
            if action.keys:
                action.keys = [
                    render(k, context) if isinstance(k, str) else k
                    for k in action.keys
                ]
            # for_each 的 items(字串型清單來源,例 "{{ input.品規清單 }}")
            v = getattr(action, "items", None)
            if isinstance(v, str) and v:
                new = render(v, context)
                if new != v:
                    action.items = new
            # 巢狀子動作(for_each.do / if_*.then / if_*.else):遞迴 render。
            # 不做的話「{{ now.year }}」在巢狀裡會以字面字元執行 —— 實測
            # for_each 裡的 uia_select 就這樣拿「{{ now.year }}」去找選項全滅。
            for _nf in ("do", "then", "else_"):
                _lst = getattr(action, _nf, None)
                if _lst:
                    for _sub in _lst:
                        if isinstance(_sub, dict):
                            _render_action_dict(_sub, context)
            _u = getattr(action, "until", None)
            if isinstance(_u, dict):
                _render_action_dict(_u, context)

    # 4) outlook_params:shallow render 每個 str value
    if step.outlook_params:
        step.outlook_params = {
            k: (render(v, context) if isinstance(v, str) else v)
            for k, v in step.outlook_params.items()
        }

    # 5) wc_urls list
    if step.wc_urls:
        step.wc_urls = [
            render(u, context) if isinstance(u, str) else u
            for u in step.wc_urls
        ]

    # 6) MCP：mcp_args(list) + mcp_tool_args(dict，值可含 {{ }}，支援巢狀 list/dict)
    def _r_deep(o):
        if isinstance(o, str):
            return render(o, context) if o else o
        if isinstance(o, list):
            return [_r_deep(x) for x in o]
        if isinstance(o, dict):
            return {k: _r_deep(v) for k, v in o.items()}
        return o
    if getattr(step, "mcp_args", None):
        step.mcp_args = [_r_deep(a) for a in step.mcp_args]
    if getattr(step, "mcp_tool_args", None):
        step.mcp_tool_args = _r_deep(step.mcp_tool_args)

    return step


# 已解析的 JSON 輸出快取:key=(path, mtime),避免每步 build_context 重讀所有 .json(原本 O(N^2))
_JSON_OUT_CACHE: dict[tuple, Any] = {}


def _load_json_output(path: str) -> Any:
    """讀該步 .json 輸出檔、回傳 dict(供 output 攤平);失敗回 None。
    安全:只讀 .json、≤1MB、utf-8-sig(容 UTF-8 BOM)、with open、(path,mtime) 快取。
    """
    if not isinstance(path, str) or not path.lower().endswith(".json"):
        return None
    cand = path
    if not os.path.exists(cand) and cand.startswith("/mnt/") and len(cand) > 7 and cand[6] == "/":
        cand = cand[5].upper() + ":" + cand[6:]   # /mnt/c/.. → C:/..
    try:
        st = os.stat(cand)
    except OSError:
        return None
    if st.st_size > 1_000_000:
        return None
    key = (cand, st.st_mtime)
    if key in _JSON_OUT_CACHE:
        return _JSON_OUT_CACHE[key]
    import json as _json
    data = None
    try:
        with open(cand, encoding="utf-8-sig") as f:   # utf-8-sig 同時容 BOM / 無 BOM
            data = _json.load(f)
    except Exception:
        data = None
    if len(_JSON_OUT_CACHE) > 256:
        _JSON_OUT_CACHE.clear()   # 簡單防爆(非完整 LRU,夠用)
    _JSON_OUT_CACHE[key] = data
    return data


# output namespace 的固定 key（步驟中繼資料）。
# 撞名時**使用者資料贏** —— output.status 讀起來就該是「我輸出的 status」。
# 步驟自己的值永遠可從 output.step.<key> 取得，不會遺失。
_FIXED_OUTPUT_KEYS = {"stdout", "stderr", "exit_code", "path", "status"}


def build_context(*, step_results=None, input_params=None,
                  env_passthrough: bool = True) -> dict:
    """組裝 render context: {steps, input, env}。

    Args:
        step_results: list[StepResult] — PipelineRun.step_results(已完成的)
        input_params: dict — run 啟動時傳的參數
        env_passthrough: 是否把 os.environ 暴露到 env namespace
    """
    steps_ns: dict[str, dict] = {}
    if step_results:
        for sr in step_results:
            out: dict[str, Any] = {
                "stdout": getattr(sr, "stdout_tail", "") or "",
                "stderr": getattr(sr, "stderr_tail", "") or "",
                "exit_code": getattr(sr, "exit_code", 0),
                "path": getattr(sr, "actual_output_path", "") or "",
                "status": getattr(sr, "validation_status", "") or "",
            }
            # ⚠ 撞名：
            #   使用者資料的欄位名如果跟上面五個固定 key 一樣
            #   （status / path / stdout / stderr / exit_code），舊寫法是固定 key 贏，
            #   使用者拿到的是「步驟中繼資料」而不是自己的值 —— 而且毫無提示。
            #   實測：腳本輸出 status='failed'，{{ steps.X.output.status }} 求出 'ok'
            #   （那是步驟執行狀態），switch 因此跳到 ok 分支。status 是極常見的欄位名。
            #   改成**使用者資料優先**；步驟中繼資料完整保留在 output.step.<key>。
            out["step"] = dict(out)
            _shadowed: list = []

            def _promote(src: dict) -> None:
                for k, v in src.items():
                    if not isinstance(k, str):
                        continue
                    if k in _FIXED_OUTPUT_KEYS and out.get(k) != v:
                        _shadowed.append(k)
                    out[k] = v

            # save_as / step_vars promote 到 output namespace
            sv = getattr(sr, "step_vars", None) or {}
            out["vars"] = dict(sv)      # 永遠拿得到，不受撞名影響
            _promote(sv)
            # 把該步「JSON 輸出檔」的欄位提供給下游 condition/switch:
            #   - 完整物件掛 output.json.<key>(永遠可用、不會與 stdout/path/status 等固定 key 衝突)
            #   - 同時攤平到 output.<key>(讓 AI 助手直覺寫 output.口碑 即可用),但不覆寫固定 key/step_vars
            #   AI 助手常寫 output.<json欄位> 而原本讀不到 → 'dict object' has no attribute 'X';這裡補上。
            _jdata = _load_json_output(out.get("path") or "")
            if isinstance(_jdata, dict):
                out["json"] = _jdata
                _promote(_jdata)
            if _shadowed:
                import logging as _lg
                _lg.getLogger("pipeline").info(
                    f"[{sr.step_name}] 輸出欄位 {sorted(set(_shadowed))} 跟步驟中繼資料同名，"
                    f"`output.<欄位>` 用你的值；步驟本身的值請用 `output.step.<欄位>`。")
            steps_ns[sr.step_name] = {"output": out}

    # now 命名空間:排程報表最常見的需求是「選當月 / 上個月」。
    # 沒有它,使用者得為了拿個月份多寫一個 script 步驟。
    # month 等欄位是**補零字串**(08 不是 8) —— 下拉選單的選項文字就是這樣,
    # 拿去 uia_select 直接對得上;要算術再 |int。
    from datetime import date as _date
    _t = _date.today()
    _pm_y, _pm_m = (_t.year, _t.month - 1) if _t.month > 1 else (_t.year - 1, 12)
    _nm_y, _nm_m = (_t.year, _t.month + 1) if _t.month < 12 else (_t.year + 1, 1)
    _now_ns = {
        "year": str(_t.year), "month": f"{_t.month:02d}", "day": f"{_t.day:02d}",
        "date": _t.isoformat(),                    # 2026-08-24
        "prev_month": f"{_pm_m:02d}", "prev_month_year": str(_pm_y),
        "next_month": f"{_nm_m:02d}", "next_month_year": str(_nm_y),
    }
    ctx: dict[str, Any] = {
        "steps": steps_ns,
        "input": dict(input_params or {}),
        "now": _now_ns,
    }
    if env_passthrough:
        # 把整份 os.environ 暴露;secrets 之後另開 namespace,不直接讀環境變數
        ctx["env"] = dict(os.environ)
    else:
        ctx["env"] = {}
    # Secrets Vault:{{ secrets.名稱 }} — lazy、引用時才解密;不存在丟明確錯誤。
    # 注意:展開後的值會進入該欄位的執行環境(命令/參數),請用於 API key 等注入,
    # 不要把 secret 印到 stdout(log 會留)。
    try:
        from secrets_vault import SecretsNamespace
        ctx["secrets"] = SecretsNamespace()
    except Exception:
        pass  # vault 模組不可用(如缺 cryptography)→ 不注入,引用時會報 undefined
    return ctx


def eval_condition(expression: str, context: dict) -> bool:
    """求值 Jinja2 boolean expression、回傳 Python truthy/falsy。

    用於 condition 節點的 IF 模式。例:
        {{ steps.x.output.rows | int > 100 }}  → True / False
        {{ input.flag }}                        → 取決於 input.flag 是 truthy / falsy
        {{ "ok" in steps.api.output.stdout }}   → in 比對

    Args:
        expression: Jinja2 expression(可含 {{ }} 包覆、也可不包)
        context: 跟 render() 同份的 {steps, input, env}

    Raises:
        ExpressionError: 表達式語法錯 / 引用未定義變數
    """
    if not expression or not expression.strip():
        raise ExpressionError("condition expression 為空")

    # 容許使用者寫 `{{ x > 1 }}` 或純 `x > 1`,統一包成 `{{ ... }}` 給 Jinja2
    expr = expression.strip()
    import re as _re
    if not expr.startswith("{{"):
        expr = "{{ " + expr + " }}"
    elif not _re.fullmatch(r"\{\{((?!\}\}).)*\}\}", expr, _re.DOTALL):
        # 陷阱寫法:`{{ x }} == True`(比較式在 }} 外面)——Jinja 只渲染大括號內,
        # 結果變字串 "False == True" → 非空字串 → 永遠判 True(實測:msi_price_watch
        # changed=false 卻走了 on_true、整條報告寄信鏈白跑)。AI 規劃器很容易產生
        # 這種形式 → 拆掉內層 {{ }}、整句重包一次,讓比較真的進 Jinja 求值。
        inner = expr.replace("{{", " ").replace("}}", " ")
        expr = "{{ " + inner + " }}"

    rendered = render(expr, context)
    # Jinja2 render 把 True/False/数值 都會轉字串(例 "True" / "0" / "")
    # 用 Python 標準的 truthiness 規則:"True" / "true" / 非空非零字串 → True
    s = (rendered or "").strip()
    if s.lower() in ("true", "1", "yes", "y", "on"):
        return True
    if s.lower() in ("false", "0", "no", "n", "off", "", "none"):
        return False
    # 其他非空字串視為 True(跟 Python `bool(non_empty_str) is True` 行為一致)
    return True


def eval_value(expression: str, context: dict) -> str:
    """求值 Jinja2 expression、回傳字串(用於 Switch 節點的 switch 比對 key)。

    跟 eval_condition 不同:這裡保留原始 render 字串、不轉 bool。
    用於 Switch 節點:expression 求值後 str(value),用來比對 cases keys。
    """
    if not expression or not expression.strip():
        raise ExpressionError("switch expression 為空")
    expr = expression.strip()
    if not expr.startswith("{{"):
        expr = "{{ " + expr + " }}"
    return render(expr, context).strip()


def find_referenced_vars(s: str) -> list[str]:
    """掃字串裡所有 Jinja2 變數引用、回傳 dotted-path list。

    例:"python a.py {{ steps.x.output.path }} --date {{ input.date }}"
       → ["steps.x.output.path", "input.date"]

    用於 GET /workflows/{id}/variables — 列舉 workflow 引用了哪些變數。
    intra-step UIA 短變數會被忽略(不算 inter-step 變數)。
    """
    if not isinstance(s, str) or "{{" not in s:
        return []
    # 移除 intra-step pattern,避免被誤掃進來
    cleaned = _INTRA_STEP_VAR_RE.sub("", s)
    # 簡單規則:抓 {{ ... }} 裡第一個 dotted identifier
    refs = []
    for m in re.finditer(r"\{\{\s*([^}|]+?)\s*(?:\|.*?)?\s*\}\}", cleaned):
        expr = m.group(1).strip()
        # 取 expression 開頭的 dotted-path(忽略後面算術)
        dotted = re.match(r"([a-zA-Z_][\w.]*)", expr)
        if dotted:
            refs.append(dotted.group(1))
    return refs
