"""分享去敏 — 匯出工作流包給別人(市集/同事)前,掃描並替換個人/敏感資訊。

處理對象:workflow yaml、canvas、每個 recipe 的 code / output_path / input_fingerprints。
規則(保守優先:寧可多遮、不可漏金鑰):
  - 使用者家目錄路徑     C:\\Users\\<名> / /home/<名> / /mnt/c/Users/<名> → <USER>
  - Email               → <EMAIL>
  - 常見金鑰樣式         sk- / ghp_ / gho_ / AIza / xoxb- / JWT(eyJ…)→ <REDACTED_KEY>
  - 私網 IP             192.168.* / 10.* / 172.16-31.* → <PRIVATE_IP>
  - Telegram bot token  數字:35字 → <REDACTED_KEY>
每次替換記入 findings(類型/遮蔽後樣本/出現處),打包進 zip 的 SHARE_REPORT.json,
分享者可自查「到底被改了什麼」。{{ secrets.X }} 引用本來就不含值,原樣保留(這正是它的用途)。
"""
from __future__ import annotations

import re
from typing import Tuple

_RULES: list[tuple[str, re.Pattern, str]] = [
    ("win_user_path",  re.compile(r"[A-Za-z]:[\\/](?:Users|users)[\\/][^\\/\s\"';,)]+"), r"C:/Users/<USER>"),
    ("wsl_user_path",  re.compile(r"/mnt/[a-z]/Users/[^/\s\"';,)]+", re.I), "/mnt/c/Users/<USER>"),
    ("home_path",      re.compile(r"/home/[^/\s\"';,)]+"), "/home/<USER>"),
    ("email",          re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    ("openai_key",     re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "<REDACTED_KEY>"),
    ("github_token",   re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<REDACTED_KEY>"),
    ("google_key",     re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "<REDACTED_KEY>"),
    ("slack_token",    re.compile(r"\bxox[abponrs]-[A-Za-z0-9-]{10,}\b"), "<REDACTED_KEY>"),
    ("tg_bot_token",   re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "<REDACTED_KEY>"),
    ("jwt",            re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "<REDACTED_KEY>"),
    # AWS access key id 有固定 AKIA/ASIA 前綴、可安全抓;secret 靠 aws_secret 上下文抓(避免誤殺一般 40 字 base64)
    ("aws_access_key",  re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<REDACTED_KEY>"),
    ("aws_secret_key",  re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)['\"]?[A-Za-z0-9/+]{40}['\"]?"), r"\1<REDACTED_KEY>"),
    # 連線字串密碼:URL 內嵌(scheme://user:PASS@host)+ key=value 形式(Password=/pwd=)
    ("conn_url_pw",     re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+):[^@/\s]+@"), r"\1:<REDACTED_KEY>@"),
    ("conn_kv_pw",      re.compile(r"(?i)\b(password|passwd|pwd)(\s*[=:]\s*)['\"]?[^\s'\";,)]+"), r"\1\2<REDACTED_KEY>"),
    # PEM / OpenSSH 私鑰整塊(含中間換行)
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<REDACTED_KEY>"),
    ("private_ip",     re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "<PRIVATE_IP>"),
]


def scrub_text(text: str, where: str, findings: list) -> str:
    """對單段文字套所有規則;每個命中記一筆 finding(遮蔽後樣本、不外洩原值)。"""
    if not text:
        return text
    out = text
    for kind, pat, repl in _RULES:
        hits = pat.findall(out)
        if hits:
            for h in set(hits if isinstance(hits[0], str) else ["(match)"]):
                sample = (h[:4] + "…" + h[-3:]) if isinstance(h, str) and len(h) > 10 else "(short)"
                findings.append({"where": where, "type": kind, "sample": sample,
                                 "count": hits.count(h) if isinstance(h, str) else len(hits)})
            out = pat.sub(repl, out)
    return out


def scrub_workflow_export(wf_export: dict, recipes: list[dict]) -> Tuple[dict, list[dict], list]:
    """回傳 (去敏後 wf_export, 去敏後 recipes, findings)。canvas 以 JSON 字串層面掃(涵蓋所有節點欄位)。"""
    import json as _j
    findings: list = []
    wf2 = dict(wf_export)
    wf2["yaml"] = scrub_text(wf_export.get("yaml") or "", "workflow.yaml", findings)
    # name 欄位也要掃 — 使用者可能把 email / 路徑 / 金鑰放進工作流名稱(實測漏網)
    if wf_export.get("name"):
        wf2["name"] = scrub_text(wf_export["name"], "workflow.name", findings)
    canvas = wf_export.get("canvas")
    if canvas:
        canvas_s = canvas if isinstance(canvas, str) else _j.dumps(canvas, ensure_ascii=False)
        canvas_s2 = scrub_text(canvas_s, "workflow.canvas", findings)
        try:
            wf2["canvas"] = _j.loads(canvas_s2) if not isinstance(canvas, str) else canvas_s2
        except Exception:
            wf2["canvas"] = canvas  # 替換弄壞 JSON(理論上不會)→ 保原樣,寧可不遮不要壞檔
    rec2 = []
    for r in recipes:
        rr = dict(r)
        w = f"recipe:{r.get('step_name')}"
        rr["code"] = scrub_text(r.get("code") or "", w + ".code", findings)
        rr["output_path"] = scrub_text(r.get("output_path") or "", w + ".output_path", findings) or None
        fp = r.get("input_fingerprints")
        if fp:
            fp_s = fp if isinstance(fp, str) else _j.dumps(fp, ensure_ascii=False)
            fp_s2 = scrub_text(fp_s, w + ".fingerprints", findings)
            try:
                rr["input_fingerprints"] = _j.loads(fp_s2) if not isinstance(fp, str) else fp_s2
            except Exception:
                rr["input_fingerprints"] = fp
        rec2.append(rr)
    return wf2, rec2, findings
