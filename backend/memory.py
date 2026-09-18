"""AI 助手長期記憶 — 階段1:Semantic facts(語意記憶 / 偏好)。

設計參考 project-to-agent phase14-memory,針對本專案調整:
- 全域單使用者(user_id 預設 'local';保留欄位,未來多人可擴)
- category 針對「工作流助手」場景:workflow_pref / domain / past_decision / vocabulary / fact
- sqlite 單檔,放 OUTPUT_BASE_PATH/agent_memory/memory.db
- 敏感資料 hard deny-list(API key / 密碼等拒記)
- 階段2 再加 episodes(對話摘要 + 向量/關鍵字檢索)+ 保守自動萃取

記憶寫入走 chat_tools 的兩步確認(confirm),本模組只負責 storage。
"""
from __future__ import annotations
import json
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from config import OUTPUT_BASE_PATH

try:
    import numpy as _np
except Exception:
    _np = None

_DB_PATH = OUTPUT_BASE_PATH / "agent_memory" / "memory.db"
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# 合法 category(工作流助手場景)
CATEGORIES = ("workflow_pref", "domain", "past_decision", "vocabulary", "fact", "preference")

# 敏感資料 hard deny-list:value 命中就拒記(避免 agent 偷記 key / 密碼)
_SENSITIVE = [
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),          # OpenAI-style key
    re.compile(r"AIza[a-zA-Z0-9_\-]{30,}"),       # Google API key
    re.compile(r"gsk_[a-zA-Z0-9]{20,}"),          # Groq key
    re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}"),    # Anthropic key
    re.compile(r"bearer\s+[a-zA-Z0-9._\-]{12,}", re.I),
    re.compile(r"\b(password|passwd|pwd|密碼)\b\s*[:=]", re.I),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bid_rsa\b|\bid_ed25519\b"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    user_id     TEXT NOT NULL DEFAULT 'local',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'fact',
    source      TEXT NOT NULL DEFAULT 'user_told',  -- 'user_told' / 'inferred'
    confidence  REAL NOT NULL DEFAULT 1.0,          -- inferred 較低
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id, updated_at DESC);

-- 情節記憶:對話摘要(+ 可選 embedding 做語意檢索)
CREATE TABLE IF NOT EXISTS episodes (
    user_id     TEXT NOT NULL DEFAULT 'local',
    conv_key    TEXT NOT NULL,                  -- 同一對話的識別、滾動更新摘要(不重複塞)
    summary     TEXT NOT NULL,                  -- LLM 1-3 句摘要
    embedding   BLOB,                           -- numpy float32 bytes;NULL = 無向量、走關鍵字
    tags        TEXT DEFAULT '',
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, conv_key)
);
CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id, updated_at DESC);
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def is_sensitive(value: str) -> Optional[str]:
    """value 含敏感樣式 → 回觸發的描述;否則 None。"""
    for pat in _SENSITIVE:
        if pat.search(value or ""):
            return pat.pattern
    return None


def remember_fact(key: str, value: str, category: str = "fact",
                  source: str = "user_told", confidence: float = 1.0,
                  user_id: str = "local") -> dict:
    """記一個事實/偏好。敏感資料拒記。回 dict 結果。"""
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return {"ok": False, "error": "key 與 value 都不可空"}
    hit = is_sensitive(value)
    if hit:
        return {"ok": False, "error": f"拒記:value 像敏感資料(樣式 {hit})、不收進記憶"}
    if category not in CATEGORIES:
        category = "fact"
    now = int(time.time())
    with _lock:
        db = _db()
        prev = db.execute("SELECT value FROM facts WHERE user_id=? AND key=?",
                          (user_id, key)).fetchone()
        db.execute(
            "INSERT OR REPLACE INTO facts(user_id, key, value, category, source, confidence, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, key, value, category, source, float(confidence), now),
        )
        db.commit()
    return {"ok": True, "key": key, "value": value, "category": category,
            "previous": prev[0] if prev else None, "updated_at": now}


def recall_fact(key: str, user_id: str = "local") -> dict:
    with _lock:
        row = _db().execute(
            "SELECT value, category, source, confidence, updated_at FROM facts WHERE user_id=? AND key=?",
            (user_id, key)).fetchone()
    if not row:
        return {"found": False, "key": key}
    return {"found": True, "key": key, "value": row[0], "category": row[1],
            "source": row[2], "confidence": row[3], "updated_at": row[4]}


def list_facts(category: Optional[str] = None, limit: int = 50,
               user_id: str = "local") -> list[dict]:
    q = "SELECT key, value, category, source, confidence, updated_at FROM facts WHERE user_id=?"
    args: list = [user_id]
    if category:
        q += " AND category=?"
        args.append(category)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    with _lock:
        rows = _db().execute(q, args).fetchall()
    return [{"key": r[0], "value": r[1], "category": r[2], "source": r[3],
             "confidence": r[4], "updated_at": r[5]} for r in rows]


def forget_fact(key: str, user_id: str = "local") -> dict:
    with _lock:
        db = _db()
        cur = db.execute("DELETE FROM facts WHERE user_id=? AND key=?", (user_id, key))
        db.commit()
    return {"ok": True, "deleted": cur.rowcount, "key": key}


def count_facts(user_id: str = "local") -> int:
    with _lock:
        row = _db().execute("SELECT COUNT(*) FROM facts WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else 0


def snapshot(limit: int = 25, user_id: str = "local") -> list[dict]:
    """給 system prompt 注入用:最近更新的 top-N facts。
    25 筆 ≈ 1-2K token,個人偏好通常 <25 筆能全帶進 context、recall 不漏;
    超過 25 筆才需靠 list_facts 主動查(system prompt 有引導)。"""
    return list_facts(limit=limit, user_id=user_id)


# ─── Episodic(對話摘要 + 向量/關鍵字檢索)──────────────────────
# 地端向量模型(provider != gemini 時用、資料完全不外送):
#   paraphrase-multilingual-MiniLM-L12-v2(多語言含繁中、384 維、僅 ~220MB、fastembed ONNX 跑、不拖 torch)。
# 本應用首次啟動會在背景預載(下載到 HF cache);沒裝 fastembed / 下載失敗 → 降級關鍵字檢索 + 啟動時提示。
_LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_local_embedder = None
_local_failed = False


def _get_local_embedder():
    """lazy 取得地端 fastembed embedder(單例;首次會載/下載模型)。失敗回 None 並記旗標。"""
    global _local_embedder, _local_failed
    if _local_embedder is not None or _local_failed:
        return _local_embedder
    try:
        from fastembed import TextEmbedding
        _local_embedder = TextEmbedding(model_name=_LOCAL_MODEL)
    except Exception as e:
        _local_failed = True
        import logging
        logging.getLogger(__name__).warning(
            f"[memory] 地端向量模型未就緒、episodic 降級關鍵字檢索:{type(e).__name__}: {e}")
    return _local_embedder


def warmup_local_embedder() -> bool:
    """應用啟動時呼叫:預載地端模型(觸發下載)。回是否就緒。"""
    em = _get_local_embedder()
    if em is None:
        return False
    try:
        list(em.embed(["warmup"]))
        return True
    except Exception:
        return False


def _embed_local(text: str) -> "Optional[_np.ndarray]":
    em = _get_local_embedder()
    if em is None:
        return None
    try:
        v = list(em.embed([text]))
        return _np.array(v[0], dtype=_np.float32) if v else None
    except Exception:
        return None


def _embed_gemini(text: str) -> "Optional[_np.ndarray]":
    from config import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return None
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               f"models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}")
        body = json.dumps({"model": "models/gemini-embedding-001",
                           "content": {"parts": [{"text": text}]}}).encode()
        rq = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        resp = json.loads(urllib.request.urlopen(rq, timeout=20).read().decode())
        vals = resp.get("embedding", {}).get("values")
        return _np.array(vals, dtype=_np.float32) if vals else None
    except Exception:
        return None


def embed(text: str) -> "Optional[_np.ndarray]":
    """文字轉向量。隱私一致性:provider=gemini(已用雲端)→ Gemini API embedding;
    其他 provider(ollama / groq / openai / anthropic)→ 地端 MiniLM、資料完全不外送。
    都不行 → None(降級關鍵字檢索)。"""
    if _np is None:
        return None
    text = (text or "")[:8000]
    try:
        from settings import get_settings
        provider = (get_settings().get("provider") or "").lower()
    except Exception:
        provider = ""
    if provider == "gemini":
        return _embed_gemini(text)   # 雲端(已接受外送)
    return _embed_local(text)        # 地端、不外送
    return _embed_gemini(text)             # 雲端


def add_episode(conv_key: str, summary: str, tags: str = "", user_id: str = "local") -> dict:
    """存/更新一條對話摘要(同 conv_key 滾動更新、不重複塞)。自動嘗試 embed。"""
    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "error": "summary 空"}
    emb = embed(summary)
    blob = emb.astype(_np.float32).tobytes() if emb is not None else None
    now = int(time.time())
    with _lock:
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO episodes(user_id, conv_key, summary, embedding, tags, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, conv_key, summary, blob, tags or "", now),
        )
        db.commit()
    return {"ok": True, "conv_key": conv_key, "has_vector": emb is not None, "updated_at": now}


def backfill_embeddings(user_id: str = "local", target_dim: "Optional[int]" = None, limit: int = 30) -> int:
    """把「無向量 或 維度與當前模型不符」的 episode 重算 embedding。
    無縫銜接:① 後來加 Gemini key ② 切換 provider 導致維度改變(Gemini 3072 ↔ 地端 384)。
    target_dim = 當前 embed 的維度;embed 失敗即停、不空轉。回補了幾筆。"""
    if _np is None or target_dim is None:
        return 0
    with _lock:
        rows = _db().execute(
            "SELECT conv_key, summary, embedding FROM episodes WHERE user_id=?", (user_id,)).fetchall()
    done = 0
    for ck, summ, emb in rows:
        if done >= limit:
            break
        cur = len(_np.frombuffer(emb, dtype=_np.float32)) if emb else 0
        if cur == target_dim:
            continue  # 已是當前模型維度
        e = embed(summ)
        if e is None or len(e) != target_dim:
            break  # 沒 embedding 能力 / 維度不穩 → 停(維持降級)
        with _lock:
            _db().execute("UPDATE episodes SET embedding=? WHERE user_id=? AND conv_key=?",
                          (e.astype(_np.float32).tobytes(), user_id, ck))
            _db().commit()
        done += 1
    return done


def recall_episode(query: str, max_results: int = 5, user_id: str = "local") -> list[dict]:
    """語意檢索過去對話摘要。有 embedding → cosine top-k;否則 → 關鍵字 LIKE。
    查詢時順手把「無向量 / 維度不符(切過 provider)」的舊 episode 重算 → 自我修復、無縫銜接。"""
    query = (query or "").strip()
    qemb = embed(query) if query else None
    qd = len(qemb) if qemb is not None else None
    if qd:
        backfill_embeddings(user_id, target_dim=qd)   # 補無向量 / 遷移維度
    with _lock:
        rows = _db().execute(
            "SELECT conv_key, summary, embedding, tags, updated_at FROM episodes WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT 500", (user_id,)).fetchall()
    if not rows:
        return []
    # 只比對「維度 == 當前 query 維度」的向量(切過 provider 的舊維度直接忽略、走關鍵字)
    vec_rows = []
    if qemb is not None and _np is not None:
        for r in rows:
            if not r[2]:
                continue
            v = _np.frombuffer(r[2], dtype=_np.float32)
            if len(v) == qd:
                vec_rows.append((r, v))
    if qemb is not None and vec_rows:
        mat = _np.stack([v for _, v in vec_rows])
        sims = mat @ qemb / (_np.linalg.norm(mat, axis=1) * _np.linalg.norm(qemb) + 1e-9)
        order = _np.argsort(sims)[::-1][:max_results]
        return [{"summary": vec_rows[i][0][1], "tags": vec_rows[i][0][3],
                 "similarity": float(sims[i]), "updated_at": vec_rows[i][0][4]} for i in order]
    # 降級:關鍵字 LIKE(query 拆詞、任一命中)
    terms = [t for t in re.split(r"\s+", query) if len(t) >= 2] or [query]
    scored = []
    for r in rows:
        hits = sum(1 for t in terms if t and t.lower() in (r[1] or "").lower())
        if hits:
            scored.append((hits, r))
    scored.sort(key=lambda x: (-x[0], -x[1][4]))
    return [{"summary": r[1], "tags": r[3], "match_terms": h, "updated_at": r[4]}
            for h, r in scored[:max_results]]


def count_episodes(user_id: str = "local") -> int:
    with _lock:
        row = _db().execute("SELECT COUNT(*) FROM episodes WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else 0
