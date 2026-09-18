"""YAML → canvas 轉換器。

使用情境：
- TG `/save` 命令把 AI 對話產生的 YAML 套到工作流時、同步重建 canvas（讓桌面開該工作流能看到節點）
- 一次性遷移 / 修復「YAML 有但 canvas 空」的工作流

把 PipelineConfig 風格的 YAML（pyyaml 解析後的 dict）轉成 frontend stepsToFlow 期望的
{nodes: [...], edges: [...]} 格式。各種節點類型旗標的對應：
- web_crawler / visual_validation / outlook_automation / computer_use /
  ai_validation / human_confirm / skill_mode / condition / subagent → 對應 React Flow node type
- 預設 → scriptStep

版面：condition（IF / Switch）分支會「扇形」攤開 —— 主線一排、分支對稱往上下展，
每條分支各自往右接成一列。讓使用者一眼看懂「這裡分流、各走各的」，不用解讀交叉的線。
"""
from __future__ import annotations

import yaml as _yaml
from typing import Optional

# 版面常數
_COL_W = 360   # 每一「欄」（深度）的水平間距
_ROW_H = 200   # 每一「列」（分支）的垂直間距
_Y0 = 160      # 主線的基準 y


def _step_to_node(step: dict, idx: int) -> dict:
    """把 YAML step dict 轉成 canvas node dict（對應 stepsToFlow 的輸出格式）。

    position 先放預設值、由 yaml_to_canvas() 算出扇形座標後覆寫。
    """
    name = step.get("name", f"步驟 {idx + 1}")
    common = {
        "id": f"step-{idx}",
        "position": {"x": idx * _COL_W, "y": _Y0},
    }
    base_data = {
        "name": name,
        "index": idx,
        "status": "idle",
        "errorMsg": "",
        "timeout": step.get("timeout", 300),
        "retry": step.get("retry", 1),
        # next:控制流跳轉(condition 分支用 next: end 防掉進對方分支)。
        # 任何節點都可能有,放 base_data 確保不會在 YAML↔canvas 來回時掉。
        "next": step.get("next", ""),
        # llm_role:主/副模型指派。任何 LLM 節點(skill/subagent/outlook/cu/vv...)都可能有;
        # 之前只有 subagent 寫、skill 等節點漏 → canvas round-trip 把 llm_role 洗掉
        # (實測:user 在 YAML 給 skill 步加 llm_role: secondary,前端 autosave 後消失)。
        "llmRole": step.get("llm_role", "primary"),
    }
    output = step.get("output") or {}
    output_path = output.get("path", "") if isinstance(output, dict) else ""
    # expect / json_schema:任何節點的 output 都可能帶(驗證閘規格)。
    # 之前只有 script/skill 分支帶 expect、全部分支都不帶 json_schema →
    # 前端從 canvas 讀不到 → autosave round-trip 把 YAML 裡寫好的驗證整批洗掉。
    # 對齊 next/llmRole 策略:統一塞進 base_data、所有節點型別通吃。
    if isinstance(output, dict):
        import json as _json_s
        base_data["expectText"] = output.get("expect") or output.get("description") or ""
        _js = output.get("json_schema")
        base_data["jsonSchemaText"] = (
            _json_s.dumps(_js, ensure_ascii=False) if isinstance(_js, dict) and _js else ""
        )
    else:
        base_data["expectText"] = ""
        base_data["jsonSchemaText"] = ""

    # human_confirm
    if step.get("human_confirm"):
        # message 存在 YAML 的 message: 欄(前端 stepsToYaml 寫的),不是 batch —
        # 之前讀 batch 導致自訂訊息在 round-trip 後全變預設「請確認」。
        # notify/screenshot/preview/send_prev/hc_on_timeout 同理:canvas 不帶就會被 autosave 洗掉。
        return {**common, "type": "humanConfirmation", "data": {
            **base_data,
            "message": step.get("message") or step.get("batch") or "請確認",
            "outputPath": output_path,
            "sendOutput": step.get("send_output", True),
            "notifyTelegram": step.get("notify_telegram", True),
            "screenshot": bool(step.get("screenshot", False)),
            "previewPrevOutput": bool(step.get("preview_prev_output", False)),
            "sendPrevOutput": bool(step.get("send_prev_output", False)),
            "hcOnTimeout": step.get("hc_on_timeout", "wait"),
        }}

    # visual_validation
    if step.get("visual_validation"):
        return {**common, "type": "visualValidation", "data": {
            **base_data,
            "source": step.get("vv_source", "prev_output"),
            "prompt": step.get("vv_prompt", ""),
            "searchRegion": step.get("vv_search_region", []),
        }}

    # outlook_automation
    if step.get("outlook_automation"):
        return {**common, "type": "outlookAutomation", "data": {
            **base_data,
            "template": step.get("outlook_template", ""),
            # 自由指令的正規欄位是 batch(runner 讀 step.batch);outlook_free_text 只是畫布別名。
            # 沒模板、只用 batch 寫自由需求時要 fallback,否則畫布 freeText 空 → 前端誤判「沒選模板也沒描述」。
            "freeText": step.get("outlook_free_text") or step.get("batch", ""),
            "params": step.get("outlook_params", {}),
            "outputPath": output_path,
        }}

    # web_crawler
    if step.get("web_crawler"):
        return {**common, "type": "webCrawler", "data": {
            **base_data,
            "mode": step.get("wc_mode", "web"),
            "url": step.get("wc_url", ""),
            "urls": step.get("wc_urls", []),
            "jsRender": step.get("wc_js_render", True),
            "waitForSelector": step.get("wc_wait_for_selector", ""),
            "cloudflareFallback": step.get("wc_cloudflare_fallback", True),
            "cookies": step.get("wc_cookies", ""),
            "interactions": step.get("wc_interactions", []),
            "downloadAssets": step.get("wc_download_assets", False),
            "scrollCount": step.get("wc_scroll_count", 0),
            "targetPostCount": step.get("wc_target_post_count", 0),
            "withChildren": step.get("wc_with_children", False),
            "childLinkPattern": step.get("wc_child_link_pattern", ""),
            "maxChildren": step.get("wc_max_children", 10),
            "videoUrl": step.get("wc_video_url", ""),
            "outputPath": output_path,
        }}

    # computer_use
    if step.get("computer_use"):
        return {**common, "type": "computerUse", "data": {
            **base_data,
            # ⚠ 這三個 key 一定要跟 YAML/models.py 一致。曾經寫成 computer_use_actions /
            #   computer_use_assets_dir / computer_use_fail_fast —— 那些名字**全 codebase
            #   只出現在這裡**,實際 YAML 用的是 actions / assets_dir / fail_fast。
            #   後果是資料損壞:轉出來的 canvas 動作是空的,使用者一碰畫布,
            #   autosave 就用空 canvas 重生 YAML 蓋回 DB,辛苦挑的 auto_id 永久消失。
            "actions": step.get("actions", []),
            "assetsDir": step.get("assets_dir", ""),
            "failFast": step.get("fail_fast", True),
            # cuMode / uiaWindow 之前完全沒讀 → UIA 節點會被重生成 pixel 模式、視窗設定消失
            "cuMode": step.get("cu_mode", "pixel"),
            "uiaWindow": step.get("uia_window", ""),
            "cuVlmCheckStrategy": step.get("cu_vlm_check_strategy", "off"),
            "cuOnMismatch": step.get("cu_on_mismatch", "stop_notify"),
            "cuVlmMaxRetries": step.get("cu_vlm_max_retries", 1),
            "cvThreshold": step.get("cv_threshold", 0.5),
            "cvSearchOnlyNear": step.get("cv_search_only_near", False),
            "cvSearchRadius": step.get("cv_search_radius", 400),
            "cvTriggerHover": step.get("cv_trigger_hover", True),
            "cvHoverWaitMs": step.get("cv_hover_wait_ms", 200),
            "cvCoordFallback": step.get("cv_coord_fallback", False),
            "ocrThreshold": step.get("ocr_threshold", 0.6),
            "ocrCvFallback": step.get("ocr_cv_fallback", False),
        }}

    # ai_validation
    if step.get("ai_validation"):
        return {**common, "type": "aiValidation", "data": {
            **base_data,
            "criteria": step.get("ai_validation_criteria", ""),
            "source": step.get("ai_validation_source", "prev_output"),
        }}

    # skill_mode
    if step.get("skill_mode"):
        return {**common, "type": "skillStep", "data": {
            **base_data,
            "taskDescription": step.get("batch", ""),
            "workingDir": step.get("working_dir", ""),
            "outputPath": output_path,
            # expect 正確層級在 output: 底下(base_data 已抽好);頂層 expect 是舊格式後備
            "expectedOutput": base_data.get("expectText") or step.get("expect", ""),
            "readonly": step.get("readonly", False),
            "skill": step.get("skill", ""),
            "askMode": step.get("ask_mode", False),
        }}

    # condition(條件判斷節點)
    if step.get("condition"):
        has_switch = bool(step.get("switch"))
        return {**common, "type": "condition", "data": {
            **base_data,
            "mode": "switch" if has_switch else "if",
            "expression": step.get("expression", ""),
            "onTrue": step.get("on_true", ""),
            "onFalse": step.get("on_false", ""),
            "switch": step.get("switch", ""),
            "cases": step.get("cases", {}) or {},
            "default": step.get("default", ""),
        }}

    # subagent(多輪代理節點)
    if step.get("subagent"):
        return {**common, "type": "subagent", "data": {
            **base_data,
            "taskDescription": step.get("batch", ""),
            "workingDir": step.get("working_dir", ""),
            "outputPath": output_path,
            "role": step.get("subagent_role", "data_analyst"),
            "maxIter": step.get("subagent_max_iter", 5),
            "llmRole": step.get("llm_role", "primary"),
        }}

    # mcp(MCP server 工具節點)— data 欄位對齊前端 McpData(_helpers.ts)
    if step.get("mcp"):
        import json as _json
        _targs = step.get("mcp_tool_args") or {}
        return {**common, "type": "mcpStep", "data": {
            **base_data,
            "server": step.get("mcp_server", ""),
            "tool": step.get("mcp_tool", ""),
            # 前端 panel 以文字編輯參數(JSON);dict → pretty JSON 字串
            "toolArgsText": _json.dumps(_targs, ensure_ascii=False, indent=2) if _targs else "",
            "timeout": step.get("mcp_timeout", 60),
            "outputPath": output_path,
        }}

    # 預設 script step
    return {**common, "type": "scriptStep", "data": {
        **base_data,
        "batch": step.get("batch", ""),
        "workingDir": step.get("working_dir", ""),
        "outputPath": output_path,
        # 前端 script 節點(StepData spread)讀的是 expect 鍵;expectedOutput 留著向後相容
        "expect": base_data.get("expectText") or step.get("expect", ""),
        "expectedOutput": base_data.get("expectText") or step.get("expect", ""),
    }}


def _is_end(nxt) -> bool:
    """next 是否為「分支終點、不再往下」標記。"""
    return isinstance(nxt, str) and nxt.strip().lower() == "end"


def _build_graph(steps: list[dict]) -> tuple[list[list[int]], list[dict]]:
    """從 steps 算出每個節點的出邊。

    回傳 (out, edges):
    - out[i] = 該節點往哪些節點(target index 清單)
    - edges  = [{"i", "j", "handle"}, ...]，handle 是 condition 分支標籤(顯示用)

    連線規則:
    - condition 節點 → 依 cases(Switch) 或 on_true/on_false(IF) 連到具名目標
    - 其他節點有 next: end → 不連(分支終點)
    - 其他節點有 next: <名稱> → 連到該節點
    - 其他節點無 next → 線性連到下一步(i → i+1)
    """
    n = len(steps)
    name_to_idx: dict[str, int] = {}
    for i, s in enumerate(steps):
        nm = s.get("name")
        if nm and str(nm) not in name_to_idx:
            name_to_idx[str(nm)] = i

    out: list[list[int]] = [[] for _ in range(n)]
    edges: list[dict] = []

    for i, s in enumerate(steps):
        targets: list[tuple[int, Optional[str]]] = []
        if s.get("condition"):
            if s.get("switch"):
                for label, tgt in (s.get("cases") or {}).items():
                    j = name_to_idx.get(str(tgt))
                    if j is not None:
                        targets.append((j, str(label)))
                dflt = s.get("default")
                if dflt and str(dflt) in name_to_idx:
                    targets.append((name_to_idx[str(dflt)], "預設"))
            else:
                ot = s.get("on_true")
                if ot and str(ot) in name_to_idx:
                    targets.append((name_to_idx[str(ot)], "成立"))
                of = s.get("on_false")
                if of and str(of) in name_to_idx:
                    targets.append((name_to_idx[str(of)], "不成立"))
        else:
            nxt = s.get("next")
            if _is_end(nxt):
                pass
            elif nxt:
                j = name_to_idx.get(str(nxt))
                if j is not None:
                    targets.append((j, None))
            elif i + 1 < n:
                targets.append((i + 1, None))

        # 同一目標去重:default 與某個 case 指向同一節點時(如 default 與「評價分歧」
        # 都指「產生分歧分析」)只畫一條邊、避免畫面上重複箭頭。先處理的 case 標籤較有意義、保留它。
        seen_j: set[int] = set()
        for j, handle in targets:
            if j in seen_j:
                continue
            seen_j.add(j)
            out[i].append(j)
            edges.append({"i": i, "j": j, "handle": handle})

    return out, edges


def _layout(n: int, out: list[list[int]]) -> list[dict]:
    """算出每個節點的扇形座標。

    - 欄(x):從起點算最長路徑深度
    - 列(y):沿邊 DFS;單一出邊 → 同列;多出邊(condition)→ 對稱往上下攤開
    """
    if n == 0:
        return []

    # 欄:最長路徑(放寬鬆弛,DAG 收斂)
    col = [0] * n
    for _ in range(n):
        changed = False
        for i in range(n):
            for j in out[i]:
                if col[j] < col[i] + 1:
                    col[j] = col[i] + 1
                    changed = True
        if not changed:
            break

    # 列:DFS,condition 多出邊對稱攤開
    lane: dict[int, float] = {}

    def dfs(i: int, l: float) -> None:
        if i in lane:
            return
        lane[i] = l
        ch = out[i]
        if len(ch) <= 1:
            for c in ch:
                dfs(c, l)
        else:
            k = len(ch)
            for idx, c in enumerate(ch):
                dfs(c, l + (idx - (k - 1) / 2.0))

    dfs(0, 0.0)
    for i in range(n):
        lane.setdefault(i, 0.0)

    # 匯流置中:多條分支收斂回同一節點時(如 IF 兩條路最後都接同一份報告),
    # 把該節點擺在各前驅的垂直中點、其後的單線子節點一起跟著移,線才不會歪。
    preds: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in out[i]:
            preds[j].append(i)
    for j in sorted(range(n), key=lambda x: col[x]):
        if len(preds[j]) >= 2:
            lane[j] = sum(lane[p] for p in preds[j]) / len(preds[j])
            cur = j
            while len(out[cur]) == 1 and len(preds[out[cur][0]]) < 2:
                nxt = out[cur][0]
                lane[nxt] = lane[cur]
                cur = nxt

    return [
        {"x": col[i] * _COL_W, "y": _Y0 + lane[i] * _ROW_H}
        for i in range(n)
    ]


def yaml_to_canvas(yaml_str: str) -> Optional[dict]:
    """解析 YAML 字串、產出 canvas dict（{nodes, edges}）。

    回傳 None：YAML 為空、無 steps、解析失敗。
    回傳 dict：{nodes: [...], edges: [...]}
    - 線性工作流 → 一排;有 condition 分支 → 扇形攤開
    - edges 依 condition 的 cases/on_true/on_false 與各步 next 連接
    """
    if not yaml_str or not yaml_str.strip():
        return None
    try:
        parsed = _yaml.safe_load(yaml_str)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    raw_steps = parsed.get("steps") or []
    if not isinstance(raw_steps, list) or len(raw_steps) == 0:
        return None
    steps = [s for s in raw_steps if isinstance(s, dict)]
    if not steps:
        return None

    nodes = [_step_to_node(s, i) for i, s in enumerate(steps)]
    out, raw_edges = _build_graph(steps)
    positions = _layout(len(nodes), out)
    for node, pos in zip(nodes, positions):
        node["position"] = pos

    edges = [
        {
            "id": f"edge-{e['i']}-{e['j']}",
            "source": f"step-{e['i']}",
            "target": f"step-{e['j']}",
        }
        for e in raw_edges
    ]

    return {"nodes": nodes, "edges": edges}
