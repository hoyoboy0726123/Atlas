"""Phase A.1 — Sandbox tools wrapped as LangChain `@tool` for native function calling.

每個 tool 用 closure 捕捉 runtime context(cwd / run_id / logger / tool_timeout / counters),
這樣 LLM 看到的 schema 只剩它要傳的「有意義參數」、不被 Atlas 內部狀態污染。

設計重點:
- 每次 subagent 啟動呼叫 `build_subagent_tools(...)` 動態建立一組 wrapper
- wrapper 內部委派給既有 `_execute_skill_tool` / `_wait_for_ask_user`、不重寫業務邏輯
- 只 export 在 `allowed_tool_names` 內的 tool(role-based 白名單)
- 跟既有 SKILL loop 共用 sandbox 路由、不會繞過 NODE_PATH 偵測 / silent fallback warning 等

A.1.1 範圍(本檔):
- run_python / run_shell / read_file / web_search / ask_user / done(6 個)
- view_image 等 A.1.2(multimodal、回傳格式特殊)

A.1.2 將會用這份 tool list 餵給 LangChain `llm.bind_tools(...)`,subagent_runner.py 改用 native FC。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.tools import tool


# ============================================================
# tool factory — 為一個 subagent 動態建一組 wrapper
# ============================================================
def build_subagent_tools(
    cwd: Optional[str],
    run_id: str,
    logger: logging.Logger,
    tool_timeout: int,
    allowed_tool_names: set[str],
    step_name: str = "",
    web_search_counter: Optional[dict] = None,
) -> list:
    """為一個 subagent 啟動動態建立 @tool wrapper list。

    Args:
        cwd: workflow 工作目錄(傳給 sandbox tool 當 cwd)
        run_id: pipeline run id(給 ask_user / process tracking 用)
        logger: per-step logger
        tool_timeout: 單次 tool 上限秒數(從 step.timeout 推導)
        allowed_tool_names: role-based 白名單(只回這 set 內的 tool)
        step_name: log 標識
        web_search_counter: mutable {"count": int}、跨輪累計、超 WEB_SEARCH_MAX_PER_STEP 拒絕

    Returns:
        list[BaseTool] — 可以直接 llm.bind_tools(返回值)
    """
    # 延後 import 避免循環依賴(executor 跟本 module 互相 import)
    from pipeline.executor import (
        _execute_skill_tool,
        _wait_for_ask_user,
        WEB_SEARCH_MAX_PER_STEP,
    )

    if web_search_counter is None:
        web_search_counter = {"count": 0}

    log = logger

    # ─── run_python ─────────────────────────────────────────────
    @tool
    def run_python(code: str) -> str:
        """執行 Python 程式碼(在 sandbox 容器內、Linux 環境)。

        用於資料處理、檔案讀寫、生成圖表、產出 Office 檔(python-pptx / openpyxl / python-docx)。
        路徑用絕對路徑(sandbox 內 /mnt/<drive>/...)。
        Args:
            code: 要執行的 Python 程式碼(完整可獨立執行)
        Returns:
            stdout + stderr(如有)+ exit code(失敗時)
        """
        return _execute_skill_tool(
            "run_python", code,
            cwd=cwd, run_id=run_id, logger=log,
            tool_timeout=tool_timeout,
        )

    # ─── run_shell ──────────────────────────────────────────────
    @tool
    def run_shell(command: str) -> str:
        """執行 shell command(在 sandbox 容器內、bash)。

        用於跑 `node script.js`、`ls`、`cat`、`find`、`grep` 等 Unix 工具。
        路徑用 Linux 格式 /mnt/<drive>/...。
        ⚠ 沙盒不可用時會 fallback 到 Windows host(cmd.exe)、tool result 會有警告前綴提示路徑切換。
        Args:
            command: shell 命令
        Returns:
            stdout + stderr + exit code
        """
        return _execute_skill_tool(
            "run_shell", command,
            cwd=cwd, run_id=run_id, logger=log,
            tool_timeout=tool_timeout,
        )

    # ─── read_file ──────────────────────────────────────────────
    @tool
    def read_file(path: str, offset: int = 0, limit: int = 100) -> str:
        """讀檔案內容(支援分段讀大檔)。

        用於讀上游 step 輸出 / 樣本檔 / 設定檔(餵推理用)、不要用於「驗證自己剛 write 的檔」
        (用 run_python 內 Path.exists() 就行)。
        Args:
            path: 檔案路徑(絕對或相對 cwd)
            offset: 從第 N 行開始讀(預設 0、檔頭)
            limit: 最多讀 N 行(預設 100)
        Returns:
            檔案內容、截斷時會說明完整長度與下一段 offset
        """
        # 重組成既有 _execute_skill_tool 接受的 JSON 格式
        ti = json.dumps({"path": path, "offset": offset, "limit": limit})
        return _execute_skill_tool(
            "read_file", ti,
            cwd=cwd, run_id=run_id, logger=log,
            tool_timeout=tool_timeout,
        )

    # ─── web_search ─────────────────────────────────────────────
    @tool
    def web_search(query: str) -> str:
        """網路搜尋(Tavily API、單 step 上限 5 次)。

        用於找最新資料、新聞、技術文件、市場資訊。
        Args:
            query: 搜尋關鍵字(英文效果通常較好)
        Returns:
            search results(answer + URL + 摘要)、超上限回錯誤訊息
        """
        web_search_counter["count"] += 1
        if web_search_counter["count"] > WEB_SEARCH_MAX_PER_STEP:
            return (
                f"[web_search 錯誤] 本步驟已達搜尋次數上限({WEB_SEARCH_MAX_PER_STEP} 次)、"
                f"請改用已搜尋的資料完成任務或 done(success=false)。"
            )
        return _execute_skill_tool(
            "web_search", query,
            cwd=cwd, run_id=run_id, logger=log,
            tool_timeout=tool_timeout,
        )

    # ─── ask_user ───────────────────────────────────────────────
    # 注意:schema 故意全用 str — Optional[list[str]] 在 Gemini API 對 Gemma 4 等模型
    # 會觸發 silent failure(整個 request 廢、LLM 回空 content + 空 tool_calls)。
    # chat_tools.py 內所有 @tool 也都只用 str / int / bool、無 Optional / list — 對齊。
    # 選項要呈現給使用者時、LLM 自己把選項列在 question 內(例:「A/B/C 哪個?」)。
    @tool
    async def ask_user(
        question: str,
        context: str = "",
    ) -> str:
        """問使用者(pipeline 暫停、Telegram / 前端推問題、等回答)。

        用於高風險動作(覆寫 / 刪除 / 外部 API)、任務歧義(欄位 / 格式 / 路徑)、多選方案。
        Args:
            question: 問題(中文、若有多選請直接寫在問題內,例「A/B/C 哪個?」)
            context: 額外脈絡(資料量、目前狀態等)、選填
        Returns:
            "使用者回答:<答案>" 或逾時錯誤
        """
        answer = await _wait_for_ask_user(
            run_id, question, [], context, log, step_name,
        )
        if answer is None:
            return "[錯誤] 等待使用者回答逾時或被取消、請以合理預設完成或呼叫 done(success=false)。"
        return f"使用者回答:{answer}"

    # ─── write_file ─────────────────────────────────────────────
    # native FC 直接傳結構化 content 參數,天生免掉「LLM 用 Python 三引號包外語碼」與
    # 「JSON 內 escape \u/引號」兩個雷(這正是 text 協議時代設計這工具的原因)。
    @tool
    def write_file(path: str, content: str) -> str:
        """直接把內容寫成檔案(不需用 Python 三引號包、不需 escape)。

        最適合:寫 JS / HTML / CSS / markdown / 設定檔等「已知完整內容」的檔。
        Args:
            path: 絕對路徑(沙盒內 /mnt/<drive>/...,或 Windows 路徑)
            content: 檔案完整內容(原文直接給、可含任意引號/反斜線/中文)
        Returns:
            寫入結果(路徑 + bytes + 行數)
        """
        ti = json.dumps({"path": path, "content": content})
        return _execute_skill_tool(
            "write_file", ti, cwd=cwd, run_id=run_id, logger=log, tool_timeout=tool_timeout,
        )

    # ─── edit_file ──────────────────────────────────────────────
    @tool
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """局部替換檔案內容(不用整檔讀+重寫)。old_text 須在檔內唯一出現(除非 replace_all)。

        Args:
            path: 檔案路徑
            old_text: 要被替換的原文(含空格/換行需完全相符)
            new_text: 替換成的新文字
            replace_all: True = 全部替換(預設 False、只替換唯一一處)
        Returns:
            替換結果
        """
        ti = json.dumps({"path": path, "old_text": old_text, "new_text": new_text, "replace_all": replace_all})
        return _execute_skill_tool(
            "edit_file", ti, cwd=cwd, run_id=run_id, logger=log, tool_timeout=tool_timeout,
        )

    # ─── grep ───────────────────────────────────────────────────
    @tool
    def grep(pattern: str, path: str = ".", glob: str = "", max_results: int = 50) -> str:
        """在檔案內搜尋 regex,回 `檔名:行號:內容`。用於找關鍵字在哪、不用 run_python 自己掃。

        Args:
            pattern: 正規表達式
            path: 搜尋根目錄或單一檔(預設當前 cwd)
            glob: 檔名過濾(例 *.py;留空 = 全部)
            max_results: 最多回幾筆(預設 50)
        Returns:
            符合的行(含 檔名:行號 前綴),或 no match
        """
        ti = json.dumps({"pattern": pattern, "path": path, "glob": glob, "max_results": max_results})
        return _execute_skill_tool(
            "grep", ti, cwd=cwd, run_id=run_id, logger=log, tool_timeout=tool_timeout,
        )

    # ─── glob ───────────────────────────────────────────────────
    @tool
    def glob(pattern: str, path: str = ".", max_results: int = 100) -> str:
        """依檔名樣式列檔(例 *.csv、**/*.xlsx)。用於探查有哪些檔、不用 run_python 自己列。

        Args:
            pattern: 檔名樣式(例 *.py、**/*.csv)
            path: 搜尋根目錄(預設當前 cwd)
            max_results: 最多回幾筆(預設 100)
        Returns:
            符合的檔案清單(相對路徑 + 大小)
        """
        ti = json.dumps({"pattern": pattern, "path": path, "max_results": max_results})
        return _execute_skill_tool(
            "glob", ti, cwd=cwd, run_id=run_id, logger=log, tool_timeout=tool_timeout,
        )

    # ─── view_image ─────────────────────────────────────────────
    # native loop(executor 的 SKILL 迴圈)會特判 tc_name=="view_image" 走多模態注入,
    # 故這裡只回 sentinel(實際讀圖+塞 image_url 由 loop 處理)。
    @tool
    def view_image(path: str) -> str:
        """查看圖片(png/jpg/gif/webp/bmp,上限 20MB)。用於驗證產出的圖表 / 從圖擷取資訊。

        Args:
            path: 圖片路徑
        Returns:
            圖片內容(由系統以多模態方式餵給視覺模型)
        """
        return _execute_skill_tool(
            "view_image", path, cwd=cwd, run_id=run_id, logger=log, tool_timeout=tool_timeout,
        )

    # ─── export_var ─────────────────────────────────────────────
    # schema 全用 str(避免 Optional/list 觸發 Gemma silent failure、對齊其它 tool)。
    # value 一律收字串;下游 condition 用 `| int` / `==` 等 Jinja filter 自行轉型。
    @tool
    def export_var(name: str, value: str) -> str:
        """把這步算好的一個具名值傳給下游節點(尤其 condition 條件節點)。

        用於:這步算出一個數字 / 字串、下游 condition 要拿它判斷分支時。
        呼叫後下游用 {{ steps.<本步驟名>.output.<name> }} 引用。
        Args:
            name: 變數名(英文、下游引用用,例 "number" / "score")
            value: 值(數字也用字串給,例 "84";下游可用 `| int` 轉回數字)
        Returns:
            匯出結果說明
        """
        ti = json.dumps({"name": name, "value": value})
        return _execute_skill_tool(
            "export_var", ti,
            cwd=cwd, run_id=run_id, logger=log,
            tool_timeout=tool_timeout,
        )

    # ─── done(sentinel — subagent loop 看 tool_calls 內名為 done 就結束)─────────
    @tool
    def done(
        success: bool = True,
        summary: str = "",
        error: str = "",
    ) -> str:
        """任務完成、結束 subagent loop。

        宣告任務結果。**呼叫 done 之前必須真實驗證**:
        - 上一個 tool 必須是 run_python(寫檔的證據)
        - 該 tool 結果含 `Path(output).exists()` 真實 print 為 True、且 size 合理
        - 不符 = orchestrator 會拒收、燒你重做的 token
        Args:
            success: 任務是否成功(預設 True)
            summary: 結果摘要(success=true 時必填、告訴使用者做了什麼)
            error: 失敗原因(success=false 時必填)
        Returns:
            "__DONE_SENTINEL__" — subagent loop 收到後結束
        """
        # subagent loop 不會真執行這個 return、它看 tool_calls[i].name == "done" 就 break
        return "__DONE_SENTINEL__"

    # ─── 過濾白名單 + 回 list ────────────────────────────────────
    all_tools = {
        "run_python": run_python,
        "run_shell": run_shell,
        "read_file": read_file,
        "web_search": web_search,
        "write_file": write_file,
        "edit_file": edit_file,
        "grep": grep,
        "glob": glob,
        "view_image": view_image,
        "ask_user": ask_user,
        "export_var": export_var,
        "done": done,
    }
    selected = [t for name, t in all_tools.items() if name in allowed_tool_names]
    if log:
        log.debug(
            f"[{step_name or 'subagent'}] build_subagent_tools 完成、"
            f"白名單 {sorted(allowed_tool_names)} → 啟用 {[t.name for t in selected]}"
        )
    return selected
