"""Secrets Vault — 憑證加密存放,工作流用 {{ secrets.名稱 }} 引用。

解決:API key / 密碼明文寫在 YAML、env、DB 的問題。
設計:
  - Fernet(AES128-CBC + HMAC)加密後存 SQLite;金鑰檔在 OUTPUT_BASE/.vault_key
    (首次自動生成、僅本機)。= 「本機層級」加密:擋 DB 檔外流 / 匯出包誤帶明文;
    不是 HSM,拿得到金鑰檔仍可解 — 誠實定位。
  - API 永不回傳明文值(list 只給名稱與遮罩)。
  - AI 助手只被告知「有哪些名稱」,值永不進提示詞。
"""
from __future__ import annotations

import os
import time
from typing import Optional

from config import OUTPUT_BASE_PATH

_KEY_PATH = OUTPUT_BASE_PATH / ".vault_key"


def _get_fernet():
    from cryptography.fernet import Fernet
    if not _KEY_PATH.exists():
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        with open(_KEY_PATH, "wb") as f:
            f.write(key)
        try:
            os.chmod(_KEY_PATH, 0o600)  # Windows 上近似 no-op,POSIX 生效
        except Exception:
            pass
    else:
        with open(_KEY_PATH, "rb") as f:
            key = f.read().strip()
    return Fernet(key)


def _ensure_table():
    from db import get_conn
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS secrets (
            name       TEXT PRIMARY KEY,
            enc_value  BLOB NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()


def set_secret(name: str, value: str) -> None:
    name = (name or "").strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("名稱只能含英數與 _ -(將以 {{ secrets.名稱 }} 引用)")
    _ensure_table()
    from db import get_conn
    enc = _get_fernet().encrypt(value.encode("utf-8"))
    now = time.time()
    conn = get_conn()
    conn.execute(
        "INSERT INTO secrets(name, enc_value, created_at, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET enc_value=excluded.enc_value, updated_at=excluded.updated_at",
        (name, enc, now, now))
    conn.commit()


def get_secret(name: str) -> Optional[str]:
    _ensure_table()
    from db import get_conn
    r = get_conn().execute("SELECT enc_value FROM secrets WHERE name=?", (name,)).fetchone()
    if not r:
        return None
    try:
        return _get_fernet().decrypt(bytes(r[0])).decode("utf-8")
    except Exception:
        return None  # 金鑰檔換過 / 資料壞 → 當不存在(引用時會明確報缺)


def list_secret_names() -> list[dict]:
    """只回名稱與時間,永不回值。"""
    _ensure_table()
    from db import get_conn
    rows = get_conn().execute(
        "SELECT name, created_at, updated_at FROM secrets ORDER BY name").fetchall()
    return [{"name": r[0], "created_at": r[1], "updated_at": r[2]} for r in rows]


def delete_secret(name: str) -> bool:
    _ensure_table()
    from db import get_conn
    conn = get_conn()
    cur = conn.execute("DELETE FROM secrets WHERE name=?", (name,))
    conn.commit()
    return cur.rowcount > 0


class SecretsNamespace:
    """給 Jinja2 context 用的 lazy namespace:{{ secrets.X }} 引用時才解密。
    引用不存在的名稱 → 丟明確錯誤(比靜默空字串安全 — 否則 API 帶空 key 打出去)。"""

    def __getattr__(self, name: str) -> str:
        v = get_secret(name)
        if v is None:
            raise KeyError(f"Secret「{name}」不存在;請到設定頁 Secrets 區新增")
        return v

    # Jinja2 對 dict-style 存取用 __getitem__
    def __getitem__(self, name: str) -> str:
        return self.__getattr__(name)
