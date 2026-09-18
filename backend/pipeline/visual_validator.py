"""
視覺驗證節點：用 Settings 主模型（必須支援視覺）判斷某個圖像是否符合預期。

2 種來源（vv_source）：
  prev_output    → 上一步 output.path 檔案
                   - 圖檔（png/jpg/...）→ 直送 VLM
                   - 非圖檔（xlsx/docx/pdf/...）→ 自動 render_file_preview 轉 PNG 再送
                     （xlsx 多 sheet → 每 sheet 一張，全部送）
  current_screen → 即時 mss 抓螢幕（典型用途：computer_use 動作做完後，VLM 判斷
                   畫面有沒有達到預期狀態，再決定要不要往下走）。可選 vv_search_region
                   把截圖裁成關鍵區域省 token。

回傳 (pass: bool, reason: str)。pass=False 步驟失敗、retry 邏輯沿用既有。
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from typing import Optional


IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
MAX_IMAGES = 6  # 多頁 xlsx / pptx 可能很多張，限制送的張數避免 token 爆掉


def _extract_text_from_llm_result(result) -> str:
    """LLM 回應的 content 在不同 provider 形態不同：
      - 純文字 LLM（Groq）：result.content 是 string
      - 多模態 LLM（Gemini, Claude）：result.content 是 list[dict]，每個 block 形如
        {"type": "text", "text": "..."} 或 {"type": "image_url", ...}
    把所有 text block 拼起來回 string。對齊 llm_factory.invoke_with_streaming 同樣處理。"""
    content = getattr(result, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or ""
                if t:
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _wsl_to_windows_path(path: str) -> str:
    """LLM / 上一節點若用沙盒輸出路徑（/mnt/c/...），轉回 Windows 路徑。"""
    import re as _re
    m = _re.match(r"^/mnt/([a-z])/(.*)$", str(path).strip())
    if not m:
        return path
    return f"{m.group(1).upper()}:\\{m.group(2).replace('/', chr(92))}"


def _resolve_image_for_file(file_path: str, out_dir: Optional[str] = None) -> list[str]:
    """檔案 → PNG 路徑清單。圖檔回自身路徑；其他類型走 render_file_preview。"""
    file_path = _wsl_to_windows_path(file_path)
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return []
    if p.suffix.lower() in IMAGE_EXTS:
        return [str(p)]
    try:
        from .file_preview import render_file_preview
    except Exception:
        from pipeline.file_preview import render_file_preview  # type: ignore
    return render_file_preview(file_path, out_dir=out_dir or str(p.parent))


def _capture_screen_region(region: Optional[tuple[int, int, int, int]],
                           out_path: Path) -> Optional[str]:
    """抓螢幕（可選裁切），存到 out_path。回傳 PNG 路徑或 None（失敗時）。
    跟 computer_use._capture_screen 相同：mss + cv2 BGR。"""
    try:
        import mss
        import cv2
        import numpy as np
        with mss.mss() as sct:
            mon = sct.monitors[0]
            img = np.array(sct.grab(mon))
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if region is not None:
            l, t, w, h = region
            x = max(0, l - mon["left"])
            y = max(0, t - mon["top"])
            x_end = min(bgr.shape[1], x + w)
            y_end = min(bgr.shape[0], y + h)
            if x_end <= x or y_end <= y:
                return None
            bgr = bgr[y:y_end, x:x_end]
        ok, buf = cv2.imencode(".png", bgr)
        if not ok:
            return None
        out_path.write_bytes(buf.tobytes())
        return str(out_path)
    except Exception:
        return None


def _read_image_b64(path: str) -> Optional[tuple[str, str]]:
    """檔案路徑 → (mime, base64)。失敗回 None。20 MB 以上跳過。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    data = p.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        return None
    ext_to_mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                   '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
    mime = ext_to_mime.get(p.suffix.lower(), 'image/png')
    return (mime, base64.b64encode(data).decode())


async def run_visual_validation(
    *,
    source: str,
    prompt: str,
    prev_output_file: Optional[str],
    out_dir: str,
    search_region: Optional[tuple[int, int, int, int]],
    logger: logging.Logger,
    llm_role: str = "primary",
) -> tuple[bool, str]:
    """主要入口。回 (pass, reason)。失敗訊息寫進 reason 給 retry 邏輯用。"""
    if not prompt or not prompt.strip():
        return (False, "vv_prompt 為空（必填，描述判斷條件）")

    # 1. 解析來源 → PNG 路徑清單
    images: list[str] = []
    # 相容舊值 prev_output_file / rendered_preview（早期版本）— 都當 prev_output 處理
    if source in ("prev_output", "prev_output_file", "rendered_preview"):
        if not prev_output_file:
            return (False, "找不到上一步輸出檔（沒有設 output.path 或檔案不存在）")
        # pptx/docx 必須走 LibreOffice 渲染真實版面、純文字 PNG 不算「視覺驗證」、直接拒絕
        _ext = prev_output_file.lower().rsplit(".", 1)[-1] if "." in prev_output_file else ""
        if _ext in ("pptx", "docx"):
            try:
                from .host_tools import get_libreoffice_status
            except ImportError:
                from pipeline.host_tools import get_libreoffice_status  # type: ignore
            _lo_ok, _ = get_libreoffice_status()
            if not _lo_ok:
                return (False, (
                    f"視覺驗證需 LibreOffice 才能渲染 {_ext.upper()} 真實版面、主機未裝。"
                    "請改用 vv_source=current_screen(目前螢幕)模式、或安裝 LibreOffice 後重啟 backend:"
                    "winget install -e --id TheDocumentFoundation.LibreOffice --source winget"
                ))
        images = _resolve_image_for_file(prev_output_file, out_dir=out_dir)
    elif source == "current_screen":
        out_path = Path(out_dir) / "_vv_screen.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shot = _capture_screen_region(search_region, out_path)
        if shot:
            images = [shot]
    else:
        return (False, f"未知 vv_source：{source}（允許 prev_output / current_screen）")

    if not images:
        return (False, f"vv_source={source} 沒拿到任何圖片可送 VLM")

    images = images[:MAX_IMAGES]  # 限張數防 token 爆
    logger.info(f"[visual_validation] source={source} images={len(images)} prompt={prompt[:80]}")

    # 2. 讀檔 → base64
    image_blocks: list[dict] = []
    for path in images:
        loaded = _read_image_b64(path)
        if loaded is None:
            logger.warning(f"[visual_validation] 跳過讀不到 / 過大的檔案：{path}")
            continue
        mime, b64 = loaded
        image_blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if not image_blocks:
        return (False, "所有候選圖片都讀不到或過大，無法送 VLM")

    # 3. 呼叫 VLM
    backend_dir = str(Path(__file__).resolve().parent.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from llm_factory import build_llm, provider_supports_vision
    from langchain_core.messages import HumanMessage, SystemMessage

    # 訂閱 CLI 是純文字橋、圖片會被丟掉 → 模型會「沒看圖就下判決」(靜默錯誤)。
    # 自動改用另一個 role;兩邊都是 CLI 才明確報錯、絕不默默硬跑。
    _eff_role = llm_role
    if not provider_supports_vision(_eff_role):
        _other = "secondary" if _eff_role == "primary" else "primary"
        if provider_supports_vision(_other):
            logger.info(f"[visual_validation] {_eff_role} 模型是訂閱 CLI(無法輸入圖片)→ 自動改用 {_other} 模型")
            _eff_role = _other
        else:
            return (False, "主/副模型都是訂閱 CLI(純文字、無法輸入圖片),視覺驗證無法執行;"
                           "請把主或副模型其中一個設為支援 vision 的 API 模型(如 Gemini)")

    sys_msg = ('你是視覺驗證器。看到的圖是「待驗證的輸出」。嚴格依使用者描述的條件回 JSON：'
               '{"pass": true/false, "reason": "簡短中文說明"}。只回 JSON，不要 markdown、不要其他文字。')
    user_text = (f"驗證條件：{prompt}\n"
                 f"請看上方共 {len(image_blocks)} 張圖（多 sheet 的 xlsx / pptx 會分多張），"
                 "判斷整體是否符合條件。回 JSON。")
    user_content = [{"type": "text", "text": user_text}, *image_blocks]

    try:
        llm = build_llm(temperature=0, role=_eff_role)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: llm.invoke([SystemMessage(content=sys_msg), HumanMessage(content=user_content)])
        )
        # 多模態模型（Gemini）回傳 content 是 list[dict]，純文字模型是 str — 統一抽出 text
        raw = _extract_text_from_llm_result(result).strip()
    except Exception as e:
        # 把常見錯誤分類給使用者具體引導訊息，比丟堆疊好讀
        err_str = f"{type(e).__name__}: {e}"
        err_low = err_str.lower()
        try:
            from settings import get_settings
            cur_provider = get_settings().get("provider", "?")
            cur_model = get_settings().get("model", "?")
        except Exception:
            cur_provider, cur_model = "?", "?"

        # 1. API Key / 認證
        if any(k in err_low for k in ("api_key", "api key", "401", "unauthorized",
                                       "authenticate", "authentication", "invalid token",
                                       "access denied", "missing credential")):
            msg = (f"❌ 視覺驗證失敗：LLM 服務認證錯誤（provider={cur_provider}）。"
                   f"請到「設定」頁確認 API Key 已填入且未過期 → 重試。")
        # 2. 模型不支援 vision
        elif any(k in err_low for k in ("image", "vision", "multimodal", "not supported",
                                         "unsupported", "image_url", "modality")):
            msg = (f"❌ 視覺驗證失敗：當前模型「{cur_model}」可能不支援視覺輸入。"
                   f"請到「設定」頁切到有 vision 能力的模型再試，例如：\n"
                   f"• Groq：`meta-llama/llama-4-scout-17b-16e-instruct` / `llama-3.2-90b-vision-preview`\n"
                   f"• Gemini：`gemini-2.5-pro` / `gemini-2.5-flash`\n"
                   f"• OpenAI：`gpt-4o` / `gpt-4o-mini`\n"
                   f"• Anthropic：`claude-sonnet-4-6` / `claude-opus-4-7`")
        # 3. 網路 / 連線
        elif any(k in err_low for k in ("connection", "timeout", "timed out", "network",
                                         "dns", "resolve")):
            msg = (f"❌ 視覺驗證失敗：連不到 LLM provider（{cur_provider}）。"
                   f"請檢查網路 / VPN，Ollama 用戶請確認 ollama serve 在跑。")
        # 4. Rate limit
        elif any(k in err_low for k in ("rate limit", "429", "quota", "too many")):
            msg = (f"❌ 視覺驗證失敗：LLM provider 限流。請等幾分鐘再試、"
                   f"或到設定頁切換另一個 provider。")
        # 5. fallback
        else:
            msg = (f"❌ 視覺驗證失敗：{err_str[:300]}\n"
                   f"（請確認設定頁的 provider={cur_provider} / model={cur_model} 可正常使用）")
        return (False, msg)

    if not raw:
        return (False, "VLM 回應為空")
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (False, f"VLM 回應不是 JSON：{raw[:200]}")
    passed = bool(data.get("pass", False))
    reason = str(data.get("reason") or "").strip() or "(無原因說明)"
    logger.info(f"[visual_validation] pass={passed} reason={reason[:120]}")
    return (passed, reason)
