"""Atlas MCP Server — 把 Atlas 工作流曝露成 MCP tools(生態位升級:Atlas 變成別的 AI 的手腳)。

Claude Desktop / 任何 MCP client 設定這支 stdio server 後,就能:
  - list_workflows()                     看有哪些工作流
  - run_workflow(name, input_params)     觸發執行(回 run_id;無人值守快速模式)
  - get_run_status(run_id)               查狀態 + 逐步結果摘要
  - get_run_output(run_id)               拿最終輸出檔內容(文字/JSON)

實作為「薄代理」:透過 HTTP 打本機 Atlas 後端(預設 http://127.0.0.1:8014,
env ATLAS_BASE_URL 可覆寫)→ 不重複商業邏輯、Atlas 後端不用改。

Claude Desktop 設定範例(claude_desktop_config.json):
{
  "mcpServers": {
    "atlas": {
      "command": "<此 venv 的 python.exe 絕對路徑>",
      "args": ["<此檔絕對路徑>"],
      "env": {"ATLAS_BASE_URL": "http://127.0.0.1:8014"}
    }
  }
}
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("ATLAS_BASE_URL", "http://127.0.0.1:8014").rstrip("/")

mcp = FastMCP("atlas")


def _get(path: str, timeout: float = 30) -> dict:
    r = httpx.get(f"{BASE}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def list_workflows() -> str:
    """列出 Atlas 裡所有工作流(名稱、id、更新時間)。"""
    try:
        wfs = _get("/workflows").get("workflows", []) or _get("/workflows").get("items", [])
    except Exception:
        # /workflows 直接回 list 的情況
        r = httpx.get(f"{BASE}/workflows", timeout=30)
        r.raise_for_status()
        data = r.json()
        wfs = data if isinstance(data, list) else data.get("workflows", [])
    out = [{"id": w.get("id"), "name": w.get("name"), "updated_at": w.get("updated_at")} for w in wfs]
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool()
def run_workflow(name_or_id: str, input_params: dict | None = None) -> str:
    """依名稱或 id 觸發一條 Atlas 工作流(無人值守快速模式)。

    Args:
        name_or_id: 工作流名稱(模糊比對)或 wf- 開頭的 id。
        input_params: 傳給工作流的啟動參數(工作流內以 {{ input.鍵 }} 取用)。
    回傳:run_id 與啟動訊息;用 get_run_status(run_id) 追蹤。
    """
    r = httpx.get(f"{BASE}/workflows", timeout=30)
    r.raise_for_status()
    data = r.json()
    wfs = data if isinstance(data, list) else data.get("workflows", [])
    q = (name_or_id or "").strip()
    wf = next((w for w in wfs if w.get("id") == q), None) \
        or next((w for w in wfs if w.get("name") == q), None) \
        or next((w for w in wfs if q and q in (w.get("name") or "")), None)
    if not wf:
        return json.dumps({"ok": False, "error": f"找不到工作流「{q}」;用 list_workflows 查名稱"},
                          ensure_ascii=False)
    detail = _get(f"/workflows/{wf['id']}")
    yaml_content = (detail.get("yaml") or "").strip()
    if not yaml_content:
        return json.dumps({"ok": False, "error": "該工作流 YAML 為空(請先在 Atlas 畫布存檔)"},
                          ensure_ascii=False)
    resp = httpx.post(f"{BASE}/pipeline/run", json={
        "yaml_content": yaml_content,
        "validate": bool(detail.get("validate", False)),
        "use_recipe": True,
        "silent_recipe": True,
        "workflow_id": wf["id"],
        "input_params": input_params or {},
    }, timeout=60)
    resp.raise_for_status()
    d = resp.json()
    return json.dumps({"ok": True, "run_id": d.get("run_id"), "message": d.get("message"),
                       "workflow": wf.get("name")}, ensure_ascii=False)


@mcp.tool()
def get_run_status(run_id: str) -> str:
    """查某次執行的狀態(running/completed/failed/awaiting_human)與逐步結果摘要。"""
    d = _get(f"/pipeline/runs/{run_id}")
    steps = [{
        "step": s.get("step_name"),
        "validation": s.get("validation_status"),
        "output_path": s.get("actual_output_path"),
    } for s in (d.get("step_results") or [])]
    return json.dumps({
        "run_id": run_id, "status": d.get("status"),
        "pipeline": d.get("pipeline_name"),
        "current_step": d.get("current_step"), "total_steps": d.get("total_steps"),
        "awaiting": d.get("awaiting_suggestion") or None,
        "steps": steps,
    }, ensure_ascii=False, indent=1)


@mcp.tool()
def get_run_output(run_id: str, step_name: str = "") -> str:
    """取執行的輸出檔內容(預設最後一個有輸出的步驟;文字/JSON,最多 50KB)。"""
    d = _get(f"/pipeline/runs/{run_id}")
    srs = [s for s in (d.get("step_results") or []) if s.get("actual_output_path")]
    if step_name:
        srs = [s for s in srs if s.get("step_name") == step_name]
    if not srs:
        return json.dumps({"ok": False, "error": "沒有輸出檔(或指定步驟無輸出)"}, ensure_ascii=False)
    path = srs[-1]["actual_output_path"]
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            content = f.read(50_000)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"讀檔失敗:{e}", "path": path}, ensure_ascii=False)
    return json.dumps({"ok": True, "step": srs[-1]["step_name"], "path": path,
                       "content": content}, ensure_ascii=False)


_NODE_REFERENCE = """# Atlas 工作流 YAML 規格(外部 AI 版)— 建立前必讀

## 結構
```yaml
name: 工作流名稱
validate: true            # 有任何 expect/AI 驗證時設 true
steps:
  - name: 步驟名(中文可)
    <節點型別欄位>
    output:
      path: 輸出檔名.json   # 相對路徑即可,Atlas 自動放進 run 資料夾
```
步驟依序執行;引用上游:`{{ steps.<步名>.output.<鍵> }}`(JSON 輸出自動攤平)、啟動參數 `{{ input.<鍵> }}`。

## 節點型別(一步只能一種)
1. **script**:`batch: <shell/python 指令>`(跑現成指令)
2. **AI 技能**:`skill_mode: true` + `batch: <自然語言任務>`(LLM 沙盒生碼執行)。輸出 JSON 給下游時**必加** `output.json_schema`(inline JSON Schema,如 `{"type":"object","required":["口碑"]}`)
3. **web_crawler**:`web_crawler: true` + `wc_url: <URL>`;**鐵律:必填 `output.expect` 描述「真的抓到目標資料」**
4. **condition**:`condition: true` + `expression: <Jinja 布林>` + `on_true:/on_false: <步名或 end>`;或 `switch:/cases:{值:步名}/default:`
5. **human_confirm**:`human_confirm: true` + `message: <給人看的確認訊息>`(TG 核准後才續)
6. **subagent**:`subagent: true` + `subagent_role: <data_analyst|coder|researcher|critic|planner>` + `batch: <任務>`(多輪深度研究)
7. **outlook**:`outlook_automation: true` + `batch: <寄信需求描述>`(本機 Outlook)
8. **mcp**:`mcp: true` + `mcp_server: <已安裝名>` + `mcp_tool: <工具名>` + `mcp_tool_args: {參數}`(先用 Atlas 內工具查已安裝清單;不可虛構)
9. **timeout**(選填秒數)、`retry`(預設1)、`llm_role: secondary`(該步用副模型)

## 完整範例
```yaml
name: 每日情報
validate: true
steps:
  - name: 抓頁面
    web_crawler: true
    wc_url: https://example.com/news
    output: {path: raw.md, expect: "抓到多篇真實新聞,非404/空頁"}
  - name: 解析
    skill_mode: true
    batch: 讀 raw.md,統計正負面則數,輸出 stats.json
    output:
      path: stats.json
      json_schema: {"type":"object","required":["正面","負面"],"properties":{"正面":{"type":"integer"},"負面":{"type":"integer"}}}
  - name: 判斷
    condition: true
    expression: "{{ steps.解析.output.負面 | int > 3 }}"
    on_true: 通知
    on_false: end
  - name: 通知
    human_confirm: true
    message: "負面超標,要寄警示嗎?"
```

## 常見拒收原因
- 步驟沒有任何節點型別欄位(只有 name)
- condition 缺 expression/on_true
- 爬蟲缺 output.expect
- mcp_server/mcp_tool 用了不存在的名稱
"""


@mcp.tool()
def get_node_reference() -> str:
    """【建立工作流前必讀】取得 Atlas 工作流 YAML 完整規格(節點型別/欄位/範例/鐵律)。"""
    return _NODE_REFERENCE


@mcp.tool()
def create_workflow(name: str, yaml_content: str) -> str:
    """依 YAML 建立新工作流(先呼叫 get_node_reference 取得規格,否則會被驗證拒絕)。

    Atlas 會做結構驗證:解析失敗 / 空節點 / 缺必要欄位 → 拒收並回具體原因,修正後重送。
    建立成功回 workflow_id;之後可用 run_workflow 執行(高風險步驟仍走人工核准)。
    """
    import yaml as _yaml
    # 1) 語法 + 模型驗證(與 Atlas 後端同一套 pydantic 模型)
    try:
        raw = _yaml.safe_load(yaml_content)
        from pipeline.models import PipelineConfig
        PipelineConfig(**raw)
    except Exception as e:
        return json.dumps({"ok": False, "rejected": f"YAML 驗證失敗:{type(e).__name__}: {str(e)[:400]}",
                           "hint": "呼叫 get_node_reference 對照規格修正後重送"}, ensure_ascii=False)
    # 2) 空節點檢查(弱模型/外部 AI 常見:只寫 name 沒型別)
    _TYPES = ("condition", "skill_mode", "subagent", "human_confirm", "computer_use",
              "visual_validation", "outlook_automation", "web_crawler", "mcp")
    for s in (raw.get("steps") or []):
        if isinstance(s, dict) and not str(s.get("batch", "")).strip() \
                and not any(s.get(k) for k in _TYPES):
            return json.dumps({"ok": False,
                               "rejected": f"step「{s.get('name')}」沒有任何節點型別(batch/skill_mode/condition/...全空)",
                               "hint": "每一步必須是九大節點之一,見 get_node_reference"}, ensure_ascii=False)
    # 3) 爬蟲鐵律
    for s in (raw.get("steps") or []):
        if isinstance(s, dict) and s.get("web_crawler"):
            out = s.get("output") or {}
            if not (out.get("expect") or out.get("description")):
                return json.dumps({"ok": False,
                                   "rejected": f"爬蟲步「{s.get('name')}」缺 output.expect(必填:描述要抓到什麼真實資料)"},
                                  ensure_ascii=False)
    # 4) 建立(名稱由後端自動避重)+ 寫入 yaml 與 canvas
    r = httpx.post(f"{BASE}/workflows", json={"name": name or raw.get("name") or "外部AI建立",
                                              "validate": bool(raw.get("validate", False))}, timeout=30)
    r.raise_for_status()
    wf = r.json()
    from yaml_to_canvas import yaml_to_canvas
    canvas = yaml_to_canvas(yaml_content)
    httpx.put(f"{BASE}/workflows/{wf['id']}",
              json={"yaml": yaml_content, "canvas": canvas}, timeout=30).raise_for_status()
    return json.dumps({"ok": True, "workflow_id": wf["id"], "name": wf.get("name"),
                       "next": "可用 run_workflow 執行;使用者也會在 Atlas 畫布上看到這條工作流"},
                      ensure_ascii=False)


@mcp.tool()
def sandbox_run_python(code: str, working_dir: str = "") -> str:
    """在 Atlas 的執行環境跑一段 Python(迭代調教用;工作目錄限 Atlas 輸出區內)。

    用法:寫碼 → 這裡跑 → 看輸出修正 → 調通後用 submit_step_code 固化成 Recipe。
    """
    r = httpx.post(f"{BASE}/sandbox/run",
                   json={"code": code, "working_dir": working_dir}, timeout=180)
    if r.status_code != 200:
        return json.dumps({"ok": False, "error": r.text[:400]}, ensure_ascii=False)
    d = r.json()
    return json.dumps({"ok": True, "working_dir": d["working_dir"],
                       "output": d["output"]}, ensure_ascii=False)


@mcp.tool()
def submit_step_code(workflow_id: str, step_name: str, code: str,
                     input_paths: list[str] | None = None) -> str:
    """把調通的程式碼提交給某工作流的 AI 技能步驟,固化成 Recipe(0-token 重放資產)。

    Atlas 會:試跑 → 驗證宣告輸出檔真的產生 → 過 json_schema 合約(若有)→ 存 Recipe。
    被拒會回具體原因;修正後重送。input_paths = 該步上游輸入檔(用於輸入指紋;無上游可省略)。
    成功後:這條工作流每次跑到該步、輸入指紋相同,就直接重放你這份碼(不再呼叫任何 LLM)。
    """
    r = httpx.post(f"{BASE}/workflows/{workflow_id}/steps/{step_name}/submit-code",
                   json={"code": code, "input_paths": input_paths or []}, timeout=300)
    if r.status_code != 200:
        return json.dumps({"ok": False, "error": r.text[:400]}, ensure_ascii=False)
    return json.dumps(r.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
