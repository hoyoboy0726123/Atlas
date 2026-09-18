"""
win32_helpers 內部共用工具：COM 連線管理、平台檢查、例外。

刻意把 pywin32 的 import 包在函式裡 lazy 載入 —— 讓 sandbox（Linux）
import win32_helpers.outlook 不會在 module load 階段就炸；只有真的呼叫
裡面的函式時才會碰到 pywin32（這時也已經是在 host 上跑了）。
"""
from __future__ import annotations

import platform
from typing import Any


class Win32NotAvailableError(RuntimeError):
    """非 Windows 環境呼叫了 win32_helpers 的函式。"""


class OutlookNotRunningError(RuntimeError):
    """連 Outlook COM 失敗（通常是 Outlook 沒裝、profile 沒設好、或被防毒擋）。"""


def _ensure_windows() -> None:
    """非 Windows 平台直接拒絕，不要走到 pywin32 import 才炸（錯誤訊息較模糊）。"""
    if platform.system() != "Windows":
        raise Win32NotAvailableError(
            f"win32_helpers 只能在 Windows host 跑（目前平台：{platform.system()}）。"
            f"如果你看到這個錯誤，代表程式碼跑在 sandbox 容器裡 —— "
            f"Outlook 自動化節點應該由 runner 路由到 host 執行。"
        )


def _get_outlook_app() -> Any:
    """取得 Outlook.Application COM 物件。Outlook 沒開時會透過 COM 啟動。

    回傳：win32com.client.Dispatch("Outlook.Application")
    例外：
      - Win32NotAvailableError：非 Windows
      - OutlookNotRunningError：Outlook 連不上（沒裝 / profile 沒設）
    """
    _ensure_windows()
    try:
        import win32com.client  # type: ignore[import-not-found]
        import pythoncom  # type: ignore[import-not-found]
        # 多執行緒環境下要 CoInitialize；對單執行緒呼叫無害
        pythoncom.CoInitialize()
        return win32com.client.Dispatch("Outlook.Application")
    except ImportError as e:
        raise Win32NotAvailableError(
            f"pywin32 未安裝或損壞：{e}。請執行 `pip install pywin32` 並 "
            f"`python -m pywin32_postinstall -install`（admin shell）。"
        ) from e
    except Exception as e:
        # com_error / pywintypes.com_error 各種變體都吃進來
        raise OutlookNotRunningError(
            f"無法連線到 Outlook：{e}。檢查項目：\n"
            f"  1. Outlook 桌面版有裝（不是 Web 版 / Outlook 365 webmail）\n"
            f"  2. Outlook 已建立預設 profile（第一次開要走完設定）\n"
            f"  3. 防毒 / EDR 沒有擋 COM 自動化\n"
            f"  4. 跑這支程式的使用者帳號 = 設定 Outlook profile 的帳號"
        ) from e


def _get_namespace() -> Any:
    """取得 Outlook MAPI namespace（讀寫信件 / 行事曆 / 聯絡人都從這個物件開始）。"""
    app = _get_outlook_app()
    return app.GetNamespace("MAPI")


# ── Outlook 預設資料夾常數 ─────────────────────────────────────────────
# 這些是 Microsoft 官方的 OlDefaultFolders enum，用 magic number 直接寫死
# 比較直白，也避免 import 整個 constants 模組。
# https://learn.microsoft.com/en-us/office/vba/api/outlook.oldefaultfolders
OL_FOLDER_INBOX = 6           # 收件匣
OL_FOLDER_SENT_MAIL = 5       # 寄件備份
OL_FOLDER_DRAFTS = 16         # 草稿
OL_FOLDER_DELETED = 3         # 刪除的郵件
OL_FOLDER_OUTBOX = 4          # 寄件匣（尚未送出）
OL_FOLDER_JUNK = 23           # 垃圾郵件
OL_FOLDER_CALENDAR = 9        # 行事曆
OL_FOLDER_CONTACTS = 10       # 連絡人
OL_FOLDER_TASKS = 13          # 工作


_DEFAULT_FOLDER_ALIASES = {
    "inbox": OL_FOLDER_INBOX, "收件匣": OL_FOLDER_INBOX,
    "sent": OL_FOLDER_SENT_MAIL, "寄件備份": OL_FOLDER_SENT_MAIL,
    "drafts": OL_FOLDER_DRAFTS, "草稿": OL_FOLDER_DRAFTS,
    "deleted": OL_FOLDER_DELETED, "trash": OL_FOLDER_DELETED, "刪除的郵件": OL_FOLDER_DELETED,
    "outbox": OL_FOLDER_OUTBOX, "寄件匣": OL_FOLDER_OUTBOX,
    "junk": OL_FOLDER_JUNK, "垃圾郵件": OL_FOLDER_JUNK,
    "calendar": OL_FOLDER_CALENDAR, "行事曆": OL_FOLDER_CALENDAR,
    "contacts": OL_FOLDER_CONTACTS, "連絡人": OL_FOLDER_CONTACTS,
    "tasks": OL_FOLDER_TASKS, "工作": OL_FOLDER_TASKS,
}


def _resolve_folder(ns: Any, folder: str) -> Any:
    """把使用者填的 folder 字串解析成 Outlook 資料夾物件。

    支援：
      - 別名："inbox" / "收件匣" / "sent" / ...
      - 路徑："收件匣/工作" 或 "Inbox/Projects/2026"（用 / 分隔）
      - 預設資料夾 magic number："6" 或 6（int）

    回傳：MAPIFolder COM 物件
    """
    f = str(folder).strip()

    # 1. 別名
    key = f.lower()
    if key in _DEFAULT_FOLDER_ALIASES:
        return ns.GetDefaultFolder(_DEFAULT_FOLDER_ALIASES[key])

    # 2. 純數字 = OlDefaultFolders enum
    if f.isdigit():
        return ns.GetDefaultFolder(int(f))

    # 3. 路徑分段：第一段當別名 / 預設資料夾，後面段一層層 .Folders[name]
    parts = [p for p in f.replace("\\", "/").split("/") if p]
    if not parts:
        return ns.GetDefaultFolder(OL_FOLDER_INBOX)
    head, *tail = parts
    head_key = head.lower()
    cur = (ns.GetDefaultFolder(_DEFAULT_FOLDER_ALIASES[head_key])
           if head_key in _DEFAULT_FOLDER_ALIASES
           else ns.Folders[head])
    for sub in tail:
        cur = cur.Folders[sub]
    return cur
