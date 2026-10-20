# -*- coding: utf-8 -*-
"""
建廠影響公文掃描 — 找出「外部單位的事情」會讓建廠暫停、延後或需要配合的公文

  STEP 1  掃 input\\pdf（目前的公文）與 input\\history（歷史公文），從檔名取日期/字號/發文單位
  STEP 2  依檔名日期篩選 → 關鍵字粗篩（掃描檔讀不到字的一律送，寧可多送不要漏）
  STEP 3  每篇轉成 PNG → 上傳 /files 拿 file_id → /chat/completions 判斷
  STEP 4  輸出三個 JSON：
            output\\construction_impact.json  還沒過去的影響，停工排最前面
            output\\history_cases.json        已經過去的案例 + input\\history 的公文（之後給 RAG）
            output\\not_related.json          粗篩排除 / LLM 判定與建廠無關（檢查有沒有漏）

在你自己的電腦上跑，所有檔案都在 BASE_DIR 底下，資料夾第一次執行時自動建立。
報表（input\\report）是選用的：有放就拿主旨跟 LLM 讀到的對照，沒放也照跑。

執行：
    python construction_impact_scan.py                    # 正式跑
    python construction_impact_scan.py --dry-run          # 不呼叫 LLM，只看掃到哪些、粗篩留下哪些
    python construction_impact_scan.py --days 30          # 只看檔名日期最近 30 天的公文
    python construction_impact_scan.py --since 2026-09-01 # 只看這天之後的公文
    python construction_impact_scan.py --all              # 不做關鍵字粗篩，日期範圍內全部送
    python construction_impact_scan.py --probe            # 只送粗篩後第一篇，印出原始回應
    python construction_impact_scan.py --probe-file "D:\\...\\xxx.pdf"   # 指定一個檔案測試
    python construction_impact_scan.py --no-cache         # 不用快取，全部重新判斷
    python construction_impact_scan.py --skip-history     # 這次不處理 input\\history

需要的套件：pip install pymupdf
（有放報表才需要：pip install pandas xlrd lxml）
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta

try:
    import pymupdf
except ImportError:          # 舊版 PyMuPDF 只有 fitz 這個名字
    import fitz as pymupdf


# ═══════════════════════════════════════════════════════════
# 設定區（要改的東西都在這裡）
# ═══════════════════════════════════════════════════════════

# ★ 根目錄，所有東西都在這底下 ★
BASE_DIR = r"D:\ASEKH\K20076\2027\Agent\Building MultiAGENT\official document"

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

# ── STEP 1：公文來源 ───────────────────────────────
# 目前的公文。子資料夾也會掃。想直接讀 U: 就在這裡多加一行路徑，例如：
#   r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\新增資料夾",
PDF_FOLDERS = [
    os.path.join(INPUT_DIR, "pdf"),
]

# 歷史公文：已經過去的案例，一樣整理成固定格式，放進 history_cases.json 給 RAG 用。
# 不做日期篩選。
HISTORY_FOLDERS = [
    os.path.join(INPUT_DIR, "history"),
]

# 選用：環安衛報表（.xls / .xlsx / .html）。有放就用「來文字號」對回主旨，沒放就略過。
REPORT_DIR = os.path.join(INPUT_DIR, "report")

FILE_EXTS = (".pdf", ".png", ".jpg", ".jpeg")

# ── STEP 2：篩選 ───────────────────────────────────
# 預設只看檔名日期在最近幾天內的公文（--days / --since 可以臨時改）
DAYS = 90

# 關鍵字粗篩：檔名、報表主旨、PDF 文字層任何一處出現就送 LLM。刻意放寬，寧可多送。
# 讀不到文字的掃描檔不管有沒有中關鍵字都會送。
KEYWORDS = [
    # 施工 / 建管
    "施工", "工程", "工地", "新建", "擴建", "建廠", "建造執照", "建照", "使用執照", "使照",
    "竣工", "室內裝修", "變更使用", "停工", "復工", "暫停", "會勘", "查驗", "勘查",
    "現場檢查", "稽查", "檢查",
    # 消防 / 勞檢
    "消防", "消檢", "勞動檢查", "勞檢", "職業安全", "職安",
    # 環保
    "揚塵", "噪音", "空污", "空氣污染", "營建剩餘土石方", "逕流廢水",
    # 水電 / 道路 / 管線
    "停電", "停水", "送電", "臨時電", "斷電", "道路", "路權", "封閉", "挖掘", "交通維持",
    "管線", "瓦斯",
]
# 內文或檔名出現廠號（K3B、K27P2…）也算命中
PLANT_PATTERN = re.compile(r"k\d{1,2}[a-z]?\d?", re.IGNORECASE)

# 文字層少於這麼多字就當成掃描檔（本機讀不到字）
MIN_TEXT_CHARS = 50

# ── STEP 3：LLM ────────────────────────────────────
ENDPOINT = "https://aiagent.domaintwin.kh.asegroup.com:4231/public/kits/openai/v1/chat/completions"
FILES_URL = ENDPOINT.rsplit("/chat/completions", 1)[0] + "/files"

# ★ key 和 agent id 要成對（每個 agent 有自己的 key，混用會回 401）★
#   key 三個來源，由上到下找第一個有值的：這個變數 → 環境變數 ASE_LLM_API_KEY → 下面那個檔案
#   ⚠ 直接填在這裡的話，這個檔案不要 commit / push 到 git
API_KEY = ""                    # ← "ask_..." 貼在這對引號中間
API_KEY_FILE = os.path.join(BASE_DIR, "api_key.txt")
AGENT_ID = ""                   # ← 你的 agent-id

UPLOAD_TIMEOUT = 60             # 秒，上傳一張圖
CHAT_TIMEOUT = 420              # 秒，一次判斷
LLM_RETRIES = 3                 # 連線失敗 / 5xx（含 504 逾時）時的重試次數
LLM_GAP = 1.0                   # 每篇之間隔幾秒

# 平台限制：單案最多 20 張、長邊縮到 1600px，圖太多容易 504
MAX_PAGES = 20
LONG_SIDE = 1600

# PDF 有文字層時，連同文字一起送，幫 LLM 讀準；太長就截斷
TEXT_LAYER_CHARS = 4000

PROMPT_FILE = os.path.join(HERE, "prompt", "construction_impact.txt")

# ★ 改完 prompt 之後，把這個數字 +1，不然會直接命中舊快取 ★
PROMPT_VERSION = 1

# ── STEP 4：輸出 ───────────────────────────────────
CURRENT_JSON = os.path.join(OUTPUT_DIR, "construction_impact.json")
HISTORY_JSON = os.path.join(OUTPUT_DIR, "history_cases.json")
NOT_RELATED_JSON = os.path.join(OUTPUT_DIR, "not_related.json")

CACHE_FILE = os.path.join(CACHE_DIR, "construction_cache.json")
LOG_FILE = os.path.join(CACHE_DIR, "construction_scan.log")


# ═══════════════════════════════════════════════════════════
# 欄位定義
# ═══════════════════════════════════════════════════════════

TEXT_FIELDS = ("來文字號", "主旨", "發文機關", "影響廠棟", "時段", "需要配合事項", "原文依據")
DATE_FIELDS = ("發文日期", "開始日期", "結束日期")

# 輸出排序也照這個順序：停工最前面，判斷失敗的排在停工後面，避免被忽略
IMPACT_LEVELS = ("全面停工", "部分停工", "待確認", "需配合作業", "僅需文件回覆", "無影響")
HAS_IMPACT = ("全面停工", "部分停工", "需配合作業")
CAUSES = ("消防檢查", "勞動檢查", "建管查驗", "環保稽查", "停電停水", "道路封閉",
          "會勘", "停工令", "審查未過", "其他")
CONFIDENCE = ("高", "中", "低")


# ═══════════════════════════════════════════════════════════
# 共用工具
# ═══════════════════════════════════════════════════════════

log = logging.getLogger("construction_scan")


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def ensure_dirs():
    """第一次執行時把資料夾結構建好。"""
    for folder in [INPUT_DIR, OUTPUT_DIR, CACHE_DIR, REPORT_DIR, *PDF_FOLDERS, *HISTORY_FOLDERS]:
        if folder.startswith(BASE_DIR):        # U: 之類外部的資料夾不要自己建
            os.makedirs(folder, exist_ok=True)


def atomic_write_json(path, data):
    """先寫同目錄的暫存檔再 os.replace，避免半截檔。"""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)


def norm(s):
    """正規化比對用字串：全形轉半形、去所有空白、轉小寫。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", s).lower()


def norm_words(s):
    """比對原文依據用：再把標點符號全部拿掉（抽字和 LLM 抄錄的標點常常不一樣）。"""
    return re.sub(r"[\W_]+", "", norm(s))


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def num_key(s):
    """數字比對用的鍵：去掉前導 0，讓 '010' 和 '10' 視為同一個。"""
    d = digits(s)
    if not d:
        return ""
    return d.lstrip("0") or "0"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_iso_date(value):
    """'2026-10-21'、'2026/10/21'、'115年10月21日'、'115.10.21' → '2026-10-21'；認不得回傳 ""。"""
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    m = re.search(r"(\d{2,4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", s)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if y < 1911:
        y += 1911                              # 民國年
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


# ═══════════════════════════════════════════════════════════
# STEP 1  掃公文
# ═══════════════════════════════════════════════════════════

def parse_file_date(s):
    """檔名第一段的日期：1150901（民國）或 20260901（西元）。"""
    s = str(s).strip()
    if not s.isdigit():
        return None
    try:
        if len(s) == 7:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
        if len(s) == 8 and s[:2] in ("19", "20"):
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def parse_filename(name):
    """檔名格式：日期_字號_發文單位_..._

      1150901_11512300_高局_EB76027628_廠務_(Security C).pdf
        → 檔名日期 2026-09-01、字號 11512300、發文單位 高局
    """
    stem = os.path.splitext(os.path.basename(str(name)))[0]
    parts = stem.split("_")
    return {
        "檔名日期": parse_file_date(parts[0]) if parts else None,
        "檔名字號": parts[1] if len(parts) >= 2 else "",
        "檔名發文單位": parts[2] if len(parts) >= 3 else "",
    }


def read_pdf_info(path):
    """回傳 (頁數, 文字層)。圖片檔沒有文字層。"""
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        if not path.lower().endswith(".pdf"):
            return pages, ""
        text = "".join(page.get_text() for page in doc)
    return pages, re.sub(r"\s+", " ", text).strip()


def scan_folders(folders, source):
    """掃資料夾（含子資料夾），回傳每個檔案的基本資訊。"""
    docs = []
    for folder in folders:
        if not os.path.isdir(folder):
            log.error("資料夾讀不到（路徑打錯？U: 沒掛載？）：%s", folder)
            continue

        found = 0
        for root, _, names in os.walk(folder):
            for name in sorted(names):
                if not name.lower().endswith(FILE_EXTS):
                    continue
                path = os.path.join(root, name)
                try:
                    pages, text = read_pdf_info(path)
                    sha = file_sha256(path)
                except Exception as e:
                    log.warning("檔案讀取失敗 %s：%s: %s", name, type(e).__name__, e)
                    continue
                docs.append({"檔名": name, "檔案路徑": path, "來源": source,
                             "頁數": pages, "文字層": text, "sha": sha, **parse_filename(name)})
                found += 1
        log.info("%s → %d 個檔案（%s）", folder, found, source)

    no_date = [d["檔名"] for d in docs if d["檔名日期"] is None]
    if no_date:
        log.warning("檔名第一段不是日期，無法做日期篩選（一律保留）：%s", no_date[:10])
    return docs


def read_report(path):
    """讀報表。ASP.NET 匯出的 .xls 常常其實是 HTML 表格，所以要有 fallback。"""
    import pandas as pd
    try:
        return pd.read_excel(path)
    except Exception as e:
        log.info("pd.read_excel 失敗（%s），改用 read_html 解析…", type(e).__name__)
    tables = pd.read_html(path, encoding="utf-8")
    if not tables:
        raise ValueError(f"報表既不是 Excel 也不是 HTML 表格：{path}")
    return max(tables, key=len)


def load_report_subjects():
    """選用：從報表取「來文字號 → 主旨」。沒有報表或讀不了就回傳空的，不影響主流程。"""
    files = []
    if os.path.isdir(REPORT_DIR):
        files = [os.path.join(REPORT_DIR, n) for n in os.listdir(REPORT_DIR)
                 if n.lower().endswith((".xls", ".xlsx", ".htm", ".html"))]
    if not files:
        log.info("input\\report 沒有報表，主旨直接由 LLM 從公文讀")
        return {}

    path = max(files, key=os.path.getmtime)
    try:
        df = read_report(path)
    except ImportError as e:
        log.warning("有報表但缺套件（pip install pandas xlrd lxml），這次不用報表：%s", e)
        return {}
    except Exception as e:
        log.warning("報表讀取失敗，這次不用報表：%s：%s: %s", path, type(e).__name__, e)
        return {}

    if "來文字號" not in df.columns or "主旨" not in df.columns:
        log.warning("報表缺少「來文字號」或「主旨」欄位，這次不用報表。實際欄位：%s", list(df.columns))
        return {}

    subjects = {}
    for _, row in df.iterrows():
        key = num_key(row["來文字號"])
        if key and key not in subjects:
            subjects[key] = str(row["主旨"]).strip()
    log.info("報表 %s → %d 筆主旨", os.path.basename(path), len(subjects))
    return subjects


# ═══════════════════════════════════════════════════════════
# STEP 2  日期篩選 + 關鍵字粗篩
# ═══════════════════════════════════════════════════════════

def prefilter(doc):
    """回傳 (要不要送 LLM, 原因)。"""
    hay = norm(doc["檔名"] + doc.get("報表主旨", "") + doc["文字層"])
    hits = [k for k in KEYWORDS if norm(k) in hay]
    hits += sorted({m.upper() for m in PLANT_PATTERN.findall(hay)})
    if hits:
        return True, "關鍵字：" + "、".join(hits[:6])
    if len(doc["文字層"]) < MIN_TEXT_CHARS:
        return True, "掃描檔，本機讀不到文字"
    return False, "有文字層但沒有任何關鍵字"


# ═══════════════════════════════════════════════════════════
# STEP 3  LLM（先上傳圖片拿 file_id，再跟文字一起送）
# ═══════════════════════════════════════════════════════════

def get_api_key():
    if not AGENT_ID.strip():
        raise RuntimeError("AGENT_ID 還沒填（設定區），key 和 agent id 要成對。")

    if API_KEY.strip():
        return API_KEY.strip()

    key = os.environ.get("ASE_LLM_API_KEY")
    if key:
        log.info("API key 來自環境變數 ASE_LLM_API_KEY")
        return key.strip()

    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r", encoding="utf-8-sig") as f:
            key = f.read().strip()
        if key:
            log.info("API key 來自 %s", API_KEY_FILE)
            return key

    raise RuntimeError(
        "找不到 API key。三選一：\n"
        "  1) 直接填在設定區的 API_KEY\n"
        "  2) 設環境變數 ASE_LLM_API_KEY\n"
        f"  3) 存成單行文字檔放在 {API_KEY_FILE}"
    )


def _open(req, timeout):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                # 內網自簽憑證
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def upload_image(api_key, path):
    """上傳一張圖 → 回傳 file_id。"""
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(
        os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as f:
        data = f.read()
    b = "----x" + uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nvision\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: {mime}\r\n\r\n"
            ).encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(FILES_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={b}"})
    res = _open(req, UPLOAD_TIMEOUT)
    file_id = res.get("id") or res.get("file_id") or (res.get("data") or {}).get("id")
    if not file_id:
        raise RuntimeError(f"上傳成功但回應裡找不到 file_id：{str(res)[:300]}")
    return file_id


def ask(api_key, prompt, image_paths):
    """文字 + file_id 一起送。file_id 每次都重新上傳，不重複使用。"""
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "file", "file": {"file_id": upload_image(api_key, p)}})
    payload = json.dumps({"model": AGENT_ID,
                          "messages": [{"role": "user", "content": content}],
                          "temperature": 0}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=payload, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    res = _open(req, CHAT_TIMEOUT)
    try:
        msg = res["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"回應格式不是 choices[0].message.content：{str(res)[:300]}")
    if isinstance(msg, list):
        msg = "".join(x.get("text", "") for x in msg if isinstance(x, dict))
    return (msg or "").strip()


def call_llm(api_key, prompt, image_paths):
    """ask() 加上重試。連線失敗 / 5xx 會退避重試，4xx 直接丟出（重試也沒用）。"""
    last_error = None
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            return ask(api_key, prompt, image_paths)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            if e.code == 401:
                raise RuntimeError(f"HTTP 401：key 和 AGENT_ID 不成對，或 key 失效。{body}")
            if 400 <= e.code < 500:
                raise RuntimeError(f"HTTP {e.code}：{body}")
            last_error = RuntimeError(f"HTTP {e.code}: {body[:200]}")
            log.warning("第 %d 次伺服器錯誤：HTTP %d%s", attempt, e.code,
                        "（逾時，頁數太多的話調低 MAX_PAGES）" if e.code == 504 else "")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            log.warning("第 %d 次連線失敗：%s: %s", attempt, type(e).__name__, e)

        if attempt < LLM_RETRIES:
            wait = 2 ** attempt
            log.info("   %d 秒後重試…", wait)
            time.sleep(wait)

    raise RuntimeError(f"呼叫 LLM 失敗（已重試 {LLM_RETRIES} 次）：{last_error}")


def render_pages(path, out_dir):
    """PDF / 圖片 → PNG，長邊縮到 LONG_SIDE，最多 MAX_PAGES 頁。"""
    paths = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES:
                break
            zoom = LONG_SIDE / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            out = os.path.join(out_dir, f"p{i + 1:02d}.png")
            pix.save(out)
            paths.append(out)
    return paths


def load_prompt():
    if not os.path.exists(PROMPT_FILE):
        raise FileNotFoundError(f"找不到 prompt 檔：{PROMPT_FILE}")
    with open(PROMPT_FILE, "r", encoding="utf-8-sig") as f:
        return f.read().strip()


def build_prompt(template, doc):
    info = {
        "檔名": doc["檔名"],
        "檔名日期": doc["檔名日期"].isoformat() if doc["檔名日期"] else "",
        "檔名字號": doc["檔名字號"],
        "檔名發文單位": doc["檔名發文單位"],
    }
    if doc.get("報表主旨"):
        info["報表主旨"] = doc["報表主旨"]

    text = f"{template}\n\n【檔案資訊】\n{json.dumps(info, ensure_ascii=False, indent=2)}"
    if len(doc["文字層"]) >= MIN_TEXT_CHARS:
        text += ("\n\n【PDF 文字層（電腦抽出的文字，可能有錯字或缺漏，以圖片為準）】\n"
                 + doc["文字層"][:TEXT_LAYER_CHARS])
    return text


def parse_json_object(text):
    """把回應解析成 dict。容忍 ```json 包裹、前後多餘說明。"""
    if not text or not text.strip():
        raise ValueError("回應是空的")

    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError(f"回應裡找不到 JSON：{cleaned[:300]}")
        data = json.loads(match.group(0))

    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict)), None)
    if not isinstance(data, dict):
        raise ValueError(f"解析出來不是 JSON 物件：{cleaned[:300]}")
    return data


def to_bool(value):
    if isinstance(value, bool):
        return value
    return norm(value) in ("true", "是", "yes", "y", "1")


def normalize_review(item):
    """補齊缺漏欄位、把選項收斂到固定值。不在選項裡的值不丟掉，記在 _原始值 供檢查。"""
    out, raw = {}, {}
    for field in TEXT_FIELDS:
        value = item.get(field)
        out[field] = str(value).strip() if value not in (None, "") else ""

    out["是否建廠相關"] = to_bool(item.get("是否建廠相關"))

    for field in DATE_FIELDS:
        value = str(item.get(field) or "").strip()
        out[field] = to_iso_date(value)
        if value and not out[field]:
            raw[field] = value

    for field, choices, fallback in (("影響程度", IMPACT_LEVELS, "待確認"),
                                     ("原因類別", CAUSES, "其他"),
                                     ("信心度", CONFIDENCE, "低")):
        value = str(item.get(field) or "").strip()
        out[field] = value if value in choices else fallback
        if value and value not in choices:
            raw[field] = value

    if raw:
        out["_原始值"] = raw
    return out


def failed_review(reason):
    review = normalize_review({})
    review.update({"是否建廠相關": None, "影響程度": "待確認", "_失敗": reason})
    return review


def check_evidence(quote, text_layer):
    """原文依據是不是真的出現在 PDF 文字層裡。"""
    if not quote:
        return "沒有原文依據"
    if len(text_layer) < MIN_TEXT_CHARS:
        return "掃描檔，無法自動比對"
    hay = norm_words(text_layer)
    # LLM 抄長句時常用「…」省略中間，分段各自比對
    segments = [norm_words(s) for s in re.split(r"[…⋯]+|\.{3,}", quote)]
    segments = [s for s in segments if len(s) >= 6] or [norm_words(quote)]
    return "符合原文" if all(s in hay for s in segments) else "原文找不到"


def same_subject(a, b):
    a, b = norm_words(a), norm_words(b)
    if not a or not b:
        return True
    return a[:12] in b or b[:12] in a


def finalize(doc, review, reason):
    """加上檔案資訊與程式端的檢查結果，標出需要人工確認的地方。"""
    notes = []
    review = dict(review)
    raw = review.pop("_原始值", {})
    for field, value in raw.items():
        notes.append(f"{field}「{value}」不是預期的格式/選項")

    if review.pop("_失敗", None) is not None:
        notes.append(f"自動判斷失敗：{doc.get('_錯誤', '')}")

    evidence = check_evidence(review["原文依據"], doc["文字層"])
    if evidence == "原文找不到":
        review["信心度"] = "低"
        notes.append("原文依據在 PDF 文字層裡找不到，可能是 LLM 改寫或編造")
    elif evidence == "沒有原文依據" and review["是否建廠相關"]:
        notes.append("沒有原文依據")

    if review["影響程度"] in HAS_IMPACT and not review["開始日期"]:
        notes.append("有影響但沒有日期")

    if doc.get("報表主旨") and not same_subject(doc["報表主旨"], review["主旨"]):
        notes.append(f"LLM 讀到的主旨跟報表不同（報表：{doc['報表主旨']}）")

    key = num_key(doc["檔名字號"])
    if key and review["來文字號"] and key not in digits(review["來文字號"]):
        notes.append(f"LLM 讀到的字號跟檔名不同（檔名：{doc['檔名字號']}）")

    if doc["頁數"] > MAX_PAGES:
        notes.append(f"共 {doc['頁數']} 頁，只送前 {MAX_PAGES} 頁")

    need_check = (review["影響程度"] == "待確認" or review["信心度"] == "低"
                  or evidence == "原文找不到"
                  or (review["影響程度"] in HAS_IMPACT and not review["開始日期"])
                  or any(n.startswith(("自動判斷失敗", "LLM 讀到的主旨")) for n in notes))

    return {
        "檔名": doc["檔名"],
        "來源": doc["來源"],
        "檔名日期": doc["檔名日期"].isoformat() if doc["檔名日期"] else "",
        **review,
        "依據檢查": evidence,
        "需人工確認": need_check,
        "檢查備註": notes,
        "粗篩原因": reason,
        "檔案路徑": doc["檔案路徑"],
    }


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("快取讀取失敗，重新建立：%s", e)
        return {}


def cache_key(doc):
    raw = f"v{PROMPT_VERSION}|{doc['sha']}|{doc.get('報表主旨', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def judge_one(api_key, template, doc):
    """一篇：轉圖 → 上傳 → 判斷。暫存圖片用完就刪。"""
    work = tempfile.mkdtemp(prefix="pages_", dir=CACHE_DIR)
    try:
        images = render_pages(doc["檔案路徑"], work)
        if not images:
            raise ValueError("轉不出任何一頁圖片")
        raw = call_llm(api_key, build_prompt(template, doc), images)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return raw, normalize_review(parse_json_object(raw))


def step3_judge(candidates, cached_only, use_cache=True):
    """candidates 要判斷（快取沒有就呼叫 LLM）；cached_only 只看快取（日期範圍外的舊結果）。

    回傳 [(doc, review, 粗篩原因), ...] 與失敗篇數。
    """
    cache = load_cache()
    if not use_cache:
        log.info("--no-cache：略過快取，每篇都重新呼叫 LLM。")

    pairs = []
    for doc in cached_only:
        key = cache_key(doc)
        if key in cache:
            pairs.append((doc, cache[key], "日期範圍外，沿用之前的判斷"))

    api_key = template = None
    if candidates and (not use_cache or any(cache_key(d) not in cache for d, _ in candidates)):
        api_key, template = get_api_key(), load_prompt()

    hits, fails = 0, 0
    for i, (doc, reason) in enumerate(candidates, 1):
        key = cache_key(doc)
        if use_cache and key in cache:
            hits += 1
            pairs.append((doc, cache[key], reason))
            continue

        try:
            _, review = judge_one(api_key, template, doc)
            cache[key] = review
            atomic_write_json(CACHE_FILE, cache)    # 每篇都存，中途斷掉不會白跑
            log.info("[%d/%d] %s → %s / %s / %s", i, len(candidates), doc["檔名"],
                     "建廠相關" if review["是否建廠相關"] else "無關",
                     review["影響程度"], review["開始日期"] or "無日期")
        except (RuntimeError, ValueError, OSError) as e:
            fails += 1
            log.error("[%d/%d] %s → 失敗：%s: %s", i, len(candidates), doc["檔名"],
                      type(e).__name__, e)
            doc["_錯誤"] = f"{type(e).__name__}: {e}"
            review = failed_review(doc["_錯誤"])
        pairs.append((doc, review, reason))
        time.sleep(LLM_GAP)

    log.info("STEP 3 完成：%d 篇，快取命中 %d、失敗 %d", len(candidates), hits, fails)
    return pairs, fails


# ═══════════════════════════════════════════════════════════
# STEP 4  分流輸出
# ═══════════════════════════════════════════════════════════

def is_past(rec, today):
    end = rec["結束日期"] or rec["開始日期"]
    return bool(end) and end < today.isoformat()


def step4_write(pairs, excluded, today):
    current, history, not_related = [], [], []

    for doc, review, reason in pairs:
        rec = finalize(doc, review, reason)
        if rec["是否建廠相關"] is False:
            not_related.append({"檔名": rec["檔名"], "來源": rec["來源"], "檔名日期": rec["檔名日期"],
                                "主旨": rec["主旨"], "原因": "LLM 判定與建廠無關",
                                "檔案路徑": rec["檔案路徑"]})
        elif rec["來源"] == "歷史" or is_past(rec, today):
            history.append(rec)
        else:
            current.append(rec)

    for doc, reason in excluded:
        not_related.append({"檔名": doc["檔名"], "來源": doc["來源"],
                            "檔名日期": doc["檔名日期"].isoformat() if doc["檔名日期"] else "",
                            "主旨": doc.get("報表主旨", ""), "原因": f"粗篩排除：{reason}",
                            "檔案路徑": doc["檔案路徑"]})

    rank = {level: i for i, level in enumerate(IMPACT_LEVELS)}
    current.sort(key=lambda r: (rank.get(r["影響程度"], len(rank)),
                                r["開始日期"] or "9999-99-99", r["檔名"]))
    history.sort(key=lambda r: (r["開始日期"] or r["檔名日期"], r["檔名"]), reverse=True)
    not_related.sort(key=lambda r: (r["檔名日期"], r["檔名"]), reverse=True)

    atomic_write_json(CURRENT_JSON, current)
    atomic_write_json(HISTORY_JSON, history)
    atomic_write_json(NOT_RELATED_JSON, not_related)

    stop = sum(1 for r in current if r["影響程度"] in ("全面停工", "部分停工"))
    check = sum(1 for r in current + history if r["需人工確認"])
    log.info("STEP 4 完成：")
    log.info("  目前的影響 %d 篇（其中停工 %d 篇）→ %s", len(current), stop, CURRENT_JSON)
    log.info("  歷史案例   %d 篇 → %s", len(history), HISTORY_JSON)
    log.info("  無關/排除  %d 篇 → %s", len(not_related), NOT_RELATED_JSON)
    log.info("  需人工確認 %d 篇", check)
    for r in current:
        if r["影響程度"] in ("全面停工", "部分停工"):
            log.info("  ★ %s %s %s %s（%s）", r["開始日期"] or "日期未定", r["影響程度"],
                     r["影響廠棟"] or "廠棟未定", r["原因類別"], r["發文機關"])


# ═══════════════════════════════════════════════════════════

def probe(doc):
    """送一篇並把送出去的文字、收回來的原始回應、整理後的結果都印出來。"""
    api_key, template = get_api_key(), load_prompt()
    prompt = build_prompt(template, doc)
    print("─" * 60)
    print(f"檔案：{doc['檔案路徑']}（{doc['頁數']} 頁，送前 {min(doc['頁數'], MAX_PAGES)} 頁）")
    print("送出的文字（最後 1500 字）：")
    print(prompt[-1500:])

    raw, review = judge_one(api_key, template, doc)
    print("─" * 60)
    print("原始回應：")
    print(raw[:4000])
    print("─" * 60)
    print("整理後（含程式檢查）：")
    print(json.dumps(finalize(doc, review, "probe"), ensure_ascii=False, indent=2))
    print("─" * 60)
    return 0


def print_dry_run(candidates, excluded, out_of_range):
    print("─" * 60)
    print(f"會送 LLM：{len(candidates)} 篇")
    for doc, reason in candidates:
        print(f"  [{doc['來源']}] {doc['檔名']}（{doc['頁數']} 頁）— {reason}")
    print(f"粗篩排除：{len(excluded)} 篇")
    for doc, reason in excluded:
        print(f"  [{doc['來源']}] {doc['檔名']} — {reason}")
    print(f"日期範圍外（只沿用快取，不送）：{len(out_of_range)} 篇")
    print("─" * 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="不呼叫 LLM，只看掃描與粗篩結果")
    parser.add_argument("--days", type=int, default=DAYS, help=f"只看檔名日期最近幾天（預設 {DAYS}）")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="只看這天之後的公文（優先於 --days）")
    parser.add_argument("--all", action="store_true", help="不做關鍵字粗篩，日期範圍內全部送")
    parser.add_argument("--probe", action="store_true", help="只送粗篩後第一篇，印出原始回應")
    parser.add_argument("--probe-file", metavar="PATH", help="指定一個 PDF/圖片送一次，印出原始回應")
    parser.add_argument("--no-cache", action="store_true", help="略過快取，每篇都重新判斷")
    parser.add_argument("--skip-history", action="store_true", help="這次不處理 input\\history")
    args = parser.parse_args()

    ensure_dirs()
    setup_logging()
    started = datetime.now()
    today = date.today()
    log.info("=" * 60)
    log.info("建廠影響掃描開始 %s（PROMPT_VERSION=%d）", started.strftime("%Y-%m-%d %H:%M:%S"),
             PROMPT_VERSION)

    try:
        if args.probe_file:
            path = os.path.abspath(args.probe_file)
            pages, text = read_pdf_info(path)
            name = os.path.basename(path)
            return probe({"檔名": name, "檔案路徑": path, "來源": "目前", "頁數": pages,
                          "文字層": text, "sha": file_sha256(path), **parse_filename(name)})

        # STEP 1
        subjects = load_report_subjects()
        docs = scan_folders(PDF_FOLDERS, "目前")
        if not args.skip_history:
            docs += scan_folders(HISTORY_FOLDERS, "歷史")
        for doc in docs:
            subject = subjects.get(num_key(doc["檔名字號"]))
            if subject:
                doc["報表主旨"] = subject

        # STEP 2
        if args.since:
            cutoff = datetime.strptime(args.since, "%Y-%m-%d").date()
        else:
            cutoff = today - timedelta(days=args.days)
        in_range, out_of_range = [], []
        for doc in docs:
            if doc["來源"] == "目前" and doc["檔名日期"] and doc["檔名日期"] < cutoff:
                out_of_range.append(doc)
            else:
                in_range.append(doc)
        log.info("STEP 2 日期篩選：%s 之後 %d 篇、範圍外 %d 篇（歷史資料夾不篩日期）",
                 cutoff.isoformat(), len(in_range), len(out_of_range))

        candidates, excluded = [], []
        for doc in in_range:
            send, reason = (True, "--all") if args.all else prefilter(doc)
            (candidates if send else excluded).append((doc, reason))
        log.info("STEP 2 粗篩：送 LLM %d 篇、排除 %d 篇", len(candidates), len(excluded))

        if args.dry_run:
            print_dry_run(candidates, excluded, out_of_range)
            log.info("--dry-run：不呼叫 LLM、不寫輸出檔。")
            return 0

        if args.probe:
            if not candidates:
                log.error("粗篩後沒有公文可以測。")
                return 1
            return probe(candidates[0][0])

        # STEP 3
        pairs, fails = step3_judge(candidates, out_of_range, use_cache=not args.no_cache)
        if candidates and fails == len(candidates):
            raise RuntimeError("所有公文都判斷失敗，不覆蓋輸出檔。")

        # STEP 4
        step4_write(pairs, excluded, today)
        log.info("全部完成，耗時 %s", datetime.now() - started)
        return 0

    except Exception as e:
        log.exception("執行失敗：%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
