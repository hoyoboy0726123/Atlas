"""
Telegram Bot callback handler — 處理 inline keyboard 按鈕回調。

在後端啟動時以背景 task 運行，持續 polling Telegram 更新。
當收到 pipe_retry / pipe_hint / pipe_log / pipe_abort / pipe_continue 回調時，
呼叫 resume_pipeline() 繼續或中止 pipeline。

pipe_hint 流程：
1. 用戶點擊「💬 補充指示」按鈕
2. Bot 回覆「請輸入補充指示：」
3. 用戶發送文字訊息
4. Bot 呼叫 resume_pipeline(run_id, "retry_with_hint", hint=text)

── 多實例協調 ─────────────────────────────────────────────────────────────
Telegram Bot API 同一 token 同時間只允許一個 getUpdates long-poll session；
多個 backend 同時 poll 會收到 409 Conflict、callback 被亂搶、按鈕按了沒人回。
為避免這種情況，啟動前先用 PID lock 檢查：
  - Lock 路徑：%LOCALAPPDATA%/atlas/telegram.lock（Windows）
              ~/.cache/atlas/telegram.lock（Unix）
  - 內容：JSON {pid, project, started_at}
  - 若 lock 被另一個還活著的 process 持有 → 本實例跳過 polling，log 清楚說明
  - 持有 process 死掉（stale lock）→ 覆蓋接管
"""
import asyncio
import html
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("telegram_handler")


def _lock_path() -> Path:
    """全機共用 lock 位置。Windows 用 %LOCALAPPDATA%，Unix 用 ~/.cache。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "atlas"
    d.mkdir(parents=True, exist_ok=True)
    return d / "telegram.lock"


def _pid_alive(pid: int) -> bool:
    """跨平台檢查 pid 是否真的還在跑（不靠 psutil）。
    Windows 坑：OpenProcess 對「已結束但 handle 還沒清完」的 process 也會成功，
    所以光靠 OpenProcess 會把 stale PID 誤判成 alive → lock 永遠釋不掉。
    改用 GetExitCodeProcess：exit_code == STILL_ACTIVE(259) 才算真活著。
    """
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED = 0x1000
            STILL_ACTIVE = 259
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _try_acquire_lock() -> bool:
    """嘗試拿下 telegram polling 的機器級 lock。
    回傳 True = 拿到、可以 poll；False = 別人還活著在 poll，本實例不 poll。
    """
    path = _lock_path()
    try:
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            holder_pid = int(meta.get("pid", 0) or 0)
            holder_proj = meta.get("project", "unknown")
            if holder_pid and holder_pid != os.getpid() and _pid_alive(holder_pid):
                logger.warning(
                    f"Telegram polling 被另一實例持有 (pid={holder_pid}, project={holder_proj})。"
                    f" 本實例跳過 polling — Telegram 按鈕/截圖/補充指示將由該實例處理。"
                    f" 若要本實例處理，請先關閉 pid {holder_pid} 或刪掉 lock：{path}"
                )
                return False
        # 寫入自己的 meta 接管 lock
        meta = {
            "pid": os.getpid(),
            "project": _detect_project_tag(),
            "started_at": time.time(),
        }
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Telegram polling lock 取得：{path} (pid={os.getpid()})")
        return True
    except Exception as e:
        # lock 檔出問題別擋啟動、照常 poll；最差就退回舊行為
        logger.warning(f"Telegram lock 操作失敗（忽略、繼續 poll）：{e}")
        return True


def _release_lock() -> None:
    """停止 polling 時釋放 lock（只有自己持有才刪）。"""
    path = _lock_path()
    try:
        if not path.exists():
            return
        meta = json.loads(path.read_text(encoding="utf-8"))
        if int(meta.get("pid", 0) or 0) == os.getpid():
            path.unlink()
    except Exception:
        pass


def _detect_project_tag() -> str:
    """從 cwd 推個專案標籤寫進 lock，方便 debug 知道是誰持有。"""
    cwd = str(Path.cwd()).lower()
    return "atlas" if "atlas" in cwd else "unknown"

# 等待用戶輸入補充指示的狀態：chat_id → run_id
_pending_hints: dict[int, str] = {}

# 等待用戶輸入 ask_user 自由回答的狀態：chat_id → run_id
_pending_answers: dict[int, str] = {}

# AI 助手 per-chat 會話歷史（in-memory、process 重啟即清）
# 格式：chat_id → list[{"role": "user"|"assistant", "content": str}]
# 用於 TG 自由文字 → AI 助手對話。每個 chat_id 一條歷史
_tg_chat_history: dict[int, list[dict]] = {}
_TG_CHAT_HISTORY_CAP = 30  # 一條歷史最多保留 30 則訊息(15 輪對話)

# Per-chat「附加上下文」快取:user 用 /log <run_id> 或 /yaml <name|id> 拉內容後存這
# 下次 _handle_tg_freeform_chat 會把這些內容 append 到 extra_system、AI 看得到完整脈絡
# 格式:chat_id → list[(label, content, ts)]、LRU 上限 _TG_CTX_MAX_ITEMS、單筆截長 _TG_CTX_ITEM_MAX
_tg_loaded_context: dict[int, list[tuple[str, str, float]]] = {}
_TG_CTX_MAX_ITEMS = 3        # 每 chat 最多 3 筆附加上下文(超過 LRU 踢)
_TG_CTX_ITEM_MAX = 12000     # 單筆超過 12KB 截掉(防 token 爆)

# Per-chat 最近 AI 對話產生的 YAML(供 /save 用)
# 格式:chat_id → {"yaml": str, "ts": float}
_tg_last_ai_yaml: dict[int, dict] = {}


def _add_loaded_context(chat_id: int, label: str, content: str) -> None:
    """加一筆附加上下文。同 label 已存在會被覆寫。"""
    if not content:
        return
    if len(content) > _TG_CTX_ITEM_MAX:
        content = content[: _TG_CTX_ITEM_MAX] + f"\n\n... (內容超過 {_TG_CTX_ITEM_MAX} 字、後面截掉)"
    items = _tg_loaded_context.setdefault(chat_id, [])
    items[:] = [it for it in items if it[0] != label]  # 去重
    items.append((label, content, time.time()))
    if len(items) > _TG_CTX_MAX_ITEMS:
        items[:] = items[-_TG_CTX_MAX_ITEMS:]  # LRU 保留最後 N 筆

# Polling loop 持有的 Bot 實例 — 升到 module scope 讓 _cmd_* 遠端遙控指令也能用
# （沒升 module scope 之前，_cmd_menu 引用會 NameError）
_bot_instance = None
_current_token = ""


def _i_still_hold_lock() -> bool:
    """檢查 lock file 裡的 pid 是不是自己。

    防護一個實際會發生的 race：
      - 舊的 backend 行程持續 polling，但中途另一個 backend 啟動、_pid_alive(舊行程)偶發
        誤回 False（Windows OpenProcess 對長 uptime 程序有時會這樣）
      - 新行程接管 lock，自己也開始 poll
      - 舊行程不知道自己已被接管，兩邊一起 poll → Telegram 回 409 Conflict

    解法：polling loop 每個 iteration 檢一次，若 lock 已不是自己 → 退出
    polling，把 token 讓給接管者。
    """
    path = _lock_path()
    try:
        if not path.exists():
            return False
        meta = json.loads(path.read_text(encoding="utf-8"))
        return int(meta.get("pid", 0) or 0) == os.getpid()
    except Exception:
        # 讀不到當作還持有，避免 lock 暫時不可讀就退出
        return True


# ── 遠端遙控指令處理（settings.telegram_remote_control=True 才生效）──────
def _is_remote_control_authorized(chat_id: int) -> bool:
    """檢查 chat_id 是否被授權使用遠端遙控指令。
    要求：(1) settings.telegram_remote_control=True (2) chat_id 等於授權 chat
    授權 chat 解析順序：settings.telegram_chat_id → .env TELEGRAM_CHAT_ID（與 runner._get_tg_chat_id 一致）
    任一不滿足都回 False（外人 DM 直接被忽略）"""
    try:
        from settings import get_settings
        s = get_settings()
        if not s.get("telegram_remote_control", False):
            return False
        auth_chat = (s.get("telegram_chat_id") or "").strip()
        if not auth_chat:
            auth_chat = (os.environ.get("TELEGRAM_CHAT_ID", "") or "").strip()
        if not auth_chat:
            return False
        return str(chat_id) == auth_chat
    except Exception:
        return False


async def _handle_remote_command(chat_id: int, text: str) -> None:
    """處理 / 開頭的指令。"""
    logger = logging.getLogger("telegram")
    cmd = text.split()[0].lower() if text else ""
    args = text[len(cmd):].strip()
    logger.info(f"[遠端遙控] 收到指令 from chat {chat_id}: {cmd}")

    try:
        if cmd in ("/menu", "/list", "/start", "/選單"):
            await _cmd_menu(chat_id)
        elif cmd in ("/status", "/狀態"):
            await _cmd_status(chat_id)
        elif cmd in ("/help", "/幫助", "/?"):
            await _cmd_help(chat_id)
        elif cmd in ("/abort", "/中止"):
            await _cmd_abort(chat_id, args)
        elif cmd in ("/screenshot", "/截圖", "/screen"):
            await _cmd_screenshot(chat_id)
        elif cmd in ("/log", "/日誌"):
            await _cmd_log(chat_id, args)
        elif cmd in ("/yaml", "/y"):
            await _cmd_yaml(chat_id, args)
        elif cmd in ("/save", "/套用"):
            await _cmd_save_yaml(chat_id, args)
        elif cmd in ("/run", "/執行", "/啟動"):
            await _cmd_run_pipeline(chat_id, args)
        elif cmd in ("/reset", "/重設", "/clear"):
            _tg_chat_history.pop(chat_id, None)
            _tg_loaded_context.pop(chat_id, None)
            _tg_last_ai_yaml.pop(chat_id, None)
            await _bot_instance.send_message(
                chat_id=chat_id, text="🧹 AI 助手對話歷史 + 附加上下文 + 緩存 YAML 已清空。"
            )
        else:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❓ 不認識的指令：{cmd}\n打 /help 看可用指令。",
            )
    except Exception as e:
        logger.error(f"[遠端遙控] 指令 {cmd} 處理失敗：{e}", exc_info=True)
        try:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 指令處理失敗：{str(e)[:200]}",
            )
        except Exception:
            pass


def _format_next_run(iso_str) -> str:
    """ISO datetime → 'MM/DD HH:MM'。失敗回空字串。"""
    if not iso_str:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ""


def _build_menu_metadata(workflows: list[dict]) -> dict[str, dict]:
    """為 /menu 蒐集每個工作流的狀態徽章資料。
    回傳 {workflow_id: {"running": PipelineRun|None, "schedule": task_dict|None}}
    """
    meta: dict[str, dict] = {wf["id"]: {"running": None, "schedule": None} for wf in workflows}

    # 執行中的 run（用 workflow_id 對應；store 已有此欄）
    try:
        from pipeline.store import get_store
        for r in get_store().list_recent(limit=30):
            wid = getattr(r, "workflow_id", None)
            if (
                r.status in ("running", "awaiting_human")
                and wid
                and wid in meta
                and meta[wid]["running"] is None
            ):
                meta[wid]["running"] = r
    except Exception:
        pass

    # 排程：scheduler 的 task name 對應 workflow name（既有的弱關聯，名字改了會脫鉤）
    try:
        from scheduler.manager import list_tasks
        name_to_wid: dict[str, str] = {}
        for wf in workflows:
            n = wf.get("name") or ""
            if n and n not in name_to_wid:
                name_to_wid[n] = wf["id"]
        for t in list_tasks():
            if t.get("output_format") != "pipeline":
                continue
            wid = name_to_wid.get(t.get("name", ""))
            if wid and wid in meta and meta[wid]["schedule"] is None:
                meta[wid]["schedule"] = t
    except Exception:
        pass

    return meta


async def _cmd_menu(chat_id: int) -> None:
    """列出工作流，附「啟動」按鈕 + 執行中／排程徽章。"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    import db
    workflows = db.list_workflows()
    if not workflows:
        await _bot_instance.send_message(
            chat_id=chat_id,
            text="📭 沒有任何工作流。請先在前端建立。",
        )
        return

    wf_meta = _build_menu_metadata(workflows)
    rows = []
    # callback_data 上限 64 bytes（wf_id 短不會爆）
    # button label 上限約 256 字元，徽章塞在文字裡夠用
    for wf in workflows[:25]:
        wf_id = wf.get("id", "")
        name = (wf.get("name") or wf_id)[:30]
        info = wf_meta.get(wf_id, {})
        badges: list[str] = []
        running = info.get("running")
        if running is not None:
            badges.append("🔄 執行中" if running.status == "running" else "⏸ 等待人工")
        sched = info.get("schedule")
        if sched is not None:
            nxt = _format_next_run(sched.get("next_run"))
            badges.append(f"📅 {nxt}" if nxt else "📅 排程中")
        label = f"▶ {name}"
        if badges:
            label += "  " + " ".join(badges)
        rows.append([InlineKeyboardButton(label[:80], callback_data=f"pipe_start_wf:{wf_id}")])

    extra_note = ""
    if len(workflows) > 25:
        extra_note = f"\n\n（還有 {len(workflows) - 25} 個未列出，請進前端 UI 啟動）"

    await _bot_instance.send_message(
        chat_id=chat_id,
        text=(
            f"📋 <b>選擇要啟動的工作流</b>（共 {len(workflows)} 個）{extra_note}\n"
            f"<i>🔄 = 執行中　📅 = 已排程（下次執行時間）</i>"
        ),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def _cmd_status(chat_id: int) -> None:
    """列出執行中的 run。"""
    from pipeline.store import get_store
    from datetime import datetime
    runs = get_store().list_recent(limit=20)

    # 只顯示「真正活著」的 run:
    #   - running:一律顯示
    #   - awaiting_human:只顯示近 2 小時內開始的(等你決策)。
    # 排除「之前失敗/自我修復卡住而被遺棄」的舊 run —— 它們的背景任務早已結束、
    # 卻仍停在 awaiting_human,會永遠霸佔 /status 變成雜訊(使用者反饋)。
    def _is_recent(r, hours: float = 2.0) -> bool:
        try:
            return (datetime.now() - datetime.fromisoformat(r.started_at)).total_seconds() < hours * 3600
        except Exception:
            return True  # 解析不出時間 → 保守保留
    active = [
        r for r in runs
        if r.status == "running" or (r.status == "awaiting_human" and _is_recent(r))
    ]
    if not active:
        await _bot_instance.send_message(
            chat_id=chat_id,
            text="🟢 目前沒有執行中的 run。\n(等待你決策的暫停步驟會直接用通知+按鈕推給你、不列在這。)",
        )
        return
    lines = ["📊 <b>執行中的 Run</b>", ""]
    for r in active:
        emoji = "🔄" if r.status == "running" else "⏸"
        try:
            steps = r.config_dict.get("steps") or []
            total = len(steps)
        except Exception:
            total = "?"
        lines.append(f"{emoji} <code>{r.run_id}</code> — {r.pipeline_name}")
        lines.append(f"  步驟 {r.current_step + 1} / {total}")
        if r.status == "awaiting_human" and r.awaiting_message:
            lines.append(f"  等待中：{r.awaiting_message[:80]}")
        lines.append("")
    await _bot_instance.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="HTML",
    )


async def _cmd_help(chat_id: int) -> None:
    text = (
        "📖 <b>Telegram 遠端遙控指令</b>\n\n"
        "🚀 <b>啟動 / 修改</b>\n"
        "<code>/menu</code> — 列出工作流（點按鈕啟動）\n"
        "<code>/run &lt;name|id&gt;</code> — 啟動某工作流\n"
        "<code>/save &lt;name|id&gt;</code> — 把對話中 AI 產的 YAML 套到指定工作流\n"
        "<code>/abort &lt;run_id&gt;</code> — 中止某個 run\n\n"
        "🔍 <b>檢視</b>\n"
        "<code>/status</code> — 查看執行中的 run\n"
        "<code>/log &lt;run_id&gt;</code> — 拉某個 run 的完整 log（支援 8 字前綴）\n"
        "<code>/yaml &lt;name|id&gt;</code> — 拉某個 workflow 的 YAML\n"
        "<code>/screenshot</code> — 抓 host 桌面即時截圖\n\n"
        "🛠 <b>對話</b>\n"
        "<code>/reset</code> — 清空 AI 助手對話歷史 + 附加上下文\n"
        "<code>/help</code> — 顯示這份說明\n\n"
        "💬 <b>直接打字（不帶 /）就會跟 AI 助手對話</b>\n"
        "AI 會自己看 log / YAML、給分析或 patch。要套用 patch:\n"
        "1. 跟 AI 對話討論修改 → AI 吐 YAML\n"
        "2. 回 <code>/save &lt;workflow 名稱&gt;</code> 套用(覆蓋前自動備份)\n"
        "3. 回 <code>/run &lt;workflow 名稱&gt;</code> 啟動\n\n"
        "歷史 in-memory、process 重啟即清。"
    )
    await _bot_instance.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def _cmd_screenshot(chat_id: int) -> None:
    """/screenshot — 隨時抓 host 桌面截圖、傳到 TG。
    1 螢幕 1 張、N 螢幕 N 張。委託 runner.take_screenshots + _tg_send_photos。"""
    logger = logging.getLogger("telegram")
    try:
        await _bot_instance.send_message(chat_id=chat_id, text="📸 正在截圖…")
        from pipeline.runner import take_screenshots, _tg_send_photos
        # pipeline_name 給個固定夾名 _remote_screenshots(避免污染工作流目錄)
        # step_name 用時間戳區分 — 多次按 /screenshot 不會互相覆蓋
        import time as _t
        ss_paths = take_screenshots("_remote_screenshots", f"tg_{_t.strftime('%H%M%S')}")
        if ss_paths:
            await _tg_send_photos(
                chat_id,
                ss_paths,
                caption_prefix="📸 桌面即時截圖",
            )
        else:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text="❌ 截圖失敗（host 主機可能無圖形介面、或 mss 套件出錯）",
            )
    except Exception as e:
        logger.error(f"[/screenshot] 失敗：{e}", exc_info=True)
        try:
            await _bot_instance.send_message(
                chat_id=chat_id, text=f"❌ 截圖失敗：{str(e)[:200]}",
            )
        except Exception:
            pass


async def _cmd_abort(chat_id: int, args: str) -> None:
    run_id = (args or "").strip()
    if not run_id:
        await _bot_instance.send_message(
            chat_id=chat_id, text="⚠ 請帶 run_id：/abort &lt;run_id&gt;",
            parse_mode="HTML",
        )
        return
    try:
        from pipeline.runner import request_abort
        request_abort(run_id)
        await _bot_instance.send_message(
            chat_id=chat_id, text=f"🛑 已要求中止 run <code>{run_id}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await _bot_instance.send_message(
            chat_id=chat_id, text=f"❌ 中止失敗：{str(e)[:200]}",
        )


async def _cmd_log(chat_id: int, args: str) -> None:
    """/log <run_id> — 拉指定 run 的完整 log、分段送 + 快取進 chat 附加上下文。
    支援 run_id 完整或前綴。"""
    rid_query = (args or "").strip()
    if not rid_query:
        await _bot_instance.send_message(
            chat_id=chat_id, text="⚠ 用法：/log &lt;run_id&gt;（前綴 8 字也行）",
            parse_mode="HTML",
        )
        return
    try:
        # log 目錄解析集中在 pipeline.logger(優先 OUTPUT_BASE_PATH/pipeline_logs、fallback
        # 舊 backend/ai_output);不可寫死路徑(之前寫死 → 搬遷後找不到新 run)。
        from pipeline.logger import resolve_log_dirs as _rld, find_run_log as _frl
        if not _rld():
            await _bot_instance.send_message(chat_id=chat_id, text="❌ log 目錄不存在")
            return
        # 找最相近的 log 檔（filename 含 run_id 前綴、跨新舊目錄取最新）
        hit = _frl(rid_query)
        matches = [hit] if hit else []
        if not matches:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 找不到 run_id 含 <code>{html.escape(rid_query)}</code> 的 log 檔",
                parse_mode="HTML",
            )
            return
        log_file = matches[0]
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
        # 快取進 chat 附加上下文(給 AI 助手下次對話用)
        _add_loaded_context(chat_id, f"log of run `{rid_query}` (檔案: {log_file.name})", log_text)
        # 不 dump 全文到 TG(避免洗版),只回收據。要看 raw 內容跟 AI 說「把 log 內容貼給我看」
        capped = min(len(log_text), _TG_CTX_ITEM_MAX)
        receipt = (
            f"📜 已載入 log of run <code>{html.escape(rid_query)}</code>\n"
            f"📁 {html.escape(log_file.name)}\n"
            f"📏 {len(log_text):,} 字元"
            + (f" (cache 截至 {capped:,} 字)" if capped < len(log_text) else "")
            + "\n\n💬 直接跟 AI 對話,他會基於這份 log 回答。\n"
            "想看 raw 內容跟 AI 說「把 log 貼給我看」即可。"
        )
        await _bot_instance.send_message(chat_id=chat_id, text=receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/log] 失敗：{e}", exc_info=True)
        await _bot_instance.send_message(chat_id=chat_id, text=f"❌ /log 失敗：{str(e)[:200]}")


async def _cmd_save_yaml(chat_id: int, args: str) -> None:
    """/save <name|id> — 把對話中最後一份 AI 產的 YAML 套到指定工作流。

    流程:
    1. 從 _tg_last_ai_yaml 拿緩存的 YAML(沒有就提示用戶先聊一聊讓 AI 產 YAML)
    2. 模糊比對找到目標 workflow(name / id 前綴)
    3. 備份原 YAML 到 _tg_loaded_context(label=「backup of <name>」、走 LRU 踢)
    4. 用 yaml_to_canvas 重建 canvas
    5. 寫入 DB(yaml + canvas)
    """
    query = (args or "").strip()
    cached = _tg_last_ai_yaml.get(chat_id)
    if not cached or not cached.get("yaml"):
        await _bot_instance.send_message(
            chat_id=chat_id,
            text="⚠ 對話中沒有 AI 產生的 YAML 可套用。先跟 AI 討論到他吐 YAML、再 /save。",
        )
        return
    if cached.get("yaml_error"):
        await _bot_instance.send_message(
            chat_id=chat_id,
            text=f"❌ 對話中緩存的 YAML 有 schema 錯誤、不能套:{cached['yaml_error'][:300]}\n"
                 "請先回去跟 AI 修好。",
        )
        return
    if not query:
        await _bot_instance.send_message(
            chat_id=chat_id, text="⚠ 用法:/save &lt;workflow 名稱或 id&gt;",
            parse_mode="HTML",
        )
        return
    try:
        from db import list_workflows, update_workflow
        from yaml_to_canvas import yaml_to_canvas
        wfs = list_workflows() or []
        # 模糊比對(同 _cmd_yaml 邏輯)
        ql = query.lower()
        matches = [w for w in wfs if (w.get("id") or "").startswith(query) or (w.get("id") or "") == query]
        if not matches:
            matches = [w for w in wfs if ql in (w.get("name") or "").lower()]
        if not matches:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 找不到符合 <code>{html.escape(query)}</code> 的 workflow",
                parse_mode="HTML",
            )
            return
        if len(matches) > 1:
            lines = [f"⚠ <b>找到多個符合的 workflow、請用更精確的名稱或 id</b>:", ""]
            for w in matches[:8]:
                lines.append(f"- <b>{html.escape(w.get('name') or '')}</b> (id=<code>{w.get('id')}</code>)")
            await _bot_instance.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
            return
        wf = matches[0]
        new_yaml = cached["yaml"]
        old_yaml = wf.get("yaml") or ""
        # 備份原 YAML 到附加上下文(萬一套錯、用戶可叫 AI 把備份貼出來)
        if old_yaml.strip():
            _add_loaded_context(
                chat_id,
                f"backup YAML of `{wf.get('name')}` (覆蓋前)",
                old_yaml,
            )
        # YAML → canvas
        canvas = yaml_to_canvas(new_yaml) or {"nodes": [], "edges": []}
        # 寫 DB
        update_workflow(wf.get("id"), {
            "yaml": new_yaml,
            "canvas": canvas,
        })
        nc = len(canvas.get("nodes") or [])
        receipt = (
            f"✅ 已套用 YAML 到 <b>{html.escape(wf.get('name') or '')}</b>\n"
            f"id: <code>{wf.get('id')}</code>\n"
            f"📏 新 YAML {len(new_yaml):,} 字、{nc} 個節點\n"
            f"🔙 原 YAML 已存到此對話的附加上下文(備份),萬一改錯叫 AI 把備份貼給你。\n\n"
            f"💡 想直接跑:回 <code>/run {html.escape(wf.get('name') or '')}</code>"
        )
        await _bot_instance.send_message(chat_id=chat_id, text=receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/save] 失敗:{e}", exc_info=True)
        await _bot_instance.send_message(chat_id=chat_id, text=f"❌ /save 失敗:{str(e)[:200]}")


def _parse_run_args(args: str) -> tuple[str, dict]:
    """把 /run 的 args 切成「workflow query」+「input_params dict」。

    支援格式:
      /run daily_report                              → ("daily_report", {})
      /run daily_report date=2026-05-10              → ("daily_report", {"date": "2026-05-10"})
      /run daily_report date=today customer=ACME     → today 自動轉今天日期
      /run daily_report customer="ACME Inc"          → 支援雙引號值

    特殊值:date 類欄位帶 "today" / "yesterday" / "tomorrow" 會自動轉 ISO 日期。
    """
    import shlex
    from datetime import datetime, timedelta
    try:
        tokens = shlex.split(args or "", posix=True)
    except ValueError:
        # shlex 解不開引號就退回單純空白切
        tokens = (args or "").split()
    if not tokens:
        return "", {}

    # 第一個 token 可能就是 workflow query;找第一個含 "=" 的 token 作為 input 起點
    split_idx = None
    for i, t in enumerate(tokens):
        if "=" in t and not t.startswith("="):
            split_idx = i
            break
    if split_idx is None:
        query = " ".join(tokens).strip()
        return query, {}

    query = " ".join(tokens[:split_idx]).strip()
    params: dict = {}
    for tok in tokens[split_idx:]:
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # 特殊日期值
        if v.lower() in ("today", "now"):
            v = datetime.now().strftime("%Y-%m-%d")
        elif v.lower() == "yesterday":
            v = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        elif v.lower() == "tomorrow":
            v = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        params[k] = v
    return query, params


def _scan_required_inputs(yaml_text: str) -> list[str]:
    """掃 workflow YAML 找所有 {{ input.X }} 引用、回 sorted unique key list。

    給 TG /run 反問用 — 缺了哪些 input 就列出來。
    """
    try:
        import yaml as _yaml
        from pipeline.models import PipelineConfig
        from pipeline.expression import find_referenced_vars
        data = _yaml.safe_load(yaml_text) or {}
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = config_dict.get("validate", True)
        config = PipelineConfig(**config_dict)
    except Exception:
        return []

    refs: set[str] = set()
    for step in config.steps:
        for fname in ("batch", "message", "uia_window", "vv_prompt", "working_dir"):
            v = getattr(step, fname, "")
            if isinstance(v, str) and v:
                refs.update(find_referenced_vars(v))
        if step.output and step.output.path:
            refs.update(find_referenced_vars(step.output.path))
        if step.actions:
            for a in step.actions:
                for fname in ("text", "title", "vlm_prompt", "expected"):
                    v = getattr(a, fname, "")
                    if isinstance(v, str) and v:
                        refs.update(find_referenced_vars(v))
    return sorted({r.split(".", 1)[1] for r in refs if r.startswith("input.") and "." in r})


async def _cmd_run_pipeline(chat_id: int, args: str) -> None:
    """/run <name|id> [k=v k=v ...] — 啟動指定工作流。

    支援:
      /run daily_report
      /run daily_report date=2026-05-10 customer=ACME
      /run daily_report date=today                      ← today/yesterday/tomorrow 自動轉日期

    若 workflow 引用了 {{ input.X }} 但 args 沒帶該 key、bot 會反問必填欄位。
    """
    query, input_params = _parse_run_args(args or "")
    if not query:
        await _bot_instance.send_message(
            chat_id=chat_id,
            text=("⚠ 用法:\n"
                  "<code>/run &lt;workflow 名稱或 id&gt;</code>\n"
                  "<code>/run &lt;workflow&gt; key=value key=value</code>\n\n"
                  "範例:<code>/run daily_report date=today customer=ACME</code>\n"
                  "特殊值:<code>today</code> / <code>yesterday</code> / <code>tomorrow</code> 自動轉日期"),
            parse_mode="HTML",
        )
        return
    try:
        from db import list_workflows
        from main import start_pipeline, PipelineRunRequest
        wfs = list_workflows() or []
        ql = query.lower()
        matches = [w for w in wfs if (w.get("id") or "").startswith(query) or (w.get("id") or "") == query]
        if not matches:
            matches = [w for w in wfs if ql in (w.get("name") or "").lower()]
        if not matches:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 找不到符合 <code>{html.escape(query)}</code> 的 workflow",
                parse_mode="HTML",
            )
            return
        if len(matches) > 1:
            lines = [f"⚠ <b>找到多個符合的 workflow、請用更精確的名稱或 id</b>:", ""]
            for w in matches[:8]:
                lines.append(f"- <b>{html.escape(w.get('name') or '')}</b> (id=<code>{w.get('id')}</code>)")
            await _bot_instance.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
            return
        wf = matches[0]
        yaml_text = wf.get("yaml") or ""
        if not yaml_text.strip():
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ workflow <b>{html.escape(wf.get('name') or '')}</b> 的 YAML 是空的、無法啟動",
                parse_mode="HTML",
            )
            return

        # 必填 input 反問:workflow 寫了 {{ input.X }} 但 args 沒帶
        required_inputs = _scan_required_inputs(yaml_text)
        missing = [k for k in required_inputs if k not in input_params]
        if missing:
            wf_name = wf.get("name") or ""
            lines = [
                f"⚠ workflow <b>{html.escape(wf_name)}</b> 需要以下啟動參數:",
                "",
            ]
            for k in missing:
                lines.append(f"  • <code>{html.escape(k)}</code>")
            example = " ".join(f"{k}=值" for k in missing[:3])
            lines.extend([
                "",
                f"請改用:<code>/run {html.escape(wf_name)} {html.escape(example)}</code>",
                "",
                "💡 日期欄位可用 <code>today</code> / <code>yesterday</code> / <code>tomorrow</code>",
            ])
            await _bot_instance.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
            return

        # 啟動 pipeline(沿用 main.py /pipeline/run 邏輯)
        try:
            import yaml as _yaml
            parsed = _yaml.safe_load(yaml_text) or {}
            steps = parsed.get("steps") or []
            needs_validate = bool(parsed.get("validate")) or any(
                isinstance(s, dict) and s.get("expect") for s in steps
            )
        except Exception:
            needs_validate = False
        req = PipelineRunRequest(
            yaml_content=yaml_text,
            validate=needs_validate,
            use_recipe=True,
            workflow_id=wf.get("id"),
            silent_recipe=True,  # 無人值守:不彈 recipe 確認 dialog
            input_params=input_params,
        )
        result = await start_pipeline(req)
        run_id = (result or {}).get("run_id") or "?"
        param_lines = ""
        if input_params:
            param_lines = "\n參數:\n" + "\n".join(
                f"  • <code>{html.escape(k)}</code> = <code>{html.escape(str(v))}</code>"
                for k, v in input_params.items()
            ) + "\n"
        receipt = (
            f"🚀 已啟動 <b>{html.escape(wf.get('name') or '')}</b>\n"
            f"run_id: <code>{run_id}</code>"
            f"{param_lines}\n"
            f"📡 進度會自動推送到此對話。\n"
            f"📜 也可隨時 /log <code>{run_id[:8]}</code> 看細節。\n"
            f"🛑 想中止:/abort <code>{run_id}</code>"
        )
        await _bot_instance.send_message(chat_id=chat_id, text=receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/run] 失敗:{e}", exc_info=True)
        await _bot_instance.send_message(chat_id=chat_id, text=f"❌ /run 失敗:{str(e)[:200]}")


async def _cmd_yaml(chat_id: int, args: str) -> None:
    """/yaml <name|id> — 拉指定 workflow 的 YAML、送 + 快取進附加上下文。
    支援工作流 name(模糊配對)或 id 前綴。"""
    query = (args or "").strip()
    if not query:
        await _bot_instance.send_message(
            chat_id=chat_id, text="⚠ 用法：/yaml &lt;workflow 名稱或 id&gt;",
            parse_mode="HTML",
        )
        return
    try:
        from db import list_workflows
        wfs = list_workflows() or []
        # 先試 id 完整或前綴比對
        matches = [w for w in wfs if (w.get("id") or "").startswith(query) or (w.get("id") or "") == query]
        if not matches:
            # 退到 name 包含比對(case-insensitive)
            ql = query.lower()
            matches = [w for w in wfs if ql in (w.get("name") or "").lower()]
        if not matches:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"❌ 找不到符合 <code>{html.escape(query)}</code> 的 workflow",
                parse_mode="HTML",
            )
            return
        if len(matches) > 1:
            lines = [f"⚠ <b>找到多個符合的 workflow、請用更精確的名稱或 id</b>：", ""]
            for w in matches[:8]:
                lines.append(f"- <b>{html.escape(w.get('name') or '')}</b> (id=<code>{w.get('id')}</code>)")
            await _bot_instance.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
            return
        wf = matches[0]
        yaml_text = wf.get("yaml") or ""
        if not yaml_text.strip():
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=f"⚠ workflow <b>{html.escape(wf.get('name') or '')}</b> 的 YAML 是空的",
                parse_mode="HTML",
            )
            return
        # 快取進 chat 附加上下文
        label = f"YAML of workflow `{wf.get('name')}` (id: {wf.get('id')})"
        _add_loaded_context(chat_id, label, yaml_text)
        # 不 dump 全文到 TG。YAML 一般 < 4KB 不會洗版,但仍走 cache-only 統一行為
        receipt = (
            f"📄 已載入 YAML of <code>{html.escape(wf.get('name') or '')}</code>\n"
            f"id: <code>{wf.get('id')}</code>\n"
            f"📏 {len(yaml_text):,} 字元\n\n"
            "💬 直接跟 AI 對話,他會基於這份 YAML 給建議或修改。\n"
            "想看 raw 內容跟 AI 說「把 YAML 貼給我看」即可。"
        )
        await _bot_instance.send_message(chat_id=chat_id, text=receipt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/yaml] 失敗：{e}", exc_info=True)
        await _bot_instance.send_message(chat_id=chat_id, text=f"❌ /yaml 失敗：{str(e)[:200]}")


# ── TG 自由文字 → AI 助手 ──────────────────────────────────────────────────
# Telegram 訊息上限 4096 字元；這裡留 200 字 safety margin 給 markdown 開頭結尾
_TG_MSG_MAX = 3900


def _markdown_to_tg_html(text: str) -> str:
    """把常見 Markdown 轉成 Telegram HTML parse mode。

    為什麼不用 Markdown V1:LLM 輸出常含 `_` 之類字元(snake_case 變數名)、Markdown V1
    把 `_` 當斜體標記、不平衡就整個 parse 失敗、TG 退到 plain text 顯示就看到 raw `**` 字面。
    HTML parse mode 較穩,只需 escape `<` `>` `&`。

    支援:
    - **粗體** → <b>粗體</b>
    - ~~刪除~~ → <s>刪除</s>
    - `inline code` → <code>inline code</code>
    - ```code block``` → <pre>code block</pre>(語言標記略過)
    - [文字](url) → <a href="url">文字</a>
    - # / ## / ### 標題 → <b>標題</b>(TG 沒原生標題)
    - 不轉斜體(*X*)避免 LLM 偶發單 * 寫法干擾
    """
    if not text:
        return text
    import re as _re
    import html as _html

    # 1. 先抽出 ```code block```、用佔位符標記、避免裡面內容被其他規則動到
    code_blocks: list[str] = []
    def _save_cb(m):
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks)-1}\x00"
    text = _re.sub(r"```(?:\w*)?\n?(.*?)\n?```", _save_cb, text, flags=_re.DOTALL)

    # 2. 抽 inline `code`
    inline_codes: list[str] = []
    def _save_ic(m):
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes)-1}\x00"
    text = _re.sub(r"`([^`\n]+)`", _save_ic, text)

    # 3. HTML escape 剩下的(此時 ` 跟 ``` 內容都不在了、不會誤殺)
    text = _html.escape(text, quote=False)

    # 4. 套 markdown → HTML
    # 標題 ### → <b>
    text = _re.sub(r"^#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text, flags=_re.MULTILINE)
    # 粗體 **X** / __X__
    text = _re.sub(r"\*\*([^\*\n]+?)\*\*", r"<b>\1</b>", text)
    text = _re.sub(r"__([^_\n]+?)__", r"<b>\1</b>", text)
    # 刪除線 ~~X~~
    text = _re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", text)
    # 連結 [X](url)
    text = _re.sub(r"\[([^\]\n]+?)\]\(([^\)\n]+?)\)", r'<a href="\2">\1</a>', text)

    # 5. 還原 code(內容仍要 escape)
    def _restore_cb(m):
        idx = int(m.group(1))
        return f"<pre>{_html.escape(code_blocks[idx], quote=False)}</pre>"
    text = _re.sub(r"\x00CB(\d+)\x00", _restore_cb, text)
    def _restore_ic(m):
        idx = int(m.group(1))
        return f"<code>{_html.escape(inline_codes[idx], quote=False)}</code>"
    text = _re.sub(r"\x00IC(\d+)\x00", _restore_ic, text)
    return text


async def _send_long_message(chat_id: int, text: str, parse_mode: str | None = None) -> None:
    """送長訊息：超過 4096 字元自動分段。盡量在換行處切、避免切到 code block 中間。

    parse_mode 失敗(如 Markdown 不合法、TG BadRequest)會自動退到 plain text 重送、
    確保使用者一定看到內容、不會因為 markdown 殘缺整個訊息消失。
    """
    if not text:
        return

    async def _send_one(content: str):
        """單則訊息送出、parse_mode 失敗自動 fallback plain text。"""
        try:
            await _bot_instance.send_message(chat_id=chat_id, text=content, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"_send_long_message parse_mode={parse_mode} failed: {e}; retrying plain")
            await _bot_instance.send_message(chat_id=chat_id, text=content)

    if len(text) <= _TG_MSG_MAX:
        await _send_one(text)
        return
    # 分段：先試在換行處切
    parts: list[str] = []
    remaining = text
    while len(remaining) > _TG_MSG_MAX:
        cut = remaining.rfind("\n\n", 0, _TG_MSG_MAX)
        if cut < 1000:
            cut = remaining.rfind("\n", 0, _TG_MSG_MAX)
        if cut < 500:
            cut = _TG_MSG_MAX
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    for i, p in enumerate(parts):
        prefix = f"({i + 1}/{len(parts)}) " if len(parts) > 1 else ""
        await _send_one(prefix + p)


def _build_tg_state_digest() -> str:
    """為 TG AI 助手注入「Atlas 目前狀態」的 markdown 區塊(讓 LLM 不用 tool 也能看到狀態)。

    包含:
      - 最近 5 個活躍 workflow(id / name / 最後 run 狀態+時間)
      - 最近一次 run(任何狀態) 的 log 末尾 30 行
      - 若有最近 24 小時內 failed/awaiting_human 的 run、額外列出
    """
    try:
        from db import list_workflows, list_runs
    except Exception:
        return ""
    try:
        wfs = list_workflows() or []
        runs = list_runs(limit=30) or []
    except Exception as e:
        return f"## 狀態 digest\n\n(載入失敗:{e})"

    # 索引每個 workflow 的最近一次 run
    wf_latest: dict[str, dict] = {}
    for r in runs:
        wid = r.get("_workflow_id") or ""
        if wid and wid not in wf_latest:
            wf_latest[wid] = r

    # 排序 workflow:有 run 的按最近 run 時間倒排排在前、沒 run 的排後
    have_runs = [w for w in wfs if wf_latest.get(w.get("id") or "")]
    no_runs = [w for w in wfs if not wf_latest.get(w.get("id") or "")]
    have_runs.sort(
        key=lambda w: wf_latest.get(w.get("id") or "", {}).get("started_at") or "",
        reverse=True,
    )
    wfs_sorted = (have_runs + no_runs)[:5]

    lines = ["## Atlas 目前狀態(自動注入,無須使用者再說一次)", ""]
    lines.append("### 最近 5 個活躍工作流")
    for wf in wfs_sorted:
        wid = wf.get("id") or ""
        name = wf.get("name") or wid
        r = wf_latest.get(wid)
        if r:
            status = r.get("status") or "?"
            started = r.get("started_at") or ""
            try:
                from datetime import datetime
                if isinstance(started, str) and started:
                    ts_str = datetime.fromisoformat(started).strftime("%m/%d %H:%M")
                elif isinstance(started, (int, float)) and started:
                    ts_str = datetime.fromtimestamp(started).strftime("%m/%d %H:%M")
                else:
                    ts_str = ""
            except Exception:
                ts_str = ""
            run_id = r.get("run_id") or ""
            lines.append(f"- **{name}** (id=`{wid}`) — 最後 run: `{status}` ({ts_str}) run_id=`{run_id}`")
        else:
            lines.append(f"- **{name}** (id=`{wid}`) — 尚未跑過")
    lines.append("")

    # 最近一次 run 的 log 摘要
    if runs:
        latest = runs[0]
        run_id = latest.get("run_id") or ""
        wf_name = latest.get("pipeline_name") or ""
        status = latest.get("status") or "?"
        lines.append(f"### 最近一次 run:`{wf_name}` ({status})")
        lines.append(f"- run_id: `{run_id}`")
        # 找對應 log 檔(filename 含 run_id 後 8 字)
        try:
            # log 目錄集中解析(優先 OUTPUT_BASE_PATH/pipeline_logs、fallback 舊 backend/ai_output)
            from pipeline.logger import find_run_log as _frl
            tail_text = ""
            hit = _frl(run_id) if run_id else None
            if hit is not None:
                log_text = hit.read_text(encoding="utf-8", errors="replace")
                tail_lines = log_text.splitlines()[-30:]
                tail_text = "\n".join(tail_lines)
            if tail_text:
                lines.append("- log 末尾 30 行:")
                lines.append("```")
                lines.append(tail_text[-2400:])  # 再截長
                lines.append("```")
        except Exception:
            pass
        lines.append("")

    lines.append("使用者若提到具體 workflow 或 run、優先用上面資料解答。"
                 "若資料不夠、請反問使用者(例:「給我那個 run 的詳細錯誤訊息」)、不要編造。")
    return "\n".join(lines)


# LLM agent 整體 timeout(從 user msg 收到到 AI 回覆完整、含多輪 tool call)
# 300s:給大 model + tool 上限 5 輪場合留充足餘裕
_TG_AI_RESPONSE_TIMEOUT = 300.0
_TG_TYPING_REFRESH_INTERVAL = 4.0  # TG typing 動畫只顯示 ~5s、要持續送 keepalive


def _tool_progress_text(tool_name: str, tool_args: dict) -> str:
    """把 tool 呼叫翻成 TG 推送的進度文字(一行、含 emoji)。

    參考 LLM 真的會 call 的 7 個 tool:
    list_workflows / get_workflow_yaml / get_recent_runs / get_run_log
    save_workflow_yaml / start_workflow / send_file_to_tg / web_search
    """
    a = tool_args or {}
    if tool_name == "list_workflows":
        return "🔍 列工作流..."
    if tool_name == "get_workflow_yaml":
        q = (a.get("query") or "")[:40]
        return f"📄 讀「{q}」的 YAML..."
    if tool_name == "get_recent_runs":
        q = (a.get("query") or "")[:40]
        return f"⏱ 看「{q}」最近執行紀錄..."
    if tool_name == "get_run_log":
        rid = (a.get("run_id") or "")[:12]
        return f"📜 讀 run <code>{rid}</code> 的 log..."
    if tool_name == "save_workflow_yaml":
        q = (a.get("query") or "")[:40]
        confirm = bool(a.get("confirm"))
        return f"💾 套用 YAML 到「{q}」..." if confirm else f"👀 預覽:準備把 YAML 套到「{q}」..."
    if tool_name == "create_workflow_yaml":
        n = (a.get("name") or "")[:40]
        confirm = bool(a.get("confirm"))
        return f"➕ 建立新工作流「{n}」..." if confirm else f"👀 預覽:準備建新工作流「{n}」..."
    if tool_name == "start_workflow":
        q = (a.get("query") or "")[:40]
        confirm = bool(a.get("confirm"))
        return f"🚀 啟動「{q}」..." if confirm else f"👀 預覽:準備啟動「{q}」..."
    if tool_name == "send_file_to_tg":
        q = (a.get("workflow_query") or "")[:30]
        fn = (a.get("filename") or "")[:30]
        confirm = bool(a.get("confirm"))
        if not fn:
            return f"📁 列「{q}」的輸出檔..."
        return f"📎 傳 <code>{fn}</code> 到 TG..." if confirm else f"👀 預覽:準備傳 <code>{fn}</code>..."
    if tool_name == "web_search":
        q = (a.get("query") or "")[:60]
        return f"🌐 搜「{q}」..."
    if tool_name == "list_schedules":
        return "📅 列排程..."
    if tool_name == "schedule_workflow":
        q = (a.get("query") or "")[:30]
        cron = (a.get("schedule_expr") or "")[:30]
        confirm = bool(a.get("confirm"))
        return f"📅 建排程「{q}」{cron}..." if confirm else f"👀 預覽:準備為「{q}」建排程({cron})..."
    if tool_name == "cancel_schedule":
        q = (a.get("task_id_or_name") or "")[:40]
        confirm = bool(a.get("confirm"))
        return f"🗑 取消排程「{q}」..." if confirm else f"👀 預覽:準備取消排程「{q}」..."
    # fallback:未知 tool
    return f"🔧 {tool_name}..."


async def _typing_keepalive(chat_id: int, stop_event: asyncio.Event) -> None:
    """背景任務:每 4 秒重發 typing action、直到 stop_event 被 set。
    確保 LLM 跑很久時 TG 上的「正在輸入...」動畫不會中斷。"""
    while not stop_event.is_set():
        try:
            await _bot_instance.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass  # send_chat_action 失敗不影響主流程
        try:
            # 等 stop_event 或 timeout、whichever first
            await asyncio.wait_for(stop_event.wait(), timeout=_TG_TYPING_REFRESH_INTERVAL)
        except asyncio.TimeoutError:
            continue


async def _handle_tg_freeform_chat(chat_id: int, text: str) -> None:
    """TG 收到非 slash、非 awaiting state 的自由文字 → 丟給 /pipeline/chat AI 助手。

    每個 chat_id 一條歷史(in-memory),保留最近 _TG_CHAT_HISTORY_CAP 則訊息。
    LLM 用 _build_pipeline_system_prompt(跟桌面 chat 一致) + TG 狀態 digest。
    LLM 跑期間用 typing keepalive 持續顯示「輸入中...」動畫;超過 _TG_AI_RESPONSE_TIMEOUT 秒拋友善 timeout。
    """
    history = _tg_chat_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})

    # 啟動 typing keepalive(背景任務、跟 LLM call 並行、function 結束時清掉)
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_keepalive(chat_id, stop_typing))

    async def _cleanup_typing():
        stop_typing.set()
        try:
            await asyncio.wait_for(typing_task, timeout=2.0)
        except Exception:
            if not typing_task.done():
                typing_task.cancel()

    try:
        # 直接 await main 的 _chat_agent_loop function、避開 HTTP roundtrip
        # _chat_agent_loop 接受 on_tool_event callback、tool 呼叫前後送進度給 TG
        from main import _chat_agent_loop, PipelineChatRequest
        digest = _build_tg_state_digest()
        # 把使用者用 /log /yaml 載入的附加上下文也注入
        loaded = _tg_loaded_context.get(chat_id) or []
        if loaded:
            ctx_blocks = ["", "## 使用者載入的完整內容(他用 /log 或 /yaml 主動拉的、優先參考)", ""]
            for label, content, _ts in loaded:
                ctx_blocks.append(f"### {label}")
                ctx_blocks.append("```")
                ctx_blocks.append(content)
                ctx_blocks.append("```")
                ctx_blocks.append("")
            digest = (digest or "") + "\n".join(ctx_blocks)
        req = PipelineChatRequest(
            messages=list(history),
            workflow_id=None,
            extra_system=digest if digest else None,
        )

        # Tool 進度 callback:tool 呼叫前送一行進度給 TG、user 知道 AI 在做啥
        # 只 fire "before"、不送 "after"(避免訊息洪水);
        # tool 完成的訊號 = 下一個 before 或最終 reply
        async def _tool_progress(phase: str, tc: dict, result: str | None) -> None:
            if phase != "before":
                return
            try:
                msg = _tool_progress_text(tc.get("name") or "", tc.get("args") or {})
                await _bot_instance.send_message(
                    chat_id=chat_id, text=msg, parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"[tool progress] 送進度失敗(忽略):{e}")

        # 包 timeout:_TG_AI_RESPONSE_TIMEOUT 秒沒回 → asyncio.TimeoutError
        result = await asyncio.wait_for(
            _chat_agent_loop(req, on_tool_event=_tool_progress),
            timeout=_TG_AI_RESPONSE_TIMEOUT,
        )
        reply = (result or {}).get("reply") or ""
        # 若 AI 產出 YAML、緩存起來給 /save 用
        if result and result.get("has_yaml") and result.get("yaml_content"):
            _tg_last_ai_yaml[chat_id] = {
                "yaml": result["yaml_content"],
                "ts": time.time(),
                "yaml_error": result.get("yaml_error"),
            }
    except asyncio.TimeoutError:
        logger.warning(f"[TG chat] LLM 回應超過 {_TG_AI_RESPONSE_TIMEOUT}s 逾時")
        await _cleanup_typing()
        await _bot_instance.send_message(
            chat_id=chat_id,
            text=(f"⏱ AI 回應超過 {int(_TG_AI_RESPONSE_TIMEOUT)} 秒沒結果、已自動中止。\n"
                  "可能網路慢、model 太大、或太複雜。建議:換更快的 model、或拆成小一點的問題重試。"),
        )
        if history and history[-1].get("role") == "user":
            history.pop()
        return
    except Exception as e:
        logger.error(f"[TG chat] AI 助手呼叫失敗：{e}", exc_info=True)
        await _cleanup_typing()
        # HTTPException 走 _friendly_llm_error 翻譯後、detail 是繁中友善訊息;
        # 其他 exception 退回 type+前 200 字
        try:
            from fastapi import HTTPException as _HTTPExc
            friendly = e.detail if isinstance(e, _HTTPExc) and getattr(e, "detail", None) else str(e)
        except Exception:
            friendly = str(e)
        await _bot_instance.send_message(
            chat_id=chat_id, text=f"❌ AI 助手回應失敗:\n{str(friendly)[:400]}",
        )
        # 失敗就把這次 user msg 移除、避免歷史污染
        if history and history[-1].get("role") == "user":
            history.pop()
        return

    # 收到 LLM 回覆 → 清掉 typing(後續送訊息不需要 typing 動畫)
    await _cleanup_typing()

    if not reply.strip():
        await _bot_instance.send_message(chat_id=chat_id, text="(AI 助手回覆為空、忽略本次)")
        return

    # 寫進歷史 + 截長
    history.append({"role": "assistant", "content": reply})
    if len(history) > _TG_CHAT_HISTORY_CAP:
        del history[: len(history) - _TG_CHAT_HISTORY_CAP]

    # 送給 user。先分段(按 markdown 換行)再各段轉 HTML、避免 <pre> 被切到中間
    # 用 HTML parse mode 比 Markdown V1 穩(後者遇到 `_` snake_case 等容易整個 parse 失敗)
    if len(reply) <= _TG_MSG_MAX:
        await _send_long_message(chat_id, _markdown_to_tg_html(reply), parse_mode="HTML")
    else:
        # 手動分段:盡量在 \n\n 切、各段獨立 markdown→HTML
        parts: list[str] = []
        remaining = reply
        while len(remaining) > _TG_MSG_MAX:
            cut = remaining.rfind("\n\n", 0, _TG_MSG_MAX)
            if cut < 1000:
                cut = remaining.rfind("\n", 0, _TG_MSG_MAX)
            if cut < 500:
                cut = _TG_MSG_MAX
            parts.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        if remaining:
            parts.append(remaining)
        for i, p in enumerate(parts):
            prefix = f"({i + 1}/{len(parts)}) " if len(parts) > 1 else ""
            try:
                await _bot_instance.send_message(
                    chat_id=chat_id, text=prefix + _markdown_to_tg_html(p), parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"HTML send failed: {e}; retry plain")
                await _bot_instance.send_message(chat_id=chat_id, text=prefix + p)


async def _register_bot_commands(bot) -> None:
    """寫入 TG 客戶端 autocomplete 清單（取代 BotFather 既有設定）。
    這是 bot-global 設定，會覆蓋此 bot token 在其他專案註冊過的 /commands、
    /skill、/approve 等舊項目。Token 變更才呼叫一次，不需常駐刷新。
    """
    from telegram import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand("menu",       "列工作流並啟動（附執行中／排程徽章）"),
            BotCommand("run",        "啟動某工作流（用法：/run <name|id>）"),
            BotCommand("save",       "把對話中 AI 產的 YAML 套到指定工作流"),
            BotCommand("status",     "查看目前執行中的 run"),
            BotCommand("log",        "拉某個 run 的完整 log（用法：/log <run_id>）"),
            BotCommand("yaml",       "拉某個 workflow 的 YAML（用法：/yaml <name|id>）"),
            BotCommand("screenshot", "抓 host 桌面即時截圖"),
            BotCommand("abort",      "中止某個 run（用法：/abort <run_id>）"),
            BotCommand("reset",      "清空 AI 助手對話歷史 + 附加上下文"),
            BotCommand("help",   "顯示指令說明"),
        ])
        logger.info("Telegram bot 指令清單（autocomplete）已更新為 Atlas 版本")
    except Exception as e:
        # set_my_commands 失敗不影響功能（autocomplete 只是視覺提示），記 log 就好
        logger.warning(f"set_my_commands 失敗（忽略，不影響功能）：{e}")


async def _start_workflow_from_tg(chat_id: int, wf_id: str, force: bool = False) -> None:
    """從 TG 啟動工作流。把 chat_id 帶進 run、後續通知會推回此對話。
    force=False 時若同 workflow 已有 running/awaiting 的 run，會先回警告 + 強制啟動按鈕。
    """
    logger = logging.getLogger("telegram")
    import db
    wf = db.get_workflow(wf_id)
    if not wf:
        await _bot_instance.send_message(
            chat_id=chat_id, text=f"❌ 找不到工作流：{wf_id}",
        )
        return
    yaml_content = (wf.get("yaml") or "").strip()
    if not yaml_content:
        await _bot_instance.send_message(
            chat_id=chat_id,
            text=("⚠ 此工作流的 YAML 為空，無法直接啟動。"
                  "請先在前端開啟並儲存一次（觸發 yaml 自動產生）後再試。"),
        )
        return

    # ── 重複啟動守門：同 workflow 已在執行 → 先警告，需用戶確認才強跑 ──
    if not force:
        try:
            from pipeline.store import get_store
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            for r in get_store().list_recent(limit=30):
                if (
                    getattr(r, "workflow_id", None) == wf_id
                    and r.status in ("running", "awaiting_human")
                ):
                    status_label = "🔄 執行中" if r.status == "running" else "⏸ 等待人工確認"
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "⚠ 仍要強制啟動（會跑兩個 run）",
                            callback_data=f"pipe_force_start_wf:{wf_id}",
                        )],
                        [InlineKeyboardButton("取消", callback_data="pipe_cancel_select")],
                    ])
                    await _bot_instance.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠ 此工作流已有執行中的 run，先確認再操作：\n\n"
                            f"  📛 名稱：<b>{wf.get('name') or wf_id}</b>\n"
                            f"  🆔 run_id：<code>{r.run_id}</code>\n"
                            f"  📍 狀態：{status_label}\n\n"
                            f"建議：\n"
                            f"  • <code>/status</code> 看詳細進度\n"
                            f"  • <code>/abort {r.run_id}</code> 中止舊 run\n\n"
                            f"若確定要再開一個（會兩個 run 平行跑），點下方按鈕。"
                        ),
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    return
        except Exception as e:
            logger.warning(f"重複 run 檢查失敗（忽略繼續啟動）：{e}")

    try:
        import uuid
        import asyncio
        import yaml as yaml_lib
        from pipeline.models import PipelineConfig
        from pipeline.runner import run_pipeline, register_task
        from pipeline.store import PipelineRun as PRun, get_store
        from pipeline.logger import create_run_logger

        data = yaml_lib.safe_load(yaml_content) or {}
        config_dict = data.get("pipeline", data)
        config_dict["validate"] = True
        config = PipelineConfig(**config_dict)

        run_id = str(uuid.uuid4())[:12]
        _, log_path = create_run_logger(run_id, config.name)
        config_d = config.model_dump()
        config_d["_workflow_id"] = wf_id

        run = PRun(
            run_id=run_id,
            pipeline_name=config.name,
            config_dict=config_d,
            telegram_chat_id=chat_id,  # 後續通知推回此對話
            log_path=log_path,
            workflow_id=wf_id,
        )
        get_store().save(run)
        task = asyncio.create_task(run_pipeline(config_d, chat_id=chat_id, run_id=run_id))
        register_task(run_id, task)

        await _bot_instance.send_message(
            chat_id=chat_id,
            text=(f"🚀 已啟動工作流：<b>{config.name}</b>\n"
                  f"run_id: <code>{run_id}</code>\n\n"
                  f"後續進度會自動推送到此對話。"),
            parse_mode="HTML",
        )
        logger.info(f"[遠端遙控] 從 TG 啟動 run {run_id}（{config.name}）")
    except Exception as e:
        logger.error(f"[遠端遙控] 啟動失敗：{e}", exc_info=True)
        await _bot_instance.send_message(
            chat_id=chat_id, text=f"❌ 啟動失敗：{str(e)[:300]}",
        )


async def _poll_loop():
    """長輪詢 Telegram updates，處理 callback_query 和文字訊息"""
    from telegram import Bot
    from telegram.error import RetryAfter, TimedOut, NetworkError, Conflict
    global _bot_instance, _current_token

    last_offset = 0
    _bot_instance = None
    _current_token = ""

    while True:
        # 每個 iteration 檢 lock 還是不是自己；不是的話退出
        # （另一實例已接管 polling 的話，本實例該停手）
        if not _i_still_hold_lock():
            logger.warning(
                "Telegram polling lock 已被另一實例接管，本實例退出 polling"
                " 避免兩邊一起 poll 同 token 造成 409 Conflict。"
            )
            return

        try:
            from settings import get_settings
            s = get_settings()
            token = s.get("telegram_bot_token", "")
            # Fallback 順序：pipeline_settings.json → .env TELEGRAM_BOT_TOKEN
            # 後端 outbound 通知是讀 env var，有些人只設 env 沒存到 settings UI，
            # polling loop 若只讀 settings 會永遠 sleep 導致 callback 收不到
            if not token:
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                await asyncio.sleep(15)
                continue

            # token 變更時重建 bot
            if token != _current_token:
                if _bot_instance:
                    try:
                        await _bot_instance.close()
                    except Exception:
                        pass
                _bot_instance = Bot(token=token)
                _current_token = token
                last_offset = 0  # 重置 offset
                # 清除舊 session，避免 Conflict
                try:
                    await _bot_instance.delete_webhook(drop_pending_updates=False)
                    # 短 timeout getUpdates 搶佔 session
                    stale = await _bot_instance.get_updates(timeout=1)
                    if stale:
                        last_offset = stale[-1].update_id + 1
                except Exception:
                    pass
                logger.info("Telegram bot 已連線（session 已重置）")
                # 把 autocomplete 指令清單覆寫成 Atlas 版（覆蓋 bot 上既有的指令清單）
                await _register_bot_commands(_bot_instance)

            updates = await _bot_instance.get_updates(
                offset=last_offset,
                timeout=30,
                allowed_updates=["callback_query", "message"],
            )

            for update in updates:
                last_offset = update.update_id + 1

                # ── 文字訊息：檢查是否有等待中的補充指示或 ask_user 答案 ──
                if update.message and update.message.text:
                    chat_id = update.message.chat_id
                    if chat_id in _pending_answers:
                        run_id = _pending_answers.pop(chat_id)
                        answer = update.message.text.strip()
                        logger.info(f"收到 ask_user 答案 for run {run_id}: {answer[:100]}")
                        try:
                            from pipeline.runner import resume_pipeline
                            msg = await resume_pipeline(run_id, "answer", hint=answer)
                            await _bot_instance.send_message(
                                chat_id=chat_id,
                                text=f"✅ {msg}",
                            )
                        except Exception as e:
                            logger.error(f"ask_user answer failed: {e}")
                            await _bot_instance.send_message(
                                chat_id=chat_id,
                                text=f"❌ 送出失敗：{str(e)[:200]}",
                            )
                        continue
                    if chat_id in _pending_hints:
                        run_id = _pending_hints.pop(chat_id)
                        hint_text = update.message.text.strip()
                        logger.info(f"收到補充指示 for run {run_id}: {hint_text[:100]}")
                        try:
                            from pipeline.runner import resume_pipeline
                            msg = await resume_pipeline(run_id, "retry_with_hint", hint=hint_text)
                            await _bot_instance.send_message(
                                chat_id=chat_id,
                                text=f"💬 已收到指示，正在重試…\n\n{msg}",
                            )
                        except Exception as e:
                            logger.error(f"Hint resume failed: {e}")
                            await _bot_instance.send_message(
                                chat_id=chat_id,
                                text=f"❌ 重試失敗：{str(e)[:200]}",
                            )
                        continue
                    # ── 遠端遙控指令（必須 settings 開啟 + chat_id 授權）──
                    text = update.message.text.strip()
                    if text.startswith("/"):
                        if _is_remote_control_authorized(chat_id):
                            await _handle_remote_command(chat_id, text)
                        else:
                            # 不授權的 /command 也記一筆 — 方便 debug 為何 /menu 沒反應
                            try:
                                from settings import get_settings as _gs
                                _s = _gs()
                                _enabled = bool(_s.get("telegram_remote_control", False))
                                _auth = (_s.get("telegram_chat_id") or "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
                                logger.warning(
                                    f"[遠端遙控] 拒絕 {text} from chat_id={chat_id}："
                                    f"toggle={_enabled}, auth_chat={_auth!r}, match={str(chat_id) == _auth}"
                                )
                            except Exception:
                                pass
                        continue
                    # ── 非 slash、非 awaiting：自由文字丟給 AI 助手 ──
                    # 同樣需要 telegram_remote_control toggle + chat_id 授權
                    # （避免外人 DM bot 把它變成免費聊天機器人）
                    if _is_remote_control_authorized(chat_id):
                        try:
                            await _handle_tg_freeform_chat(chat_id, text)
                        except Exception as e:
                            logger.error(f"[TG chat] freeform 處理失敗：{e}", exc_info=True)
                            # 通知使用者、避免「沒回應」黑洞體驗
                            try:
                                await _bot_instance.send_message(
                                    chat_id=chat_id,
                                    text=f"❌ 處理訊息時發生錯誤：{type(e).__name__}: {str(e)[:200]}",
                                )
                            except Exception:
                                pass
                    continue

                if not update.callback_query:
                    continue

                cb = update.callback_query
                data = cb.data or ""

                # 解析 callback_data: pipe_{action}:{run_id} 或 pipe_answer:{run_id}:{idx}
                if not data.startswith("pipe_"):
                    continue

                parts = data.split(":", 2)
                if len(parts) < 2:
                    continue

                # ── 授權檢查(P1 安全):callback 能核准 human_confirm / 中止 run,
                #    必須來自授權 chat(settings.telegram_chat_id / .env TELEGRAM_CHAT_ID)。
                #    否則任何拿到 bot 的人按舊訊息按鈕 / 偽造 callback 就能放行高風險動作。
                _cb_chat = cb.message.chat_id if cb.message else None
                _auth_chat = ""
                try:
                    from settings import get_settings as _gs_cb
                    _auth_chat = (_gs_cb().get("telegram_chat_id") or "").strip()
                except Exception:
                    pass
                if not _auth_chat:
                    _auth_chat = (os.environ.get("TELEGRAM_CHAT_ID", "") or "").strip()
                if _auth_chat and str(_cb_chat) != _auth_chat:
                    logger.warning(f"Telegram: 拒絕未授權 callback(chat {_cb_chat} ≠ 授權 {_auth_chat})")
                    try:
                        await cb.answer("⛔ 未授權", show_alert=True)
                    except Exception:
                        pass
                    continue

                action = parts[0].replace("pipe_", "")
                run_id = parts[1]
                extra = parts[2] if len(parts) >= 3 else ""

                # ── 查看 Log ──
                if action == "log":
                    logger.info(f"Telegram: 查看 log for run {run_id}")
                    try:
                        from pipeline.runner import get_run_log_tail
                        log_text = get_run_log_tail(run_id, lines=25)
                        # Telegram 訊息上限 4096 字元
                        if len(log_text) > 3800:
                            log_text = "…（前面省略）\n" + log_text[-3800:]
                        safe_log = html.escape(log_text)
                        await cb.answer("📋 Log 已發送")
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text=f"📋 <b>Pipeline Log（最近 25 行）</b>\n\n<pre>{safe_log}</pre>",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        await cb.answer(f"❌ {str(e)[:150]}")
                    continue

                # ── 截圖 ── 逐螢幕截（1 螢幕 1 張、N 螢幕 N 張）
                # 委託給 runner._tg_send_photos，行為跟自動截圖一致，並含 photo→document fallback
                # （4K 螢幕 PNG 常 >5MB、send_photo 會被拒；send_document 不受尺寸/壓縮限制）
                if action == "screenshot":
                    logger.info(f"Telegram: 截圖 for run {run_id}")
                    try:
                        from pipeline.store import get_store
                        from pipeline.runner import take_screenshots, _tg_send_photos, _run_output_name
                        store = get_store()
                        run = store.load(run_id)
                        if not run:
                            await cb.answer("❌ 找不到此 run")
                            continue
                        steps = run.config_dict.get("steps", [])
                        step_idx = run.current_step
                        step_name = steps[step_idx]["name"] if step_idx < len(steps) else "unknown"
                        await cb.answer("📸 正在截圖…")
                        # 用 run-scoped 名(<顯示名>/run_<ts>)→ 截圖落進該次 run 的資料夾、跟其他產物同夾;
                        # take_screenshots 回傳實際路徑、_tg_send_photos 用回傳值直接傳、不影響 TG 傳送。
                        ss_paths = take_screenshots(_run_output_name(run), step_name)
                        if ss_paths:
                            await _tg_send_photos(
                                cb.message.chat_id,
                                ss_paths,
                                caption_prefix=f"📸 {run.pipeline_name} / {step_name}",
                            )
                        else:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text="❌ 截圖失敗，請確認後端主機是否有螢幕",
                            )
                    except Exception as e:
                        logger.error(f"Screenshot failed: {e}")
                        try:
                            await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception:
                            pass
                    continue

                # ── HQ 預覽：使用者按「🎨 原版式預覽」→ LibreOffice 轉 PDF → render ──
                # B1 的 docx/pptx 只抽文字，版式看不到；此按鈕用 LibreOffice 轉出真版式
                # 時間開銷 5-10s / 檔案，所以做成按鈕觸發、不自動跑
                if action == "preview_hq":
                    logger.info(f"Telegram: 原版式預覽 for run {run_id}")
                    try:
                        from pipeline.store import get_store
                        from pipeline.models import PipelineConfig
                        from pipeline.runner import _find_prev_output_file
                        from pipeline.file_preview import _render_via_libreoffice, _libreoffice_binary
                        store = get_store()
                        run = store.load(run_id)
                        if not run:
                            await cb.answer("❌ 找不到此 run")
                            continue
                        if not _libreoffice_binary():
                            await cb.answer("⚠️ 未安裝 LibreOffice")
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text=(
                                    "❌ 原版式預覽需要 LibreOffice，但本機未安裝。\n"
                                    "下載：https://libreoffice.org（免費，~500MB）\n"
                                    "裝完不用改任何設定，系統會自動偵測。"
                                ),
                            )
                            continue
                        config = PipelineConfig.from_dict(run.config_dict)
                        prev_file = _find_prev_output_file(run, config)
                        if not prev_file:
                            await cb.answer("⚠️ 找不到上一步輸出檔")
                            continue
                        await cb.answer("🎨 LibreOffice 轉檔中，約 5-10 秒…")
                        # 在 executor 跑（轉檔 CPU 重，避免 block poll loop）
                        import asyncio as _a
                        from pathlib import Path as _P
                        preview_paths = await _a.get_event_loop().run_in_executor(
                            None,
                            lambda fp=prev_file: _render_via_libreoffice(_P(fp), _P(fp).parent),
                        )
                        if preview_paths:
                            from pipeline.runner import _tg_send_photos
                            await _tg_send_photos(
                                cb.message.chat_id,
                                preview_paths,
                                caption_prefix=f"🎨 原版式預覽：{_P(prev_file).name}",
                            )
                        else:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text="❌ LibreOffice 轉檔後沒有產生可預覽的頁面",
                            )
                    except Exception as e:
                        logger.error(f"preview_hq failed: {e}")
                        try:
                            await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception:
                            pass
                        try:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text=f"❌ 原版式預覽失敗：{str(e)[:300]}",
                            )
                        except Exception:
                            pass
                    continue

                # ── 取上一步輸出檔（pipe_prev_output）──
                # 人工確認 keyboard 的「📎 上一步輸出」按鈕；不論 send_prev_output 是否開都可用
                if action == "prev_output":
                    logger.info(f"Telegram: 取上一步輸出 for run {run_id}")
                    try:
                        from pipeline.store import get_store
                        from pipeline.models import PipelineConfig
                        from pipeline.runner import _send_step_output_to_tg, _run_output_name
                        store = get_store()
                        run = store.load(run_id)
                        if not run:
                            await cb.answer("❌ 找不到此 run")
                            continue
                        config = PipelineConfig.from_dict(run.config_dict)
                        # 回呼重建 config 不帶 run-scoping → 補上,讓 actual_output_path 缺席時的
                        # fallback 也指向本次執行的 run_<ts>/ 子夾(送檔本身優先用 actual_output_path)
                        config.name = _run_output_name(run)
                        # 跳過連續 human_confirm 找上一個可執行步驟（跟 auto-send 邏輯一致）
                        idx = run.current_step - 1
                        while idx >= 0 and config.steps[idx].human_confirm:
                            idx -= 1
                        if idx < 0:
                            await cb.answer("⚠ 沒有上一步")
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text="⚠ 此節點是第一步、沒有上一步輸出可取",
                            )
                            continue
                        prev_step = config.steps[idx]
                        # 從 run.step_results 找對應的 StepResult、給 actual_output_path
                        prev_result = next((sr for sr in run.step_results if sr.step_index == idx), None)
                        await cb.answer("📎 取得中…")
                        ok, msg = await _send_step_output_to_tg(
                            cb.message.chat_id, prev_step,
                            step_label=f"步驟 {idx+1}：{prev_step.name}",
                            workflow_name=config.name,
                            logger=logger,
                            step_result=prev_result,
                        )
                        if not ok:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id, text=f"⚠ {msg}",
                            )
                    except Exception as e:
                        logger.error(f"prev_output failed: {e}")
                        try: await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception: pass
                    continue

                # ── 列出所有步驟讓使用者挑要取哪一步輸出（pipe_select_step）──
                # 點下去 bot 回一個新訊息、含每步的按鈕；按按鈕觸發 pipe_step_output:{run_id}:{idx}
                if action == "select_step":
                    logger.info(f"Telegram: 列出步驟選單 for run {run_id}")
                    try:
                        from pipeline.store import get_store
                        from pipeline.models import PipelineConfig
                        from pipeline.runner import _resolve_step_output_for_tg, _run_output_name
                        store = get_store()
                        run = store.load(run_id)
                        if not run:
                            await cb.answer("❌ 找不到此 run")
                            continue
                        config = PipelineConfig.from_dict(run.config_dict)
                        config.name = _run_output_name(run)  # 回呼補 run-scoping(見 prev_output)
                        # 列「可能有輸出」的步驟：
                        #   - 明確設 output.path（任何節點類型）
                        #   - 節點類型有 default rule（outlook / web_crawler）
                        #   - skill_mode / 一般 script（會寫檔到 working_dir）
                        # 排除：human_confirm / visual_validation / computer_use（不寫檔）
                        from pipeline.runner import _step_default_output_path
                        # 預先把 step_results 做成 idx → StepResult map，給每步解析時用
                        sr_by_idx = {sr.step_index: sr for sr in run.step_results}
                        listed: list[tuple[int, str, str]] = []  # (idx, label, status_emoji)
                        for i, st in enumerate(config.steps):
                            has_explicit = bool(st.output and st.output.path)
                            has_default = bool(_step_default_output_path(st, config.name))
                            could_produce = bool(
                                getattr(st, "skill_mode", False)
                                or (not getattr(st, "human_confirm", False)
                                    and not getattr(st, "visual_validation", False)
                                    and not getattr(st, "computer_use", False)
                                    and getattr(st, "batch", ""))
                            )
                            sr_i = sr_by_idx.get(i)
                            has_actual = bool(getattr(sr_i, "actual_output_path", "") if sr_i else "")
                            if not has_explicit and not has_default and not could_produce and not has_actual:
                                continue  # 該節點本就沒輸出概念
                            fp, _disp, err = _resolve_step_output_for_tg(
                                st, workflow_name=config.name, logger=logger,
                                step_result=sr_i,
                            )
                            emoji = "✅" if fp else "⚠"
                            label = f"{emoji} {i+1}. {st.name}"
                            # callback_data 上限 64 bytes、保險裁短 label
                            if len(label) > 50:
                                label = label[:47] + "…"
                            listed.append((i, label, emoji))

                        if not listed:
                            await cb.answer("⚠ 沒有任何步驟有可傳的輸出")
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id,
                                text="⚠ 此工作流沒有任何步驟有可傳的輸出檔（檢查 step.output.path）",
                            )
                            continue

                        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                        rows = [[InlineKeyboardButton(label, callback_data=f"pipe_step_output:{run_id}:{i}")]
                                for i, label, _ in listed]
                        rows.append([InlineKeyboardButton("✕ 取消", callback_data=f"pipe_cancel_select:{run_id}")])
                        await cb.answer()
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text=("📂 <b>選擇要取得哪一步的輸出</b>\n\n"
                                  "✅ = 檔案準備好可傳；⚠ = 設了 output.path 但檔案不存在 / 太大"),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(rows),
                        )
                    except Exception as e:
                        logger.error(f"select_step failed: {e}")
                        try: await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception: pass
                    continue

                # ── 使用者從步驟選單挑了某步、傳該步輸出（pipe_step_output）──
                if action == "step_output":
                    try:
                        target_idx = int(extra) if extra else -1
                    except Exception:
                        await cb.answer("❌ 步驟索引錯誤")
                        continue
                    logger.info(f"Telegram: 取步驟 #{target_idx} 輸出 for run {run_id}")
                    try:
                        from pipeline.store import get_store
                        from pipeline.models import PipelineConfig
                        from pipeline.runner import _send_step_output_to_tg, _run_output_name
                        store = get_store()
                        run = store.load(run_id)
                        if not run:
                            await cb.answer("❌ 找不到此 run")
                            continue
                        config = PipelineConfig.from_dict(run.config_dict)
                        config.name = _run_output_name(run)  # 回呼補 run-scoping(見 prev_output)
                        if target_idx < 0 or target_idx >= len(config.steps):
                            await cb.answer("❌ 步驟索引超出範圍")
                            continue
                        st = config.steps[target_idx]
                        sr_target = next((sr for sr in run.step_results if sr.step_index == target_idx), None)
                        await cb.answer("📎 取得中…")
                        ok, msg = await _send_step_output_to_tg(
                            cb.message.chat_id, st,
                            step_label=f"步驟 {target_idx+1}：{st.name}",
                            workflow_name=config.name,
                            logger=logger,
                            step_result=sr_target,
                        )
                        if not ok:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id, text=f"⚠ {msg}",
                            )
                    except Exception as e:
                        logger.error(f"step_output failed: {e}")
                        try: await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception: pass
                    continue

                # ── 取消選擇步驟（pipe_cancel_select）──
                if action == "cancel_select":
                    try:
                        await cb.answer()
                        # 把選單訊息刪掉、避免殘留
                        await cb.message.delete()
                    except Exception:
                        pass
                    continue

                # ── 自我修復成功 → 把修好的 YAML 存回工作流（pipe_heal_writeback）──
                # 對齊 web 完成卡片的「存回工作流」,讓遠端使用者也能拍板。邏輯同
                # main.py 的 /heal-writeback endpoint:把 run.config_dict(已含修好的 YAML)寫回。
                if action == "heal_writeback":
                    try:
                        from pipeline.store import get_store
                        from db import update_workflow
                        import yaml as _yaml
                        run = get_store().load(run_id)
                        if not run or not run.workflow_id:
                            await cb.answer("❌ 找不到 run 或無關聯工作流")
                            continue
                        clean = {k: v for k, v in (run.config_dict or {}).items() if not k.startswith("_")}
                        yaml_str = _yaml.safe_dump(clean, allow_unicode=True, sort_keys=False)
                        patch = {"yaml": yaml_str}
                        try:
                            from yaml_to_canvas import yaml_to_canvas
                            _cv = yaml_to_canvas(yaml_str)
                            if _cv:
                                patch["canvas"] = _cv
                        except Exception:
                            pass
                        wf = update_workflow(run.workflow_id, patch)
                        # 與 main.py /heal-writeback 一致:寫回 YAML 同時落地延遲 recipe，
                        # workflow batch 與 recipe task_hash 才一致，下次跑 0 成本命中。
                        recipes_saved = 0
                        if run.pending_recipes:
                            from db import save_recipe as _db_save_recipe
                            for r in run.pending_recipes:
                                try:
                                    _db_save_recipe(
                                        r["pipeline_id"], r["step_name"], r["task_hash"],
                                        r["input_fingerprints"], r["output_path"], r["code"],
                                        r["python_version"], r["runtime_sec"],
                                    )
                                    recipes_saved += 1
                                except Exception:
                                    pass
                            run.pending_recipes = []
                            get_store().save(run)
                        await cb.answer("✅ 已存回")
                        _recipe_note = f"\n📦 同時存下 {recipes_saved} 筆 recipe,下次跑可 0 成本重播。" if recipes_saved else ""
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text=f"💾 已把修好的版本存回工作流「{(wf or {}).get('name', '')}」,下次跑同工作流不會再踩同樣的錯。{_recipe_note}",
                        )
                    except Exception as e:
                        logger.error(f"heal_writeback failed: {e}")
                        try: await cb.answer(f"❌ {str(e)[:150]}")
                        except Exception: pass
                    continue

                # ── 自我修復成功但選擇不存回（pipe_heal_dismiss）──
                if action == "heal_dismiss":
                    try:
                        await cb.answer("好的")
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text="👌 這次的修正只用於本次執行,工作流存檔維持原樣。",
                        )
                    except Exception:
                        pass
                    continue

                # ── 遠端遙控：從 TG 啟動工作流 ──
                if action in ("start_wf", "force_start_wf"):
                    # 必須通過授權檢查
                    if not _is_remote_control_authorized(cb.message.chat_id):
                        await cb.answer("❌ 未授權")
                        continue
                    wf_id = run_id  # callback_data parsed slot 用來放 wf_id
                    if not wf_id:
                        await cb.answer("❌ 無效的工作流 ID")
                        continue
                    force = (action == "force_start_wf")
                    try:
                        await cb.answer("⏳ 啟動中…" if force else "⏳ 檢查中…")
                        # 強制啟動時把警告訊息刪掉，避免畫面留警告
                        if force:
                            try:
                                await cb.message.delete()
                            except Exception:
                                pass
                        await _start_workflow_from_tg(cb.message.chat_id, wf_id, force=force)
                    except Exception as e:
                        logger.error(f"{action} failed: {e}", exc_info=True)
                        try:
                            await cb.answer(f"❌ {str(e)[:50]}")
                        except Exception:
                            pass
                    continue

                # ── ask_user 按選項回答 ──
                if action == "answer":
                    # extra 是 option index
                    try:
                        opt_idx = int(extra)
                    except Exception:
                        await cb.answer("❌ 選項索引錯誤")
                        continue
                    # 從 run 狀態取出原 options
                    from pipeline.store import get_store
                    import json as _json
                    store = get_store()
                    run = store.load(run_id)
                    if not run or run.awaiting_type != "ask_user":
                        await cb.answer("⚠️ 已非等待狀態")
                        continue
                    try:
                        meta = _json.loads(run.awaiting_suggestion or "{}")
                        options = meta.get("options") or []
                    except Exception:
                        options = []
                    if opt_idx < 0 or opt_idx >= len(options):
                        await cb.answer("❌ 選項索引越界")
                        continue
                    chosen = str(options[opt_idx])
                    logger.info(f"Telegram: ask_user 選項 {chosen} for run {run_id}")
                    try:
                        from pipeline.runner import resume_pipeline
                        msg = await resume_pipeline(run_id, "answer", hint=chosen)
                        await cb.answer(f"已選：{chosen[:50]}")
                        try:
                            await cb.edit_message_text(
                                text=(cb.message.text or "") + f"\n\n✅ 已選擇：{chosen}",
                            )
                        except Exception:
                            pass
                    except Exception as e:
                        await cb.answer(f"❌ {str(e)[:150]}")
                    continue

                # ── ask_user 自由輸入：設定等待狀態，改走文字訊息 ──
                if action == "answer_free":
                    logger.info(f"Telegram: 等待 ask_user 自由輸入 for run {run_id}")
                    _pending_answers[cb.message.chat_id] = run_id
                    await cb.answer("請輸入答案")
                    await _bot_instance.send_message(
                        chat_id=cb.message.chat_id,
                        text=(
                            "✍ <b>請輸入你的答案</b>\n\n"
                            "直接回覆文字訊息即可。AI 會根據你的回答繼續任務。"
                        ),
                        parse_mode="HTML",
                    )
                    continue

                # ── 補充指示：設定等待狀態 ──
                if action == "hint":
                    logger.info(f"Telegram: 等待補充指示 for run {run_id}")
                    _pending_hints[cb.message.chat_id] = run_id
                    await cb.answer("請輸入補充指示")
                    await _bot_instance.send_message(
                        chat_id=cb.message.chat_id,
                        text=(
                            "💬 <b>請輸入補充指示</b>\n\n"
                            "AI 會根據你的指示重新嘗試此步驟。\n"
                            "例如：「改用 selenium」「檢查 CSS selector 是否正確」「用另一個 API」"
                        ),
                        parse_mode="HTML",
                    )
                    continue

                # ── 缺套件:允許安裝單一套件 ──
                # callback_data 格式 pipe_install_dep:{run_id}:{pkg_name}
                if action == "install_dep":
                    pkg_name = extra
                    if not pkg_name:
                        await cb.answer("❌ 缺套件名")
                        continue
                    logger.info(f"Telegram: 允許安裝套件 {pkg_name} for run {run_id}")
                    await cb.answer(f"⏳ 正在安裝 {pkg_name}…")
                    try:
                        from pipeline.runner import resume_pipeline
                        msg = await resume_pipeline(run_id, "install_dep", hint=pkg_name)
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id, text=msg[:600],
                        )
                    except Exception as e:
                        logger.error(f"install_dep failed: {e}", exc_info=True)
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text=f"❌ 安裝失敗：{str(e)[:300]}",
                        )
                    continue

                # ── ask_mode 敏感命令授權:允許/拒絕/改任務 ──
                if action in ("approve_cmd", "deny_cmd", "hint_cmd"):
                    _map = {
                        "approve_cmd": "approve_command",
                        "deny_cmd":    "deny_command",
                        "hint_cmd":    "hint_command",
                    }
                    api_dec = _map[action]
                    logger.info(f"Telegram: command_approval={api_dec} for run {run_id}")
                    await cb.answer({"approve_cmd":"✅ 已允許", "deny_cmd":"❌ 已拒絕", "hint_cmd":"💬 改任務"}[action])
                    try:
                        from pipeline.runner import resume_pipeline
                        msg = await resume_pipeline(run_id, api_dec)
                        await _bot_instance.send_message(chat_id=cb.message.chat_id, text=msg[:300])
                    except Exception as e:
                        logger.error(f"{action} failed: {e}", exc_info=True)
                        await _bot_instance.send_message(chat_id=cb.message.chat_id, text=f"❌ {str(e)[:200]}")
                    continue

                # ── 缺套件:允許全部安裝 ──
                if action == "install_all":
                    logger.info(f"Telegram: 允許全部安裝 for run {run_id}")
                    await cb.answer("⏳ 全部安裝中…")
                    try:
                        from pipeline.store import get_store as _gs
                        run = _gs().load(run_id)
                        if not run:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id, text="❌ 找不到 run",
                            )
                            continue
                        import json as _json
                        meta = _json.loads(run.awaiting_suggestion or "{}")
                        pkgs = meta.get("packages") or []
                        if not pkgs:
                            await _bot_instance.send_message(
                                chat_id=cb.message.chat_id, text="❌ 找不到套件清單",
                            )
                            continue
                        from pipeline.runner import resume_pipeline
                        msg = await resume_pipeline(run_id, "install_dep", hint=",".join(pkgs))
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id, text=msg[:600],
                        )
                    except Exception as e:
                        logger.error(f"install_all failed: {e}", exc_info=True)
                        await _bot_instance.send_message(
                            chat_id=cb.message.chat_id,
                            text=f"❌ 全部安裝失敗:{str(e)[:300]}",
                        )
                    continue

                if action not in ("retry", "skip", "abort", "continue", "redo_prev", "self_heal_now"):
                    await cb.answer("❓ 未知操作")
                    continue

                logger.info(f"Telegram callback: {action} for run {run_id}")

                try:
                    from pipeline.runner import resume_pipeline
                    msg = await resume_pipeline(run_id, action)
                    await cb.answer(msg[:200])
                    # 更新原訊息，標記已處理
                    action_labels = {
                        "retry": "🔄 已選擇重試",
                        "skip": "⏩ 已選擇跳過此步",
                        "abort": "🛑 已選擇中止",
                        "continue": "✅ 已確認繼續",
                        "redo_prev": "↩ 已選擇重做上一步",
                        "self_heal_now": "🔧 已交給 AI 試修",
                    }
                    try:
                        original_text = cb.message.text or ""
                        await cb.edit_message_text(
                            text=original_text + f"\n\n{action_labels.get(action, action)}",
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(f"Resume failed: {e}")
                    try:
                        await cb.answer(f"❌ {str(e)[:150]}")
                    except Exception:
                        pass

        except asyncio.CancelledError:
            logger.info("Telegram polling stopped")
            if _bot_instance:
                try:
                    await _bot_instance.close()
                except Exception:
                    pass
            break
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"Telegram flood control, waiting {wait}s")
            await asyncio.sleep(wait)
        except Conflict:
            # 409 Conflict = 有別人（同機別 backend / 別台機器的 bot）在 poll 同一 token。
            # 不再悶頭重試：大聲 log、等久一點（30s），避免跟對方亂搶亂吃 callback。
            logger.warning(
                "Telegram 409 Conflict — 另一個 bot 實例正在 poll 同一 token。"
                " 這代表有別的 backend（本機或其他機器）在用同一個 token，"
                " 會造成按鈕 callback 被亂搶。請確認只開一個 backend，或為每個版本用不同 bot token。"
            )
            await asyncio.sleep(30)
        except (TimedOut, NetworkError):
            # 正常的 long-poll 超時或網路問題
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Telegram poll error: {e}")
            await asyncio.sleep(10)


_poll_task = None


async def start_polling():
    """啟動 Telegram callback polling（背景 task）
    啟動前先試著拿機器級 lock；拿不到代表已有實例在 poll，本實例就不啟 task
    （避免同機多 backend 互搶 Telegram getUpdates session）。
    """
    global _poll_task
    if _poll_task and not _poll_task.done():
        return
    if not _try_acquire_lock():
        return  # 另一實例持有 — 本實例只做 outbound 通知
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info("Telegram callback polling 已啟動")


async def stop_polling():
    """停止 polling"""
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None
    _release_lock()
