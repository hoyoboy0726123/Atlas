"""Computer Use Phase 1 — 動作執行後的 VLM 把關驗證

跟 visual_validator.py 不同(後者是 explicit「視覺驗證節點」步驟、單張圖):
這個是「每個 cu_action 跑完後自動驗」、看前後兩張截圖、判斷動作有沒生效。

主要 API: verify_action_outcome(before_path, after_path, expected, logger) → verdict dict

verdict 結構:
{
  "ok": bool,           # 動作後是否符合 expected
  "reason": str,        # 1-2 句說明
  "unexpected": str,    # 偵測到的非預期元素(popup / 錯誤對話框)、空字串 = 無
  "mismatch_type": str, # missing_element / no_effect / unexpected_popup / ui_changed / "" = ok
  "confidence": float,  # 0-1、VLM 自評信心(可選)
}

設計原則:
- 不算座標、不挑元素;純粹「看圖 + 對描述」回 ok/fail
- 用 settings.model 主模型(複用既有 LLM 設定);後續若 cu_vlm_provider 指定再走別的 provider
- 失敗模式分 4 種、push TG 時方便用戶辨識怎麼處理

"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# 預設驗證圖最大邊長:1280px;超過會被 PIL resize、避免 token 爆
# (1920x1080 全螢幕 → 1280x720、約 100KB PNG → ~150K tok 上下、Sonnet/GPT-4o 都吃得下)
MAX_IMAGE_DIM = 1280


def _extract_text_from_llm_result(result) -> str:
    """跟 visual_validator._extract_text_from_llm_result 同邏輯、抽 LLM 回應文字。"""
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


def _read_image_b64(path: str, max_dim: int = MAX_IMAGE_DIM) -> Optional[tuple[str, str]]:
    """讀檔 + 必要時 resize、回 (mime, base64)。讀失敗 / 過大跳過回 None。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    data = p.read_bytes()
    # 先看是否需要 resize:超過 max_dim 用 PIL 縮、省 token
    if len(data) > 200 * 1024:  # >200KB 才考慮 resize、小檔不動
        try:
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(data))
            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                buf = _io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                data = buf.getvalue()
        except Exception:
            pass  # PIL 失敗就用原圖
    if len(data) > 20 * 1024 * 1024:
        return None  # 仍 >20MB 跳過(LLM 不收)
    mime = "image/png"
    return (mime, base64.b64encode(data).decode())


_SYSTEM_PROMPT = (
    "你是 GUI 自動化驗證器。看 2 張螢幕截圖(動作前 / 動作後)、"
    "判斷使用者描述的『預期狀態』是否成立。\n"
    "嚴格回 JSON、不要 markdown、不要其他文字:\n"
    "{\n"
    '  "ok": true 或 false,\n'
    '  "reason": "1-2 句中文、說明判斷依據",\n'
    '  "unexpected": "若偵測到非預期元素(popup / 錯誤對話框 / 跳出視窗)寫描述、'
    '無就空字串",\n'
    '  "mismatch_type": "missing_element / no_effect / unexpected_popup / ui_changed / 空字串(若 ok=true)",\n'
    '  "confidence": 0.0 到 1.0 之間\n'
    "}\n\n"
    "判斷標準:\n"
    "- ok=true: 動作後畫面**確實**符合預期描述、且沒有非預期元素\n"
    "- ok=false: 4 種 mismatch 之一:\n"
    "  * missing_element: 預期出現的元素不在(例『檔案選單已開啟』但選單沒開)\n"
    "  * no_effect: 動作前後幾乎沒變(例 click 沒生效、視窗沒反應)\n"
    "  * unexpected_popup: 出現了非預期的對話框 / 通知 / 廣告蓋住目標\n"
    "  * ui_changed: 整個 UI 變了(例視窗被關、切到別的 app)\n"
)


def _build_user_message(expected: str, n_images: int) -> str:
    return (
        f"預期狀態:{expected}\n\n"
        f"上方共 {n_images} 張截圖、第 1 張是『動作前』、第 2 張是『動作後』。\n"
        f"判斷『動作後』是否符合預期狀態。回 JSON。"
    )


async def verify_action_outcome(
    *,
    before_path: str,
    after_path: str,
    expected: str,
    logger: logging.Logger,
    timeout_sec: float = 30.0,
    llm_role: str = "primary",
) -> dict:
    """送前後截圖 + expected 給 VLM、回 verdict dict。

    失敗模式:
    - VLM 認證 / 限流 / 模型不支援 vision → ok=False、mismatch_type=verifier_error、reason 含建議
    - 圖讀不到 → ok=False、reason 寫明
    - LLM 回非 JSON → ok=False、reason 寫明
    """
    if not expected or not expected.strip():
        return {
            "ok": True, "reason": "(expected 為空、跳過驗證)", "unexpected": "",
            "mismatch_type": "", "confidence": 0.0,
        }

    # 1. 讀前後圖 → base64 blocks
    before = _read_image_b64(before_path)
    after = _read_image_b64(after_path)
    if before is None or after is None:
        return {
            "ok": False,
            "reason": f"截圖讀取失敗(before={before is not None}, after={after is not None})",
            "unexpected": "", "mismatch_type": "verifier_error", "confidence": 0.0,
        }

    image_blocks = [
        {"type": "image_url", "image_url": {"url": f"data:{before[0]};base64,{before[1]}"}},
        {"type": "image_url", "image_url": {"url": f"data:{after[0]};base64,{after[1]}"}},
    ]

    # 2. 呼叫 VLM (用 settings.model 主模型;後續若要支援 cu_vlm_provider override 再擴)
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
            logger.info(f"[cu_vlm] {_eff_role} 模型是訂閱 CLI(無法輸入圖片)→ 視覺驗證自動改用 {_other} 模型")
            _eff_role = _other
        else:
            return {
                "ok": False,
                "reason": "主/副模型都是訂閱 CLI(純文字、無法輸入圖片),視覺驗證無法執行;"
                          "請把主或副模型其中一個設為支援 vision 的 API 模型(如 Gemini)",
                "unexpected": "", "mismatch_type": "verifier_error", "confidence": 0.0,
            }

    user_text = _build_user_message(expected, n_images=2)
    user_content = [{"type": "text", "text": user_text}, *image_blocks]

    try:
        llm = build_llm(temperature=0, role=_eff_role)
        # run_in_executor 避免阻塞 event loop;timeout 防 LLM hang 影響整套 pipeline
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: llm.invoke([
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=user_content),
                ])
            ),
            timeout=timeout_sec,
        )
        raw = _extract_text_from_llm_result(result).strip()
    except asyncio.TimeoutError:
        return {
            "ok": False, "reason": f"VLM 驗證 timeout ({timeout_sec}s)、把這步當失敗處理",
            "unexpected": "", "mismatch_type": "verifier_error", "confidence": 0.0,
        }
    except Exception as e:
        err_str = f"{type(e).__name__}: {e}"
        err_low = err_str.lower()
        try:
            from settings import get_settings
            cur_provider = get_settings().get("provider", "?")
            cur_model = get_settings().get("model", "?")
        except Exception:
            cur_provider, cur_model = "?", "?"

        if any(k in err_low for k in ("api_key", "401", "unauthorized", "authentication")):
            msg = f"VLM 認證錯誤(provider={cur_provider})、檢查設定頁 API Key"
        elif any(k in err_low for k in ("image", "vision", "multimodal", "not supported", "unsupported")):
            msg = (f"當前模型「{cur_model}」不支援 vision。"
                   "請切到 Sonnet 4.6 / GPT-4o / Gemini 2.5 Pro 等視覺模型")
        elif any(k in err_low for k in ("rate limit", "429", "quota")):
            msg = f"VLM provider 限流({cur_provider})、之後重試或切 provider"
        elif any(k in err_low for k in ("connection", "timeout", "network", "dns")):
            msg = f"VLM provider 連線失敗({cur_provider})、檢查網路"
        else:
            msg = f"VLM 呼叫失敗:{err_str[:200]}"
        return {
            "ok": False, "reason": msg, "unexpected": "",
            "mismatch_type": "verifier_error", "confidence": 0.0,
        }

    if not raw:
        return {
            "ok": False, "reason": "VLM 回應為空",
            "unexpected": "", "mismatch_type": "verifier_error", "confidence": 0.0,
        }

    # 抓 markdown ```json ... ``` 內容(模型有時會包)
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "ok": False, "reason": f"VLM 回非 JSON:{raw[:200]}",
            "unexpected": "", "mismatch_type": "verifier_error", "confidence": 0.0,
        }

    # 標準化 verdict
    verdict = {
        "ok": bool(data.get("ok", False)),
        "reason": str(data.get("reason") or "").strip()[:500] or "(無說明)",
        "unexpected": str(data.get("unexpected") or "").strip()[:300],
        "mismatch_type": str(data.get("mismatch_type") or "").strip(),
        "confidence": float(data.get("confidence") or 0.0),
    }
    # 若 ok=true 但 mismatch_type 有值、清空(不一致時以 ok 為準)
    if verdict["ok"] and verdict["mismatch_type"]:
        verdict["mismatch_type"] = ""
    # 若 ok=false 但沒填 mismatch_type、給 unspecified
    if not verdict["ok"] and not verdict["mismatch_type"]:
        verdict["mismatch_type"] = "unspecified"

    logger.info(
        f"[cu_vlm_verifier] ok={verdict['ok']} type={verdict['mismatch_type']!r} "
        f"conf={verdict['confidence']:.2f} reason={verdict['reason'][:120]}"
    )
    return verdict
