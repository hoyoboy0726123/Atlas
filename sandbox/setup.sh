#!/usr/bin/env bash
# Atlas — WSL 內的沙盒安裝腳本
#
# 由 setup_sandbox.bat 從 Windows 呼叫進來，在 WSL Ubuntu 內執行。
# 做三件事：
#   1. 如果沒有 Docker Engine 就裝
#   2. build 沙盒映像檔（如果尚未存在）
#   3. 啟動長駐容器 atlas-sandbox（bind mount 專案根目錄）
#
# 之後 backend 會透過 `wsl docker exec atlas-sandbox ...` 執行 skill 程式碼。
#
# 用法：
#   setup.sh <project_dir_in_wsl>              # 一般安裝（跳過已存在的 image / container）
#   setup.sh <project_dir_in_wsl> --rebuild    # 強制 rebuild image + 重建 container
#                                              # （改了 Dockerfile / requirements.txt 後用）
set -euo pipefail

# ── 參數：專案根目錄 + 可選旗標
PROJECT_DIR="${1:-}"
REBUILD="no"
GUI_MODEL="ask"          # ask（互動時問 Y/N）/ yes / no
for arg in "${@:2}"; do
    case "$arg" in
        --rebuild|-r)      REBUILD="yes" ;;
        --with-gui-model)  GUI_MODEL="yes" ;;   # 無人值守安裝：強制裝
        --no-gui-model)    GUI_MODEL="no"  ;;   # 無人值守安裝：強制跳過
    esac
done
if [[ -z "$PROJECT_DIR" ]]; then
    echo "用法：$0 <project_dir_in_wsl> [--rebuild] [--with-gui-model|--no-gui-model]"
    echo "範例：$0 /mnt/c/Users/<you>/Atlas"
    echo "改了 Dockerfile / requirements.txt 要重裝：$0 ... --rebuild"
    echo ""
    echo "GUI 定位模型（選用，8.9GB，需 NVIDIA GPU）："
    echo "  預設：偵測到 GPU 時互動詢問 Y/N；沒有終端機（排程 / CI）時自動跳過"
    echo "  --with-gui-model  不詢問直接安裝"
    echo "  --no-gui-model    不詢問直接跳過"
    exit 1
fi
if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "✗ 找不到專案目錄：$PROJECT_DIR"
    exit 1
fi

# 必須與 backend/pipeline/sandbox.py 的 CONTAINER_NAME 一致。可用 env 覆寫。
CONTAINER="${SANDBOX_CONTAINER:-atlas-sandbox}"
IMAGE="atlas-sandbox:latest"

echo "══════════════════════════════════════════════════════"
echo "Atlas — 沙盒安裝"
echo "══════════════════════════════════════════════════════"
echo "專案目錄：$PROJECT_DIR"
echo ""

# ── Docker CLI 前綴偵測：優先跑 plain docker；失敗才用 sudo
# 已加入 docker group 的使用者（usermod -aG docker）重啟 WSL 後就免 sudo
if docker info &>/dev/null; then
    DOCKER="docker"
    echo "✓ docker 免 sudo 可用"
else
    DOCKER="sudo docker"
    echo "ℹ docker 需要 sudo（尚未加入 docker group 或 WSL 還沒重啟）"
fi
echo ""

# ── 1. 確認 / 安裝 Docker Engine
if ! command -v docker &>/dev/null; then
    echo "==> Docker 未安裝，開始自動安裝（~2-3 分鐘）..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "✓ Docker 已安裝"
    echo "  ⚠ 已把目前使用者加進 docker group，WSL 重啟後免 sudo 可用 docker"
else
    echo "✓ Docker 已存在：$(docker --version)"
fi

# ── 2. 啟動 Docker daemon（WSL 內 systemd 未預設啟動時需手動）
# `docker info` 已通過代表 daemon 在跑、跳過後續 sudo service 檢查（避免卡 sudo 密碼）
if [[ "$DOCKER" == "sudo docker" ]] && ! sudo -n service docker status &>/dev/null; then
    echo "==> 啟動 Docker daemon..."
    sudo service docker start
fi

# ── 3. Build 沙盒映像檔
# --rebuild：強制砍掉舊 image + 舊 container，重裝（改 Dockerfile / requirements.txt 後用）
# 沒 --rebuild：沒 image 才 build；有就跳過（fresh clone 會 build；重跑不浪費時間）
if [[ "$REBUILD" == "yes" ]]; then
    echo "==> 強制 rebuild（--rebuild）：先移除舊 container + image..."
    $DOCKER rm -f "$CONTAINER" 2>/dev/null || true
    $DOCKER rmi -f "$IMAGE" 2>/dev/null || true
    echo "==> 重建映像檔 $IMAGE（約 5-10 分鐘，含 Node.js + 所有 pip 套件）..."
    $DOCKER build --no-cache -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ 映像檔已 rebuild"
elif [[ "$($DOCKER images -q $IMAGE 2>/dev/null)" == "" ]]; then
    echo "==> Build 沙盒映像檔 $IMAGE（首次約 5-10 分鐘）..."
    $DOCKER build -t "$IMAGE" "$PROJECT_DIR/sandbox"
    echo "✓ 映像檔已建立"
else
    echo "✓ 映像檔已存在：$IMAGE"
    echo "  （改了 Dockerfile / requirements.txt 要生效，加 --rebuild 重跑本腳本）"
fi

# ── 4. 計算 AGENTS_DIR（永遠都做、後面 default_skills 安裝跟 container mount 都要用）
# 找出 Windows 使用者 home 對應的 WSL 路徑（/mnt/c/Users/XXX）
WIN_USER=$(echo "$PROJECT_DIR" | sed -n 's|^/mnt/\([a-z]\)/Users/\([^/]*\)/.*|\2|p')
DRIVE_LETTER=$(echo "$PROJECT_DIR" | sed -n 's|^/mnt/\([a-z]\)/.*|\1|p')
if [[ -n "$WIN_USER" && -n "$DRIVE_LETTER" ]]; then
    USER_HOME_WSL="/mnt/$DRIVE_LETTER/Users/$WIN_USER"
else
    # 專案不在 /mnt/c/Users/... 下（例如放在 D:\ 或其他位置）
    # → 仍讓 ~/.agents 有 fallback，指到 Windows 預設 C:\Users\<current>\.agents
    USER_HOME_WSL="/mnt/c/Users/$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r')"
    echo "ℹ 專案不在 /mnt/<drive>/Users/... 下，.agents 將定位到：$USER_HOME_WSL"
fi
AGENTS_DIR="$USER_HOME_WSL/.agents"
mkdir -p "$AGENTS_DIR/skills"

# ── 4b. 安裝預設 skill（idempotent、不覆蓋使用者已有版本）
# repo 內的 default_skills/ 是專案的「出廠技能包」（兩個 Atlas 專屬 skill）：
#   • scraped-content-parser — 爬蟲節點抓回來的原始內容結構化
#   • python-cli-extractor   — 把現成的 Python GUI/Web app 無破壞性接進 Atlas pipeline
# Office 三件套（docx/pptx/xlsx）使用者自己從 Anthropic / Claude Code 安裝、不 bundle。
# 已存在的 skill 一律跳過、保留使用者的版本（可能他自己改過或升過級）
DEFAULT_SKILLS_DIR="$PROJECT_DIR/default_skills"
if [[ -d "$DEFAULT_SKILLS_DIR" ]]; then
    installed=0
    skipped=0
    for src in "$DEFAULT_SKILLS_DIR"/*/; do
        # 防 glob 沒展開到任何子資料夾時、$src 留下字面 "*/" 害 cp 炸
        [[ -d "$src" ]] || continue
        name=$(basename "$src")
        target="$AGENTS_DIR/skills/$name"
        if [[ -d "$target" ]]; then
            # 注意：用 $((var+1)) 不用 ((var++))。後者在 var=0 那次回傳 exit 1
            # (舊值 0 被當 false)，搭 set -e 會整個 abort 掉
            skipped=$((skipped + 1))
        else
            cp -r "$src" "$target"
            installed=$((installed + 1))
        fi
    done
    if (( installed > 0 )); then
        echo "✓ 已安裝 $installed 個預設 skill 到 $AGENTS_DIR/skills/"
    fi
    if (( skipped > 0 )); then
        echo "  ($skipped 個 skill 已存在、保留使用者版本)"
    fi
fi

# ── 4c. GPU 偵測（可選能力；沒有就靜默走 CPU）
# 目的：讓吃 GPU 的工作流（whisper 語音轉文字、地端 VLM…）在沙盒內跑得動。
#
# 判斷邏輯（**完全不看 GPU 型號**，換卡不用改這裡）：
#   /dev/dxg 存在      → WSL2 GPU 直通已開（Windows 側驅動有裝）
#   nvidia-smi -L 成功 → 確實是 NVIDIA 且驅動可用
#   兩者都成立才啟用；否則 GPU_FLAG 保持空字串 → docker run 與沒 GPU 時完全相同
#
# ⚠️ 侷限（不要對使用者宣稱「有 GPU 就會好」）：
#   只支援 NVIDIA。AMD / Intel Arc 會被判定為「無 GPU」而走 CPU —— 它們需要
#   ROCm / oneAPI，是完全不同的方案。
#
# ⚠️ 本段任何失敗都**不可中斷安裝**：沙盒用 CPU 仍完全可用，
#   為了「加速」讓整個安裝掛掉是本末倒置。故全程容錯、失敗即降級。
GPU_FLAG=""
echo ""
echo "==> 偵測 GPU（可選；沒有就用 CPU 模式）..."
if [[ -e /dev/dxg ]] && /usr/lib/wsl/lib/nvidia-smi -L &>/dev/null; then
    _gpu_name=$(/usr/lib/wsl/lib/nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    echo "✓ 偵測到 NVIDIA GPU：${_gpu_name:-unknown}"

    # ⚠️⚠️ 這整段的 sudo 一律用 `sudo -n`（non-interactive）。
    # 血淚教訓：原本寫成 `sudo xxx &>/dev/null || true`，以為 `|| true` 就安全了 ——
    # 但 `|| true` 只擋得住「失敗」，擋不住「卡住」。沒有 TTY 時 sudo 會停在密碼
    # 提示乾等，而 &>/dev/null 又把提示吞掉，外觀上就是整個安裝腳本當機不動。
    # `sudo -n` 在需要密碼時**立刻**回非零，才能真正走到降級分支。
    _SUDO="sudo -n"
    if ! $_SUDO true &>/dev/null; then
        echo "  ⚠ 目前無免密碼 sudo 權限 → 跳過 GPU 設定，沙盒以 CPU 模式執行"
        echo "    （要啟用 GPU：在互動終端機自行執行下列指令後，重跑本腳本）"
        echo "    sudo apt-get install -y nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo service docker restart"
        _SUDO=""      # 標記為不可用，下面全部跳過
    fi

    # nvidia-container-toolkit：把 GPU 交給容器的橋接層，必裝且需 root
    if [[ -n "$_SUDO" ]] && ! command -v nvidia-ctk &>/dev/null; then
        echo "  → 未安裝 nvidia-container-toolkit，嘗試安裝..."
        if (
            set -e
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                | $_SUDO gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                | $_SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
            $_SUDO apt-get update -qq
            $_SUDO apt-get install -y -qq nvidia-container-toolkit
        ) &>/dev/null; then
            echo "  ✓ nvidia-container-toolkit 已安裝"
        else
            echo "  ⚠ 安裝失敗（權限或網路）→ 沙盒改用 CPU 模式"
        fi
    fi

    # 註冊 runtime 並重啟 docker（已註冊過為 no-op）
    if command -v nvidia-ctk &>/dev/null; then
        if [[ -n "$_SUDO" ]]; then
            $_SUDO nvidia-ctk runtime configure --runtime=docker &>/dev/null || true
            ($_SUDO systemctl restart docker &>/dev/null || $_SUDO service docker restart &>/dev/null) || true
            sleep 2
        fi
        # 沒有 sudo 也要往下測 —— daemon 可能早就設定好了（例如使用者先前手動裝過）
        # 實測驗證：真的起一個容器看得不看得到 GPU，成功才啟用（不靠推測）
        if $DOCKER run --rm --gpus all "$IMAGE" nvidia-smi -L &>/dev/null; then
            GPU_FLAG="--gpus all"
            echo "  ✓ 容器 GPU 直通驗證通過 → 沙盒將以 GPU 模式建立"
        else
            echo "  ⚠ 容器仍取不到 GPU（toolkit/daemon 設定未生效）→ 沙盒改用 CPU 模式"
        fi
    fi
else
    echo "· 未偵測到可用的 NVIDIA GPU → 沙盒以 CPU 模式執行（正常，非錯誤）"
fi

# ── 5. 啟動 / 重建容器
if $DOCKER ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    # 已存在 → 確認是否 running
    # 注意：既有容器的 GPU 設定是建立當下決定的，改不了。
    # 使用者若是「先無卡安裝、後來才有 GPU」，要 --rebuild 才會帶上 --gpus。
    if [[ -n "$GPU_FLAG" ]] && \
       [[ "$($DOCKER inspect "$CONTAINER" --format '{{json .HostConfig.DeviceRequests}}' 2>/dev/null)" == "null" ]]; then
        echo "ℹ 偵測到 GPU，但既有容器是以 CPU 模式建立的。"
        echo "  要讓沙盒吃到 GPU，請重跑本腳本並加 --rebuild"
    fi
    if $DOCKER ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        echo "✓ 容器 $CONTAINER 已經在跑"
    else
        echo "==> 容器 $CONTAINER 存在但已停止，啟動中..."
        $DOCKER start "$CONTAINER"
    fi
else
    echo "==> 建立並啟動容器 $CONTAINER..."
    # ── Bind mount 策略 ──────────────────────────────────────────
    # 需要讓容器看到三類檔案（都用「同路徑映射」，不翻譯路徑）：
    #   (1) 專案本體：$PROJECT_DIR（讓使用者工作流產出存 ai_output/ 時兩邊同步）
    #   (2) Agent Skills：$AGENTS_DIR（skill 掛載時 LLM 呼叫 scripts/）
    #   (3) 容器內的 $HOME 也指到同一份 .agents，這樣 Path.home() / ".agents"
    #       在容器跟 Windows 都指向同一個地方
    # $GPU_FLAG 由上面 4c 決定：有 NVIDIA GPU 且驗證通過才是 "--gpus all"，
    # 否則是空字串 → 展開後這行與沒有 GPU 支援時完全相同（不加引號才會被正確省略）
    $DOCKER run -d \
        --name "$CONTAINER" \
        --restart unless-stopped \
        $GPU_FLAG \
        -v "$PROJECT_DIR:$PROJECT_DIR" \
        -v "$AGENTS_DIR:$AGENTS_DIR" \
        -v "$AGENTS_DIR:/root/.agents" \
        -w "$PROJECT_DIR" \
        "$IMAGE"
    echo "✓ 容器已啟動，掛載："
    echo "    $PROJECT_DIR → $PROJECT_DIR（專案本體）"
    echo "    $AGENTS_DIR → $AGENTS_DIR（Agent Skills，絕對路徑相容）"
    echo "    $AGENTS_DIR → /root/.agents（容器內 ~/.agents 相容）"
    if [[ -n "$GPU_FLAG" ]]; then
        echo "    GPU：已直通（--gpus all）"
    else
        echo "    GPU：未啟用（CPU 模式）"
    fi
fi

# ── 4d. GUI 定位模型（選用；computer_use 的 vlm_mode='grounding' 用）
# 沒有 GPU 就完全不提 —— 提了也裝不動，只會讓使用者困惑。
# 有 GPU 才問；沒有終端機（排程 / CI / 從 .bat 非互動呼叫）預設跳過，
# 不能讓無人值守安裝卡在等輸入，也不該偷偷下載 8.9GB。
echo ""
if [[ -z "$GPU_FLAG" ]]; then
    echo "==> GUI 定位模型：跳過（需要 NVIDIA GPU）"
elif $DOCKER exec "$CONTAINER" bash -c \
        'ls -d /root/.cache/huggingface/hub/models--Mininglamp-2718--Mano-CUA-* >/dev/null 2>&1' 2>/dev/null; then
    echo "==> GUI 定位模型：已安裝，跳過下載"
else
    _DO_DL="no"
    case "$GUI_MODEL" in
        yes) _DO_DL="yes" ;;
        no)  echo "==> GUI 定位模型：依 --no-gui-model 跳過" ;;
        ask)
            if [[ -t 0 ]]; then
                echo "==> 選用功能：GUI 定位模型"
                echo "    用途：桌面自動化的錨點圖失效時（主題色變、視窗縮放），"
                echo "          用一句話描述就能找到按鈕位置，當作 CV 比對的備援。"
                echo "    代價：下載 8.9GB、佔磁碟 8.3GB，安裝約 5-10 分鐘。"
                echo "    不裝也完全不影響其他功能，之後可再執行本腳本加裝。"
                echo ""
                read -r -p "    要現在安裝嗎？[y/N] " _ans
                case "$_ans" in [yY]*) _DO_DL="yes" ;; *) echo "    → 跳過" ;; esac
            else
                echo "==> GUI 定位模型：跳過（非互動模式）"
                echo "    需要的話重跑：setup.sh <dir> --with-gui-model"
            fi
            ;;
    esac
    if [[ "$_DO_DL" == "yes" ]]; then
        echo "==> 下載 GUI 定位模型（8.9GB，請耐心等）..."
        if $DOCKER exec "$CONTAINER" pip install --no-cache-dir -q \
                transformers accelerate qwen-vl-utils safetensors torchvision bitsandbytes \
            && $DOCKER exec "$CONTAINER" python3 -c \
                "from huggingface_hub import snapshot_download; \
                 snapshot_download(repo_id='Mininglamp-2718/Mano-CUA-4B-Thinking-1.1')"; then
            echo "✓ GUI 定位模型安裝完成"
        else
            echo "⚠ GUI 定位模型安裝失敗（網路或磁碟空間）→ 該功能停用，其餘不受影響"
            echo "  之後可重跑：setup.sh <dir> --with-gui-model"
        fi
    fi
fi

# ── 4b. FlareSolverr（web_crawler 節點 Tier 2 用：解 Cloudflare challenge）
# 走 docker compose；compose file 在 $PROJECT_DIR/sandbox/docker-compose.yml
# 失敗不擋整體（爬蟲 Tier 1 仍可運作，只是遇到 CF 站會 fallback 失敗）
echo ""
echo "==> 啟動 FlareSolverr（web_crawler 節點 Tier 2 fallback）..."
if $DOCKER compose version &>/dev/null; then
    if (cd "$PROJECT_DIR/sandbox" && $DOCKER compose up -d flaresolverr); then
        echo "✓ FlareSolverr 已啟動：http://localhost:8191"
    else
        echo "⚠ FlareSolverr 啟動失敗（不影響 Tier 1 爬蟲；遇到 Cloudflare 時 Tier 2 會無法 fallback）"
    fi
else
    echo "⚠ docker compose 不可用，跳過 FlareSolverr（你的 docker 版本太舊？升級到 20.10+ 即可）"
fi

# ── 5. 冒煙測試
echo ""
echo "==> 冒煙測試 — 核心套件："
if ! $DOCKER exec "$CONTAINER" python -c "import pandas, openpyxl, numpy, requests; print('  ✓ Tier 1-2 OK')"; then
    echo "✗ 核心套件測試失敗"
    exit 1
fi

echo "==> 冒煙測試 — 進階套件（Tier 4-5）："
$DOCKER exec "$CONTAINER" python -c "
missing = []
for name in ['pptx', 'pdfplumber', 'newspaper', 'cloudscraper', 'feedparser', 'fake_useragent']:
    try:
        __import__(name)
    except Exception as e:
        missing.append(f'{name} ({e.__class__.__name__})')
if missing:
    print('  ⚠ 缺少：', ', '.join(missing))
    print('    解法：setup_sandbox.bat --rebuild 重建')
else:
    print('  ✓ python-pptx / pdfplumber / newspaper3k / cloudscraper / feedparser / fake_useragent 全部 OK')
" || true

echo "==> 冒煙測試 — web_crawler（Crawl4AI + Playwright Chromium）："
$DOCKER exec "$CONTAINER" python -c "
import sys
try:
    import crawl4ai
    # crawl4ai 0.8 把版本放在 .__version__.__version__；舊版可能直接是字串
    v = getattr(crawl4ai, '__version__', None)
    v = getattr(v, '__version__', v)
    print(f'  ✓ crawl4ai {v}')
except Exception as e:
    print(f'  ⚠ crawl4ai 未安裝（{e.__class__.__name__}）— setup_sandbox.bat --rebuild')
try:
    import trafilatura, markdownify
    print('  ✓ trafilatura + markdownify')
except Exception as e:
    print(f'  ⚠ trafilatura / markdownify 缺：{e}')
# 用 Playwright 自己 API 拿 chromium binary 路徑（最可靠；版本不同子目錄名稱會變）
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        cp = p.chromium.executable_path
    import os
    if cp and os.path.exists(cp):
        print(f'  ✓ Chromium binary：{cp}')
    else:
        print(f'  ⚠ Chromium binary 不存在：{cp} — setup_sandbox.bat --rebuild')
except Exception as e:
    print(f'  ⚠ Playwright Chromium 偵測失敗（{e.__class__.__name__}）— setup_sandbox.bat --rebuild')
" || true

echo "==> 冒煙測試 — Node.js + pptxgenjs："
if $DOCKER exec "$CONTAINER" bash -c 'node --version && npm list -g --depth=0 2>/dev/null | grep pptxgenjs' >/dev/null 2>&1; then
    NODE_VER=$($DOCKER exec "$CONTAINER" node --version 2>/dev/null)
    echo "  ✓ Node.js $NODE_VER + pptxgenjs OK"
else
    echo "  ⚠ Node.js 或 pptxgenjs 未安裝（解法：setup_sandbox.bat --rebuild）"
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "✓ 沙盒就緒！"
echo "  容器名：$CONTAINER"
$DOCKER inspect "$CONTAINER" --format '{{range .Mounts}}    {{.Source}} → {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null || true
echo "══════════════════════════════════════════════════════"

# ── 6. 寫旗標讓 setup_sandbox.bat 知道要不要提醒使用者關閉 WSL
# 條件：當前還在用 sudo 跑 docker (代表 docker group 還沒 reload)
# 旗標檔讀完即刪、不留下殘留
FLAG_FILE="$PROJECT_DIR/sandbox/.needs_wsl_shutdown"
if [[ "$DOCKER" == "sudo docker" ]]; then
    touch "$FLAG_FILE" 2>/dev/null || true
else
    rm -f "$FLAG_FILE" 2>/dev/null || true
fi
