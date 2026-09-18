"""主機系統工具健康檢查 + 安裝指令提示。

某些 backend 功能需要 host 端裝有特定工具(非 pip 能裝):
- LibreOffice(soffice)— file_preview 渲染 pptx/docx 真實版面、visual_validation 必須
- FFmpeg — 影片處理(web_crawler video 模式)

不列的:
- Tesseract:backend OCR 走 Windows.Media.Ocr(內建、零外部依賴)、不用 Tesseract
- Poppler:PDF 走 pypdfium2(pip 套件)、不需 binary

設計:跑時用 shutil.which / 常見路徑 detect,給每個工具:
- installed: bool
- found_at: 找到的路徑(沒裝就 None)
- install_cmd: 各平台建議安裝指令
- why: 哪些功能需要它(用來決定 user 該不該裝)
"""
from __future__ import annotations
import os
import platform
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class HostTool:
    name: str           # 顯示名 "LibreOffice"
    bin: str            # 主要 binary 名 "soffice"
    installed: bool
    found_at: Optional[str]
    install_cmd: dict   # {"windows": "winget install ...", "macos": "brew install ...", "linux": "apt install ..."}
    why: str            # "渲染 pptx/docx 真實版面 — visual_validation / 預覽必須"
    required: bool      # True = 沒裝會有功能完全不能用;False = 退化但仍可用


def _find_libreoffice() -> Optional[str]:
    """重用 file_preview 的探測邏輯"""
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    # Windows 常見路徑
    if platform.system() == "Windows":
        for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
            if not root:
                continue
            guess = Path(root) / "LibreOffice" / "program" / "soffice.exe"
            if guess.exists():
                return str(guess)
    # macOS 常見路徑
    if platform.system() == "Darwin":
        guess = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if guess.exists():
            return str(guess)
    return None


def _find(bin_name: str) -> Optional[str]:
    return shutil.which(bin_name)


def get_host_tools() -> list[HostTool]:
    """掃描所有 backend 用得到的 host 系統工具、回狀態 list。"""
    tools: list[HostTool] = []

    # LibreOffice — 最重要、visual_validation pptx/docx 必須
    lo_path = _find_libreoffice()
    tools.append(HostTool(
        name="LibreOffice",
        bin="soffice",
        installed=bool(lo_path),
        found_at=lo_path,
        install_cmd={
            "windows": "winget install -e --id TheDocumentFoundation.LibreOffice --source winget",
            "macos": "brew install --cask libreoffice",
            "linux": "sudo apt install libreoffice",
        },
        why="渲染 pptx / docx 真實版面 — 視覺驗證 / 人工確認預覽必須(沒裝會用純文字 PNG、VLM 評不過)",
        required=True,
    ))

    # FFmpeg — web_crawler 影片模式
    ff_path = _find("ffmpeg")
    tools.append(HostTool(
        name="FFmpeg",
        bin="ffmpeg",
        installed=bool(ff_path),
        found_at=ff_path,
        install_cmd={
            "windows": "winget install -e --id Gyan.FFmpeg --source winget",
            "macos": "brew install ffmpeg",
            "linux": "sudo apt install ffmpeg",
        },
        why="web_crawler 影片模式(yt-dlp 合併 stream)— 沒裝部分影片格式抓不到",
        required=False,
    ))

    return tools


def check_host_tools_summary() -> str:
    """開機健康檢查用的單行訊息;沒裝 required tool → 警告字串、全裝 → 空字串。"""
    missing_required = [t for t in get_host_tools() if t.required and not t.installed]
    if not missing_required:
        return ""
    names = ", ".join(t.name for t in missing_required)
    return f"⚠ Host 缺少必要工具:{names}(部分功能會降級)"


def get_libreoffice_status() -> tuple[bool, Optional[str]]:
    """快速問:LibreOffice 裝了沒?回 (installed, path)"""
    p = _find_libreoffice()
    return bool(p), p
