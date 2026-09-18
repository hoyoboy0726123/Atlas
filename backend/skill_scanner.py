"""
掃描使用者的 Claude Code skill 目錄（~/.agents/skills/），
列出可用的 skill 名稱、描述與子資源。
"""
from pathlib import Path
from typing import Optional
import re
import ast
import json


_DEFAULT_SKILLS_ROOT = Path.home() / ".agents" / "skills"


def get_skills_root() -> Path:
    """Skill 檔案目錄。優先序:環境變數 SKILLS_DIR → 設定 skills_dir → 預設 ~/.agents/skills/。
    讓專案使用者能自選 skill 檔放在哪。"""
    import os
    env = (os.getenv("SKILLS_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from settings import get_settings
        cfg = (get_settings().get("skills_dir") or "").strip()
        if cfg:
            return Path(cfg).expanduser()
    except Exception:
        pass
    return _DEFAULT_SKILLS_ROOT


def seed_default_skills() -> int:
    """首次啟動把 repo 內建的 default_skills/ 複製到使用者 skill 目錄(若該 skill 尚不存在)。

    用途:讓別人 clone 後不必手動裝,就能跑用到內建 skill 的範例(如 scraped-content-parser)。
    已存在的 skill **不覆蓋**(尊重使用者自己改過的版本)。回傳本次複製了幾個。
    注意:docx / pptx 等 Anthropic 官方 skill 不在 repo 內、不由此處理(README 另有說明)。
    """
    import shutil
    try:
        src_root = Path(__file__).parent.parent / "default_skills"
        if not src_root.is_dir():
            return 0
        dst_root = get_skills_root()
        dst_root.mkdir(parents=True, exist_ok=True)
        copied = 0
        for skill_dir in sorted(src_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            dst = dst_root / skill_dir.name
            if dst.exists():
                continue
            shutil.copytree(skill_dir, dst)
            copied += 1
        return copied
    except Exception:
        return 0


# Python 內建模組（不需要 pip install）— 動態用 sys.stdlib_module_names 取得，
# 相容 Python 3.10+；失敗時 fallback 到寫死清單
try:
    import sys as _sys
    _STDLIB_MODULES = set(_sys.stdlib_module_names)  # Python 3.10+
except AttributeError:
    _STDLIB_MODULES = {
        "os", "sys", "re", "json", "math", "random", "time", "datetime", "pathlib",
        "subprocess", "shutil", "glob", "io", "csv", "hashlib", "urllib", "http",
        "collections", "itertools", "functools", "typing", "dataclasses", "enum",
        "abc", "argparse", "asyncio", "logging", "traceback", "threading", "queue",
        "multiprocessing", "concurrent", "socket", "struct", "copy", "pickle",
        "base64", "uuid", "tempfile", "warnings", "inspect", "textwrap", "string",
        "unicodedata", "zipfile", "tarfile", "gzip", "bz2", "sqlite3", "xml", "html",
        "email", "mimetypes", "platform", "operator", "contextlib", "types",
        "weakref", "gc", "atexit", "signal", "fnmatch", "select", "webbrowser",
    }

# 常見 import 名稱 → pip 套件名稱的對應（名稱不一致的情況）
_PIP_NAME_MAP = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "magic": "python-magic",
    "fitz": "PyMuPDF",
    "win32gui": "pywin32",
    "win32con": "pywin32",
    "win32api": "pywin32",
}


def _parse_frontmatter(skill_md_path: Path) -> dict:
    """從 SKILL.md 讀取 YAML frontmatter 的 name / description。"""
    result = {"name": "", "description": ""}
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except Exception:
        return result
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return result
    fm = m.group(1)
    # 優先用 PyYAML 解析（穩，能處理 block scalar / 引號）
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(fm) or {}
        if isinstance(parsed, dict):
            result["name"] = str(parsed.get("name", "")).strip()
            desc = str(parsed.get("description", "")).strip()
            # 多行描述壓成單行
            desc = re.sub(r"\s+", " ", desc)
            result["description"] = desc
            return result
    except Exception:
        pass
    # Fallback：regex（YAML 不可用時的備援）
    name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    desc_m = re.search(r"^description:\s*(.+?)(?=\n[a-zA-Z_]+:|\Z)", fm, re.MULTILINE | re.DOTALL)
    if name_m:
        result["name"] = name_m.group(1).strip().strip('"\'')
    if desc_m:
        desc = desc_m.group(1).strip().strip('"\'')
        desc = re.sub(r"\s+", " ", desc)
        result["description"] = desc
    return result


def list_available_skills() -> list[dict]:
    """
    掃 ~/.agents/skills/ 下每個子資料夾，回傳 skill 清單。

    每筆格式：
    {
        "name": "skill-creator",
        "display_name": "Skill Creator",
        "description": "Create new skills...",
        "path": "C:/Users/.../skill-creator",
        "has_scripts": True,
        "has_references": True,
        "has_assets": False,
    }
    """
    root = get_skills_root()
    if not root.exists():
        return []

    skills = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        meta = _parse_frontmatter(skill_md)
        skills.append({
            "name": meta["name"] or entry.name,
            "display_name": entry.name,
            "description": meta["description"],
            "path": str(entry.absolute()),
            "has_scripts": (entry / "scripts").is_dir(),
            "has_references": (entry / "references").is_dir(),
            "has_assets": (entry / "assets").is_dir(),
            "has_package_json": (entry / "package.json").is_file(),
            "has_requirements": (entry / "requirements.txt").is_file(),
        })
    return skills


def _resolve_skill_dir(skill_name: str) -> Optional[Path]:
    """從 skill_name（資料夾名或 frontmatter 的 name）找到 skill 資料夾。"""
    root = get_skills_root()
    if not root.exists():
        return None
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name == skill_name:
            return entry
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = _parse_frontmatter(entry / "SKILL.md")
        if meta["name"] == skill_name:
            return entry
    return None


def _extract_py_imports(py_file: Path) -> set[str]:
    """用 AST 解析 .py 檔抽出所有 top-level import 模組名。"""
    names: set[str] = set()
    try:
        src = py_file.read_text(encoding="utf-8")
    except Exception:
        return names
    try:
        tree = ast.parse(src)
    except Exception:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


# 常見系統 CLI 工具：被提及在 SKILL.md 即認定為外部依賴（使用者需自行安裝）
_KNOWN_SYSTEM_TOOLS = {
    "soffice": "LibreOffice",
    "libreoffice": "LibreOffice",
    "pdftoppm": "Poppler",
    "pdftocairo": "Poppler",
    "pdfinfo": "Poppler",
    "ffmpeg": "FFmpeg",
    "ffprobe": "FFmpeg",
    "imagemagick": "ImageMagick",
    "convert": "ImageMagick",
    "magick": "ImageMagick",
    "tesseract": "Tesseract OCR",
    "wkhtmltopdf": "wkhtmltopdf",
    "pandoc": "Pandoc",
    "git": "Git",
    "node": "Node.js",
    "npm": "Node.js/npm",
    "npx": "Node.js/npm",
    "docker": "Docker",
}


_NPM_CACHE: dict = {"ts": 0.0, "data": set()}
_NPM_CACHE_TTL = 60.0


def list_global_npm_packages(force_refresh: bool = False) -> set[str]:
    """跑 `npm list -g --depth=0 --json` 取得目前全域安裝的 npm 套件名（小寫）。
    自動跟著 settings.skill_sandbox_mode 走：
      - host 模式 → 查 host 機器的 npm
      - sandbox 模式 → `wsl docker exec <container> npm list -g`（查容器）
    有 60 秒記憶體快取；找不到 npm 或執行失敗時回傳空集合。
    """
    import time as _time
    import subprocess
    import shutil as _shutil

    if not force_refresh and (_time.time() - _NPM_CACHE["ts"]) < _NPM_CACHE_TTL and _NPM_CACHE["data"]:
        return _NPM_CACHE["data"]

    # 跟 skill_pkg_manager 同邏輯讀 mode
    mode = "host"
    try:
        from settings import get_settings
        m = (get_settings().get("skill_sandbox_mode") or "host").strip()
        if m == "wsl_docker":
            mode = "sandbox"
    except Exception:
        pass

    result: set[str] = set()
    try:
        if mode == "sandbox":
            # 容器模式：透過 wsl docker exec 查容器內 npm globals
            from pipeline.sandbox import _detect_docker_prefix, CONTAINER_NAME
            prefix = _detect_docker_prefix()
            cmd = ["wsl", "-e", *prefix, "exec", CONTAINER_NAME,
                   "npm", "list", "-g", "--depth=0", "--json"]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="ignore",
            )
        else:
            # host 模式
            npm_cmd = _shutil.which("npm")
            if not npm_cmd:
                _NPM_CACHE["ts"] = _time.time()
                _NPM_CACHE["data"] = set()
                return set()
            proc = subprocess.run(
                [npm_cmd, "list", "-g", "--depth=0", "--json"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="ignore",
            )
        data = json.loads(proc.stdout or "{}")
        deps = data.get("dependencies") or {}
        result = {name.lower() for name in deps.keys()}
    except Exception:
        result = set()
    _NPM_CACHE["ts"] = _time.time()
    _NPM_CACHE["data"] = result
    return result


def _parse_install_commands(text: str) -> tuple[list[str], list[str], list[str]]:
    """
    掃描 markdown 文字裡的依賴提示，回傳：
    - pip 套件（來自 `pip install X` / `pip install "X[extra]"` 等）
    - npm 套件（來自 `npm install X` / `npm install -g X`）
    - 系統工具（來自反引號包住的 CLI 名稱 + 已知工具清單比對）
    """
    pip_pkgs: list[str] = []
    npm_pkgs: list[str] = []
    system_tools: set[str] = set()

    # 只在「像指令的文字」裡找安裝指令,避免把散文當套件
    # (踩過:"pip install the following packages: pandas" 把 the/following/packages 都當套件)。
    # 來源:markdown 程式碼區塊 ``` ``` + 反引號行內碼 + 以 pip/npm install 開頭的行。
    _cmd_chunks: list[str] = []
    _cmd_chunks += re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    _cmd_chunks += re.findall(r"`([^`\n]+)`", text)
    for _ln in text.splitlines():
        _s = _ln.strip().lstrip("$").strip()
        if re.match(r"(pip[0-9.]*|python[0-9.]*\s+-m\s+pip|npm)\s+install\b", _s, re.IGNORECASE):
            _cmd_chunks.append(_s)
    scan_text = "\n".join(_cmd_chunks)

    # 合法套件名:名稱 + 可選 extras + 可選「嚴格」版本約束。
    # 版本部分必須是「運算子 + 數字開頭的版本」(>=0.3.0),不能吃任意字元 ——
    # 舊版 [<>=!~][^\s]* 會把程式碼片段 missing_packages=["<pkg>"]) 誤判成套件(有 = 和 <)。
    _pkg_re = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*"                       # PEP 508 名稱
        r"(\[[A-Za-z0-9,._\-]+\])?"                          # 可選 extras
        r"((===|==|!=|~=|>=|<=|>|<)[0-9][A-Za-z0-9.*+!_.\-]*"  # 版本:運算子+數字開頭
        r"(\s*,\s*(===|==|!=|~=|>=|<=|>|<)[0-9][A-Za-z0-9.*+!_.\-]*)*)?$"
    )

    def _accept(pkg: str, self_names: tuple) -> str:
        pkg = pkg.strip().strip("\"'").rstrip(",;.")
        if not pkg or pkg.startswith("-") or pkg.startswith(".") or pkg.endswith(".txt"):
            return ""
        if pkg.lower() in self_names:
            return ""
        return pkg if _pkg_re.match(pkg) else ""

    # pip install：抓每條 install 指令後面的套件名(允許引號、extras、版本)
    for m in re.finditer(r"\bpip[0-9.]*\s+install\s+([^\n`]+)", scan_text, re.IGNORECASE):
        for tok in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', m.group(1)):
            pkg = _accept(next(filter(None, tok), ""), ("install", "pip"))
            if pkg:
                pip_pkgs.append(pkg)

    # npm install：同上
    for m in re.finditer(r"\bnpm\s+install\s+([^\n`]+)", scan_text, re.IGNORECASE):
        for tok in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', m.group(1)):
            pkg = _accept(next(filter(None, tok), ""), ("install", "npm"))
            if pkg:
                npm_pkgs.append(pkg)

    # 系統工具：只認反引號包住的（`soffice`、`pdftoppm`）或括號內的（(`magick`)）
    # 避免匹配到英文敘述裡的普通單字（例如 "Convert slides" 不該認作 ImageMagick）
    for tool_name, display in _KNOWN_SYSTEM_TOOLS.items():
        if re.search(rf"`{re.escape(tool_name)}`", text, re.IGNORECASE):
            system_tools.add(display)

    # 去重但保留順序
    pip_pkgs = list(dict.fromkeys(pip_pkgs))
    npm_pkgs = list(dict.fromkeys(npm_pkgs))
    return pip_pkgs, npm_pkgs, sorted(system_tools)


def _scan_markdown_dependencies(skill_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """讀 skill 根目錄的所有 .md 檔（SKILL.md + references/），合併依賴提示。"""
    all_pip: list[str] = []
    all_npm: list[str] = []
    all_sys: set[str] = set()
    md_files = list(skill_dir.glob("*.md"))
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        md_files.extend(ref_dir.glob("*.md"))
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        pip_pkgs, npm_pkgs, sys_tools = _parse_install_commands(text)
        all_pip.extend(pip_pkgs)
        all_npm.extend(npm_pkgs)
        all_sys.update(sys_tools)
    return (
        list(dict.fromkeys(all_pip)),
        list(dict.fromkeys(all_npm)),
        sorted(all_sys),
    )


def scan_skill_dependencies(skill_name: str) -> dict:
    """
    掃描指定 skill 的依賴，回傳：
    {
        "skill_name": "...",
        "found": true,
        "python": {
            "requirements_txt": ["pandas>=1.0", ...],       # requirements.txt 原文
            "imports_detected": ["pandas", "openpyxl", ...], # 從 .py 檔靜態分析
            "suggested_pip": ["pandas", "openpyxl", ...],    # 推薦安裝的 pip 套件（排除 stdlib）
        },
        "node": {
            "package_json": {"dependencies": {...}, "devDependencies": {...}} or null,
            "needs_npm_install": true/false,
        },
    }
    """
    skill_dir = _resolve_skill_dir(skill_name)
    if skill_dir is None:
        return {"skill_name": skill_name, "found": False}

    # ── Python 依賴 ──────────────────────────────────────
    requirements_txt: list[str] = []
    req_file = skill_dir / "requirements.txt"
    if req_file.is_file():
        try:
            for line in req_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    requirements_txt.append(line)
        except Exception:
            pass

    # 靜態分析所有 .py 檔的 import
    imports_detected: set[str] = set()
    for py_file in skill_dir.rglob("*.py"):
        imports_detected.update(_extract_py_imports(py_file))

    # 排除 stdlib + skill 自家模組（所有 .py 檔名 + skill 底下所有層級的資料夾名）
    local_module_names = {f.stem for f in skill_dir.rglob("*.py")}
    for d in skill_dir.rglob("*"):
        if d.is_dir() and not d.name.startswith(".") and d.name != "__pycache__":
            local_module_names.add(d.name)
    third_party = sorted(
        imp for imp in imports_detected
        if imp not in _STDLIB_MODULES and imp not in local_module_names and not imp.startswith("_")
    )

    # ── 從 SKILL.md 與參考 .md 文字中抽出 pip 提示 + 系統工具 ──
    md_pip, _md_npm_ignored, system_tools = _scan_markdown_dependencies(skill_dir)

    # ── 可一鍵安裝的依賴 = 只認「高信心的宣告來源」────────────────────────────
    # 設計原則:寧可漏報、不可誤導安裝(缺套件由 runtime 自癒準確補裝)。
    #   ✅ requirements.txt        —— 權威,全收
    #   ✅ 散文 pip install 帶版本約束者(X>=1.0)—— 幾乎必是作者刻意宣告的真套件
    #      (tavily-python>=0.3.0 這種),版本號正好把「真依賴」和雜訊分開
    #   ❌ 散文裸單字(backend / agent / db)—— 丟。無法與真的裸名(streamlit)分辨,
    #      但這些高風險詞(本地模組名)沒版本號、會被此規則擋掉
    #   ❌ import 掃描 —— 一律不進安裝清單:import 名≠套件名(telegram→python-telegram-bot)、
    #      且會撈到專案本地模組(atlas-assistant 的 db/pipeline 不在 skill 內)→
    #      `pip install db` 會裝到 PyPI 別人的同名套件(供應鏈風險)
    #   ❌ markdown 的 npm install —— 丟(文件範例 `npm install -g backend` 會被誤撈);npm 只認 package.json
    def _has_version(spec: str) -> bool:
        return bool(re.search(r"[<>=!~]", spec))

    suggested_pip: list[str] = []
    _pip_bases = set()
    for pkg in requirements_txt + [p for p in md_pip if _has_version(p)]:
        base = re.split(r"[<>=!~\[]", pkg)[0].strip().lower().replace("_", "-")
        if base and base not in _pip_bases:   # 依「套件基底名」去重(擋 anthropic + anthropic>=0.40 重複)
            suggested_pip.append(pkg)
            _pip_bases.add(base)
    # 僅供參考:偵測到但未在安裝清單的第三方 import(可能含本地模組/誤判、不可一鍵安裝)
    undeclared_imports = [
        m for m in third_party
        if m.lower().replace("_", "-") not in _pip_bases
        and _PIP_NAME_MAP.get(m, m).lower().replace("_", "-") not in _pip_bases
    ]

    # ── Node.js 依賴 ─────────────────────────────────────
    package_json = None
    pkg_file = skill_dir / "package.json"
    if pkg_file.is_file():
        try:
            package_json = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception:
            package_json = None

    # npm 只認 package.json 的 dependencies(不再吃 markdown 的 npm install 散文)
    suggested_npm: list[str] = []
    if isinstance(package_json, dict):
        for key in ("dependencies", "devDependencies"):
            deps = package_json.get(key) or {}
            if isinstance(deps, dict):
                for name in deps:
                    if name not in suggested_npm:
                        suggested_npm.append(name)

    return {
        "skill_name": skill_name,
        "found": True,
        "path": str(skill_dir.absolute()),
        "python": {
            "requirements_txt": requirements_txt,
            "imports_detected": sorted(imports_detected),
            "undeclared_imports": undeclared_imports,   # 僅參考、非可安裝
            "suggested_pip": suggested_pip,             # = requirements.txt(權威)
            "has_manifest": bool(requirements_txt),
        },
        "node": {
            "package_json": package_json,
            "needs_npm_install": package_json is not None,
            "suggested_npm": suggested_npm,             # = package.json(權威)
        },
        "system_tools": system_tools,
    }


def get_skill_prompt_injection(skill_name: str) -> Optional[str]:
    """
    給定 skill 名稱（資料夾名），回傳要注入 LLM system prompt 的文字：
    - SKILL.md 全文
    - 子資源清單（scripts/references/assets）

    找不到則回 None。
    """
    skill_dir = _resolve_skill_dir(skill_name)
    if skill_dir is None:
        return None

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    md_text = skill_md.read_text(encoding="utf-8")

    # 列出可用腳本
    scripts_lines: list[str] = []
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        for f in sorted(scripts_dir.iterdir()):
            if f.is_file() and f.suffix in (".py", ".sh"):
                scripts_lines.append(f"  - scripts/{f.name}")

    references_lines: list[str] = []
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        for f in sorted(ref_dir.iterdir()):
            if f.is_file():
                references_lines.append(f"  - references/{f.name}")

    assets_lines: list[str] = []
    assets_dir = skill_dir / "assets"
    if assets_dir.is_dir():
        for f in sorted(assets_dir.iterdir()):
            if f.is_file():
                assets_lines.append(f"  - assets/{f.name}")

    # 用 forward slash 讓 LLM 寫 Python 時不用處理 Windows escape
    abs_path = str(skill_dir.absolute()).replace("\\", "/")

    parts = [
        f"\n\n【掛載 Skill：{skill_name}】",
        f"**Skill 根目錄**：`{abs_path}`",
        f"存取子資源時請使用絕對路徑（上方根目錄 + 子路徑）。",
    ]
    if scripts_lines:
        parts.append(f"\n**可執行腳本（scripts/）**：\n" + "\n".join(scripts_lines))
        parts.append(
            "呼叫方式：`subprocess.run([sys.executable, \"<scripts 絕對路徑>\", args...])` "
            "或 `sys.path.insert(0, \"<scripts 絕對路徑>\")` 再 import。"
        )
    if references_lines:
        parts.append(f"\n**參考文件（references/）**：\n" + "\n".join(references_lines))
        parts.append("需要更多資訊時用 read_file 工具讀取這些文件。")
    if assets_lines:
        parts.append(f"\n**資源檔案（assets/）**：\n" + "\n".join(assets_lines))

    parts.append(f"\n--- SKILL.md 內容開始 ---\n{md_text}\n--- SKILL.md 內容結束 ---")

    parts.append(
        "\n\n【Skill 使用原則】\n"
        "SKILL.md 是這個 skill 的**使用說明書與入口**。正確的使用流程：\n"
        "\n"
        "1. **先讀懂 SKILL.md 全文** — 理解這個 skill 設計來解決什麼問題、推薦的工作流程、有哪些步驟需要依序執行\n"
        "2. **依照 SKILL.md 的指引行動** — SKILL.md 會告訴你「什麼情境下讀哪個 references 文件」「什麼步驟要跑哪個 scripts 腳本」「哪些 assets 要引用」。你的決策應來自 SKILL.md，而非自己的猜測\n"
        "3. **按需讀取子資源** — 不用一次讀完所有 references / scripts，按 SKILL.md 的指引在**當下需要**時才用 read_file 讀\n"
        "\n"
        "**不要做這些事**：\n"
        "- 不要**憑 skill 名稱或描述猜測它的實作方式**。skill 可能用非直覺的技術路線完成任務（例如一個處理某格式的 skill，不一定使用該格式的同名 pip 套件；可能改用 CLI 工具、檔案解壓、API 呼叫等其他手段）\n"
        "- 不要**跳過 SKILL.md 直接自己寫程式碼** — 可能忽略了 skill 設計時納入的關鍵步驟、驗證邏輯、或錯誤處理\n"
        "- 不要**預期任何外部套件存在**。若 SKILL.md 或 scripts/ 未明示某套件是依賴，請以系統的「已安裝 Python 套件」清單為唯一根據\n"
        "\n"
        "**執行時的優先順序**（依序判斷）：\n"
        "1. 若 scripts/ 有合適的腳本 → 直接呼叫（已驗證過，比現場寫更可靠）\n"
        "2. 若 SKILL.md 或 references/ **指示使用特定工具或命令**（例如 Node.js、npm 套件、CLI 工具、其他語言的 subprocess 等非 Python 方案）→ **照 SKILL 推薦的方式執行，不要擅自改用 Python**。可用 `run_shell` 呼叫任何可執行指令\n"
        "3. 若 SKILL 沒指示特定工具 → 自行撰寫 Python，只用已安裝的套件\n"
        "4. 不確定工具或套件是否可用 → 先用 `run_shell` 檢查（如 `node -v`、`where <cmd>`、`pip show <pkg>`）後再決定，不要盲目嘗試\n"
        "\n"
        "**關於互動式指示**：\n"
        "若 SKILL.md 描述包含「詢問使用者」「讓使用者選擇」「ask the user」等互動步驟，\n"
        "可以呼叫 **ask_user 工具**（見下方），把問題丟給使用者回答。請遵守以下原則：\n"
        "- 任務描述已指定選項 → **不要問**，直接以任務描述為準\n"
        "- 選項是可推論的合理預設 → **不要問**，直接用預設並在 summary 說明假設\n"
        "- 只有在**會嚴重影響結果**且無法推論時才用 ask_user（例如「要以哪個使用者身份執行」「要處理哪份機密資料」）\n"
        "- ask_user 有次數上限，用完就不能再問。**請珍惜使用**\n"
        "\n"
        "【ask_user 工具】\n"
        "用法：\n"
        "<tool>ask_user</tool>\n"
        "<input>{\n"
        "  \"question\": \"要輸出哪種格式的報告？\",\n"
        "  \"options\": [\"PDF\", \"Word\", \"Markdown\"],\n"
        "  \"context\": \"資料共 120 筆，標題為中文\"\n"
        "}</input>\n"
        "\n"
        "- `question`（必填）：問題本身，用中文\n"
        "- `options`（選填）：選項陣列；若提供，使用者介面會顯示成按鈕。若為純文字回答則省略此欄\n"
        "- `context`（選填）：幫助使用者做決定的背景資訊\n"
        "\n"
        "使用者回答後，工具會回傳 `使用者回答：<答案>`，你再依答案繼續任務。若逾時或被取消則回傳錯誤提示，此時請以合理預設完成或呼叫 done(success=false)。"
    )

    return "\n".join(parts)
