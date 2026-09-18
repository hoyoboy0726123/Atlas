"""
Pipeline Run 狀態持久化（使用統一 SQLite DB）。

每次 pipeline 執行建立一個 PipelineRun 記錄，
包含每步的執行結果與驗證結論，支援暫停後恢復。
"""
import json
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

from db import get_conn


@dataclass
class StepResult:
    step_index: int
    step_name: str
    exit_code: int
    stdout_tail: str        # 最後 ~500 字（完整輸出在 log 檔）
    stderr_tail: str        # 最後 ~200 字
    validation_status: str  # "ok" | "warning" | "failed"
    validation_reason: str
    validation_suggestion: str
    retries_used: int = 0
    # 步驟在 workflow 輸出資料夾實際產生 / 修改的主要檔案絕對路徑。
    # 用 dir-snapshot 比對 mtime 算出來、給 TG「取任一步輸出」用。
    # 沒明確 output.path 的 skill 節點，用這欄就能拿到該步真正寫的檔，
    # 不會跟其他 skill 節點搶到同一個「workflow dir 最新檔」。
    actual_output_path: str = ""
    # 該步驟用了多少 LLM token（subagent / skill 多輪 loop 累計；script / human_confirm 等保持空）。
    # 結構: {"input_tokens": int, "output_tokens": int, "total_tokens": int, "model": str}
    token_usage: dict = field(default_factory=dict)
    # 該步驟內部的 tool 呼叫時間軸（subagent / skill 用；給前端 trace 視圖渲染）。
    # 每筆: {"name": str, "input_preview": str(<=200), "result_preview": str(<=300)}
    tool_calls: list = field(default_factory=list)
    # ISO 時間戳（給 trace 算 step 耗時、subagent / skill loop 第一輪 LLM call 前 / 最後 done 後）。
    started_at: str = ""
    ended_at: str = ""
    # 該步驟匯出的變數（UIA save_as / Computer Use save_as / Skill 顯式 export 等）。
    # 之後 step 可用 `{{ steps.<name>.output.<key> }}` 引用（pipeline.expression 會 promote 到 output namespace）。
    step_vars: dict = field(default_factory=dict)


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    config_dict: dict
    current_step: int = 0
    step_results: list = field(default_factory=list)  # list[StepResult]
    status: str = "running"   # running | awaiting_human | completed | failed | aborted
    telegram_chat_id: Optional[int] = None
    log_path: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    ended_at: Optional[str] = None
    workflow_id: Optional[str] = None
    pending_recipes: list = field(default_factory=list)  # list[dict] — 延遲儲存的 recipes
    awaiting_type: str = ""       # "" | "failure" | "human_confirm" | "self_heal"(AI 修復中過渡狀態)
    awaiting_message: str = ""    # 人工確認節點的自訂訊息 / 失敗原因
    awaiting_suggestion: str = "" # 失敗時的解決建議（套件安裝、工具選擇等）
    # 啟動 workflow 時傳入的參數（POST /pipeline/run 的 input_params body）。
    # 在 Jinja2 render 階段以 `{{ input.<key> }}` 引用。沒傳就空 dict、舊 workflow 不受影響。
    input_params: dict = field(default_factory=dict)
    # 自我修復:這次 run 已自動修復幾次(跨重跑持久化、達上限就轉人工)、每次修復的診斷歷史
    # (回灌進下次修復 prompt 避免越改越糟)。舊 run 反序列化缺這兩欄 → 靠 default 補、不會壞。
    self_heal_count: int = 0
    self_heal_history: list = field(default_factory=list)  # [{attempt, diagnosis, old_yaml_hash}]


class RunStore:
    def save(self, run: PipelineRun):
        conn = get_conn()
        raw = asdict(run)
        raw["step_results"] = [
            asdict(s) if isinstance(s, StepResult) else s
            for s in run.step_results
        ]
        workflow_id = raw.pop("workflow_id", None)
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, workflow_id, data) VALUES (?, ?, ?)",
            (run.run_id, workflow_id, json.dumps(raw, ensure_ascii=False)),
        )
        conn.commit()

    def load(self, run_id: str) -> Optional[PipelineRun]:
        conn = get_conn()
        row = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        d["step_results"] = [StepResult(**s) for s in d.get("step_results", [])]
        d["workflow_id"] = row[1]
        return PipelineRun(**d)

    def list_recent(self, limit: int = 10) -> list[PipelineRun]:
        conn = get_conn()
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for data, wid in rows:
            d = json.loads(data)
            d["step_results"] = [StepResult(**s) for s in d.get("step_results", [])]
            d["workflow_id"] = wid
            result.append(PipelineRun(**d))
        return result

    def list_by_workflow(self, wf_id: str, limit: int = 10) -> list[PipelineRun]:
        """只取某 workflow 的 run(新→舊)。給「可用變數」查詢用 ——
        不受全域 list_recent 視窗影響、跨多次 run 都抓得到。"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE workflow_id=? ORDER BY rowid DESC LIMIT ?",
            (wf_id, limit),
        ).fetchall()
        result = []
        for data, wid in rows:
            d = json.loads(data)
            d["step_results"] = [StepResult(**s) for s in d.get("step_results", [])]
            d["workflow_id"] = wid
            result.append(PipelineRun(**d))
        return result

    def delete(self, run_id: str) -> bool:
        conn = get_conn()
        cursor = conn.execute(
            "DELETE FROM runs WHERE run_id=?", (run_id,)
        )
        conn.commit()
        return cursor.rowcount > 0

    def list_awaiting(self) -> list[PipelineRun]:
        """回傳所有正在等待人為決策的 run"""
        conn = get_conn()
        rows = conn.execute("SELECT data, workflow_id FROM runs").fetchall()
        result = []
        for data, wid in rows:
            d = json.loads(data)
            if d.get("status") == "awaiting_human":
                d["step_results"] = [StepResult(**s) for s in d.get("step_results", [])]
                d["workflow_id"] = wid
                result.append(PipelineRun(**d))
        return result


_store: Optional[RunStore] = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store
