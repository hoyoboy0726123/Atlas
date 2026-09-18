# Atlas

用白話描述你想自動化的事,Atlas 的 AI 助手會幫你把它變成一條可重複執行的工作流 —— 在畫布上看得到每一步、可以手動調整、可以排程、跑完留下紀錄。

AI 模型可以用雲端(Groq / Gemini / OpenAI / Anthropic),也可以完全在本機用 Ollama 跑,資料不必離開你的電腦。

## 能做什麼

工作流由「節點」串成,常用的有:

| 節點 | 用途 |
|---|---|
| 腳本 | 執行你現有的 `.py` / `.bat` / shell 腳本 |
| AI 技能 | 白話描述任務,AI 寫程式並執行;第二次起走快取,幾乎不花 token |
| 多代理 | 多個專業角色分工研究、分析、審查、撰寫 |
| 條件分支 | 依上一步的結果走不同路(if / switch) |
| 人工確認 | 暫停等你在 Telegram 點頭,再繼續不可逆的動作 |
| 網頁爬蟲 | 把網頁轉成乾淨內容,支援需要登入或有防護的網站 |
| Outlook 自動化 | 收發信、附件、行事曆(需要本機安裝 Outlook) |
| 視覺驗證 | 讓視覺模型看產出的畫面,判斷符不符合預期 |
| 桌面自動化 | 操作沒有 API 的軟體:錄製滑鼠鍵盤,或直接挑選畫面上的元件(Windows) |

另外還有:排程執行、Telegram 通知與遙控、把工作流開放給其他 AI 用的 MCP server。

## 系統需求

- **Windows 10 / 11**(建議)。macOS / Linux 可以跑大部分功能,但桌面自動化節點只支援 Windows。
- **Python 3.11 ~ 3.13**(3.14 目前還不能用)
- **Node.js 18 以上**
- **[uv](https://docs.astral.sh/uv/)**(建議,安裝最快;沒有也可以用 pip)
- **[Ollama](https://ollama.com/)**(選配,想用本機模型才需要)

## 安裝與啟動

### Windows:一鍵啟動

```bat
git clone <this-repo-url> Atlas
cd Atlas
launch.bat
```

第一次執行會自動安裝後端與前端的依賴(有 uv 就用 uv,沒有就用 pip),需要幾分鐘。之後每次執行只會啟動服務。

啟動後打開 **http://localhost:3012**。

### 用 uv 手動安裝

```bash
cd backend
uv sync                      # 依 pyproject.toml / uv.lock 建立 .venv 並安裝依賴
cp .env.example .env         # Windows 用 copy
uv run uvicorn main:app --host 127.0.0.1 --port 8014
```

另開一個終端機啟動前端:

```bash
cd frontend
npm install
npx next dev --port 3012
```

### 用 pip 手動安裝

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate       # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
```

之後同上啟動後端與前端。macOS / Linux 也可以直接執行 `./start.sh`。

## 設定 AI 模型

第一次使用時不用先設定:直接在首頁輸入你想自動化的事並送出,如果還沒有可用的模型,Atlas 會顯示設定指引,設定好後自動送出你剛剛的訊息。它也會偵測這台電腦現成可用的模型(已安裝的 Ollama 模型、已登入的 Claude Code),可以一鍵改用。

之後要換模型,到 **設定頁**(http://localhost:3012/settings)選擇主模型的供應商與模型。

### Google Gemini(預設)

預設使用 Gemini 的 `gemini-3.5-flash-lite`,回應快、免費額度大;要規劃複雜的工作流時,建議改用 Claude 訂閱。到 [Google AI Studio](https://aistudio.google.com/apikey) 免費建立一把 API Key,貼到設定指引的輸入框即可;Atlas 會先向 Google 確認金鑰有效,再存進 `backend/.env`,不需要重新啟動。

### 其他雲端模型

在 `backend/.env` 填入 API Key,重新啟動後端,再到設定頁選擇供應商與模型:

| 供應商 | 環境變數 |
|---|---|
| Google Gemini | `GEMINI_API_KEY` |
| Groq | `GROQ_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |

### Claude 訂閱(Claude Code)

已經有 Claude Pro / Max 方案的話,可以直接用訂閱額度,不需要 API Key:

```bash
npm install -g @anthropic-ai/claude-code
claude          # 照畫面指示登入
```

登入後在設定指引按「改用並送出」,或到設定頁把供應商選成 Claude 訂閱。

### 本機模型(Ollama)

裝好 Ollama、下載模型後,在設定指引按「改用並送出」,或到設定頁把供應商選成 **Ollama**,不需要任何 API Key,資料不會離開你的電腦。

以下是實際測試過、能擔任 AI 助手的最小建議配置:

| 模型 | 下載指令 | 顯示記憶體(context 65536 時實測) |
|---|---|---|
| Gemma 4 12B | `ollama pull gemma4:12b` | 9.2 GB,建議 12 GB 顯示卡 |
| Qwen3.8 27B | `ollama pull qwen3.8:27b` | 18.4 GB,建議 24 GB 顯示卡 |

- 顯示記憶體足夠的話建議用 Qwen3.8 27B:需要 AI 助手直接幫你修改工作流時,較大的模型明顯可靠。
- 設定頁的 **context 長度**預設 65536,不要調低。AI 助手一輪要送 1.4～4 萬 tokens 的提示詞加上工具定義與對話,Ollama 塞不下時不會報錯,會直接截掉提示詞前半,模型就會漏掉大部分規則。
- 模型第一次回應需要先載入顯示卡,會比較慢,之後就正常。

## 選配功能

- **Telegram**:在設定頁填入 Bot Token 與 Chat ID,就能收到工作流完成通知、在手機上核准「人工確認」節點,也可以開啟遠端遙控。
- **Skill 沙盒**:AI 技能產生的程式預設直接在本機執行。想隔離執行的話,執行 `sandbox\setup_sandbox.bat` 建立 WSL + Docker 沙盒容器,再到設定頁切換成沙盒模式。
- **網路搜尋**:在設定頁填入 [Tavily](https://tavily.com/) API Key 並開啟,AI 技能與多代理就能查即時資料。

## 安全須知

- 後端預設只接受本機連線(`127.0.0.1`)。它的 API **沒有登入驗證**,而且會執行 AI 產生的程式、操作你的桌面,請不要直接開放到網路上。確實需要區網存取時,啟動前設定環境變數 `ATLAS_HOST=0.0.0.0`,並自行評估風險。
- API Key 只放在 `backend/.env`,這個檔案已被 `.gitignore` 排除,不會被提交。
- 桌面自動化執行時會真的移動滑鼠、按鍵盤。跑到一半要停,半秒內連按兩次 `Esc`,或把滑鼠甩到螢幕左上角。

## 授權

[MIT](LICENSE)
