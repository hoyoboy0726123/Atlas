# Atlas — Skill 執行沙盒
#
# 用途：LLM 生成的 Python / Shell 程式碼在此容器內執行，避免直接碰 Windows host。
# 原則：只裝「純運算 / I/O」類套件。桌面自動化（pyautogui / mss / pynput）走不了
# 容器（沒有 display），保留在 host 上跑。
#
# 分層策略：故意把 pip install 拆成多個 RUN，每層獨立釋放記憶體，避免在
# WSL2 預設 8GB 下一次壓縮 20 個套件導致 buildkit OOM（SIGBUS）。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── System：locale + ca-certificates + curl ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        locales ca-certificates curl \
    && sed -i '/zh_TW.UTF-8/s/^# //g; /en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8

# ── Tier 1：核心數值/文件處理（小而快）──
RUN pip install --no-cache-dir \
    pandas openpyxl numpy python-dateutil pytz defusedxml Pillow python-docx

# ── Tier 2：HTTP/解析（中量）──
RUN pip install --no-cache-dir \
    requests beautifulsoup4 lxml "httpx[http2]"

# ── Tier 3a：繪圖（matplotlib + 中文字型）──
# 單獨一層，讓 layer commit 的資料量不會跟 Tier 3b 疊加。
# 中文字型 fonts-noto-cjk = Google Noto Sans CJK（繁中/簡中/日/韓 一包搞定，~50MB）。
# 沒裝會狂吐 "Glyph XXXX missing from font(s) DejaVu Sans" + 圖中文變豆腐□。
# 字型先裝、matplotlib 後裝，這樣 font cache 第一次建時就會掃到 Noto CJK。
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir matplotlib
# 設 matplotlibrc 預設字型鏈：Noto CJK 排前、DejaVu 殿後當 ASCII fallback。
# axes.unicode_minus=False 解決中文圖負號變成 □ 的舊問題（matplotlib FAQ 標準解）。
# 這樣 LLM 寫 plt.plot(...) 不用每次自己 set_rcParams 也能正確顯示中文。
RUN MPLRC=$(python -c "import matplotlib; print(matplotlib.matplotlib_fname())") \
    && echo "font.family: sans-serif" >> "$MPLRC" \
    && echo "font.sans-serif: Noto Sans CJK TC, Noto Sans CJK SC, DejaVu Sans, sans-serif" >> "$MPLRC" \
    && echo "axes.unicode_minus: False" >> "$MPLRC"

# ── Tier 3b：影像處理（opencv headless，最大的單一套件）──
RUN pip install --no-cache-dir opencv-python-headless

# ── Tier 4：文件產出（PPT / PDF / Markdown）──
# python-pptx：Python 原生產 PPT；pdfplumber：PDF 文字抽取
# markdown：Python-Markdown，Markdown → HTML(報表常用);與 markdownify(HTML→MD) 互補、別搞混
RUN pip install --no-cache-dir python-pptx pdfplumber markdown

# ── Tier 5：爬蟲進階（新聞 + Cloudflare 繞過 + RSS + UA rotation）──
# newspaper3k 會拉 nltk / tldextract / feedparser（~100MB）；cloudscraper 繞 CF 5s 挑戰
# lxml_html_clean：lxml 5.x 後 html.clean 被拆成獨立套件，newspaper3k 需要
# fake_useragent：隨機 User-Agent 避免網站封 bot（比自己寫 UA 表簡單）
RUN pip install --no-cache-dir newspaper3k cloudscraper feedparser lxml_html_clean fake_useragent

# ── Tier 5b：通用網頁爬蟲（web_crawler 節點用）─────────────
# Crawl4AI = LLM-optimized 爬蟲：Playwright + Chromium + stealth + markdown 直出
# trafilatura：Tier 2（FlareSolverr）回 HTML 後做 markdown 轉換用的高品質 extractor
# markdownify：trafilatura 失敗時的最終 fallback；輕量、保結構、MIT 授權
# 這層大概 200MB Python 套件 + 後面 chromium browser 約 250MB
RUN pip install --no-cache-dir crawl4ai trafilatura markdownify

# Playwright Chromium browser（headless），含必要 system libs（libnss / libatk / fonts 等）
# `--with-deps` 會自動 apt-get install 缺的 .so，免我們在 Dockerfile 自己列一堆 lib*
# Chromium 大約 ~170MB；連 deps ~300MB（值得，全在這層獨立 commit）
RUN python -m playwright install --with-deps chromium

# ── Tier 5c：影片下載（YouTube / Vimeo / Bilibili 等 1700+ 站，Atlas web_crawler 影片模式用）──
# yt-dlp：業界標準影片抓取工具（解 HLS / DASH / 簽名 URL / 多 audio 軌 mux）
# ffmpeg：yt-dlp 把分離的 video / audio stream 合成 mp4 必備；也用於字幕格式轉換
# ffmpeg apt 包約 ~80MB；yt-dlp pip 包小（~10MB）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp

# ── Tier 6：Node.js runtime + pptxgenjs（JS 版 PPT 產生器）──
# debian 12 預設 nodejs 是 v18，對 pptxgenjs 3.x 足夠用
# 把 node + pptxgenjs 放在這，這樣 LLM agent 收到 `.agents/skills/pptx` skill 時能直接 `require('pptxgenjs')`
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pptxgenjs docx \
    && npm cache clean --force

# 讓任何 cwd 下的 node 進程都能 require('pptxgenjs') / require('docx')；
# 否則 LLM 在 working_dir 跑 node script、require 找不到 globals → 自己 npm install
# 把 node_modules 8.9 MB 殘檔倒進工作流輸出資料夾（user 報過坑）
ENV NODE_PATH=/usr/local/lib/node_modules

WORKDIR /project

# tail -f /dev/null：讓容器保持 running 等 exec 指令
CMD ["tail", "-f", "/dev/null"]
