"""輸出 JSON Schema 合約閘門 — 確定性驗證(0 token)。

弱模型解析步最常見的翻車:產出 frontmatter 垃圾 / 缺欄位 / 型別錯(統計 0/0/0)卻報成功。
LLM 驗證(expect)是語意層、會漏接結構問題;這裡用 JSON Schema 做「硬合約」:
  - 不過 → fail + 具體錯誤(哪個欄位、什麼問題)→ 自癒 LLM 讀得懂、知道怎麼修。
  - 通過 → 再走(可選的)LLM 語意驗證。
"""
from __future__ import annotations

import json
import os
from typing import Tuple


def validate_output_schema(path: str, schema: dict) -> Tuple[bool, str]:
    """驗證輸出檔內容是否符合 JSON Schema。

    回傳 (ok, error_text)。error_text 為中文、含具體欄位路徑,給自癒/使用者讀。
    非 .json 檔 / schema 空 → (True, "")(不啟用,不擋)。
    """
    if not schema or not isinstance(schema, dict):
        return True, ""
    if not path or not str(path).lower().endswith(".json"):
        return True, ""
    if not os.path.exists(path):
        return False, f"輸出檔不存在:{path}"
    try:
        with open(path, encoding="utf-8-sig") as f:  # 容 BOM(Windows 常見)
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, (f"輸出檔不是合法 JSON(schema 合約要求 JSON):"
                       f"line {e.lineno} col {e.colno} — {e.msg}。"
                       f"常見原因:混入 markdown/frontmatter/說明文字,請只輸出純 JSON。")
    except Exception as e:
        return False, f"讀取輸出檔失敗:{e}"

    try:
        import jsonschema
    except ImportError:
        # ⚠ 不能靜默放行。2026-08-06 實測：jsonschema 從來沒被列進 requirements.txt，
        #   Atlas 是靠 mcp 套件把它當相依順便裝進來才「剛好能用」——
        #   相依樹一動就會全部失效，而且失效時還會印「✅ schema 合約通過」。
        #   宣告了驗證卻靜默跳過比不驗更危險，改成明確失敗。
        return False, ("缺少 jsonschema 套件，無法驗證 output.json_schema 合約。"
                       "請執行：pip install jsonschema")

    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as e:
        # schema 本身寫錯 → 視為設定錯誤,擋下並講清楚(否則假通過更危險)
        return False, f"json_schema 本身不合法:{e.message}"

    errors = sorted(validator_cls(schema).iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return True, ""
    msgs = []
    for e in errors[:5]:
        loc = ".".join(str(p) for p in e.path) or "(根層)"
        msgs.append(f"欄位 `{loc}`:{e.message}")
    more = f"(還有 {len(errors) - 5} 個錯誤)" if len(errors) > 5 else ""
    return False, ("輸出 JSON 不符合 schema 合約:\n- " + "\n- ".join(msgs) + more
                   + "\n請修正輸出結構後重新產出(欄位名/型別必須完全符合)。")
