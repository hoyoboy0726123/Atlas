"""地端 GUI 定位模型（Mano-CUA / Qwen3-VL 系）客戶端。

用途：`vlm_mode='grounding'` — 給一句自然語言描述，回螢幕上的精確座標。

為什麼是獨立模組而不是走 llm_factory：
  llm_factory 出來的是通用雲端模型，2026-08-03 實測它們給座標不準，
  這也是既有 description / anchor_pick 兩個模式刻意「不讓 VLM 給座標」的原因。
  這裡接的是**專門訓練過 GUI 定位**的地端模型，實測 14/14 命中、
  誤差中位數 4.5px（1080p~2560x1600、網頁/Excel ribbon/檔案總管都涵蓋）。
  兩者能力不同、不該混用：地端這顆「定位強、讀字會編」，
  所以只拿它定位，讀字仍由既有 OCR / 雲端模型負責。

架構：模型跑在沙盒容器（吃 GPU），動作執行留在 Windows host。
      兩邊用檔案交換（容器與 host 共用掛載目錄），不用開網路埠。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 交換目錄（容器與 host 都看得到）
_IO_WIN = Path(os.environ.get("ATLAS_VLM_IO") or
               (Path(__file__).resolve().parent.parent.parent / "ai_output" / "_vlm_grounding"))
_REQ = _IO_WIN / "_req.json"
_RESP = _IO_WIN / "_resp.json"
_READY = _IO_WIN / "_ready"
_SERVER_PY = Path(__file__).resolve().parent / "vlm_grounding_server.py"

_LOAD_TIMEOUT = 180.0     # 模型載入上限（實測冷啟 12~30s）
_INFER_TIMEOUT = 60.0     # 單次推論上限（實測 1~7s）
# 啟動失敗後的冷卻期。實測「沙盒容器不存在」每次要 5 秒才失敗，
# 20 步的工作流全開 grounding 就白等 100 秒。失敗一次後直接短路，
# 讓每一步都用既有的 CV 路徑跑，不再重試。
_FAIL_COOLDOWN = 300.0
_proc: Optional[subprocess.Popen] = None
_fail_until: float = 0.0
_fail_why: str = ""


def shared_dir() -> Path:
    """容器與 host 共用的交換目錄。

    截圖一定要寫在這裡 —— Windows 的 %TEMP% 沒有掛進沙盒容器，
    寫進去容器會回「No such file or directory」（2026-08-03 實測踩到）。
    """
    _IO_WIN.mkdir(parents=True, exist_ok=True)
    return _IO_WIN


def _to_wsl(p: Path | str) -> str:
    """C:\\a\\b → /mnt/c/a/b"""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        return "/mnt/" + s[0].lower() + s[2:]
    return s


def _container_name() -> str:
    """沙盒容器名稱。單一事實來源是 pipeline.sandbox.CONTAINER_NAME。

    (2026-08-03 修正:原本讀 settings 的 `skill_sandbox_container` ——
     那個鍵**從來不存在**,是我憑空發明的。settings.py:88 的白名單載入
     會把不在 _DEFAULT 的鍵直接丟掉,所以它永遠讀到空字串、靠 fallback
     的寫死值救回來。改成讀既有常數,容器改名時兩邊才會一致。)
    """
    env = os.environ.get("ATLAS_SANDBOX_CONTAINER", "").strip()
    if env:
        return env
    try:
        from pipeline.sandbox import CONTAINER_NAME
        return CONTAINER_NAME
    except Exception:
        try:
            from .sandbox import CONTAINER_NAME  # 抽成獨立專案後包名可能不同
            return CONTAINER_NAME
        except Exception:
            return "atlas-sandbox"


# （2026-08-03 移除 is_enabled()）
# 原本打算做一個全域「啟用 GUI 定位」設定開關，但那個設計有兩個問題：
#   1. 它讀的 `vlm_grounding_enabled` 從來不在 settings._DEFAULT 裡，
#      而 settings.py:88 是白名單載入 —— 不在 _DEFAULT 的鍵會被丟掉，
#      所以它永遠回 False。函式本身也從沒被任何地方呼叫過，是死碼。
#   2. 更根本的是：使用者在某個步驟的面板上選「直接定位」，
#      本身就是明確的 per-step opt-in；再加一層全域開關只會讓人困惑
#      （「為什麼我選了卻不動？」）。
# 真正該擋的是「硬體/模型不具備」，那由 capability() 負責回報，前端據此停用按鈕。


def _server_alive() -> bool:
    """服務是否真的活著。

    不能只看 _ready 檔 —— 後端重啟、或上一輪關閉沒清乾淨時，
    殘留的 _ready 會讓這裡誤判成「還在跑」，接著每個請求都逾時
    （2026-08-03 實測踩到，整份測試因此無效）。
    _proc is None 代表這個行程沒有啟動過它，一律當成沒在跑、重新拉起。
    """
    return _READY.exists() and _proc is not None and _proc.poll() is None


_STATUS_CACHE: dict = {"ts": 0.0, "data": None}
_STATUS_TTL = 60.0


def capability(force: bool = False) -> dict:
    """回報這台機器能不能用 grounding，以及不能用的話缺什麼。

    仿 /settings/node-status 的慣例（含快取與 install_hint）——
    要 exec 進容器查，不能每次前端 render 都跑一次。
    """
    import subprocess as _sp
    import time as _t
    if not force and _STATUS_CACHE["data"] and (_t.time() - _STATUS_CACHE["ts"]) < _STATUS_TTL:
        return _STATUS_CACHE["data"]

    def _exec(script: str, timeout: float = 12.0) -> tuple[bool, str]:
        try:
            r = _sp.run(["wsl", "-d", "Ubuntu", "-e", "docker", "exec",
                         _container_name(), "bash", "-lc", script],
                        capture_output=True, text=True, timeout=timeout,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            return r.returncode == 0, (r.stdout or "").strip()
        except Exception:
            return False, ""

    sandbox_ok, _ = _exec("echo ok", 10.0)
    model_ok = gpu_ok = False
    vram = 0.0
    if sandbox_ok:
        model_ok, _ = _exec(
            "ls -d /root/.cache/huggingface/hub/models--Mininglamp-2718--Mano-CUA-* >/dev/null 2>&1")
        ok, out = _exec(
            "python3 -c \"import torch;print(torch.cuda.is_available(),"
            "torch.cuda.get_device_properties(0).total_memory/1e9 if torch.cuda.is_available() else 0)\"",
            25.0)
        if ok and out:
            parts = out.split()
            gpu_ok = parts[0] == "True"
            try:
                vram = float(parts[1])
            except (IndexError, ValueError):
                vram = 0.0

    # 精度門檻與 vlm_grounding_server.py 的自動判斷一致
    enough_vram = vram >= 4.5
    available = sandbox_ok and model_ok and gpu_ok and enough_vram
    if available:
        reason = ""
    elif not sandbox_ok:
        reason = "沙盒容器沒有在執行"
    elif not gpu_ok:
        reason = "沙盒容器看不到 NVIDIA GPU"
    elif not enough_vram:
        reason = f"顯卡記憶體不足（{vram:.1f}GB，至少需要 4.5GB）"
    else:
        reason = "GUI 定位模型尚未下載（8.9GB）"

    data = {
        "available": available,
        "sandbox_ok": sandbox_ok,
        "model_present": model_ok,
        "gpu_ok": gpu_ok,
        "vram_gb": round(vram, 1),
        "precision": ("fp16" if vram >= 11.0 else "int4") if available else "",
        "reason": reason,
        "install_hint": "執行 sandbox\\setup_sandbox.bat 並在詢問時選 y（或加 --with-gui-model）",
        # 這條容易被誤解，明講：computer_use 永遠在 Windows 桌面點擊，
        # 容器在這裡只是「裝了 GPU 模型的推論後端」，跟 skill 沙盒模式無關。
        "note": "此功能用沙盒容器當 GPU 推論後端。即使 skill 沙盒模式設為 host，仍需要容器運行。",
    }
    _STATUS_CACHE["ts"] = _t.time()
    _STATUS_CACHE["data"] = data
    return data


def reset_failure() -> None:
    """手動清掉冷卻（使用者裝好沙盒/模型後不用重啟後端）。"""
    global _fail_until, _fail_why
    _fail_until, _fail_why = 0.0, ""


def ensure_server(logger: logging.Logger | None = None) -> tuple[bool, str]:
    """確保推論服務在跑。已在跑就直接回 True。"""
    global _proc, _fail_until, _fail_why
    lg = logger or log
    if _server_alive():
        return True, "already running"
    if time.time() < _fail_until:
        return False, f"（冷卻中，前次失敗：{_fail_why}）"

    _IO_WIN.mkdir(parents=True, exist_ok=True)
    for f in (_REQ, _RESP, _READY):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # 注意：不能用 `docker exec -d`。實測分離的程序印完啟動訊息就被回收，
    # 服務看似啟動成功、實際已死（2026-08-03 踩過）。改用 Popen 讓它掛在
    # 後端程序下、生命週期跟著後端走。
    cmd = ["wsl", "-d", "Ubuntu", "-e", "docker", "exec", _container_name(),
           "python3", "-u", _to_wsl(_SERVER_PY), _to_wsl(_IO_WIN)]
    lg.info(f"[vlm_grounding] 啟動推論服務：{' '.join(cmd[-3:])}")
    try:
        _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return _mark_fail(lg, f"啟動失敗：{e.__class__.__name__}: {e}")

    t0 = time.time()
    while time.time() - t0 < _LOAD_TIMEOUT:
        if _READY.exists():
            lg.info(f"[vlm_grounding] 模型就緒（{time.time() - t0:.0f}s）")
            reset_failure()
            return True, "ok"
        if _proc.poll() is not None:
            return _mark_fail(lg, "服務程序意外結束（容器沒跑？模型沒下載？）")
        time.sleep(0.5)
    return _mark_fail(lg, f"模型載入逾時（>{_LOAD_TIMEOUT:.0f}s）")


def _mark_fail(lg: logging.Logger, why: str) -> tuple[bool, str]:
    global _fail_until, _fail_why
    _fail_until = time.time() + _FAIL_COOLDOWN
    _fail_why = why
    lg.warning(f"[vlm_grounding] {why} → {_FAIL_COOLDOWN:.0f}s 內不再重試，"
               f"這段期間所有 grounding 步驟直接走 CV")
    return False, why


def shutdown() -> None:
    global _proc
    try:
        _IO_WIN.mkdir(parents=True, exist_ok=True)
        _REQ.write_text(json.dumps({"cmd": "quit"}), encoding="utf-8")
        time.sleep(1.0)
    except Exception:
        pass
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:
            pass
    _proc = None
    # 一定要清 _ready —— 留著會讓下次 _server_alive() 誤判成還在跑
    for f in (_READY, _REQ, _RESP):
        try:
            f.unlink()
        except (FileNotFoundError, OSError):
            pass


def locate(prompt: str, screenshot_path: Path | str,
           img_w: int, img_h: int,
           logger: logging.Logger | None = None) -> tuple[bool, int, int, str]:
    """問模型「描述的東西在哪」，回 (ok, x, y, reason)。

    x/y 是相對 screenshot 左上角的像素座標；呼叫端自行加上截圖原點位移。
    """
    lg = logger or log
    ok, why = ensure_server(lg)
    if not ok:
        return False, 0, 0, f"推論服務不可用：{why}"

    try:
        _RESP.unlink()
    except (FileNotFoundError, OSError):
        pass
    try:
        _REQ.write_text(json.dumps(
            {"image": _to_wsl(screenshot_path), "prompt": prompt},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return False, 0, 0, f"寫入請求失敗：{e}"

    t0 = time.time()
    while time.time() - t0 < _INFER_TIMEOUT:
        if _RESP.exists():
            time.sleep(0.12)          # 等容器寫完
            try:
                r = json.loads(_RESP.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(0.2)
                continue
            if not r.get("ok"):
                return False, 0, 0, r.get("reason") or "模型未回座標"
            nx, ny = int(r["nx"]), int(r["ny"])
            # 模型輸出正規化到 [0,1000]，換回像素
            x = int(nx / 1000 * img_w)
            y = int(ny / 1000 * img_h)
            if not (0 <= x < img_w and 0 <= y < img_h):
                return False, 0, 0, f"座標 ({x},{y}) 超出截圖範圍 {img_w}x{img_h}"
            lg.info(f"[vlm_grounding] ({nx},{ny})/1000 → ({x},{y}) px"
                    f"（{r.get('elapsed', 0):.1f}s）{r.get('desp', '')[:60]}")
            return True, x, y, r.get("desp") or ""
        if _proc is not None and _proc.poll() is not None:
            return False, 0, 0, "推論服務中途死亡"
        time.sleep(0.15)
    return False, 0, 0, f"推論逾時（>{_INFER_TIMEOUT:.0f}s）"
