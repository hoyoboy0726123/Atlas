# -*- coding: utf-8 -*-
"""skill_llm — 在 skill / subagent 的 run_python 內呼叫「系統當前設定的那顆 LLM」。

設計理念(對應「skill runtime 不知道自己就是 LLM」這個坑):
  任務若要求「用 LLM 逐筆/逐段做同一種轉換」(校對、翻譯、分類…),正確做法優先是
  agent **自己**逐段做(read_file → 產出 → write_file)。但若真的有幾十~幾百筆、
  適合在程式裡跑迴圈,就用這個 helper —— 它走「系統現在設定的 provider/model」、
  用系統已載入的金鑰,**完全不需要也不應該 `import openai`**。

用法(在 run_python 內):
    from skill_llm import llm
    out = llm("把這段校對成通順中文,只回正文:\\n" + text)
    out = llm("翻成英文", system="你是專業字幕翻譯")

純標準函式庫(urllib + json),不依賴容器有裝任何套件。執行環境由 Atlas 後端在
run_python 時注入這些環境變數:SKILL_LLM_PROVIDER / SKILL_LLM_MODEL /
SKILL_LLM_KEY / SKILL_LLM_BASE_URL。
"""
import os
import json
import urllib.request


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def llm(prompt: str, system: str = None, temperature: float = 0.0,
        max_tokens: int = 4096, timeout: int = 120) -> str:
    """呼叫系統當前設定的 LLM,回傳純文字(已 strip)。

    沙盒 / host 兩種 run_python 模式都可用。失敗會 raise,讓 run_python rc!=0、
    錯誤訊息回到 agent,而不是悄悄回空字串。
    """
    provider = (os.environ.get("SKILL_LLM_PROVIDER") or "").strip().lower()
    model = (os.environ.get("SKILL_LLM_MODEL") or "").strip()
    key = os.environ.get("SKILL_LLM_KEY") or ""
    base = (os.environ.get("SKILL_LLM_BASE_URL") or "").strip()
    if not provider or not model:
        raise RuntimeError(
            "skill_llm 未設定:系統沒注入 SKILL_LLM_* 環境變數。"
            "可能原因:SKILL_LLM_HELPER 被關閉、當前無有效的 provider/model 設定、"
            "或主模型是訂閱 CLI 大腦(它的執行檔在 host、沙盒內用不了)且沒設可用的副模型。"
            "→ 改成由你自己逐段處理(read_file → 產出 → write_file);"
            "若要在技能內呼叫 LLM,請在設定頁把副模型設成一般 API 模型(如 Gemini)。"
        )

    if provider in ("gemini", "google", "gemma"):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        resp = _post_json(url, payload, {}, timeout)
        cands = resp.get("candidates") or []
        if not cands:
            raise RuntimeError(f"skill_llm gemini 空回應:{json.dumps(resp)[:300]}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise RuntimeError(f"skill_llm gemini 回應無文字內容:{json.dumps(resp)[:300]}")
        return text

    if provider in ("groq", "openai"):
        default_base = "https://api.groq.com/openai/v1" if provider == "groq" else "https://api.openai.com/v1"
        url = (base.rstrip("/") if base else default_base) + "/chat/completions"
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        resp = _post_json(url, payload, {"Authorization": f"Bearer {key}"}, timeout)
        return (resp["choices"][0]["message"]["content"] or "").strip()

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        resp = _post_json(url, payload,
                          {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        blocks = resp.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    if provider == "ollama":
        url = (base.rstrip("/") if base else "http://localhost:11434") + "/api/chat"
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        resp = _post_json(url, {"model": model, "messages": messages, "stream": False}, {}, timeout)
        return ((resp.get("message") or {}).get("content") or "").strip()

    raise RuntimeError(f"skill_llm 不支援 provider={provider!r}")
