"""
win32_helpers — Outlook 自動化節點專用 wrapper API。

設計原則：
1. 只在 host（Windows）跑得起來。模組 import 時不直接 import pywin32，
   避免 sandbox（Linux）環境連 import 都炸。實際 COM 物件在函式內部 lazy import。
2. 提供 high-level 函式給 agent 直接呼叫，避開原生 COM 細節
   （例如 MAPI.GetDefaultFolder(6) 這類 magic number）。
3. 所有需要 Outlook 帳號 / Profile 的函式預期使用者已在 host 登入 Outlook。
   Outlook 沒開時會自動透過 COM 啟動。
4. 函式回傳結構化資料（dataframe / dict / Path）而非 COM 物件 ——
   COM 物件離開 host process 邊界就無效，回傳結構化資料才能讓 agent
   接 pandas / 寫檔等下游處理。

模組結構：
    win32_helpers/
        outlook.py    ← 寄信、收信、行事曆（Phase 1，主力）
        excel.py      ← 真 Excel 渲染、Macro、Power Query（Phase 2）
        word.py       ← Word COM（Phase 3）
        pptx.py       ← PowerPoint COM（Phase 3）
        _common.py    ← COM 連線管理、錯誤處理共用工具
"""
from __future__ import annotations

# 故意不在這裡 from .outlook import *
# Agent 在 host 上會直接 from win32_helpers.outlook import search_mail
# Sandbox 上 import win32_helpers 不會炸（因為這個 __init__ 沒做 lazy 之外的事）
__all__: list[str] = ["outlook"]
