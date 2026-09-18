"""Run Diff — 兩次執行的逐步輸出差異(競品監控類工作流的殺手鐧)。

「今天比昨天多了哪台新機 / 價格變了多少」直接可視化,不用人工開兩份檔案對照。
比較策略(按輸出檔型別):
  - .json:鍵值差異(added / removed / changed,dot-path 攤平,含數值變化量)
  - 文字(.md/.txt/.csv/.log/.html):unified diff(截行)+ 增刪行統計
  - 其他(binary / 資料夾 / 缺檔):存在性 + 大小變化
"""
from __future__ import annotations

import difflib
import json
import os
from typing import Any, Optional

_TEXT_EXTS = {".md", ".txt", ".csv", ".log", ".html", ".yaml", ".yml", ".py", ".js", ".ts"}
_MAX_TEXT = 1_500_000       # 單檔最多讀 1.5MB(夠比、防爆)
_MAX_DIFF_LINES = 300       # unified diff 最多回傳行數
_MAX_JSON_CHANGES = 80      # JSON 差異最多回傳筆數


def _read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > _MAX_TEXT:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(_MAX_TEXT)
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def _flatten(obj: Any, prefix: str = "", out: dict = None) -> dict:
    """JSON 攤平成 dot-path → 純量;list 以索引展開(前 50 項)。"""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            _flatten(v, f"{prefix}[{i}]", out)
        if len(obj) > 50:
            out[f"{prefix}.__len__"] = len(obj)
    else:
        out[prefix or "(root)"] = obj
    return out


def _diff_json(path_a: str, path_b: str) -> dict:
    try:
        with open(path_a, encoding="utf-8-sig") as f:
            a = json.load(f)
        with open(path_b, encoding="utf-8-sig") as f:
            b = json.load(f)
    except Exception as e:
        return {"kind": "error", "error": f"JSON 讀取失敗:{e}"}
    fa, fb = _flatten(a), _flatten(b)
    added = [{"key": k, "value": fb[k]} for k in fb if k not in fa]
    removed = [{"key": k, "value": fa[k]} for k in fa if k not in fb]
    changed = []
    for k in fa:
        if k in fb and fa[k] != fb[k]:
            item = {"key": k, "before": fa[k], "after": fb[k]}
            if isinstance(fa[k], (int, float)) and isinstance(fb[k], (int, float)) \
                    and not isinstance(fa[k], bool) and not isinstance(fb[k], bool):
                item["delta"] = round(fb[k] - fa[k], 6)
            changed.append(item)
    total = len(added) + len(removed) + len(changed)
    return {
        "kind": "json",
        "identical": total == 0,
        "added": added[:_MAX_JSON_CHANGES],
        "removed": removed[:_MAX_JSON_CHANGES],
        "changed": changed[:_MAX_JSON_CHANGES],
        "truncated": total > _MAX_JSON_CHANGES,
        "summary": (f"{len(added)} 新增鍵、{len(removed)} 移除鍵、{len(changed)} 變更"
                    if total else "內容相同"),
    }


def _diff_text(path_a: str, path_b: str) -> dict:
    ta, tb = _read_text(path_a), _read_text(path_b)
    if ta is None or tb is None:
        return {"kind": "error", "error": "文字檔讀取失敗"}
    if ta == tb:
        return {"kind": "text", "identical": True, "summary": "內容相同", "diff": "", "plus": 0, "minus": 0}
    la, lb = ta.splitlines(), tb.splitlines()
    diff_lines = list(difflib.unified_diff(la, lb, fromfile="上次", tofile="這次", lineterm="", n=2))
    plus = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    minus = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    truncated = len(diff_lines) > _MAX_DIFF_LINES
    return {
        "kind": "text", "identical": False,
        "diff": "\n".join(diff_lines[:_MAX_DIFF_LINES]),
        "truncated": truncated,
        "plus": plus, "minus": minus,
        "summary": f"+{plus} 行 / -{minus} 行" + ("(diff 截斷)" if truncated else ""),
    }


def _diff_output(path_a: Optional[str], path_b: Optional[str]) -> dict:
    ea = bool(path_a and os.path.isfile(path_a))
    eb = bool(path_b and os.path.isfile(path_b))
    if not ea and not eb:
        return {"kind": "none", "summary": "兩次皆無輸出檔"}
    if ea != eb:
        return {"kind": "presence", "summary": ("這次新增輸出檔" if eb else "這次缺輸出檔(上次有)"),
                "identical": False}
    ext = os.path.splitext(path_b)[1].lower()
    if ext == ".json":
        return _diff_json(path_a, path_b)
    if ext in _TEXT_EXTS:
        return _diff_text(path_a, path_b)
    sa, sb = os.path.getsize(path_a), os.path.getsize(path_b)
    return {"kind": "binary", "identical": sa == sb,
            "summary": f"二進位檔,大小 {sa} → {sb} bytes" + ("(相同)" if sa == sb else f"(Δ{sb - sa:+d})")}


def diff_runs(run_a: dict, run_b: dict) -> dict:
    """run_a = 較舊(基準)、run_b = 較新。以 step_name 對齊逐步比較。"""
    sa = {s.get("step_name"): s for s in (run_a.get("step_results") or [])}
    sb = {s.get("step_name"): s for s in (run_b.get("step_results") or [])}
    names = list(dict.fromkeys(list(sa.keys()) + list(sb.keys())))  # 保序聯集
    steps = []
    changed_count = 0
    for n in names:
        a, b = sa.get(n), sb.get(n)
        entry: dict = {"step_name": n,
                       "in_a": a is not None, "in_b": b is not None,
                       "validation_a": (a or {}).get("validation_status"),
                       "validation_b": (b or {}).get("validation_status")}
        if a and b:
            entry["output"] = _diff_output(a.get("actual_output_path"), b.get("actual_output_path"))
        elif b:
            entry["output"] = {"kind": "presence", "summary": "此步驟為這次新增", "identical": False}
        else:
            entry["output"] = {"kind": "presence", "summary": "此步驟這次不存在(上次有)", "identical": False}
        if not entry["output"].get("identical", False) and entry["output"].get("kind") != "none":
            changed_count += 1
        steps.append(entry)
    return {
        "run_a": {"run_id": run_a.get("run_id"), "started_at": run_a.get("started_at"),
                  "status": run_a.get("status")},
        "run_b": {"run_id": run_b.get("run_id"), "started_at": run_b.get("started_at"),
                  "status": run_b.get("status")},
        "steps": steps,
        "changed_steps": changed_count,
        "summary": (f"{changed_count}/{len(steps)} 個步驟輸出有變化" if steps else "無步驟可比"),
    }
