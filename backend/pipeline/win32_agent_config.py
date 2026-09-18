"""
Outlook 自動化節點 — agent 執行環境的硬性限制（allowlist）。

設計原則：
  - agent 只能 import 這份清單裡的套件；其他 import 直接拒絕、要 agent 改用允許套件
    或回報「做不到」。
  - 限制不在 venv 層做（venv 是共用的、UI 隨時可加套件），改在 prompt + AST 兩層做。
  - AST 檢查是 belt-and-suspenders：就算 agent 偷渡 importlib.import_module 也擋不掉，
    但 90% 的 LLM 走正常 import，AST 一掃就抓到。

清單分類動機：
  1. **Windows 核心**：pywin32 / pywinauto / comtypes — 節點存在的理由
  2. **本專案 wrapper**：win32_helpers.* — 高階 API，agent 主要靠這個
  3. **資料整理**：pandas / openpyxl — 「整理信件成 xlsx」必備
  4. **HTML / 模板**：bs4 / jinja2 / markdown — 信件本文 / 報告產生
  5. **檔案產出**：python-docx / python-pptx / Pillow
  6. **標準庫**：re / datetime / pathlib / json / csv / html / email + 一般工具

不在清單的（明確排除）：
  - requests / httpx / urllib：對外 HTTP 是 skill 節點的事，不是 win32 specialist
  - selenium / playwright：瀏覽器自動化 → computer_use 或另開節點
  - subprocess.* 跑任意 exe：會繞過「只用 pywin32」邊界（要開 app 用 os.startfile / Shell.Application）
  - ML 套件（sklearn / torch / transformers）：用不到、又肥
"""
from __future__ import annotations

import ast
from typing import Optional


# ── Allowlist ─────────────────────────────────────────────────────────
# 比對規則：agent 寫 `import X` 或 `from X.Y import Z`，X 取頂層 module 名，
# 在這清單裡就 OK；否則拒絕。這份清單故意「寬」一點，因為例如 win32com 的子模組
# 很多 (win32com.client / win32com.gen_py)、一個一個列太瑣碎。

WIN32_AGENT_ALLOWED_IMPORTS: frozenset[str] = frozenset({
    # Windows 核心
    "win32com", "win32api", "win32gui", "win32con", "win32clipboard",
    "win32process", "win32event", "win32file", "win32service",
    "pythoncom", "pywintypes",
    "pywinauto", "comtypes",

    # 本專案 wrapper（agent 主力）
    "win32_helpers",
    "pipeline",  # 偶爾要 from pipeline.X import Y（極少用）

    # 資料整理
    "pandas", "numpy", "openpyxl",

    # Office 非 COM 讀寫（COM 太重的場景退這個）
    "docx",       # python-docx
    "pptx",       # python-pptx

    # HTML / Email / 模板
    "bs4",
    "jinja2",
    "markdown",
    "html",
    "email",
    "mimetypes",

    # 圖片
    "PIL",        # Pillow

    # 標準庫常用
    "re", "datetime", "pathlib", "json", "csv", "io", "os", "sys",
    "time", "collections", "typing", "dataclasses", "functools",
    "itertools", "logging", "string", "textwrap", "math",
    "decimal", "uuid", "hashlib", "base64", "binascii",
    "tempfile", "shutil", "glob", "fnmatch",
    "warnings", "traceback",
    "enum", "abc", "copy", "weakref",
    "operator",
    "__future__",
})


# ── AST import 檢查器 ─────────────────────────────────────────────────


class DisallowedImportError(ValueError):
    """Agent 寫的 code 嘗試 import 不在 allowlist 的套件。"""
    def __init__(self, module: str, line: int, suggestion: str = ""):
        self.module = module
        self.line = line
        self.suggestion = suggestion
        msg = (
            f"第 {line} 行 import {module}：此套件不在 Outlook 自動化節點允許清單。"
        )
        if suggestion:
            msg += f" 建議：{suggestion}"
        super().__init__(msg)


def _top_module(name: str) -> str:
    """`win32com.client` → `win32com`；`a.b.c` → `a`。"""
    return name.split(".", 1)[0]


def check_imports(
    code: str,
    allowed: Optional[frozenset[str]] = None,
) -> list[DisallowedImportError]:
    """掃 agent 提交的 Python code，回傳所有 disallowed import 的錯誤清單。

    回傳空 list = 全合法。否則 caller 應該把錯誤訊息回給 agent 要求改寫。

    支援：
      - import X
      - import X.Y
      - import X as Z
      - from X import a, b
      - from X.Y import a
      - from . import x（相對 import 跳過 — agent 不應該寫這個，但不擋）

    不支援（會放行）：
      - importlib.import_module("X")  ← 動態 import，AST 看不到
      - __import__("X")               ← 同上
        這兩個算 known limitation；prompt 會直接禁止使用。
    """
    allow = allowed if allowed is not None else WIN32_AGENT_ALLOWED_IMPORTS
    errors: list[DisallowedImportError] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        # SyntaxError 不是 import 錯誤，往上拋讓 caller 自己處理
        raise

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_module(alias.name)
                if top not in allow:
                    errors.append(DisallowedImportError(
                        module=alias.name,
                        line=node.lineno,
                        suggestion=_suggest_alternative(top),
                    ))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # 相對 import（from . / from ..）跳過
                continue
            if node.module is None:
                continue
            top = _top_module(node.module)
            if top not in allow:
                errors.append(DisallowedImportError(
                    module=node.module,
                    line=node.lineno,
                    suggestion=_suggest_alternative(top),
                ))

    return errors


def _suggest_alternative(top: str) -> str:
    """常見的 disallowed import 給建議。沒命中就回空字串。"""
    suggestions = {
        "requests": "對外 HTTP 抓資料請使用一般 Skill 節點，把結果存檔後再用本節點處理",
        "httpx": "同 requests — 此節點不負責對外網路請求",
        "urllib": "同上 — 對外連線請拆成獨立 Skill 節點",
        "urllib3": "同上",
        "aiohttp": "同上",
        "selenium": "瀏覽器自動化請用 computer_use 節點或獨立 web scraping 節點",
        "playwright": "同 selenium",
        "subprocess": "若是要開 app，請用 win32_helpers 或 win32com.client.Dispatch（COM 啟動）；"
                      "或 os.startfile() 用預設程式開檔。本節點禁止跑任意 exe",
        "sklearn": "ML 不在 Outlook 自動化節點範圍",
        "torch": "ML 不在 Outlook 自動化節點範圍",
        "transformers": "ML 不在 Outlook 自動化節點範圍",
        "scrapy": "爬蟲請用獨立節點",
        "smtplib": "本節點透過 Outlook COM 寄信（win32_helpers.outlook.send_mail）；"
                   "不直接用 SMTP 避免繞過 Outlook profile",
        "imaplib": "讀信請用 win32_helpers.outlook.search_mail（透過 Outlook 讀，不直連 IMAP）",
        "poplib": "同 imaplib",
    }
    return suggestions.get(top, "")


def format_errors_for_agent(errors: list[DisallowedImportError]) -> str:
    """把 import 錯誤組成給 agent 看的訊息（中文）。

    這個訊息會送回 agent 當下一輪 user message，讓它知道哪些 import 要拿掉、
    並提示替代方案。"""
    if not errors:
        return ""
    lines = ["[系統] 你的程式碼包含 Outlook 自動化節點不允許的 import："]
    for e in errors:
        lines.append(f"  · 第 {e.line} 行：`{e.module}`")
        if e.suggestion:
            lines.append(f"      建議：{e.suggestion}")
    lines.append("")
    lines.append("請選擇：")
    lines.append("  1. 改寫程式碼，只用允許的套件（以 win32_helpers / pywin32 / pandas 為主）")
    lines.append("  2. 如果這個需求真的需要被禁套件，呼叫 done(success=false) 並說明，"
                 "建議使用者把此步驟拆給一般 Skill 節點處理。")
    return "\n".join(lines)
