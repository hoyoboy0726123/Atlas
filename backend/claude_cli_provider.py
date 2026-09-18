"""Claude / Codex「訂閱帳號 CLI」當 Atlas 的模型 provider。

概念(見 docs 參考 SUBSCRIPTION_BRAINS.md):不是 API 金鑰按 token 計費,而是
spawn 官方 `claude` CLI 的 headless(-p)模式 → 吃使用者的 Pro/Max **訂閱額度**。
因為是從 Atlas *內部*的 build_llm() 走,會吃到完整 `_PIPELINE_SYSTEM_BASE` 系統提示
注入 + app 全部上下文(這正是 MCP 外掛做不到的)。

合規紅線:單一使用者、本機使用;絕不自己讀/重放 OAuth token(登入是 CLI 的事);
把「你的訂閱」路由給他人用違反 ToS。

實作為 LangChain BaseChatModel 子類 → 直接塞進 llm_factory.build_llm(),
其餘 Atlas 程式碼(invoke_with_streaming 的 .astream)完全不用改。

⚠️ CLI 大腦沒有原生 function calling → 用它當 provider 時,skill/subagent 迴圈要走
「文字協議」(SUBAGENT_LOOP_MODE=text)。llm_factory 會據 provider 自動切(見該檔)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

# 走訂閱時要從子行程環境剝掉的變數(否則 CLI 會優先用 API 金鑰計費 → 使用者以為免費實際燒錢)
_STRIP_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)

_DISALLOWED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch,NotebookEdit,Agent,Task,TodoWrite"

# ⚠️ 關鍵:完整 `claude` CLI 本身是個 agentic harness(有自己的工具與「Claude Code」身分)。
# 直接餵 Atlas 的 skill/subagent 提示,模型會把「用工具」理解成用它『自己的』工具(Bash/Write…),
# 但我們用 --disallowedTools 全禁 → 它會跳出角色、meta 抱怨「這個 harness 沒有 run_python 工具/
# 需要批准」→ 破壞文字協議。用這段 clamp 壓住:你只是嵌在 Atlas 裡的純文字引擎,只輸出協議文字。
_PROTOCOL_CLAMP = (
    "【最高優先·角色鎖定】你是被嵌入在另一個系統「Atlas」內部的『純文字生成引擎』,不是互動式 agent。\n"
    "- 你沒有任何屬於你自己的可執行工具(Bash / Write / Edit / Read / run_python 都不是你的工具、也不需要你去執行)。\n"
    "- 你唯一的工作:完全依照下方系統提示教你的格式,輸出『純文字』回覆。若下方要求用 `<tool>名稱</tool>` 之類的文字協議,\n"
    "  你就直接把那段協議文字寫出來即可 —— Atlas 會解析你輸出的文字、代替你執行、再把結果回傳給你,你只要繼續輸出下一則協議文字。\n"
    "- 絕對禁止:說「這個 harness 沒有 X 工具」「工具不存在」「需要批准/approval」「我用 ToolSearch 查過」這類話;\n"
    "  也不要嘗試真的去執行任何東西、不要提到你的執行環境。你看到的『工具』都是要你用文字協議格式輸出的、由 Atlas 執行的,不是你的。\n"
    "- 把注意力完全放在完成使用者的任務,並嚴格用下方規定的文字協議格式回覆。"
)

# 訂閱有共享 rate window:多個 skill/subagent 同時 spawn CLI 會自己 DDoS 自己的額度。
# 用 semaphore 限併發(可用 env 覆寫)。預設 2(參考另一專案實戰值)。
_MAX_CONCURRENT = int(os.environ.get("CLAUDE_CLI_MAX_CONCURRENT", "2") or "2")
_cli_semaphore = threading.Semaphore(_MAX_CONCURRENT)


def _kill_tree(pid: int) -> None:
    """殺整棵行程樹(CLI 會生 MCP/sandbox 孫行程,握著 pipe 不放 → 逾時後永久卡)。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


# 只快取「找到」的路徑。⚠️ 不要用 @lru_cache 連 None 一起快取 ——
# 使用者照著錯誤訊息去 `npm i -g` 裝好後,設定頁仍會顯示「未安裝」,
# 非得重啟後端不可(實測過的困惑點)。找不到時重掃的成本只是幾次
# Path.exists() + 一次 which,可忽略。
_EXE_CACHE: Optional[str] = None


def resolve_claude_exe() -> Optional[str]:
    """找出可直接 spawn 的 claude 執行檔。

    Windows 上 `npm i -g` 裝的是 claude.cmd shim,execFile/subprocess 不透過 shell 跑不動,
    要找套件內真正的 .exe / cli.js。回傳可執行路徑或 None。
    """
    global _EXE_CACHE
    if _EXE_CACHE and Path(_EXE_CACHE).exists():
        return _EXE_CACHE

    found: Optional[str] = None
    # 1) 套件內已知路徑(Windows 真 exe)
    base = Path(os.path.expanduser("~")) / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code"
    for cand in (base / "bin/claude.exe", base / "cli.js"):
        if cand.exists():
            found = str(cand)
            break
    if not found:
        # 2) which(POSIX 直接可用;Windows 會拿到 .cmd,退而求其次)
        w = shutil.which("claude")
        if w:
            if w.lower().endswith(".cmd"):
                # Windows shim → 嘗試找旁邊套件的真 exe
                guess = Path(w).parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
                found = str(guess) if guess.exists() else w
            else:
                found = w

    _EXE_CACHE = found       # None 不會被「記住」,下次呼叫會重掃
    return found


def is_logged_in() -> bool:
    """只檢查憑證檔『存在』(絕不讀內容),或設了長效 token。

    ⚠️ 刻意不快取:這只是一次 Path.exists(),成本可忽略;而快取會讓使用者
    `claude login` 完之後設定頁仍顯示未登入、非重啟後端不可。
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    cred = Path(os.path.expanduser("~")) / ".claude/.credentials.json"
    return cred.exists()


def availability() -> dict:
    """三態偵測給設定頁用:installed / logged_in / exe 路徑。"""
    exe = resolve_claude_exe()
    return {
        "installed": bool(exe),
        "logged_in": is_logged_in() if exe else False,
        "exe": exe or "",
    }


def _child_env() -> dict:
    env = dict(os.environ)
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


def _messages_to_prompt(messages: list[BaseMessage]) -> tuple[str, str]:
    """把 LangChain messages 拆成 (system, user_transcript)。

    - 所有 SystemMessage → 合併成 system(走 --append-system-prompt)
    - 其餘(Human/AI/Tool)→ 序列化成一段對話 transcript 當 stdin user prompt
      (CLI 是單發、無多輪 state,把歷史攤進 prompt,讓模型接著回下一則 Assistant)
    """
    sys_parts: list[str] = []
    convo: list[str] = []
    for m in messages:
        role = getattr(m, "type", "") or m.__class__.__name__.lower()
        content = m.content
        if isinstance(content, list):  # multimodal blocks → 取 text
            _texts: list[str] = []
            _dropped = 0
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") in ("image_url", "image"):
                        _dropped += 1          # CLI 是純文字橋,圖片過不去
                        continue
                    _texts.append(b.get("text", ""))
                else:
                    _texts.append(str(b))
            content = "".join(_texts)
            # ⚠️ 絕不能「靜默」丟圖 —— 那會讓模型不知道自己沒看到圖,
            # 照樣自信地下判決(實測踩過:VLM 驗證沒看圖就給 verdict)。
            # 明講出來,模型才會回「我看不到圖」而不是編一個答案。
            if _dropped:
                content += (
                    f"\n\n[系統提示:此訊息原本附了 {_dropped} 張圖片,"
                    "但目前的模型是純文字橋接、**看不到任何圖片**。"
                    "→ 不准根據想像描述或判斷圖片內容;"
                    "若這個任務必須看圖才能完成,請直接說明你看不到圖片、無法判斷。]"
                )
        content = str(content or "")
        if role in ("system", "systemmessage"):
            sys_parts.append(content)
        elif role in ("human", "humanmessage"):
            convo.append(f"[使用者]\n{content}")
        elif role in ("ai", "aimessage"):
            convo.append(f"[你先前的回覆]\n{content}")
        elif role in ("tool", "toolmessage"):
            convo.append(f"[工具執行結果]\n{content}")
        else:
            convo.append(content)
    system = "\n\n".join(p for p in sys_parts if p.strip())
    user = "\n\n".join(convo) if convo else "請開始。"
    if convo:
        user += "\n\n[請接著輸出你的下一則回覆]"
    return system, user


class ClaudeCliChat(BaseChatModel):
    """用訂閱版 `claude` CLI(headless -p)當 chat model。
    invoke 走 json 單發;stream 走 stream-json + partial(真逐段串流、首字 ~3s)。"""

    model: str = "sonnet"
    temperature: float = 0.0
    max_output_tokens: int = 16384
    timeout_s: int = 300

    @property
    def _llm_type(self) -> str:
        return "claude_cli"

    # ---- 核心:spawn CLI ----
    def _call_cli(self, messages: list[BaseMessage]) -> tuple[str, dict]:
        exe = resolve_claude_exe()
        if not exe:
            raise RuntimeError("找不到 claude CLI(請先 npm i -g @anthropic-ai/claude-code 並登入)")
        system, user = _messages_to_prompt(messages)
        args = [
            exe, "-p", "--output-format", "json", "--model", self.model,
            "--disallowedTools", _DISALLOWED_TOOLS, "--strict-mcp-config",
            # ⚠️ 只有『小的』clamp 走 argv(system 級、權威)。Atlas 的系統提示
            # (_PIPELINE_SYSTEM_BASE 可上萬字)絕不能走 argv,否則超過 Windows 命令列
            # 長度上限 → WinError 206「檔名或副檔名太長」(助手 chat 一定踩)。大提示改走 stdin。
            "--append-system-prompt", _PROTOCOL_CLAMP,
        ]
        # cli.js 要用 node 跑
        if exe.endswith(".js"):
            args = ["node"] + args
        env = _child_env()
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        # system(大)+ user 一起走 stdin(無長度限制);clamp 已在 argv 當真正的 system。
        stdin_prompt = (
            f"{system}\n\n===== 以下是本次對話 / 任務 =====\n\n{user}" if system else user
        )

        # semaphore 限併發(訂閱共享 rate window)+ 逾時殺整棵行程樹(孫行程握 pipe 不放)
        with _cli_semaphore:
            popen_kw: dict = dict(
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, encoding="utf-8", errors="replace",
            )
            if os.name == "nt":
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(args, **popen_kw)
            try:
                out, err = proc.communicate(input=stdin_prompt, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc.pid)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise RuntimeError(f"claude CLI 逾時(>{self.timeout_s}s)")
        out = out or ""
        proc.stderr_text = err or ""
        parsed = _parse_claude_json(out)
        if parsed is None:
            tail = (getattr(proc, "stderr_text", "") or out or "")[-300:]
            raise RuntimeError(f"claude CLI 回應無法解析:{tail}")
        if parsed.get("is_error"):
            raise RuntimeError(f"claude CLI 回錯:{str(parsed.get('result'))[:300]}")
        text = str(parsed.get("result") or "")
        u = parsed.get("usage") or {}
        usage = {
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "total_tokens": int((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)),
            # 訂閱額度:cache 資訊照收(給 log 看),但這不是「帳單」
            "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
        }
        return text, usage

    def _generate(
        self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any,
    ) -> ChatResult:
        text, usage = self._call_cli(messages)
        msg = AIMessage(content=text, usage_metadata={
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            # 同 _stream:不帶快取數的話下游成本會低估
            "input_token_details": {
                "cache_read": usage.get("cache_read_tokens", 0),
                "cache_creation": usage.get("cache_creation_tokens", 0),
            },
        })
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """真串流:--output-format stream-json --include-partial-messages。

        實測事件格式(2026-07 探測程式驗證):
        - stream_event → event.type=content_block_delta → event.delta.text(逐段文字,首字 ~2.7s)
        - assistant(尾端)= 完整訊息;舊版 CLI 沒有 partial 時當後備、一次 yield 全文
        - result = usage + is_error;system/init、rate_limit_event 等雜訊事件安全忽略
        原本 json 模式整段 17-26s 才一次到達,hero 體感像當機;partial 首字 2.7s。
        """
        exe = resolve_claude_exe()
        if not exe:
            raise RuntimeError("找不到 claude CLI(請先 npm i -g @anthropic-ai/claude-code 並登入)")
        system, user = _messages_to_prompt(messages)
        args = [
            exe, "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--model", self.model,
            "--disallowedTools", _DISALLOWED_TOOLS, "--strict-mcp-config",
            "--append-system-prompt", _PROTOCOL_CLAMP,
        ]
        if exe.endswith(".js"):
            args = ["node"] + args
        env = _child_env()
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        stdin_prompt = (
            f"{system}\n\n===== 以下是本次對話 / 任務 =====\n\n{user}" if system else user
        )
        with _cli_semaphore:
            popen_kw: dict = dict(
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, encoding="utf-8", errors="replace",
            )
            if os.name == "nt":
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(args, **popen_kw)
            # ⚠️ stderr 必須「持續」排空,不能等迴圈跑完才讀:
            # 我們只在下面 for-loop 讀 stdout,若 CLI 往 stderr 寫超過 OS pipe
            # buffer(Windows 約 64KB),子行程會卡在 write、父行程卡在 stdout
            # 讀取 → 死鎖,只能等 watchdog 逾時殺掉(預設 300s)。
            # 用背景 daemon thread 邊跑邊收,順便留著給錯誤訊息用。
            _err_buf: list[str] = []

            def _drain_stderr() -> None:
                try:
                    for _line in proc.stderr:
                        _err_buf.append(_line)
                        if len(_err_buf) > 400:      # 只留尾巴,避免長跑吃記憶體
                            del _err_buf[:200]
                except Exception:
                    pass

            _err_thread = threading.Thread(target=_drain_stderr, daemon=True)
            _err_thread.start()
            # readline 是 blocking → 用 watchdog timer 強制殺整棵樹當總逾時
            _watchdog = threading.Timer(float(self.timeout_s), lambda: _kill_tree(proc.pid))
            _watchdog.daemon = True
            _watchdog.start()
            got_delta = False
            emitted_chars = 0
            is_error = False
            result_text = ""
            usage: dict = {}
            try:
                proc.stdin.write(stdin_prompt)
                proc.stdin.close()
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    ty = ev.get("type")
                    if ty == "stream_event":
                        e = ev.get("event") or {}
                        if e.get("type") == "content_block_delta":
                            t = (e.get("delta") or {}).get("text") or ""
                            if t:
                                got_delta = True
                                emitted_chars += len(t)
                                chunk = ChatGenerationChunk(message=AIMessageChunk(content=t))
                                if run_manager:
                                    run_manager.on_llm_new_token(t, chunk=chunk)
                                yield chunk
                    elif ty == "assistant" and not got_delta:
                        # 舊版 CLI 沒 --include-partial-messages 事件 → 整段當一顆後備
                        txt = "".join(
                            b.get("text") or "" for b in (ev.get("message") or {}).get("content") or []
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if txt:
                            emitted_chars += len(txt)
                            chunk = ChatGenerationChunk(message=AIMessageChunk(content=txt))
                            if run_manager:
                                run_manager.on_llm_new_token(txt, chunk=chunk)
                            yield chunk
                    elif ty == "result":
                        is_error = bool(ev.get("is_error"))
                        result_text = str(ev.get("result") or "")[:300]
                        u = ev.get("usage") or {}
                        usage = {
                            "input_tokens": int(u.get("input_tokens") or 0),
                            "output_tokens": int(u.get("output_tokens") or 0),
                            # 快取欄位一定要收:Anthropic 的 input_tokens 不含快取讀取,
                            # 少了這兩個,成本會低估到只剩零頭(實測 input=6)
                            "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                            "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
                        }
                try:
                    proc.wait(timeout=10)
                except Exception:
                    _kill_tree(proc.pid)
            finally:
                _watchdog.cancel()
                if proc.poll() is None:
                    _kill_tree(proc.pid)
        if emitted_chars == 0:
            # 整場沒吐出任何文字:逾時被殺 / CLI 出錯 → 拋錯讓上層退避重試
            # stderr 由 _drain_stderr 執行緒收在 _err_buf(不能在這裡才 read,
            # 那正是先前會死鎖的寫法)。等它收尾一下再取。
            stderr_tail = ""
            try:
                _err_thread.join(timeout=2.0)
                stderr_tail = ("".join(_err_buf) or "")[-300:]
            except Exception:
                pass
            if is_error:
                raise RuntimeError(f"claude CLI 回錯:{result_text}")
            raise RuntimeError(f"claude CLI 串流無輸出(可能逾時 >{self.timeout_s}s):{stderr_tail}")
        # 收尾 chunk:附 usage(invoke_with_streaming 靠這個記帳)
        # input_token_details 用 langchain 0.3+ 的標準形狀,llm_factory 才收得到快取數
        _cr = usage.get("cache_read_tokens", 0)
        _cc = usage.get("cache_creation_tokens", 0)
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata={
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "input_token_details": {"cache_read": _cr, "cache_creation": _cc},
        }))

    # BaseChatModel 的 async 介面預設會把 _stream/_generate 丟到 thread executor,
    # subprocess.run 是 blocking → 用預設 async 包裝即可(不阻塞 event loop)。


def _parse_claude_json(out: str) -> Optional[dict]:
    """容錯解析:stdout 可能夾前置雜訊,掃描最外層平衡大括號取 JSON 物件。"""
    if not out:
        return None
    out = out.strip()
    try:
        return json.loads(out)
    except Exception:
        pass
    # 掃第一個 '{' 到平衡的 '}'
    start = out.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(out)):
        ch = out[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(out[start:i + 1])
                    except Exception:
                        return None
    return None
