"""RapidOCR 引擎包裝層 — Atlas OCR 引擎可選版本。

RapidOCR 是 PaddleOCR 訓練好的模型轉成 ONNX 版,只依賴 onnxruntime
(~50MB),沒有 PaddlePaddle 750MB 框架的肥胖、跟 paddle 3.3 PIR/oneDNN
bug 那些。Python 3.13 直接可用,不需要遷移 backend venv。

對中文整詞識別準確率跟 paddle 同級(同一套訓練好的模型),適合 Mica
半透明背景、小字、跨機台場景 — 對 Windows.Media.Ocr 在 < 150% scaling
下複雜中文字漏字的問題是直接解。

切換:
    export OCR_ENGINE=rapid    (linux/mac)
    $env:OCR_ENGINE = "rapid"  (powershell)
    SET OCR_ENGINE=rapid       (cmd)

安裝:
    pip install rapidocr onnxruntime
    # 可選:簡繁歸一化(RapidOCR 模型是簡體訓練、target 通常是繁體)
    pip install opencc-python-reimplemented

設計目標:
- 整個檔案 optional — rapidocr 沒裝就 graceful fail、不影響 Atlas 主流程
- Engine 全域 cache(每次新建模型載入要 5-10s)
- 回傳格式跟 Windows OCR _recognize() 同 schema: [{text, x, y, w, h, line_text, line_index, confidence}]
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


# 全域 cache:RapidOCR 第一次建要載模型 ~5-10s,不能每次呼叫都重建
_RAPID_OCR_INSTANCE = None
_RAPID_OCR_LOCK = threading.Lock()


def is_available() -> bool:
    """檢測 rapidocr 是不是裝得起來(不真的 init 引擎、那很慢)。"""
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


def _get_rapid_engine():
    """Lazy init 全域 RapidOCR instance。Thread-safe。"""
    global _RAPID_OCR_INSTANCE
    if _RAPID_OCR_INSTANCE is not None:
        return _RAPID_OCR_INSTANCE
    with _RAPID_OCR_LOCK:
        if _RAPID_OCR_INSTANCE is not None:
            return _RAPID_OCR_INSTANCE
        log.info("[ocr_rapid] 首次初始化 RapidOCR、載入 ONNX 模型 (~5-10s)...")
        from rapidocr import RapidOCR
        # ⚠ 一定要指定 PP-OCRv5:RapidOCR() 的預設是 ch/PP-OCRv4,而 v4 的字典
        #   (ppocr_keys_v1.txt)裡**根本沒有**「額 稅 憑 簽」這些繁體字 —— 不是辨識錯,
        #   是模型物理上輸出不了,會退化成「金额 / 營業税」或整個讀錯。
        #   實測同一張繁中憑證:繁體標籤 v4 只對 5/12、v5 對 12/12,且 v5 還快 40%。
        #   server 版沒更準(同樣 12/12)但慢 66%、載入 36s → 用 mobile。
        #   chinese_cht 繁中專用版標籤也 12/12、但數字掉一個 → v5 通用版更好。
        _params = None
        try:
            from rapidocr import LangRec, ModelType, OCRVersion
            _params = {
                "Rec.lang_type": LangRec.CH,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
            }
        except Exception as e:  # 舊版 rapidocr 沒這些列舉
            log.warning(f"[ocr_rapid] 取不到 v5 列舉({e})、退回套件預設模型")
        try:
            _RAPID_OCR_INSTANCE = RapidOCR(params=_params) if _params else RapidOCR()
        except Exception as e:
            # 模型下載失敗 / 參數不被接受 → 退回預設,寧可辨識差也別讓 OCR 整個掛掉
            log.warning(f"[ocr_rapid] 以 PP-OCRv5 初始化失敗({e})、退回套件預設模型")
            _RAPID_OCR_INSTANCE = RapidOCR()
        log.info("[ocr_rapid] RapidOCR ready")
    return _RAPID_OCR_INSTANCE


def recognize_rapid(img_bgr: np.ndarray, lang_tag: Optional[str] = None) -> list[dict]:
    """跑 RapidOCR、回傳跟 Windows OCR _recognize() 同樣的 list[dict] schema。

    lang_tag 參數忽略 —— 但**不是因為模型不挑語言**。實測 PP-OCRv4 中文模型的字典
    缺「額稅憑簽」等繁體字、輸出不了;繁中要靠 _get_rapid_engine() 指定 PP-OCRv5 解決。
    RapidOCR 每個 detected region 直接是整詞、不像 Win OCR 把 CJK 拆單字,
    所以 line_text 跟 text 一樣,line_index 用 detection 順序當 index。
    """
    engine = _get_rapid_engine()

    # 防呆檢查:確保 image 是 uint8 BGR 3-channel ndarray
    try:
        h_img, w_img = img_bgr.shape[:2]
        channels = img_bgr.shape[2] if len(img_bgr.shape) >= 3 else 1
        log.info(f"[ocr_rapid] 輸入影像: {w_img}x{h_img} channels={channels} dtype={img_bgr.dtype}")
    except Exception as e:
        log.warning(f"[ocr_rapid] 無法讀 image shape: {e}")

    # 新版 RapidOCR 3.x 偏好 .predict(),舊版用 __call__
    if hasattr(engine, "predict"):
        result = engine.predict(img_bgr)
        log.info(f"[ocr_rapid] 用 engine.predict()、result type={type(result).__name__}")
    else:
        result = engine(img_bgr)
        log.info(f"[ocr_rapid] 用 engine(__call__)、result type={type(result).__name__}")

    items: list[dict] = []

    # RapidOCR 新版 (>= 3.x):RapidOCROutput 物件,有 .boxes / .txts / .scores
    # RapidOCR 舊版 (rapidocr-onnxruntime ~1.x):回 (list[(box, text, score)], info_dict)
    try:
        parsed_via = "none"
        if hasattr(result, "txts"):
            # 新版 RapidOCROutput — 注意 boxes/scores 可能是 numpy ndarray,
            # 不能用 `or []` (ndarray 拒絕 boolean evaluation),用 None 比對處理
            parsed_via = "new-RapidOCROutput"
            texts = getattr(result, "txts", None)
            scores = getattr(result, "scores", None)
            boxes = getattr(result, "boxes", None)
            if texts is None: texts = []
            if scores is None: scores = []
            if boxes is None: boxes = []
            log.info(f"[ocr_rapid] new-format: {len(texts)} texts, {len(scores)} scores, {len(boxes)} boxes")
            for i, text in enumerate(texts):
                if not text:
                    continue
                conf = float(scores[i]) if i < len(scores) else 0.0
                if i < len(boxes):
                    box = boxes[i]
                    xs = [float(p[0]) for p in box]
                    ys = [float(p[1]) for p in box]
                    x, y = int(min(xs)), int(min(ys))
                    w, h = int(max(xs) - x), int(max(ys) - y)
                else:
                    x = y = w = h = 0
                items.append({
                    "text": text,
                    "x": x, "y": y, "w": w, "h": h,
                    "line_text": text,
                    "line_index": i,
                    "confidence": conf,
                })
        elif isinstance(result, tuple) and len(result) >= 1 and result[0]:
            # 舊版 (rapidocr-onnxruntime): (list, time_info)
            parsed_via = "old-tuple"
            log.info(f"[ocr_rapid] old-format: result[0] has {len(result[0])} lines")
            for i, line in enumerate(result[0]):
                if not line or len(line) < 3:
                    continue
                box, text, conf = line[0], line[1], line[2]
                if not text:
                    continue
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                x, y = int(min(xs)), int(min(ys))
                w, h = int(max(xs) - x), int(max(ys) - y)
                items.append({
                    "text": text,
                    "x": x, "y": y, "w": w, "h": h,
                    "line_text": text,
                    "line_index": i,
                    "confidence": float(conf),
                })
        elif isinstance(result, list):
            # 最新版 paddleocr/rapidocr 可能回 list[dict],dict 含 rec_texts/rec_scores/rec_polys
            parsed_via = "list-of-dicts"
            log.info(f"[ocr_rapid] list-format: len={len(result)}")
            for first in result:
                if isinstance(first, dict) and "rec_texts" in first:
                    # 同樣不能 `or []`,ndarray 拒絕 boolean
                    texts = first.get("rec_texts")
                    scores = first.get("rec_scores")
                    polys = first.get("rec_polys")
                    if polys is None: polys = first.get("dt_polys")
                    if texts is None: texts = []
                    if scores is None: scores = []
                    if polys is None: polys = []
                    log.info(f"[ocr_rapid] list-item dict: {len(texts)} texts")
                    for i, text in enumerate(texts):
                        if not text:
                            continue
                        conf = float(scores[i]) if i < len(scores) else 0.0
                        if i < len(polys):
                            poly = polys[i]
                            xs = [float(p[0]) for p in poly]
                            ys = [float(p[1]) for p in poly]
                            x, y = int(min(xs)), int(min(ys))
                            w, h = int(max(xs) - x), int(max(ys) - y)
                        else:
                            x = y = w = h = 0
                        items.append({
                            "text": text,
                            "x": x, "y": y, "w": w, "h": h,
                            "line_text": text,
                            "line_index": i,
                            "confidence": conf,
                        })
        log.info(f"[ocr_rapid] 解析路徑={parsed_via}、回傳 {len(items)} 個 word")
        if not items:
            log.warning(f"[ocr_rapid] 解出 0 個 item。原始 result repr(前 800): {repr(result)[:800]}")
    except Exception as e:
        log.warning(f"[ocr_rapid] 解析 result 結構失敗:{type(e).__name__}: {e}")
        log.warning(f"[ocr_rapid] 原始 result type={type(result)} repr 前 800 字:{repr(result)[:800]}")

    return items
