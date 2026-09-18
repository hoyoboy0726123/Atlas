"""
Outlook COM wrapper —— Outlook 自動化節點的核心工具集。

API 哲學：
  - 函式收 keyword arguments、回傳 pandas.DataFrame 或 dict（不要回 COM 物件給呼叫者）
  - 信件 ID 用 EntryID（Outlook 全域唯一），不用 index（會變）
  - 日期參數收 datetime / pandas.Timestamp / ISO 字串，內部統一轉成 datetime
  - 過濾條件全部支援「精確 vs 模糊」雙模式（exact_match=True/False）

Phase 1 範圍（這個檔案）：
  - search_mail / get_mail_by_id           讀
  - download_attachments / save_mail_body  讀的副作用
  - send_mail / reply_mail / forward_mail  寫
  - calendar_list / create_meeting          行事曆讀寫

Phase 2（之後再加，現在 stub）：
  - move_mail / mark_read / set_flag        信件分類 / 標記
  - update_meeting / cancel_meeting         會議修改

實作策略：
  - 大部分函式只是 ~30-50 行，可以直接寫死
  - 共用的 datetime 轉換 / 過濾條件 / 安全檔名統一抽到下方 helper 區
  - COM 物件 lifecycle 短暫使用 —— 函式進來建立、出去就放掉、不留全域狀態
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Union

# pandas 在 sandbox / host 都有，可以正常 import；pywin32 才需要 lazy
import pandas as pd

from ._common import (
    OL_FOLDER_INBOX,
    OutlookNotRunningError,
    Win32NotAvailableError,
    _ensure_windows,
    _get_namespace,
    _get_outlook_app,
    _resolve_folder,
)

_module_logger = logging.getLogger("pipeline.win32_helpers.outlook")


# ── 型別別名 ──────────────────────────────────────────────────────────
DateLike = Union[datetime, pd.Timestamp, str, None]
StrOrList = Union[str, list[str], None]


# ── Helper：日期 / 過濾條件正規化 ────────────────────────────────────
def _to_datetime(d: DateLike) -> Optional[datetime]:
    """各種日期輸入 → **naive 本地牆鐘** datetime；None 回 None。

    ⚠️ 為什麼是 naive 而不是 timezone-aware（這裡踩過坑）：
    Outlook COM 的 ReceivedTime 是 pywintypes.datetime，它**帶 tzinfo 但那個值不可信**
    —— 實測在 UTC+8 機器上回傳 `2026-07-28 11:50 tzinfo=GMT Standard Time(UTC+00:00)`，
    但 11:50 其實是**本地**時間（本機時鐘就是 11:51）。也就是說：牆鐘是本地的、
    標籤卻寫 GMT。

    舊版這裡回 aware(+08:00) 去跟那個假 GMT 比較，Python 會把「11:50 本地」當成
    「11:50 UTC」，切點等於整整早了 8 小時 —— 實測「撈今天」會多撈到昨天 16:00
    之後的 99 封信，每日報表的資料範圍是錯的。

    所以統一成「naive 本地牆鐘」比較：使用者講的 today 是本地午夜，COM 給的也是
    本地牆鐘，兩邊脫掉 tz 直接比才是對的。這也跟下方寫進 dataframe 的處理一致
    （那裡的註解本來就寫著「避免 pywintypes 的不正規 tzinfo」）。"""
    if d is None:
        return None
    if isinstance(d, datetime):
        out = d
    elif isinstance(d, pd.Timestamp):
        out = d.to_pydatetime()
    elif isinstance(d, str):
        out = pd.to_datetime(d).to_pydatetime()
    else:
        raise TypeError(f"不認識的日期型別：{type(d).__name__}")
    # 使用者若主動帶了 tz（例 ISO 字串含 offset）→ 先換算成本地，再脫成牆鐘
    if out.tzinfo is not None:
        out = out.astimezone().replace(tzinfo=None)
    return out


def _naive_local(dt):
    """COM datetime → naive 本地牆鐘，供日期比較用。

    pywintypes 的 tzinfo 標示不可信（見 _to_datetime），但牆鐘值是本地的，
    所以直接脫掉 tzinfo 即可，**不可**做 astimezone 換算（那會再位移一次）。"""
    if dt is None:
        return None
    try:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) is not None else dt
    except Exception:
        return dt


def _match(text: str, pattern: Optional[str], exact: bool) -> bool:
    """字串比對：pattern=None 就回 True（沒過濾）；exact=True 走完全相等、
    False 走「子字串 + case-insensitive」模糊比對。pattern 是 list 時 OR 邏輯。"""
    if pattern is None:
        return True
    if isinstance(pattern, list):
        return any(_match(text, p, exact) for p in pattern)
    if exact:
        return text == pattern
    return pattern.lower() in (text or "").lower()


def _safe_filename(name: str, max_len: int = 80) -> str:
    """把寄件人 / 主旨等字串清成可用的檔名 fragment（去掉 Windows 禁字）。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_len] if cleaned else "unnamed"


# ── 主要 API ──────────────────────────────────────────────────────────


def search_mail(
    *,
    subject: StrOrList = None,
    sender: StrOrList = None,
    body_keyword: StrOrList = None,
    since: DateLike = None,
    until: DateLike = None,
    folder: str = "inbox",
    unread_only: bool = False,
    has_attachment: Optional[bool] = None,
    exact_match: bool = False,
    limit: int = 500,
    logger: Optional[logging.Logger] = None,
    progress_every: int = 500,
) -> pd.DataFrame:
    """搜尋信件。回傳 DataFrame，欄位：

        entry_id            EntryID（Outlook 全域唯一，後續操作用這個）
        received            收件時間（Timestamp，本地時區）
        sender_name         寄件人顯示名
        sender_email        寄件人 SMTP（解 Exchange X.500 → SMTP，盡量提供）
        subject             主旨
        body_preview        本文純文字（前 500 字）
        body_text           本文純文字（完整，可能很長）
        has_attachments     有無附件
        attachment_names    附件檔名清單（list[str]）
        is_unread           未讀狀態
        importance          重要性（0=低、1=普通、2=高）
        folder_name         所在資料夾名

    Args:
        subject:        主旨關鍵字（單字串或 list[str]，list = OR 邏輯）
        sender:         寄件人（顯示名或 email，單字串或 list）
        body_keyword:   本文關鍵字
        since/until:    收件時間範圍（含起、含迄）
        folder:         資料夾（"inbox" / "收件匣" / "Inbox/Projects" / 6 ...）
        unread_only:    只取未讀
        has_attachment: True=只取有附件、False=只取無附件、None=不過濾
        exact_match:    True=完全相等比對；False（預設）=模糊（子字串 + 不分大小寫）
        limit:          最多回傳幾筆（避免大資料夾爆 RAM）

    為什麼不用 Outlook 內建的 Restrict / DASL 搜尋語法：
        Restrict 對 Exchange 帳號有時很慢、語法又怪（DASL URI），
        我們直接 Python 端 filter 反而簡單可控。對 < 5000 筆的資料夾速度足夠。
    """
    log = logger or _module_logger
    _ensure_windows()
    ns = _get_namespace()
    fld = _resolve_folder(ns, folder)
    folder_name = fld.Name

    items = fld.Items
    items.Sort("[ReceivedTime]", True)  # True = descending

    since_dt = _to_datetime(since)
    until_dt = _to_datetime(until)

    # 大資料夾（10000+ 封）會跑很久，預先告訴使用者開始掃了
    try:
        total_in_folder = int(items.Count)
    except Exception:
        total_in_folder = -1
    log.info(f"search_mail: 開始掃 {folder_name}（資料夾共 {total_in_folder if total_in_folder >= 0 else '?'} 封）"
             f"、條件 since={since_dt}, until={until_dt}, limit={limit}")
    import time as _time
    _t_start = _time.time()

    rows = []
    count_scanned = 0
    for item in items:
        count_scanned += 1
        if count_scanned > 10000:  # 硬上限，防止資料夾爆量
            log.warning(f"search_mail: 掃過 10000 封信仍未滿足 limit={limit}，提早結束")
            break

        # 進度回報：每 progress_every 封顯示一次（讓使用者知道沒當機）
        if progress_every > 0 and count_scanned % progress_every == 0:
            elapsed = _time.time() - _t_start
            log.info(f"search_mail: 進度 {count_scanned} 封已掃，目前命中 {len(rows)} 封"
                     f"（耗時 {elapsed:.1f}s）")

        # MailItem.Class = 43；行事曆 / 約會 / 工作要過濾掉
        try:
            if item.Class != 43:
                continue
        except Exception:
            continue

        try:
            received = item.ReceivedTime  # COM datetime
        except Exception:
            continue

        # 比較一律用 naive 本地牆鐘 —— COM 的 tzinfo 標示不可信（見 _to_datetime）
        received_cmp = _naive_local(received)
        # 因為 items 已 sort by received desc，遇到比 since 更早就可以中止
        if since_dt and received_cmp < since_dt:
            break
        if until_dt and received_cmp > until_dt:
            continue

        if unread_only and not bool(item.UnRead):
            continue

        attachment_count = item.Attachments.Count
        if has_attachment is True and attachment_count == 0:
            continue
        if has_attachment is False and attachment_count > 0:
            continue

        item_subject = (item.Subject or "")
        sender_name = (item.SenderName or "")
        sender_email = _resolve_sender_email(item)
        body_text = (item.Body or "")  # COM 已給純文字版

        # 過濾條件
        if not _match(item_subject, subject, exact_match):
            continue
        if not _match(sender_name + "|" + sender_email, sender, exact_match):
            # 寄件人模糊比對：name 跟 email 任一含 pattern 就算中
            continue
        if not _match(body_text, body_keyword, exact_match):
            continue

        attachment_names: list[str] = []
        if attachment_count > 0:
            for i in range(1, attachment_count + 1):
                try:
                    attachment_names.append(item.Attachments.Item(i).FileName)
                except Exception:
                    pass

        # received：強制轉 UTC 後脫 tz（naive datetime），避免 pywintypes 的不正規 tzinfo
        # 在 pandas 內部 _localize_tso 觸發 'NoneType has no total_seconds' crash。
        # 這樣 DataFrame 在後續 iterrows / to_markdown / astype 都不會炸。
        try:
            _received_pd = pd.Timestamp(received)
            if _received_pd.tz is not None:
                _received_pd = _received_pd.tz_convert("UTC").tz_localize(None)
        except Exception:
            # 任何轉 fail 就退到原始 datetime（無 tz）
            _received_pd = pd.Timestamp(received.replace(tzinfo=None) if hasattr(received, 'replace') else received)

        # ConversationID：同一個 thread 的信會共用，給 unanswered prefetch 比對用
        try:
            conv_id = str(item.ConversationID or "")
        except Exception:
            conv_id = ""

        rows.append({
            "entry_id": item.EntryID,
            "conversation_id": conv_id,
            "received": _received_pd,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": item_subject,
            "body_preview": body_text[:500],
            "body_text": body_text,
            "has_attachments": attachment_count > 0,
            "attachment_names": attachment_names,
            "is_unread": bool(item.UnRead),
            "importance": int(item.Importance),
            "folder_name": folder_name,
        })

        if len(rows) >= limit:
            break

    df = pd.DataFrame(rows)
    log.info(f"search_mail: 完成 — 從 {folder_name} 掃 {count_scanned} 封、命中 {len(df)} 封"
             f"（總耗時 {_time.time() - _t_start:.1f}s）")
    return df


def _resolve_sender_email(item: Any) -> str:
    """Outlook 內網帳號的 SenderEmailAddress 是 X.500（很長一坨），
    要透過 Sender.AddressEntry → Exchange User → PrimarySmtpAddress 解成 SMTP。
    對外部寄件人直接回 SenderEmailAddress 即可。"""
    try:
        addr = item.SenderEmailAddress or ""
        if "@" in addr:
            return addr  # 已是 SMTP
        # X.500 / EX 格式 → 解 Exchange User
        sender = item.Sender
        if sender is None:
            return addr
        try:
            ex_user = sender.GetExchangeUser()
            if ex_user is not None:
                return ex_user.PrimarySmtpAddress or addr
        except Exception:
            pass
        return addr
    except Exception:
        return ""


def get_mail_by_id(entry_id: str) -> dict:
    """讀單封信完整資料（從 search_mail 結果拿 entry_id 後二次調用）。

    回傳跟 search_mail 一列資料相同的欄位 + body_html（如果有）。
    """
    _ensure_windows()
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    return {
        "entry_id": item.EntryID,
        "received": pd.Timestamp(item.ReceivedTime),
        "sender_name": item.SenderName or "",
        "sender_email": _resolve_sender_email(item),
        "subject": item.Subject or "",
        "body_text": item.Body or "",
        "body_html": item.HTMLBody or "",
        "has_attachments": item.Attachments.Count > 0,
        "attachment_names": [
            item.Attachments.Item(i).FileName
            for i in range(1, item.Attachments.Count + 1)
        ],
        "is_unread": bool(item.UnRead),
        "importance": int(item.Importance),
        "to": item.To or "",
        "cc": item.CC or "",
    }


def download_attachments(
    *,
    entry_ids: list[str],
    out_dir: Union[str, Path],
    name_template: str = "{date}_{sender}_{filename}",
    overwrite: bool = False,
    extensions: Optional[list[str]] = None,
) -> list[Path]:
    """把指定信件的附件全部下載到 out_dir。

    Args:
        entry_ids:      要處理的信件 EntryID 清單
        out_dir:        目標資料夾（會自動建立）
        name_template:  檔名範本，可用變數：
                          {date}     收件日期 YYYYMMDD
                          {sender}   寄件人顯示名（清過 Windows 禁字）
                          {subject}  主旨（清過）
                          {filename} 附件原檔名
        overwrite:      True=同名覆蓋；False=自動加 _1 / _2 後綴
        extensions:     副檔名白名單（不分大小寫，可帶或不帶 '.'）。
                          None 或空 list = 不過濾、抓全部
                          ['pdf', '.xlsx'] = 只抓 .pdf 跟 .xlsx

    回傳：實際存下的 Path 清單。
    """
    _ensure_windows()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 副檔名白名單正規化成 set('.pdf', '.xlsx', ...)
    ext_filter: Optional[set[str]] = None
    if extensions:
        ext_filter = {('.' + e.strip().lstrip('.').lower()) for e in extensions if e and str(e).strip()}
        if not ext_filter:
            ext_filter = None
    ns = _get_namespace()
    saved: list[Path] = []
    for eid in entry_ids:
        try:
            item = ns.GetItemFromID(eid)
        except Exception as e:
            _module_logger.warning(f"download_attachments: EntryID {eid[:20]}... 找不到信件：{e}")
            continue
        if item.Attachments.Count == 0:
            continue
        sender = _safe_filename(item.SenderName or "unknown")
        subject = _safe_filename(item.Subject or "no_subject")
        date_str = pd.Timestamp(item.ReceivedTime).strftime("%Y%m%d")
        for i in range(1, item.Attachments.Count + 1):
            att = item.Attachments.Item(i)
            if ext_filter is not None:
                this_ext = Path(att.FileName).suffix.lower()
                if this_ext not in ext_filter:
                    continue
            target_name = name_template.format(
                date=date_str, sender=sender, subject=subject, filename=att.FileName,
            )
            target = out / _safe_filename(target_name, max_len=200)
            if target.exists() and not overwrite:
                stem, suffix = target.stem, target.suffix
                k = 1
                while (out / f"{stem}_{k}{suffix}").exists():
                    k += 1
                target = out / f"{stem}_{k}{suffix}"
            att.SaveAsFile(str(target.resolve()))
            saved.append(target)
    if ext_filter:
        _module_logger.info(f"download_attachments: {len(saved)} 個附件存到 {out}（過濾 {sorted(ext_filter)}）")
    else:
        _module_logger.info(f"download_attachments: {len(saved)} 個附件存到 {out}")
    return saved


def send_mail(
    *,
    to: Union[str, list[str]],
    subject: str,
    body: str,
    body_format: str = "html",
    cc: Optional[Union[str, list[str]]] = None,
    bcc: Optional[Union[str, list[str]]] = None,
    attachments: Optional[list[Union[str, Path]]] = None,
    importance: int = 1,
    save_to_drafts: bool = False,
) -> str:
    """寄信。

    Args:
        to/cc/bcc:    收件人 email（單一或 list）；自動用「; 」串接
        subject:      主旨
        body:         本文
        body_format:  "html" / "text"。html 模式 body 是 HTML 字串
        attachments:  附件檔案路徑清單
        importance:   0=低、1=普通、2=高
        save_to_drafts: True=只存草稿不送出（之後人工檢查再送）

    回傳：寄出後的 EntryID（之後可從寄件備份找到）；save_to_drafts=True 時是草稿 EntryID。
    """
    _ensure_windows()
    app = _get_outlook_app()
    mail = app.CreateItem(0)  # 0 = olMailItem

    mail.To = _join_recipients(to)
    if cc:
        mail.CC = _join_recipients(cc)
    if bcc:
        mail.BCC = _join_recipients(bcc)
    mail.Subject = subject
    mail.Importance = max(0, min(2, int(importance)))
    if body_format.lower() == "html":
        mail.HTMLBody = body
    else:
        mail.Body = body
    for att in attachments or []:
        p = Path(att).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"附件不存在：{p}")
        mail.Attachments.Add(str(p))

    # 重點：mail.Send() 會把 COM 物件從 Outbox 移到 Sent Items，原本的 reference 立即失效。
    # 任何 .Send() 之後存取 mail.To / mail.EntryID 等屬性都會噴
    # com_error: '項目已經移動或刪除'。所以要在 Send 前把所有要用的屬性存到 local var。
    to_str = str(mail.To)[:60] if mail.To else ""
    subj_preview = subject[:40] if subject else ""

    if save_to_drafts:
        mail.Save()
        try:
            eid = mail.EntryID
        except Exception:
            eid = ""
        _module_logger.info(f"send_mail: 草稿已存（subject={subj_preview}）")
        return eid

    # 在 Send 前先抓 EntryID（送出後立刻會失效）
    try:
        eid_before = mail.EntryID
    except Exception:
        eid_before = ""
    mail.Send()
    _module_logger.info(f"send_mail: 已送出（to={to_str}, subject={subj_preview}）")
    return eid_before


def _join_recipients(r: Union[str, list[str]]) -> str:
    """list[str] → "a@x.com; b@x.com"；str 直接回。"""
    if isinstance(r, str):
        return r
    return "; ".join(r)


def reply_mail(
    *,
    entry_id: str,
    body: str,
    body_format: str = "html",
    reply_all: bool = False,
    additional_attachments: Optional[list[Union[str, Path]]] = None,
) -> str:
    """回覆某封信（保留引用內文，附加我們寫的回覆 body）。

    Args:
        entry_id:                要回覆的信件 EntryID
        body:                    新增的回覆文字（HTML 或純文字）
        reply_all:               True 走 ReplyAll
        additional_attachments:  額外附件

    回傳：寄出後的 EntryID。

    實作備註：Outlook ReplyHTMLBody 會自帶引用區塊（"From: ... Sent: ..."），
    我們的 body 用 HTML 模式時直接 prepend 到原 HTMLBody 前；text 模式類似。
    """
    _ensure_windows()
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    reply = item.ReplyAll() if reply_all else item.Reply()
    if body_format.lower() == "html":
        reply.HTMLBody = body + (reply.HTMLBody or "")
    else:
        reply.Body = body + "\n\n" + (reply.Body or "")
    for att in additional_attachments or []:
        p = Path(att).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"附件不存在：{p}")
        reply.Attachments.Add(str(p))
    # Send 前先抓屬性 — Send 後物件立即失效（'項目已經移動或刪除'）
    src_subj = (item.Subject or "")[:40]
    try:
        eid_before = reply.EntryID
    except Exception:
        eid_before = ""
    reply.Send()
    _module_logger.info(f"reply_mail: 已回覆（reply_all={reply_all}, source_subject={src_subj}）")
    return eid_before


def forward_mail(
    *,
    entry_id: str,
    to: Union[str, list[str]],
    body: str = "",
    body_format: str = "html",
    additional_attachments: Optional[list[Union[str, Path]]] = None,
) -> str:
    """轉寄某封信（保留原信內容跟附件）。"""
    _ensure_windows()
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    fwd = item.Forward()
    fwd.To = _join_recipients(to)
    if body:
        if body_format.lower() == "html":
            fwd.HTMLBody = body + (fwd.HTMLBody or "")
        else:
            fwd.Body = body + "\n\n" + (fwd.Body or "")
    for att in additional_attachments or []:
        p = Path(att).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"附件不存在：{p}")
        fwd.Attachments.Add(str(p))
    # Send 前抓屬性
    to_str = str(fwd.To)[:60] if fwd.To else ""
    src_subj = (item.Subject or "")[:40]
    try:
        eid_before = fwd.EntryID
    except Exception:
        eid_before = ""
    fwd.Send()
    _module_logger.info(f"forward_mail: 已轉寄（to={to_str}, source_subject={src_subj}）")
    return eid_before


# ── 行事曆 ────────────────────────────────────────────────────────────


def calendar_list(
    *,
    since: DateLike = None,
    until: DateLike = None,
    folder: str = "calendar",
    include_recurring: bool = True,
    limit: int = 200,
    logger: Optional[logging.Logger] = None,
    progress_every: int = 200,
) -> pd.DataFrame:
    """列出指定時間範圍的會議。回傳 DataFrame，欄位：

        entry_id, subject, start, end, location, organizer,
        required_attendees, optional_attendees, body, is_recurring

    `include_recurring=True` 時會展開所有 recurrence pattern 的實例
    （Outlook 預設不展開，要 IncludeRecurrences=True + 排序 by Start）。
    """
    log = logger or _module_logger
    _ensure_windows()
    ns = _get_namespace()
    fld = _resolve_folder(ns, folder)
    items = fld.Items
    if include_recurring:
        items.Sort("[Start]")
        items.IncludeRecurrences = True

    since_dt = _to_datetime(since) or datetime.now() - timedelta(days=7)
    until_dt = _to_datetime(until) or datetime.now() + timedelta(days=30)

    try:
        cal_total = int(items.Count)
    except Exception:
        cal_total = -1
    log.info(f"calendar_list: 開始掃 {fld.Name}（共 {cal_total if cal_total >= 0 else '?'} 項）"
             f"、時間範圍 {since_dt} ~ {until_dt}")
    import time as _time
    _t_start = _time.time()

    rows = []
    scanned = 0
    for item in items:
        scanned += 1
        if progress_every > 0 and scanned % progress_every == 0:
            log.info(f"calendar_list: 進度 {scanned} 項已掃，命中 {len(rows)}"
                     f"（耗時 {_time.time() - _t_start:.1f}s）")
        try:
            if item.Class != 26:  # AppointmentItem.Class = 26
                continue
            start = item.Start
            # 與 search_mail 同理:COM tzinfo 不可信，比較一律用 naive 本地牆鐘。
            # （_to_datetime 現在回 naive，這裡不脫 tz 會 TypeError）
            start_cmp = _naive_local(start)
            if start_cmp < since_dt:
                # IncludeRecurrences 下 items 已 sorted by Start，超過 since_dt 才開始計算
                continue
            if start_cmp > until_dt:
                break
            rows.append({
                "entry_id": item.EntryID,
                "subject": item.Subject or "",
                "start": pd.Timestamp(start),
                "end": pd.Timestamp(item.End),
                "location": item.Location or "",
                "organizer": item.Organizer or "",
                "required_attendees": item.RequiredAttendees or "",
                "optional_attendees": item.OptionalAttendees or "",
                "body": (item.Body or "")[:1000],  # 行事曆 body 通常不重要、截短
                "is_recurring": bool(item.IsRecurring),
            })
            if len(rows) >= limit:
                break
        except Exception as e:
            log.debug(f"calendar_list: 跳過一個無法解析的項目：{e}")
            continue

    df = pd.DataFrame(rows)
    log.info(f"calendar_list: 完成 — {since_dt}~{until_dt} 共 {len(df)} 個會議"
             f"（總耗時 {_time.time() - _t_start:.1f}s）")
    return df


def create_meeting(
    *,
    subject: str,
    start: DateLike,
    end: DateLike,
    location: str = "",
    body: str = "",
    required_attendees: Optional[Union[str, list[str]]] = None,
    optional_attendees: Optional[Union[str, list[str]]] = None,
    reminder_minutes: int = 15,
    send_invitation: bool = True,
) -> str:
    """新增會議邀請。

    Args:
        send_invitation:  True=自動發送邀請給 attendees；False=只存自己行事曆

    回傳：建立的 AppointmentItem EntryID。
    """
    _ensure_windows()
    app = _get_outlook_app()
    appt = app.CreateItem(1)  # 1 = olAppointmentItem
    appt.Subject = subject
    appt.Start = _to_datetime(start)
    appt.End = _to_datetime(end)
    appt.Location = location
    appt.Body = body
    appt.ReminderSet = True
    appt.ReminderMinutesBeforeStart = max(0, int(reminder_minutes))

    has_attendees = bool(required_attendees) or bool(optional_attendees)
    if has_attendees:
        appt.MeetingStatus = 1  # olMeeting
        if required_attendees:
            appt.RequiredAttendees = _join_recipients(required_attendees)
        if optional_attendees:
            appt.OptionalAttendees = _join_recipients(optional_attendees)
        if send_invitation:
            appt.Send()
        else:
            appt.Save()
    else:
        appt.Save()

    _module_logger.info(
        f"create_meeting: {subject[:40]} @ {appt.Start} (邀請={send_invitation and has_attendees})"
    )
    try:
        return appt.EntryID
    except Exception:
        return ""


# ── Phase 2：信件分類 / 標記 ─────────────────────────────────────────


# Outlook FlagStatus enum
_OL_NO_FLAG = 0
_OL_FLAG_COMPLETE = 1
_OL_FLAG_MARKED = 2

_FLAG_ALIASES = {
    # follow_up = 標記追蹤（紅旗）
    "follow_up": _OL_FLAG_MARKED, "marked": _OL_FLAG_MARKED, "flag": _OL_FLAG_MARKED,
    "追蹤": _OL_FLAG_MARKED, "旗標": _OL_FLAG_MARKED,
    # complete = 已完成（打勾）
    "complete": _OL_FLAG_COMPLETE, "done": _OL_FLAG_COMPLETE,
    "完成": _OL_FLAG_COMPLETE,
    # clear = 清除旗標
    "clear": _OL_NO_FLAG, "none": _OL_NO_FLAG, "off": _OL_NO_FLAG,
    "清除": _OL_NO_FLAG, "取消": _OL_NO_FLAG,
}


def move_mail(*, entry_id: str, target_folder: str) -> None:
    """把指定信件移到目標資料夾。

    Args:
        entry_id:       信件 EntryID
        target_folder:  資料夾別名（"inbox"/"收件匣"）、路徑（"Inbox/Projects"）或
                        預設資料夾 magic number。
    """
    _ensure_windows()
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    folder = _resolve_folder(ns, target_folder)
    item.Move(folder)
    _module_logger.info(f"move_mail: {entry_id[:20]}... → {target_folder}")


def mark_read(*, entry_id: str, unread: bool = False) -> None:
    """標已讀 / 未讀。

    Args:
        entry_id:  信件 EntryID
        unread:    True = 標未讀；False（預設） = 標已讀
    """
    _ensure_windows()
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    item.UnRead = bool(unread)
    item.Save()
    state = "未讀" if unread else "已讀"
    _module_logger.info(f"mark_read: {entry_id[:20]}... → {state}")


def set_flag(*, entry_id: str, flag: Optional[str] = "follow_up") -> None:
    """加 / 改 / 取消信件旗標。

    Args:
        entry_id:  信件 EntryID
        flag:      'follow_up' / 'marked' / '追蹤' / '旗標' → 標記追蹤
                   'complete' / 'done' / '完成'          → 已完成
                   'clear' / 'none' / '取消' / None / '' → 清除旗標
    """
    _ensure_windows()
    key = (flag or "clear").strip().lower()
    if key not in _FLAG_ALIASES:
        valid = sorted({k for k in _FLAG_ALIASES if not any('一' <= c <= '鿿' for c in k)})
        raise ValueError(f"未知的 flag 值：{flag!r}。支援：{', '.join(valid)}（或對應中文）")
    status = _FLAG_ALIASES[key]
    ns = _get_namespace()
    item = ns.GetItemFromID(entry_id)
    item.FlagStatus = status
    item.Save()
    desc = {0: "清除旗標", 1: "已完成", 2: "追蹤"}.get(status, str(status))
    _module_logger.info(f"set_flag: {entry_id[:20]}... → {desc}")


__all__ = [
    # 例外
    "Win32NotAvailableError", "OutlookNotRunningError",
    # 信件讀
    "search_mail", "get_mail_by_id", "download_attachments",
    # 信件寫
    "send_mail", "reply_mail", "forward_mail",
    # 信件分類 / 標記
    "move_mail", "mark_read", "set_flag",
    # 行事曆
    "calendar_list", "create_meeting",
]
