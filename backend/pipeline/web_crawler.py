"""
網頁爬蟲節點引擎。

設計原則：
  - **跑在 sandbox**：所有 Crawl4AI / Playwright / Chromium 動作都在 `atlas-sandbox`
    容器內執行；host backend 只負責拼指令、處理 fallback、寫檔。
  - **Tier 1 + Tier 2 fallback**：
      Tier 1 = Crawl4AI（容器內）→ 95% 網站
      Tier 2 = FlareSolverr（獨立 container、port 8191）→ +4% Cloudflare 站
    Tier 2 由 host 直接打 HTTP（FlareSolverr 內部用 Puppeteer 解 CF challenge），
    拿到 HTML 後 host 用 markdownify 轉 markdown。
  - **輸出格式**：Markdown + YAML frontmatter（適合 LLM 餵入）
    單頁直接寫到 output.path；多頁時 output.path 是資料夾，pages/*.md + index.json

不負責：
  - 沙盒容器的建立 / Chromium 安裝（由 sandbox/Dockerfile + setup.sh 一次性處理）
  - FlareSolverr container 的啟動（由 sandbox/setup.sh 順手起一個 -p 8191:8191）
  - 排程（由 scheduler/manager.py 既有機制驅動）

使用方式見 `pipeline/executor.py: execute_step_with_web_crawler`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qsl

# 沙盒既有 wsl docker exec 封裝
from .sandbox import (
    _docker_exec_cmd,
    _write_code_tempfile,
    windows_to_wsl_path,
    _decode_subprocess_output,
)

log = logging.getLogger(__name__)

# ── 常數 ───────────────────────────────────────────────────────────
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
# Windows + WSL2 docker:backend 跑在 Windows host、FlareSolverr 容器在 WSL 裡。
# host 連 docker-published port 走 localhost 常不穩(localhost→::1 / 轉發間歇掉),
# 但走 WSL IP 直連穩定。故候選 URL 順序:env 覆寫 > WSL IP > localhost > 127.0.0.1。
# 實測:同一刻 localhost POST /v1 連不上、WSL-IP POST /v1 = ok。
_FLARE_IP_CACHE = {"ip": "", "ts": 0.0}


def _wsl_host_ip() -> str:
    """取 WSL2 的 IP(`wsl hostname -I` 第一個);快取 5 分鐘。非 Windows / 無 wsl → ""。"""
    import sys as _sys
    if not _sys.platform.startswith("win"):
        return ""
    now = time.time()
    if _FLARE_IP_CACHE["ip"] and now - _FLARE_IP_CACHE["ts"] < 300:
        return _FLARE_IP_CACHE["ip"]
    try:
        out = subprocess.run(["wsl", "hostname", "-I"], capture_output=True,
                             text=True, timeout=8)
        ip = (out.stdout or "").strip().split()[0] if (out.stdout or "").strip() else ""
    except Exception:
        ip = ""
    _FLARE_IP_CACHE["ip"], _FLARE_IP_CACHE["ts"] = ip, now
    return ip


def _flaresolverr_candidates() -> list[str]:
    """回傳要嘗試的 FlareSolverr /v1 URL 候選(依可靠度排序)。"""
    env = os.environ.get("FLARESOLVERR_URL")
    if env:
        return [env]
    cands: list[str] = []
    ip = _wsl_host_ip()
    if ip:
        cands.append(f"http://{ip}:8191/v1")
    cands.append("http://localhost:8191/v1")
    cands.append("http://127.0.0.1:8191/v1")
    return cands
DEFAULT_TIMEOUT_SEC = 180
# Cloudflare 偵測訊號（回應裡出現任一即視為被擋）
_CF_MARKERS = (
    "Just a moment...",
    "Checking your browser before accessing",
    "cf-browser-verification",
    "cf-challenge-running",
    "DDoS protection by Cloudflare",
    "Attention Required! | Cloudflare",
)
_CF_HEADER_KEYS = ("cf-ray", "cf-cache-status", "cf-mitigated")

# ── SPA 自動重試的「智慧預設」 ───────────────────────────────────────
# 設計演進：
#   v1 用 `article, main, ...` 容器 selector → Dcard 殼有空 <article> 太早 match → 失敗
#   v2 用 wait_for=body + js_code 強制等 → React 還沒 render，scroll 觸發不到 lazy load → 失敗
#   v3（現行）用「等實際內容連結」selector — 大部分內容站的貼文 URL 有共通模式：
#     Dcard/Medium/Instagram → /p/        Reddit → /comments/
#     WordPress/Tumblr → /post/           Twitter/X → /status/
#     新聞站 → /article/                  Hacker News → /item?id=
#   等到第一個 match 才繼續，confirm 文章列表已 render；再加滾動觸發 infinite scroll
_SPA_FALLBACK_WAIT_SELECTOR = (
    'a[href*="/p/"], '             # Dcard / Medium / Instagram
    'a[href*="/post/"], '          # Tumblr / wordpress.com
    'a[href*="/posts/"], '         # ProductHunt / Indie Hackers
    'a[href*="/article/"], '       # 新聞站 / 部落格
    'a[href*="/articles/"], '
    'a[href*="/comments/"], '      # Reddit
    'a[href*="/status/"], '        # Twitter / X
    'a[href*="/threads/"], '       # Threads
    'a[href*="/video/"], '         # TikTok / 影音站
    'a[href*="/item?id="], '       # Hacker News
    'main article + article'       # 兜底：main 內有兩個以上 article（避開單一空殼）
)
_SPA_FALLBACK_INTERACTIONS = [
    {"type": "scroll", "to": "bottom"},    # 確認第一輪內容後、滾動觸發 infinite scroll
    {"type": "wait", "seconds": 1.5},
    {"type": "scroll", "to": "bottom"},
    {"type": "wait", "seconds": 1.5},
]
# 智慧滾動安全上限（不論模式，超過就停 — 防無限滾或 throttle 卡死）
_SMART_SCROLL_MAX_ROUNDS = 10
_SMART_SCROLL_MAX_SECONDS = 60
# 「結果太瘦」的判斷門檻
# 之前用 AND（markdown<1500 且 links<5）太保守 → SPA 殼有 navigation 連結就誤放過
# 改成 OR / 兩種命中模式：
#   1. markdown < 2000 bytes      — 殼頁面再多 nav 連結也不該 > 2KB
#   2. markdown < 5000 且 links<3 — 中等長度但連結極少，像獨立網頁卻抓不到列表
# 真的靜態純文章 < 2000 bytes 的少見，且重試成本只多 ~10 秒（結果沒變好就保留原本）
_THIN_MARKDOWN_BYTES = 2000
_THIN_MEDIUM_BYTES = 5000
_THIN_MEDIUM_LINKS = 3

# ── Per-host 能力註冊表 ──────────────────────────────────────────────
# 把「我們特別認識的站」的爬取提示集中一處。**沒列在這裡的站 → 全走通用路徑、
# 行為完全不變**(零副作用)。命中的站只會多兩件事、都只可能變好、不會變差:
#   1. SPA 智慧重試時、用本站專屬 wait_selector(例電商等「價格元素」render)取代通用 selector
#      —— 只在「第一輪結果太瘦、本來就要重試」時生效,不影響第一輪、不會新增 timeout。
#   2. 抓到殼 / 失敗且本站標 known_hard 時、把「為什麼難爬 + 該怎麼辦」誠實附進錯誤訊息
#      —— 讓使用者/驗證看到「momo 需登入 cookie」而不是一句籠統失敗。
#
# 電商商品頁常見「價格靠 JS 後載」、通用 SPA selector(/p/、/comments/)等不到價格元素,
# 補一組價格錨點讓重試等得到真內容。
_SHOP_PRICE_SELECTOR = (
    '[itemprop="price"], meta[itemprop="price"], '
    '[class*="price"], [class*="Price"], [class*="amount"], '
    '[data-price], .prdPrice, .priceArea, .price-now, .o-prdPrice__price'
)

# 每筆欄位都可選:
#   match         : hostname 子字串清單(任一命中即套用)
#   wait_selector : 本站「內容真的 render 出來」的 selector(餵 SPA 重試)
#   known_hard    : True = 已知難爬(自家反爬 / 重 SPA),抓到殼時主動誠實警告
#   needs_cookie  : True = 通常要登入 cookie 才有完整內容
#   note          : 警告訊息附的人話說明(known_hard 時用)
HOST_REGISTRY: list[dict] = [
    # ── 台灣電商(自家反爬 + 重 SPA;有商品 URL pattern 但內容常只拿到殼)──
    {"match": ["momoshop.com.tw", "momo.com.tw"], "wait_selector": _SHOP_PRICE_SELECTOR,
     "known_hard": True, "needs_cookie": False,
     "note": "momo 是重 SPA、價格靠 JS 後載、又有自家反爬;匿名常只拿到導覽殼。"
             "建議改用官方 App / 搜尋頁 API,或在進階設定貼登入 cookie。"},
    {"match": ["shopee.tw", "shopee.com"], "wait_selector": _SHOP_PRICE_SELECTOR,
     "known_hard": True, "needs_cookie": True,
     "note": "蝦皮自家反爬強、且多數內容要登入;沒 cookie 幾乎抓不到商品內容。"},
    {"match": ["24h.pchome.com.tw", "pchome.com.tw", "ecshweb.pchome.com.tw"],
     "wait_selector": _SHOP_PRICE_SELECTOR, "known_hard": False, "needs_cookie": False,
     "note": "PChome 商品頁價格靠 JS 後載,等價格元素 render 即可。"},
    {"match": ["ruten.com.tw"], "wait_selector": _SHOP_PRICE_SELECTOR,
     "known_hard": True, "needs_cookie": False, "note": "露天有反爬、商品頁多為 SPA。"},
    # ── 中國電商(強反爬、普遍需登入)──
    {"match": ["taobao.com", "tmall.com"], "known_hard": True, "needs_cookie": True,
     "note": "淘寶/天貓自家反爬極強、需登入 cookie;匿名抓不到。"},
    {"match": ["jd.com"], "known_hard": True, "needs_cookie": True,
     "note": "京東自家反爬、商品頁需登入 cookie 才完整。"},
    # ── 國際電商(相對好爬、補價格錨點即可)──
    {"match": ["amazon."], "wait_selector": '#corePrice_feature_div, .a-price, #priceblock_ourprice, [class*="price"]',
     "known_hard": False, "needs_cookie": False, "note": "Amazon 商品頁等價格元素 render。"},
    # BestBuy:非美國 IP / headless 常被導到「Select your Country」國家選擇頁、拿不到商品。
    {"match": ["bestbuy.com"], "known_hard": True, "needs_cookie": False,
     "note": "BestBuy 會依地區 / 偵測 headless 導到國家選擇頁;非美國 IP 匿名常拿到空殼。"
             "需美國出口 IP / cookie,或改用其他來源。"},
    # ── 重 SPA 社群(子頁 pattern 已涵蓋、這裡補 render 等待 + needs_cookie 提示)──
    {"match": ["shopee"], "known_hard": True, "needs_cookie": True},  # shopee 其他 TLD 兜底
]


def _lookup_host_registry(url: str) -> Optional[dict]:
    """回傳該 URL host 命中的 registry entry(第一個 match);沒命中 → None(走通用路徑)。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for entry in HOST_REGISTRY:
        if any(m.lower() in host for m in entry.get("match", [])):
            return entry
    return None


# 失敗 / 需登入時給使用者的友善、可操作 cookie 指引(一條龍步驟)。
_COOKIE_HELP = (
    "👉 要抓需登入的站,請設 cookie:① 用瀏覽器登入該網站 → "
    "② 裝 Chrome 擴充「Cookie-Editor」匯出(或 DevTools→Network→複製 Cookie: 那行)→ "
    "③ 貼到本爬蟲節點面板「進階設定 → 登入 Cookies」欄(建議填 ${VAR} 把真值放 backend/.env、不明文存)。"
    "面板裡有完整圖文教學「教學:怎麼從瀏覽器抓 cookies?」。"
)

# 登入牆 / 攔截頁訊號(thin 時用來判斷「是不是因為要登入才拿不到」)。
_LOGIN_WALL_MARKERS = (
    "請先登入", "請登入", "會員登入", "登入後", "歡迎登录", "请登录", "請登錄",
    "sign in to", "please log in", "please sign in", "log in to continue",
    "select your country", "您被攔截", "access denied", "verify you are human",
)


def _looks_like_login_wall(result: "CrawlResult") -> bool:
    """thin 結果是不是「登入 / 國家選擇 / 攔截頁」(→ 需 cookie 才看得到真內容)。"""
    blob = ((result.title or "") + " " + (result.markdown or "")[:1500]).lower()
    return any(m.lower() in blob for m in _LOGIN_WALL_MARKERS)


def url_to_filename(url: str, ext: str = ".md") -> str:
    """把 URL 轉成可預測、不撞名的檔名。
    規則：host + path + query → 替換特殊字元成 _ → 折疊連續 _ → 必要時截斷加 hash。
    """
    import hashlib
    p = urlparse(url)
    parts: list[str] = []
    if p.hostname:
        parts.append(p.hostname)
    parts.extend(seg for seg in p.path.split("/") if seg)
    if p.query:
        parts.append(p.query)
    raw = "_".join(parts) or "page"
    # 只留字母數字 dash dot；其他全換 _
    safe = re.sub(r"[^a-zA-Z0-9\-\.]", "_", raw)
    # 折疊連續 _、去掉頭尾的 _
    safe = re.sub(r"_+", "_", safe).strip("_")
    # 最大 100 chars；超過就截 90 + hash 8
    if len(safe) > 100:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        safe = safe[:90].rstrip("._-") + "_" + h
    return safe + ext


def _looks_thin(result: "CrawlResult") -> bool:
    """偵測「頁面是 SPA 沒 hydrate 完的殼」/「列表頁沒抓到列表」。
    寧可多重試一次（多 ~10 秒），也不要漏掉 SPA case。
    """
    md_len = len(result.markdown or "")
    int_links = len((result.extra or {}).get("links_internal") or [])
    if md_len < _THIN_MARKDOWN_BYTES:
        return True
    if md_len < _THIN_MEDIUM_BYTES and int_links < _THIN_MEDIUM_LINKS:
        return True
    return False


# 反爬挑戰頁 / 未渲染導覽殼 的強訊號(子頁抓回的不是正文、是殼)。
# 實測 Reddit r/LocalLLaMA 衝 100 子頁:子頁全回傳這種殼(js_challenge / 跳至主要內容 / 開啟選單)。
# 這裡在「爬蟲層」確定性判定、不靠下游 LLM,殼內文一律標失敗、只留標題與連結。
_RENDER_SHELL_MARKERS = (
    "跳至主要內容", "skip to main content", "js_challenge", "jsc_orig_r",
    "開啟選單", "開啟導覽列", "open navigation menu",
    "just a moment", "checking your browser", "enable javascript",
    "請啟用 javascript", "需要啟用 javascript", "javascript is required",
    "前往 reddit 首頁", "go to reddit",
)
_SHELL_STRONG = ("js_challenge", "jsc_orig_r", "just a moment", "checking your browser")
# 已渲染內容頁的長度下限:≥ 此值即視為「有實質正文」、不當殼(避免 skip-link/導覽列字串誤殺)。
# 真正的挑戰頁 / 未 hydrate 殼遠小於此(CF interstitial 多 < 2KB)。
_SHELL_MAX_BYTES = 6000
CHILD_SHELL_FAILED = "（內文抓取失敗:此子頁回傳反爬挑戰頁/未渲染殼,僅保留標題與連結）"


def _looks_like_render_shell(md: Optional[str]) -> bool:
    """子頁 markdown 是反爬挑戰頁 / 未渲染導覽殼(沒抓到正文)→ True。
    保守:命中唯一強訊號(js_challenge / Just a moment …)即判;否則需 ≥ 2 個弱訊號,
    避免誤判正常正文裡偶然提到「Enable JavaScript」之類的字。

    ⚠️ 長度閘(重要、修誤殺):真正的挑戰頁 / 未渲染殼**內容很短**(CF「Just a moment」
    interstitial、未 hydrate 的 SPA 殼通常 < 數 KB)。而**已成功渲染的內容頁**即使在
    導覽列 / skip-link 網址裡帶有 js_challenge / jsc_orig_r / 跳至主要內容 等字串
    (Reddit 正常頁每頁都有),也是完整正文。所以先看長度:有實質內容(≥ SHELL_MAX_BYTES)
    就不是殼,避免把正常 Reddit 貼文頁(28KB、含 PO 文+留言)整頁誤判丟掉。
    (實測:www.reddit 單篇貼文 status=200、len≈27KB、含完整正文,卻因 skip-link 的
     ?js_challenge=1 被舊版判成殼 → 子頁全 0 成功;加長度閘後正常通過。)"""
    if not md:
        return False
    if len(md) >= _SHELL_MAX_BYTES:
        return False
    low = md.lower()
    if any(s in low for s in _SHELL_STRONG):
        return True
    return sum(1 for m in _RENDER_SHELL_MARKERS if m in low) >= 2


@dataclass
class CrawlResult:
    """單次爬取的回傳。"""
    ok: bool
    tier: str            # "crawl4ai" / "flaresolverr" / "failed"
    url: str
    final_url: str       # 跟 url 一樣 / 跳轉後的 URL
    status_code: int
    markdown: str        # 已包 frontmatter 的完整 .md 內容
    html: str            # 原始 HTML（debug 用，不一定有）
    title: str
    error: str           # ok=False 時填
    duration_ms: int
    extra: dict          # 圖片連結 / 內部連結 / metadata 等


# ── 公開 API ─────────────────────────────────────────────────────────

async def crawl_video(
    *,
    url: str,
    output_path: str,
    quality: str = "720p",
    max_filesize_mb: int = 500,
    max_duration_min: int = 30,
    subs: bool = True,
    subs_langs: str = "",
    save_info_json: bool = False,
    cookies: str = "",
    timeout: int = 600,
    logger: logging.Logger = log,
    step_name: str = "web_crawler",
) -> CrawlResult:
    """影片下載（yt-dlp）：YouTube / Vimeo / Bilibili / Twitter / TikTok 等 1700+ 站。
    產出檔案放在 output_path 同層資料夾（不寫進 output_path 自身、output_path 變成 .md 摘要）：
      - video.mp4（或 yt-dlp 選的容器）
      - video.info.json（yt-dlp full metadata）
      - video.<lang>.srt（字幕，每語言一份；subs=True 才有）
      - <output_path>.md（給後續 skill 節點看的 markdown 摘要 + frontmatter）
    """
    t0 = time.time()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return CrawlResult(
            ok=False, tier="failed", url=url, final_url=url, status_code=0,
            markdown="", html="", title="", duration_ms=0, extra={},
            error=f"URL scheme 必須是 http / https：{url}",
        )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{step_name}] 影片模式：yt-dlp 下載 {url}")
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _run_ytdlp_in_sandbox(
            url=url, output_path=output_path,
            quality=quality, max_filesize_mb=max_filesize_mb,
            max_duration_min=max_duration_min,
            subs=subs, subs_langs=subs_langs,
            save_info_json=save_info_json,
            cookies=_parse_cookies(cookies),
            timeout=timeout, logger=logger, step_name=step_name,
        ),
    )

    if not result.ok:
        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    # 影片模式：result.markdown 已是「摘要 + 檔案清單」由 sandbox 腳本組好；host 加 frontmatter
    md = _wrap_with_frontmatter(result, requested_url=url, started_at=t0,
                                tier_used=result.tier)
    Path(output_path).write_text(md, encoding="utf-8")
    logger.info(f"[{step_name}] ✓ 影片摘要寫入：{output_path}（檔案在同層資料夾、tier={result.tier}）")
    result.markdown = md
    result.duration_ms = int((time.time() - t0) * 1000)
    return result


async def crawl_urls(
    *,
    urls: list[str],
    output_dir: str,
    js_render: bool = True,
    wait_for_selector: str = "",
    cloudflare_fallback: bool = True,
    cookies: str = "",
    interactions: Optional[list[dict]] = None,
    download_assets: bool = False,
    scroll_count: int = 0,
    target_post_count: int = 0,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    logger: logging.Logger = log,
    step_name: str = "web_crawler",
) -> dict:
    """多 URL 爬取：每個 URL 落地一個 .md 到 output_dir，並寫 index.json 當 manifest。

    回傳 summary dict（給 executor 包成 ExecResult.stdout）：
      { ok, total, successful, failed, output_dir, results: [...] }
    """
    t0 = time.time()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 過濾空行 / # 開頭註解
    cleaned = [u.strip() for u in urls if u and u.strip() and not u.strip().startswith("#")]
    if not cleaned:
        return {"ok": False, "total": 0, "successful": 0, "failed": 0,
                "output_dir": str(out), "results": [], "error": "URL 列表為空"}

    logger.info(f"[{step_name}] 多 URL 模式：共 {len(cleaned)} 個 URL → {output_dir}")
    results: list[dict] = []

    # 收集已用過的檔名、處理同 URL 不同 query 但 sanitize 後撞名
    used_names: set[str] = set()

    for i, url in enumerate(cleaned, start=1):
        logger.info(f"[{step_name}] ── ({i}/{len(cleaned)}) {url}")
        fname = url_to_filename(url)
        # 撞名處理：加 _2 / _3 ...
        base_fname = fname
        cnt = 2
        while fname in used_names:
            fname = base_fname.replace(".md", f"_{cnt}.md")
            cnt += 1
        used_names.add(fname)

        target_path = str(out / fname)
        try:
            r = await crawl_single_url(
                url=url, output_path=target_path,
                js_render=js_render, wait_for_selector=wait_for_selector,
                cloudflare_fallback=cloudflare_fallback,
                cookies=cookies,
                interactions=interactions or [],
                download_assets=download_assets,
                scroll_count=scroll_count,
                target_post_count=target_post_count,
                timeout=timeout, logger=logger,
                step_name=f"{step_name} #{i}",
            )
        except Exception as e:
            logger.error(f"[{step_name}] ({i}/{len(cleaned)}) 例外：{e}")
            results.append({"url": url, "file": None, "status": "error", "error": str(e)})
            continue

        if r.ok:
            results.append({
                "url": url, "file": fname, "status": "ok",
                "title": r.title, "tier": r.tier,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
            })
            logger.info(f"[{step_name}] ✓ ({i}/{len(cleaned)}) {fname}")
        else:
            results.append({
                "url": url, "file": None, "status": "failed",
                "error": r.error, "tier": r.tier,
            })
            logger.warning(f"[{step_name}] ✗ ({i}/{len(cleaned)}) {url}：{r.error}")

    # 寫 index.json — 給後續 skill 節點當 manifest 讀
    manifest = {
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total": len(cleaned),
        "successful": sum(1 for r in results if r.get("status") == "ok"),
        "failed": sum(1 for r in results if r.get("status") != "ok"),
        "output_dir": str(out),
        "duration_ms": int((time.time() - t0) * 1000),
        "results": results,
    }
    index_path = out / "index.json"
    index_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        f"[{step_name}] ✓ 完成：{manifest['successful']}/{manifest['total']} 成功 "
        f"→ {index_path}"
    )
    manifest["ok"] = manifest["successful"] > 0
    return manifest


# ── Reddit 特例:站方反爬路由 ──────────────────────────────────────────
# 2026-06 起 www.reddit.com(shreddit)全站貼文/列表前面擋一層「Please wait for
# verification」JS 驗證牆 → headless 瀏覽器(crawl4ai / FlareSolverr 都是瀏覽器)
# 過不了貼文頁(status=0)。實測:
#   - www.reddit.com 任何頁 → 8KB「Please wait for verification」interstitial
#   - old.reddit.com      → 完整靜態 HTML、**但**擋 headless 瀏覽器指紋(回 403)
#   - 純 httpx + 瀏覽器 UA 打 old.reddit → 200、完整內容(57KB+)
# 所以 reddit 一律改寫成 old.reddit + 走純 HTTP 抓取(繞過會被擋的瀏覽器)。
_REDDIT_STATIC_HOSTS = {
    "reddit.com", "www.reddit.com", "new.reddit.com",
    "np.reddit.com", "m.reddit.com", "old.reddit.com",
}


def _maybe_reddit_static_url(url: str) -> Optional[str]:
    """reddit 系網域 → 回改寫成 old.reddit.com 的 URL(該走純 HTTP);非 reddit 回 None。
    media 子網域(i.redd.it / v.redd.it)不是 reddit.com、回 None 照常走瀏覽器。"""
    from urllib.parse import urlunparse
    try:
        p = urlparse(url)
    except Exception:
        return None
    if (p.hostname or "").lower() in _REDDIT_STATIC_HOSTS:
        return urlunparse(p._replace(netloc="old.reddit.com"))
    return None


async def _curl_exe_get(url: str, headers: dict, timeout: int) -> tuple[int, str, str]:
    """用系統 curl.exe 抓頁(Windows Schannel TLS 指紋,能過 Reddit 等擋 Python 客戶端的站)。
    回 (status_code, html, final_url)。curl 不存在或失敗 → (0, "", "")。跑在 thread pool 不擋 loop。"""
    import shutil
    curl = shutil.which("curl")
    if not curl:
        return (0, "", "")
    ua = headers.get("User-Agent", "Mozilla/5.0")
    # -L 跟轉址; -sS 靜默但留錯誤; -w 末尾附 status 與 final url(用少見分隔符切開)
    SEP = "\n<<<CURLMETA>>>\n"
    args = [curl, "-sSL", "--compressed", "-m", str(timeout),
            "-A", ua, "-H", "Accept-Language: en-US,en;q=0.9",
            "-w", f"{SEP}%{{http_code}}{SEP}%{{url_effective}}", url]
    def _run():
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout + 10)
            out = p.stdout.decode("utf-8", "replace")
            if SEP not in out:
                return (0, "", "")
            body, status, final = out.rsplit(SEP, 2)
            return (int(status.strip() or 0), body, final.strip())
        except Exception:
            return (0, "", "")
    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def _fetch_static_http(*, fetch_url: str, requested_url: str, output_path: str,
                             timeout: int, logger: logging.Logger, step_name: str,
                             started_at: float) -> "CrawlResult":
    """純 HTTP(httpx + 瀏覽器 UA)抓靜態頁 → markdown,繞過 headless 瀏覽器。
    用於 old.reddit 這種「放行 HTTP、擋瀏覽器指紋」的站。連結轉絕對、用 markdownify
    保留全部連結(下游 crawl_list_with_children 要靠列表頁 markdown 抽子頁連結)。"""
    import httpx
    from urllib.parse import urljoin
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    status_code = 0
    html = ""
    base = fetch_url
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout,
                                     follow_redirects=True) as c:
            resp = await c.get(fetch_url)
        status_code = resp.status_code
        html = resp.text or ""
        base = str(resp.url)
    except Exception as e:
        logger.warning(f"[{step_name}] httpx 抓取失敗:{e},改試系統 curl.exe")

    # Reddit 等站對 Python HTTP 客戶端(httpx / curl_cffi 含 impersonate)一律回 403,
    # 但系統 curl.exe(Windows Schannel TLS 指紋)拿得到 200 → 被擋時退回 curl.exe。
    if status_code != 200 or len(html) < 500:
        c_status, c_html, c_url = await _curl_exe_get(fetch_url, headers, timeout)
        if c_status == 200 and len(c_html) >= 500:
            logger.info(f"[{step_name}] 🔁 httpx 被擋(status={status_code}),curl.exe fallback 成功")
            status_code, html, base = c_status, c_html, (c_url or base)

    if status_code != 200 or len(html) < 500:
        return _err(requested_url,
                    f"純 HTTP 回 status={status_code}、內容過薄({len(html)} bytes);"
                    f"httpx 與 curl.exe 皆無法取得(站點反爬)",
                    tier="http")

    # ── 被導去登入牆 / 驗證牆 ────────────────────────────────────────
    # ⚠ 2026-08-13 實測：Reddit 現在對匿名請求回 302 → /login/?reason=lor2，
    #   最終是 200 + 308KB 的登入頁。上面兩道守門(狀態碼、內容過薄)都擋不住，
    #   於是「抓到登入頁」被當成抓取成功，下游 subagent 就去分析一個登入頁，
    #   產出一份看起來正常、實際毫無根據的報告 —— 典型的靜默失敗。
    #   status 200 不等於拿到你要的內容，最終網址與標題才是證據。
    _blocked = _detect_login_wall(requested_url, base, html)
    if _blocked:
        return _err(requested_url,
                    f"抓到的是登入/驗證頁而不是內容：{_blocked}。"
                    f"（HTTP 200 但被導向 {base}）"
                    f"此來源目前需要登入或通過驗證，請改用官方 API、RSS，或換一個資料源。",
                    tier="http")

    links_internal: list = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        host = (urlparse(base).hostname or "")
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base, a["href"])  # 相對 → 絕對,下游才抽得到子頁
        links_internal = sorted({a["href"] for a in soup.find_all("a", href=True)
                                 if (urlparse(a["href"]).hostname or "") == host})
        html = str(soup)
    except Exception:
        pass

    # 內文清洗:優先 trafilatura(乾淨抽正文 + 留言、去掉 share/save/vote/loading 等 chrome),
    # 對「列表頁」這種非文章常抽不到 → 退回 markdownify(全頁、較雜但保底)。
    md = ""
    try:
        import trafilatura
        md = trafilatura.extract(html, output_format="markdown",
                                 include_links=True, include_comments=True) or ""
    except Exception:
        md = ""
    if len((md or "").strip()) < 200:
        try:
            from markdownify import markdownify as _md
            md = _md(html, heading_style="ATX")
        except Exception:
            md = re.sub(r"<[^>]+>", "", html)
    # trafilatura 會砍掉連結 → 末尾補一段「同站 permalink 連結」(只留貼文/項目類、不附導覽 chrome),
    # 讓下游 crawl_list_with_children 仍從列表頁 markdown 抽得到子頁;對內文摘要的雜訊極小。
    _ITEM_MARKERS = ("/comments/", "/item", "/p/", "/post", "/article",
                     "/status/", "/threads/", "/video/")
    _perma = [u for u in links_internal if any(m in u for m in _ITEM_MARKERS)]
    if _perma:
        tail = "\n\n## 連結\n" + "\n".join(f"- [{i + 1}]({u})" for i, u in enumerate(_perma[:80]))
        md = (md or "") + tail

    res = CrawlResult(
        ok=True, tier="http", url=requested_url, final_url=base,
        status_code=status_code, markdown=md, html="", title=_extract_title(html, md),
        error="", duration_ms=0, extra={"links_internal": links_internal},
    )
    wrapped = _wrap_with_frontmatter(res, requested_url=requested_url,
                                     started_at=started_at, tier_used="http")
    Path(output_path).write_text(wrapped, encoding="utf-8")
    res.markdown = wrapped
    res.duration_ms = int((time.time() - started_at) * 1000)
    logger.info(f"[{step_name}] ✓ 純 HTTP 抓取(old.reddit/靜態):{output_path}"
                f"({len(wrapped):,} bytes、{len(links_internal)} 同站連結)")
    return res


async def crawl_single_url(
    *,
    url: str,
    output_path: str,
    js_render: bool = True,
    wait_for_selector: str = "",
    cloudflare_fallback: bool = True,
    cookies: str = "",
    interactions: Optional[list[dict]] = None,
    download_assets: bool = False,
    scroll_count: int = 0,
    target_post_count: int = 0,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    logger: logging.Logger = log,
    step_name: str = "web_crawler",
) -> CrawlResult:
    """爬取單一 URL，落地一個 .md 檔到 output_path。

    Tier 1 失敗（且 cloudflare_fallback=True 且偵測到 CF）才會走 Tier 2。
    """
    t0 = time.time()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return CrawlResult(
            ok=False, tier="failed", url=url, final_url=url, status_code=0,
            markdown="", html="", title="", duration_ms=0, extra={},
            error=f"URL scheme 必須是 http / https：{url}",
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Reddit 特例:改寫 old.reddit + 純 HTTP 抓取(繞過 www JS 驗證牆 / 瀏覽器指紋封鎖)──
    _reddit_url = _maybe_reddit_static_url(url)
    if _reddit_url:
        logger.info(f"[{step_name}] Reddit → 改用 old.reddit + 純 HTTP 抓取:{_reddit_url}")
        return await _fetch_static_http(
            fetch_url=_reddit_url, requested_url=url, output_path=output_path,
            timeout=timeout, logger=logger, step_name=step_name, started_at=t0,
        )

    # ── Tier 1：Crawl4AI in sandbox ────────────────────────────────
    # 在 thread pool 跑（_run_crawl4ai_in_sandbox 是 sync + 內部會即時 print log），
    # 不擋 asyncio event loop → frontend 可以繼續 poll log file 看到進度
    logger.info(f"[{step_name}] Tier 1：Crawl4AI 抓取 {url}")
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _run_crawl4ai_in_sandbox(
            url=url, output_path=output_path,
            js_render=js_render, wait_for_selector=wait_for_selector,
            cookies=_parse_cookies(cookies),
            interactions=interactions or [],
            download_assets=download_assets,
            scroll_count=scroll_count,
            target_post_count=target_post_count,
            timeout=timeout, logger=logger, step_name=step_name,
        ),
    )

    # 偵測 Cloudflare / 反爬。三個獨立訊號才 fallback FlareSolverr：
    #   A. 真 CF 跡象：status 403/503,或 HTML 含 CF challenge marker
    #   B. 上游 HTTP 錯誤：not ok 且有有效 status_code(非 0,如 5xx)— 站點真的回錯
    #   C. 成功但內容過薄(< 200 bytes)— 反爬常見「200 + 空殼」(例 nowsecure.nl 6 字殼)
    #
    # 故意排除:status_code=0 + ok=False 的情況。這代表 Crawl4AI 內部失敗
    # (Playwright timeout / SPA wait 條件沒命中等),不是被 CF 擋。
    # 這時 FlareSolverr (Puppeteer) 救不了、白繞 ~10 秒、徒增延遲。
    md_len = len((result.markdown or "").strip())
    is_cf_signal = (
        result.status_code in (403, 503)
        or _looks_like_cf_challenge(result.html, result.markdown)
    )
    is_upstream_error = (not result.ok) and result.status_code not in (0, None)
    is_thin = md_len < 200 and result.ok  # 只在「成功但內容薄」時算反爬空殼
    # crawl4ai 自己偵測到反爬 / captcha(Amazon 等)→ 訊息含這些字。
    # 這類 status=0 + ok=False 之前被當「內部錯誤」跳過 fallback,但其實該讓
    # Tier 2 FlareSolverr(真瀏覽器 Puppeteer)試一次 — 它常能過 headless 過不了的反爬。
    _errlow = (result.error or "").lower()
    is_antibot = (not result.ok) and any(
        k in _errlow for k in ("anti-bot", "antibot", "bot protection",
                                "captcha", "blocked by", "access denied", "verify you are human")
    )

    # 快速失敗優化:known_hard + 需 cookie 的站、使用者又沒給 cookie →
    # FlareSolverr(無 session)和 SPA 重試(內容鎖在登入後)都救不了,
    # 不要白等(蝦皮實測省 ~110s)。直接跳過兩段重試、走後面的 cookie 指引失敗。
    _reg_hard = _lookup_host_registry(url)
    _no_cookie_hardwall = bool(_reg_hard and _reg_hard.get("needs_cookie")) and not _parse_cookies(cookies)

    needs_fallback = (cloudflare_fallback and not _no_cookie_hardwall
                      and (is_cf_signal or is_upstream_error or is_thin or is_antibot))

    if _no_cookie_hardwall and (not result.ok or is_thin):
        logger.info(
            f"[{step_name}] 此站需登入 cookie、但沒提供 → 跳過 FlareSolverr / SPA 重試"
            f"(都救不了)、直接給 cookie 設定指引"
        )

    # 跳過 fallback 但有失敗跡象 → 紀錄為 Crawl4AI 內部問題,讓 user 知道不是 CF
    if cloudflare_fallback and not needs_fallback and not result.ok and not _no_cookie_hardwall:
        logger.info(
            f"[{step_name}] Tier 1 抓取失敗（status={result.status_code}, ok={result.ok}）"
            f" — 判定為 Crawl4AI 內部錯誤而非 Cloudflare,不 fallback FlareSolverr"
        )

    if needs_fallback:
        if is_cf_signal:
            reason = "結果像被 Cloudflare 擋"
        elif is_antibot:
            reason = "Crawl4AI 偵測到反爬 / captcha"
        elif is_upstream_error:
            reason = f"上游回非 2xx（status={result.status_code}）"
        else:
            reason = f"內容過薄（{md_len} bytes）疑似反爬空殼"
        logger.warning(
            f"[{step_name}] Tier 1 {reason}"
            f"（status={result.status_code}, ok={result.ok}）→ 切到 Tier 2 FlareSolverr"
        )
        flare = await _run_flaresolverr(url=url, cookies=_parse_cookies(cookies),
                                        timeout=timeout, logger=logger, step_name=step_name)
        # 「不更差才覆寫」保護：Tier 2 拿到更多內容 / 或 Tier 1 本來就壞 → 才用 Tier 2
        # 否則保留 Tier 1 — 避免 Tier 2 也回空殼把更長的 Tier 1 結果換掉
        if flare.ok and (
            len((flare.markdown or "").strip()) > md_len
            or not result.ok
        ):
            result = flare
        elif flare.ok:
            logger.info(
                f"[{step_name}] Tier 2 拿到的內容（{len((flare.markdown or '').strip())} bytes）"
                f"沒比 Tier 1 多，保留 Tier 1 結果"
            )

    # ── SPA 自動重試 ──────────────────────────────────────────────
    # 條件三項都成立才觸發：
    #   1. 第一輪有抓到東西（result.ok）但內容偏少（_looks_thin）
    #   2. 使用者沒手動設 wait_for_selector / interactions（不要踩在他們頭上）
    #   3. 不是被 CF 擋（CF fallback 已處理）
    user_set_overrides = bool(wait_for_selector) or bool(interactions)
    if (result.ok and not user_set_overrides and _looks_thin(result)
            and result.tier == "crawl4ai" and not _no_cookie_hardwall):
        md_len = len(result.markdown or "")
        link_n = len((result.extra or {}).get("links_internal") or [])
        # 本站若在 registry 有專屬 wait_selector(例電商價格錨點)→ 重試用它、否則用通用
        _reg = _lookup_host_registry(url)
        _retry_selector = (_reg or {}).get("wait_selector") or _SPA_FALLBACK_WAIT_SELECTOR
        logger.warning(
            f"[{step_name}] 第一輪結果偏瘦（{md_len} bytes / {link_n} 內部連結），"
            f"很可能是 SPA 沒 hydrate 完 → 啟用 SPA 智慧重試（自動加 wait_for + 滾動）"
            + (f"、套用 {_reg['match'][0]} 專屬 selector" if _reg and _reg.get("wait_selector") else "")
        )
        retry = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _run_crawl4ai_in_sandbox(
                url=url, output_path=output_path,
                js_render=True,                                 # SPA 必開
                wait_for_selector=_retry_selector,
                cookies=_parse_cookies(cookies),
                interactions=[],                                # 走智慧滾動、不再用寫死序列
                download_assets=download_assets,
                # 沒設目標數時 retry 也走自動模式（一直滾到 scrollHeight 不變）
                scroll_count=scroll_count,
                target_post_count=target_post_count,
                timeout=timeout, logger=logger,
                step_name=f"{step_name} (SPA retry)",
            ),
        )
        # 比對：取較豐富的那輪
        if retry.ok:
            r_md = len(retry.markdown or "")
            r_links = len((retry.extra or {}).get("links_internal") or [])
            if r_md > md_len or r_links > link_n:
                logger.info(
                    f"[{step_name}] SPA 重試成功：{md_len} → {r_md} bytes、"
                    f"{link_n} → {r_links} 內部連結"
                )
                result = retry
            else:
                logger.info(f"[{step_name}] SPA 重試結果沒比較好（保留原本）")
        else:
            logger.warning(f"[{step_name}] SPA 重試失敗（保留原本）：{retry.error}")

    # ── known_hard 站誠實告知 ─────────────────────────────────────
    # 本站在 registry 標 known_hard、而最終結果失敗或仍偏瘦(只拿到殼)→
    # 把「為什麼難爬 + 該怎麼辦(多半是貼 cookie / 換來源)」附進訊息,
    # 讓使用者 / output.expect 驗證看到具體原因、不是一句籠統失敗。純訊息、不改流程。
    _reg_final = _lookup_host_registry(url)
    _thin_or_fail = (not result.ok) or _looks_thin(result)
    _login_wall = _looks_like_login_wall(result)
    _needs_cookie = bool(_reg_final and _reg_final.get("needs_cookie")) or _login_wall
    if _thin_or_fail and ((_reg_final and _reg_final.get("known_hard")) or _login_wall):
        _parts = []
        if _reg_final and _reg_final.get("note"):
            _parts.append(_reg_final["note"])
        elif _login_wall:
            _parts.append("這個頁面看起來需要先登入才看得到完整內容(回到登入 / 國家選擇 / 攔截頁)。")
        else:
            _parts.append("此站已知難爬(自家反爬 / 重 SPA)、匿名常只拿到殼。")
        if _needs_cookie:
            _parts.append(_COOKIE_HELP)
        _hint = " ".join(_parts)
        logger.warning(f"[{step_name}] ⚠ {_hint}")
        # 即使 Tier1 回 200 但其實是殼(登入牆 / known_hard 需 cookie + thin)→ 視為失敗,
        # 否則會把「登入頁 / 空殼」當成功內容往下傳。需 cookie 才看得到 → 明確 fail + 友善指引。
        _shell_needs_cookie = _needs_cookie and _looks_thin(result)
        if (not result.ok) or (_login_wall and _looks_thin(result)) or _shell_needs_cookie:
            result.ok = False
            result.error = (result.error or "只拿到登入 / 攔截頁 / 空殼、沒有目標內容").rstrip("。") + "。\n" + _hint

    if not result.ok:
        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    # ── 寫檔 ──────────────────────────────────────────────────────
    md = _wrap_with_frontmatter(result, requested_url=url, started_at=t0,
                                tier_used=result.tier)
    Path(output_path).write_text(md, encoding="utf-8")
    logger.info(f"[{step_name}] ✓ 已寫入：{output_path}（{len(md):,} bytes、tier={result.tier}）")

    result.markdown = md
    result.duration_ms = int((time.time() - t0) * 1000)
    return result


# ── 論壇 / 討論區模式：列表頁 → 抽子頁連結 → 並行抓子頁 → 合併單一 markdown ───
# 取代之前「使用者拉 skill 節點讓 LLM 自己寫 crawl4ai code 抓 N 篇」的脆弱方案。
# 整段 deterministic、無 LLM、不會有 hardcode 偷懶 bug。
_AUTO_CHILD_LINK_PATTERNS = [
    r'/comments/[a-z0-9]+/[\w-]+',     # Reddit
    r'/p/\d+',                          # Dcard
    r'/post/[a-z0-9-]+',                # Tumblr / wordpress.com / Threads
    r'/posts/[\w-]+',                   # ProductHunt / Indie Hackers(slug、非純數字)
    r'/article/[\w-]+',                 # 新聞站常見
    r'/articles/[\w-]+',
    r'/status/\d+',                     # Twitter / X
    r'/threads/[\w-]+',
    r'/video/[\w-]+',                   # TikTok 等影音
    r'/item\?id=\d+',                   # Hacker News
    r'/M\.\d+\.A\.[A-F0-9]+',           # PTT (M.timestamp.A.hash)
    r'/bbs/[\w-]+/M\.\d+\.A\.[A-F0-9]+',  # PTT 含板名
    r'/topicdetail\.php\?f=\d+&t=\d+',  # Mobile01 (topicdetail.php?f=板&t=文章)
    # ── 購物 / 電商商品詳情頁 ──────────────────────────────
    # 注意:pattern 只負責「從列表頁認出商品連結」;站點 anti-bot 擋不擋是另一回事。
    r'-i\.\d+\.\d+',                    # 蝦皮 Shopee (name-i.shopid.itemid)
    r'GoodsDetail\.jsp\?i_code=\d+',    # momo 購物網
    r'/prod/[A-Z0-9]+',                 # PChome 24h
    r'/item/(?:show\?)?\d{8,}',         # 露天拍賣 Ruten:/item/show?<id> 或 /item/<id>(實測確認)
    r'/dp/[A-Z0-9]{10}',                # Amazon (/dp/ASIN)
    r'/gp/product/[A-Z0-9]{10}',        # Amazon (/gp/product/ASIN)
    r'/itm/\d+',                        # eBay
    r'/\d{6,}\.p(?:\?|$|/)',            # BestBuy:/site/<slug>/<skuId>.p?skuId=…(實測格式)
    r'/ip/[\w-]+/\d+',                  # Walmart
    r'item\.htm\?.*\bid=\d+',           # 淘寶 / 天貓
    r'item\.jd\.com/\d+\.html',         # 京東 JD
]


# 純追蹤用 query 參數 — 去重時砍掉(它們不影響「是哪一篇」)。
# 保留的是帶文章 / 商品 ID 的參數(t / id / i_code / f 等)。
_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "msclkid", "ref", "ref_", "ref_src",
    "spm", "scm", "share_token", "share_tag", "share_crt_v",
    "_branch_match_id", "from", "source", "src", "fromurl",
}


def _extract_child_links_from_markdown(
    md: str, pattern: str = "", max_count: int = 10,
    skip_pinned_blocks: bool = True,
    parent_host: str = "",
) -> list[str]:
    """從列表頁 markdown 抽子頁連結。

    pattern：
      - "" 或 "auto" → 用 _AUTO_CHILD_LINK_PATTERNS（涵蓋 Reddit/Dcard/PTT/HN 等 12 種）
      - 其他字串 → 當 regex 用，需匹配 URL 的某段（用 re.search 而非 fullmatch）

    skip_pinned_blocks：避開「公告 / Pinned / 超級討論串 / 社群精選」這類釘選區塊
    （這些通常是版主公告、不是真正討論文）

    parent_host：列表頁的 hostname；非空時會過濾掉「不同 domain」的子頁 URL。
    這條對所有站點都通用安全 — 列表頁的子頁本來就應該是同站內容；
    跨站連結（外部新聞、help 文件、廣告連結）抓回來幾乎一定不是要的東西。
    例：r/LocalLLaMA 列表頁出現 support.reddithelp.com 文件、preview.redd.it 圖檔，
    都會被這條擋下。

    回傳：去重後的子頁 URL 列表（最多 max_count 個）
    """
    if not md:
        return []
    # 抽所有 markdown link target
    url_re = re.compile(r'\]\((https?://[^)\s]+)\)')

    # 釘選區塊過濾：分隔線之間若帶釘選關鍵字、整塊跳過
    if skip_pinned_blocks:
        # 用 markdown 的 hr (`* * *` / `---`) 切塊
        blocks = re.split(r'\n\s*[\*\-]\s*[\*\-]\s*[\*\-]\s*\n', md)
    else:
        blocks = [md]

    # 比對 pattern
    custom_re = None
    if pattern and pattern.strip().lower() not in ("", "auto"):
        try:
            custom_re = re.compile(pattern)
        except re.error:
            custom_re = None  # 壞 regex → fallback 到 auto

    auto_re_list = [re.compile(p) for p in _AUTO_CHILD_LINK_PATTERNS]

    def _match(url: str) -> bool:
        if custom_re is not None:
            return bool(custom_re.search(url))
        return any(r.search(url) for r in auto_re_list)

    seen = set()
    out: list[str] = []
    for block in blocks:
        block_urls = url_re.findall(block)
        block_matches = [u for u in block_urls if _match(u)]
        # 釘選區塊跳過 — 但**只跳「小區塊」**。
        # 真正的釘選 / 公告區只有寥寥幾筆;若整個商品 / 文章清單擠成一大塊
        # (例:eBay 搜尋結果整頁一塊、塊內剛好有 "Announcement" 字樣),
        # 不能因為一個關鍵字就把整塊幾百筆連結全丟掉。
        # 規則:塊內命中子頁 pattern 的連結 ≥ 5,視為「主清單」、不跳。
        if (skip_pinned_blocks and len(block_matches) < 5 and any(
            kw in block for kw in ("超級討論串", "社群精選貼文", "公告", "Pinned",
                                     "moderator post", "Announcement", "📌")
        )):
            continue
        for url in block_matches:
            if not _match(url):
                continue
            try:
                p = urlparse(url)
                # 同 domain 過濾（通用安全；parent_host 為空時不啟用、保留舊行為）
                if parent_host and (p.hostname or "") != parent_host:
                    continue
                # 去重 key = hostname + path + 「有意義的 query 參數」。
                # query 不能整個丟 — 很多站(Mobile01 topicdetail.php?t=、momo
                # GoodsDetail.jsp?i_code=、淘寶 item.htm?id=)文章/商品 ID 在 query 裡、
                # 整個 path 都一樣;若忽略 query 會把整批不同文章誤判成同一篇而塌成 1 個。
                # 但純追蹤參數(utm_* / fbclid / spm 等)要砍掉、否則同篇不同來源連結不會去重。
                _meaningful_q = "&".join(
                    f"{k}={v}" for k, v in sorted(parse_qsl(p.query))
                    if k.lower() not in _TRACKING_QUERY_KEYS
                )
                key = (p.hostname or "", p.path.rstrip("/"), _meaningful_q)
            except Exception:
                key = ("", url)
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= max_count:
                return out
    return out


async def crawl_list_with_children(
    *,
    list_url: str,
    output_path: str,
    js_render: bool = True,
    wait_for_selector: str = "",
    cloudflare_fallback: bool = True,
    cookies: str = "",
    interactions: Optional[list[dict]] = None,
    download_assets: bool = False,
    scroll_count: int = 0,
    target_post_count: int = 0,
    child_link_pattern: str = "",
    max_children: int = 10,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    logger: logging.Logger = log,
    step_name: str = "web_crawler",
) -> CrawlResult:
    """論壇 / 列表式爬蟲：先抓列表頁、抽前 N 個子頁連結、並行抓子頁、合併成單一 markdown。

    輸出格式（合併到 output_path）：
      # 列表頁：<title>
      ...列表頁 markdown...
      ---
      ---
      # 子頁 1/N：<title>
      URL：<url>
      ...子頁 markdown...
      ---
      ---
      # 子頁 2/N：...

    下游 skill 節點直接讀這個檔做摘要、不用自己寫爬蟲程式。
    """
    import tempfile
    t0 = time.time()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Reddit:先把列表 URL 統一成 old.reddit,讓 parent_host 與抽出的子頁同網域(否則
    # 子頁連結是 old.reddit.com、parent_host 卻是 www.reddit.com → 同網域過濾會誤殺)。
    _rr = _maybe_reddit_static_url(list_url)
    if _rr:
        list_url = _rr

    # 1. 抓列表頁（用臨時檔，最後不留下）
    list_tmp = tempfile.NamedTemporaryFile(suffix="_list.md", delete=False)
    list_tmp.close()
    try:
        logger.info(f"[{step_name}] ▶ 階段 1/2：抓列表頁 {list_url}")
        list_result = await crawl_single_url(
            url=list_url, output_path=list_tmp.name,
            js_render=js_render,
            wait_for_selector=wait_for_selector,
            cloudflare_fallback=cloudflare_fallback,
            cookies=cookies,
            interactions=interactions or [],
            download_assets=download_assets,
            scroll_count=scroll_count,
            target_post_count=target_post_count,
            timeout=timeout,
            logger=logger,
            step_name=f"{step_name} (list)",
        )
        if not list_result.ok:
            return list_result
        list_md = list_result.markdown or ""

        # 2. 抽子頁連結（同 domain 過濾 — 通用安全，避免跨站連結混進子頁清單）
        try:
            _parent_host = urlparse(list_url).hostname or ""
        except Exception:
            _parent_host = ""
        child_urls = _extract_child_links_from_markdown(
            list_md, pattern=child_link_pattern, max_count=max_children,
            parent_host=_parent_host,
        )
        logger.info(f"[{step_name}] ▶ 從列表頁抽出 {len(child_urls)} 個子頁連結"
                    f"（pattern={child_link_pattern or 'auto'}、max={max_children}）")

        if not child_urls:
            # 沒抽到子頁 → 至少把列表頁存下來、不要整個 step 失敗
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(list_md)
            logger.warning(f"[{step_name}] ⚠ 沒抽到子頁、只輸出列表頁。"
                           f"檢查 child_link_pattern 是否符合此站的子頁 URL 結構")
            list_result.duration_ms = int((time.time() - t0) * 1000)
            return list_result

        # 3. 並行抓子頁（Semaphore 限 5、避免同站對方限流）
        logger.info(f"[{step_name}] ▶ 階段 2/2：並行抓 {len(child_urls)} 個子頁（concurrency=5）")
        sem = asyncio.Semaphore(5)

        async def _crawl_one(url: str) -> tuple[str, Optional[str], str]:
            async with sem:
                tmp = tempfile.NamedTemporaryFile(suffix="_child.md", delete=False)
                tmp.close()
                try:
                    r = await crawl_single_url(
                        url=url, output_path=tmp.name,
                        js_render=js_render,
                        wait_for_selector=wait_for_selector,
                        cloudflare_fallback=cloudflare_fallback,
                        cookies=cookies,
                        interactions=[],
                        download_assets=False,
                        scroll_count=0,
                        target_post_count=0,
                        timeout=timeout,
                        logger=logger,
                        step_name=f"{step_name} (child)",
                    )
                    return (url, r.markdown if r.ok else None, r.title or "")
                except Exception as e:
                    logger.warning(f"[{step_name}] 子頁失敗：{url} — {e}")
                    return (url, None, "")
                finally:
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass

        child_results = await asyncio.gather(*[_crawl_one(u) for u in child_urls])

        # 4. 合併成單一 markdown
        #    先確定性判每個子頁是否為反爬殼:殼 → 內文標失敗、保留標題與連結(不靠下游 LLM)。
        processed: list[tuple[str, str, str, str]] = []  # (url, title, body, status: ok|shell|fail)
        for (url, md, title) in child_results:
            if md and _looks_like_render_shell(md):
                processed.append((url, title, CHILD_SHELL_FAILED, "shell"))
            elif md:
                processed.append((url, title, md.strip(), "ok"))
            else:
                processed.append((url, title, "（抓取失敗）", "fail"))
        ok_count = sum(1 for *_, s in processed if s == "ok")
        shell_count = sum(1 for *_, s in processed if s == "shell")
        _summary = f"子頁數: {ok_count}/{len(child_urls)} 取得正文"
        if shell_count:
            _summary += f"、{shell_count} 反爬殼(僅標題與連結)"
        # 結構標記都用半形冒號 ":"（不是全形「：」），跟一般 prompt / 下游
        # skill 寫的 regex 對齊。之前用全形時，LLM 看 prompt 寫 `# 子頁 N/M:`
        # regex 抓不到全形 → 多繞 5 分鐘做 debug。
        parts: list[str] = [
            f"# 列表頁: {list_result.title or list_url}",
            "",
            f"來源 URL: {list_url}",
            f"抓取時間: {datetime.now().isoformat(timespec='seconds')}",
            _summary,
            "",
            "---",
            "",
            list_md.strip(),
            "",
        ]
        for i, (url, title, body, _status) in enumerate(processed, 1):
            parts.extend([
                "",
                "---",
                "---",
                "",
                f"# 子頁 {i}/{len(child_urls)}: {title or '(無標題)'}",
                f"URL: {url}",
                "",
                body,
                "",
            ])
        combined = "\n".join(parts)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(combined)
        logger.info(f"[{step_name}] ✓ 合併寫入 {output_path}（{len(combined):,} bytes、"
                    f"列表 + {ok_count}/{len(child_urls)} 子頁取得正文"
                    + (f"、{shell_count} 反爬殼" if shell_count else "") + "）")

        # 用 list_result 當 base、改 markdown / duration / title
        list_result.markdown = combined
        list_result.duration_ms = int((time.time() - t0) * 1000)
        if list_result.title:
            list_result.title = f"{list_result.title} + {ok_count} 子頁"
        return list_result
    finally:
        try:
            os.unlink(list_tmp.name)
        except Exception:
            pass


# ── Tier 1：Crawl4AI in sandbox ─────────────────────────────────────

def _run_crawl4ai_in_sandbox(
    *,
    url: str,
    output_path: str,
    js_render: bool,
    wait_for_selector: str,
    cookies: list[dict],
    interactions: list[dict],
    download_assets: bool,
    scroll_count: int = 0,
    target_post_count: int = 0,
    timeout: int,
    logger: logging.Logger,
    step_name: str,
) -> CrawlResult:
    """把 Crawl4AI 呼叫包成一個 Python 腳本丟進沙盒；以 Popen 串流 stdout，
    每一行非 JSON 的輸出都即時 logger.info 出去，frontend log polling 就能
    看到爬取進度（Crawl4AI 自己的 [INIT]/[FETCH]/[SCRAPE] 訊息也會即時顯示）。
    最後一行 JSON dict 是結構化結果，用來組 CrawlResult。

    JS 組合邏輯：
      - 使用者填了 interactions（明確 click / scroll 序列）→ 用使用者的，跳過智慧滾動
      - 沒填 interactions → 用 _smart_scroll_js（走 scroll_count / target_post_count / 自動模式）
    """
    if interactions:
        js_code = _interactions_to_js(interactions)
        if scroll_count > 0 or target_post_count > 0:
            logger.info(
                f"[{step_name}] 使用者已填 interactions、忽略 scroll_count/target_post_count"
            )
    else:
        js_code = _smart_scroll_js(
            scroll_count=scroll_count,
            target_post_count=target_post_count,
        )
        if scroll_count > 0:
            logger.info(f"[{step_name}] 智慧滾動：固定滾動 {min(scroll_count, _SMART_SCROLL_MAX_ROUNDS)} 次")
        elif target_post_count > 0:
            logger.info(f"[{step_name}] 智慧滾動：滾到至少 {target_post_count} 個貼文連結（不設輪數上限、110s 內 deadline）")
        else:
            logger.info(f"[{step_name}] 智慧滾動：預設模式（滾 {_DEFAULT_SCROLL_ROUNDS} 次；要更多請進進階設定）")
    # output_path 在 host 是 Windows 路徑、容器內是同一個 mount 的 /mnt/c/... 路徑
    # 沙盒同路徑映射策略：傳 WSL 形式進腳本，os.path 操作就直接通
    output_wsl = windows_to_wsl_path(output_path) if output_path else ""
    script = _build_crawl4ai_script(
        url=url, output_path_wsl=output_wsl,
        js_render=js_render, wait_for_selector=wait_for_selector,
        cookies=cookies, js_code=js_code, download_assets=download_assets,
    )

    # 把 script 寫到 sandbox/_tmp/（已 bind-mount 到容器內同路徑）
    tmp_win = _write_code_tempfile(script, suffix=".py")
    try:
        script_wsl = windows_to_wsl_path(tmp_win)
        # python -u：完全關閉 stdout buffering，讓 print 即時送出去（無這旗標的話 print
        # 會被 Python 攢一段才寫；containerized + pipe 下會嚴重卡 log）
        cmd = _docker_exec_cmd(None, ["python", "-u", script_wsl])
        return _stream_subprocess(cmd, url=url, timeout=timeout, logger=logger,
                                  step_name=step_name)
    finally:
        try:
            os.unlink(tmp_win)
        except Exception:
            pass


def _stream_subprocess(
    cmd: list[str], *,
    url: str, timeout: int,
    logger: logging.Logger, step_name: str,
) -> CrawlResult:
    """以 Popen 串 stdout 行；每行非 JSON 的丟 logger.info（即時）；最後一行 JSON 拿來組 result。
    stderr 合併進 stdout（Crawl4AI 的部分進度訊息走 stderr，併進來省事）。
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,    # 合併，所有輸出走同一條 pipe
            # 不指定 text/encoding,用 bytes;每行讀進來後用 _decode_subprocess_output
            # 自動偵測編碼(wsl.exe 失敗時輸出 UTF-16 LE、容器內 python 走 utf-8)
            bufsize=1,                   # line-buffered(搭配腳本 -u 才有效)
        )
    except FileNotFoundError:
        return _err(url, "找不到 wsl 指令；確認你在 Windows host 跑 backend", tier="crawl4ai")
    except Exception as e:
        return _err(url, f"啟動沙盒 subprocess 失敗：{e}", tier="crawl4ai")

    deadline = time.time() + timeout
    last_json: Optional[dict] = None
    raw_lines: list[str] = []
    raw_lines_bytes: list[bytes] = []  # 失敗路徑時整段 join 解碼,UTF-16 LE 不會被 \n byte 切壞

    try:
        # 一行一行讀,**每行各自**用 _decode_subprocess_output 自動偵測編碼
        # (utf-8 / UTF-16 LE / mbcs)。原因:同一 subprocess 可能先吐 utf-8
        # (我們 python 腳本自己的 print)後接 UTF-16 LE(wsl.exe 失敗訊息),
        # 不能整流鎖一種編碼。
        #
        # 切行用 `\n`(byte 0x0a)。UTF-16 LE 的 line ending 是 \n\x00,
        # 找到 \n 後若下一 byte 是 \x00 就一起跳掉,以免下一行從半個字開始。
        buffer = b""
        while True:
            if time.time() > deadline:
                # 超時:強制砍 + 收尾
                try:
                    proc.kill()
                    proc.wait(timeout=3.0)
                except Exception:
                    pass
                logger.warning(f"[{step_name}] Crawl4AI timeout({timeout}s),已強制終止")
                return _err(url, f"Crawl4AI timeout({timeout}s)", tier="crawl4ai")

            chunk = proc.stdout.read1(4096)
            if not chunk:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue

            buffer += chunk
            raw_lines_bytes.append(chunk)

            while True:
                idx = buffer.find(b"\n")
                if idx == -1:
                    break
                line_bytes = buffer[:idx]
                next_pos = idx + 1
                # UTF-16 LE 的 \n\x00:下一 byte 是 \x00 就跳掉(避免下一行起手是孤立的 \x00)
                if next_pos < len(buffer) and buffer[next_pos] == 0:
                    next_pos += 1
                buffer = buffer[next_pos:]

                line = _decode_subprocess_output(line_bytes).rstrip("\r")
                if not line:
                    continue
                raw_lines.append(line)

                # 嘗試解析成 JSON dict(最後一行的結構化結果)
                stripped = line.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict) and ("ok" in parsed or "error" in parsed):
                            last_json = parsed
                            # 結果 JSON 不丟 log(會洗版且使用者看不懂)
                            continue
                    except json.JSONDecodeError:
                        pass

                # 進度行:直接 logger.info → 寫進 run log → frontend polling 拿到
                logger.info(f"[{step_name}]   {line}")

        # process 結束,buffer 殘餘(沒以 \n 結尾的最後一段)也 flush 出去
        if buffer:
            tail_line = _decode_subprocess_output(buffer).rstrip("\r\n")
            if tail_line:
                raw_lines.append(tail_line)
                logger.info(f"[{step_name}]   {tail_line}")
    except Exception as e:
        logger.error(f"[{step_name}] 讀取沙盒 stdout 例外：{e}")
        try:
            proc.kill()
        except Exception:
            pass
        return _err(url, f"讀取沙盒 stdout 例外：{e}", tier="crawl4ai")

    # 全部讀完，確認 last_json
    if last_json is None:
        # 把所有 raw bytes 串起來一次解碼。UTF-16 LE 情況下 readline 會在 \n byte 切到
        # 字元中間造成每行 mojibake;整段一次解就正確。再取最後 20 行(非空)。
        all_bytes = b"".join(raw_lines_bytes)
        full_text = _decode_subprocess_output(all_bytes)
        tail_lines = [l for l in full_text.splitlines() if l.strip()][-20:]
        tail = "\n".join(tail_lines)
        return _err(url, f"沙盒回傳沒有 JSON 結果（exit={proc.returncode}）。最後 20 行：\n{tail}",
                    tier="crawl4ai")
    if not last_json.get("ok"):
        return _err(url, last_json.get("error") or "Crawl4AI 失敗（無錯誤訊息）",
                    tier="crawl4ai")

    return CrawlResult(
        ok=True, tier="crawl4ai", url=url,
        final_url=last_json.get("final_url") or url,
        status_code=int(last_json.get("status_code") or 200),
        markdown=last_json.get("markdown") or "",
        html=last_json.get("html") or "",
        title=last_json.get("title") or "",
        error="", duration_ms=0,
        extra={
            "links_internal": last_json.get("links_internal") or [],
            "links_external": last_json.get("links_external") or [],
            "images": last_json.get("images") or [],
            "language": last_json.get("language") or "",
            "downloaded_assets_count": int(last_json.get("downloaded_assets_count") or 0),
        },
    )


def _run_ytdlp_in_sandbox(
    *,
    url: str,
    output_path: str,
    quality: str,
    max_filesize_mb: int,
    max_duration_min: int,
    subs: bool,
    subs_langs: str,
    save_info_json: bool,
    cookies: list[dict],
    timeout: int,
    logger: logging.Logger,
    step_name: str,
) -> CrawlResult:
    """yt-dlp 在沙盒裡跑、走跟 Crawl4AI 同樣的 streaming 流程。"""
    output_wsl = windows_to_wsl_path(output_path) if output_path else ""
    script = _build_ytdlp_script(
        url=url, output_path_wsl=output_wsl,
        quality=quality, max_filesize_mb=max_filesize_mb,
        max_duration_min=max_duration_min,
        subs=subs, subs_langs=subs_langs,
        save_info_json=save_info_json,
        cookies=cookies,
    )
    tmp_win = _write_code_tempfile(script, suffix=".py")
    try:
        script_wsl = windows_to_wsl_path(tmp_win)
        cmd = _docker_exec_cmd(None, ["python", "-u", script_wsl])
        result = _stream_subprocess(cmd, url=url, timeout=timeout, logger=logger,
                                    step_name=step_name)
        # 把 tier 改成 yt-dlp（_stream_subprocess 預設標 crawl4ai）
        if result.ok:
            result.tier = "yt-dlp"
        return result
    finally:
        try:
            os.unlink(tmp_win)
        except Exception:
            pass


def _build_ytdlp_script(
    *,
    url: str,
    output_path_wsl: str,
    quality: str,
    max_filesize_mb: int,
    max_duration_min: int,
    subs: bool,
    subs_langs: str,
    save_info_json: bool,
    cookies: list[dict],
) -> str:
    """產生跑 yt-dlp 的沙盒腳本。
    yt-dlp 會把進度 print 到 stdout（hook 進去格式化）；最後 print 一行 JSON 結果。
    output_path_wsl = host 的 .md 路徑（容器看到同路徑）；同層的資料夾擺實際檔案。
    """
    # 字幕語言預設：繁中 → 簡中 → 英文（依序找有的就抓）
    final_subs_langs = subs_langs.strip() or "zh-TW,zh-Hant,zh-CN,zh-Hans,en"
    # quality → yt-dlp -f 表達式
    # 不強制 mp4 原生編碼（h264+AAC）：YT 大部分短片只給 av1+opus、強制會觸發 ffmpeg 重編碼
    # 浪費 CPU + 檔案略大；既然 AI 後續吃 srt 字幕做摘要、不在意 mp4 編碼，就讓 yt-dlp 自己挑最佳。
    # 如果要播放出聲，用 VLC / 瀏覽器即可（內建 codec 解 av1/opus）。
    quality_to_format = {
        "best": "bv*+ba/b",                              # 不限解析度、最佳
        "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
        "720p": "bv*[height<=720]+ba/b[height<=720]",
        "480p": "bv*[height<=480]+ba/b[height<=480]",
        "360p": "bv*[height<=360]+ba/b[height<=360]",
    }
    fmt_expr = quality_to_format.get(quality, quality_to_format["720p"])

    payload = {
        "url": url,
        "output_path_wsl": output_path_wsl,
        "format": fmt_expr,
        "max_filesize_bytes": int(max_filesize_mb) * 1024 * 1024,
        "max_duration_sec": int(max_duration_min) * 60 if max_duration_min > 0 else 0,
        "subs": bool(subs),
        "subs_langs": [s.strip() for s in final_subs_langs.split(",") if s.strip()],
        "save_info_json": bool(save_info_json),
        "cookies": cookies,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    return f'''
import json, os, sys, time, traceback
PAYLOAD = json.loads({payload_json!r})

def emit(msg):
    print(msg, flush=True)

def _translate_ytdlp_error(e_str: str) -> str:
    """把 yt-dlp 常見錯誤翻譯成使用者看得懂的中文訊息。"""
    s = e_str.lower()
    if "429" in s or "too many requests" in s:
        return (
            "❗ YouTube rate limit (HTTP 429) — 你最近抓太頻繁、IP 被擋了。\\n"
            "\\n"
            "解法：等 5–15 分鐘讓限制自動解除，再試一次。\\n"
            "（YouTube 對每個 IP 一段時間內請求次數有限制；不需要操作什麼，等就好）\\n"
            "\\n"
            "如果頻繁遇到 429：\\n"
            "  - 別連續測同一支影片（cookies 抓多次也會觸發）\\n"
            "  - 多個影片一個一個分批跑、別塞滿 list 一次跑\\n"
            "\\n"
            f"原始錯誤：{{e_str[:200]}}"
        )
    if "private video" in s or "this video is private" in s:
        return (
            "❗ 這是私人影片，沒登入看不到。\\n"
            "\\n"
            "解法：在 panel「進階設定」貼上你登入後的 cookies、再試一次。\\n"
            f"\\n原始錯誤：{{e_str[:200]}}"
        )
    if "sign in to confirm your age" in s or "age-restricted" in s:
        return (
            "❗ 這是年齡限制影片（18+），匿名抓不到。\\n"
            "\\n"
            "解法：在 panel「進階設定」貼上你（成年帳號）登入後的 cookies、再試一次。\\n"
            f"\\n原始錯誤：{{e_str[:200]}}"
        )
    if "members-only" in s or "members only" in s:
        return (
            "❗ 這是會員專屬內容，需要付費會員 cookies。\\n"
            "\\n"
            "解法：在 panel「進階設定」貼上有會員身分的 cookies、再試一次。\\n"
            f"\\n原始錯誤：{{e_str[:200]}}"
        )
    if "video unavailable" in s or "unavailable" in s:
        return (
            "❗ 影片無法存取（已被刪除 / 設為私人 / 地區限制）。\\n"
            "\\n"
            "如果是地區限制，可能要透過 VPN（本工具不直接支援）。\\n"
            f"\\n原始錯誤：{{e_str[:200]}}"
        )
    if "live event will begin" in s or "premiere" in s:
        return (
            "❗ 這是直播 / 首映即將開始的影片，還沒能下載。\\n"
            "\\n"
            "等首映開始後（或結束後）再試。\\n"
            f"\\n原始錯誤：{{e_str[:200]}}"
        )
    # 其他錯誤：原樣回傳
    return f"yt-dlp 下載失敗：{{e_str}}"


def main():
    t0 = time.time()
    emit(f"🎬 yt-dlp 影片下載：{{PAYLOAD['url']}}")
    try:
        import yt_dlp
    except ImportError as e:
        print(json.dumps({{"ok": False, "error": f"yt-dlp 未安裝（{{e}}）；請 setup_sandbox.bat --rebuild"}}, ensure_ascii=False), flush=True)
        return

    # 輸出路徑：output_path_wsl 是 .md 檔，影片放同層
    md_path = PAYLOAD["output_path_wsl"]
    out_dir = os.path.dirname(md_path) if md_path else "/tmp/yt-dlp"
    os.makedirs(out_dir, exist_ok=True)
    base = "video"

    # ── 進度 hook：每 ~1 秒一次（yt-dlp 內部呼叫頻率高、要 throttle）
    last_pct_emit = [0.0]
    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            # total 可能是 None（字幕 / 串流檔案常無法預估）
            # 之前用 max(total or 1, 1) 結果小檔會除 1 → 100,000% 假百分比
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            if time.time() - last_pct_emit[0] >= 1.0:
                speed_mb = (d.get("speed") or 0) / 1024 / 1024
                eta = d.get("eta") or 0
                if total and total > 0:
                    pct = downloaded / total * 100
                    emit(f"  ⏬ {{pct:5.1f}}%  {{speed_mb:5.2f}} MB/s  ETA {{eta}}s")
                else:
                    # 沒 total（字幕 / 小串流）→ 印已下載 KB 取代百分比
                    emit(f"  ⏬ {{downloaded//1024:>4}} KB  {{speed_mb:5.2f}} MB/s")
                last_pct_emit[0] = time.time()
        elif status == "finished":
            emit(f"  ✓ 下載完成：{{d.get('filename', '')[-60:]}}")
        elif status == "error":
            emit(f"  ✗ 下載錯誤：{{d.get('error', '')}}")

    def postprocessor_hook(d):
        when = d.get("status")
        pp = d.get("postprocessor", "")
        if when == "started":
            emit(f"  🔧 後處理：{{pp}}（合成 / 字幕轉換中）")

    # ── 過長 / 過大檢查：用 match_filter 在 download 前 reject
    # 順便 capture info_dict 給後面「0 檔案落地」分支組詳細錯誤用（含實際長度 / 大小估算）
    captured_info = {{"info": None}}
    def match_filter(info_dict, *, incomplete=False):
        captured_info["info"] = info_dict
        if PAYLOAD["max_duration_sec"] > 0:
            dur = info_dict.get("duration") or 0
            if dur and dur > PAYLOAD["max_duration_sec"]:
                return f"影片長度 {{int(dur)}}s 超過上限 {{PAYLOAD['max_duration_sec']}}s"
        return None

    ydl_opts = {{
        "format": PAYLOAD["format"],
        "outtmpl": {{"default": os.path.join(out_dir, base + ".%(ext)s")}},
        "writeinfojson": PAYLOAD["save_info_json"],   # OFF by default — 90% 內容是過期 URL / 格式列表
        "merge_output_format": "mp4",      # 合成成 mp4 容器
        "overwrites": True,                # 重抓一律覆蓋舊檔；避免上次失敗 / 中斷的殘檔被 reuse
        "max_filesize": PAYLOAD["max_filesize_bytes"] or None,
        "match_filter": match_filter,
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "noprogress": True,                # 我們自己 hook 印；關掉 yt-dlp 預設那條 stderr 的進度條（避免雙印）
        "retries": 3,
        "fragment_retries": 3,
        # yt-dlp 2025.10+ YouTube extractor 要 JS runtime 解 n-cipher / signature challenges：
        # 容器有 node.js（Tier 6 給 pptxgenjs 用的），告訴 yt-dlp 走 node 而不是 deno
        # 配上 ejs:github 自動拉 GitHub 上的 challenge solver script（首次跑會下載、之後 cache）
        # 格式：CLI 接受字串、但 Python API 要 dict[str, dict|None] / set
        "js_runtimes": {{"node": {{}}}},
        "remote_components": {{"ejs:github"}},
    }}
    if PAYLOAD["subs"]:
        ydl_opts.update({{
            "writesubtitles": True,
            "writeautomaticsub": True,         # 沒手動字幕時用 YT 的 auto-generated
            "subtitleslangs": PAYLOAD["subs_langs"],
            "subtitlesformat": "srt/best",     # SRT 是 LLM 最易讀格式
            # convert-subs 透過 postprocessor 轉成 srt（如果原始是 vtt）
            "postprocessors": [{{"key": "FFmpegSubtitlesConvertor", "format": "srt"}}],
        }})
    if PAYLOAD["cookies"]:
        # yt-dlp 沒有「直接吃 cookie list」的 API，用 cookiefile 暫存最簡單
        cookiefile = os.path.join(out_dir, ".cookies.txt")
        with open(cookiefile, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\\n")
            for c in PAYLOAD["cookies"]:
                # name value: 沒 domain 的話用 .
                f.write(f"{{c.get('domain') or '.'}}\\tTRUE\\t/\\tFALSE\\t0\\t{{c['name']}}\\t{{c['value']}}\\n")
        ydl_opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            emit("📡 取得影片 metadata...")
            info = ydl.extract_info(PAYLOAD["url"], download=True)
            # 取 entry：playlist 抓第一筆，single 直接拿
            if isinstance(info, dict) and "entries" in info:
                entries = list(info.get("entries") or [])
                info = entries[0] if entries else info
    except yt_dlp.utils.DownloadError as e:
        e_str = str(e)
        emit(f"✗ yt-dlp 下載失敗：{{e_str[:200]}}")
        # 常見錯誤翻譯成友善訊息
        friendly = _translate_ytdlp_error(e_str)
        print(json.dumps({{"ok": False, "error": friendly}}, ensure_ascii=False), flush=True)
        return
    except Exception as e:
        emit(f"✗ 例外：{{e.__class__.__name__}}：{{e}}")
        traceback.print_exc()
        print(json.dumps({{"ok": False, "error": f"{{e.__class__.__name__}}：{{e}}"}}, ensure_ascii=False), flush=True)
        return

    # ── 收集實際下載的檔案 ────────────────────────────────────────
    downloaded_files = []
    subs_files = []
    for fn in sorted(os.listdir(out_dir)):
        if fn.startswith(base + "."):
            full = os.path.join(out_dir, fn)
            size = os.path.getsize(full)
            if fn.endswith(".info.json"):
                downloaded_files.append({{"file": fn, "size": size, "kind": "metadata"}})
            elif fn.endswith(".srt"):
                subs_files.append({{"file": fn, "size": size}})
            elif fn.endswith((".mp4", ".webm", ".mkv", ".m4a", ".mp3")):
                downloaded_files.append({{"file": fn, "size": size, "kind": "video"}})
            else:
                downloaded_files.append({{"file": fn, "size": size, "kind": "other"}})

    # ── 組 markdown 摘要（給下個 skill 節點吃）───────────────────
    title = (info or {{}}).get("title") or ""
    uploader = (info or {{}}).get("uploader") or (info or {{}}).get("channel") or ""
    duration = int((info or {{}}).get("duration") or 0)
    upload_date = (info or {{}}).get("upload_date") or ""
    if upload_date and len(upload_date) == 8:
        upload_date = f"{{upload_date[:4]}}-{{upload_date[4:6]}}-{{upload_date[6:]}}"
    description = (info or {{}}).get("description") or ""
    view_count = (info or {{}}).get("view_count") or 0
    res = ""
    w, h = (info or {{}}).get("width"), (info or {{}}).get("height")
    if w and h:
        res = f"{{w}}x{{h}}"

    def fmt_dur(s):
        s = int(s)
        h, m, s2 = s // 3600, (s % 3600) // 60, s % 60
        return f"{{h}}h {{m}}m {{s2}}s" if h else f"{{m}}m {{s2}}s"

    md_lines = [
        f"# {{title or '影片'}}",
        "",
        f"- **頻道**：{{uploader}}" if uploader else "",
        f"- **上傳日**：{{upload_date}}" if upload_date else "",
        f"- **長度**：{{fmt_dur(duration)}}" if duration else "",
        f"- **解析度**：{{res}}" if res else "",
        f"- **觀看數**：{{view_count:,}}" if view_count else "",
        f"- **原始 URL**：{{PAYLOAD['url']}}",
        "",
        "## 已下載檔案",
    ]
    for f in downloaded_files:
        md_lines.append(f"- `{{f['file']}}`（{{f['size']/1024/1024:.2f}} MB，{{f['kind']}}）")
    if subs_files:
        md_lines.append("")
        md_lines.append("## 字幕檔")
        for f in subs_files:
            # 字幕檔名格式 video.<lang>.srt
            lang = f["file"].replace(base + ".", "").replace(".srt", "")
            md_lines.append(f"- `{{f['file']}}` — {{lang}}（{{f['size']//1024}} KB）")
    if description:
        md_lines.append("")
        md_lines.append("## 影片描述")
        md_lines.append(description[:2000] + ("..." if len(description) > 2000 else ""))

    markdown = "\\n".join([ln for ln in md_lines if ln is not None])

    # 視為失敗：跑完但沒檔案落地（多半是 match_filter 把影片擋下：太長 / 太大）
    # 不擋的話 pipeline 會顯示「✅ 通過」+ 0 檔案、訊號太弱、使用者會誤以為成功
    if not downloaded_files and not subs_files:
        # 從 captured_info（match_filter 抓到的）或 info 取資料；組詳細錯誤含實際長度 + 大小估算
        rej_info = captured_info["info"] or info or {{}}
        actual_dur = int(rej_info.get("duration") or 0)
        actual_title = (rej_info.get("title") or "")[:50]

        # 大小估算：掃 formats 拿最大 filesize（or filesize_approx）
        # YT 通常每個格式都有 filesize_approx；取最大 = 最高品質下這支影片可能的大小
        fmts = rej_info.get("formats") or []
        fsize_candidates = []
        for f in fmts:
            s = f.get("filesize") or f.get("filesize_approx")
            if s:
                fsize_candidates.append(s)
        max_size_mb = max(fsize_candidates) // 1024 // 1024 if fsize_candidates else 0

        # 組使用者目前的上限文字
        limit_hints = []
        if PAYLOAD["max_duration_sec"] > 0:
            limit_hints.append(f"長度上限 {{PAYLOAD['max_duration_sec']//60}} 分")
        if PAYLOAD["max_filesize_bytes"]:
            limit_hints.append(f"大小上限 {{PAYLOAD['max_filesize_bytes']//1024//1024}} MB")
        hint_str = "、".join(limit_hints) or "（未設定上限）"

        emit(f"✗ 0 個檔案落地（多半是被 match_filter 擋）")
        err_lines = [
            "影片被 match_filter 擋住、沒檔案落地。",
            "",
            "── 影片實際資訊 ──",
            f"標題：{{actual_title}}",
            f"長度：{{actual_dur}} 秒（約 {{actual_dur//60}} 分 {{actual_dur%60}} 秒）",
        ]
        if max_size_mb > 0:
            err_lines.append(f"預估大小：最高約 {{max_size_mb}} MB（依抓取的解析度而定，較低解析度會更小）")
        err_lines += [
            "",
            f"── 你目前的上限 ──",
            hint_str,
            "",
            "請進 panel「影片設定」把對應上限調高（或設成 0 = 不限）後重試。",
        ]
        print(json.dumps({{"ok": False, "error": "\\n".join(err_lines)}}, ensure_ascii=False), flush=True)
        return

    emit(f"✓ 完成：{{len(downloaded_files)}} 個檔案 + {{len(subs_files)}} 個字幕檔（{{int((time.time()-t0)*1000)}}ms）")

    out = {{
        "ok": True,
        "url": PAYLOAD["url"],
        "final_url": (info or {{}}).get("webpage_url") or PAYLOAD["url"],
        "status_code": 200,
        "title": title,
        "markdown": markdown,
        "html": "",
        "links_internal": [],
        "links_external": [],
        "images": [],
        "language": (info or {{}}).get("language") or "",
        "downloaded_assets_count": len(downloaded_files) + len(subs_files),
    }}
    print(json.dumps(out, ensure_ascii=False), flush=True)

main()
'''


def _build_crawl4ai_script(
    *,
    url: str,
    output_path_wsl: str,
    js_render: bool,
    wait_for_selector: str,
    cookies: list[dict],
    js_code: str,
    download_assets: bool,
) -> str:
    """產生在沙盒裡跑的 Python 腳本字串。
    腳本一邊跑會 print 進度行（被 host streaming reader 即時 logger.info），
    最後 print 一行 JSON dict 當結構化結果。

    download_assets 由腳本內處理：
      圖片（result.media.images）+ markdown 裡的 PDF / Office 文件連結
      → 用 httpx 下載到 <output_path 同層>/assets/
      → 把 markdown 裡的 URL 替換成 ./assets/<filename>，給後續 skill 節點直接讀本機檔
    """
    payload = {
        "url": url,
        "output_path_wsl": output_path_wsl,
        "js_render": js_render,
        "wait_for_selector": wait_for_selector,
        "cookies": cookies,
        "js_code": js_code,
        "download_assets": download_assets,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    # 用 raw triple-string；裡面別放未 escape 的 """ 或 backslash 干擾
    return f'''
import asyncio, json, os, re, sys, time
import urllib.parse as _up
PAYLOAD = json.loads({payload_json!r})

def emit(msg):
    """進度訊息：print 後 flush，host streaming reader 才即時收得到（不會被 buffer 卡住）"""
    print(msg, flush=True)

async def main():
    t0 = time.time()
    emit(f"🚀 啟動 headless Chromium...")
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    except Exception as e:
        print(json.dumps({{"ok": False, "error": f"crawl4ai 未安裝：{{e}}"}}, ensure_ascii=False), flush=True)
        return

    browser_cfg = BrowserConfig(
        headless=True,
        java_script_enabled=PAYLOAD["js_render"],
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for=PAYLOAD["wait_for_selector"] or None,
        js_code=PAYLOAD["js_code"] or None,
        page_timeout=120000,
    )

    if PAYLOAD["wait_for_selector"]:
        emit(f"⏳ 將等待 selector：{{PAYLOAD['wait_for_selector']}}")
    if PAYLOAD["js_code"]:
        emit(f"▶ JS 互動序列已掛載")
    if PAYLOAD["cookies"]:
        emit(f"🔑 注入 {{len(PAYLOAD['cookies'])}} 個 cookie")

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        # 注入 cookies
        if PAYLOAD["cookies"]:
            try:
                ctx = crawler.crawler_strategy.browser.contexts[0] if crawler.crawler_strategy.browser.contexts else None
                if ctx:
                    await ctx.add_cookies([
                        dict(c, url=PAYLOAD["url"]) if "domain" not in c and "url" not in c else c
                        for c in PAYLOAD["cookies"]
                    ])
            except Exception as e:
                emit(f"⚠ cookie 注入失敗：{{e}}")

        emit(f"📡 導航：{{PAYLOAD['url']}}")
        result = await crawler.arun(url=PAYLOAD["url"], config=run_cfg)
        emit(f"📝 抽取 markdown（{{len(result.markdown or '')}} bytes）")

        # 抽 title（Crawl4AI 不給；自己挖）
        title = ""
        html_blob = result.cleaned_html or result.html or ""
        try:
            m = re.search(r"<title[^>]*>([^<]+)</title>", html_blob, re.I)
            if m:
                title = m.group(1).strip()
            else:
                m2 = re.search(r"^#\\s+(.+)$", result.markdown or "", re.M)
                if m2:
                    title = m2.group(1).strip()
        except Exception:
            pass

        markdown = result.markdown or ""
        images = [m.get("src") for m in (result.media or {{}}).get("images", []) if m.get("src")][:100]
        links_int = [l.get("href") for l in (result.links or {{}}).get("internal", []) if l.get("href")][:200]
        links_ext = [l.get("href") for l in (result.links or {{}}).get("external", []) if l.get("href")][:200]

        # ── 下載附件（圖片 + markdown 裡的 PDF/Office 文件連結）─────────
        downloaded_count = 0
        if PAYLOAD["download_assets"] and PAYLOAD["output_path_wsl"]:
            try:
                import httpx
                assets_dir = os.path.join(os.path.dirname(PAYLOAD["output_path_wsl"]), "assets")
                os.makedirs(assets_dir, exist_ok=True)

                # 收集要下載的 URL：圖片 + markdown 裡的文件類連結
                doc_exts = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                            "csv", "tsv", "txt", "json", "xml", "zip", "rar", "7z")
                doc_urls = re.findall(
                    r"\\]\\((https?://[^\\s)]+\\.(?:" + "|".join(doc_exts) + r"))\\)",
                    markdown, flags=re.I,
                )
                all_urls = list(images) + list(doc_urls)
                # 去重保序
                seen = set()
                all_urls = [u for u in all_urls if not (u in seen or seen.add(u))]

                if not all_urls:
                    emit(f"📦 download_assets=true 但頁面沒抓到圖片或文件連結")
                else:
                    emit(f"📦 開始下載 {{len(all_urls)}} 個附件 → {{assets_dir}}")
                    cookies_dict = {{c["name"]: c["value"] for c in PAYLOAD["cookies"] if c.get("name")}}
                    used_names = set()
                    url_to_local = {{}}
                    with httpx.Client(timeout=30, cookies=cookies_dict, follow_redirects=True,
                                      headers={{"User-Agent": "Mozilla/5.0 (compatible; atlas/web_crawler)"}}) as client:
                        for i, u in enumerate(all_urls, 1):
                            try:
                                abs_u = _up.urljoin(PAYLOAD["url"], u)
                                base = os.path.basename(_up.urlparse(abs_u).path) or f"asset_{{i}}.bin"
                                # 處理沒副檔名 / 名字衝突
                                base = re.sub(r"[^\\w.\\-]+", "_", base)
                                fname = base
                                cnt = 1
                                while fname in used_names:
                                    name, ext = os.path.splitext(base)
                                    fname = f"{{name}}_{{cnt}}{{ext}}"
                                    cnt += 1
                                used_names.add(fname)

                                resp = client.get(abs_u)
                                if resp.status_code == 200 and resp.content:
                                    local = os.path.join(assets_dir, fname)
                                    with open(local, "wb") as f:
                                        f.write(resp.content)
                                    url_to_local[u] = fname
                                    downloaded_count += 1
                                    emit(f"  ✓ ({{i}}/{{len(all_urls)}}) {{fname}} ({{len(resp.content)//1024}}KB)")
                                else:
                                    emit(f"  ✗ ({{i}}/{{len(all_urls)}}) {{base}} HTTP {{resp.status_code}}")
                            except Exception as e:
                                emit(f"  ✗ ({{i}}/{{len(all_urls)}}) {{u[:60]}}... {{e.__class__.__name__}}：{{str(e)[:80]}}")

                    # 把 markdown 裡的 URL 換成 ./assets/<fname>，下個 skill 節點可直接讀本機檔
                    for orig, local_name in url_to_local.items():
                        markdown = markdown.replace(orig, f"./assets/{{local_name}}")
                    emit(f"📦 下載完成：成功 {{downloaded_count}} / 共 {{len(all_urls)}}")
            except ImportError:
                emit(f"⚠ httpx 未安裝、跳過附件下載")
            except Exception as e:
                emit(f"⚠ 附件下載例外（不影響主流程）：{{e}}")

        emit(f"✓ 爬取完成 ({{int((time.time()-t0)*1000)}}ms)")

        out = {{
            "ok": bool(result.success),
            "url": PAYLOAD["url"],
            "final_url": getattr(result, "url", PAYLOAD["url"]),
            "status_code": getattr(result, "status_code", 200) or 200,
            "title": title,
            "markdown": markdown,
            "html": html_blob,
            "links_internal": links_int,
            "links_external": links_ext,
            "images": images,
            "language": "",
            "downloaded_assets_count": downloaded_count,
            "error": getattr(result, "error_message", "") or "",
        }}
        # 結果 JSON：必須是「最後一行」、單行（host 抓最後一行 valid JSON）
        print(json.dumps(out, ensure_ascii=False), flush=True)

asyncio.run(main())
'''


_DEFAULT_SCROLL_ROUNDS = 2  # 預設滾 2 次（跟改動前的 SPA fallback 行為一致、避免一次撈太多）


def _smart_scroll_js(
    *, scroll_count: int = 0, target_post_count: int = 0,
    target_selector: str = "",
) -> str:
    """組智慧滾動 JS。回傳會在頁面 eval 的 async IIFE 字串。

    三種模式（依優先序）：
      1. scroll_count > 0 → 固定滾 N 次（夾到 _SMART_SCROLL_MAX_ROUNDS 上限）
      2. target_post_count > 0 → 滾到 querySelectorAll(target_selector).length >= N 為止
         **不設輪數上限** — 讓使用者能撈到他要的數量；只受底層 page_timeout（120s）約束、
         JS 自己用 110s deadline 主動退、留 buffer 給 crawl4ai 收尾
      3. 兩者皆 0（預設）→ 固定滾 _DEFAULT_SCROLL_ROUNDS（=2）次
         避免「無底站」一次撈過量；要更多就進進階設定指定。
    """
    sel = (target_selector or _SPA_FALLBACK_WAIT_SELECTOR).replace("'", "\\'").replace("\n", " ")
    max_rounds = _SMART_SCROLL_MAX_ROUNDS
    if scroll_count > 0:
        # 固定次數模式：上限取使用者值與安全上限的較小者（避免使用者填 999 跑壞）
        n = min(int(scroll_count), max_rounds)
    elif target_post_count > 0:
        # 目標模式：不設輪數上限 — 只看 deadline（110s，留 10s 給 crawl4ai 收尾）
        # 連續 5 輪 scrollHeight 沒變就主動退（站到底了、再滾也沒用，避免無限空滾）
        n_target = int(target_post_count)
        deadline_ms = 110000
        # 目標模式：邊滾邊累積看過的 URL（Reddit / Twitter 用 virtual scroll，滾過去
        # 的舊元素會從 DOM 移除，querySelectorAll 數量永遠停在某個值附近 —
        # 必須自己 Set 累積、最後塞回頁面當隱藏連結列表，crawl4ai 才抓得到完整清單）
        # 注意：crawl4ai 0.8.6 自己會把這段塞進它的 async wrapper 跑，
        # 寫**裸 statement block** 即可、不要包 IIFE（包了 await 會 fire-and-forget）
        return (
            f"const TARGET = {n_target}; const SEL = '{sel}'; "
            f"const DEADLINE = Date.now() + {deadline_ms}; "
            "const seen = new Set(); "
            "const collect = () => { "
            "  document.querySelectorAll(SEL).forEach(a => { "
            "    if (a.href) seen.add(a.href); "
            "  }); "
            "}; "
            "let prev = -1; let stable = 0; "
            "while (true) { "
            "  collect(); "
            "  if (seen.size >= TARGET) break; "
            "  if (Date.now() > DEADLINE) break; "
            "  const h = document.body.scrollHeight; "
            "  if (h === prev) { stable++; if (stable >= 5) break; } else { stable = 0; } "
            "  prev = h; "
            "  window.scrollTo(0, document.body.scrollHeight); "
            "  await new Promise(r => setTimeout(r, 1500)); "
            "} "
            "collect(); "  # 最後再收一次（最後一次滾完還沒進 next iter 就 break 的情況）
            # 把累積的 URL 注入到頁面底部當隱藏連結列表，crawl4ai 抽 markdown 時才看得到
            "const box = document.createElement('div'); "
            "box.id = '__crawl4ai_collected_links'; "
            "box.style.cssText = 'visibility:hidden;height:1px;overflow:hidden'; "
            "seen.forEach(u => { "
            "  const a = document.createElement('a'); "
            "  a.href = u; a.textContent = u; "
            "  box.appendChild(a); "
            "  box.appendChild(document.createElement('br')); "
            "}); "
            "document.body.appendChild(box);"
        )
    else:
        # 預設模式：固定 _DEFAULT_SCROLL_ROUNDS 次（跟改動前一致）
        n = _DEFAULT_SCROLL_ROUNDS
    # 固定次數模式：也累積 URL — 滾 N 次的目的就是「拿到滾出來的東西」,
    # virtual-scroll 站不累積就會被洗掉（同上理由）
    return (
        f"const N = {n}; const SEL = '{sel}'; "
        "const seen = new Set(); "
        "const collect = () => document.querySelectorAll(SEL).forEach(a => { if (a.href) seen.add(a.href); }); "
        "for (let i = 0; i < N; i++) { "
        "  collect(); "
        "  window.scrollTo(0, document.body.scrollHeight); "
        "  await new Promise(r => setTimeout(r, 1500)); "
        "} "
        "collect(); "
        "const box = document.createElement('div'); "
        "box.id = '__crawl4ai_collected_links'; "
        "box.style.cssText = 'visibility:hidden;height:1px;overflow:hidden'; "
        "seen.forEach(u => { "
        "  const a = document.createElement('a'); a.href = u; a.textContent = u; "
        "  box.appendChild(a); box.appendChild(document.createElement('br')); "
        "}); "
        "document.body.appendChild(box);"
    )


def _interactions_to_js(actions: list[dict]) -> str:
    """把 panel 的 interaction 序列轉成在頁面執行的 JS。
    Crawl4AI 接收的是「會在頁面上 eval」的字串、需要是頂層 async。
    """
    if not actions:
        return ""
    parts = []
    for a in actions:
        t = (a.get("type") or "").lower()
        if t == "click":
            sel = (a.get("selector") or "").replace("'", "\\'")
            parts.append(f"(document.querySelector('{sel}') && document.querySelector('{sel}').click());")
        elif t == "scroll":
            to = (a.get("to") or "bottom").lower()
            if to == "bottom":
                parts.append("window.scrollTo(0, document.body.scrollHeight);")
            elif to == "top":
                parts.append("window.scrollTo(0, 0);")
            else:
                px = int(a.get("pixels") or 1000)
                parts.append(f"window.scrollBy(0, {px});")
        elif t == "wait":
            sec = float(a.get("seconds") or 1.0)
            parts.append(f"await new Promise(r => setTimeout(r, {int(sec*1000)}));")
        elif t == "wait_for":
            sel = (a.get("selector") or "").replace("'", "\\'")
            parts.append(
                "await new Promise(r => { "
                "const c = () => (document.querySelector('" + sel + "') ? r() : setTimeout(c, 100)); "
                "c(); });"
            )
        elif t == "type":
            sel = (a.get("selector") or "").replace("'", "\\'")
            text = (a.get("text") or "").replace("'", "\\'")
            parts.append(
                f"{{ const e = document.querySelector('{sel}'); "
                f"if (e) {{ e.focus(); e.value = '{text}'; "
                f"e.dispatchEvent(new Event('input', {{bubbles: true}})); }} }}"
            )
    # crawl4ai 0.8.6 自己會把這段塞到 async wrapper 裡跑（見 _smart_scroll_js
    # comment），所以這裡寫**裸 statement block**、不再包 IIFE。
    return " ".join(parts)


# ── Tier 2：FlareSolverr ────────────────────────────────────────────

async def _run_flaresolverr(
    *,
    url: str,
    cookies: list[dict],
    timeout: int,
    logger: logging.Logger,
    step_name: str,
) -> CrawlResult:
    """打 FlareSolverr 的 /v1 endpoint 拿 HTML，用 markdownify 轉 markdown。"""
    try:
        import httpx  # host venv 已有
    except ImportError:
        return _err(url, "host 缺 httpx 套件，無法呼叫 FlareSolverr", tier="flaresolverr")

    body: dict = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
    if cookies:
        body["cookies"] = cookies

    # 依序試候選 URL(WSL IP > localhost > 127.0.0.1):Windows host 連 WSL docker port
    # 走 localhost 常不穩、WSL IP 直連穩。連不上(ConnectError)就換下一個。
    # _flaresolverr_candidates() 內含 subprocess.run(同步)→ 丟 executor,不阻塞 event loop
    candidates = await asyncio.get_running_loop().run_in_executor(None, _flaresolverr_candidates)
    data = None
    last_err = ""
    for cand in candidates:
        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as client:
                resp = await client.post(cand, json=body)
            if resp.status_code != 200:
                last_err = f"FlareSolverr 回 {resp.status_code}：{resp.text[:300]}"
                continue
            data = resp.json()
            if cand != candidates[0]:
                logger.info(f"[{step_name}] FlareSolverr 改用 {cand} 成功(localhost 不通)")
            break
        except httpx.ConnectError:
            last_err = f"連不上 {cand}"
            continue
        except Exception as e:
            last_err = f"{cand} 呼叫失敗：{e}"
            continue
    if data is None:
        # WSL IP 若快取過期(WSL 重啟換 IP)→ 清快取讓下次重抓
        _FLARE_IP_CACHE["ip"] = ""
        return _err(
            url,
            f"FlareSolverr 連不上(試過 {candidates};最後錯誤:{last_err})。"
            f"確認 sandbox/docker-compose.yml 的 flaresolverr 容器有起來"
            f"(sandbox 內跑 `docker compose up -d flaresolverr`)。",
            tier="flaresolverr",
        )

    if data.get("status") != "ok":
        return _err(url, f"FlareSolverr 回非 ok：{data.get('message') or data}",
                    tier="flaresolverr")

    sol = data.get("solution") or {}
    html = sol.get("response") or ""
    final_url = sol.get("url") or url
    status_code = int(sol.get("status") or 200)

    markdown = _html_to_markdown(html)
    title = _extract_title(html, markdown)

    return CrawlResult(
        ok=True, tier="flaresolverr", url=url,
        final_url=final_url, status_code=status_code,
        markdown=markdown, html=html, title=title,
        error="", duration_ms=0, extra={},
    )


# ── 工具 helpers ─────────────────────────────────────────────────────

def _resolve_cookie_env_refs(raw: str) -> str:
    """把 cookie 字串裡的 `${VAR_NAME}` 換成環境變數值。

    用途:cookie 是 session 憑證(等同帳密)、不該明文存進 workflow JSON。
    使用者可在 cookie 欄位只填佔位符 `${MY_SHOPEE_COOKIE}`、真值放 backend/.env
    (.env 在敏感 deny list、不進版控)。workflow 存的永遠是佔位符。

    找不到對應環境變數 → 該 token 換成空字串(cookie 自然失效、使用者會察覺)。
    """
    if not raw or "${" not in raw:
        return raw
    import os as _os
    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
        lambda m: _os.environ.get(m.group(1), ""),
        raw,
    )


def _parse_cookies(raw: str) -> list[dict]:
    """容忍三種輸入：
      - `key=value` 一行一個
      - `Cookie: key=v; k2=v2` 整串貼上
      - JSON list（[{name, value, domain?}]）
    另外:輸入裡的 `${VAR}` 會先從環境變數 / .env 解析(見 _resolve_cookie_env_refs)。
    """
    if not raw:
        return []
    raw = _resolve_cookie_env_refs(raw.strip())
    if not raw:
        return []
    raw = raw.strip()
    # JSON list
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [c for c in arr if isinstance(c, dict) and c.get("name")]
        except json.JSONDecodeError:
            pass
    # 純文字：先去掉 Cookie: 前綴、再切 ; 跟 換行
    raw = re.sub(r"^Cookie\s*:\s*", "", raw, flags=re.I)
    out: list[dict] = []
    for chunk in re.split(r"[;\n]+", raw):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v})
    return out


def _looks_like_cf_challenge(html: str, markdown: str) -> bool:
    blob = (html or "") + "\n" + (markdown or "")
    return any(m in blob for m in _CF_MARKERS)


def _extract_last_json_line(s: str) -> Optional[dict]:
    """找最後一個能 parse 成 dict 的行。"""
    for line in reversed(s.splitlines()):
        line = line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _html_to_markdown(html: str) -> str:
    """Tier 2 的 HTML → Markdown 轉換。host 端跑、不依賴沙盒。
    優先 trafilatura（語意保留好）、退 markdownify、再退純文字。"""
    if not html:
        return ""
    try:
        import trafilatura  # 在 sandbox/requirements.txt；host 不一定有
        text = trafilatura.extract(html, output_format="markdown",
                                   include_links=True, include_images=True)
        if text:
            return text
    except ImportError:
        pass
    try:
        from markdownify import markdownify as _markdownify
        # markdownify（MIT 授權）取代 html2text（GPL）；同樣保留連結與圖片，
        # heading_style=ATX 產生「# 標題」（對下游 LLM 較友善、不換行）
        return _markdownify(html, heading_style="ATX")
    except ImportError:
        pass
    # 最差只剩去 HTML tag
    return re.sub(r"<[^>]+>", "", html)


def _extract_title(html: str, markdown: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html or "", re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"^#\s+(.+)$", markdown or "", re.M)
    if m2:
        return m2.group(1).strip()
    return ""


def _wrap_with_frontmatter(r: CrawlResult, *, requested_url: str, started_at: float,
                          tier_used: str) -> str:
    """把 markdown 包上 YAML frontmatter；對 LLM 後續節點最友善。"""
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    word_count = len(re.findall(r"\w+", r.markdown))
    fm: dict = {
        "url": requested_url,
        "final_url": r.final_url,
        "title": r.title or "",
        "fetched_at": fetched_at,
        "status_code": r.status_code,
        "word_count": word_count,
        "language": (r.extra or {}).get("language", ""),
        "links_internal_count": len((r.extra or {}).get("links_internal", [])),
        "links_external_count": len((r.extra or {}).get("links_external", [])),
        "images_count": len((r.extra or {}).get("images", [])),
        "crawler": {
            "engine": tier_used,
            "duration_ms": int((time.time() - started_at) * 1000),
        },
    }
    return _format_frontmatter(fm) + "\n" + (r.markdown or "")


def _format_frontmatter(fm: dict) -> str:
    """簡單 YAML dumper（避免引入 PyYAML on host 純為了這幾行）。"""
    lines = ["---"]

    def emit(key: str, val):
        if isinstance(val, dict):
            lines.append(f"{key}:")
            for k2, v2 in val.items():
                lines.append(f"  {k2}: {_yaml_scalar(v2)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")

    for k, v in fm.items():
        emit(k, v)
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # 含特殊字元 / 多行 → 用 double-quoted、escape "
    if any(c in s for c in ":#\n[]{}\"") or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def _detect_login_wall(requested_url: str, final_url: str, html: str) -> str:
    """回「被導去登入/驗證牆」的原因；沒有就回空字串。

    為什麼需要這支：站點擋爬時不一定回 4xx。實測 Reddit 是 302 → 登入頁 →
    200 + 308KB，狀態碼與長度都正常，靠那兩項判斷會把登入頁當成抓取成功。

    判定盡量保守 —— 只在「原本要的網址跟最終網址不是同一件事」時才看內容特徵，
    避免把本來就叫「登入」的頁面誤判（使用者可能真的要抓登入頁）。
    """
    from urllib.parse import urlparse
    try:
        rp, fp = urlparse(requested_url), urlparse(final_url or requested_url)
    except Exception:
        return ""
    # 使用者原本就要抓登入頁 → 不判定
    if any(k in (rp.path or "").lower() for k in ("/login", "/signin", "/sign_in", "/auth")):
        return ""
    moved = (rp.path or "/").rstrip("/") != (fp.path or "/").rstrip("/")
    fpath = (fp.path or "").lower()
    if moved and any(k in fpath for k in ("/login", "/signin", "/sign_in",
                                          "/account/login", "/auth/")):
        return f"被轉址到登入頁 {fpath}"
    # 沒轉址但頁面本身是驗證牆（Cloudflare / 「請稍候」型）
    head = html[:4000].lower()
    for kw, why in (("just a moment", "Cloudflare 驗證牆"),
                    ("please wait for verification", "驗證等待頁"),
                    ("checking your browser", "瀏覽器檢查頁"),
                    ("enable javascript and cookies to continue", "需要 JS/Cookie 的攔截頁")):
        if kw in head:
            return why
    return ""


def _err(url: str, msg: str, *, tier: str) -> CrawlResult:
    return CrawlResult(
        ok=False, tier=tier, url=url, final_url=url, status_code=0,
        markdown="", html="", title="", error=msg, duration_ms=0, extra={},
    )
