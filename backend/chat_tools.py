"""AI 助手 read-only 工具集。

由 /pipeline/chat 的 agent loop 在 LLM 要求時呼叫。LLM 透過 langchain bind_tools
看到這些工具的 docstring 與簽名、自己決定何時 call。

設計原則：
- 純讀（不寫 DB / 不執行 pipeline）— 安全可隨意呼叫
- 每個 tool 有合理 cap（log / yaml 都截長、避免 token 爆）
- 工具回傳 string（JSON 序列化或純文字）— LLM 看到的就是文字
- 名稱模糊配對（id 前綴、name 包含）— LLM 不需記精確 id
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool


def _resolve_workflow(query: str) -> tuple[Optional[dict], str]:
    """共用查找邏輯：先試 id 前綴 / 完整、再退到 name 包含（case-insensitive）。

    回傳 (workflow_dict | None, 訊息字串)。多筆命中時 workflow_dict=None、訊息列出候選。
    """
    from db import list_workflows
    if not query or not query.strip():
        return None, "query 為空。請提供工作流名稱或 id（前綴也行）"
    wfs = list_workflows() or []
    q = query.strip()
    matches = [w for w in wfs if (w.get("id") or "") == q or (w.get("id") or "").startswith(q)]
    if not matches:
        ql = q.lower()
        matches = [w for w in wfs if ql in (w.get("name") or "").lower()]
    if not matches:
        return None, f"找不到符合 '{q}' 的工作流"
    if len(matches) > 1:
        cand = ", ".join(f"{w.get('name')} (id={w.get('id')})" for w in matches[:8])
        return None, f"找到 {len(matches)} 個符合的、請用更精確的 query：{cand}"
    return matches[0], ""


@tool
def list_workflows() -> str:
    """列出系統內所有工作流的概況。

    使用時機：
    - 使用者問「有哪些工作流」「最近哪個失敗了」「列出所有測試案例」等。
    - 你不確定使用者指的是哪個工作流時、用這個 tool 看清單再判斷。

    回傳：JSON 陣列，每筆含：
      - id: 工作流 id（如 wf-abc123）
      - name: 顯示名稱
      - last_run: {run_id, status, started_at}（沒跑過則為 null）
    """
    from db import list_workflows as _list_wfs, list_runs as _list_runs
    wfs = _list_wfs() or []
    runs = _list_runs(limit=100) or []
    wf_latest: dict[str, dict] = {}
    for r in runs:
        wid = r.get("_workflow_id") or ""
        if wid and wid not in wf_latest:
            wf_latest[wid] = r
    out = []
    for wf in wfs:
        wid = wf.get("id") or ""
        r = wf_latest.get(wid)
        out.append({
            "id": wid,
            "name": wf.get("name"),
            "last_run": {
                "run_id": r.get("run_id"),
                "status": r.get("status"),
                "started_at": r.get("started_at"),
            } if r else None,
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


@tool
def get_workflow_yaml(query: str) -> str:
    """取得指定工作流的 YAML 內容。

    使用時機：
    - 使用者要看 / 修某工作流 YAML。
    - 你要分析某工作流結構（例：判斷它有哪些 step、用哪些節點類型）。

    Args:
        query: 工作流 name（模糊比對、case-insensitive）或 id（完整或前綴）。

    回傳：YAML 純文字，或錯誤訊息（找不到 / 多筆命中時請使用者縮窄 query）。
    """
    wf, err = _resolve_workflow(query)
    if not wf:
        return err
    yaml_text = wf.get("yaml") or ""
    if not yaml_text.strip():
        return f"工作流 '{wf.get('name')}' (id={wf.get('id')}) 的 YAML 是空的"
    return yaml_text


@tool
def get_recent_runs(query: str, limit: int = 5) -> str:
    """取得指定工作流最近的執行紀錄。

    使用時機：
    - 使用者問「X 工作流最近幾次跑得怎樣」「之前是不是有失敗過」等。
    - 你要找某 workflow 的歷史 run_id 給後續 get_run_log 用。

    Args:
        query: 工作流 name（模糊比對）或 id（完整或前綴）。
        limit: 回幾筆（預設 5、上限會自動 cap 到 20）。

    回傳：JSON 陣列，每筆含 run_id, status, started_at, ended_at。
    """
    wf, err = _resolve_workflow(query)
    if not wf:
        return err
    from db import list_runs as _list_runs
    n = max(1, min(int(limit or 5), 20))
    runs = _list_runs(limit=n, workflow_id=wf.get("id")) or []
    out = [
        {
            "run_id": r.get("run_id"),
            "status": r.get("status"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
        }
        for r in runs
    ]
    return json.dumps(out, ensure_ascii=False, indent=2)


@tool
def get_run_log(run_id: str, max_chars: int = 12000) -> str:
    """取得指定 run 的執行 log。

    使用時機：
    - 使用者問某次 run 為何失敗 / 細節。
    - 你診斷問題、要看 step 執行細節 / 錯誤訊息。

    Args:
        run_id: run id 完整字串、或前 8 字前綴（如 "55c0d04c"）。
        max_chars: 回多少字（預設 12000、上限 30000；超過會從末段截，因為 log
                   末尾通常是錯誤訊息所在處）。

    回傳：log 文字。內容超過 max_chars 時保留末段 + 「(前面 N 字截掉)」前綴。
    """
    # log 目錄解析集中在 pipeline.logger(優先 OUTPUT_BASE_PATH/pipeline_logs、fallback
    # 舊 backend/ai_output);不可自己寫死路徑、避免又跟實際寫入處不符(這 bug 犯過 3 次)。
    if not run_id or not run_id.strip():
        return "請提供 run_id（前 8 字也行）"
    from pipeline.logger import resolve_log_dirs as _rld, find_run_log as _frl
    if not _rld():
        return "log 目錄不存在(已找 OUTPUT_BASE_PATH/pipeline_logs 與 backend/ai_output/pipeline_logs)"
    hit = _frl(run_id)
    if hit is None:
        return f"找不到 run_id 含 '{run_id}' 的 log 檔"
    cap = max(2000, min(int(max_chars or 12000), 30000))
    text = hit.read_text(encoding="utf-8", errors="replace")
    if len(text) > cap:
        truncated_n = len(text) - cap
        text = f"... (前面 {truncated_n:,} 字截掉、保留末段)\n" + text[-cap:]
    return text


def _validate_subagent_roles_in_yaml(yaml_content: str) -> Optional[str]:
    """掃 YAML 內所有 subagent step、檢查 subagent_role 都存在於可用清單。

    回 None = 沒問題;回 string = 錯誤訊息(列未知 role + 可選 role + 提示)。

    為什麼擋在 save/create_workflow_yaml 寫入前:
    - AI 助手有時直接寫 subagent_role: boss 但根本沒先 create_subagent_role 建過
    - 雖然 step 執行時會 fail、但那是 workflow 跑到該步才爆、使用者已浪費 token
    - 寫入時就擋下、AI 收到錯誤訊息會回去先建 role 再重寫 yaml
    """
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(yaml_content) or {}
        steps = parsed.get("steps") or parsed.get("pipeline", {}).get("steps") or []
        used_roles: set[str] = set()
        for s in steps:
            if not isinstance(s, dict):
                continue
            if s.get("subagent") is True or s.get("subagent_role"):
                rid = (s.get("subagent_role") or "").strip()
                if rid:
                    used_roles.add(rid)
        if not used_roles:
            return None
        from pipeline.subagent_runner import load_roles
        available = set(load_roles().keys())
        unknown = sorted(used_roles - available)
        if unknown:
            return (
                f"YAML 含未存在的 subagent_role: {unknown}。\n"
                f"可用 role (內建 + 自訂):{sorted(available)}。\n"
                f"請**先**呼叫 create_subagent_role 工具(走兩步 confirm 協議)把這些 role 建好、"
                f"再重新呼叫本工具寫入 YAML。"
            )
        return None
    except Exception:
        # YAML 解析失敗會在後續 PipelineConfig 驗證階段擋下、這裡不重複報
        return None


@tool
def save_workflow_yaml(query: str, yaml_content: str, confirm: bool = False) -> str:
    """覆蓋既有 workflow YAML(重建畫布)、走兩步協議。

    Args:
        query: workflow name(模糊)或 id 前綴
        yaml_content: 完整 YAML
        confirm: False 預覽、True 真寫(等使用者明確同意才設 True)
    """
    wf, err = _resolve_workflow(query)
    if not wf:
        return err
    if not yaml_content or not yaml_content.strip():
        return "yaml_content 為空、不能寫入"

    # 試解析 YAML 跟轉 canvas、預覽用
    try:
        from yaml_to_canvas import yaml_to_canvas
        new_canvas = yaml_to_canvas(yaml_content)
    except Exception as e:
        return f"YAML 解析失敗：{type(e).__name__}: {str(e)[:200]}"
    if not new_canvas:
        return "YAML 解析後沒有有效 step、不寫入。檢查 YAML 結構（要有 steps 陣列）"

    # 試走 PipelineConfig schema 驗證
    try:
        import yaml as _yaml
        from pipeline.models import PipelineConfig
        parsed = _yaml.safe_load(yaml_content) or {}
        raw_cfg = parsed.get("pipeline", parsed)
        PipelineConfig.from_dict({k: v for k, v in raw_cfg.items() if not str(k).startswith("_")})
    except Exception as e:
        return f"YAML schema 驗證失敗、不寫入：{type(e).__name__}: {str(e)[:200]}"

    # subagent_role 存在性預驗(寫入前擋下、防 AI 漏建 role 直接寫 workflow)
    _role_err = _validate_subagent_roles_in_yaml(yaml_content)
    if _role_err:
        return f"⛔ subagent_role 驗證失敗、不寫入:\n{_role_err}"

    new_nodes = len(new_canvas.get("nodes") or [])
    old_yaml = wf.get("yaml") or ""
    old_canvas = wf.get("canvas") or {}
    old_nodes = len(old_canvas.get("nodes") or [])

    if not confirm:
        return (
            f"[PREVIEW 不寫入] 目標 workflow:'{wf.get('name')}' (id={wf.get('id')})\n"
            f"原 YAML {len(old_yaml):,} 字元 / {old_nodes} 節點\n"
            f"新 YAML {len(yaml_content):,} 字元 / {new_nodes} 節點\n"
            f"YAML 結構驗證通過、可以寫入。\n\n"
            f"⚠️ 請取得使用者明確同意（『yes』『OK』『套用』『好』等）後、再次呼叫本工具並設 confirm=True。"
        )

    # confirm=True、真寫
    try:
        from db import update_workflow
        update_workflow(wf.get("id"), {
            "yaml": yaml_content,
            "canvas": new_canvas,
        })
        return (
            f"✅ 已寫入 workflow '{wf.get('name')}' (id={wf.get('id')})\n"
            f"YAML {len(yaml_content):,} 字、{new_nodes} 節點\n"
            f"原 YAML 大小 {len(old_yaml):,} 字 / {old_nodes} 節點 (已被覆蓋)\n"
            f"請告訴使用者:套用完成。"
        )
    except Exception as e:
        return f"寫入失敗：{type(e).__name__}: {str(e)[:200]}"


@tool
def patch_node_actions(query: str, step_name: str, ops_json: str,
                       confirm: bool = False) -> str:
    """**定點修改**某個 computer_use 節點的動作序列(不重寫整份 YAML)。

    使用者在 UIA Inspector 上一個一個挑出來的 control(auto_id / name)是整份設定裡
    最貴的部分。用 save_workflow_yaml 整份覆蓋會把它們洗掉(你沒看過那些 id、
    重寫必然生錯)。要動動作序列一律用這個工具。

    Args:
        query: workflow 名稱(模糊)或 id 前綴
        step_name: 要改的步驟名(就是節點名稱)
        ops_json: 操作清單 JSON 陣列。⚠ **index 是對「前一個操作執行後」的清單算的**
                  —— 一次刪兩個要從後往前刪,否則第二個 index 會指到別的動作。
                  不確定就一次只送一個操作。支援五種:
          {"op":"append","action":{...}}                            尾端新增
          {"op":"insert","index":2,"action":{...}}                  插到第 index 個之前(0 起算)
          {"op":"set","index":1,"field":"text","value":"{{金額}}"}   改某動作的欄位
          {"op":"move","from":3,"to":0}                             搬動作(修「取值排在填值後面」)
          {"op":"delete","index":2}                                 刪動作
        confirm: False = 預覽(回改動摘要)、True = 真寫。**一定先預覽給使用者看**。

    使用者還沒挑控制項時,action 的 control 可以留空、改帶 description 說明
    「這格待指定」,然後在回覆裡告訴他去哪個面板挑。
    """
    import json as _json
    import re as _re
    import yaml as _yaml
    from db import update_workflow
    wf, err = _resolve_workflow(query)
    if not wf:
        return err

    try:
        ops = _json.loads(ops_json)
        if isinstance(ops, dict):
            ops = [ops]
        if not isinstance(ops, list) or not ops:
            return "ops_json 要是非空的 JSON 陣列"
    except Exception as e:
        return f"ops_json 不是合法 JSON:{e}"

    raw = wf.get("yaml") or ""
    if not raw.strip():
        return "這個工作流還沒有 YAML(可能是空白畫布)、沒有動作可改"
    try:
        doc = _yaml.safe_load(raw) or {}
    except Exception as e:
        return f"現有 YAML 解析失敗、不動它:{type(e).__name__}: {e}"

    root = doc.get("pipeline") if isinstance(doc.get("pipeline"), dict) else doc
    steps = root.get("steps")
    if not isinstance(steps, list):
        return "YAML 裡找不到 steps 陣列"

    _hits = [s for s in steps
             if isinstance(s, dict)
             and str(s.get("name") or "").strip() == step_name.strip()]
    if len(_hits) > 1:
        # 安靜改第一個 = 使用者以為改到 B 其實改到 A。而且重名的話
        # {{ steps.X.output.y }} 本來就指不明確,該先改名
        return (f"有 {len(_hits)} 個步驟都叫「{step_name}」,不知道要改哪一個。"
                f"請先把步驟改成不同名稱 —— 重名的話 {{{{ steps.X }}}} 引用本來就是壞的。")
    target = _hits[0] if _hits else None
    if target is None:
        names = [str(s.get("name") or "?") for s in steps if isinstance(s, dict)]
        return f"找不到步驟「{step_name}」。這個工作流的步驟有:{names}(名稱要完全一致)"
    if not target.get("computer_use"):
        return (f"步驟「{step_name}」不是桌面自動化節點、沒有動作序列。"
                f"要改別的節點請用 save_workflow_yaml。")

    actions = list(target.get("actions") or [])
    before = len(actions)
    log: list = []

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            return f"第 {i+1} 個操作不是物件"
        kind = str(op.get("op") or "").lower()
        try:
            if kind in ("append", "insert"):
                a = op.get("action") or {}
                if not a.get("type"):
                    return f"第 {i+1} 個 {kind} 缺 action.type"
                if kind == "append":
                    idx = len(actions)
                else:
                    idx = int(op.get("index", len(actions)))
                    # 越界不可夾擠 —— AI 常用 -1 表達「最後」,夾成 0 會插到最前面、
                    # 讓取值/填值順序整個反過來,而且不報錯
                    if not (0 <= idx <= len(actions)):
                        return (f"第 {i+1} 個 insert 的 index={idx} 超出範圍"
                                f"(目前 {len(actions)} 個動作,可填 0~{len(actions)};"
                                f"要加在最後請用 op=append)")
                actions.insert(idx, a)
                log.append(f"+ 新增 {a.get('type')} 到第 {idx+1} 位")
            elif kind == "set":
                idx = int(op.get("index", -1))
                if not (0 <= idx < len(actions)):
                    return f"第 {i+1} 個 set 的 index={idx} 超出範圍(共 {len(actions)} 個)"
                field = str(op.get("field") or "")
                if not field:
                    return f"第 {i+1} 個 set 缺 field"
                if "value" not in op:
                    # op.get("value") 會回 None,原本會把欄位寫成 null 而不報錯
                    return f"第 {i+1} 個 set 缺 value(要把欄位清掉請用 value: \"\")"
                old = actions[idx].get(field)
                actions[idx] = {**actions[idx], field: op.get("value")}
                log.append(f"~ 第 {idx+1} 個動作 {field}: {old!r} → {op.get('value')!r}")
            elif kind == "move":
                f_i, t_i = int(op.get("from", -1)), int(op.get("to", -1))
                if not (0 <= f_i < len(actions)) or not (0 <= t_i < len(actions)):
                    return f"第 {i+1} 個 move 的 index 超出範圍(共 {len(actions)} 個)"
                actions.insert(t_i, actions.pop(f_i))
                log.append(f"↕ 第 {f_i+1} 個動作移到第 {t_i+1} 位")
            elif kind == "delete":
                idx = int(op.get("index", -1))
                if not (0 <= idx < len(actions)):
                    return f"第 {i+1} 個 delete 的 index={idx} 超出範圍"
                a = actions.pop(idx)
                log.append(f"- 刪掉第 {idx+1} 個動作 {a.get('type')}")
            else:
                return f"第 {i+1} 個操作 op={kind!r} 不支援(只有 append/insert/set/move/delete)"
        except Exception as e:
            return f"第 {i+1} 個操作失敗:{type(e).__name__}: {e}"

    # 取值必須排在用值之前 —— 順序錯了變數是空的,而且不報錯,是靜默給錯值
    warn: list = []
    seen: set = set()
    for idx, a in enumerate(actions):
        blob = " ".join(str(a.get(k) or "") for k in ("text", "batch", "message"))
        for ref in _re.findall(r"\{\{\s*([^\s.}]+)\s*\}\}", blob):
            if ref not in seen:
                warn.append(f"第 {idx+1} 個動作用了 {{{{{ref}}}}},但前面沒有動作存過這個變數")
        sa = str(a.get("save_as") or "").strip()
        if sa:
            seen.add(sa)

    # 這兩種本來就不需要 control(uia_set_clipboard 根本不找控制項、
    # uia_close_window 沒給就用 step window),列進警告會叫使用者去挑一個不需要的東西
    _NO_CTRL_NEEDED = {"uia_set_clipboard", "uia_close_window"}
    missing = [f"第 {i+1} 個({a.get('type')})" for i, a in enumerate(actions)
               if str(a.get("type") or "").startswith("uia_")
               and str(a.get("type")) not in _NO_CTRL_NEEDED
               and not a.get("control") and not a.get("rect")]

    summary = (f"步驟「{step_name}」動作 {before} → {len(actions)} 個\n"
               + "\n".join("  " + x for x in log))
    if warn:
        summary += "\n⚠ 變數順序:\n" + "\n".join("  " + w for w in warn)
    if missing:
        summary += ("\n⚠ 還沒指定控制項(要使用者自己在 UIA Inspector 上挑):\n  "
                    + "、".join(missing))

    if not confirm:
        return ("[PREVIEW 不寫入]\n" + summary
                + "\n\n把這份摘要講給使用者聽、取得同意後再用 confirm=True 寫入。")

    target["actions"] = actions
    # 寫入前先過 schema —— 不驗的話 AI 塞錯欄位名或錯型別(例 text 給 dict)
    # 要等使用者按「執行」才炸,而且 ComputerUseAction 沒設 extra="forbid",
    # 用錯欄位名不會報錯、執行時那個動作等於什麼都沒做(靜默給錯結果)
    try:
        from pipeline.models import PipelineConfig
        _root = doc.get("pipeline") if isinstance(doc.get("pipeline"), dict) else doc
        PipelineConfig.from_dict(
            {k: v for k, v in _root.items() if not str(k).startswith("_")})
    except Exception as e:
        return (f"改完的內容過不了 schema 驗證、不寫入:{type(e).__name__}: {str(e)[:300]}\n"
                f"多半是 action 欄位名或型別錯了(填值的文字欄位是 text、不是 value)")
    try:
        new_yaml = _yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=4096)
    except Exception as e:
        return f"YAML 產生失敗、不寫入:{type(e).__name__}: {e}"
    # 畫布用既有 yaml_to_canvas 重生 —— canvas→YAML 的轉換只在前端,
    # 只更新其中一邊會讓兩者漂移(執行看 YAML、畫面看 canvas)
    try:
        from yaml_to_canvas import yaml_to_canvas
        new_canvas = yaml_to_canvas(new_yaml)
        if not new_canvas:
            return "改完的 YAML 轉不出畫布、不寫入(避免畫面與執行不一致)"
    except Exception as e:
        return f"改完的 YAML 轉畫布失敗、不寫入:{type(e).__name__}: {e}"
    try:
        update_workflow(wf.get("id"), {"yaml": new_yaml, "canvas": new_canvas})
    except Exception as e:
        return f"寫入失敗:{type(e).__name__}: {str(e)[:200]}"
    return ("✅ 已套用\n" + summary
            + "\n請告訴使用者:改好了,重新整理畫布就會看到。")


@tool
def create_workflow_yaml(name: str, yaml_content: str, confirm: bool = False) -> str:
    """**建立**新工作流(撞名拒絕、防誤覆蓋)、走兩步協議。

    與 save_workflow_yaml 差別:save_=更新既有覆蓋、create_=新建撞名失敗。

    Args:
        name: 新 workflow 名(撞名拒絕、要更新請用 save_workflow_yaml)
        yaml_content: 完整 YAML
        confirm: False 預覽、True 真建
    """
    name = (name or "").strip()
    if not name:
        return "name 為空、不能建"
    if not yaml_content or not yaml_content.strip():
        return "yaml_content 為空、不能建"

    # 1. 檢查 name 是否已存在
    from db import list_workflows as _list_wfs
    wfs = _list_wfs() or []
    same = [w for w in wfs if (w.get("name") or "").strip() == name]
    if same:
        existing = same[0]
        return (
            f"名稱「{name}」已存在(id={existing.get('id')}、{len((existing.get('canvas') or {}).get('nodes') or [])} 節點)。\n"
            "請改用 save_workflow_yaml 工具更新該工作流、或改用其他名稱再呼叫本工具。"
        )

    # 2. 解析 YAML + 驗證
    try:
        from yaml_to_canvas import yaml_to_canvas
        new_canvas = yaml_to_canvas(yaml_content)
    except Exception as e:
        return f"YAML 解析失敗:{type(e).__name__}: {str(e)[:200]}"
    if not new_canvas:
        return "YAML 解析後沒有有效 step、不建。檢查 YAML 結構(要有 steps 陣列)"
    try:
        import yaml as _yaml
        from pipeline.models import PipelineConfig
        parsed = _yaml.safe_load(yaml_content) or {}
        raw_cfg = parsed.get("pipeline", parsed)
        PipelineConfig.from_dict({k: v for k, v in raw_cfg.items() if not str(k).startswith("_")})
    except Exception as e:
        return f"YAML schema 驗證失敗、不建:{type(e).__name__}: {str(e)[:200]}"

    # subagent_role 存在性預驗(建立前擋下、防 AI 漏建 role 直接建 workflow)
    _role_err = _validate_subagent_roles_in_yaml(yaml_content)
    if _role_err:
        return f"⛔ subagent_role 驗證失敗、不建:\n{_role_err}"

    new_nodes = len(new_canvas.get("nodes") or [])
    if not confirm:
        return (
            f"[PREVIEW 不建] 將建立新工作流:'{name}'\n"
            f"YAML {len(yaml_content):,} 字、{new_nodes} 節點\n"
            f"YAML 結構驗證通過、可以建立。\n\n"
            f"⚠️ 請取得使用者明確同意(『yes』『建』『OK』等)後、再次呼叫本工具並設 confirm=True。"
        )

    # 3. confirm=True、真建
    try:
        from db import create_workflow as _create_wf, update_workflow as _update_wf
        # 先建空工作流(拿到 id)
        wf = _create_wf(name=name, canvas=new_canvas, validate=False)
        wf_id = wf.get("id")
        # 再把 yaml 寫進去(create_workflow 不一定接受 yaml param)
        _update_wf(wf_id, {"yaml": yaml_content, "canvas": new_canvas})
        return (
            f"✅ 已建立新工作流 '{name}' (id={wf_id})\n"
            f"YAML {len(yaml_content):,} 字、{new_nodes} 節點\n"
            f"請告訴使用者:工作流已建好、id 是 {wf_id}。可立即啟動或編輯。"
        )
    except Exception as e:
        return f"建立失敗:{type(e).__name__}: {str(e)[:200]}"


@tool
def create_subagent_role(
    role_id: str,
    label: str,
    description: str,
    tools: list[str],
    system_prompt: str,
    confirm: bool = False,
) -> str:
    """新增自訂 subagent role(寫到 custom_subagent_roles.yaml)、走兩步協議。

    Args:
        role_id: 英文 snake_case、不可撞內建(data_analyst/coder/researcher/critic/planner)
        label: 中文顯示名(畫布顯示)
        description: 一句話用途(UI 提示)
        tools: 從 run_python/run_shell/read_file/web_search/view_image/ask_user 挑(done 自動加)
        system_prompt: role 第一條 system message。寫**純語意**敘述職能+工作流、**不要寫 `<tool>` 文字格式範例**(native FC 自動處理、教 `<tool>` 反會讓 LLM 退回文字模式失敗)
        confirm: False 預覽、True 真寫
    """
    from pipeline.subagent_runner import (
        BUILTIN_ROLE_IDS, SELECTABLE_TOOLS, load_custom_roles, save_custom_roles,
    )
    import re as _re

    role_id = (role_id or "").strip()
    label = (label or "").strip()
    description = (description or "").strip()
    system_prompt = system_prompt or ""

    # 1. 欄位驗證
    if not _re.match(r"^[a-z][a-z0-9_]{1,39}$", role_id):
        return "role_id 必須英文 snake_case (小寫開頭、長 2-40、只能含 a-z 0-9 _)、不能空"
    if role_id in BUILTIN_ROLE_IDS:
        return f"role_id '{role_id}' 是內建角色名、不可使用。內建有:{sorted(BUILTIN_ROLE_IDS)}"
    if not label:
        return "label(中文顯示名)不能空"
    if not description:
        return "description(一句話用途)不能空"
    if not isinstance(tools, list):
        return "tools 必須是 list (例 ['run_python', 'read_file'])"
    _bad = [t for t in tools if t not in SELECTABLE_TOOLS]
    if _bad:
        return f"tools 含未知工具 {_bad};可選:{SELECTABLE_TOOLS}(done 會自動加)"
    if len(system_prompt.strip()) < 30:
        return "system_prompt 太短(至少 30 字)、要寫清楚角色職能 + 工作流 + 最高優先級違規規則"

    # 2. 撞名檢查
    existing = load_custom_roles()
    if role_id in existing:
        return (
            f"自訂角色 '{role_id}' 已存在。\n"
            f"要編輯請告訴使用者去設定頁的『Subagent 角色管理』、或刪除舊的再呼本工具。"
        )

    # 3. confirm=False → preview
    _tools_with_done = list(tools)
    if "done" not in _tools_with_done:
        _tools_with_done.append("done")
    if not confirm:
        return (
            f"[PREVIEW 不寫] 將新增自訂角色:\n"
            f"  role_id: {role_id}\n"
            f"  label: {label}\n"
            f"  description: {description}\n"
            f"  tools: {_tools_with_done}\n"
            f"  system_prompt: ({len(system_prompt)} 字、開頭 100 字: {system_prompt[:100]!r})\n"
            f"\n⚠️ 請取得使用者明確同意(『yes』『建』『OK』等)後、再次呼叫本工具並設 confirm=True。"
        )

    # 4. confirm=True → 真寫
    try:
        existing[role_id] = {
            "label": label,
            "description": description,
            "tools": _tools_with_done,
            "system_prompt": system_prompt,
        }
        save_custom_roles(existing)
        return (
            f"✅ 已新增自訂角色 '{role_id}' ({label})\n"
            f"tools: {_tools_with_done}\n"
            f"請告訴使用者:角色已可用、現在 workflow YAML 可以寫 subagent_role: {role_id}"
        )
    except Exception as e:
        return f"寫入失敗:{type(e).__name__}: {str(e)[:200]}"


@tool
async def start_workflow(query: str, confirm: bool = False) -> str:
    """啟動指定工作流(實際跑 pipeline)。

    ⚠️ 這是會實際執行的寫操作、**必須走兩步協議**(同 save_workflow_yaml):

    **步驟 1 (confirm=False、預覽)**:
    - 確認目標 workflow 存在、YAML 可解析
    - 接著向使用者確認:「要立即啟動 X 嗎?」

    **步驟 2 (confirm=True、實啟)**:
    - 使用者明確同意後才設 confirm=True

    Args:
        query: 目標 workflow name(模糊)或 id(前綴)。
        confirm: False=預覽 / True=實啟。預設 False。

    回傳:預覽資訊 或 run_id / 錯誤訊息。
    """
    wf, err = _resolve_workflow(query)
    if not wf:
        return err
    yaml_text = wf.get("yaml") or ""
    if not yaml_text.strip():
        return f"workflow '{wf.get('name')}' 的 YAML 是空的、無法啟動"

    if not confirm:
        # 預覽:解析 YAML 看 step 數量
        try:
            import yaml as _yaml
            parsed = _yaml.safe_load(yaml_text) or {}
            steps = parsed.get("steps") or []
            n_steps = len(steps)
            step_names = [s.get("name", "?") for s in steps[:5] if isinstance(s, dict)]
        except Exception as e:
            return f"YAML 解析失敗:{type(e).__name__}: {str(e)[:200]}"
        return (
            f"[PREVIEW 不啟動] 目標 workflow:'{wf.get('name')}' (id={wf.get('id')})\n"
            f"共 {n_steps} 步驟" + (f":{', '.join(step_names)}" if step_names else "") + "\n\n"
            f"⚠️ 請取得使用者明確同意後、再次呼叫本工具並設 confirm=True。"
        )

    # confirm=True、真啟動
    try:
        # 推測是否需要 validate
        import yaml as _yaml
        parsed = _yaml.safe_load(yaml_text) or {}
        steps = parsed.get("steps") or []
        # expect 巢在 output: 底下(expect / description 同義)、json_schema 也算要驗證;
        # 之前讀步驟頂層 s.get("expect") 永遠 miss → validate 只靠 YAML 明寫才會開
        def _step_has_gate(s):
            if not isinstance(s, dict):
                return False
            o = s.get("output")
            return isinstance(o, dict) and bool(
                o.get("expect") or o.get("description") or o.get("json_schema")
            )
        needs_validate = bool(parsed.get("validate")) or any(_step_has_gate(s) for s in steps)
        # 重用 main.start_pipeline 已寫好的全套邏輯(parse / validate / save run / 背景啟動)
        # 改 async tool 直接 await、不再 thread + asyncio.run(那會關掉 background task 的 loop、
        # pipeline 跑到一半被砍。最早的版本用 thread 是因為 tool 本來是 sync、
        # 改 async 後就能直接 await、background task 掛在主 event loop、能正常跑完)
        from main import start_pipeline, PipelineRunRequest
        req = PipelineRunRequest(
            yaml_content=yaml_text,
            validate=needs_validate,
            use_recipe=True,
            workflow_id=wf.get("id"),
            silent_recipe=True,  # 無人值守:不彈 recipe 確認 dialog
        )
        result = await start_pipeline(req)
        run_id = (result or {}).get("run_id") or "?"
        return (
            f"🚀 已啟動 workflow '{wf.get('name')}'\n"
            f"run_id: {run_id}\n"
            f"狀態:已送 runner 背景執行。\n"
            f"進度會自動推送到 TG、隨時可以 get_run_log 查細節。\n"
            f"\n💡 提示給使用者:在桌面右側欄點該 workflow、可以即時看執行狀態 + log。"
            f"桌面當前顯示的 workflow 不會自動切、避免打斷你正在編輯的內容。"
        )
    except Exception as e:
        return f"啟動失敗:{type(e).__name__}: {str(e)[:300]}"


@tool
def list_schedules() -> str:
    """列出所有排程任務(cron 定時)。

    使用時機:
    - 使用者問「我有哪些排程」「定時任務排了什麼」
    - 要 schedule_workflow / cancel_schedule 之前先看現況

    回傳:JSON 陣列、含 task_id / name / schedule_expr / next_run / last_run
    """
    import json as _json
    try:
        from scheduler.manager import list_tasks
        tasks = list_tasks() or []
    except Exception as e:
        return f"列排程失敗: {type(e).__name__}: {str(e)[:200]}"
    if not tasks:
        return "目前沒有任何排程任務。"
    out = []
    for t in tasks:
        out.append({
            "task_id": t.get("id", "?"),
            "name": t.get("name", ""),
            "schedule_type": t.get("schedule_type", ""),
            "schedule_expr": t.get("schedule_expr", ""),
            "next_run": t.get("next_run", ""),
            "last_run": t.get("last_run", ""),
        })
    return _json.dumps(out, ensure_ascii=False, indent=2)


@tool
async def schedule_workflow(query: str, schedule_expr: str, confirm: bool = False) -> str:
    """為 workflow 建 cron 排程、走兩步協議。

    Args:
        query: workflow name(模糊)或 id 前綴
        schedule_expr: cron 5 欄表達式(分 時 日 月 週、例 "0 9 * * 1-5" = 週一至五 9 點)
        confirm: False 預覽、True 真建
    """
    wf, err = _resolve_workflow(query)
    if not wf:
        return err
    yaml_text = wf.get("yaml") or ""
    if not yaml_text.strip():
        return f"workflow '{wf.get('name')}' 的 YAML 是空的、無法排程"

    # 解析 cron 給人看的描述(簡化、不全面)
    parts = schedule_expr.strip().split()
    if len(parts) != 5:
        return f"cron 表達式格式錯誤(應為 5 個欄位:分 時 日 月 週)、收到:{schedule_expr!r}"

    if not confirm:
        # 給可讀描述
        m, h, d, mo, w = parts
        if m == "0" and h.isdigit() and d == "*" and mo == "*" and w == "*":
            human = f"每天 {h.zfill(2)}:00"
        elif m == "0" and h.isdigit() and d == "*" and mo == "*" and w == "1-5":
            human = f"週一至週五 {h.zfill(2)}:00"
        elif m == "0" and h.startswith("*/"):
            human = f"每 {h[2:]} 小時"
        else:
            human = "(自訂規則)"
        return (
            f"[PREVIEW 不建立] 排程:工作流 '{wf.get('name')}' (id={wf.get('id')})\n"
            f"cron:{schedule_expr}({human})\n"
            f"⚠️ 請取得使用者明確同意後、再次呼叫並設 confirm=True。"
        )

    # confirm=True、實建
    try:
        from main import create_pipeline_schedule, PipelineScheduleRequest
        req = PipelineScheduleRequest(
            name=wf.get("name"),
            yaml_content=yaml_text,
            schedule_type="cron",
            schedule_expr=schedule_expr,
            validate=False,
            use_recipe=True,
            workflow_id=wf.get("id"),
        )
        result = await create_pipeline_schedule(req)
        task = (result or {}).get("task") or {}
        task_id = task.get("task_id") or "?"
        next_run = task.get("next_run") or "?"
        return (
            f"✅ 已建立排程:'{wf.get('name')}'\n"
            f"task_id: {task_id}\n"
            f"cron: {schedule_expr}\n"
            f"下次執行: {next_run}\n"
            f"請告訴使用者:排程已生效、會自動執行(無人值守、不彈 dialog)。"
        )
    except Exception as e:
        return f"排程建立失敗: {type(e).__name__}: {str(e)[:300]}"


@tool
async def cancel_schedule(task_id_or_name: str, confirm: bool = False) -> str:
    """取消(刪除)某個 cron 排程。

    ⚠️ 寫操作(刪除)、**必須走兩步協議**:
    步驟 1 (confirm=False):用 list_schedules 找對應 task 並預覽
    步驟 2 (confirm=True):用戶明確同意才取消

    Args:
        task_id_or_name: 排程 task_id 或 workflow name(模糊比對)
        confirm: False=預覽 / True=實刪
    """
    q = (task_id_or_name or "").strip()
    if not q:
        return "task_id_or_name 為空"
    try:
        from scheduler.manager import list_tasks
        tasks = list_tasks() or []
    except Exception as e:
        return f"載入排程清單失敗: {e}"
    matches = [t for t in tasks if t.get("id", "") == q]
    if not matches:
        ql = q.lower()
        matches = [t for t in tasks if ql in (t.get("name", "") or "").lower()]
    if not matches:
        return f"找不到符合 '{q}' 的排程任務"
    if len(matches) > 1:
        names = ", ".join(f"{t.get('name')}(task_id={t.get('id')})" for t in matches[:5])
        return f"多個符合、請用更精確的:{names}"
    t = matches[0]
    if not confirm:
        return (
            f"[PREVIEW 不刪] 排程:'{t.get('name')}' task_id={t.get('id')} cron={t.get('schedule_expr')}\n"
            f"⚠️ 取得使用者明確同意後、再次呼叫並設 confirm=True 才刪除。"
        )
    try:
        from scheduler.manager import remove_task
        ok = remove_task(t.get("id"))
        if ok:
            return f"✅ 已刪除排程 '{t.get('name')}' (task_id={t.get('id')})"
        return f"刪除失敗:找不到 task_id={t.get('id')}"
    except Exception as e:
        return f"刪除失敗: {type(e).__name__}: {str(e)[:200]}"


def _resolve_workflow_output_dir(workflow_name: str):
    """找某 workflow 的輸出資料夾。
    重用 runner 既有邏輯:`<專案根>/ai_output/<workflow_name>/`(workflow_name = pipeline.name)。

    安全:workflow_name 來自 DB、可能含 `../` 等惡意內容。確認 resolve 後的目標路徑
    必須在 `<proj_root>/ai_output/` 真實底下(用 resolve()+relative_to() 防 traversal)。

    回傳 (Path | None, 訊息)。
    """
    try:
        from pipeline.runner import _workflow_output_dir
    except Exception as e:
        return None, f"載入 _workflow_output_dir 失敗: {e}"
    target = _workflow_output_dir(workflow_name)
    if not target:
        return None, "workflow_name 為空"

    # 真正的 ai_output root(固定、不被 workflow_name 影響)
    # __file__ 在 backend/chat_tools.py、parent.parent = proj_root/
    ai_output_root = (Path(__file__).parent.parent / "ai_output").resolve()
    try:
        target_resolved = target.resolve()
        target_resolved.relative_to(ai_output_root)  # 不在 ai_output 底下會 raise
    except (ValueError, OSError):
        return None, f"路徑越界、拒絕存取: {target}(必須在 {ai_output_root} 底下)"

    if not target.exists() or not target.is_dir():
        return None, f"輸出資料夾不存在: {target}"
    # per-run 子資料夾:新版每次執行的產物落在 <工作流>/run_<時間戳>/。
    # 若本層底下有 run_*/ 子夾 → 挑「最近修改」那個當作要找檔的目錄(= 最新一次執行)。
    # 舊版工作流把檔案直接放本層的、沒有 run_*/ → 維持回傳本層(向後相容)。
    try:
        run_dirs = [d for d in target.iterdir() if d.is_dir() and d.name.startswith("run_")]
        if run_dirs:
            latest = max(run_dirs, key=lambda d: d.stat().st_mtime)
            return latest, ""
    except OSError:
        pass
    return target, ""


@tool
def send_file_to_tg(workflow_query: str, filename: str = "", confirm: bool = False) -> str:
    """從 workflow 輸出資料夾抓檔傳 TG、走兩步協議。50MB cap、限定 OUTPUT_BASE_PATH 內。

    Args:
        workflow_query: workflow name(模糊)或 id 前綴
        filename: 完整檔名;空 → 列所有檔讓使用者選
        confirm: False 預覽、True 真送
    """
    # 1. 找 workflow
    wf, err = _resolve_workflow(workflow_query)
    if not wf:
        return err
    wf_name = wf.get("name") or ""

    # 2. 找預設輸出資料夾(主要候選位置)
    out_dir, err = _resolve_workflow_output_dir(wf_name)
    # out_dir 可能不存在(workflow 把輸出寫到別的目錄、或未跑過)、不直接 fail

    # 3. 收集候選檔案池:
    #    a) workflow 預設輸出目錄內的(若存在)
    #    b) 該 workflow 最近成功 run 的 step actual_output_path 提到的(允許跨目錄、但仍須在 ai_output/ 內)
    from pathlib import Path as _P
    try:
        from config import OUTPUT_BASE_PATH
        ai_output_root = _P(OUTPUT_BASE_PATH).resolve()
    except Exception:
        # fallback:猜測為 <project>/ai_output(跟 runner._workflow_output_dir 一致)
        ai_output_root = _P(__file__).parent.parent / "ai_output"
        ai_output_root = ai_output_root.resolve()
    # 工作流真實輸出基底(runner 用的、跟 ai_output_root 不同):
    runner_ai_output = None
    if out_dir is not None:
        runner_ai_output = out_dir.parent.resolve()

    file_pool: dict[str, _P] = {}  # display_name → Path(無重複)
    if out_dir and out_dir.exists() and out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.is_file():
                file_pool[p.name] = p

    # 從 step_results 補檔(可能在 ai_output 別的子目錄)
    step_to_files: dict[str, str] = {}  # display_name → step_name
    final_output_display: str | None = None
    try:
        from db import list_runs
        runs = list_runs(limit=20, workflow_id=wf.get("id")) or []
        success_runs = [r for r in runs if r.get("status") == "completed"]
        if success_runs:
            latest_ok = success_runs[0]
            step_results = latest_ok.get("step_results") or []
            for sr in step_results:
                op = (sr.get("actual_output_path") or "").strip()
                if not op:
                    continue
                op_path = _P(op)
                if not op_path.exists() or not op_path.is_file():
                    continue
                # 安全:必須在 ai_output 任一 root 底下
                op_resolved = op_path.resolve()
                in_scope = False
                for root in (ai_output_root, runner_ai_output):
                    if root is None:
                        continue
                    try:
                        op_resolved.relative_to(root)
                        in_scope = True
                        break
                    except Exception:
                        continue
                if not in_scope:
                    continue
                # display_name:若在預設目錄就用 name、否則加上子目錄前綴(讓 AI 看清來自哪)
                if out_dir and out_dir.exists():
                    try:
                        rel = op_path.resolve().relative_to(out_dir.resolve())
                        display = str(rel).replace("\\", "/")
                    except Exception:
                        # 不在預設目錄、用 <subdir>/<name>
                        display = f"{op_path.parent.name}/{op_path.name}"
                else:
                    display = f"{op_path.parent.name}/{op_path.name}"
                file_pool[display] = op_path
                step_to_files[display] = sr.get("step_name") or ""
            # 推測「主要產出」:從末段往前掃、第一個「非寄送/通知類 + > 1KB」的 step 產出
            # 邏輯:
            # - 反向掃 step_results、優先末段(下游 step、通常是處理過的成品)
            # - 跳過寄送/通知類(產出通常只是回執)
            # - 跳過 < 1KB(空殼)
            _delivery_keywords = ("寄送", "寄信", "傳送", "通知", "上傳", "送出", "send", "email", "mail",
                                  "deliver", "notify", "publish", "upload", "post")
            for sr in reversed(step_results):
                op = (sr.get("actual_output_path") or "").strip()
                if not op:
                    continue
                op_path = _P(op)
                if not op_path.exists():
                    continue
                step_name_lower = (sr.get("step_name") or "").lower()
                if any(kw in step_name_lower for kw in _delivery_keywords):
                    continue
                try:
                    size = op_path.stat().st_size
                except Exception:
                    continue
                if size < 1024:
                    continue
                # 找對應 display 並設為主要產出
                for d, p in file_pool.items():
                    try:
                        if p.resolve() == op_path.resolve():
                            final_output_display = d
                            break
                    except Exception:
                        continue
                if final_output_display:
                    break
            if not final_output_display:
                # 保底:沒任何「主要」候選 → 用最後一步的產出
                for sr in reversed(step_results):
                    op = (sr.get("actual_output_path") or "").strip()
                    if not op:
                        continue
                    op_path = _P(op)
                    if not op_path.exists():
                        continue
                    for d, p in file_pool.items():
                        try:
                            if p.resolve() == op_path.resolve():
                                final_output_display = d
                                break
                        except Exception:
                            continue
                    if final_output_display:
                        break
    except Exception:
        pass

    if not file_pool:
        return (
            f"workflow '{wf_name}' 找不到任何輸出檔。\n"
            f"預設目錄: {out_dir if out_dir else '(未確定)'}\n"
            f"最近 run 的 step_results 也沒記到產出檔。\n"
            f"可改用 get_recent_runs 看跑過沒有 / get_run_log 看細節。"
        )

    # 排序:按 mtime 倒序
    all_files_sorted = sorted(
        file_pool.items(),
        key=lambda kv: kv[1].stat().st_mtime, reverse=True,
    )

    # 4. 沒給 filename → 列清單(預覽用)
    from datetime import datetime
    now_ts = datetime.now().timestamp()

    def _fmt_age(mtime: float) -> str:
        sec = max(0, now_ts - mtime)
        if sec < 90: return f"{int(sec)}s 前"
        if sec < 5400: return f"{int(sec / 60)} 分鐘前"
        if sec < 90000: return f"{int(sec / 3600)} 小時前"
        return f"{int(sec / 86400)} 天前"

    if not filename or not filename.strip():
        header_dir = f"{out_dir.name}/" if out_dir else "(預設目錄不存在、列出 step_results 提到的檔)"
        lines = [f"workflow '{wf_name}' 全部產出 (按時間倒序、共 {len(all_files_sorted)} 個):"]
        for display, p in all_files_sorted[:20]:
            try:
                size_kb = p.stat().st_size / 1024
                age = _fmt_age(p.stat().st_mtime)
            except Exception:
                size_kb = 0
                age = "?"
            step_label = step_to_files.get(display)
            tags = []
            if final_output_display and display == final_output_display:
                tags.append("📌 主要產出")
            if step_label:
                tags.append(f"來自步驟「{step_label}」")
            tag_str = f" — {' | '.join(tags)}" if tags else ""
            lines.append(f"  • {display} ({size_kb:,.1f} KB, {age}){tag_str}")
        if len(all_files_sorted) > 20:
            lines.append(f"  ... 還有 {len(all_files_sorted) - 20} 個")

        if final_output_display:
            lines.append(f"\n💡 推測主要產出 = `{final_output_display}`(排除寄送/通知類步驟、選最大實質內容檔)。")
            lines.append("使用者若沒明確指定、優先送這份。如果他要的是寄送回執之類、再按需要選別的。")
        else:
            lines.append("\n💡 沒有成功 run 紀錄、推不出主要產出。建議按時間最新的優先。")
        lines.append("呼叫 send_file_to_tg 時 filename 用上面顯示的名稱(含 / 子目錄前綴也 OK)、先 confirm=False 預覽再 confirm=True 送。")
        return "\n".join(lines)

    # 5. 找指定 filename
    target_name = filename.strip().replace("\\", "/")
    # 完整 display name 命中(優先)
    matches: list[tuple[str, _P]] = []
    for d, p in all_files_sorted:
        if d == target_name or d.replace("\\", "/") == target_name:
            matches.append((d, p))
    # 退到 basename 命中
    if not matches:
        bn = target_name.split("/")[-1]
        for d, p in all_files_sorted:
            if d.split("/")[-1] == bn or p.name == bn:
                matches.append((d, p))
    # 退到模糊比對(case-insensitive、子字串)
    if not matches:
        ql = target_name.lower()
        for d, p in all_files_sorted:
            if ql in d.lower() or ql in p.name.lower():
                matches.append((d, p))
    if not matches:
        return f"找不到符合 '{target_name}' 的檔案。可用清單: {[d for d, _ in all_files_sorted[:10]]}"
    if len(matches) > 1:
        return f"模糊比對到多個檔案、請用更精確的 filename: {[d for d, _ in matches[:8]]}"

    target_display, target = matches[0]
    size_bytes = target.stat().st_size
    size_kb = size_bytes / 1024

    # 6. confirm=False → 預覽(不送)
    final_marker = " (📌 主要產出)" if target_display == final_output_display else ""
    step_marker = step_to_files.get(target_display)
    step_str = f"\n步驟: 「{step_marker}」" if step_marker else ""
    if not confirm:
        return (
            f"[PREVIEW 不送] 目標檔案: {target_display}{final_marker}\n"
            f"workflow: '{wf_name}'{step_str}\n"
            f"路徑: {target}\n"
            f"大小: {size_kb:,.1f} KB ({size_bytes:,} bytes)\n"
            f"⚠️ 請取得使用者明確同意、再次呼叫本工具並設 confirm=True。\n"
            f"（TG 單檔上限 50 MB、超過會直接失敗）"
        )

    # 7. confirm=True → 真送
    # TG send_document 上限 50 MB
    if size_bytes > 50 * 1024 * 1024:
        return f"檔案 {size_kb:,.1f} KB 超過 TG 50 MB 上限、無法送"

    try:
        from pipeline.runner import _get_tg_token, _get_tg_chat_id, _prepare_tg_file_with_bom
        from telegram import Bot
        import asyncio, os as _os, logging as _logging
        token = _get_tg_token()
        chat_id = _get_tg_chat_id()
        if not token or not chat_id:
            return "Telegram 未設定 (token / chat_id 缺)、無法送"

        # 文字檔(.md / .txt / .csv …)送 TG 前注入 UTF-8 BOM、避免 iOS TG 解成 Big5 亂碼
        _send_path, _temp_to_cleanup = _prepare_tg_file_with_bom(
            str(target), _logging.getLogger("chat_tools"), target.name,
        )

        async def _do_send():
            async with Bot(token=token) as bot:
                with open(_send_path, "rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=target.name,
                        caption=f"📎 {target.name} (來自 {wf_name})",
                    )

        # 從 sync tool 呼叫 async function:用既有 event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在 agent loop 內(已有 event loop)、用 task 排程
                # 但我們要等結果 — sync tool 必須阻塞等
                # 解法:在新 thread 跑 asyncio.run
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(asyncio.run, _do_send())
                    future.result(timeout=120)
            else:
                loop.run_until_complete(_do_send())
        except RuntimeError:
            asyncio.run(_do_send())

        # cleanup BOM-injected temp(若有)
        if _temp_to_cleanup:
            try:
                _os.unlink(_temp_to_cleanup)
            except Exception:
                pass

        return (
            f"✅ 已送 {target.name} 到 Telegram (chat_id={chat_id})\n"
            f"大小: {size_kb:,.1f} KB\n"
            f"請告訴使用者:檔案已傳送。"
        )
    except Exception as e:
        return f"送檔失敗: {type(e).__name__}: {str(e)[:300]}"


@tool
def web_search(query: str, max_results: int = 5, full_content: bool = False) -> str:
    """搜尋網路、回頭幾筆相關結果(用 Tavily API)。

    使用時機(限定工作流相關研究):
    - 使用者要規劃工作流、需要查站點結構/URL/RSS 等(例:「ithome 的 RSS 在哪」)
    - LLM 不知道某個 Python 套件的最新 API、版本、用法(例:「crawl4ai 0.8 新增的功能」)
    - 不知道某個依賴怎麼裝、有什麼替代品
    - 工作流任務需要的具體技術 / 服務細節

    **不要在這些場合用**(會浪費 quota + 漂離主題):
    - 使用者問通用聊天話題(天氣、股價、新聞)→ 簡短回答 + 引導回工作流主題、不要 search
    - 已經在你常識內的事(Python 基本語法、HTTP 概念)
    - 使用者問你身分 / 用途等元問題

    Args:
        query: 搜尋關鍵字
        max_results: 回幾筆結果(1-5、預設 5)
        full_content: True=拿全文(15KB)、貴慢但細節完整;False=只拿摘要(500 字)、快輕量

    回傳:answer + 來源 URL 清單(+ 全文若 full_content=True)、或錯誤訊息
    """
    try:
        from settings import get_settings
    except Exception as e:
        return f"[web_search 錯誤] 載入 settings 失敗:{e}"
    s = get_settings()
    if not s.get("web_search_enabled"):
        return "[web_search 錯誤] 網路搜尋未啟用(到設定頁開啟)"
    key = (s.get("tavily_api_key") or "").strip()
    if not key:
        return "[web_search 錯誤] Tavily API key 未設定(到設定頁填入)"
    q = (query or "").strip()
    if not q:
        return "[web_search 錯誤] query 不可為空"
    # 完整內容模式:caller 沒明確要 full、就看設定頁的「完整內容模式」開關
    # (修:原本只讀 caller 參數、UI 開了也沒用 — 對齊 _skill_web_search 的設定讀取)
    if not full_content:
        full_content = bool(s.get("web_search_full_content_default", False))
    n = max(1, min(int(max_results or 5), 8))

    # 搜尋深度:讀 settings.web_search_deep_default(預設 True、advanced 模式)
    # Atlas 定位深度研究、預設 ON;帳單失控時設定頁關掉。
    _deep = bool(s.get("web_search_deep_default", True))
    _depth = "advanced" if _deep else "basic"

    import requests as _requests
    try:
        resp = _requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": q,
                "max_results": n,
                "search_depth": _depth,
                "include_answer": True,
                "include_raw_content": bool(full_content),
            },
            timeout=60 if (full_content or _deep) else 20,
        )
        if resp.status_code == 401:
            return "[web_search 錯誤] Tavily API key 無效(401)、請更新"
        if resp.status_code == 429:
            return "[web_search 錯誤] Tavily 配額用盡或速率受限(429)、請稍後或換 plan"
        resp.raise_for_status()
        data = resp.json()
    except _requests.Timeout:
        return "[web_search 錯誤] Tavily 連線逾時、請稍後再試"
    except _requests.HTTPError:
        return f"[web_search 錯誤] Tavily HTTP {resp.status_code}:{resp.text[:200]}"
    except Exception as e:
        return f"[web_search 錯誤] Tavily 呼叫失敗:{type(e).__name__}: {str(e)[:200]}"

    answer = (data.get("answer") or "").strip()
    results = data.get("results") or []
    mode_tag = "full" if full_content else "light"
    lines = [f"[web_search] query=\"{q[:80]}\" (mode={mode_tag})"]
    if answer:
        lines.append(f"answer: {answer}")
    lines.append("")
    lines.append(f"來源 (共 {len(results)} 項):")
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()[:120]
        url = (r.get("url") or "").strip()
        lines.append(f"[{i}] {title} — {url}")
        if full_content:
            raw = (r.get("raw_content") or r.get("content") or "").strip()
            if raw:
                lines.append(f"    {raw[:6000]}{'...' if len(raw) > 6000 else ''}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# Subagent 派出 / 查狀態(非同步、in-memory registry)
# 讓 chat agent 能在對話中派子代理進沙盒寫程式 / 跑測試、立即釋放對話、
# 之後可隨時查狀態。Phase 1: dispatch + check_status。Phase 2(in-flight digest)
# 在 main.py 的 _build_pipeline_system_prompt 注入。
# ─────────────────────────────────────────────────────────────────

# in-memory registry：{task_id: {state, started_at, ended_at, role, task,
#                                working_dir, run_id, result}}
# 重啟 backend 會清空(可接受、用戶通常單一 session 內互動)
_chat_subagents: dict[str, dict] = {}

# 全 registry 上限(避免 in-memory 爆)；超過時把最舊已完成的丟掉
_SUBAGENT_REGISTRY_CAP = 50


def _trim_registry() -> None:
    if len(_chat_subagents) <= _SUBAGENT_REGISTRY_CAP:
        return
    # 保 in-flight + 最近 N 個 done
    done = [(tid, info) for tid, info in _chat_subagents.items()
            if info.get("state") in ("completed", "failed")]
    done.sort(key=lambda x: x[1].get("ended_at", 0))
    drop = len(_chat_subagents) - _SUBAGENT_REGISTRY_CAP
    for tid, _ in done[:drop]:
        _chat_subagents.pop(tid, None)


VALID_SUBAGENT_ROLES = ("data_analyst", "coder", "researcher", "critic", "planner")


@tool
async def dispatch_subagent_async(
    role: str,
    task: str,
    working_dir: str = "",
    max_iter: int = 8,
    follow_up: Optional[list[dict]] = None,
) -> str:
    """派子代理進沙盒非同步跑 ad-hoc 任務(寫 code / debug / 分析)、立即回 task_id。

    Args:
        role: data_analyst / coder / researcher / critic / planner
        task: 自然語言任務描述
        working_dir: 工作目錄,留空 → 自動 ai_output/chat-adhoc/<ts>_<id>/
        max_iter: 最大輪數,預設 8、複雜 10-15、勿低於 5
        follow_up: chain 模式、list[{role, task, max_iter}]、共用 working_dir

    後續:check_subagent_status(task_id) 查;細則見 system prompt「派子代理 vs 建 workflow」。
    """
    import asyncio
    import time as _time
    import uuid as _uuid
    from datetime import datetime
    from pathlib import Path
    from pipeline.subagent_runner import run_subagent
    from config import OUTPUT_BASE_PATH

    role = (role or "").strip().lower()
    if role not in VALID_SUBAGENT_ROLES:
        return f"❌ 不支援的 role={role!r}。可選:data_analyst / coder / researcher / critic / planner"
    if not task or not task.strip():
        return "❌ task 不能為空、請描述要子代理做什麼"

    # 驗 follow_up 格式
    follow_up = follow_up or []
    if not isinstance(follow_up, list):
        return f"❌ follow_up 必須是 list[dict]、收到 {type(follow_up).__name__}"
    for i, step in enumerate(follow_up):
        if not isinstance(step, dict):
            return f"❌ follow_up[{i}] 必須是 dict 含 role + task、收到 {type(step).__name__}"
        sr = (step.get("role") or "").strip().lower()
        if sr not in VALID_SUBAGENT_ROLES:
            return f"❌ follow_up[{i}].role={sr!r} 不支援。可選:{VALID_SUBAGENT_ROLES}"
        if not (step.get("task") or "").strip():
            return f"❌ follow_up[{i}].task 不能為空"

    # 並發 cap (#4)：避免使用者一聲令下派 10 個 ad-hoc 子代理擠爆沙盒。
    # 沙盒層另有 SANDBOX_MAX_CONCURRENT semaphore(預設 3)、那是 docker exec 級;
    # 這層是「dispatch 路徑」級、防止 in-flight subagent 過多
    _IN_FLIGHT_CAP = 3
    running_count = sum(1 for info in _chat_subagents.values()
                        if info.get("state") == "running")
    if running_count >= _IN_FLIGHT_CAP:
        running_ids = [tid for tid, info in _chat_subagents.items()
                       if info.get("state") == "running"][:5]
        return (
            f"❌ 已有 {running_count} 個子代理在跑、達到上限 {_IN_FLIGHT_CAP}。\n"
            f"in-flight task_ids: {', '.join(running_ids)}\n"
            f"先 check_subagent_status 看其中一個狀態、等完成再派。"
        )

    task_id = _uuid.uuid4().hex[:12]
    if not working_dir or not working_dir.strip():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        working_dir = f"ai_output/chat-adhoc/{ts}_{task_id[:6]}"

    # 解析絕對路徑(支援相對於 OUTPUT_BASE_PATH 的父目錄、跟 pipeline runner 邏輯一致)
    # 強制 .resolve() 確保 absolute、避免 sandbox docker exec -w 拿到 relative path
    # 撞「OCI runtime exec failed: Cwd must be an absolute path」
    wd = Path(working_dir)
    if not wd.is_absolute():
        # ai_output 已是 OUTPUT_BASE_PATH、外面包個 ai_output/ prefix 視為相對於 root
        if working_dir.startswith("ai_output/"):
            wd = OUTPUT_BASE_PATH.parent / working_dir
        else:
            wd = OUTPUT_BASE_PATH / working_dir
    wd = wd.resolve()
    # 安全容器化:working_dir 一律限制在輸出目錄內。擋絕對路徑(C:\Windows…)與 .. 逃逸,
    # 否則子代理 cwd 可讀寫專案外任意 host 目錄(對比 read_subagent_file/send_subagent_file_to_tg
    # 都有 relative_to 檢查,此處原本獨缺)。
    _out_base = OUTPUT_BASE_PATH.resolve()
    if not (wd == _out_base or wd.is_relative_to(_out_base)):
        return json.dumps({
            "ok": False,
            "error": f"working_dir 必須位於輸出目錄({_out_base})之內;已擋下越界路徑:{working_dir}",
        }, ensure_ascii=False)
    wd.mkdir(parents=True, exist_ok=True)

    chain_total = 1 + len(follow_up)
    _chat_subagents[task_id] = {
        "state": "running",
        "started_at": _time.time(),
        "ended_at": None,
        "role": role,
        "task": task[:500],
        "working_dir": str(wd),
        "run_id": f"chat-{task_id}",
        "max_iter": max_iter,
        "result": None,
        # chain metadata（無 follow_up 時 chain_total=1、等同單一 task）
        "follow_up": list(follow_up),
        "chain_position": 0,
        "chain_total": chain_total,
        "chain_root_id": task_id,
    }
    _trim_registry()

    # 把 working_dir 明確 prepend 到 task prompt、覆蓋 subagent_runner 的 sandbox
    # hint「專案根目錄為基準」(那段對 chat-adhoc 會誤導:LLM 看到 task 內路徑會用
    # Atlas root 為 anchor 算 absolute、結果寫去專案根而非 cwd)
    try:
        from pipeline.sandbox import windows_to_wsl_path
        wd_wsl = windows_to_wsl_path(str(wd)) or str(wd)
    except Exception:
        wd_wsl = str(wd)
    augmented_task = (
        f"⚠️ 路徑規則(很重要、優先此規則):\n"
        f"- 你在 Linux Docker container 內、**cwd 已被 docker exec 設為**:\n"
        f"  `{wd_wsl}`\n"
        f"- **寫檔請用 relative path 直接寫進 cwd**、例如:\n"
        f"  `Path('calculator.py').write_text(...)` ← 直接落在 cwd 內、不要加任何前綴\n"
        f"- **不要**自己算 absolute path、**不要**用「專案根目錄」當 anchor、\n"
        f"  **不要**寫到 `/mnt/c/.../Atlas/...`\n"
        f"- 任務描述內的相對路徑(例如 'ai_output/calc_v2/')是**訊息級別**的描述、\n"
        f"  你的 cwd 已經幫你解到對的位置了、寫檔時忽略那段 prefix、用 basename 即可\n"
        f"\n"
        f"任務:\n{task}"
    )

    async def _runner():
        try:
            result = await run_subagent(
                role_name=role,
                task=augmented_task,
                max_iter=max_iter,
                workflow_dir=str(wd),
                run_id=f"chat-{task_id}",
                step_name=f"chat-subagent-{task_id[:6]}",
                timeout=900,
            )
            _chat_subagents[task_id].update({
                "state": "completed" if result.success else "failed",
                "ended_at": _time.time(),
                "result": {
                    "success": result.success,
                    "iterations": result.iterations,
                    "tools": [t.get("name") for t in (result.tool_calls_made or [])],
                    "token_usage": result.token_usage or {},
                    "summary": (result.final_message or "")[:1500],
                    "error": result.error,
                },
            })
        except Exception as e:
            _chat_subagents[task_id].update({
                "state": "failed",
                "ended_at": _time.time(),
                "result": {"error": f"{type(e).__name__}: {e}"},
            })
        # chain logic:跑完看有沒 follow_up、有就 spawn 下一個;沒就 push 終結通知
        try:
            await _maybe_chain_next(task_id)
        except Exception:
            pass

    _bg_task = asyncio.create_task(_runner())
    # 把 asyncio.Task 物件也存進 registry、給 cancel_subagent_task 用
    _chat_subagents[task_id]["_task"] = _bg_task

    est = "30-60s" if max_iter <= 4 else "60-180s"
    chain_note = ""
    if follow_up:
        chain_note = f"\n  chain: {chain_total} 階段(完成第 1 後自動接力)"
        for i, step in enumerate(follow_up, start=2):
            chain_note += f"\n    第 {i} 階段: {step.get('role')} — {(step.get('task') or '')[:60]}"
    return (
        f"✅ 子代理已派出\n"
        f"  task_id: {task_id}\n"
        f"  role:    {role}\n"
        f"  working_dir: {wd}\n"
        f"  max_iter: {max_iter}{chain_note}\n"
        f"  預估: {est}\n\n"
        f"對話可繼續。要查狀態 → check_subagent_status('{task_id}')。"
    )


# ── chain 處理：第一階段完→派下一個;最後一階段完→push 終結通知 ──
async def _maybe_chain_next(prev_task_id: str) -> None:
    """看 prev task state、若 success + remaining follow_up → spawn 下一個 chain step;
    否則 push 終結通知。Chain 內每個 step 完都呼叫這個 helper(recursive via _next_runner)。
    """
    import asyncio as _aio
    import time as _time
    import uuid as _uuid
    from pathlib import Path

    prev = _chat_subagents.get(prev_task_id)
    if not prev:
        return

    prev_result = prev.get("result") or {}
    success = prev_result.get("success", False)
    remaining = prev.get("follow_up") or []

    if not success or not remaining:
        # chain 結束(成功最後一個 / 失敗中斷)、push 最終通知
        await _push_subagent_done_to_tg(prev_task_id)
        return

    # 派下一個 chain step
    next_step = remaining[0]
    next_role = (next_step.get("role") or "").strip().lower()
    next_task_str = (next_step.get("task") or "").strip()
    next_max_iter = int(next_step.get("max_iter", prev.get("max_iter", 8)))

    if next_role not in VALID_SUBAGENT_ROLES or not next_task_str:
        # follow_up step config 無效、停 chain、把 error 寫進 prev result + push
        prev["result"]["error"] = (
            (prev["result"].get("error") or "")
            + f"\n[chain 中斷] follow_up 第 {prev['chain_position'] + 2} 階段 config 無效"
        )
        await _push_subagent_done_to_tg(prev_task_id)
        return

    next_task_id = _uuid.uuid4().hex[:12]
    wd = Path(prev["working_dir"])
    chain_position = prev["chain_position"] + 1
    chain_total = prev["chain_total"]
    chain_root = prev.get("chain_root_id", prev_task_id)
    prev_role = prev.get("role", "?")
    prev_summary = (prev_result.get("summary") or "").strip()[:1200]

    # WSL path for cwd anchor hint(同 dispatch_subagent_async 內邏輯)
    try:
        from pipeline.sandbox import windows_to_wsl_path
        wd_wsl = windows_to_wsl_path(str(wd)) or str(wd)
    except Exception:
        wd_wsl = str(wd)

    chained_task = (
        f"⚠️ 路徑規則(很重要、優先此規則):\n"
        f"- 你在 Linux Docker container 內、cwd 已被 docker exec 設為 `{wd_wsl}`\n"
        f"- 寫檔請用 relative path 直接到 cwd、不要算 absolute、不要用「專案根目錄」當 anchor\n"
        f"\n"
        f"## Chain 階段資訊\n"
        f"你在一個 multi-stage chain、現在是第 {chain_position + 1} / {chain_total} 階段\n"
        f"\n"
        f"## 上一階段 ({prev_role}, task_id={prev_task_id[:8]}) 的成果摘要\n"
        f"{prev_summary or '(空、上階段沒給摘要)'}\n"
        f"\n"
        f"## 共用工作目錄(讀寫請用 cwd-relative path)\n"
        f"{wd_wsl}\n"
        f"\n"
        f"## 你的任務\n"
        f"{next_task_str}"
    )

    _chat_subagents[next_task_id] = {
        "state": "running",
        "started_at": _time.time(),
        "ended_at": None,
        "role": next_role,
        "task": next_task_str[:500],
        "working_dir": str(wd),
        "run_id": f"chat-{next_task_id}",
        "max_iter": next_max_iter,
        "result": None,
        "follow_up": remaining[1:],
        "chain_position": chain_position,
        "chain_total": chain_total,
        "chain_root_id": chain_root,
    }
    _trim_registry()

    async def _next_runner():
        from pipeline.subagent_runner import run_subagent
        try:
            result = await run_subagent(
                role_name=next_role,
                task=chained_task,
                max_iter=next_max_iter,
                workflow_dir=str(wd),
                run_id=f"chat-{next_task_id}",
                step_name=f"chat-subagent-{next_task_id[:6]}",
                timeout=900,
            )
            _chat_subagents[next_task_id].update({
                "state": "completed" if result.success else "failed",
                "ended_at": _time.time(),
                "result": {
                    "success": result.success,
                    "iterations": result.iterations,
                    "tools": [t.get("name") for t in (result.tool_calls_made or [])],
                    "token_usage": result.token_usage or {},
                    "summary": (result.final_message or "")[:1500],
                    "error": result.error,
                },
            })
        except Exception as e:
            _chat_subagents[next_task_id].update({
                "state": "failed",
                "ended_at": _time.time(),
                "result": {"error": f"{type(e).__name__}: {e}"},
            })
        # 遞迴 chain
        try:
            await _maybe_chain_next(next_task_id)
        except Exception:
            pass

    next_bg = _aio.create_task(_next_runner())
    _chat_subagents[next_task_id]["_task"] = next_bg

    # push「第 N 完成、第 N+1 已派出」中間階段通知
    try:
        await _push_chain_step_to_tg(prev_task_id, next_task_id)
    except Exception:
        pass


async def _push_chain_step_to_tg(prev_task_id: str, next_task_id: str) -> None:
    """chain 中間階段完成的 TG push:✅ 第 N/M 完 → 🔁 第 N+1/M (role) 已派出"""
    prev = _chat_subagents.get(prev_task_id)
    nxt = _chat_subagents.get(next_task_id)
    if not prev or not nxt:
        return
    try:
        from pipeline.runner import _get_tg_token, _get_tg_chat_id
        from telegram import Bot
    except Exception:
        return
    token = _get_tg_token()
    chat_id = _get_tg_chat_id()
    if not token or not chat_id:
        return

    pos = prev.get("chain_position", 0) + 1  # 1-indexed for display
    total = prev.get("chain_total", 1)
    prev_role = prev.get("role", "?")
    next_role = nxt.get("role", "?")
    pr = prev.get("result") or {}
    tu = pr.get("token_usage") or {}
    summary = (pr.get("summary") or "").replace("\n", " ").strip()[:200]

    msg = (
        f"✅ 第 {pos}/{total} 階段 ({prev_role}) 完成 → 🔁 派出第 {pos + 1}/{total} ({next_role})\n\n"
        f"prev: `{prev_task_id}` ({tu.get('total_tokens', 0):,} tok)\n"
        f"摘要: {summary}\n\n"
        f"next: `{next_task_id}` ({next_role})\n"
        f"任務: {(nxt.get('task') or '')[:120]}"
    )
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")
    except Exception:
        try:
            async with Bot(token=token) as bot:
                await bot.send_message(chat_id=int(chat_id), text=msg)
        except Exception:
            pass


# ── 完成 push 到 TG(任何 channel 派出的 subagent 都能 push、只要有 telegram_chat_id) ──
async def _push_subagent_done_to_tg(task_id: str) -> None:
    info = _chat_subagents.get(task_id)
    if not info:
        return
    try:
        from pipeline.runner import _get_tg_token, _get_tg_chat_id
        from telegram import Bot
    except Exception:
        return
    token = _get_tg_token()
    chat_id = _get_tg_chat_id()
    if not token or not chat_id:
        return  # 沒設 TG 就跳過

    state = info.get("state", "?")
    role = info.get("role", "?")
    r = info.get("result") or {}
    tu = r.get("token_usage") or {}
    tools = r.get("tools") or []
    wd = info.get("working_dir", "")
    summary = (r.get("summary") or "").strip()[:600]
    success_mark = "✅" if r.get("success") else "❌"

    # chain 上下文(若是 chain 一員、加進訊息頂)
    chain_total = info.get("chain_total", 1)
    chain_pos = info.get("chain_position", 0) + 1  # 1-indexed for display
    chain_root = info.get("chain_root_id", task_id)
    is_chain = chain_total > 1
    is_last_stage = chain_pos == chain_total
    remaining_steps = info.get("follow_up") or []

    if state == "completed":
        if is_chain and is_last_stage and r.get("success"):
            # chain 完整跑完(最後一階段成功)
            msg = (
                f"🎉 整條 chain 完成({chain_total} 階段全成功)\n"
                f"chain root: `{chain_root}`\n\n"
                f"最後階段 ({role}, `{task_id}`):\n"
                f"  輪數: {r.get('iterations')}  ·  tokens: {tu.get('total_tokens', 0):,}\n"
                f"  工具: {tools}\n"
                f"  產物: `{wd}`\n\n"
                f"摘要:\n{summary}\n\n"
                f"想看內容 → 「貼 <檔名> 給我」\n"
                f"想下載檔案 → 「把 <檔名> 傳給我」"
            )
        else:
            msg = (
                f"{success_mark} 子代理 `{task_id}` ({role}) 完成\n\n"
                f"輪數: {r.get('iterations')}  ·  tokens: {tu.get('total_tokens', 0):,}\n"
                f"工具: {tools}\n"
                f"產物: `{wd}`\n\n"
                f"摘要:\n{summary}\n\n"
                f"想看內容 → 「貼 <檔名> 給我」\n"
                f"想下載檔案 → 「把 <檔名> 傳給我」"
            )
    else:
        err = r.get("error") or "未知錯誤"
        if is_chain:
            # chain 中斷:標明階段、列未跑階段
            chain_header = (
                f"❌ chain 中斷在第 {chain_pos}/{chain_total} 階段 ({role}) — `{task_id}`\n"
                f"chain root: `{chain_root}`\n\n"
            )
            skipped_lines = ""
            if remaining_steps:
                skipped_lines = "\n未跑階段(已取消):\n"
                for i, step in enumerate(remaining_steps, start=chain_pos + 1):
                    sr = step.get("role", "?")
                    st = (step.get("task") or "")[:80]
                    skipped_lines += f"  - 第 {i}/{chain_total} ({sr}): {st}\n"
            msg = (
                f"{chain_header}"
                f"原因: {err[:300]}\n"
                f"輪數: {r.get('iterations', '?')}  ·  tokens: {tu.get('total_tokens', 0):,}\n"
                f"產物: `{wd}` (可能空 / 部分產出)"
                f"{skipped_lines}\n"
                f"要不要重派此階段、跳過、或放棄整 chain? 跟我說。"
            )
        else:
            msg = (
                f"❌ 子代理 `{task_id}` ({role}) 失敗\n\n"
                f"原因: {err[:300]}\n"
                f"輪數: {r.get('iterations', '?')}  ·  tokens: {tu.get('total_tokens', 0):,}\n"
                f"產物: `{wd}` (可能空 / 部分產出)\n\n"
                f"要不要重派、改 prompt、或放棄? 跟我說。"
            )
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")
    except Exception:
        # Markdown parse fail 退到純文字
        try:
            async with Bot(token=token) as bot:
                await bot.send_message(chat_id=int(chat_id), text=msg)
        except Exception:
            pass


@tool
def check_subagent_status(task_id: str = "") -> str:
    """查子代理狀態。task_id 留空 → 列最近 5 個(in-flight + completed)摘要。

    Args:
        task_id: dispatch_subagent_async 回的 ID(12 字 hex)。

    Returns:
        狀態 + (若已完成)final summary + 工具用量 + token 數。
    """
    import time as _time

    if not _chat_subagents:
        return "(目前沒有任何子代理紀錄)"

    if not task_id or not task_id.strip():
        items = list(_chat_subagents.items())
        items.sort(key=lambda x: x[1].get("started_at", 0), reverse=True)
        items = items[:5]
        lines = [f"最近 {len(items)} 個子代理:"]
        now = _time.time()
        for tid, info in items:
            elapsed = int(now - info.get("started_at", now))
            state = info.get("state", "?")
            role = info.get("role", "?")
            task_preview = (info.get("task") or "")[:60]
            lines.append(f"  {tid} [{state}] {elapsed}s {role}: {task_preview}")
        return "\n".join(lines)

    tid = task_id.strip()
    info = _chat_subagents.get(tid)
    if not info:
        return f"❌ 找不到 task_id={tid!r}"

    state = info.get("state", "?")
    started = info.get("started_at", 0)
    ended = info.get("ended_at")
    elapsed = int((ended or _time.time()) - started)

    out = [
        f"task_id: {tid}",
        f"state: {state}",
        f"role: {info.get('role')}",
        f"working_dir: {info.get('working_dir')}",
        f"elapsed: {elapsed}s",
    ]
    if state == "running":
        out.append("(子代理還在跑、再 await 一下、或這次 chat turn 後再查)")
        return "\n".join(out)

    r = info.get("result") or {}
    out.append(f"iterations: {r.get('iterations')}")
    tools = r.get("tools") or []
    out.append(f"tools used: {tools}")
    tu = r.get("token_usage") or {}
    if tu.get("total_tokens"):
        # 一定要連快取數一起印:Anthropic 的 input_tokens 不含快取讀取,
        # 只印 input/output 會讓人以為用量只有零頭
        _cr = tu.get("cache_read_tokens", 0) or 0
        _cc = tu.get("cache_creation_tokens", 0) or 0
        _line = (f"tokens: input={tu.get('input_tokens', 0)} output={tu.get('output_tokens', 0)}"
                 f" cache_read={_cr} cache_write={_cc} model={tu.get('model', '')!r}")
        try:
            from token_cost import estimate_cost, format_usd
            _c = estimate_cost(tu)
            if _c.get("priced"):
                _line += f" | 成本 {format_usd(_c['total_usd'])}"
                if _c.get("saved_usd", 0) > 0:
                    _line += f"(快取省下 {format_usd(_c['saved_usd'])})"
        except Exception:
            pass
        out.append(_line)
    if r.get("error"):
        out.append(f"error: {r['error'][:300]}")
    _summary = r.get('summary', '(空)')
    out.append(f"\nsummary:\n{_summary}")

    # ⛔ Hallucination 偵測:子代理 summary 宣稱「已送到 TG」但沒走 Atlas 統一傳檔工具
    #   → 真實案例:coder 子代理自己 import requests 呼 TG Bot API、寫 summary 「ok=true、message_id=X」
    #     但實際可能沒真送或送錯 chat_id、使用者沒收到。AI 助手字面採信轉述 user 就誤導。
    #   解法:server 這層偵測 + AI 助手讀到自動加 disclaimer。
    _SEND_CLAIMS = (
        "已送", "已傳", "已寄", "已發送", "已成功傳送", "成功傳送", "傳送成功",
        "API ok", "ok=true", "ok: true", "ok\":true", "ok\": true",
        "message_id", "messageid", "sendDocument", "send_document 成功",
    )
    _has_send_claim = any(k.lower() in _summary.lower() for k in _SEND_CLAIMS)
    _used_v5_tool = "send_subagent_file_to_tg" in tools or "send_file_to_tg" in tools
    if _has_send_claim and not _used_v5_tool:
        out.append(
            "\n⚠️ HALLUCINATION 警示:子代理 summary 宣稱『已傳送/API ok/message_id』、"
            "但 tools used 不含 send_subagent_file_to_tg(Atlas 統一傳檔工具)。\n"
            "  子代理可能自己 import requests 呼 TG Bot API、結果未經系統驗證、實際是否到達未知。\n"
            "  AI 助手轉述使用者時請加 disclaimer『子代理自報、實際請確認』、"
            "或改用 send_subagent_file_to_tg 工具重傳一次保證到達。"
        )
    return "\n".join(out)


@tool
def read_subagent_file(task_id: str, filename: str = "") -> str:
    """讀 ad-hoc 子代理(dispatch_subagent_async 派出的)產物檔案內容、純文字回到 chat。

    用途:使用者要看子代理寫了什麼程式 / 報告 / 結果時、用這個工具讀檔貼在 chat 內。

    Args:
        task_id: dispatch_subagent_async 回的 task_id(12 字 hex)
        filename: 檔名(留空 → 列 working_dir 內所有檔)

    Returns:
        檔案文字內容(50KB 以下)、過大或 binary 檔提示用 send_subagent_file_to_tg 改傳檔。

    安全限制:
        - 路徑限定在該 task 的 working_dir 內、無法讀外部
        - 50 KB 以下才能 inline 貼進 chat、避免 token 爆
    """
    from pathlib import Path
    info = _chat_subagents.get(task_id)
    if not info:
        return f"❌ task_id={task_id!r} 不存在(可能還沒派、或重啟後消失)。先 check_subagent_status 看現有 task"
    wd = Path(info.get("working_dir", "")).resolve()
    if not wd.exists():
        return f"❌ working_dir 不存在: {wd}(子代理可能還沒寫任何檔)"

    if not filename or not filename.strip():
        files = sorted(f for f in wd.iterdir() if f.is_file())
        if not files:
            return f"📁 task {task_id} working_dir 內目前沒任何檔: {wd}"
        listing = "\n".join(f"  - {f.name} ({f.stat().st_size:,} bytes)" for f in files)
        return f"📁 task {task_id} working_dir: {wd}\n{listing}\n\n用 filename 參數指定要讀哪個。"

    # 安全:解析 filename 不可跳出 wd
    target = (wd / filename).resolve()
    try:
        target.relative_to(wd)
    except ValueError:
        return f"❌ {filename!r} 跳出 task working_dir、拒絕"
    if not target.exists():
        return f"❌ {filename!r} 不存在 in {wd}"
    if not target.is_file():
        return f"❌ {filename!r} 不是檔案"

    size = target.stat().st_size
    if size > 50_000:
        return (
            f"❌ {filename} 太大 ({size:,} bytes、>50KB)、不適合 inline 貼進 chat。\n"
            f"請改呼叫 send_subagent_file_to_tg 直接傳檔到使用者 Telegram。"
        )

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return f"=== {filename} ({size:,} bytes) ===\n{content}"
    except Exception as e:
        return f"❌ 讀 {filename} 失敗: {type(e).__name__}: {e}"


@tool
def send_subagent_file_to_tg(task_id: str, filename: str = "", confirm: bool = False) -> str:
    """把 ad-hoc 子代理的產物送到使用者 Telegram(走 send_document)。

    跟 send_file_to_tg 的差別:後者必須綁某 workflow、不接受 chat-adhoc 路徑;
    本工具用 task_id 索引、限定在該 task 的 working_dir 內、可送任何 binary / 大檔。

    Args:
        task_id: dispatch_subagent_async 的 task_id
        filename: 檔名(留空 → 列檔讓使用者選);
                  也接受相對路徑(例 'src/main.py')、解到 working_dir 內。
        confirm: False=預覽(走兩步協議) / True=實送

    安全限制:
        - 路徑限定在該 task 的 working_dir 內
        - TG 單檔上限 50 MB(TG 自己限制)
    """
    from pathlib import Path
    info = _chat_subagents.get(task_id)
    if not info:
        return f"❌ task_id={task_id!r} 不存在"
    wd = Path(info.get("working_dir", "")).resolve()
    if not wd.exists():
        return f"❌ working_dir 不存在: {wd}"

    if not filename or not filename.strip():
        files = sorted(f for f in wd.iterdir() if f.is_file())
        if not files:
            return f"📁 working_dir 內沒任何檔: {wd}"
        listing = "\n".join(f"  - {f.name} ({f.stat().st_size:,} bytes)" for f in files)
        return f"📁 task {task_id} 內可送的檔:\n{listing}\n\n用 filename 指定送哪個。"

    target = (wd / filename).resolve()
    try:
        target.relative_to(wd)
    except ValueError:
        return f"❌ {filename!r} 跳出 task working_dir、拒絕"
    if not target.exists():
        return f"❌ {filename!r} 不存在 in {wd}"
    if not target.is_file():
        return f"❌ {filename!r} 不是檔案"

    size_bytes = target.stat().st_size
    size_kb = size_bytes / 1024

    if not confirm:
        return (
            f"[PREVIEW 不送] task {task_id} → {filename}\n"
            f"路徑: {target}\n"
            f"大小: {size_kb:,.1f} KB ({size_bytes:,} bytes)\n"
            f"⚠️ 取得使用者同意後、再次呼叫本工具並設 confirm=True。\n"
            f"（TG 單檔上限 50 MB、超過會失敗）"
        )

    if size_bytes > 50 * 1024 * 1024:
        return f"❌ {size_kb:,.1f} KB 超過 TG 50 MB 上限"

    try:
        from pipeline.runner import _get_tg_token, _get_tg_chat_id, _prepare_tg_file_with_bom
        from telegram import Bot
        import asyncio as _asyncio, os as _os, logging as _logging
        token = _get_tg_token()
        chat_id = _get_tg_chat_id()
        if not token or not chat_id:
            return "❌ Telegram 未設定(token / chat_id 缺)、無法送"

        # 文字檔送 TG 前注入 UTF-8 BOM、避免 iOS TG 解成 Big5 亂碼
        _send_path, _temp_to_cleanup = _prepare_tg_file_with_bom(
            str(target), _logging.getLogger("chat_tools"), target.name,
        )

        async def _do_send():
            async with Bot(token=token) as bot:
                with open(_send_path, "rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=target.name,
                        caption=f"📎 {target.name}\n來自子代理 task {task_id} ({info.get('role')})",
                    )

        # sync tool → async send:看當前是否有 running event loop、若有就 thread 內跑 asyncio.run
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_asyncio.run, _do_send())
                    future.result(timeout=120)
            else:
                loop.run_until_complete(_do_send())
        except RuntimeError:
            _asyncio.run(_do_send())

        # cleanup BOM-injected temp(若有)
        if _temp_to_cleanup:
            try:
                _os.unlink(_temp_to_cleanup)
            except Exception:
                pass

        return f"✅ 已送出 {target.name} ({size_kb:,.1f} KB) 到 Telegram"
    except Exception as e:
        return f"❌ 送 TG 失敗: {type(e).__name__}: {e}"


@tool
def cancel_subagent_task(task_id: str) -> str:
    """中止正在跑的 ad-hoc 子代理 task。標記為 cancelled、push TG 通知、回收 asyncio.Task。

    使用情境:
    - 使用者說「停止」「中斷」「不要跑了」「太久了 cancel」
    - 子代理跑超過合理時間(>5 分鐘)、而且 check_subagent_status 顯示還 running
    - 使用者已經拿到夠用的部分結果、不必等完整完成

    限制:
    - 只能 cancel still-running 的 task。已 completed / failed / cancelled 的 noop
    - asyncio.cancel 會中斷正在 await 的 LLM call / tool dispatch、
      但**已 spawn 的 docker exec subprocess 可能還會跑完 5-10 秒**(不影響 state)
    - 重啟 backend 過的 task_id 會找不到、回不存在
    """
    import asyncio as _asyncio
    import time as _time
    info = _chat_subagents.get(task_id)
    if not info:
        return f"❌ task_id={task_id!r} 不存在(可能 backend 重啟過、in-memory registry 清了)"
    state = info.get("state")
    if state in ("completed", "failed", "cancelled"):
        return f"task {task_id} 已是 {state}、不需 cancel"

    bg_task = info.get("_task")
    if bg_task is not None and not bg_task.done():
        try:
            bg_task.cancel()
        except Exception:
            pass

    info["state"] = "cancelled"
    info["ended_at"] = _time.time()
    info["result"] = {
        "success": False,
        "iterations": 0,
        "tools": [],
        "token_usage": {},
        "summary": "使用者主動 cancel",
        "error": "cancelled by user",
    }

    # Push TG「已停止」(fire-and-forget)
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            _asyncio.create_task(_push_subagent_done_to_tg(task_id))
        else:
            loop.run_until_complete(_push_subagent_done_to_tg(task_id))
    except Exception:
        pass

    return (
        f"✅ task {task_id} 已標記為 cancelled、TG 已通知。\n"
        f"⚠️ 背景已 spawn 的 docker exec 可能還會跑完 5-10 秒(資源無關緊要)、"
        f"但不會再影響 state 或重新派出。"
    )


@tool
def read_help_doc(topic: str = "") -> str:
    """讀子代理進階用法的 help doc。**system prompt 沒寫細節時來查這裡**。

    可選 topic:
    - chain   : 多階段子代理接力(dispatch follow_up 參數的格式 / 各 role 的 max_iter
                建議 / 何時用 chain vs 單一 dispatch / 典型 chain 配置)
    - files   : 子代理產物的讀檔 / 傳檔(read_subagent_file vs send_subagent_file_to_tg
                vs send_file_to_tg 差別)
    - cancel  : 中止跑中的子代理(cancel_subagent_task 判斷規則 / TG push 行為)

    Args:
        topic: 上述 topic 之一。留空 → 列可選 topic + 簡介。

    Returns:
        該 topic 的完整教學(markdown)。
    """
    from help_docs import get_help_doc
    return get_help_doc(topic)


@tool
def list_workflow_variables(query: str) -> str:
    """列出某工作流的可用變數(steps output / input / env)+ 上次跑出來的實際值。

    使用時機:
    - 使用者要規劃 / 修改某工作流、需要知道下游 step 可以引用哪些上游 output
    - 使用者問「step1 抓到的 X 怎麼餵給 step2?」
    - 你想建議使用者用 `{{ }}` 變數化某個欄位前、先確認該變數真的存在

    Args:
        query: 工作流名稱或 id 前綴(模糊比對)

    Returns:
        JSON 字串、含:
          available.steps: 每個非 human_confirm step 提供的 output 欄位 + 上次值
            (path / stdout / exit_code / status,以及該 step UIA save_as 的所有 key)
          available.input: 此 workflow 引用到的 input.X + 上次傳入值
          available.env:  常用環境變數(KEY/TOKEN/SECRET 已過濾)
          referenced:     整份 YAML 引用到的所有 dotted-path
    """
    wf, msg = _resolve_workflow(query)
    if not wf:
        return msg

    import yaml as _yaml
    import os as _os
    from pipeline.models import PipelineConfig
    from pipeline.expression import find_referenced_vars
    from pipeline.store import get_store

    yaml_str = wf.get("yaml") or ""
    if not yaml_str.strip():
        return json.dumps({
            "workflow_id": wf.get("id"),
            "workflow_name": wf.get("name"),
            "available": {"steps": [], "input": [], "env": []},
            "referenced": [],
            "note": "workflow YAML 為空、沒有可用變數",
        }, ensure_ascii=False, indent=2)

    try:
        data = _yaml.safe_load(yaml_str) or {}
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = config_dict.get("validate", True)
        config = PipelineConfig(**config_dict)
    except Exception as e:
        return f"workflow YAML 解析失敗:{e}"

    last_step_by_name: dict = {}
    last_input_params: dict = {}
    try:
        for r in get_store().list_recent(20):
            if r.workflow_id == wf.get("id"):
                last_input_params = getattr(r, "input_params", None) or {}
                for sr in r.step_results:
                    last_step_by_name[sr.step_name] = sr
                break
    except Exception:
        pass

    referenced: set[str] = set()
    for step in config.steps:
        for fname in ("batch", "message", "uia_window", "vv_prompt", "working_dir"):
            v = getattr(step, fname, "")
            if isinstance(v, str) and v:
                referenced.update(find_referenced_vars(v))
        if step.output and step.output.path:
            referenced.update(find_referenced_vars(step.output.path))
        if step.actions:
            for a in step.actions:
                for fname in ("text", "title", "vlm_prompt", "expected"):
                    v = getattr(a, fname, "")
                    if isinstance(v, str) and v:
                        referenced.update(find_referenced_vars(v))

    avail_steps: list[dict] = []
    for step in config.steps:
        if step.human_confirm:
            continue
        sr = last_step_by_name.get(step.name)
        fields: list[dict] = []
        if step.output and step.output.path:
            fields.append({"key": "path", "type": "string",
                           "last_value": (sr.actual_output_path if sr else "") or step.output.path})
        if sr:
            fields.append({"key": "stdout", "type": "string", "last_value": (sr.stdout_tail or "")[:120]})
            fields.append({"key": "exit_code", "type": "number", "last_value": sr.exit_code})
            fields.append({"key": "status", "type": "string", "last_value": sr.validation_status})
        if step.actions:
            seen: set[str] = set()
            for a in step.actions:
                if a.save_as and a.save_as not in seen:
                    seen.add(a.save_as)
                    last_v = ""
                    if sr and getattr(sr, "step_vars", None):
                        last_v = sr.step_vars.get(a.save_as, "")
                    fields.append({
                        "key": a.save_as, "type": "string",
                        "last_value": str(last_v) if last_v else "",
                        "source": f"save_as ({a.type})",
                    })
        avail_steps.append({
            "name": step.name,
            "fields": fields,
        })

    input_keys = sorted({
        ref.split(".", 1)[1] for ref in referenced
        if ref.startswith("input.") and "." in ref
    })
    avail_input = [
        {"key": k, "last_value": last_input_params.get(k, "")}
        for k in input_keys
    ]

    common_env = ["OUTPUT_BASE_PATH", "PIPELINE_DIR", "HOME", "USERPROFILE", "TIMEZONE"]
    env_keys: set[str] = set(common_env)
    for ref in referenced:
        if ref.startswith("env.") and "." in ref:
            env_keys.add(ref.split(".", 1)[1])
    def _is_secret(k: str) -> bool:
        u = k.upper()
        return any(t in u for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD"))
    avail_env = [
        {"key": k, "last_value": _os.environ.get(k, "")}
        for k in sorted(env_keys) if not _is_secret(k) and _os.environ.get(k)
    ]

    return json.dumps({
        "workflow_id": wf.get("id"),
        "workflow_name": wf.get("name"),
        "available": {
            "steps": avail_steps,
            "input": avail_input,
            "env": avail_env,
        },
        "referenced": sorted(referenced),
    }, ensure_ascii=False, indent=2)


# ── 長期記憶工具(階段1:facts 語意記憶)──────────────────────────
# 只在 settings.memory_enabled=True 時被掛載(main.py _active_tools filter)。
# 寫操作(remember/forget)走 two-step confirm;recall/list/state 是讀、不必 confirm。

@tool
def remember_fact(key: str, value: str, category: str = "fact", confirm: bool = False) -> str:
    """把一個關於使用者的事實 / 偏好記進長期記憶、跨對話永久保留(讓助手越用越懂使用者)。

    什麼時候用:使用者明確說「記一下 / 記住 / 以後都這樣」,或表達了穩定偏好
    (例「報告我都要正式 Word」「不要爬 PChome」「我做硬體競品研究」)。
    ⚠️ 一次性需求不要記。敏感資料(密碼 / API key)會被系統拒記。

    Args:
        key: 短鍵、英數底線(例 'report_format' / 'domain' / 'avoid_sites')
        value: 內容(例 '正式 Word' / '硬體競品研究')
        category: workflow_pref(工作流偏好)/ domain(領域)/ past_decision(過去取捨)
                  / vocabulary(慣用詞)/ preference / fact
        confirm: False=預覽、True=真寫(取得使用者同意後才設 True)
    """
    import memory as _mem
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return "key 與 value 都不可空"
    hit = _mem.is_sensitive(value)
    if hit:
        return f"⛔ 拒記:這看起來像敏感資料(密碼 / 金鑰),不收進記憶。"
    if not confirm:
        prev = _mem.recall_fact(key)
        prev_line = f"\n（會覆蓋舊值：{prev['value']}）" if prev.get("found") else ""
        return (f"📝 預覽:要記住「{key} = {value}」(分類 {category}){prev_line}\n"
                f"⚠️ 取得使用者同意(『好』『記』『OK』)後,再次呼叫本工具並設 confirm=True。")
    r = _mem.remember_fact(key, value, category=category, source="user_told", confidence=1.0)
    if not r.get("ok"):
        return f"記憶失敗:{r.get('error')}"
    return f"✅ 已記住:{key} = {value}(分類 {category})。之後跨對話都記得。"


@tool
def recall_fact(key: str) -> str:
    """從長期記憶查一個 key 的值。找不到會說明。"""
    import memory as _mem
    r = _mem.recall_fact((key or "").strip())
    if not r.get("found"):
        return f"記憶裡沒有 '{key}'。"
    src = "(推測)" if r.get("source") == "inferred" else ""
    return f"{key} = {r['value']}{src}(分類 {r.get('category')})"


@tool
def list_facts(category: str = "", limit: int = 20) -> str:
    """列出記得的事實 / 偏好。可選 category 過濾。"""
    import memory as _mem
    cat = (category or "").strip() or None
    rows = _mem.list_facts(category=cat, limit=int(limit))
    if not rows:
        return "目前沒有任何記憶。"
    lines = []
    for r in rows:
        src = "(推測)" if r.get("source") == "inferred" else ""
        lines.append(f"- [{r.get('category')}] {r['key']} = {r['value']}{src}")
    return f"目前記得 {len(rows)} 筆:\n" + "\n".join(lines)


@tool
def forget_fact(key: str, confirm: bool = False) -> str:
    """從長期記憶刪掉一個 key(使用者說「忘掉 / 別記 X」時用)。走 two-step。"""
    import memory as _mem
    key = (key or "").strip()
    if not confirm:
        prev = _mem.recall_fact(key)
        if not prev.get("found"):
            return f"記憶裡本來就沒有 '{key}'、不必刪。"
        return (f"🗑️ 預覽:要刪掉記憶「{key} = {prev['value']}」\n"
                f"⚠️ 取得使用者同意後,再次呼叫本工具並設 confirm=True。")
    r = _mem.forget_fact(key)
    return f"✅ 已刪掉記憶 '{key}'。" if r.get("deleted") else f"記憶裡沒有 '{key}'。"


@tool
def recall_episode(query: str, max_results: int = 5) -> str:
    """查過去對話的摘要 —— 使用者問「上次 / 之前聊的那個…」「我們之前討論的 X 結論是?」
    這類「過去發生的事」(不是單點偏好)時用。語意檢索、用不同詞也找得到。"""
    import memory as _mem
    eps = _mem.recall_episode((query or "").strip(), max_results=int(max_results))
    if not eps:
        return "過去對話摘要裡找不到相關的。可請使用者多給點線索。"
    lines = [f"- {e['summary']}" for e in eps]
    return "找到這些過去對話的摘要(可據此回答使用者):\n" + "\n".join(lines)


@tool
def memory_state() -> str:
    """看記憶現況:記了幾筆事實、幾段對話摘要、最近幾筆是什麼。"""
    import memory as _mem
    nf = _mem.count_facts()
    ne = _mem.count_episodes()
    if nf == 0 and ne == 0:
        return "長期記憶目前是空的(還沒記任何事實、也沒對話摘要)。"
    top = _mem.list_facts(limit=5)
    lines = [f"- [{r.get('category')}] {r['key']} = {r['value']}" for r in top]
    body = f"長期記憶:{nf} 筆事實偏好、{ne} 段對話摘要。"
    if lines:
        body += "\n最近 5 筆偏好:\n" + "\n".join(lines)
    return body


# ── 既有專案探查工具(啟動既有 Python 專案 / GUI / CLI 用) ──────────────────────
# 敏感檔名 pattern(對齊 CLAUDE.md deny-list):這些檔一律不列出、不讀內容。
_SENSITIVE_GLOBS = (
    ".env", ".env.*", "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "*.gpg", "credentials*", "*credentials*",
    "*secret*", "*token*", ".npmrc",
)


def _is_sensitive_filename(name: str) -> bool:
    import fnmatch
    low = (name or "").lower()
    return any(fnmatch.fnmatch(low, p) for p in _SENSITIVE_GLOBS)


def _detect_proj_venv(proj: Path) -> dict:
    """偵測專案目錄下的虛擬環境(venv / .venv,Win Scripts / Unix bin)。
    與 main.py 的 /fs/check-venv 同邏輯:venv 先(Windows 慣例)、誰先找到用誰。"""
    import os
    is_win = (os.name == "nt")
    sub = "Scripts" if is_win else "bin"
    py = "python.exe" if is_win else "python"
    for vdir in ("venv", ".venv"):
        vpy = proj / vdir / sub / py
        if vpy.exists():
            return {"has_venv": True, "python_path": str(vpy.resolve()), "venv_dir_name": vdir}
    return {"has_venv": False, "python_path": None, "venv_dir_name": None}


@tool
def inspect_project(path: str) -> str:
    """探查使用者既有的 Python 專案資料夾,為「啟動既有專案」工作流收集規劃所需資訊。

    什麼時候用:使用者說「我有 Python 專案 / 啟動我的程式 / 跑 main.py」並給了資料夾路徑後,
    **第一步就呼叫本工具**,別憑空猜入口或依賴。回傳 JSON:
    - venv:虛擬環境偵測。has_venv=true 時 python_path 就是該專案的 python,
      **組 batch 時把它當 python 前綴**(例 `"<python_path>" main.py --arg`)、確保依賴齊全、不會 ModuleNotFoundError。
    - entry_candidates:入口候選(main.py / app.py / run.py / cli.py / manage.py …)
    - dependency_files:依賴檔(requirements.txt / pyproject.toml / Pipfile …)
    - top_level_tree:頂層目錄樹(限 2 層;.env / *.key / *secret* / *token* 等敏感檔一律不列)

    拿到結果後的 SOP:用 read_project_file 讀入口檔判斷怎麼跑(argparse / input() / CLI 參數)→
    ask_user 跟使用者確認入口與參數 → 組 script 節點 batch。
    無 venv → 用 `python` 跑、並在規劃時提醒「此專案無虛擬環境、依賴可能缺、建議先建 venv」。

    Args:
        path: 專案資料夾的絕對路徑(例 C:\\Users\\me\\external_projects\\my_tool)
    """
    import os
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return "請提供專案資料夾路徑(絕對路徑)。"
    proj = Path(p).expanduser()
    if not proj.exists():
        return (f"路徑不存在:{proj}\n請確認專案已放好(建議放本專案 external_projects/<你的專案>/ 底下)、"
                f"再給我正確的絕對路徑。")
    if proj.is_file():
        proj = proj.parent
    venv = _detect_proj_venv(proj)
    entry_names = ("main.py", "app.py", "run.py", "cli.py", "__main__.py",
                   "manage.py", "start.py", "server.py", "gui.py", "bot.py")
    dep_names = ("requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
                 "environment.yml", "setup.py", "setup.cfg", "poetry.lock")
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv",
                 ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "dist", "build", ".ruff_cache"}
    entries, deps, readmes, tree = [], [], [], []
    try:
        for child in sorted(proj.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            nm = child.name
            if _is_sensitive_filename(nm):
                continue
            if child.is_dir():
                if nm in skip_dirs:
                    tree.append(f"{nm}/  (略)")
                    continue
                tree.append(f"{nm}/")
                try:  # 第二層只列 .py 與 dep 檔
                    for g in sorted(child.iterdir()):
                        if _is_sensitive_filename(g.name):
                            continue
                        if g.is_file() and (g.suffix == ".py" or g.name in dep_names):
                            tree.append(f"  {nm}/{g.name}")
                            if g.name in entry_names:
                                entries.append(f"{nm}/{g.name}")
                except Exception:
                    pass
            else:
                tree.append(nm)
                if nm in entry_names:
                    entries.append(nm)
                if nm in dep_names:
                    deps.append(nm)
                if nm.lower() in ("readme.md", "readme.txt", "readme.rst", "readme"):
                    readmes.append(nm)
    except Exception as e:
        return f"讀取資料夾失敗:{e}"
    if len(tree) > 120:
        tree = tree[:120] + [f"...(還有 {len(tree) - 120} 項略過)"]
    result = {
        "project_dir": str(proj.resolve()),
        "venv": venv,
        "entry_candidates": entries or "(頂層沒找到常見入口檔、請看 top_level_tree 或讀 README)",
        "dependency_files": deps,
        "readme_files": readmes,
        "top_level_tree": tree,
        "hint": ("有 venv → batch 用 python_path 當前綴跑;無 venv → 用 python 並提醒依賴可能缺。"
                 "下一步:read_project_file 讀入口檔/README 判斷怎麼跑與有哪些參數,再 ask_user 確認。"),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool
def read_project_file(path: str, max_chars: int = 8000) -> str:
    """讀使用者既有專案裡的某個檔(原始碼 / README / requirements),判斷怎麼跑、有哪些 CLI 參數。

    什麼時候用:inspect_project 之後,要讀入口檔(main.py 等)看它的 argparse / input() / 啟動方式,
    或讀 README / requirements.txt 了解用法與依賴,據此組 batch 與 ask_user 的選項。
    ⚠️ .env / *.key / credentials / *secret* / *token* 等敏感檔會被拒讀。

    Args:
        path: 檔案絕對路徑
        max_chars: 最多回傳字元(預設 8000、避免 token 爆;超過會截斷並提示)
    """
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return "請提供檔案的絕對路徑。"
    f = Path(p).expanduser()
    if _is_sensitive_filename(f.name):
        return f"⛔ 拒讀:{f.name} 屬敏感檔(.env / 金鑰 / 憑證 / token),不讀取內容。"
    if not f.exists():
        return f"檔案不存在:{f}"
    if f.is_dir():
        return f"{f} 是資料夾、不是檔案。要看目錄結構請用 inspect_project。"
    try:
        sz = f.stat().st_size
        if sz > 2_000_000:
            return f"檔案過大({sz} bytes),拒讀以免 token 爆。請改讀較小的入口檔 / README。"
        raw = f.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"讀檔失敗:{e}"
    n = max(500, int(max_chars))
    if len(raw) > n:
        return raw[:n] + f"\n\n...(檔案還有 {len(raw) - n} 字元被截斷;需要的話用更大的 max_chars 或讀特定段落)"
    return raw or "(檔案是空的)"


@tool
async def list_outlook_folders(max_depth: int = 2) -> str:
    """列出本機 Outlook 的所有信箱(帳號)與資料夾,含每個資料夾的信件數。

    規劃 Outlook 工作流前用這個確認「要撈哪個帳號的哪個資料夾」——
    folder 參數預設 inbox 只會抓到第一個信箱;多帳號要寫「信箱名/資料夾名」。

    Args:
        max_depth: 往下展開幾層子資料夾(預設 2;信箱本身算第 1 層)。
    """
    def _walk():
        import win32com.client, pythoncom
        pythoncom.CoInitialize()
        try:
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            out = []
            for i in range(1, ns.Folders.Count + 1):
                store = ns.Folders.Item(i)
                out.append(f"📮 信箱:{store.Name}")

                def _sub(folder, depth, prefix):
                    if depth > max_depth:
                        return
                    try:
                        for j in range(1, folder.Folders.Count + 1):
                            f = folder.Folders.Item(j)
                            try:
                                cnt = f.Items.Count
                            except Exception:
                                cnt = "?"
                            if cnt == "?" or int(cnt) > 0:
                                out.append(f"{prefix}└ {f.Name}  ({cnt} 封)"
                                           f"   → folder: \"{store.Name}/{f.Name}\"")
                            _sub(f, depth + 1, prefix + "  ")
                    except Exception:
                        pass
                _sub(store, 1, "   ")
            return "\n".join(out) if out else "(找不到任何 Outlook 信箱)"
        finally:
            pythoncom.CoUninitialize()
    try:
        import asyncio as _a
        res = await _a.get_event_loop().run_in_executor(None, _walk)
        return (res + "\n\n用法:把上面的 folder 值填進 outlook_params 的 folder 欄位;"
                      "只填 inbox 會抓第一個信箱。")
    except Exception as e:
        return f"列舉 Outlook 資料夾失敗({type(e).__name__}: {e})。請確認 Outlook 已安裝且已登入。"


@tool
async def test_mcp_server(name: str = "", tool: str = "", tool_args_json: str = "") -> str:
    """實測 MCP server 連線(規劃含 mcp 節點的工作流前、自我修復診斷、或使用者要求檢測時用)。

    Args:
        name: 已安裝 server 的註冊名(如 filesystem / memory)。留空 = 測所有已安裝 server。
        tool: (選填)要實際呼叫的 tool 名稱;不填只做連線 + 列 tools 的健康檢查。
        tool_args_json: (選填)呼叫 tool 時的參數,JSON 字串,如 '{"path": "C:/x.txt"}'。
    """
    import json as _j
    try:
        from db import list_mcp_servers, get_mcp_server
        from mcp_client import list_mcp_tools, call_mcp_tool
    except Exception as e:
        return f"❌ MCP 模組載入失敗:{e}"

    async def _probe(s: dict) -> str:
        r = await list_mcp_tools(command=s["command"], args=s["args"],
                                 env=(s["env"] or None), timeout=60.0)
        if not r.get("ok"):
            return f"❌ `{s['name']}` 連線失敗:{r.get('error')}"
        return f"✅ `{s['name']}` 連線正常({len(r['tools'])} 個 tools:{', '.join(t['name'] for t in r['tools'][:8])}…)"

    if not (name or "").strip():
        servers = list_mcp_servers(enabled_only=True)
        if not servers:
            return "目前沒有安裝任何 MCP server(設定頁 MCP 專區可安裝)。規劃工作流時請勿使用 mcp 節點。"
        results = [await _probe(s) for s in servers]
        return "MCP 連線檢測結果:\n" + "\n".join(results)

    s = get_mcp_server(name.strip())
    if not s or not s.get("enabled"):
        return f"❌ server「{name}」未安裝或已停用。可用 test_mcp_server() 不帶參數列出全部;或請使用者到設定頁 MCP 專區安裝。"
    if not (tool or "").strip():
        return await _probe(s)
    # 實際呼叫指定 tool
    try:
        targs = _j.loads(tool_args_json) if (tool_args_json or "").strip() else {}
        if not isinstance(targs, dict):
            return "❌ tool_args_json 必須是 JSON object(dict)"
    except Exception as e:
        return f"❌ tool_args_json 不是合法 JSON:{e}"
    r = await call_mcp_tool(command=s["command"], args=s["args"], tool_name=tool.strip(),
                            tool_args=targs, env=(s["env"] or None), timeout=60.0)
    if not r.get("ok"):
        detail = (r.get("result_text") or r.get("error") or "").strip()
        return f"❌ `{name}.{tool}` 呼叫失敗:{detail[:600]}"
    return f"✅ `{name}.{tool}` 呼叫成功。回傳(節錄):{(r.get('result_text') or '')[:400]}"


# Module-level export 給 main.py 用
CHAT_TOOLS = [
    list_workflows, get_workflow_yaml, get_recent_runs, get_run_log,
    list_workflow_variables,                 # 列工作流可用變數(規劃 / 修改用)
    save_workflow_yaml, create_workflow_yaml, start_workflow,
    patch_node_actions,                      # 定點改動作序列(不覆蓋整份、保住使用者挑的控制項)    # 寫工具(走 two-step approval)
    create_subagent_role,                    # 新增自訂 subagent role(走 two-step approval)
    send_file_to_tg,                         # 送檔到 TG(走 two-step approval)
    web_search,                              # 網路搜尋(限定工作流相關研究)
    list_schedules, schedule_workflow, cancel_schedule,  # 排程相關(write 走 two-step)
    dispatch_subagent_async, check_subagent_status,  # 子代理派出 / 查狀態(沙盒隔離、無 confirm)
    read_subagent_file, send_subagent_file_to_tg,    # 子代理產物 讀 / 傳 TG(限定 task working_dir)
    cancel_subagent_task,                            # 中止正在跑的子代理(asyncio.cancel + push TG)
    read_help_doc,                                   # 進階用法 lazy doc(chain / files / cancel)
    inspect_project, read_project_file,              # 探查既有專案 + 讀源碼(啟動既有 Python 專案、偵測 venv 用)
    test_mcp_server,                                 # MCP 連線實測(規劃前健檢 / 自癒診斷 / 使用者要求檢測)
    list_outlook_folders,                            # 列 Outlook 信箱/資料夾(多帳號時確認 folder 該填什麼)
    remember_fact, recall_fact, list_facts, forget_fact, recall_episode, memory_state,  # 長期記憶(memory_enabled 時掛載)
]
CHAT_TOOLS_BY_NAME = {t.name: t for t in CHAT_TOOLS}
# 記憶工具名單(main.py 依 settings.memory_enabled 決定掛不掛)
MEMORY_TOOL_NAMES = {"remember_fact", "recall_fact", "list_facts", "forget_fact", "recall_episode", "memory_state"}
