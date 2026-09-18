---
name: python-cli-extractor
description: 把使用者既有的 Python GUI / Streamlit / Flask / FastAPI 專案無破壞性接進 Atlas pipeline:在專案內新增單一 `main_CLI.py` 啟動檔(不動原本程式碼)、用 ask_user 互動選功能、選了就跑。第一次 ask_user 的選擇會被 Recipe 機制記住、下次快速模式 replay 同樣選擇、不再問。當使用者說「把這個 GUI 專案接進工作流」「我有現成的 Python app 想丟進 pipeline」「自動分析這個專案的功能讓我選一個跑」「把 X 變成可以在工作流呼叫的 CLI」這類訴求時都要觸發、不限於明說 "CLI extractor" 字樣。
---

# Python CLI Extractor — Atlas 互動式 + Recipe 友善

## 目的

讓使用者把現成的 Python 專案(通常是 GUI 起家、邏輯跟介面糾纏)接進 Atlas pipeline。**不破壞原專案任何可運作的檔案** — 預設只新增 `main_CLI.py`;若沙盒解耦需要動到耦合檔、改副本不改原檔。

## 兩種策略(分析完後讓使用者挑)

| 策略 | 適合場景 | 產出 |
|---|---|---|
| **A. 互動式單檔(預設、推薦給非工程使用者)** | 想用 Skill 節點掛載這 skill、互動選一個功能跑、依賴 Recipe replay | 一支 `main_CLI.py`(argparse subcommand)+ ask_user 互動 + run_shell 跑選中的 |
| **B. 多個獨立 CLI(給工程使用者手動拼工作流)** | 想自己拉 Script 節點、明確控制每個 step 跑哪支、不依賴 ask_user | 多支 `cli_tools/cli_<feature>.py`、本 skill 不幫跑、使用者自己拉節點 |

**選 A 還是 B 在第 3 步 ask_user 讓使用者挑**。

## 流程

1. 使用者掛載這 skill → batch 寫「跑我 `~/MyApp/` 專案」
2. **Step −1:冪等檢查 — 偵測 `<project>/main_CLI.py` 是否已存在**(retry 時超關鍵、省 4-5 個 iter)
3. **Step 0:偵測執行模式**(看 system prompt 有無 Sandbox 段、不問使用者)
4. 分析專案、識別 GUI 框架 + 可抽出功能
5. **用 `ask_user` 問使用者要 A 還是 B**
6. 走 A 路徑或 B 路徑(見下方各自的 Step)
7. 回 `done(success=true)`、summary 說明做了什麼 + 產出位置

## ⚙️ Iter 預算與工具紀律(必先看)

skill agent 每個 step 有 **20 個迭代上限**、每個迭代只能呼叫一個 tool。
ask_user 次數另有上限(預設模式 6 次、ask_mode ON 時無上限)。

Happy path 大致:冪等檢查(1)→ 模式偵測(0 個 tool、純讀 prompt)→ 掃描(2)→ A/B 問答(3)→ 寫檔(4)→ subcommand 問答(5)→ 參數問答(6)→ 執行(7)→ done(8)。

**絕對守則**:
1. **一次 reply 只發一個 `<tool>` 標籤** — 後端只跑第一個、剩的會被丟,任何「想批次」的衝動都壓住
2. **不要重覆掃描** — `**/*.py` 掃過就把結果記在心裡、下一輪別再掃一次
3. **模式偵測不花 ask_user** — Step 0 是讀 system prompt、不是問使用者。省下的 ask_user 額度留給 A/B、subcommand、參數
4. **碰到 retry(失敗歷史注入)→ 先讀失敗歷史內的錯誤訊息、直接修**:
   - 訊息含 `ModuleNotFoundError` → 直接 `done(success=false, missing_packages=["<pkg>"])`(系統會跳確認、裝後自動重跑)、**不要自己 pip install**、不要回頭問 A/B
   - 訊息含 `os.add_dll_directory` / `delvewheel` → 上次 run_shell 含 PYTHONPATH、拿掉重發
   - 訊息含 `command not found` → 改用對應 Linux 工具
   - 都不是上述 → 跑 Step −1 冪等檢查、再決定下一步
5. **Retry 時禁止行為**:不要重 `ask_user` 問 A/B、不要重 scan 專案。這些上輪已做過、答案藏在失敗歷史內、抄過來用就好。

## Step −1:冪等檢查(retry / 重跑時超關鍵)

**第一個 tool 呼叫永遠先跑這個**(在任何 ask_user 之前):

```python
# 用 run_python:
from pathlib import Path
import os
# 你應該從 batch / 失敗歷史 推得專案路徑;若 batch 沒明說、用 ask_user 問
project_root = Path("<推得的專案路徑、例如 /mnt/c/Users/X/MyApp>")
main_cli = project_root / "main_CLI.py"
if main_cli.exists():
    content = main_cli.read_text(encoding="utf-8", errors="ignore")
    print(f"EXISTS:{main_cli}|{len(content)}_bytes")
    # 印前 30 行讓 LLM 看 subcommand 結構
    print("\n".join(content.splitlines()[:30]))
else:
    print(f"NOT_EXISTS:{main_cli}")
```

**判讀**:
- 印出 `EXISTS:` → main_CLI.py 已寫好(可能是上輪寫的)、**直接跳 Step 5A 問使用者要跑哪個 subcommand**、不要重 scan、不要重寫
- 印出 `NOT_EXISTS:` → 真的是初次跑、從 Step 0 開始(偵測模式)
- 如果連 project_root 都不確定、第一個 tool 改成 `ask_user` 問專案路徑、拿到後再跑這個冪等檢查

## Step 0:**偵測**執行模式(不要問 — 用偵測)

⚠️ **不要 ask_user 問 host/sandbox**。執行模式是後端設定(`settings.skill_sandbox_mode`)決定的、**使用者在對話裡選什麼都不會真的切換模式**。如果你問了、使用者選了跟實際不符的、結果是路徑全錯(在容器寫 `C:\...`、或在 host 寫 `/mnt/c/...`),災難。

**正確做法 — 看你自己的 system prompt**:
- system prompt 內**有「🛡️ Sandbox 環境」這一段** → 你在 **sandbox(wsl_docker)模式** → 套用下方「沙盒鐵律」
- **沒有那段** → 你在 **host 模式** → 用 Windows 路徑、所有 import 都當得了

判定後**一句話告知**使用者(不需要他回答、不是 ask_user):
> 「偵測到目前為 sandbox 模式、我會照容器規則產 main_CLI.py。(若想用 host 完整環境、請到設定頁切換後重跑)」

然後直接接 Step 1。**不消耗 ask_user 次數**。

### 🚫 沙盒鐵律(偵測到 sandbox 模式才看)

1. **路徑一律 `/mnt/c/Users/X/...` 形式**、別用 `C:\Users\X\...`(bash 不認、會被當相對路徑接 CWD)
   - `write_file` 給 Windows 路徑沒事(後端會自動轉)
   - **`run_shell` 字串內**的路徑必須手動寫成 `/mnt/c/...`
2. **絕對不要設 `PYTHONPATH` 指向使用者 venv**(`...\.venv\Lib\site-packages`):
   - 那是 Windows 二進位、Linux 容器 import 就死
   - 容器**已預裝 pandas / openpyxl / requests / pillow** 等常用套件、**直接 import 就好**
   - 真有缺套件 → **不要自己 `pip install`**、直接 `done(success=false, missing_packages=["<pkg>"])`,系統會跳「允許安裝」確認、使用者同意後裝到容器、自動重跑(也別拿使用者 venv 補)
3. **`subprocess` 呼叫不要打 `powershell` / `cmd` / `where`**、只能用 Linux 工具
4. **`run_shell` 範例**(專案在 `external_projects/AI-/`):
   ```bash
   # ❌ 錯
   python C:\Users\X\Atlas\external_projects\AI-\main_CLI.py docx_report
   # ✅ 對
   python /mnt/c/Users/X/Atlas/external_projects/AI-/main_CLI.py docx_report
   ```
5. **🪜 碰到 ImportError → 立即爬樓梯、不要回到 Step 0/1/3 重來**

   ⚠️ **絕對守則**:run_shell 跑 main_CLI.py 撞 ImportError、**下一個 tool 必須是依下方 SOP 對應處理(缺套件 → `done(missing_packages)` 讓系統裝、PYTHONPATH 牽連 → 改命令)**、不要重 scan、不要重問 host/sandbox、不要重問 A/B、不要 read_file 看程式碼。retry 時看到失敗歷史有 `ModuleNotFoundError` 的、直接從這個 SOP 第一階開始、跳過 Step 0/1/3。

   ### SOP(依錯誤訊息直接對應)

   **訊息含 `ModuleNotFoundError: No module named 'X'`** → 容器沒裝該套件
   - ⚠️ **不要自己 `pip install`**(executor 會攔截、你也裝不進系統管控的環境)。直接 `done(success=false, missing_packages=["X"])`。
   - 系統會跳出「✅ 允許安裝 X」確認給使用者、同意後裝到容器、**自動重跑這步**(你不用管安裝、重跑後該套件就在了)。
   - 重跑後 X 仍 import 失敗(例如 X 需編譯 C 套件、linux 裝不起來)→ 進「Windows-only 判定」階

   **訊息含 `os.add_dll_directory` 不存在 / `_delvewheel_patch_` 字樣** → user 的 Windows venv 被 PYTHONPATH 灌進來
   - 檢查上一次 run_shell 是不是含 `PYTHONPATH=...\.venv\...`
   - 有 → 拿掉那段 export、直接重發 `python /mnt/c/.../main_CLI.py <subcmd> ...`
   - 沒有 → user 專案的 `sys.path.insert` 自己加了 venv、改 PYTHONPATH 蓋過:`run_shell("PYTHONPATH= python /mnt/c/.../main_CLI.py ...")`(把 PYTHONPATH 設空)

   **訊息含 `No module named 'win32com'` / `pywin32` / `pyodbc`** → Windows COM/驅動,容器永遠裝不上
   - 直接「Windows-only 判定」階

   **訊息含 `libtk` / `_tkinter` / `Cannot connect to display` / `no display name` / `customtkinter` / `PyQt` / `libvlc` / `vlc`** → GUI / 多媒體系統庫
   - ⚠️ **先別放棄、進「GUI 牽連解耦」階** — 多數情況這是「邏輯跟 GUI 寫在同一個檔、頂層 import 把 GUI 庫拖進來」的**牽連 import**、不是選中功能真的需要 GUI。

   ### 🔧 GUI 牽連解耦階(撞 GUI/多媒體庫時優先試這個、不要直接放棄)

   **目標**:CLI 要的是純邏輯(ffmpeg 轉檔、API 轉錄、資料處理),這些**執行時根本不碰 tkinter/vlc**。卡住只是因為 import chain 牽連。能解就解、撐到最後一刻。

   **Step A:判斷選中功能是不是「真的需要 GUI」**
   - 用 `read_file` 讀**選中 subcommand 對應的核心函式**源碼(只讀那個函式、不要整檔)
   - 看函式**本體**有沒有真的呼叫 GUI:`tk.Tk()`、`messagebox.*`、`CTk()`、`vlc.MediaPlayer()`、`.mainloop()` 等
     - 函式本體**沒碰** GUI(只是檔案頂層 import 了)→ 可解耦、進 Step B
     - 函式本體**真的碰** GUI(例如「播放影片」這種功能)→ 真的要 host、進「Windows-only 判定」階

   **Step B:在 `main_CLI.py` 開頭 stub 掉純 GUI 庫**(import 原專案模組「之前」)

   用「萬用 no-op stub」— 不只擋 import、連「邏輯函式被寫成 GUI window 類別的方法、
   要實例化整個視窗類別」也擋得住(`self.title()` / `self.geometry()` 全靜默):
   ```python
   import sys, types

   class _GuiStub:
       """萬用 GUI no-op:當基底類別時、任何 widget 方法(self.title/.geometry/.protocol…)都靜默回 _GuiStub。"""
       def __init__(self, *a, **k): pass
       def __getattr__(self, _n): return _GuiStub()
       def __call__(self, *a, **k): return _GuiStub()
       def __iter__(self): return iter(())
       def __setattr__(self, _n, _v): pass

   def _stub_module(name):
       m = types.ModuleType(name)
       m.__getattr__ = lambda _n: _GuiStub   # 模組層任意屬性(CTk / Tk / messagebox…)→ 回 _GuiStub 類
       return m

   for _m in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
              "tkinter.scrolledtext", "tkinter.font", "customtkinter", "vlc", "PIL.ImageTk"):
       if _m not in sys.modules:
           sys.modules[_m] = _stub_module(_m)
   # ↑ 之後 import 原專案模組不撞 GUI lib;連 class X(customtkinter.CTk) 都能實例化(空殼)
   from app.converter import convert_media
   ```
   改好 `main_CLI.py` 後重跑 run_shell。

   **Step C:stub 後仍失敗 → 看錯誤類型決定**
   - `ImportError` 還在 / 系統庫(libvlc 真的要播放)→ 進「Windows-only 判定」階
   - **邏輯函式是 GUI 類別的 method、且 method 內讀 `self.某widget.get()` 拿輸入** → stub 騙不過(widget 回的是 `_GuiStub`、不是真值)→ 進下方「抽方法副本」

   ### 抽方法副本(邏輯黏在 GUI window 類別、stub 騙不過時)

   重度 GUI 耦合的專案、核心邏輯常寫成 window 類別的 method(`class App(CTk): def run_ffmpeg(self): ...`)、
   而且 method 內直接讀寫 widget。這時 stub 只能讓它**構造空殼**、method 一讀 `self.entry.get()` 就拿到假值。

   解法 — **複製一份改副本**(不動原檔):
   1. `read_file` 讀那個 method 的完整本體(用 offset 分段讀大檔)
   2. 新增 `<project>/<模組>_cli.py`、把 method 本體**複製**成獨立純函式:
      - `self.entry_input.get()` / `self.var_xxx.get()` → 改成函式參數 `input_path` 等
      - `self.label.config(...)` / `messagebox.*` / `self.after(...)` → 改成 `print(...)` 或直接刪
      - method 內呼叫的「其他純函式」→ 照舊 import 原專案的(只抽會碰 widget 的那層)
   3. `main_CLI.py` 改 `from <模組>_cli import <抽出的函式>`、用 argparse 參數餵它
   4. **原專案任何 .py 都不動**、只新增 `main_CLI.py` + `<模組>_cli.py`

   ### 改寫原始碼的紀律(總則)

   - **可以**為了沙盒能跑而改寫 — 但**一律改副本**(`X.py` → `X_cli.py`)
   - **絕不修改原專案任何「目前可運作」的 .py** — 原檔保持原狀、使用者的 GUI 還要能跑
   - done summary 要說明:新增了哪些檔(`main_CLI.py` + 副本)、原檔 0 改動

   ### Windows-only 判定階(GUI 解耦也救不回、或 win32/COM 類才走)

   ```
   done({
     "success": false,
     "summary": "❌ Sandbox 容器無法執行此功能:<列出實際撞到的、例如 win32com、或「需真實 libvlc 播放影片」>\n\n已嘗試:\n- pip install <pkg> → <結果>\n- GUI 牽連解耦(sys.modules stub)→ <結果、為何救不回>\n\n👉 請去前端**設定頁**、把『Skill Sandbox Mode』改為 **host(本機模式)**、儲存後重跑本工作流即可。host 模式直接用你的 Windows venv / 系統庫、全部都會通。"
   })
   ```

   **絕對不要**碰到 ImportError 就直接 done(success=false);GUI/多媒體庫一定先試「解耦階」;也**絕對不要**碰到 ImportError 就回頭重 ask Step 0(host/sandbox)或 Step 3(A/B)— 那是浪費 iter 的災難。

## Recipe 機制 — 第二次跑就不用問

Recipe 機制(`backend/pipeline/executor.py` 內 `was_interactive` 旗標)會把**這次互動產生的整段流程**存成快取。具體運作:

- **第一次跑**:你的 SKILL agent → ask_user 等使用者回答 → 把答案組進後續 run_shell → 完成。系統把整段 tool 序列(write_file + ask_user + 答案 + run_shell)存成 recipe。
- **下次同個 workflow 跑(fast mode / recipe hit)**:Atlas 直接 replay 同樣的 run_shell 命令(含上次選的 subcommand)、**完全不重新呼叫 LLM、也不 ask_user**。使用者體感是「秒過」。
- 如果使用者**想換選項**:停用快速模式、或刪掉那 step 的 recipe(前端 Recipes 頁面),重跑就會再 ask 一次。

**對你的影響**:寫 main_CLI.py 跟 run_shell 命令時要**確定性**(同樣輸入永遠同樣輸出),不要隨機名稱、不要時間戳。否則 recipe 命中率會崩。

## Atlas 工作流慣例(寫 main_CLI.py 必守)

- **CWD = workflow 資料夾**(`ai_output/<workflow_name>/`),Script subprocess 跑在這
- 輸出用**相對路徑**寫(`open("result.csv", "w")` / `Path("out.json").write_text(...)`)→ 落 workflow dir → 下游 `{{ steps.X.output.path }}` 自動接到
- 寫死絕對路徑 / 寫到 `__file__` 旁 = ❌(下游接不到)
- 結束時 `print` 結果路徑、exit 0 = 成功

## 執行流程

### Step 1:確認專案路徑

如果 batch 沒明說、用 `ask_user`:
```
ask_user({
  "question": "你要接進工作流的 Python 專案在哪?",
  "context": "請給完整路徑、例如 C:\\Users\\X\\MyApp 或 ~/MyApp"
})
```

### Step 2:掃描識別

⚠️ **掃描必須排除虛擬環境 / 第三方套件目錄**、否則會把幾千個第三方 `__main__.py` 全拉進 context、撞 LLM token 上限直接 400。

**排除清單(必跑)**:
```
.venv  venv  env  virtualenv  .env-*
node_modules  __pycache__  .git  .pytest_cache  .mypy_cache  .ruff_cache
build  dist  *.egg-info  .tox
```

**正確掃法範例**(直接抄)、用 `run_python`:
```python
from pathlib import Path
EXCLUDE = {".venv", "venv", "env", "virtualenv", "node_modules",
           "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
           ".ruff_cache", "build", "dist", ".tox"}

def scan(root: Path):
    for p in root.rglob("*.py"):
        # 任一中段目錄在排除清單 → 跳過
        if any(part in EXCLUDE or part.endswith(".egg-info") for part in p.parts):
            continue
        # 排除 hidden 目錄(以 . 開頭、但 . 跟 .. 不算)
        if any(part.startswith(".") and part not in (".", "..") for part in p.relative_to(root).parts[:-1]):
            continue
        yield p

files = list(scan(Path("/mnt/c/Users/X/MyApp")))
print(f"{len(files)} python files (excluding venv/deps)")
for f in files[:50]:  # 只 print 前 50 個、再多沒意義
    print(f.relative_to(Path("/mnt/c/Users/X/MyApp")))
```

掃完用 `read_file` 看**入口檔**(`main.py` / `app.py` / `gui.py` / `streamlit_app.py` / `__main__.py`)及 `core/` / `logic/` / `utils/` 等模組。

**控制 read 數量**:一次最多 read 10 個檔(每個 < 500 行);若專案大、優先 read entry + import depth ≤ 2 的模組、不要全 read。

### 🚨 Windows-only 執行期依賴預檢(sandbox 模式:**讀完源碼後立刻做、別等到跑才發現**)

讀源碼時**除了看 import、也要看函式體內的執行期呼叫**。若核心功能**靠 Windows-only 機制**才能完成,Linux 沙盒容器永遠跑不了 —— 這時**不要往下問參數、不要寫 main_CLI.py、不要試跑**,直接走下方「Windows-only 判定階」`done(success=false)` 引導使用者切 host。早期 fail-fast + 明確引導,遠勝填完參數後在最後一刻撞 `FileNotFoundError: 'powershell'`。

**偵測訊號**(出現任一、且該功能核心就靠它):
- `subprocess` / `os.system` / `os.popen` 字串含 `powershell` / `pwsh` / `cmd /c` / `wmic` / `reg ` / `where ` / 某個 Windows `.exe`
- `import winrt` / `Windows.Media.*`(WinRT)、`win32com` / `win32api` / `pywin32` / `comtypes`、`ctypes.windll` / `ctypes.WinDLL`
- 寫死的 Windows 系統路徑(`C:\Windows\System32\...`)

> 範例(就是這次 SSD 失敗的型態):OCR 工具用 `subprocess.run(["powershell", ... Windows.Media.Ocr ...])` 做辨識 → 沙盒沒 powershell / WinRT → **預檢階段就判 Windows-only**,直接告知「請到設定頁切 host 重跑」,**不要**再 `ask_user` 問關鍵字 / 圖片路徑。

⚠️ **分辨核心 vs 順手**:OCR 工具的辨識本體靠 WinRT = 核心 → 判 Windows-only。若只是某非必要分支用到、主功能不靠它 → 照常解耦、那分支略過即可、別誤判整個專案。

**識別 GUI 框架**(看 import):
| import | 框架 |
|---|---|
| `tkinter` / `customtkinter` | Tkinter |
| `PyQt5` / `PyQt6` / `PySide2` / `PySide6` | PyQt |
| `streamlit as st` | Streamlit |
| `flask` / `fastapi` | Web backend |

**識別「可抽出功能」**(找有清楚輸入 / 輸出的純運算函式):
- Tkinter:`self.btn_xxx.config(command=self.on_click_xxx)` → `on_click_xxx` callback 內呼叫的核心邏輯
- Streamlit:`if st.button("X"): result = func(args)` → `func`
- PyQt:`self.btn.clicked.connect(self.handle_X)` → `handle_X`
- Flask/FastAPI:`@app.route("/xxx")` / `@app.post("/yyy")` → route handler

**抽出後存成清單**:`[{"name": "clean_data", "module": "core.cleaner", "func": "clean_data", "args": ["input_path", "output_path"]}, ...]`

### Step 3:**問使用者選策略 A / B**

```
ask_user({
  "question": "找到 N 個可抽出功能(列出)。要怎麼接進工作流?",
  "options": [
    "A. 互動式單檔 — 產一支 main_CLI.py、待會問我選一個跑、Recipe 會記住下次秒過(推薦)",
    "B. 多個獨立 CLI — 產多支 cli_<功能>.py、我自己在 canvas 拉 Script 節點接、不靠互動"
  ],
  "context": "A 適合『一個工作流就跑專案一個功能』情境;B 適合『工作流會用到專案好幾個功能』、自己拉節點精確控制"
})
```

依使用者回答走 **Step 4A** 或 **Step 4B**。

### Step 4A:策略 A — 寫 `main_CLI.py`(0 改動原專案)

用 `write_file` 在**專案根**(不是 `cli_tools/` 子資料夾、就是專案根、跟原 main.py 並列)寫 `main_CLI.py`,內容是 **argparse subcommand 統一啟動檔**:

```python
"""統一 CLI 入口、由 Atlas 的 python-cli-extractor skill 自動產生。
本檔案是「新增」、原專案任何 .py 都沒動。可以安全刪除。

用法:
  python main_CLI.py <subcommand> [--args ...]
  python main_CLI.py --help                # 列出所有 subcommand
"""
import sys
import argparse
from pathlib import Path

# 把專案根加進 sys.path、確保 import 原專案模組能找到
sys.path.insert(0, str(Path(__file__).parent))

# 從原專案 import 核心邏輯(不複製、直接 import 重用)
from core.cleaner import clean_data
from core.analyzer import analyze
# ...其他被選中的函式

def cmd_clean(args):
    """清資料 — 對應原專案的 clean_data() 函式"""
    out_path = args.output or "cleaned.csv"
    clean_data(args.input, out_path)
    print(f"✓ 完成、輸出:{out_path}")

def cmd_analyze(args):
    """分析 — 對應原專案的 analyze() 函式"""
    out_path = args.output or "analysis.json"
    result = analyze(args.input)
    import json
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"✓ 完成、輸出:{out_path}")

def main():
    parser = argparse.ArgumentParser(description="統一 CLI 入口")
    sub = parser.add_subparsers(dest="command", required=True, help="選一個功能跑")

    # === clean 子命令 ===
    p_clean = sub.add_parser("clean", help="清資料")
    p_clean.add_argument("--input", required=True)
    p_clean.add_argument("--output", default=None, help="預設 cleaned.csv(相對 CWD)")
    p_clean.set_defaults(func=cmd_clean)

    # === analyze 子命令 ===
    p_an = sub.add_parser("analyze", help="分析")
    p_an.add_argument("--input", required=True)
    p_an.add_argument("--output", default=None)
    p_an.set_defaults(func=cmd_analyze)

    # ...其他子命令

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
```

### 處理 GUI 耦合(核心邏輯被綁在 UI 類別)

許多 callback 不是純函式、它從 UI 元件抓值。寫 main_CLI.py 時:

| 原寫法 | 替換成 |
|---|---|
| Tkinter:`self.entry_input.get()` | argparse `--input` |
| Streamlit:`st.file_uploader(...)` | argparse `--input` 接檔案路徑 |
| Streamlit:`st.session_state["X"]` | argparse 參數 / 中繼檔讀取 |
| PyQt:`self.lineEdit.text()` | argparse `--text` |
| 寫回 UI:`st.success(...)` / `self.label.setText(...)` | `print(...)` |

**如果核心邏輯跟 UI 完全黏在一起**(callback 內直接讀寫 UI 元件、無法剝離):
- 用 `ask_user` 告知使用者:「`<function_name>` 的邏輯跟 UI 元件直接耦合(`self.X.get()` 之類)、無法不改動原專案抽出。要怎麼處理? A) 略過這個功能 B) 我直接寫個 wrapper 進 `main_CLI.py` 把 UI 邏輯複製過來(仍不改原專案)」

**import-time 牽連耦合(頂層 `import tkinter` 之類把 GUI 庫拖進來)**:
這跟上面「callback 抓 UI 值」不同 — 邏輯函式本身是純的、只是檔案頂層 import 了 GUI。
→ 不需要問使用者、直接用上方「🔧 GUI 牽連解耦階」的 `sys.modules` stub 處理。

### Step 5A:用 `ask_user` 讓使用者挑要跑哪個 subcommand

```
ask_user({
  "question": "我從你專案找到這幾個可用功能、選一個跑:",
  "options": ["clean: 清資料", "analyze: 分析", "train: 訓練模型"],
  "context": "選了之後我會跑 python main_CLI.py <你選的> --input ... 。下次同樣工作流跑、會自動 replay 你的選擇、不再問。"
})
```

⚠️ **`options` 一定要填、每個 subcommand 列一項、絕對不可留空 `[]`**(最常見錯誤、務必遵守):
前端 / TG 是靠 `options` 陣列把「可點選的功能按鈕」顯示給使用者。**options 留空 → 使用者只看到「自由輸入」、完全不知道有哪些功能可選**,等於沒列功能、體驗很差。
- main_CLI.py 有幾個 subcommand,options 就要有幾項,格式 `"<subcommand>: <一句話說明>"`
- 例:main_CLI.py 有 `random` / `sequence` → **options 必須是** `["random: 隨機產生數字", "sequence: 產生等差序列"]`、不可寫 `[]`
- 你剛剛 Step 2 掃描識別出的「可抽出功能清單」裡每一項,都要變成一個 option

如果使用者選項需要額外參數(input 路徑、輸出位置等),**再用一次 ask_user** 問:
```
ask_user({
  "question": "clean 需要輸入 CSV 路徑、給我絕對路徑或工作流資料夾內的相對檔名",
  "context": "之後會跑 python main_CLI.py clean --input <你給的> --output cleaned.csv"
})
```

**節制使用 ask_user**:能從 batch 推論的、能用合理預設的(例如 output 用 `cleaned.csv`)→ 不要問。**ask_user 有次數上限**。

### Step 6A:跑使用者選的 subcommand

```
run_shell({
  "input": "python /full/path/to/main_CLI.py clean --input data.csv --output cleaned.csv"
})
```

注意 working_dir = workflow 資料夾,所以:
- `--input data.csv` → 系統在 workflow dir 找這檔(如果是上一步輸出、就在那邊)
- `--output cleaned.csv` → 結果落 workflow dir、下游可用 `{{ steps.X.output.path }}` 接

### ⚠️ 原專案輸出含時間戳 / 隨機名 → main_CLI.py 收尾要固定它

**只在這種情況才處理**(條件式、不是每個專案都要):

寫 main_CLI.py 的 `cmd_xxx` 時、看被包的函式**怎麼決定輸出檔名**:
- 函式輸出**固定檔名**(回傳 / 印出固定路徑)→ 什麼都不用做、直接用
- 函式內部用 `f"out_{datetime.now()}.csv"` / `uuid` / 時間戳決定檔名 → **main_CLI.py 收尾多一步**:
  把函式實際產出的檔 **copy** 成 `--output` 指定的固定名,再回報固定名。

```python
def cmd_convert(args):
    real_out = convert_media(args.input)   # 假設這函式回傳含時間戳的路徑
    import shutil
    fixed = args.output or "converted.mp3"
    shutil.copy(real_out, fixed)           # copy 不是 move — 保留原時間戳檔、不破壞「留歷史」設計
    print(f"✓ 完成、輸出:{fixed}")
```

為什麼:時間戳檔名每次跑都不同 → 下游 `{{ steps.X.output.path }}` 接不穩、Recipe replay 記的舊路徑會過時。固定名是 pipeline 該有的契約。`copy` 不破壞原專案行為。

---

## 策略 B 路徑 — 多個獨立 CLI

### Step 4B:用 `ask_user` 讓使用者挑要拆哪幾個功能

```
ask_user({
  "question": "B 路徑:選要拆成獨立 CLI 的功能(可多選、逗號分隔)",
  "options": ["1. clean", "2. analyze", "3. train", "4. report", "全部"],
  "context": "每個會產一支 cli_tools/cli_<feature>.py、可以在 Script 節點分別呼叫"
})
```

### Step 5B:對每個選中功能寫一支 `cli_<feature>.py`

放在 `<project_root>/cli_tools/`(資料夾不存在就建)。每支只負責一個功能:

```python
"""cli_clean.py — 對應原專案的 clean_data() 函式。由 python-cli-extractor 自動產生。"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 把專案根加進 sys.path
from core.cleaner import clean_data                    # 直接 import、不複製邏輯

def main():
    parser = argparse.ArgumentParser(description="清資料")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="cleaned.csv")
    args = parser.parse_args()
    clean_data(args.input, args.output)
    print(f"✓ 完成、輸出:{args.output}")

if __name__ == "__main__":
    main()
```

### Step 6B:用 `run_shell` 各跑一次 `--help` 驗證

```
run_shell({"input": "python /path/cli_tools/cli_clean.py --help"})
run_shell({"input": "python /path/cli_tools/cli_analyze.py --help"})
```

確保 argparse 語法 OK、import 解得到。如果 `--help` 出錯、修到通(或 ask_user 確認跳過)。

### Step 7B:給使用者**工作流 YAML 範例**

在 done summary 內附:

```yaml
# 把這幾步貼到 Atlas 工作流(每個 Script 節點一個 step)
- name: 清資料
  batch: python /full/path/to/cli_tools/cli_clean.py --input data.csv --output cleaned.csv
  output:
    path: cleaned.csv

- name: 分析
  batch: python /full/path/to/cli_tools/cli_analyze.py --input {{ steps.清資料.output.path }}
  output:
    path: analysis.json
```

**B 路徑不主動跑使用者選的功能** — 你的職責到此為止、產出 cli 檔 + 給範例就 done。使用者自己回到 canvas 拉節點。

---

### Step 7:`done`(兩條路徑都用)

**策略 A done summary 範本**:
```
✓ 已分析 ~/MyApp(Tkinter 專案)、產 main_CLI.py 統一入口
   本次選擇:clean 子命令、輸出 cleaned.csv(N bytes)
   下次同工作流跑:Recipe 會 replay clean 子命令、自動跳過 ask_user
   想換選項:Recipes 頁刪掉本 step 的快取、重跑會再問
```

**策略 B done summary 範本**:
```
✓ 已分析 ~/MyApp(Tkinter 專案)、產 3 支獨立 CLI:
   - cli_tools/cli_clean.py
   - cli_tools/cli_analyze.py
   - cli_tools/cli_train.py
   全部 --help 通過。請回 canvas 拉 Script 節點、上方 YAML 範例可直接複製到 batch 欄。
```

## Recipe 友善的寫法(讓 fast mode replay 順)

1. **`main_CLI.py` 內容固定** — 用同樣的專案、應該生出 byte-byte 一致的 main_CLI.py(用 sorted 處理 import 順序)
2. **subcommand 對應固定** — `clean` 永遠指向 `clean_data`、不要隨機
3. **不依賴環境變數 / 時間戳** — 不要在 main_CLI.py 寫 `f"output_{datetime.now()}.csv"` 之類、會讓每次跑 output path 不同、recipe 失效
4. **使用者答案的處理一致** — 同樣 ask_user 問題、使用者選同樣選項、下次 replay 走同樣 run_shell

## 設計原則

1. **不破壞原專案的可運作檔案** — 預設只新增 `<project_root>/main_CLI.py`。**絕不修改任何「目前能正常運作」的原始 .py**(使用者的 GUI 還要能跑)。
   - 但**允許**:為了讓功能在沙盒跑、可以**複製**某支耦合過深的檔案改副本(`converter.py` → `converter_cli.py`)、`main_CLI.py` 指向副本。原檔保持原狀。
2. **重用 import、不複製邏輯** — main_CLI.py 是薄殼、真正運算優先 `from <user_project>.X import Y` 走原專案 code;只有解耦需要時才改副本。
3. **撐到最後一刻才放棄** — 沙盒撞 GUI/系統庫、先試解耦(sys.modules stub / 改副本)、真的救不回才 `done(success=false)` 要使用者切 host。
4. **互動但節制** — `ask_user` 只用在「真不知道」的時候,該推論的就推論
5. **Recipe 第一**:寫的 main_CLI.py 內容要確定性,run_shell 命令要可 replay
6. **完成回報用 done** — `success=True/False` + 一句話 summary,使用者前端會看到

## 跑完後給使用者的提示(在 done summary 內)

```
✓ 已分析 ~/MyApp 並接進工作流
- 新增檔案:~/MyApp/main_CLI.py(原專案 0 改動)
- 本次選擇:clean 子命令
- 下次同個工作流跑時、Recipe 會 replay clean 子命令、自動跳過 ask_user
- 想換選項:刪除這 step 的 recipe(Recipes 頁面)、重跑即可
```
