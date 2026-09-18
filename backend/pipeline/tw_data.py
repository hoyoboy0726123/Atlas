"""台灣資料驗證與清洗。

為什麼獨立成一支:這些規則跟「格式長得像」不同 —— 它們能**證偽**。
OCR 把 8 讀成 3、把 0 讀成 O,格式檢查一律放行,檢查碼卻會當場算不過。
把它接進抽取流程,等於免費得到一層 OCR 錯字偵測。

對外:
  validate_tax_id(s)     統一編號(含 2023 新制)
  roc_to_ad(s) / ad_to_roc(d)   民國年 ↔ 西元
  normalize_fullwidth(s) 全形 → 半形
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

# 統編權重。第 7 碼(index 6)為 7 時有特例,見 validate_tax_id
_TAX_ID_WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)
_TAX_ID_RE = re.compile(r"^\d{8}$")


def _tax_id_sum(digits: str) -> int:
    """逐位乘權重,乘積為兩位數時**十位與個位要拆開相加**。"""
    total = 0
    for i, c in enumerate(digits):
        p = int(c) * _TAX_ID_WEIGHTS[i]
        total += p // 10 + p % 10
    return total


def validate_tax_id(value: Optional[str]) -> tuple[bool, str]:
    """驗證統一編號。回 (是否有效, 說明)。

    ⚠️ 2023-04 財政部改了檢查邏輯:**由「可被 10 整除」改為「可被 5 整除」**
    (舊號段預估 113 年用罄,112/4 起釋出新號)。
    任何 2023 年前寫的驗證程式都會把新統編誤判成無效 —— 這裡兩制都收,
    但會在說明裡標出是哪一制過的,方便追查上游系統是不是也該更新。

    第 7 碼為 7 時有特例:總和或總和+1 任一可整除即算有效。

    **偵測力(本機實測,300 個有效統編 × 21,600 次單字元替換)**:
      新制 %5   抓到 85.1% 的單字元錯誤
      舊制 %10  抓到 98.0%
    也就是改制之後偵測力掉了 13 個百分點 —— 仍遠優於「只驗格式」(0%),
    但**不要當成保證**:約 1/7 的單字元 OCR 錯誤仍會通過。
    金額類欄位請另外做交叉驗算,不要只靠這個。
    """
    s = normalize_fullwidth(str(value or "")).strip()
    s = re.sub(r"[\s\-]", "", s)          # 容忍 OCR 讀出來的空白與連字號
    if not _TAX_ID_RE.match(s):
        return False, f"統編必須是 8 位數字,收到 {value!r}"

    total = _tax_id_sum(s)
    special = s[6] == "7"                  # 第 7 碼為 7 的特例

    new_ok = total % 5 == 0 or (special and (total + 1) % 5 == 0)
    old_ok = total % 10 == 0 or (special and (total + 1) % 10 == 0)

    if not new_ok:
        # 新制不過 → 舊制必定也不過(能被 10 整除必能被 5 整除)
        return False, (f"統編 {s} 檢查碼不符(加權總和 {total},需可被 5 整除)。"
                       f"常見原因:OCR 把數字讀錯、或少讀/多讀一位")
    if old_ok:
        return True, f"統編 {s} 有效"
    return True, (f"統編 {s} 有效(2023 新制)。"
                  f"注意:舊制 %10 規則會把它判成無效 —— "
                  f"若下游系統是 2023 年前寫的,可能會退件")


# ── 民國年 ───────────────────────────────────────────────
# 動機很實際:政府系統匯出的日期是「113/05/20」這種字串,
# Excel 認成**文字不是日期**,排序與運算全部失效。
_ROC_RE = re.compile(r"^(\d{2,3})\s*[/\-年.]\s*(\d{1,2})\s*[/\-月.]\s*(\d{1,2})\s*日?$")


def roc_to_ad(value: Optional[str]) -> Optional[date]:
    """民國年字串 → date。吃 113/05/20、113-5-20、113年5月20日。

    無法解析回 None —— 不猜。日期猜錯比讀不到嚴重。
    """
    s = normalize_fullwidth(str(value or "")).strip()
    m = _ROC_RE.match(s)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y + 1911, mo, d)
    except ValueError:
        return None                        # 例如 113/02/30


def ad_to_roc(value: date, sep: str = "/") -> str:
    """date → 民國年字串(民國前的日期不處理,直接 raise)。"""
    y = value.year - 1911
    if y <= 0:
        raise ValueError(f"{value} 早於民國元年,無法轉換")
    return f"{y}{sep}{value.month:02d}{sep}{value.day:02d}"


# ── 全形 → 半形 ──────────────────────────────────────────
# OCR 與政府系統匯出常混用全形數字/英文/標點,
# 直接拿去比對或轉數字會失敗且看不出原因(畫面上長得幾乎一樣)。
def normalize_fullwidth(s: Optional[str]) -> str:
    """全形英數與標點轉半形;全形空白轉半形空白。中文字不動。"""
    if not s:
        return ""
    out = []
    for ch in str(s):
        code = ord(ch)
        if code == 0x3000:                 # 全形空白
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:     # 全形 ! ~ ~
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)
