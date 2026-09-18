"""
Skill 套件管理器 — 管理 AI技能節點可用的 Python 第三方套件

支援雙環境：
- host       ：backend 所在 Windows venv（對應 skill_packages.txt）
- sandbox    ：WSL Docker 容器 atlas-sandbox（對應 sandbox/requirements.txt）

UI 一次只操作一邊，跟著 skill_sandbox_mode toggle 走。target 參數決定目標：
  "auto" → 讀 settings.skill_sandbox_mode 決定（預設）
  "host" / "sandbox" → 明確指定
"""
import os
import re
import subprocess
import sys
import time
import json as _json
from pathlib import Path
from threading import Lock

# in-app pip install 逾時(秒)。超過視為「裝太久 → 多半是大型依賴」,中止並回終端機
# 手動安裝指引(通用機制、不只靠 _LARGE_PKGS 硬編碼表)。可用 env 調整。
try:
    SKILL_INSTALL_TIMEOUT_SEC = int(os.environ.get("SKILL_INSTALL_TIMEOUT_SEC", "180") or 180)
except ValueError:
    SKILL_INSTALL_TIMEOUT_SEC = 180


# ── 套件名 PEP 503 正規化 — 整個模組唯一的「相同套件」比對基準 ────────────
# 規則：
#   - 全部小寫
#   - extras（[xxx]）與版本約束（==、>=、<=）剝掉
#   - 連續的 _ / - / . 都規約成單一 -
# 例：fake_useragent / Fake-UserAgent / fake-useragent 都規約成 fake-useragent，
#     lxml_html_clean / lxml-html-clean 也視為同一個。
# 任何「比較兩個套件名是否相同」的地方都要走這個函式 — 直接用 .lower() 不夠
# （pip list 輸出常用底線、requirements 常用 dash、user 隨便寫），會誤判「未安裝」。
_NORM_RE = re.compile(r"[-_.]+")


def normalize_pkg_name(pkg: str) -> str:
    """PEP 503 正規化套件名（去 extras/版本、底線→dash、小寫）。"""
    base = pkg.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0].split("!")[0].strip().lower()
    return _NORM_RE.sub("-", base)


# 舊名 alias（先前 caller 都叫 _base_name，保留向後相容）
def _base_name(pkg: str) -> str:
    return normalize_pkg_name(pkg)


_PKG_FILE = Path(__file__).parent / "skill_packages.txt"
# 沙盒的套件清單（對應容器內環境）— 跟 Dockerfile 一起被 sandbox/setup.sh 讀取
_SANDBOX_REQ_FILE = Path(__file__).parent.parent / "sandbox" / "requirements.txt"
# 從 sandbox 模組共用容器名、避免兩邊寫死不一致(寫死不同名 → 套件全顯示「未安裝」)
try:
    from pipeline.sandbox import CONTAINER_NAME as _SANDBOX_CONTAINER
except Exception:
    _SANDBOX_CONTAINER = "atlas-sandbox"


# ── Host-only 套件（裝沙盒容器會失敗，因為它們是 Windows-only） ────────
# 這些套件 sandbox（Linux 容器）連 pip install 都不會成功，直接在 add_package_sandbox
# 攔截並回友善訊息。Outlook 自動化節點需要這些，但只跑 host，所以 sandbox 不需要。
HOST_ONLY_PACKAGES: frozenset[str] = frozenset({
    "pywin32",        # win32com / win32api / pythoncom 等的母套件
    "pywin32-ctypes", # 偶爾被當依賴拉進來
    "pywinauto",      # UI Automation
    "comtypes",       # 補位 COM interface
    # 注意：python-docx / python-pptx / pandas / openpyxl 不在這 — 它們跨平台
})


def is_host_only(pkg_name: str) -> bool:
    """套件是否只能裝 host venv（裝 sandbox 會失敗）。"""
    return normalize_pkg_name(pkg_name) in HOST_ONLY_PACKAGES


# ── 大型依賴（在 app 內裝必逾時、靜默失敗）────────────────────────────────
# 這些套件會拉進巨大相依(torch / CUDA / 模型框架,動輒數百 MB~數 GB),
# in-app 安裝(對話框「安裝並繼續」或設定頁)會超過安裝逾時 → 靜默失敗、
# 使用者以為當機。一律不在 app 內裝,直接給終端機手動安裝指引。
# 偵測用 normalize 後的名字比對(底線/連字號/大小寫不敏感)。
_LARGE_PKGS: frozenset[str] = frozenset(normalize_pkg_name(p) for p in {
    "openai-whisper", "whisper", "faster-whisper", "whisperx",
    "torch", "torchvision", "torchaudio",
    "tensorflow", "tensorflow-cpu", "tensorflow-gpu", "keras",
    "transformers", "sentence-transformers", "accelerate", "diffusers",
    "spacy", "stanza", "flair",
    "paddlepaddle", "paddleocr", "easyocr", "rapidocr-onnxruntime",
    "ultralytics", "detectron2", "mmcv", "mmdet",
    "onnxruntime-gpu", "faiss-gpu",
    "llama-cpp-python", "vllm", "ctransformers",
})


def is_large_pkg(pkg_name: str) -> bool:
    """套件是否屬「大型依賴」（in-app 安裝必逾時、應改終端機手動裝）。"""
    return normalize_pkg_name(pkg_name) in _LARGE_PKGS


def manual_install_instructions(pkgs: list[str], target: str) -> str:
    """組「請到終端機手動安裝」指引,含正確指令 + 路徑(sandbox 與 host 不同)。"""
    joined = " ".join(pkgs)
    if target == "sandbox":
        return (
            f"這是大型依賴(會拉進 torch/CUDA 等,數百 MB~數 GB),app 內安裝會逾時。\n"
            f"請開「終端機」手動安裝到沙盒容器(會跑數分鐘,請耐心等到出現 Successfully installed):\n"
            f"  wsl sudo docker exec {_SANDBOX_CONTAINER} pip install {joined}\n"
            f"裝完回到這裡按「安裝並繼續」—— 系統會偵測到已安裝、直接往下跑。"
        )
    # host:用實際的 venv python,確保裝到對的環境
    py = sys.executable
    return (
        f"這是大型依賴(會拉進 torch/CUDA 等,數百 MB~數 GB),app 內安裝會逾時。\n"
        f"請開「終端機」手動安裝到後端 venv(會跑數分鐘,請耐心等到出現 Successfully installed):\n"
        f"  \"{py}\" -m pip install {joined}\n"
        f"裝完回到這裡按「安裝並繼續」—— 系統會偵測到已安裝、直接往下跑。"
    )


def _resolve_target(target: str) -> str:
    """把 'auto' 解析成實際的 'host' 或 'sandbox'（讀 settings）。"""
    t = (target or "auto").strip().lower()
    if t in ("host", "sandbox"):
        return t
    try:
        from settings import get_settings
        mode = (get_settings().get("skill_sandbox_mode") or "host").strip()
        return "sandbox" if mode == "wsl_docker" else "host"
    except Exception:
        return "host"

# ── pip list 快取（一次抓全部，避免對每個套件各呼叫 pip show）──
_PIP_CACHE: dict = {"ts": 0.0, "data": {}}  # {"pandas": {"version": "2.0", "installed": True}, ...}
_PIP_CACHE_TTL = 60.0  # 秒
_PIP_CACHE_LOCK = Lock()


def _pip_snapshot(force_refresh: bool = False) -> dict[str, dict]:
    """用單次 `pip list --format=json` 取得所有已安裝套件（名稱小寫 → {version}）。
    有 60s 快取，大幅避免 Windows 上 subprocess spawn 的開銷。"""
    with _PIP_CACHE_LOCK:
        if not force_refresh and (time.time() - _PIP_CACHE["ts"]) < _PIP_CACHE_TTL and _PIP_CACHE["data"]:
            return _PIP_CACHE["data"]
        snapshot: dict[str, dict] = {}
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=json"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                for item in _json.loads(r.stdout or "[]"):
                    name = str(item.get("name") or "")
                    if name:
                        # 走唯一的正規化函式 — 跟所有 caller 一致
                        snapshot[normalize_pkg_name(name)] = {"version": str(item.get("version") or "")}
        except Exception:
            pass
        _PIP_CACHE["ts"] = time.time()
        _PIP_CACHE["data"] = snapshot
        return snapshot


def _invalidate_pip_cache() -> None:
    """安裝/移除套件後呼叫，確保下次讀到最新狀態。"""
    with _PIP_CACHE_LOCK:
        _PIP_CACHE["ts"] = 0.0
        _PIP_CACHE["data"] = {}


def _read_packages() -> list[str]:
    """讀取 skill_packages.txt，回傳套件名清單（忽略空行和註解）"""
    if not _PKG_FILE.exists():
        return []
    lines = _PKG_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _write_packages(packages: list[str]) -> None:
    """寫入套件清單到 skill_packages.txt（保留 header 註解）"""
    header = (
        "# AI技能節點可用的 Python 套件\n"
        "# 後端啟動時自動安裝缺少的套件到本專案 venv\n"
        "# 可透過管理介面新增或移除\n\n"
    )
    _PKG_FILE.write_text(header + "\n".join(packages) + "\n", encoding="utf-8")


def _is_installed(pkg_name: str) -> bool:
    """檢查套件是否已安裝（走快照，不呼叫 subprocess）"""
    return normalize_pkg_name(pkg_name) in _pip_snapshot()


def _pip_check_host() -> str:
    """host 環境跑 `pip check` 偵測依賴衝突,回衝突摘要(無衝突回空字串)。
    擋掉「後裝的套件升/降級了其他套件、弄壞既有專案」這種 silent breakage。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 or not out or "No broken requirements" in out:
            return ""
        return "\n".join(l for l in out.splitlines() if l.strip())[:600]
    except Exception:
        return ""


def _pip_install(pkg_name: str) -> tuple[bool, str]:
    """安裝單一套件，回傳 (成功, 訊息)。裝完跑 pip check、有依賴衝突就附告警。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg_name, "-q"],
            capture_output=True, text=True, timeout=SKILL_INSTALL_TIMEOUT_SEC,
        )
        if result.returncode == 0:
            msg = f"✅ {pkg_name} 安裝成功"
            conflicts = _pip_check_host()
            if conflicts:
                msg += (f"\n⚠️ 依賴衝突告警(裝 {pkg_name} 後 pip check):\n{conflicts}\n"
                        f"→ 可能影響其他既有專案、建議檢查版本相容性。")
            return True, msg
        return False, f"❌ {pkg_name} 安裝失敗：{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, f"❌ {pkg_name} 安裝逾時"
    except Exception as e:
        return False, f"❌ {pkg_name} 安裝錯誤：{e}"


def _pip_uninstall(pkg_name: str) -> tuple[bool, str]:
    """移除單一套件"""
    base = normalize_pkg_name(pkg_name)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", base, "-y", "-q"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, f"✅ {base} 已移除"
        return False, f"❌ {base} 移除失敗：{result.stderr.strip()}"
    except Exception as e:
        return False, f"❌ {base} 移除錯誤：{e}"


def auto_install_packages() -> None:
    """後端啟動時自動安裝缺少的套件"""
    packages = _read_packages()
    if not packages:
        return
    missing = [p for p in packages if not _is_installed(p)]
    if not missing:
        print(f"✅ Skill 套件全部已安裝（{len(packages)} 個）")
        return
    print(f"📦 正在安裝缺少的 Skill 套件：{', '.join(missing)}")
    for pkg in missing:
        ok, msg = _pip_install(pkg)
        print(f"  {msg}")


def list_packages() -> list[dict]:
    """列出所有 skill 套件及安裝狀態（全部走一次 pip list 快照，~200ms 內完成）"""
    packages = _read_packages()
    snapshot = _pip_snapshot()
    result = []
    for pkg in packages:
        info = snapshot.get(normalize_pkg_name(pkg))
        installed = info is not None
        version = info.get("version", "") if info else ""
        result.append({
            "name": pkg,
            "installed": installed,
            "version": version,
        })
    return result


def add_package(pkg_name: str) -> tuple[bool, str]:
    """新增套件：實際安裝 + 寫入清單。
    流程順序很重要：
      1. 先檢查容器/venv 內實際是否安裝 — 已裝就直接回成功（聲明 ≠ 已安裝）
      2. 沒裝就跑 pip install
      3. 安裝成功後若沒在清單也加入清單
    之前的版本只檢查「是否在清單聲明中」、user 手動編輯了 requirements.txt
    但容器還沒裝、按 [安裝] 按鈕會被誤拒「已在清單中」。
    """
    pkg_name = pkg_name.strip()
    if not pkg_name:
        return False, "套件名稱不能為空"

    packages = _read_packages()
    base = _base_name(pkg_name)
    in_list = any(_base_name(p) == base for p in packages)

    # 1. 已實際安裝（不論清單）→ 直接成功
    snapshot = _pip_snapshot()
    if base in snapshot:
        # 順手補登清單（聲明跟實際同步）
        if not in_list:
            packages.append(pkg_name)
            _write_packages(packages)
        return True, f"✅ {pkg_name} 已安裝（{snapshot[base].get('version', '')}）"

    # 1.5 大型依賴 → 不在 app 內裝(必逾時),回終端機手動安裝指引
    if is_large_pkg(pkg_name):
        return False, manual_install_instructions([pkg_name], "host")

    # 2. 沒安裝 → 跑 pip install
    ok, msg = _pip_install(pkg_name)
    if not ok:
        return False, msg

    # 3. 安裝成功 + 補登清單
    if not in_list:
        packages.append(pkg_name)
        _write_packages(packages)
    _invalidate_pip_cache()
    return True, msg


def remove_package(pkg_name: str) -> tuple[bool, str]:
    """移除套件：從清單移除 + 解除安裝"""
    pkg_name = pkg_name.strip()
    packages = _read_packages()
    base = normalize_pkg_name(pkg_name)

    # 從清單中移除
    new_packages = []
    found = False
    for p in packages:
        if normalize_pkg_name(p) == base:
            found = True
        else:
            new_packages.append(p)

    if not found:
        return False, f"{pkg_name} 不在清單中"

    # 解除安裝
    _pip_uninstall(pkg_name)

    # 更新清單 + 讓快取失效
    _write_packages(new_packages)
    _invalidate_pip_cache()
    return True, f"✅ {pkg_name} 已從清單移除並解除安裝"


# ── venv 同步：找出已裝但不在清單中的套件 ────────────────────────────────────
_BOOTSTRAP_EXCLUDES = {"pip", "setuptools", "wheel"}


def scan_unlisted_packages() -> list[dict]:
    """
    掃 venv 中**已安裝但不在 skill_packages.txt 也不在 requirements.txt** 的套件。
    只列出頂層套件（非其他套件的依賴），避免列出一堆傳遞依賴。

    回傳 list[{name, version}]。
    """
    # 1. 讀出 skill_packages.txt 和 requirements.txt 的 base names
    skill_bases = {_base_name(p) for p in _read_packages()}

    req_file = Path(__file__).parent / "requirements.txt"
    req_bases: set[str] = set()
    if req_file.exists():
        for line in req_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                req_bases.add(_base_name(line))

    # 2. 用 pip list --not-required 取得頂層套件
    import json as _json
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--not-required", "--format=json"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return []
        installed = _json.loads(r.stdout)
    except Exception:
        return []

    # 3. 過濾：排除 bootstrap、已在 skill 清單、已在 requirements
    unlisted = []
    for pkg in installed:
        name = pkg.get("name", "")
        if not name:
            continue
        base = normalize_pkg_name(name)
        if base in _BOOTSTRAP_EXCLUDES:
            continue
        if base in skill_bases:
            continue
        if base in req_bases:
            continue
        unlisted.append({"name": name, "version": pkg.get("version", "")})
    unlisted.sort(key=lambda x: normalize_pkg_name(x["name"]))
    return unlisted


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox 版本（走 wsl docker exec）
# ─────────────────────────────────────────────────────────────────────────────

_SANDBOX_PIP_CACHE: dict = {"ts": 0.0, "data": {}}


def _sandbox_docker_prefix() -> list[str]:
    """借用 sandbox 模組已偵測好的 docker 前綴（免重複偵測）。"""
    try:
        from pipeline import sandbox as _s
        return _s._detect_docker_prefix()  # noqa: SLF001
    except Exception:
        return ["docker"]


def _sandbox_pip_snapshot(force_refresh: bool = False) -> dict[str, dict]:
    with _PIP_CACHE_LOCK:
        if not force_refresh and (time.time() - _SANDBOX_PIP_CACHE["ts"]) < _PIP_CACHE_TTL and _SANDBOX_PIP_CACHE["data"]:
            return _SANDBOX_PIP_CACHE["data"]
        snapshot: dict[str, dict] = {}
        prefix = _sandbox_docker_prefix()
        try:
            r = subprocess.run(
                ["wsl", "-e", *prefix, "exec", _SANDBOX_CONTAINER, "pip", "list", "--format=json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if r.returncode == 0:
                for item in _json.loads(r.stdout or "[]"):
                    name = str(item.get("name") or "")
                    if name:
                        # 走唯一的正規化函式 — 跟 _base_name / list_packages_sandbox 一致
                        snapshot[normalize_pkg_name(name)] = {"version": str(item.get("version") or "")}
        except Exception:
            pass
        _SANDBOX_PIP_CACHE["ts"] = time.time()
        _SANDBOX_PIP_CACHE["data"] = snapshot
        return snapshot


def _invalidate_sandbox_pip_cache() -> None:
    with _PIP_CACHE_LOCK:
        _SANDBOX_PIP_CACHE["ts"] = 0.0
        _SANDBOX_PIP_CACHE["data"] = {}


def _sandbox_pip_check() -> str:
    """沙盒容器內跑 `pip check` 偵測依賴衝突,回衝突摘要(無衝突回空字串)。"""
    prefix = _sandbox_docker_prefix()
    try:
        r = subprocess.run(
            ["wsl", "-e", *prefix, "exec", _SANDBOX_CONTAINER, "pip", "check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 or not out or "No broken requirements" in out:
            return ""
        return "\n".join(l for l in out.splitlines() if l.strip())[:600]
    except Exception:
        return ""


def _sandbox_pip_install(pkg_name: str) -> tuple[bool, str]:
    prefix = _sandbox_docker_prefix()
    try:
        result = subprocess.run(
            ["wsl", "-e", *prefix, "exec", _SANDBOX_CONTAINER, "pip", "install", "--no-cache-dir", pkg_name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=SKILL_INSTALL_TIMEOUT_SEC,
        )
        if result.returncode == 0:
            msg = f"✅ {pkg_name} 已安裝到沙盒容器"
            conflicts = _sandbox_pip_check()
            if conflicts:
                msg += (f"\n⚠️ 依賴衝突告警(裝 {pkg_name} 後容器 pip check):\n{conflicts}\n"
                        f"→ 可能影響其他既有專案、建議檢查版本相容性。")
            return True, msg
        return False, f"❌ {pkg_name} 沙盒安裝失敗：{(result.stderr or result.stdout or '').strip()[:500]}"
    except subprocess.TimeoutExpired:
        return False, f"❌ {pkg_name} 沙盒安裝逾時（>180 秒）"
    except Exception as e:
        return False, f"❌ {pkg_name} 沙盒安裝錯誤：{e}"


def _sandbox_pip_uninstall(pkg_name: str) -> tuple[bool, str]:
    base = _base_name(pkg_name)
    prefix = _sandbox_docker_prefix()
    try:
        result = subprocess.run(
            ["wsl", "-e", *prefix, "exec", _SANDBOX_CONTAINER, "pip", "uninstall", base, "-y"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode == 0:
            return True, f"✅ {base} 已從沙盒移除"
        return False, f"❌ {base} 沙盒移除失敗：{(result.stderr or '').strip()[:300]}"
    except Exception as e:
        return False, f"❌ {base} 沙盒移除錯誤：{e}"


def _read_sandbox_packages() -> list[str]:
    if not _SANDBOX_REQ_FILE.exists():
        return []
    lines = _SANDBOX_REQ_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _write_sandbox_packages(packages: list[str]) -> None:
    """保留原始標頭註解（tier 分組、提示），只更新套件部分。"""
    header = (
        "# Skill 沙盒容器預裝套件\n"
        "# 分成幾個 tier：build 時 Dockerfile 會拆成多個 RUN 讓每層獨立裝，避免一次性 OOM\n"
        "#\n"
        "# 沒進這清單但某個 workflow 需要的套件，可以事後臨時裝進容器：\n"
        "#   wsl sudo docker exec atlas-sandbox pip install <pkg>\n"
        "# 要永久加，把它寫進這檔然後 rebuild image。\n\n"
    )
    _SANDBOX_REQ_FILE.write_text(header + "\n".join(packages) + "\n", encoding="utf-8")


def list_packages_sandbox() -> list[dict]:
    """列出沙盒容器已安裝套件 + 標記哪些在 requirements.txt 裡。"""
    declared = _read_sandbox_packages()
    declared_bases = {_base_name(p) for p in declared}
    snapshot = _sandbox_pip_snapshot()
    # 包含「在清單但還沒裝」(rare) + 「已裝且在清單」 + 「臨時裝但不在清單」
    result = []
    seen: set[str] = set()
    for pkg in declared:
        base = _base_name(pkg)
        info = snapshot.get(base)
        result.append({
            "name": pkg,
            "installed": info is not None,
            "version": info.get("version", "") if info else "",
            "managed": True,  # 在 requirements.txt 裡
        })
        seen.add(base)
    # 容器中還有的（臨時裝的），也列出來但標為 managed=False
    for base, info in snapshot.items():
        if base in seen:
            continue
        # 略過 pip/setuptools/wheel 這類 bootstrap
        if base in _BOOTSTRAP_EXCLUDES:
            continue
        result.append({
            "name": base,
            "installed": True,
            "version": info.get("version", ""),
            "managed": False,
        })
    return result


def add_package_sandbox(pkg_name: str) -> tuple[bool, str]:
    """沙盒安裝套件 + 寫進 sandbox/requirements.txt（rebuild 後也保留）。
    跟 host 版同樣的順序：先檢查容器內實際是否裝、沒裝才跑 pip、最後補登 requirements.txt。
    之前直接檢查「在 requirements.txt 聲明過」就拒絕、會讓 user 手動編輯 txt 但容器
    還沒裝時、按[安裝]按鈕被誤拒「已在沙盒清單中」。
    """
    pkg_name = pkg_name.strip()
    if not pkg_name:
        return False, "套件名稱不能為空"
    # Host-only 套件（pywin32 等）裝 Linux 容器會失敗 — 提早攔下、給清楚訊息
    if is_host_only(pkg_name):
        return False, (f"❌ {pkg_name} 是 Windows-only 套件，無法裝到 Linux 沙盒容器。"
                       f"請切換到 host 模式（Settings → 沙盒模式 → host）後再裝；"
                       f"或這個套件本來就是給 Outlook 自動化節點用，sandbox 用不到。")

    declared = _read_sandbox_packages()
    base = _base_name(pkg_name)
    in_list = any(_base_name(p) == base for p in declared)

    # 1. 已實際安裝在容器內 → 直接成功
    snapshot = _sandbox_pip_snapshot()
    if base in snapshot:
        if not in_list:
            declared.append(pkg_name)
            _write_sandbox_packages(declared)
        return True, f"✅ {pkg_name} 已安裝在沙盒容器（{snapshot[base].get('version', '')}）"

    # 1.5 大型依賴 → 不在 app 內裝(必逾時),回終端機手動安裝指引
    if is_large_pkg(pkg_name):
        return False, manual_install_instructions([pkg_name], "sandbox")

    # 2. 沒裝 → 跑 pip install
    ok, msg = _sandbox_pip_install(pkg_name)
    if not ok:
        return False, msg

    # 3. 安裝成功後補登 requirements.txt
    if not in_list:
        declared.append(pkg_name)
        _write_sandbox_packages(declared)
    _invalidate_sandbox_pip_cache()
    return True, msg


def remove_package_sandbox(pkg_name: str) -> tuple[bool, str]:
    """沙盒移除套件 + 從 sandbox/requirements.txt 拿掉。"""
    pkg_name = pkg_name.strip()
    declared = _read_sandbox_packages()
    base = _base_name(pkg_name)
    new_declared = [p for p in declared if _base_name(p) != base]
    found_in_list = len(new_declared) != len(declared)
    ok, msg = _sandbox_pip_uninstall(pkg_name)
    if found_in_list:
        _write_sandbox_packages(new_declared)
    _invalidate_sandbox_pip_cache()
    if not ok and not found_in_list:
        return False, f"{pkg_name} 不在沙盒清單中"
    return ok or found_in_list, msg if ok else f"✅ {base} 已從沙盒清單移除（容器內未安裝或移除失敗）"


# ─────────────────────────────────────────────────────────────────────────────
# 統一入口（依 target 分流）
# ─────────────────────────────────────────────────────────────────────────────

def list_packages_by_target(target: str = "auto") -> dict:
    t = _resolve_target(target)
    if t == "sandbox":
        return {"target": "sandbox", "packages": list_packages_sandbox()}
    return {"target": "host", "packages": list_packages()}


def add_package_by_target(pkg_name: str, target: str = "auto") -> tuple[bool, str, str]:
    t = _resolve_target(target)
    ok, msg = add_package_sandbox(pkg_name) if t == "sandbox" else add_package(pkg_name)
    return ok, msg, t


def remove_package_by_target(pkg_name: str, target: str = "auto") -> tuple[bool, str, str]:
    t = _resolve_target(target)
    ok, msg = remove_package_sandbox(pkg_name) if t == "sandbox" else remove_package(pkg_name)
    return ok, msg, t


def add_to_list_only(pkg_name: str) -> tuple[bool, str]:
    """
    只把套件名加到 skill_packages.txt，不再跑 pip install
    （用於已手動安裝、只需納管的情境）。
    """
    pkg_name = pkg_name.strip()
    if not pkg_name:
        return False, "套件名稱不能為空"
    packages = _read_packages()
    base = _base_name(pkg_name)
    for p in packages:
        if _base_name(p) == base:
            return False, f"{pkg_name} 已在清單中"
    packages.append(pkg_name)
    _write_packages(packages)
    return True, f"✅ {pkg_name} 已加入 skill_packages.txt"
