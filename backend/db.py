"""
統一 SQLite 資料庫：workflows、recipes、pipeline_runs。

DB 路徑：~/ai_output/pipeline.db
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from config import OUTPUT_BASE_PATH

DB_PATH = str(OUTPUT_BASE_PATH / "pipeline.db")
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """每個 thread 一個 connection（SQLite thread-safety）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        # WAL 仍只允許單一 writer；前端高頻輪詢 + 背景 run/scheduler 同時寫時，
        # 若無 busy_timeout，writer 拿不到鎖會「立即」拋 database is locked → POST 回 500。
        # 設 5s 等待，讓短暫寫鎖競爭自動退讓重試。
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    """建立所有表格（冪等）。"""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '新工作流',
            yaml       TEXT NOT NULL DEFAULT '',
            canvas     TEXT NOT NULL DEFAULT '{}',
            validate   INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipes (
            id                 TEXT PRIMARY KEY,
            workflow_id        TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            step_name          TEXT NOT NULL,
            task_hash          TEXT NOT NULL,
            input_fingerprints TEXT NOT NULL DEFAULT '{}',
            output_path        TEXT,
            code               TEXT NOT NULL DEFAULT '',
            python_version     TEXT NOT NULL DEFAULT '',
            success_count      INTEGER NOT NULL DEFAULT 0,
            fail_count         INTEGER NOT NULL DEFAULT 0,
            created_at         REAL NOT NULL,
            last_success_at    REAL NOT NULL DEFAULT 0,
            last_fail_at       REAL NOT NULL DEFAULT 0,
            avg_runtime_sec    REAL NOT NULL DEFAULT 0,
            disabled           INTEGER NOT NULL DEFAULT 0,
            was_interactive    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workflow_id, step_name)
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            workflow_id TEXT REFERENCES workflows(id) ON DELETE SET NULL,
            data        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webhooks (
            token         TEXT PRIMARY KEY,
            workflow_id   TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    REAL NOT NULL,
            last_fired_at REAL NOT NULL DEFAULT 0,
            fire_count    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workflow_id)
        );

        CREATE TABLE IF NOT EXISTS folder_watches (
            id              TEXT PRIMARY KEY,
            workflow_id     TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            folder_path     TEXT NOT NULL,
            pattern         TEXT NOT NULL DEFAULT '*',
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL,
            last_seen_mtime REAL NOT NULL DEFAULT 0,
            trigger_count   INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workflow_id)
        );

        CREATE TABLE IF NOT EXISTS mcp_servers (
            name         TEXT PRIMARY KEY,
            command      TEXT NOT NULL,
            args         TEXT NOT NULL DEFAULT '[]',
            env          TEXT NOT NULL DEFAULT '{}',
            enabled      INTEGER NOT NULL DEFAULT 1,
            tools_cache  TEXT NOT NULL DEFAULT '[]',
            created_at   REAL NOT NULL,
            last_scan_at REAL NOT NULL DEFAULT 0,
            transport    TEXT NOT NULL DEFAULT 'stdio',
            url          TEXT NOT NULL DEFAULT ''
        );
    """)
    # 舊 DB 遷移:補 transport / url 欄位(遠端 MCP 支援)。已存在則 ALTER 報錯、吞掉。
    for _col, _def in (("transport", "'stdio'"), ("url", "''")):
        try:
            conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {_col} TEXT NOT NULL DEFAULT {_def}")
        except Exception:
            pass
    conn.commit()

    # 遷移：如果舊 pipeline_runs.db 存在，匯入 runs 資料
    _migrate_old_runs(conn)
    # 遷移：如果舊 recipe JSON 檔案存在，匯入 recipes
    _migrate_old_recipes(conn)
    # 欄位遷移：舊版 recipes 表缺 was_interactive 欄位
    _add_column_if_missing(conn, "recipes", "was_interactive", "INTEGER NOT NULL DEFAULT 0")
    # 欄位遷移：workflows 表新增 chat_messages 欄位（每工作流一條 AI 助手對話）
    # 儲存 JSON 陣列 [{role: 'user'|'assistant', content: str, ts: float}, ...]
    _add_column_if_missing(conn, "workflows", "chat_messages", "TEXT NOT NULL DEFAULT '[]'")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, col_def: str):
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            conn.commit()
    except Exception:
        pass


def _migrate_old_runs(conn: sqlite3.Connection):
    """從舊 pipeline_runs.db 匯入（一次性遷移）。"""
    old_db = OUTPUT_BASE_PATH / "pipeline_runs.db"
    if not old_db.exists():
        return
    try:
        old_conn = sqlite3.connect(str(old_db))
        rows = old_conn.execute("SELECT run_id, data FROM pipeline_runs").fetchall()
        old_conn.close()
        if not rows:
            return
        for run_id, data in rows:
            existing = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT OR IGNORE INTO runs (run_id, workflow_id, data) VALUES (?, NULL, ?)",
                    (run_id, data),
                )
        conn.commit()
        # 遷移完成，重命名舊 DB
        old_db.rename(old_db.with_suffix(".db.migrated"))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"遷移舊 runs 失敗：{e}")


def _migrate_old_recipes(conn: sqlite3.Connection):
    """從舊 recipe JSON 檔案匯入（一次性遷移）。"""
    recipe_root = OUTPUT_BASE_PATH / "pipeline_recipes"
    if not recipe_root.exists():
        return
    try:
        count = 0
        for sub in recipe_root.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.glob("*.json"):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        r = json.load(fh)
                    # 舊 recipe 用 pipeline_name，遷移時先放 workflow_id=NULL
                    # 後續由 workflow 建立時關聯
                    existing = conn.execute(
                        "SELECT 1 FROM recipes WHERE id=?", (r["recipe_id"],)
                    ).fetchone()
                    if not existing:
                        conn.execute("""
                            INSERT OR IGNORE INTO recipes
                            (id, workflow_id, step_name, task_hash, input_fingerprints,
                             output_path, code, python_version, success_count, fail_count,
                             created_at, last_success_at, last_fail_at, avg_runtime_sec, disabled)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            r["recipe_id"],
                            "__legacy__" + r.get("pipeline_id", ""),  # 暫存舊 pipeline_name
                            r["step_name"],
                            r["task_hash"],
                            json.dumps(r.get("input_fingerprints", {}), ensure_ascii=False),
                            r.get("output_path"),
                            r.get("code", ""),
                            r.get("python_version", ""),
                            r.get("success_count", 0),
                            r.get("fail_count", 0),
                            r.get("created_at", time.time()),
                            r.get("last_success_at", 0),
                            r.get("last_fail_at", 0),
                            r.get("avg_runtime_sec", 0),
                            1 if r.get("disabled") else 0,
                        ))
                        count += 1
                except Exception:
                    pass
        if count > 0:
            conn.commit()
            # 遷移完成，重命名舊目錄
            recipe_root.rename(recipe_root.with_suffix(".migrated"))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"遷移舊 recipes 失敗：{e}")


# ── Workflow CRUD ────────────────────────────────────────────────────────────

def create_workflow(name: str = "新工作流", canvas: dict = None, validate: bool = False) -> dict:
    conn = get_conn()
    
    # ── 自動避重名邏輯 ──
    existing_names = {row[0] for row in conn.execute("SELECT name FROM workflows").fetchall()}
    final_name = name
    counter = 1
    while final_name in existing_names:
        final_name = f"{name}({counter})"
        counter += 1
    
    wf_id = f"wf-{uuid.uuid4().hex[:12]}"
    now = time.time()
    canvas_json = json.dumps(canvas or {"nodes": [], "edges": []}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO workflows (id, name, yaml, canvas, validate, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (wf_id, final_name, "", canvas_json, 1 if validate else 0, now, now),
    )
    conn.commit()
    return {"id": wf_id, "name": final_name, "canvas": canvas or {"nodes": [], "edges": []},
            "validate": validate, "created_at": now, "updated_at": now}


def get_workflow(wf_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT id, name, yaml, canvas, validate, created_at, updated_at FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        return None
    return _row_to_workflow(row)


def list_workflows() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, name, yaml, canvas, validate, created_at, updated_at FROM workflows ORDER BY updated_at DESC").fetchall()
    return [_row_to_workflow(r) for r in rows]


def update_workflow(wf_id: str, patch: dict) -> Optional[dict]:
    conn = get_conn()
    existing = get_workflow(wf_id)
    if not existing:
        return None
    sets = []
    vals = []
    if "name" in patch:
        sets.append("name=?"); vals.append(patch["name"])
    if "yaml" in patch:
        sets.append("yaml=?"); vals.append(patch["yaml"])
    if "canvas" in patch:
        sets.append("canvas=?"); vals.append(json.dumps(patch["canvas"], ensure_ascii=False))
    if "validate" in patch:
        sets.append("validate=?"); vals.append(1 if patch["validate"] else 0)
    if not sets:
        return existing
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(wf_id)
    conn.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return get_workflow(wf_id)


def delete_workflow(wf_id: str, cascade: bool = True) -> bool:
    """刪除工作流。cascade=True 時一併刪除 recipes 和 runs。"""
    conn = get_conn()
    if cascade:
        conn.execute("DELETE FROM recipes WHERE workflow_id=?", (wf_id,))
        conn.execute("UPDATE runs SET workflow_id=NULL WHERE workflow_id=?", (wf_id,))
    conn.execute("DELETE FROM workflows WHERE id=?", (wf_id,))
    conn.commit()
    return True


def _row_to_workflow(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "yaml": row[2],
        "canvas": json.loads(row[3]) if row[3] else {"nodes": [], "edges": []},
        "validate": bool(row[4]),
        "created_at": row[5],
        "updated_at": row[6],
    }


# ── Chat CRUD（per-workflow AI 助手對話歷史）────────────────────────────────
# 儲存格式：JSON 陣列；每則訊息 {role: 'user'|'assistant', content: str, ts: float}
# 不更新 workflows.updated_at（聊天不是「真正的工作流改動」，避免推擠排序）

def get_workflow_chat(wf_id: str) -> Optional[list]:
    """回傳指定工作流的對話訊息陣列；workflow 不存在回 None。"""
    conn = get_conn()
    row = conn.execute("SELECT chat_messages FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []


def set_workflow_chat(wf_id: str, messages: list) -> bool:
    """整批寫入對話歷史（取代既有）。workflow 不存在回 False。"""
    conn = get_conn()
    existing = conn.execute("SELECT 1 FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not existing:
        return False
    # 基本 schema 檢查：每筆要有 role + content
    clean = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        entry = {"role": role, "content": content}
        if "ts" in m and isinstance(m["ts"], (int, float)):
            entry["ts"] = m["ts"]
        clean.append(entry)
    conn.execute(
        "UPDATE workflows SET chat_messages=? WHERE id=?",
        (json.dumps(clean, ensure_ascii=False), wf_id),
    )
    conn.commit()
    return True


def append_workflow_chat(wf_id: str, role: str, content: str) -> Optional[list]:
    """在尾端追加一則訊息。回傳新的完整訊息陣列；workflow 不存在回 None。"""
    if role not in ("user", "assistant"):
        return None
    msgs = get_workflow_chat(wf_id)
    if msgs is None:
        return None
    msgs.append({"role": role, "content": content, "ts": time.time()})
    set_workflow_chat(wf_id, msgs)
    return msgs


def clear_workflow_chat(wf_id: str) -> bool:
    """清空對話歷史（使用者按「新話題」）。"""
    return set_workflow_chat(wf_id, [])


# ── Recipe CRUD（改為 workflow_id 關聯）───────────────────────────────────────

def save_recipe(workflow_id: str, step_name: str, task_hash: str,
                input_fingerprints: dict, output_path: Optional[str],
                code: str, python_version: str, runtime_sec: float,
                was_interactive: bool = False) -> dict:
    import hashlib
    conn = get_conn()
    rid = hashlib.sha1(f"{workflow_id}:{step_name}:{task_hash}".encode()).hexdigest()[:16]
    now = time.time()

    fps_json = json.dumps(input_fingerprints, ensure_ascii=False)
    wi = 1 if was_interactive else 0

    # P1 原子性修正:原本 SELECT-then-INSERT/UPDATE 非原子 —— 兩條 run 同時完成同一
    # (workflow, step) 時會雙 INSERT 撞 UNIQUE 而炸。改單條 UPSERT,計數/平均在 SQL 內算。
    conn.execute("""
        INSERT INTO recipes (id, workflow_id, step_name, task_hash, input_fingerprints,
        output_path, code, python_version, success_count, fail_count,
        created_at, last_success_at, avg_runtime_sec, was_interactive)
        VALUES (?,?,?,?,?,?,?,?,1,0,?,?,?,?)
        ON CONFLICT(workflow_id, step_name) DO UPDATE SET
            task_hash=excluded.task_hash,
            input_fingerprints=excluded.input_fingerprints,
            output_path=excluded.output_path,
            code=excluded.code,
            python_version=excluded.python_version,
            success_count=recipes.success_count + 1,
            last_success_at=excluded.last_success_at,
            avg_runtime_sec=(recipes.avg_runtime_sec * recipes.success_count
                             + excluded.avg_runtime_sec) / (recipes.success_count + 1),
            disabled=0,
            was_interactive=excluded.was_interactive
    """, (rid, workflow_id, step_name, task_hash, fps_json,
          output_path, code, python_version, now, now, runtime_sec, wi))
    conn.commit()
    return get_recipe(workflow_id, step_name)


def get_recipe(workflow_id: str, step_name: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM recipes WHERE workflow_id=? AND step_name=?",
        (workflow_id, step_name),
    ).fetchone()
    if not row:
        return None
    return _row_to_recipe(row)


def match_recipe(workflow_id: str, step_name: str, task_hash: str,
                 input_fingerprints: dict) -> Optional[dict]:
    """檢查是否有可重用 recipe：task_hash 吻合、輸入指紋吻合、且未停用。

    輸入指紋比對「只比指紋值的多重集合、忽略路徑 key」(sorted(values))：
    runner 執行時的輸入路徑來自上游步驟輸出(prev_outputs);相對路徑的輸出會被 reroute 進
    per-run 夾 `ai_output/<wf>/run_<時間戳>/`,使指紋 dict 的 key(路徑)帶有 recipe 灌入時
    無法預知的 run 時間戳。若用「含 key 的整個 dict」精確比對,下游步驟的 recipe 會永遠 miss、
    退回弱模型重學。recipe 重播只在乎「輸入的 schema/內容指紋是否一致」,路徑 key 屬附帶資訊,
    故改比對 sorted(values)。(同時修正舊版 `A if cond else B != x` 三元運算子優先序 bug。)
    """
    r = get_recipe(workflow_id, step_name)
    if not r or r["disabled"]:
        return None
    if r["task_hash"] != task_hash:
        return None
    saved = r["input_fingerprints"]
    if isinstance(saved, str):
        saved = json.loads(saved)
    if sorted((saved or {}).values()) != sorted((input_fingerprints or {}).values()):
        return None
    return r


def mark_recipe_failed(workflow_id: str, step_name: str):
    conn = get_conn()
    conn.execute("""
        UPDATE recipes SET fail_count = fail_count + 1, last_fail_at = ?,
        disabled = CASE WHEN fail_count + 1 >= 2 THEN 1 ELSE 0 END
        WHERE workflow_id=? AND step_name=?
    """, (time.time(), workflow_id, step_name))
    conn.commit()


# ── Webhook 觸發器:外部 HTTP POST /webhooks/<token> 觸發工作流 ──────────
def create_webhook(workflow_id: str) -> dict:
    """為 workflow 建立(或重新產生)webhook token。一個 workflow 一個 webhook;重生即換 token。"""
    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    conn = get_conn()
    conn.execute("DELETE FROM webhooks WHERE workflow_id=?", (workflow_id,))
    conn.execute(
        "INSERT INTO webhooks(token, workflow_id, enabled, created_at) VALUES(?,?,1,?)",
        (token, workflow_id, time.time()),
    )
    conn.commit()
    return {"token": token, "workflow_id": workflow_id, "enabled": True}


_WEBHOOK_COLS = ("token", "workflow_id", "enabled", "created_at", "last_fired_at", "fire_count")


def _webhook_row(r) -> Optional[dict]:
    # get_conn 沒設 row_factory → fetchone 回 tuple,用欄位名 zip 成 dict
    return dict(zip(_WEBHOOK_COLS, r)) if r else None


def get_webhook_by_workflow(workflow_id: str) -> Optional[dict]:
    r = get_conn().execute(
        "SELECT token, workflow_id, enabled, created_at, last_fired_at, fire_count "
        "FROM webhooks WHERE workflow_id=?", (workflow_id,)).fetchone()
    return _webhook_row(r)


def get_webhook_by_token(token: str) -> Optional[dict]:
    """只回傳 enabled=1 的(停用 / 不存在 → None,觸發端一律 404)。"""
    r = get_conn().execute(
        "SELECT token, workflow_id, enabled, created_at, last_fired_at, fire_count "
        "FROM webhooks WHERE token=? AND enabled=1", (token,)).fetchone()
    return _webhook_row(r)


def mark_webhook_fired(token: str):
    conn = get_conn()
    conn.execute("UPDATE webhooks SET last_fired_at=?, fire_count=fire_count+1 WHERE token=?",
                 (time.time(), token))
    conn.commit()


def disable_webhook(workflow_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE webhooks SET enabled=0 WHERE workflow_id=?", (workflow_id,))
    conn.commit()
    return cur.rowcount > 0


# ── 檔案夾監看觸發器:folder 出現新檔 → 觸發工作流(輪詢式,免 watchdog)──────
_FWATCH_COLS = ("id", "workflow_id", "folder_path", "pattern", "enabled",
                "created_at", "last_seen_mtime", "trigger_count")


def _fwatch_row(r) -> Optional[dict]:
    return dict(zip(_FWATCH_COLS, r)) if r else None


def create_folder_watch(workflow_id: str, folder_path: str, pattern: str = "*") -> dict:
    """為 workflow 建立(或取代)檔案夾監看。一個 workflow 一個。
    last_seen_mtime 初始化為現在 → 只有「建立之後」新增的檔才觸發,不會對既有檔一次全轟。"""
    import uuid as _uuid
    wid_row = f"fw-{_uuid.uuid4().hex[:12]}"
    conn = get_conn()
    conn.execute("DELETE FROM folder_watches WHERE workflow_id=?", (workflow_id,))
    now = time.time()
    conn.execute(
        "INSERT INTO folder_watches(id, workflow_id, folder_path, pattern, enabled, created_at, last_seen_mtime) "
        "VALUES(?,?,?,?,1,?,?)",
        (wid_row, workflow_id, folder_path, pattern or "*", now, now),
    )
    conn.commit()
    return get_folder_watch_by_workflow(workflow_id)


def get_folder_watch_by_workflow(workflow_id: str) -> Optional[dict]:
    r = get_conn().execute(
        "SELECT id, workflow_id, folder_path, pattern, enabled, created_at, last_seen_mtime, trigger_count "
        "FROM folder_watches WHERE workflow_id=?", (workflow_id,)).fetchone()
    return _fwatch_row(r)


def list_enabled_folder_watches() -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, workflow_id, folder_path, pattern, enabled, created_at, last_seen_mtime, trigger_count "
        "FROM folder_watches WHERE enabled=1").fetchall()
    return [_fwatch_row(r) for r in rows]


def update_folder_watch_progress(watch_id: str, last_seen_mtime: float, triggered: int):
    conn = get_conn()
    conn.execute(
        "UPDATE folder_watches SET last_seen_mtime=?, trigger_count=trigger_count+? WHERE id=?",
        (last_seen_mtime, triggered, watch_id))
    conn.commit()


def disable_folder_watch(workflow_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE folder_watches SET enabled=0 WHERE workflow_id=?", (workflow_id,))
    conn.commit()
    return cur.rowcount > 0


# ── MCP servers 註冊表:像 Skill 掛載一樣管理「已安裝的 MCP server」──────────
#   新增時由 API 層先 live 連線掃 tools 成功才存;tools_cache 供 AI 助手提示詞注入。
_MCP_COLS = ("name", "command", "args", "env", "enabled", "tools_cache", "created_at", "last_scan_at", "transport", "url")


def _mcp_row(r) -> Optional[dict]:
    if not r:
        return None
    import json as _j
    d = dict(zip(_MCP_COLS, r))
    for k in ("args", "env", "tools_cache"):
        try:
            d[k] = _j.loads(d[k] or ("[]" if k != "env" else "{}"))
        except Exception:
            d[k] = [] if k != "env" else {}
    d["enabled"] = bool(d["enabled"])
    return d


def upsert_mcp_server(name: str, command: str, args: list, env: dict, tools: list,
                      transport: str = "stdio", url: str = "") -> dict:
    import json as _j
    conn = get_conn()
    now = time.time()
    conn.execute(
        "INSERT INTO mcp_servers(name, command, args, env, enabled, tools_cache, created_at, last_scan_at, transport, url) "
        "VALUES(?,?,?,?,1,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET command=excluded.command, args=excluded.args, "
        "env=excluded.env, enabled=1, tools_cache=excluded.tools_cache, last_scan_at=excluded.last_scan_at, "
        "transport=excluded.transport, url=excluded.url",
        (name, command, _j.dumps(args or [], ensure_ascii=False), _j.dumps(env or {}, ensure_ascii=False),
         _j.dumps(tools or [], ensure_ascii=False), now, now, transport or "stdio", url or ""))
    conn.commit()
    return get_mcp_server(name)


def get_mcp_server(name: str) -> Optional[dict]:
    r = get_conn().execute(
        "SELECT name, command, args, env, enabled, tools_cache, created_at, last_scan_at, transport, url "
        "FROM mcp_servers WHERE name=?", (name,)).fetchone()
    return _mcp_row(r)


def list_mcp_servers(enabled_only: bool = False) -> list[dict]:
    q = ("SELECT name, command, args, env, enabled, tools_cache, created_at, last_scan_at, transport, url "
         "FROM mcp_servers")
    if enabled_only:
        q += " WHERE enabled=1"
    return [_mcp_row(r) for r in get_conn().execute(q + " ORDER BY name").fetchall()]


def remove_mcp_server(name: str) -> bool:
    """乾淨移除:server 是隨叫隨起的 subprocess(npx/uvx),無常駐、無殘留,刪 DB 列即完成。"""
    conn = get_conn()
    cur = conn.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
    conn.commit()
    return cur.rowcount > 0


def list_recipes(workflow_id: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if workflow_id:
        rows = conn.execute("SELECT * FROM recipes WHERE workflow_id=?", (workflow_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM recipes").fetchall()
    return [_row_to_recipe(r) for r in rows]


def delete_recipe(workflow_id: str, step_name: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM recipes WHERE workflow_id=? AND step_name=?", (workflow_id, step_name))
    conn.commit()
    return cur.rowcount > 0


def delete_workflow_recipes(workflow_id: str) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM recipes WHERE workflow_id=?", (workflow_id,))
    conn.commit()
    return cur.rowcount


def _find_recipe(workflow_id: str, step_name: str) -> Optional[dict]:
    """查找 recipe：先精確匹配，再嘗試「N:name」索引格式（相容新舊 key）。"""
    r = get_recipe(workflow_id, step_name)
    if r:
        return r
    # 新格式：step_name 存為 "1:AI技能 1"，前端傳 "AI技能 1"
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM recipes WHERE workflow_id=? AND step_name LIKE ?",
        (workflow_id, f"%:{step_name}"),
    ).fetchone()
    return _row_to_recipe(row) if row else None


def get_recipe_status(workflow_id: str, step_names: list[str]) -> dict:
    steps_info = {}
    covered = 0
    for name in step_names:
        r = _find_recipe(workflow_id, name)
        if r and not r["disabled"]:
            steps_info[name] = {"has_recipe": True, "success_count": r["success_count"],
                                "avg_runtime_sec": round(r["avg_runtime_sec"], 1)}
            covered += 1
        else:
            steps_info[name] = {"has_recipe": False, "success_count": 0, "avg_runtime_sec": 0}
    return {"has_recipes": covered > 0, "total_skill_steps": len(step_names),
            "covered_steps": covered, "steps": steps_info}


def _row_to_recipe(row) -> dict:
    # row schema: id(0) wf(1) step(2) hash(3) fps(4) out(5) code(6) ver(7)
    #             scnt(8) fcnt(9) created(10) succ_at(11) fail_at(12) runtime(13)
    #             disabled(14) was_interactive(15)
    return {
        "recipe_id": row[0], "workflow_id": row[1], "step_name": row[2],
        "task_hash": row[3],
        "input_fingerprints": json.loads(row[4]) if isinstance(row[4], str) else row[4],
        "output_path": row[5], "code": row[6], "python_version": row[7],
        "success_count": row[8], "fail_count": row[9],
        "created_at": row[10], "last_success_at": row[11], "last_fail_at": row[12],
        "avg_runtime_sec": row[13], "disabled": bool(row[14]),
        "was_interactive": bool(row[15]) if len(row) > 15 else False,
    }


# ── Run CRUD（保持與舊 store.py 相容）──────────────────────────────────────

def save_run(run_data: dict, workflow_id: Optional[str] = None):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, workflow_id, data) VALUES (?,?,?)",
        (run_data["run_id"], workflow_id, json.dumps(run_data, ensure_ascii=False)),
    )
    conn.commit()


def load_run(run_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT data, workflow_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row[0])
    d["_workflow_id"] = row[1]
    return d


def list_runs(limit: int = 20, workflow_id: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if workflow_id:
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs WHERE workflow_id=? ORDER BY rowid DESC LIMIT ?",
            (workflow_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT data, workflow_id FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for data, wid in rows:
        d = json.loads(data)
        d["_workflow_id"] = wid
        result.append(d)
    return result


def delete_run(run_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    conn.commit()
    return cur.rowcount > 0
