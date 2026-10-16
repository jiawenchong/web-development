# -*- coding: utf-8 -*-
"""
公文簽核 agent - 單一入口 pipeline

把原本 4 支腳本 + RPA + 剪貼簿 合併成一支：

  STEP 1  下載環安衛報表 (呼叫既有的 Selenium 腳本)
  STEP 2  讀 Excel + 掃 PDF → 組出「未簽核公文全文」
  STEP 3  POST 到 Pensieve flow_chat 取得 summary / 指派 / 知會 / comment
  STEP 4  原子寫入 web\data\officialdoc.json（feedback_server.py 直接提供這個網頁）
  STEP 5  發信（待補）

全部跑在 10.10.28.170 上，所有檔案都在 BASE_DIR 底下（本機路徑），
排程不需要任何網路磁碟或登入，詳見 README「10.10.28.170 資料夾結構」。

設計原則（針對「手動 OK、RPA 有時候不行」）：
  * 完全不用剪貼簿——剪貼簿在 RPA / Session 0 下本來就不可靠
  * 不用 bare except；任何一步失敗都會 log 出原因並讓程式非 0 結束
  * 一律用本機路徑；真的要讀別台的檔案時用 UNC，不用磁碟機代號（排程帳號下可能沒掛載）
  * 寫檔一律 temp + os.replace（原子），不會出現寫到一半的 JSON
  * 讀下載檔前先等檔案大小穩定，避免讀到還在寫入的檔
  * 每篇公文各自 POST 一次，單篇失敗不會拖垮整批
  * 已審過的公文會走本機快取，重跑不重複呼叫

執行：
    python officialdoc_pipeline.py
    python officialdoc_pipeline.py --skip-download      # 用現有的報表檔
    python officialdoc_pipeline.py --dry-run            # 不呼叫 API，只看抓到幾篇
    python officialdoc_pipeline.py --probe              # 只送一篇，把原始回應印出來
    python officialdoc_pipeline.py --upload-knowledge   # 只把回饋案例上傳到 Pensieve 知識庫（測試用）
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

import pandas as pd
import pdfplumber
import requests


# ═══════════════════════════════════════════════════════════
# 設定區（要改的東西都在這裡）
# ═══════════════════════════════════════════════════════════

# ★ 10.10.28.170 上的根目錄，所有東西都在這底下。那台沒有 D: 就只改這一行 ★
BASE_DIR = r"D:\officialdoc_agent"

WORK_DIR = BASE_DIR              # 程式、token、log、快取、下載的報表都放這裡

# ── STEP 1：下載腳本 ───────────────────────────────
RUN_DOWNLOAD = True
DOWNLOAD_SCRIPT = os.path.join(WORK_DIR, "download_report.py")   # ← 確認你的檔名
DOWNLOAD_TIMEOUT = 600                                            # 秒

# ── STEP 2：報表與 PDF ─────────────────────────────
# download_report.py 要把報表下載到 WORK_DIR（Selenium 的 download.default_directory）
REPORT_XLS = os.path.join(WORK_DIR, "環安衛收發文管理查詢報表_(Security B).xls")
TARGET_STATUS = "FAC廠處長簽核"

# 公文 PDF 放這兩個資料夾（對應原本 U: 底下的「新增資料夾」與「公文下載-自動化\解壓縮」）。
# ★ 如果 PDF 還是由別人 / 別的程式放在 U: 那個共用資料夾，這裡要改成那個共用資料夾的
#   UNC 路徑（\\伺服器\分享\...），不要用 U:，排程帳號下沒有 U:。
PDF_FOLDERS = [
    os.path.join(BASE_DIR, "pdf", "scan"),
    os.path.join(BASE_DIR, "pdf", "download"),
]

FULLTEXT_JSON = os.path.join(WORK_DIR, "未簽核的公文全文.json")   # 中繼檔（除錯用）

# ── STEP 3：Pensieve flow_chat ────────────────────
PENSIEVE_URL = "https://pensieve.kh.asegroup.com/api/flow_chat/"

# ★ 這是「呼叫 Pensieve API 的人」的工號——也就是你自己的 ★
#   注意：跟下載腳本裡那個 EMP_NO = "D8698" 完全不同。
#   那個是要在環安衛網頁上查詢的「承辦人工號」，跟這個 API 沒關係。
PENSIEVE_EMPNO = "K20076"          # ← 填你自己的工號

# ★ Flow Token 填這裡 ★
#   三個來源，由上到下找第一個有值的：這個變數 → 環境變數 PENSIEVE_TOKEN → 下面那個檔案
PENSIEVE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmbG93X2lkIjoiNWQ1ZDY1YWQxOTM3NDQxOWIyODFlY2FiMTIxMTRjYTkiLCJhbGxvd190aW1lIjoiMjAyNi0wNi0yMiAxMTo0Njo0MiJ9.9p6CQDTpg-V3XYjgMbSd1cMacUuvVYp3_voeLhDksc0"          # ← 把 Flow Token 貼在這對引號中間

# 不想寫在程式裡的話，把 token 存成單行純文字檔放這裡，上面那格留空即可
PENSIEVE_TOKEN_FILE = os.path.join(WORK_DIR, "pensieve_token.txt")

# 第一個 node 是 user intervention，它吃的變數名稱就是 input
# 等同 RPA 裡的：$_alldoc = {"input": $_doc}
PENSIEVE_INPUT_VAR = "input"

# 文件範本寫要帶 "Answer": "QM"，但你實際在跑的 $_alldoc 沒有這個鍵。
# 這裡預設不帶（跟現況一致）；若之後 flow 多了要回答的 node，填 "QM" 就會自動帶上。
PENSIEVE_ANSWER = None

# True  → input 的值是「一段 JSON 字串」（$_doc 從剪貼簿來，本來就是字串，維持一致）
# False → input 的值是巢狀的 JSON 物件／陣列
PAYLOAD_AS_STRING = True

PENSIEVE_TIMEOUT = 180        # 秒，flow 跑比較久就往上調
PENSIEVE_RETRIES = 3          # 連線失敗 / 5xx 時的重試次數
PENSIEVE_GAP = 1.0            # 每次呼叫之間隔幾秒，避免打太快

# 憑證驗證。pensieve 用的是公司內部自簽憑證，Python 的信任清單裡沒有它的 CA，
# 所以 True 會噴 CERTIFICATE_VERIFY_FAILED / unable to get local issuer certificate。
#   False       → 不驗證（內網站台的實務做法，跟你原本那段 verify=False 一樣）
#   "C:\\...cer" → 填公司 CA 憑證檔的路徑，這是比較正規的做法
VERIFY_SSL = False

# 繞過公司 proxy。pensieve 是內網站台，走 proxy 出去再繞回來常常會失敗或逾時，
# 而 HTTP_PROXY / HTTPS_PROXY 這些環境變數是跟著 Windows 帳號走的——
# 這正是「你手動跑得動、RPA 換個帳號就不行」的另一個常見原因。
BYPASS_PROXY = True
PROXIES = {"http": None, "https": None} if BYPASS_PROXY else None

# 關掉驗證後每次呼叫都會噴 InsecureRequestWarning，把 log 洗版；這裡靜音掉。
if VERIFY_SSL is False:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# "per_doc" = 一篇一次呼叫（送一篇收一篇，直接配對，不需要靠來文字號對回去）
# "batch"   = 整包一次送（flow 若一次只處理一筆，多送的那幾篇會沒有結果）
SEND_MODE = "per_doc"

REVIEW_CACHE = os.path.join(WORK_DIR, ".review_cache.json")

# ★ 改完 flow 裡的 prompt 之後，把這個數字 +1 ★
#   prompt 住在 Pensieve flow 裡，這支程式看不到它變了。不 +1 的話，
#   同一篇公文會直接命中舊快取，你會以為 prompt 沒生效。
#   （臨時想重跑一次而不想動這裡，就加 --no-cache。）
PROMPT_VERSION = 3

# ── 使用者回饋（officialdoc.html 上的評分）──────────
#   feedback_server.py 跟這支程式放同一個資料夾，把回饋寫進 FEEDBACK_DIR\feedback.jsonl。
#   開啟後：
#     * 每篇公文送出時，會帶上主旨最相近的幾筆回饋，放在 input 的「參考回饋」欄位
#     * 快取裡的 comment 被打了低於 5 分 → 這篇會帶著那筆修正意見重新產生一次
#   flow 的 comment prompt 要有對應的說明（見 prompt/comment.txt「使用者回饋」段落），
#   而且 comment 那個 node 必須讀得到原始的 input。
USE_FEEDBACK = True
FEEDBACK_FIELD = "參考回饋"
FEEDBACK_DIR = os.path.join(BASE_DIR, "feedback")    # 必須跟 feedback_knowledge.py 的預設一致

# 每次跑完把回饋整理成案例卡，覆蓋上傳到 Pensieve 知識庫。
# 內容沒變就不重傳；上傳失敗只記警告，不影響公文審核結果。
USE_VECTOR_UPLOAD = True

# ★ 知識庫的 vector_id 存成單行文字檔 vector_id.txt ★
#   放在「officialdoc_pipeline.py 同一個資料夾」或 WORK_DIR 都可以，依序找第一個存在的。
#   不要放進 web\ 資料夾，那裡的東西別人用瀏覽器看得到。
VECTOR_ID_FILES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vector_id.txt"),
    os.path.join(WORK_DIR, "vector_id.txt"),
]

# ── STEP 4：最終輸出 ───────────────────────────────
# feedback_server.py 會把 web\officialdoc.html 和這個 JSON 當網頁提供：
#   http://10.10.28.170:8765/
OUTPUT_JSON = os.path.join(BASE_DIR, "web", "data", "officialdoc.json")

LOG_FILE = os.path.join(WORK_DIR, "pipeline.log")

# 回傳必須有的欄位；缺的會補「待確認」而不是整篇丟掉
REQUIRED_FIELDS = ("summary", "指派人員", "知會人員", "comment")


# ═══════════════════════════════════════════════════════════
# 共用工具
# ═══════════════════════════════════════════════════════════

log = logging.getLogger("pipeline")


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


def atomic_write_json(path, data):
    """先寫同目錄的暫存檔再 os.replace，避免半截檔或讀寫打架。"""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)          # 同一個磁碟上是原子操作
    log.info("已寫入 %s（%d 筆）", path, len(data) if hasattr(data, "__len__") else 1)


def wait_until_stable(path, timeout=60, quiet=2.0):
    """等檔案存在且大小連續 quiet 秒沒變，才算真的寫完。"""
    end = time.time() + timeout
    last_size, last_change = -1, time.time()

    while time.time() < end:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 0 and size == last_size:
                if time.time() - last_change >= quiet:
                    return True
            else:
                last_size, last_change = size, time.time()
        time.sleep(0.5)

    raise TimeoutError(f"等不到穩定的檔案：{path}")


def norm(s):
    """正規化比對用字串：全形轉半形、去所有空白、轉小寫。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", s).lower()


def clean_doc_text(text):
    if not text:
        return None
    cleaned = re.sub(r"[.\n]+", "。", text)
    return re.sub(r"。+", "。", cleaned).strip()


# ═══════════════════════════════════════════════════════════
# STEP 1  下載報表
# ═══════════════════════════════════════════════════════════

def step1_download():
    if not RUN_DOWNLOAD:
        log.info("STEP 1 已停用，沿用現有報表檔。")
        return

    if not os.path.exists(DOWNLOAD_SCRIPT):
        raise FileNotFoundError(f"找不到下載腳本：{DOWNLOAD_SCRIPT}")

    log.info("STEP 1 執行下載腳本：%s", DOWNLOAD_SCRIPT)

    # Windows 上子行程預設用 cp950(Big5) 印中文，這裡強制它改用 UTF-8，
    # 否則 log 會變成 "�}�ҭ���..." 這種亂碼，出事時看不到原因。
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

    result = subprocess.run(
        [sys.executable, DOWNLOAD_SCRIPT],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=DOWNLOAD_TIMEOUT, env=child_env,
    )

    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log.info("  [download] %s", line)

    # stderr 不管成功失敗都印出來——Selenium 的 traceback 都在這裡
    if result.stderr and result.stderr.strip():
        level = log.error if result.returncode != 0 else log.warning
        for line in result.stderr.strip().splitlines():
            level("  [download:err] %s", line)

    if result.returncode != 0:
        if not (result.stderr or "").strip():
            log.error("下載腳本沒有輸出 stderr，直接以 returncode=%d 結束。",
                      result.returncode)
        raise RuntimeError(f"下載腳本失敗，returncode={result.returncode}")

    wait_until_stable(REPORT_XLS)
    log.info("STEP 1 完成，報表大小 %d bytes", os.path.getsize(REPORT_XLS))


# ═══════════════════════════════════════════════════════════
# STEP 2  Excel + PDF → 公文全文
# ═══════════════════════════════════════════════════════════

def read_report(path):
    """讀報表。ASP.NET 匯出的 .xls 常常其實是 HTML 表格，所以要有 fallback。"""
    wait_until_stable(path, timeout=30)

    try:
        df = pd.read_excel(path)
        log.info("報表以 Excel 格式讀取成功，%d 列", len(df))
        return df
    except Exception as e:
        log.warning("pd.read_excel 失敗（%s），改用 read_html 解析…", type(e).__name__)

    tables = pd.read_html(path, encoding="utf-8")
    if not tables:
        raise ValueError(f"報表既不是 Excel 也不是 HTML 表格：{path}")

    df = max(tables, key=len)
    log.info("報表以 HTML 格式讀取成功，%d 列", len(df))
    return df


def load_pdf_texts(folders):
    """掃所有資料夾的 PDF，回傳 [(檔名, 內文), ...]。"""
    docs = []

    for folder in folders:
        if not os.path.isdir(folder):
            # 這裡以前是 continue，正是「RPA 抓不到卻毫無聲音」的原因
            log.error("PDF 資料夾讀不到（權限？UNC 路徑打錯？）：%s", folder)
            continue

        names = [n for n in os.listdir(folder) if n.lower().endswith(".pdf")]
        log.info("%s → %d 個 PDF", folder, len(names))

        for name in names:
            full = os.path.join(folder, name)
            try:
                with pdfplumber.open(full) as pdf:
                    text = "".join(p.extract_text() or "" for p in pdf.pages)
                docs.append((name, clean_doc_text(text)))
            except Exception as e:
                log.warning("PDF 解析失敗 %s：%s: %s", name, type(e).__name__, e)

    log.info("共讀入 %d 份 PDF 內文", len(docs))
    return docs


def digits(s):
    """只留數字。"""
    return re.sub(r"\D", "", str(s or ""))


def num_key(s):
    """數字比對用的鍵：去掉前導 0，讓 '010' 和 '10' 視為同一個。"""
    d = digits(s)
    if not d:
        return ""
    return d.lstrip("0") or "0"


def filename_doc_no(name):
    """從檔名取出字號。

    檔名格式：日期_字號_發文單位_..._
      1150901_11512300_高局_EB76027628_廠務_(Security C).pdf  →  11512300
      1150831_10_高局_E8_廠務_(Security C).pdf                →  10
    取第一個和第二個底線之間那一段。
    """
    stem = os.path.splitext(os.path.basename(str(name)))[0]
    parts = stem.split("_")
    return parts[1] if len(parts) >= 2 else ""


def match_fulltext(doc_id, subject, pdf_docs):
    """由嚴到寬依序比對，先命中先回傳。"""
    nid, nsub = norm(doc_id), norm(subject)
    key_id = num_key(doc_id)
    did = digits(doc_id)

    # 1) 檔名第 2 段（字號）與來文字號的數字相同——主要靠這條
    if key_id:
        for name, text in pdf_docs:
            if num_key(filename_doc_no(name)) == key_id:
                return text

    # 2) 完整字號出現在檔名
    if nid:
        for name, text in pdf_docs:
            if nid in norm(name):
                return text

    # 3) 字號數字出現在檔名任何位置（檔名格式不標準時的後備）
    if len(did) >= 5:
        for name, text in pdf_docs:
            if did in digits(name):
                return text

    # 4) 完整字號出現在內文
    if nid:
        for name, text in pdf_docs:
            if text and nid in norm(text):
                return text

    # 5) 字號數字出現在內文（要求長一點，避免誤中）
    if len(did) >= 6:
        for name, text in pdf_docs:
            if text and did in digits(text):
                return text

    # 6) 主旨前 12 字出現在檔名或內文
    key = nsub[:12]
    if len(key) >= 6:
        for name, text in pdf_docs:
            if key in norm(name) or (text and key in norm(text)):
                return text

    return None


def step2_build_fulltext():
    log.info("STEP 2 開始")
    df = read_report(REPORT_XLS)

    for col in ("簽核狀態", "來文字號", "主旨"):
        if col not in df.columns:
            raise KeyError(f"報表缺少欄位「{col}」，實際欄位：{list(df.columns)}")

    # 用正規化後比對，避免全形空白 / 前後空白讓筆數變 0
    target = norm(TARGET_STATUS)
    filtered = df[df["簽核狀態"].map(norm) == target]

    log.info("報表共 %d 列，狀態為「%s」的有 %d 列", len(df), TARGET_STATUS, len(filtered))
    if len(filtered) == 0:
        log.warning("沒有待簽核公文。實際出現過的簽核狀態：%s",
                    sorted(set(df["簽核狀態"].dropna().astype(str))))

    pdf_docs = load_pdf_texts(PDF_FOLDERS)

    records, matched, unmatched = [], 0, []
    for _, row in filtered.iterrows():
        doc_id = str(row["來文字號"]).strip()
        subject = str(row["主旨"]).strip()
        text = match_fulltext(doc_id, subject, pdf_docs)
        if text:
            matched += 1
        else:
            unmatched.append(doc_id)

        records.append({
            "來文字號": doc_id,
            "主旨": subject,
            "全文": text if text else "紙本公文，無法解析",
        })

    log.info("STEP 2 完成：%d 篇，其中 %d 篇找到 PDF 全文", len(records), matched)

    if unmatched:
        log.warning("配不到 PDF 的來文字號：%s", unmatched)
        log.warning("（其數字部分：%s）", [digits(d) for d in unmatched])
        sample = [name for name, _ in pdf_docs[:10]]
        log.warning("資料夾裡的 PDF 檔名（前 10 個）：%s", sample)
    atomic_write_json(FULLTEXT_JSON, records)
    return records


# ═══════════════════════════════════════════════════════════
# STEP 3  Pensieve flow_chat
# ═══════════════════════════════════════════════════════════

def get_token():
    if not PENSIEVE_EMPNO.strip():
        raise RuntimeError(
            "PENSIEVE_EMPNO 還沒填。這要填「你自己的工號」——呼叫 Pensieve API 的人。\n"
            "不要填下載腳本裡那個 D8698，那是要在環安衛網頁上查詢的承辦人工號。"
        )

    if PENSIEVE_TOKEN.strip():
        return PENSIEVE_TOKEN.strip()

    token = os.environ.get("PENSIEVE_TOKEN")
    if token:
        log.info("Flow Token 來自環境變數")
        return token.strip()

    if os.path.exists(PENSIEVE_TOKEN_FILE):
        with open(PENSIEVE_TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            log.info("Flow Token 來自 %s", PENSIEVE_TOKEN_FILE)
            return token

    raise RuntimeError(
        "找不到 Flow Token。三選一：\n"
        "  1) 直接填在設定區的 PENSIEVE_TOKEN\n"
        "  2) 設環境變數 PENSIEVE_TOKEN\n"
        f"  3) 把 token 存成單行文字檔放在 {PENSIEVE_TOKEN_FILE}\n"
        "注意：RPA 若用不同 Windows 帳號執行，你自己設的「使用者」環境變數它讀不到，"
        "要設成「系統」環境變數，或改用 1 / 3。"
    )


def build_body(token, payload):
    """組出 POST 的 JSON body。payload 是 dict 或 list（尚未序列化）。

    對應 RPA 的：
        $_alldoc = {"input": $_doc}
        variables = $_alldoc
    """
    variables = {
        PENSIEVE_INPUT_VAR: json.dumps(payload, ensure_ascii=False)
                            if PAYLOAD_AS_STRING else payload,
    }
    if PENSIEVE_ANSWER:
        variables["Answer"] = PENSIEVE_ANSWER

    return {"token": token, "empno": PENSIEVE_EMPNO, "variables": variables}


def call_pensieve(session, token, payload):
    """POST 一次，回傳解出來的文字內容。連線失敗 / 5xx 會退避重試。"""
    body = build_body(token, payload)

    last_error = None
    for attempt in range(1, PENSIEVE_RETRIES + 1):
        try:
            resp = session.post(
                PENSIEVE_URL, json=body,
                timeout=PENSIEVE_TIMEOUT, verify=VERIFY_SSL, proxies=PROXIES,
            )
        except requests.RequestException as e:
            last_error = e
            log.warning("第 %d 次連線失敗：%s: %s", attempt, type(e).__name__, e)
        else:
            # 4xx 是我們自己送錯（token 過期、變數名稱打錯），重試沒意義
            if 400 <= resp.status_code < 500:
                raise RuntimeError(
                    f"HTTP {resp.status_code}：{resp.text[:500]}\n"
                    "→ 通常是 token 失效、empno 不對，或中斷點變數名稱打錯。"
                )
            if resp.status_code >= 500:
                last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                log.warning("第 %d 次伺服器錯誤：HTTP %d", attempt, resp.status_code)
            else:
                return extract_text(resp)

        if attempt < PENSIEVE_RETRIES:
            wait = 2 ** attempt
            log.info("   %d 秒後重試…", wait)
            time.sleep(wait)

    raise RuntimeError(f"呼叫 Pensieve 失敗（已重試 {PENSIEVE_RETRIES} 次）：{last_error}")


def extract_text(resp):
    """把模型輸出從回應裡挖出來。
    不曉得 flow 回的確切欄位，所以依序找常見鍵名，找不到就整包回傳。
    確認實際格式後，可以把這裡簡化成直接取那一個欄位。
    """
    try:
        data = resp.json()
    except ValueError:
        # 不是 JSON：自己用 UTF-8 解，別讓 requests 猜錯編碼變亂碼
        return resp.content.decode("utf-8", errors="replace")

    if isinstance(data, str):
        return data

    if isinstance(data, dict):
        # 鍵名大小寫不一定（flow 回的是 "Result" 大寫 R），統一轉小寫再找
        lower = {str(k).lower(): v for k, v in data.items()}

        # 外層信封：{"isSuccess": bool, "Result": ...}
        if lower.get("issuccess") is False:
            raise RuntimeError(f"flow 回報 isSuccess=False：{lower.get('result') or data}")

        for key in ("result", "answer", "output", "response", "message",
                    "content", "text", "reply", "data"):
            if key in lower and lower[key] not in (None, ""):
                value = lower[key]
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    return json.dumps(data, ensure_ascii=False)


def parse_reviews(text):
    """把回應文字解析成 list[dict]。容忍 ```json 包裹、前後多餘說明。"""
    if not text or not text.strip():
        raise ValueError("回應是空的")

    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if not match:
            raise ValueError(f"回應裡找不到 JSON：{cleaned[:300]}")
        data = json.loads(match.group(1))

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(f"解析出來不是物件也不是陣列：{type(data).__name__}")

    return data


def normalize_review(item):
    """補齊缺漏欄位，不讓一個少掉的欄位炸掉整批。"""
    out = {}
    for field in REQUIRED_FIELDS:
        value = item.get(field)
        out[field] = str(value).strip() if value not in (None, "") else "待確認"
    return out


FAILED_REVIEW = {
    "summary": "",
    "指派人員": "待確認",
    "知會人員": "待確認",
    "comment": "⚠️ 自動審核失敗，請人工處理",
}


def load_cache():
    if not os.path.exists(REVIEW_CACHE):
        return {}
    try:
        with open(REVIEW_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("快取讀取失敗，重新建立：%s", e)
        return {}


def cache_key(record):
    raw = (f"v{PROMPT_VERSION}|{record['來文字號']}|{record['主旨']}"
           f"|{record['全文']}|{PENSIEVE_INPUT_VAR}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def merge(record, review):
    return {
        "來文字號": record["來文字號"],
        "主旨": record["主旨"],
        "summary": review["summary"],
        "指派人員": review["指派人員"],
        "知會人員": review["知會人員"],
        "comment": review["comment"],
    }


fk = None     # feedback_knowledge 模組，由 load_feedback_module() 載入


def load_feedback_module():
    """載入同資料夾的 feedback_knowledge.py，資料指到 FEEDBACK_DIR。失敗就回傳 None，回饋功能這次停用。"""
    global fk
    if fk is not None or not USE_FEEDBACK:
        return fk
    try:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
    except OSError as e:
        log.warning("回饋資料夾建立失敗，這次不使用回饋：%s（%s）", FEEDBACK_DIR, e)
        return None
    try:
        import feedback_knowledge as module
    except ImportError as e:
        log.warning("載入 feedback_knowledge.py 失敗（要跟這支程式放同一個資料夾），"
                    "這次不使用回饋：%s", e)
        return None
    module.set_knowledge_dir(FEEDBACK_DIR)
    module.VECTOR_ID_FILE = find_vector_id_file()
    fk = module
    return fk


def find_vector_id_file():
    """回傳第一個存在的 vector_id.txt；都沒有就回傳第一個候選路徑，並提示常見的檔名錯誤。"""
    candidates = list(dict.fromkeys(VECTOR_ID_FILES))      # 去重、保留順序
    for path in candidates:
        if os.path.isfile(path):
            return path

    for path in candidates:
        # Windows 預設隱藏副檔名：檔案總管顯示 vector_id.txt，實際可能是 vector_id.txt.txt
        if os.path.isfile(path + ".txt"):
            log.warning("找到 %s.txt——檔名多了一個 .txt，請改名成 vector_id.txt", path)
    if USE_VECTOR_UPLOAD:
        log.warning("找不到 vector_id.txt，找過：%s", "、".join(candidates))
    return candidates[0]


def load_feedback_index():
    """讀使用者回饋，依公文去重。讀不到只記 log，不影響審核。"""
    if load_feedback_module() is None:
        return {}
    try:
        latest = fk.latest_by_doc(fk.load_feedback())
    except OSError as e:
        log.warning("回饋檔讀取失敗，這次不帶參考回饋：%s", e)
        return {}
    low = sum(1 for r in latest.values() if r["score"] < 5)
    log.info("讀入 %d 篇公文的使用者回饋（其中低於 5 分 %d 篇）：%s",
             len(latest), low, fk.FEEDBACK_FILE)
    return latest


def doc_payload(rec, feedback):
    """送給 flow 的一篇公文。有相關回饋才加「參考回饋」欄位，沒有就跟以前完全一樣。"""
    item = {"來文字號": rec["來文字號"], "主旨": rec["主旨"], "全文": rec["全文"]}
    examples = fk.select_examples(rec, feedback) if feedback else []
    if examples:
        item[FEEDBACK_FIELD] = examples
    return item


def needs_regenerate(rec, cached, feedback):
    """快取裡這篇的 comment 被打了低於 5 分、且還沒針對那筆回饋重產過 → 回傳那筆回饋。"""
    if not feedback:
        return None
    fb = feedback.get(fk.doc_key(rec))
    if not fb or fb["score"] >= 5:
        return None
    if norm(fb.get("comment")) != norm(cached.get("comment")):
        return None      # 評的是更早的版本，現在的 comment 已經換過了
    if cached.get("_regenerated_for") == fb.get("id"):
        return None      # 已經帶這筆回饋重產過一次，不要每次跑都重打
    return fb


def review_per_doc(session, token, records, use_cache=True, feedback=None):
    feedback = feedback or {}
    cache = load_cache() if use_cache else {}
    if not use_cache:
        log.info("--no-cache：略過快取，每篇都重新呼叫 Pensieve。")
    results, hits, fails, regens = [], 0, 0, 0

    for i, rec in enumerate(records, 1):
        key = cache_key(rec)
        regen_fb = None

        if use_cache and key in cache:
            regen_fb = needs_regenerate(rec, cache[key], feedback)
            if regen_fb is None:
                hits += 1
                log.info("[%d/%d] %s → 快取命中", i, len(records), rec["來文字號"])
                results.append(merge(rec, cache[key]))
                continue
            regens += 1
            log.info("[%d/%d] %s → 目前的 comment 被評 %d 分，帶修正意見重新產生",
                     i, len(records), rec["來文字號"], regen_fb["score"])

        # 包成 1 筆的陣列，讓 flow 收到的形狀跟 batch 模式一致
        payload = [doc_payload(rec, feedback)]
        n_fb = len(payload[0].get(FEEDBACK_FIELD, []))

        try:
            raw = call_pensieve(session, token, payload)
            review = normalize_review(parse_reviews(raw)[0])
            if regen_fb:
                review["_regenerated_for"] = regen_fb.get("id")
                if norm(review["comment"]) == norm(regen_fb.get("comment")):
                    log.warning("   重新產生後 comment 沒有變——確認 flow 的 comment node "
                                "讀得到 input 裡的「%s」", FEEDBACK_FIELD)
            cache[key] = review
            log.info("[%d/%d] %s → %s%s", i, len(records), rec["來文字號"],
                     review["comment"][:30], f"（參考回饋 {n_fb} 筆）" if n_fb else "")
        except (RuntimeError, ValueError, json.JSONDecodeError, IndexError) as e:
            log.error("[%d/%d] %s → 失敗：%s: %s",
                      i, len(records), rec["來文字號"], type(e).__name__, e)
            if regen_fb:
                review = cache[key]      # 重產失敗就沿用原本的結果，下次再試
            else:
                fails += 1
                review = FAILED_REVIEW

        results.append(merge(rec, review))
        time.sleep(PENSIEVE_GAP)

    # --no-cache 時把這次的新結果併回原本的快取，不要把舊的洗掉
    if not use_cache:
        merged = load_cache()
        merged.update(cache)
        cache = merged

    atomic_write_json(REVIEW_CACHE, cache)
    log.info("STEP 3 完成：快取命中 %d、依回饋重產 %d、失敗 %d", hits, regens, fails)

    if fails == len(records) and records:
        raise RuntimeError("所有公文都審核失敗，不覆蓋線上檔案。")

    return results


def review_batch(session, token, records, feedback=None):
    """整包一次送。回來後用「來文字號」對回去，不靠順序。"""
    payload = [doc_payload(r, feedback) for r in records]
    log.info("整批送出 %d 篇（%d 字元）", len(records),
             len(json.dumps(payload, ensure_ascii=False)))

    raw = call_pensieve(session, token, payload)
    items = [it for it in parse_reviews(raw) if isinstance(it, dict)]
    log.info("回來 %d 筆", len(items))

    if items:
        log.info("回來的第一筆有這些欄位：%s", list(items[0].keys()))
        log.info("回來的來文字號：%s", [it.get("來文字號") for it in items])

    # 兩套索引：完整字串、以及只看數字（LLM 常常把字號寫成不同格式）
    by_text = {norm(it.get("來文字號")): it for it in items if it.get("來文字號")}
    by_num = {}
    for it in items:
        k = num_key(it.get("來文字號"))
        if k and k not in by_num:
            by_num[k] = it

    if not by_text and not by_num:
        log.warning("回應沒有可用的「來文字號」，只能靠順序對應。")

    results, misses = [], 0
    for i, rec in enumerate(records):
        item = by_text.get(norm(rec["來文字號"])) or by_num.get(num_key(rec["來文字號"]))

        # 字號對不上，但回來的筆數跟送出去的一樣 → 照順序對
        if item is None and len(items) == len(records):
            item = items[i]
            log.warning("%s → 字號對不上，改用順序對應第 %d 筆（回應字號：%s）",
                        rec["來文字號"], i + 1, items[i].get("來文字號"))

        if item is None:
            misses += 1
            log.error("%s → 回應裡找不到對應的來文字號", rec["來文字號"])
            results.append(merge(rec, FAILED_REVIEW))
        else:
            results.append(merge(rec, normalize_review(item)))

    if misses == len(records) and records:
        log.error("─" * 50)
        log.error("整批對不回來。收到的原始回應（前 3000 字）：")
        log.error("%s", raw[:3000] if raw else "(空的)")
        log.error("─" * 50)
        log.error("→ 若上面看不到 5 筆審核結果，代表 flow 一次只處理一筆；"
                  "把設定區的 SEND_MODE 改成 \"per_doc\" 再跑一次。")
        raise RuntimeError("整批都對不回來，不覆蓋線上檔案。")

    log.info("STEP 3 完成：對不到 %d 筆", misses)
    return results


def step3_review(records, dry_run=False, use_cache=True):
    if dry_run:
        log.info("STEP 3 跳過（--dry-run）")
        return [merge(r, dict.fromkeys(REQUIRED_FIELDS, "")) for r in records]

    log.info("STEP 3 開始（%s 模式，PROMPT_VERSION=%d），共 %d 篇",
             SEND_MODE, PROMPT_VERSION, len(records))
    token = get_token()
    feedback = load_feedback_index()

    with requests.Session() as session:
        session.headers.update({"Content-Type": "application/json"})
        if SEND_MODE == "batch":
            # batch 模式不使用快取
            return review_batch(session, token, records, feedback=feedback)
        return review_per_doc(session, token, records, use_cache=use_cache,
                              feedback=feedback)


def probe(records, raw_file=None):
    """送一筆並把送出去的 body 和收回來的原始回應都印出來。

    raw_file 指定時，改送該檔案的「原始文字內容」，完全不做 json.dumps。
    用途：把你在 Pensieve 網頁上貼進 user intervention node 的內容，
    原封不動存成 txt，用這個送一次。如果 UI 的結果對、API 的結果不對，
    這一步可以判斷問題是出在「送進去的內容不一樣」還是「flow 版本不一樣」。
      * 送一樣的內容，結果就對了 → 問題在輸入格式（看 PAYLOAD_AS_STRING）
      * 送一樣的內容，結果還是不對 → 問題在 flow 那端（多半是草稿沒發布）
    """
    token = get_token()

    if raw_file:
        with open(raw_file, "r", encoding="utf-8") as f:
            raw_text = f.read()
        log.info("送出 %s 的原始內容（%d 字元，不做 json.dumps）", raw_file, len(raw_text))
        body = {
            "token": token,
            "empno": PENSIEVE_EMPNO,
            "variables": {PENSIEVE_INPUT_VAR: raw_text},
        }
        if PENSIEVE_ANSWER:
            body["variables"]["Answer"] = PENSIEVE_ANSWER
    else:
        if not records:
            log.error("沒有公文可以測。")
            return 1
        body = build_body(token, [doc_payload(records[0], load_feedback_index())])

    print("─" * 60)
    print("送出的 body（token 已遮蔽）：")
    print(json.dumps({**body, "token": "***"}, ensure_ascii=False, indent=2)[:2000])

    resp = requests.post(PENSIEVE_URL, json=body, timeout=PENSIEVE_TIMEOUT,
                         verify=VERIFY_SSL, proxies=PROXIES)

    print("─" * 60)
    print("HTTP", resp.status_code)
    print("Content-Type:", resp.headers.get("Content-Type"))
    print("─" * 60)
    print(resp.content.decode("utf-8", errors="replace")[:4000])
    print("─" * 60)
    return 0


# ═══════════════════════════════════════════════════════════
# STEP 4 / 5
# ═══════════════════════════════════════════════════════════

def step4_publish(results):
    log.info("STEP 4 寫入 %s", OUTPUT_JSON)
    folder = os.path.dirname(OUTPUT_JSON)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"輸出目錄讀不到（網路磁碟權限？）：{folder}")
    atomic_write_json(OUTPUT_JSON, results)


def step5_send_mail(results):
    # TODO: 補上寄信。要先確認是走 Outlook COM、Lotus Notes 還是 SMTP。
    log.info("STEP 5 尚未實作，跳過（%d 筆待通知）", len(results))


# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true", help="不跑 Selenium，用現有報表檔")
    parser.add_argument("--dry-run", action="store_true", help="不呼叫 API，只驗證抓檔與比對")
    parser.add_argument("--probe", action="store_true", help="只送一篇並印出原始回應")
    parser.add_argument("--no-cache", action="store_true",
                        help="略過審核快取，每篇都重新呼叫（改完 flow 的 prompt 後用）")
    parser.add_argument("--probe-file", metavar="TXT",
                        help="送出這個檔案的原始文字（不做 json.dumps），"
                             "用來比對「網頁上貼的內容」與「API 送的內容」")
    parser.add_argument("--upload-knowledge", action="store_true",
                        help="只把回饋案例上傳到 Pensieve 知識庫（不管內容有沒有變都重傳），其他步驟都不跑")
    args = parser.parse_args()

    setup_logging()
    started = datetime.now()
    log.info("=" * 60)
    log.info("pipeline 開始 %s", started.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        # --probe-file 完全不需要報表與 PDF，直接送檔案內容
        if args.probe_file:
            return probe(None, raw_file=args.probe_file)

        if args.upload_knowledge:
            if load_feedback_module() is None:
                return 1
            result = fk.upload_cases(force=True, log=log.info)
            return 0 if result == "uploaded" else 1

        if not args.skip_download and not args.probe:
            step1_download()

        records = step2_build_fulltext()
        if not records:
            log.warning("沒有待簽核公文，不覆蓋線上檔案，正常結束。")
            return 0

        if args.probe:
            return probe(records)

        results = step3_review(records, dry_run=args.dry_run,
                               use_cache=not args.no_cache)

        if args.dry_run:
            log.info("--dry-run：不寫入線上檔案。")
            return 0

        step4_publish(results)
        step5_send_mail(results)

        # 順手更新回饋彙整檔、同步知識庫；失敗不影響主流程
        if load_feedback_module() is not None:
            try:
                log.info("回饋彙整已更新：%s", fk.write_digest())
            except OSError as e:
                log.warning("回饋彙整產生失敗：%s", e)
            if USE_VECTOR_UPLOAD:
                try:
                    fk.upload_cases(log=log.info)
                except (RuntimeError, OSError) as e:
                    log.warning("%s（下次執行會再試）", e)

        log.info("全部完成，耗時 %s", datetime.now() - started)
        return 0

    except Exception as e:
        log.exception("pipeline 失敗：%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
