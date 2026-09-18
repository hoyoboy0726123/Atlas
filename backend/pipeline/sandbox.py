"""
Skill 沙盒執行層：把 LLM 生成的 Python / Shell 送進 WSL 內的 Docker
容器 `atlas-sandbox` 執行，隔離 Windows host。

此模組只負責：
  1. 狀態檢查 — WSL / Docker / 容器 是否就緒
  2. 自動復活 — 容器停了試著 start
  3. 路徑翻譯 — Windows `C:\\...` → WSL/容器內 `/mnt/c/...`（同路徑映射）
  4. 執行 + I/O 捕捉 + timeout + 可中止

不負責：
  - 建立容器（`sandbox/setup.sh` 負責，一次性）
  - 決定要不要用沙盒（`executor._execute_skill_tool` 根據 settings 判斷）

使用方式見 `pipeline/executor.py` 的沙盒分支（Stage 3）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── 常數 ───────────────────────────────────────────────────────────
CONTAINER_NAME = "atlas-sandbox"
SANDBOX_TOOL_TIMEOUT = 60  # 秒；對齊 executor.SKILL_TOOL_TIMEOUT

# Docker CLI 呼叫習慣：優先嘗試 plain `docker`（使用者已 `usermod -aG docker` + 重啟 WSL），
# 失敗再 fallback `sudo docker`。快取結果避免每次都試。
_DOCKER_PREFIX_CACHE: dict = {"prefix": None}

# ── 並發護欄(2026-05 加) ────────────────────────────────────────────
# #1 同時 docker exec 上限：避免兩個 workflow + ad-hoc subagent 同時併發跑、
#    讓 docker daemon 變慢、container 內 CPU/RAM 互搶。可用 env var 調(預設 3)
# #2 pip install global lock：兩個 task 同時 pip install 不同 pkg 會撞 dpkg lock /
#    .pyc cache、最易壞。任何 cmd 含 'pip install' 字眼一律序列化。
import os as _os
_SANDBOX_MAX_CONCURRENT = max(1, int(_os.getenv("SANDBOX_MAX_CONCURRENT", "3") or 3))
_SANDBOX_EXEC_SEMA = threading.Semaphore(_SANDBOX_MAX_CONCURRENT)
_PIP_INSTALL_LOCK = threading.Lock()


def _is_pip_install_cmd(cmd: list[str]) -> bool:
    """Detect cmd 內是否含 pip install 操作(序列化用)。
    cmd 通常是 ['docker', 'exec', ..., 'sh', '-c', '<shell-cmd>'] 或 python script。
    sh -c 的 arg 內若有 pip install / pip3 install / python -m pip install / uv pip install
    都算。Python 程式內透過 subprocess 呼 pip 抓不到、那場景少且 LLM 通常會被
    runner 早期 ModuleNotFoundError 攔截、不會走 runtime pip install。"""
    blob = " ".join(str(c) for c in cmd).lower()
    if "pip install" in blob or "pip3 install" in blob or "uv pip install" in blob:
        return True
    # python -m pip install / python3 -m pip install
    if "-m pip" in blob and "install" in blob:
        return True
    return False
_DOCKER_PREFIX_LOCK = threading.Lock()


# ── 路徑翻譯 ───────────────────────────────────────────────────────
_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def windows_to_wsl_path(path: str) -> str:
    """`C:\\Users\\X\\y` → `/mnt/c/Users/X/y`；
    已是 POSIX 或空字串則原樣回傳。"""
    if not path:
        return path
    m = _DRIVE_PATH_RE.match(path)
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def translate_code_paths(code: str) -> str:
    """把 LLM 程式碼裡的 Windows 絕對路徑都換成 WSL/`/mnt/c/...` 形式。
    只針對 r-string 與普通字串裡 `X:\\...` / `X:/...` 的字面值做替換，
    避免誤傷變數名或 URL 等正規字串。"""
    def _sub(m: re.Match) -> str:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\\\", "/").replace("\\", "/")
        return f'"/mnt/{drive}/{rest}"'

    # 匹配 "C:\Users\..." 或 "C:/Users/..." 或 r"C:\..." 的字面值
    pattern = re.compile(r'r?["\']([A-Za-z]):[\\/]([^"\']*)["\']')
    return pattern.sub(_sub, code)


_SHELL_DRIVE_RE = re.compile(r'(?<![A-Za-z])([A-Za-z]):[\\/]([^"\'\s]*)')


def translate_shell_paths(cmd_str: str) -> str:
    """把 shell 字串內的 Windows 絕對路徑(`C:\\path` / `C:/path`)轉成 WSL `/mnt/c/path` 形式。

    LLM 在 sandbox 模式下會把 write_file 回傳的 Windows 路徑直接抄進 run_shell、
    bash 不認 `C:\\`、會把整串當相對檔名拼到 CWD 後面而失敗。這個函式攔截 cmd 字串、
    在送進容器前把所有 drive-letter 路徑轉成 sandbox 看得懂的 `/mnt/<drive>/...`。

    限制:含空格的路徑(`C:\\Program Files\\X`)只轉到第一個空格為止;
    使用者放在 external_projects/ 下的專案通常無空格、實務上影響小。"""
    def _sub(m: re.Match) -> str:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return _SHELL_DRIVE_RE.sub(_sub, cmd_str)


# ── subprocess 輸出解碼:Windows wsl.exe / 其他內建工具,不同情境輸出不同編碼:
#    - 對 terminal 輸出 UTF-16 LE
#    - 對 pipe 輸出 系統本地碼頁 mbcs(中文 Windows = CP950 / Big5)
#    一律當 utf-8 讀會跳「每字夾 □」或「字被 ? 替換」的 mojibake。
#    依序試:UTF-16 LE(BOM 或 sample 含 >5% \x00 byte)→ utf-8 → mbcs。
#    utf-8 解出來 U+FFFD 比例過高 → 改用 mbcs 結果。
def _decode_subprocess_output(b: bytes) -> str:
    if not b:
        return ""
    # 1. UTF-16 LE 偵測:BOM
    if b.startswith(b"\xff\xfe"):
        try:
            return b.decode("utf-16-le", errors="replace").lstrip("﻿")
        except Exception:
            pass
    # 2. UTF-16 LE 偵測:sample 含 >5% \x00 byte
    #    (utf-8 文字輸出基本沒 null byte;UTF-16 LE 只要混到 ASCII 字元必然產生 null)
    sample = b[:64]
    null_in_sample = sum(1 for byte in sample if byte == 0)
    if null_in_sample >= max(1, len(sample) * 0.05):
        try:
            return b.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    # 3. utf-8;若解出來 U+FFFD 比例 >5%(代表大量 byte 無效)→ 改用 mbcs(系統本地碼頁)
    try:
        utf8_decoded = b.decode("utf-8", errors="replace")
        repl_ratio = utf8_decoded.count("�") / max(len(utf8_decoded), 1)
        if repl_ratio > 0.05:
            try:
                return b.decode("mbcs", errors="replace")
            except Exception:
                pass
        return utf8_decoded
    except Exception:
        try:
            return b.decode("mbcs", errors="replace")
        except Exception:
            return repr(b)


# ── wsl 指令呼叫封裝 ───────────────────────────────────────────────
def _run_wsl(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """執行 `wsl.exe <args>`，回傳 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            ["wsl", *args],
            capture_output=True,
            timeout=timeout,
        )
        # wsl.exe 預設輸出 UTF-16 LE(中文 locale 錯誤訊息尤其),用 _decode_subprocess_output 自動偵測
        return proc.returncode, _decode_subprocess_output(proc.stdout), _decode_subprocess_output(proc.stderr)
    except FileNotFoundError:
        return -1, "", "wsl.exe not found — Windows 可能沒裝 WSL"
    except subprocess.TimeoutExpired:
        return -2, "", f"wsl 指令 timeout（>{timeout}s）"
    except Exception as e:
        return -3, "", f"wsl 呼叫例外：{e}"


def _detect_docker_prefix() -> list[str]:
    """決定呼叫 docker 時要不要加 sudo。
    優先試 plain `docker ps`；失敗試 `sudo docker ps`。結果快取。
    用 `docker ps` 不用 `docker info`：前者極快（ms 級），後者會列 daemon 資訊（數百行、慢），
    cold-start 的 WSL 常超過 5s 讓偵測誤判為失敗。"""
    with _DOCKER_PREFIX_LOCK:
        if _DOCKER_PREFIX_CACHE["prefix"] is not None:
            return _DOCKER_PREFIX_CACHE["prefix"]

        # 試 plain docker（前提：使用者已加入 docker group 且 WSL 重啟過）
        # timeout 拉到 15s 容納 WSL cold start（首次 wsl.exe 呼叫可能要 5-10s 起 VM）
        rc, _, _ = _run_wsl(["-e", "docker", "ps", "-q"], timeout=15.0)
        if rc == 0:
            _DOCKER_PREFIX_CACHE["prefix"] = ["docker"]
        else:
            # 試 sudo（使用者需 NOPASSWD 或 cached sudo ticket）
            rc2, _, _ = _run_wsl(["-n", "-e", "sudo", "-n", "docker", "ps", "-q"], timeout=10.0)
            if rc2 == 0:
                _DOCKER_PREFIX_CACHE["prefix"] = ["sudo", "docker"]
            else:
                # 兩種都不行 — 還是記 ["docker"]，check_status 會判失敗讓 UI 顯示 hint
                _DOCKER_PREFIX_CACHE["prefix"] = ["docker"]
        return _DOCKER_PREFIX_CACHE["prefix"]


def _invalidate_docker_prefix_cache() -> None:
    """狀態異常時重新探測（例如使用者剛重啟 WSL）。"""
    with _DOCKER_PREFIX_LOCK:
        _DOCKER_PREFIX_CACHE["prefix"] = None


# ── 狀態檢查 ───────────────────────────────────────────────────────
# 上次成功在容器跑完 docker exec 的時間戳。剛剛成功跑過 = 沙盒鐵定還活著,
# ensure_running 短路跳過慢探測。WSL VM 在連續呼叫之間偶有冷啟動延遲、
# wsl --status 超過 10s timeout、check_status 整套要 20s+ 才回 unhealthy、
# 連續 skill 步驟全部誤 fallback 到 host(明明沙盒 ON、log 卻一直在 host 跑)。
_LAST_HEALTHY_EXEC_TS: float = 0.0
# 60s:給足下一個 skill 工具呼叫間隔。LLM 步驟通常 30-90s,設 60s 不會卡到
# 真的需要重探的情境(容器中途被砍/WSL shutdown),但能擋連續呼叫的雜訊。
_RECENT_EXEC_TTL: float = 60.0


def _mark_sandbox_healthy() -> None:
    """成功跑完一次 docker exec 後喊一聲、把短路 cache 更新。
    讓下一個 skill 工具呼叫不用再花 ~20s 重做慢探測。
    『成功』定義:subprocess 有完整跑完(timeout 不算)— 即使 Python 在容器內
    raise Exception、返回 non-zero rc,也代表容器是通的、沙盒鐵定還活著。"""
    global _LAST_HEALTHY_EXEC_TS
    _LAST_HEALTHY_EXEC_TS = time.time()


_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
# 分別 TTL：
#   健康 → cache 久一點（30s），因為剛確認好幾秒內不用重查，避免每個 skill tool 都多跑 ~1s WSL probe
#   不健康 → cache 短一點（5s），使用者剛修好狀態能快點反映到 UI
# 先前統一 5s 會讓每次 skill 呼叫都重做一次健康檢查，碰到 WSL 冷啟動
# （wsl --status 偶爾超過 5s）就誤判「沒裝 WSL」→ fallback 到 host → 路徑錯
_STATUS_TTL_HEALTHY = 30.0
_STATUS_TTL_UNHEALTHY = 5.0
# wsl --status 超時。5s 在 WSL VM 冷啟動時會超 → 誤判為未安裝。實測 8-10s 覆蓋絕大多數情況
_WSL_STATUS_TIMEOUT = 10.0


def check_status(force_refresh: bool = False) -> dict:
    """沙盒健康檢查。Return:
        {
          "wsl_ok": bool, "wsl_hint": str,
          "docker_ok": bool, "docker_version": str,
          "container_exists": bool, "container_running": bool,
          "ready": bool,           # wsl + docker + container 都綠
          "reasons": list[str],    # 使用者可讀的問題描述
          "hint": str,             # 建議下一步動作
        }
    結果按健康與否套不同 TTL（健康 30s / 不健康 5s）。"""
    now = time.time()
    if not force_refresh and _STATUS_CACHE["data"]:
        ttl = _STATUS_TTL_HEALTHY if _STATUS_CACHE["data"].get("ready") else _STATUS_TTL_UNHEALTHY
        if now - _STATUS_CACHE["ts"] < ttl:
            return _STATUS_CACHE["data"]

    reasons: list[str] = []
    wsl_ok = False
    wsl_timed_out = False  # timeout 要跟「真的沒裝」分開提示，不然 hint 誤導
    docker_ok = False
    docker_version = ""
    container_exists = False
    container_running = False

    # 1. WSL 可用嗎
    rc, out, err = _run_wsl(["--status"], timeout=_WSL_STATUS_TIMEOUT)
    if rc == 0:
        wsl_ok = True
    else:
        # _run_wsl 的 rc 意義：-1=找不到 wsl.exe，-2=timeout，-3=其他例外，>0=WSL 真的回錯
        # 只有 -2 算「瞬時 timeout 可能可重試」，其他都是比較穩定的真實失敗
        if rc == -2:
            wsl_timed_out = True
            reasons.append(f"WSL `--status` 回應逾時（>{_WSL_STATUS_TIMEOUT}s），可能 VM 冷啟動中")
        else:
            reasons.append(f"WSL 無法使用：{err.strip() or f'rc={rc}'}")

    # 2. Docker daemon 可用嗎
    if wsl_ok:
        docker_prefix = _detect_docker_prefix()
        # timeout 拉到 10s：cold-start WSL 首次 wsl.exe 啟 VM 要 5-10s
        rc, out, _ = _run_wsl(["-e", *docker_prefix, "--version"], timeout=10.0)
        if rc == 0 and out.strip():
            docker_ok = True
            docker_version = out.strip().split("\n", 1)[0][:100]
        else:
            reasons.append("Docker Engine 未安裝或無法使用 — 請執行 sandbox/setup_sandbox.bat")

    # 3. 容器狀態
    if docker_ok:
        docker_prefix = _detect_docker_prefix()
        # 用 `docker ps -a --filter name=... --format {{.Status}}` — 最簡單
        rc, out, _ = _run_wsl(
            ["-e", *docker_prefix, "ps", "-a",
             "--filter", f"name=^{CONTAINER_NAME}$",
             "--format", "{{.Status}}"],
            timeout=10.0,
        )
        status_line = out.strip()
        if rc == 0 and status_line:
            container_exists = True
            container_running = status_line.lower().startswith("up ")
            if not container_running:
                reasons.append(f"容器 {CONTAINER_NAME} 存在但已停止（狀態：{status_line[:60]}）")
        else:
            reasons.append(f"容器 {CONTAINER_NAME} 不存在 — 請執行 sandbox/setup_sandbox.bat")

    ready = wsl_ok and docker_ok and container_running
    hint = ""
    if not wsl_ok:
        hint = (
            f"WSL 回應逾時（>{_WSL_STATUS_TIMEOUT}s），可能 VM 剛喚醒；稍後自動重試"
            if wsl_timed_out else
            "請先安裝 WSL：開管理員 PowerShell 跑 `wsl --install` 並重啟"
        )
    elif not docker_ok:
        hint = "請跑 sandbox/setup_sandbox.bat 安裝 Docker + 建容器"
    elif not container_exists:
        hint = "請跑 sandbox/setup_sandbox.bat 建立容器"
    elif not container_running:
        hint = "容器已停止 — backend 會嘗試自動啟動，或手動 `wsl sudo docker start atlas-sandbox`"

    data = {
        "wsl_ok": wsl_ok,
        "docker_ok": docker_ok,
        "docker_version": docker_version,
        "container_exists": container_exists,
        "container_running": container_running,
        "ready": ready,
        "reasons": reasons,
        "hint": hint,
    }
    _STATUS_CACHE["ts"] = now
    _STATUS_CACHE["data"] = data
    return data


def ensure_running() -> tuple[bool, str]:
    """容器若沒跑就試著 start。回傳 (ok, reason)。
    先用 cache（健康 30s 內免重查）→ 不 healthy 才強制 refresh → 還是不行的話
    對瞬時失敗（例如 WSL 冷啟動 timeout）再重試一次，避免單次慢就誤判 fallback。
    """
    # 短路:剛剛成功跑過 docker exec → 沙盒鐵定還活著、跳過全套慢探測
    # 這是擋「wsl --status 偶發冷啟動延遲拖到 20s+ 害連續 skill 步驟都誤 fallback」的關鍵防線
    if time.time() - _LAST_HEALTHY_EXEC_TS < _RECENT_EXEC_TTL:
        return True, ""

    status = check_status(force_refresh=False)
    if status["ready"]:
        return True, ""
    # 強制重查真實狀態
    status = check_status(force_refresh=True)
    if status["ready"]:
        return True, ""
    # WSL timeout 類的瞬時失敗 → 重試一次（VM 冷啟動通常第二次會通）
    if not status.get("wsl_ok") and "逾時" in (status.get("hint") or ""):
        log.info("[sandbox] 首次 WSL 狀態查詢逾時，重試一次…")
        status = check_status(force_refresh=True)
        if status["ready"]:
            log.info("[sandbox] 重試後健康")
            return True, ""
    # 容器存在但停了 → 嘗試 start
    if status["container_exists"] and not status["container_running"]:
        log.info(f"[sandbox] 容器 {CONTAINER_NAME} 已停止，嘗試 docker start …")
        docker_prefix = _detect_docker_prefix()
        rc, _, err = _run_wsl(["-e", *docker_prefix, "start", CONTAINER_NAME], timeout=15.0)
        if rc == 0:
            status = check_status(force_refresh=True)
            if status["ready"]:
                log.info(f"[sandbox] 容器 {CONTAINER_NAME} 已成功啟動")
                return True, ""
            return False, status["hint"] or "啟動後仍不正常"
        return False, f"容器啟動失敗：{err.strip() or '未知'}"
    return False, status["hint"] or "沙盒未就緒"


# ── 執行 ──────────────────────────────────────────────────────────
@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


# 偵測過的真實 npm global 路徑(image 可能用 nvm、apt、Dockerfile 不同安裝、各家路徑不一致)
# 第一次呼 _docker_exec_cmd 時抓 `npm root -g`、後續用 cache。
_DETECTED_NODE_PATH: Optional[str] = None
_NODE_PATH_DEFAULT = "/usr/local/lib/node_modules"  # Dockerfile 寫死的、apt-get install 後預期位置
_NODE_PATH_LOCK = threading.Lock()


def _detect_npm_root_g() -> str:
    """跑容器內 `npm root -g` 抓真實 global node_modules 位置、cache 起來。
    image 用 nvm 安裝 Node 時實際路徑 = /root/.nvm/versions/node/vX.Y.Z/lib/node_modules,
    跟 Dockerfile 寫死的 /usr/local/lib/node_modules 不同 → require('pptxgenjs') 找不到。
    偵測失敗回 default,不擋路。"""
    global _DETECTED_NODE_PATH
    with _NODE_PATH_LOCK:
        if _DETECTED_NODE_PATH is not None:
            return _DETECTED_NODE_PATH
        try:
            docker_prefix = _detect_docker_prefix()
            rc, out, err = _run_wsl(
                [*docker_prefix, "exec", CONTAINER_NAME, "npm", "root", "-g"],
                timeout=10.0,
            )
            path = (out or "").strip()
            if rc == 0 and path.startswith("/"):
                _DETECTED_NODE_PATH = path
                log.info(f"[sandbox] 偵測到 npm global path: {path}(此後 docker exec 用此值)")
            else:
                _DETECTED_NODE_PATH = _NODE_PATH_DEFAULT
                log.info(f"[sandbox] npm root -g 失敗(rc={rc} err={err[:80]!r})、回 default {_NODE_PATH_DEFAULT}")
        except Exception as e:
            _DETECTED_NODE_PATH = _NODE_PATH_DEFAULT
            log.warning(f"[sandbox] 偵測 NODE_PATH 例外({e})、回 default {_NODE_PATH_DEFAULT}")
        return _DETECTED_NODE_PATH


def _docker_exec_cmd(workdir_wsl: Optional[str], runner: list[str],
                     extra_env: Optional[dict] = None) -> list[str]:
    """組 `wsl <docker_prefix> exec [-w ...] atlas-sandbox <runner...>`

    NODE_PATH：讓 LLM 跑 `subprocess.run(["node", ...])` 時、`require('docx')` /
    `require('pptxgenjs')` 等全域 npm 套件能直接 resolve、不用每次 `npm install` 到 working_dir。
    動態偵測(image 可能用 nvm、apt、不同 setup),避免 Dockerfile 寫死路徑跟實際不符。
    """
    docker_prefix = _detect_docker_prefix()
    node_path = _detect_npm_root_g()
    cmd = ["wsl", "-e", *docker_prefix, "exec",
           "-e", f"NODE_PATH={node_path}"]
    # 額外環境變數(例 skill_llm helper 的 SKILL_LLM_* + PYTHONPATH);
    # 注意:值會出現在程序 arg list,沙盒本就是執行 LLM 程式碼的信任邊界,僅單機本地用。
    if extra_env:
        for k, v in extra_env.items():
            if v is None or v == "":
                continue
            cmd += ["-e", f"{k}={v}"]
    if workdir_wsl:
        cmd += ["-w", workdir_wsl]
    cmd += [CONTAINER_NAME, *runner]
    return cmd


# 專案根目錄下放 LLM 程式碼的暫存區（已被 bind mount 到容器內同路徑）
_TMP_DIR = Path(__file__).resolve().parent.parent.parent / "sandbox" / "_tmp"


def _write_code_tempfile(code: str, suffix: str = ".py") -> str:
    """把 LLM 程式碼寫到 sandbox/_tmp/ 下，回傳 Windows 路徑。
    這個目錄在 bind mount 的範圍內 → 容器用 /mnt/c/... 能讀到同一份檔。"""
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="skill_", dir=str(_TMP_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)
    return tmp_path


def _run_subprocess(
    cmd: list[str],
    timeout: float,
    run_id: str,
    register_cb: Optional[Callable],
    unregister_cb: Optional[Callable],
) -> SandboxResult:
    """執行指令，串 I/O 回來。對齊 executor._skill_run_python 的行為：
    - encoding='utf-8', errors='replace'
    - timeout → kill + 返回 timed_out=True
    - register_cb / unregister_cb 讓 executor 可以中止
    - 並發護欄: _SANDBOX_EXEC_SEMA(預設 3)、pip install 額外 _PIP_INSTALL_LOCK"""
    is_pip = _is_pip_install_cmd(cmd)
    if is_pip:
        log.info(f"[sandbox] 偵測到 pip install、走全域 _PIP_INSTALL_LOCK 序列化(避免 dpkg/cache race)")
    # 用 ExitStack 串多個 lock(sema 必拿、pip 才額外拿)
    import contextlib as _ctx
    with _ctx.ExitStack() as stack:
        stack.enter_context(_SANDBOX_EXEC_SEMA)
        if is_pip:
            stack.enter_context(_PIP_INSTALL_LOCK)
        return _run_subprocess_inner(cmd, timeout, run_id, register_cb, unregister_cb)


def _run_subprocess_inner(
    cmd: list[str],
    timeout: float,
    run_id: str,
    register_cb: Optional[Callable],
    unregister_cb: Optional[Callable],
) -> SandboxResult:
    """實際 spawn subprocess(原 _run_subprocess 邏輯)。並發鎖在外面 _run_subprocess 包好。"""
    proc = None
    try:
        # 沙盒執行時 stdin 關掉，避免子指令等輸入卡死
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 不指定 text/encoding,以 bytes 接;之後用 _decode_subprocess_output 自動偵測
            # UTF-16 LE / utf-8(wsl.exe 失敗時 stderr 是 UTF-16 LE、容器內 python 輸出是 utf-8)
        )
        if register_cb and run_id:
            try:
                register_cb(run_id, proc)
            except Exception as e:
                log.warning(f"[sandbox] register_cb 失敗：{e}")
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
            # subprocess 跑完整(沒 timeout)= 沙盒鐵定通,更新短路 cache
            # 即使 returncode != 0(Python 在容器內 raise Exception)也算通
            _mark_sandbox_healthy()
            return SandboxResult(
                stdout=_decode_subprocess_output(stdout_b),
                stderr=_decode_subprocess_output(stderr_b),
                returncode=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate(timeout=3.0)
            except Exception:
                pass
            return SandboxResult(
                stdout="",
                stderr=f"[錯誤] 沙盒執行超時（>{timeout}秒）",
                returncode=-1,
                timed_out=True,
            )
        finally:
            if unregister_cb and run_id and proc is not None:
                try:
                    unregister_cb(run_id, proc)
                except Exception:
                    pass
    except Exception as e:
        return SandboxResult(
            stdout="",
            stderr=f"[錯誤] 沙盒呼叫失敗：{type(e).__name__}：{e}",
            returncode=-1,
        )


def run_python(
    code: str,
    cwd: Optional[str] = None,
    timeout: float = SANDBOX_TOOL_TIMEOUT,
    run_id: str = "",
    register_cb: Optional[Callable] = None,
    unregister_cb: Optional[Callable] = None,
    translate_paths: bool = True,
) -> SandboxResult:
    """在沙盒內執行 Python 程式碼。
    - code: Python 原始碼
    - cwd: 絕對 Windows 或 WSL 路徑（會自動翻譯）
    - translate_paths: 是否把 code 中的 Windows 絕對路徑自動轉 WSL 形式（預設開）"""
    final_code = translate_code_paths(code) if translate_paths else code
    tmp_win = _write_code_tempfile(final_code, suffix=".py")
    try:
        script_wsl = windows_to_wsl_path(tmp_win)
        cwd_wsl = windows_to_wsl_path(cwd) if cwd else None
        # skill_llm helper:注入 SKILL_LLM_* + PYTHONPATH(指向 bind-mount 的 helper 目錄的 WSL 路徑)
        # 讓容器內 `from skill_llm import llm` 能走「系統現在這顆模型」、不必 import openai。
        extra_env = {}
        try:
            from pipeline.skill_llm_env import runtime_env, HELPERS_DIR_WIN
            _renv = runtime_env()
            if _renv:
                extra_env = dict(_renv)
                extra_env["PYTHONPATH"] = windows_to_wsl_path(str(HELPERS_DIR_WIN))
        except Exception:
            extra_env = {}
        cmd = _docker_exec_cmd(cwd_wsl, ["python", script_wsl], extra_env=extra_env)
        return _run_subprocess(cmd, timeout, run_id, register_cb, unregister_cb)
    finally:
        try:
            os.unlink(tmp_win)
        except Exception:
            pass


def run_shell(
    cmd_str: str,
    cwd: Optional[str] = None,
    timeout: float = SANDBOX_TOOL_TIMEOUT,
    run_id: str = "",
    register_cb: Optional[Callable] = None,
    unregister_cb: Optional[Callable] = None,
) -> SandboxResult:
    """在沙盒內執行 shell 命令（透過 sh -c）。"""
    cwd_wsl = windows_to_wsl_path(cwd) if cwd else None
    final_cmd = translate_shell_paths(cmd_str)
    cmd = _docker_exec_cmd(cwd_wsl, ["sh", "-c", final_cmd])
    return _run_subprocess(cmd, timeout, run_id, register_cb, unregister_cb)
