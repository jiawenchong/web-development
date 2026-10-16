# -*- coding: utf-8 -*-
"""
公文 comment 回饋 → LLM 的知識整理

feedback_server.py 把使用者在 officialdoc.html 上的評分寫進 feedback.jsonl，
這支負責把那些回饋「整理成 LLM 吃得下的形狀」，有三條路：

  1) prompt：officialdoc_pipeline.py 每次呼叫 Pensieve 前，用 select_examples()
     挑出「主旨最像」的幾筆回饋，塞進 input 的「參考回饋」欄位一起送出。
     一定送得到 LLM 手上，是主要的那一層。

  2) 知識庫（RAG）：write_cases() 把每篇公文最新的回饋寫成一則一則的「案例卡」，
     upload_cases() 再用 /api/uploadVector/ 上傳覆蓋到 Pensieve 知識庫。
     內容沒變就不重傳。這是第二層保險。

  3) 人工：write_digest() 產生 feedback_digest.md。
     同一種錯一直出現時，代表 comment.txt 的規則本身要改——
     照著彙整檔改 prompt，再把 pipeline 的 PROMPT_VERSION +1。

指令：
    python feedback_knowledge.py digest              # 產生 feedback_digest.md
    python feedback_knowledge.py preview "主旨文字"   # 看某個主旨會帶哪幾筆回饋
    python feedback_knowledge.py cases               # 只產生案例卡檔案，不上傳
    python feedback_knowledge.py upload              # 產生案例卡並上傳知識庫（內容沒變就跳過）
    python feedback_knowledge.py upload --force      # 不管有沒有變都重傳
"""

import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime


# ═══════════════════════════════════════════════════════════
# 設定區
# ═══════════════════════════════════════════════════════════

# 回饋資料預設放在「這支程式所在的資料夾」。
#   feedback_server.py 與 officialdoc_pipeline.py 都放在 10.10.28.170 的 BASE_DIR，
#   所以兩邊自動指到同一個資料夾（pipeline 也會再用 set_knowledge_dir() 指定一次）。
# 底下所有檔案路徑都由 set_knowledge_dir() 統一算出來（檔案最下面會先套用一次預設值）。
KNOWLEDGE_DIR = os.path.dirname(os.path.abspath(__file__))

# 每篇公文最多帶幾筆參考回饋。帶太多會把 prompt 撐大，也容易讓 LLM 混淆
MAX_EXAMPLES = 3

# 主旨相似度門檻（0~1，字元 bigram 的 Dice 係數）。
# 同一篇公文（來文字號相同）一律視為 1.0。
MIN_SIMILARITY = 0.3

# 5 分的回饋也拿來當「這樣做是對的」示範；False 則只帶低分的修正
INCLUDE_FIVE_STAR = True

# 來文字號是這些值時視為沒有字號，改用主旨當作識別
_EMPTY_DOC_NO = {"", "nan", "none", "—", "-", "無"}

# ── Pensieve 知識庫（RAG）──────────────────────────
VECTOR_UPLOAD_URL = "https://pensieve.kh.asegroup.com/api/uploadVector/"

# ★ 知識庫的 vector_id ★
#   三個來源，由上到下找第一個有值的：這個變數 → 環境變數 PENSIEVE_VECTOR_ID → VECTOR_ID_FILE
#   這個 id 等於知識庫的寫入權限，不要 commit 進 git。
#   pipeline 會把 VECTOR_ID_FILE 指到它自己的資料夾（BASE_DIR\vector_id.txt）。
VECTOR_ID = ""

# 覆蓋 / 附加如果需要額外參數（例如 {"mode": "replace"}），填在這裡會一起送出
VECTOR_UPLOAD_EXTRA_DATA = {}

VECTOR_UPLOAD_TIMEOUT = 180

# 跟 officialdoc_pipeline.py 一樣：內部自簽憑證、內網不走 proxy
VERIFY_SSL = False
PROXIES = {"http": None, "https": None}


# ═══════════════════════════════════════════════════════════

def set_knowledge_dir(path):
    """把回饋相關檔案都指到 path 底下。"""
    global KNOWLEDGE_DIR, FEEDBACK_FILE, DIGEST_FILE, CASES_FILE, UPLOAD_STATE_FILE
    KNOWLEDGE_DIR = path
    FEEDBACK_FILE = os.path.join(path, "feedback.jsonl")
    DIGEST_FILE = os.path.join(path, "feedback_digest.md")
    # 上傳的檔名固定不變：每次上傳同一個檔名＝覆蓋舊的那份（知識庫設定成 replace），
    # 知識庫裡永遠只有「最新一版」的案例，不會累積過時或互相矛盾的內容。
    CASES_FILE = os.path.join(path, "officialdoc_feedback_cases.txt")
    UPLOAD_STATE_FILE = os.path.join(path, ".vector_upload_state.json")


set_knowledge_dir(KNOWLEDGE_DIR)

# 存 vector_id 的單行文字檔。None = 用 KNOWLEDGE_DIR\vector_id.txt；
# pipeline 會把它設成你電腦 WORK_DIR 底下的檔案。
VECTOR_ID_FILE = None


def vector_id_file():
    return VECTOR_ID_FILE or os.path.join(KNOWLEDGE_DIR, "vector_id.txt")


def norm(s):
    """全形轉半形、去空白、轉小寫。跟 pipeline 的 norm 一樣。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", s).lower()


def doc_key(item):
    """一篇公文的識別。HTML 端的 docKey() 規則必須跟這裡一致。"""
    doc_no = str(item.get("來文字號") or "").strip()
    if doc_no.lower() not in _EMPTY_DOC_NO:
        return doc_no
    return str(item.get("主旨") or "").strip()


def atomic_write_text(path, content):
    """先寫暫存檔再 os.replace，避免半截檔。"""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    return path


def load_feedback(path=None):
    """讀 feedback.jsonl。壞掉的行跳過，不讓一行爛資料拖垮整個 pipeline。"""
    path = path or FEEDBACK_FILE
    if not os.path.exists(path):
        return []

    records = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"[feedback] 第 {lineno} 行不是合法 JSON，略過", file=sys.stderr)
                continue
            if isinstance(rec, dict) and isinstance(rec.get("score"), int):
                records.append(rec)
    return records


def latest_by_doc(records):
    """同一篇公文只留最新一筆（使用者改過評分，以最後一次為準）。

    feedback.jsonl 是依送出順序一行一行附加的，所以檔案裡越後面就越新。
    回傳的 dict 也照「最後一次被評分」的先後排序（最舊在前）。
    """
    latest = {}
    for rec in records:
        key = doc_key(rec)
        if not key:
            continue
        latest.pop(key, None)
        latest[key] = rec
    return latest


def _bigrams(s):
    s = norm(s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a, b):
    """字元 bigram 的 Dice 係數。中文主旨不用斷詞也能用。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return 2 * len(x & y) / (len(x) + len(y))


# ═══════════════════════════════════════════════════════════
# 1) prompt：參考回饋
# ═══════════════════════════════════════════════════════════

def to_example(fb):
    """把一筆回饋轉成送給 LLM 的形狀，空欄位不帶。"""
    example = {
        "來文字號": fb.get("來文字號", ""),
        "主旨": fb.get("主旨", ""),
        "當時的指派人員": fb.get("指派人員", ""),
        "當時的知會人員": fb.get("知會人員", ""),
        "當時的comment": fb.get("comment", ""),
        "分數": fb["score"],
        "原因": fb.get("reason", ""),
        "建議的comment": fb.get("suggested_comment", ""),
    }
    return {k: v for k, v in example.items() if v not in (None, "")}


def select_examples(record, latest, max_examples=MAX_EXAMPLES,
                    min_similarity=MIN_SIMILARITY):
    """挑出跟這篇公文最相關的回饋。

    record : pipeline 裡的一篇公文（至少有 來文字號 / 主旨）
    latest : latest_by_doc() 的結果
    排序：相似度高的優先；相似度一樣時低分優先（低分的修正資訊量比較大）。
    """
    if not latest:
        return []

    own_key = doc_key(record)
    scored = []
    for key, fb in latest.items():
        if fb["score"] >= 5 and not INCLUDE_FIVE_STAR:
            continue
        sim = 1.0 if key == own_key else similarity(record.get("主旨"), fb.get("主旨"))
        if sim >= min_similarity:
            scored.append((sim, fb))

    scored.sort(key=lambda t: (-t[0], t[1]["score"]))
    return [to_example(fb) for _, fb in scored[:max_examples]]


# ═══════════════════════════════════════════════════════════
# 2) 知識庫：案例卡
# ═══════════════════════════════════════════════════════════

def _line(text):
    """單行化，避免換行把案例卡切散。"""
    return re.sub(r"\s*\n\s*", " / ", str(text or "").replace("\r", "")).strip()


def case_card(fb):
    """一篇公文的回饋 → 一則獨立、自己就看得懂的案例。

    RAG 會把檔案切成小段、只撈「跟這次公文最像」的幾段給 LLM，
    所以每則案例都要自己帶齊主旨、摘要、錯在哪、正解，不能依賴前後文。
    主旨與摘要放最前面，讓檢索時容易被相似公文命中。
    """
    score = fb["score"]
    lines = [
        f"【公文回饋案例】{_line(fb.get('來文字號')) or '無字號'}",
        f"主旨：{_line(fb.get('主旨'))}",
    ]
    if fb.get("summary"):
        lines.append(f"摘要：{_line(fb.get('summary'))}")

    if score >= 5:
        lines += [
            "使用者評分：5/5（正確示範）",
            f"指派人員：{_line(fb.get('指派人員'))}",
            f"知會人員：{_line(fb.get('知會人員'))}",
            f"comment：{_line(fb.get('comment'))}",
            "處理原則：主旨與內容相似的公文，指派人員、知會人員與 comment 比照辦理。",
        ]
    else:
        lines += [
            f"使用者評分：{score}/5（錯誤案例，請勿照抄當時的結果）",
            f"當時的指派人員：{_line(fb.get('指派人員'))}",
            f"當時的知會人員：{_line(fb.get('知會人員'))}",
            f"當時的comment：{_line(fb.get('comment'))}",
            f"錯誤原因：{_line(fb.get('reason'))}",
        ]
        if fb.get("suggested_comment"):
            lines.append(f"使用者要的comment：{_line(fb.get('suggested_comment'))}")
        lines.append("處理原則：主旨與內容相似的公文，依「錯誤原因」修正指派人員、知會人員與 comment。")

    return "\n".join(lines)


def build_cases(records):
    """整份知識庫文件。每篇公文只留最新評分，新的在前。"""
    latest = latest_by_doc(records)
    cards = [case_card(fb) for fb in reversed(latest.values())]
    header = (
        "公文指派與 comment 的使用者回饋案例集。\n"
        "每則【公文回饋案例】是使用者對某篇公文的指派結果與 comment 的評分："
        "5/5 是正確示範；低於 5 分是錯誤案例，以「錯誤原因」與「使用者要的comment」為準。"
    )
    return header + "\n\n" + "\n\n".join(cards) + "\n"


def write_cases(feedback_file=None, cases_file=None):
    """產生案例卡檔案。沒有任何回饋時回傳 None。"""
    records = load_feedback(feedback_file)
    if not latest_by_doc(records):
        return None
    return atomic_write_text(cases_file or CASES_FILE, build_cases(records))


def get_vector_id():
    if VECTOR_ID.strip():
        return VECTOR_ID.strip()
    env = os.environ.get("PENSIEVE_VECTOR_ID", "").strip()
    if env:
        return env
    path = vector_id_file()
    if os.path.exists(path):
        # utf-8-sig：記事本存成「UTF-8 (BOM)」時開頭會多一個看不見的字元，這樣讀會自動去掉
        with open(path, "r", encoding="utf-8-sig") as f:
            value = f.read().strip()
        if value:
            return value
    return ""


def _load_state():
    try:
        with open(UPLOAD_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_upload_response(resp, filename):
    """回傳 (成功與否, 說明)。"""
    try:
        data = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code}，回應不是 JSON：{resp.text[:300]}"

    if not isinstance(data, dict):
        return False, f"看不懂的回應：{str(data)[:300]}"

    lower = {str(k).lower(): v for k, v in data.items()}
    if not lower.get("issuccess"):
        return False, f"isSuccess=False：{lower.get('result') or data}"

    success = [str(x) for x in (lower.get("successfile") or [])]
    fail = [str(x) for x in (lower.get("failfile") or [])]
    if any(filename in x for x in fail):
        return False, f"Embedding 失敗（FailFile）：{fail}"
    if success and not any(filename in x for x in success):
        return False, f"SuccessFile 裡沒有 {filename}：{success}"
    return True, f"SuccessFile={success}"


def upload_cases(force=False, dry_run=False, log=print):
    """產生案例卡並上傳 Pensieve 知識庫。

    回傳 "uploaded" / "unchanged" / "skipped"；上傳失敗丟 RuntimeError。
    內容跟上次成功上傳的一模一樣就不重傳，避免每次排程都重新 embedding。
    """
    path = write_cases()
    if path is None:
        log("知識庫：還沒有任何回饋，不上傳")
        return "skipped"

    vector_id = get_vector_id()
    if not vector_id:
        log(f"知識庫：沒有設定 vector_id（VECTOR_ID / 環境變數 PENSIEVE_VECTOR_ID / {vector_id_file()}），不上傳")
        return "skipped"

    with open(path, "rb") as f:
        content = f.read()
    digest = hashlib.sha256(content + vector_id.encode("utf-8")).hexdigest()

    state = _load_state()
    if not force and state.get("sha256") == digest:
        log("知識庫：案例內容沒變，不重傳")
        return "unchanged"

    filename = os.path.basename(path)
    if dry_run:
        log(f"知識庫（dry-run）：會上傳 {filename}（{len(content)} bytes）到 {VECTOR_UPLOAD_URL}")
        return "skipped"

    import requests            # 只有上傳時才需要；feedback_server.py 不用裝 requests
    if VERIFY_SSL is False:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    data = {"vector_id": vector_id, **VECTOR_UPLOAD_EXTRA_DATA}
    files = {"file": (filename, content, "text/plain; charset=utf-8")}
    try:
        resp = requests.post(VECTOR_UPLOAD_URL, data=data, files=files,
                             timeout=VECTOR_UPLOAD_TIMEOUT, verify=VERIFY_SSL, proxies=PROXIES)
    except requests.RequestException as e:
        raise RuntimeError(f"知識庫上傳連線失敗：{type(e).__name__}: {e}") from e

    ok, detail = _parse_upload_response(resp, filename)
    if not ok:
        raise RuntimeError(f"知識庫上傳失敗：{detail}")

    atomic_write_text(UPLOAD_STATE_FILE, json.dumps(
        {"sha256": digest, "file": filename, "uploaded_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
        ensure_ascii=False, indent=2))
    log(f"知識庫：已上傳 {filename}，{detail}")
    return "uploaded"


# ═══════════════════════════════════════════════════════════
# 3) 彙整檔（給人看，用來改 prompt）
# ═══════════════════════════════════════════════════════════

def _md(text):
    """單行化，避免 comment 裡的換行把 markdown 版面弄亂。"""
    return _line(text) or "（無）"


def build_digest(records):
    latest = latest_by_doc(records)
    items = list(reversed(latest.values()))      # 新到舊

    lines = [
        "# 公文 comment 回饋彙整",
        "",
        f"產生時間：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
    ]

    if not items:
        lines.append("目前還沒有任何回饋。")
        return "\n".join(lines) + "\n"

    dist = {n: 0 for n in range(1, 6)}
    for r in items:
        dist[r["score"]] = dist.get(r["score"], 0) + 1
    avg = sum(r["score"] for r in items) / len(items)

    lines += [
        f"- 原始回饋 {len(records)} 筆，依公文去重後 {len(items)} 篇",
        f"- 平均分數 {avg:.2f}",
        "- 分數分布：" + "、".join(f"{n} 分 × {dist[n]}" for n in range(5, 0, -1)),
        "",
        "> 怎麼用：同一種「原因」反覆出現 → 代表 prompt/comment.txt 的規則要改。",
        "> 改完 flow 裡的 prompt 後，把 officialdoc_pipeline.py 的 PROMPT_VERSION +1。",
        "> 個別、零星的修正不用改規則，pipeline 會自動把相似的回饋帶給 LLM 參考。",
        "",
    ]

    low = [r for r in items if r["score"] < 5]
    lines += [f"## 低於 5 分（{len(low)} 篇，新到舊）", ""]
    if not low:
        lines += ["（沒有）", ""]
    for r in low:
        lines += [
            f"### {r['score']} 分｜{_md(r.get('來文字號'))}｜{_md(r.get('主旨'))}",
            "",
            f"- 當時指派：{_md(r.get('指派人員'))}",
            f"- 當時知會：{_md(r.get('知會人員'))}",
            f"- 當時 comment：{_md(r.get('comment'))}",
            f"- **原因：{_md(r.get('reason'))}**",
            f"- 建議 comment：{_md(r.get('suggested_comment'))}",
            "",
        ]

    good = [r for r in items if r["score"] >= 5]
    lines += [f"## 5 分（{len(good)} 篇）", ""]
    for r in good:
        lines.append(f"- {_md(r.get('來文字號'))}｜{_md(r.get('主旨'))} → {_md(r.get('comment'))}")
    lines.append("")

    return "\n".join(lines)


def write_digest(feedback_file=None, digest_file=None):
    """產生彙整檔，回傳寫入的路徑。"""
    return atomic_write_text(digest_file or DIGEST_FILE, build_digest(load_feedback(feedback_file)))


# ═══════════════════════════════════════════════════════════

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="公文 comment 回饋整理")
    parser.add_argument("--file", default=None, help="feedback.jsonl 的路徑")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_digest = sub.add_parser("digest", help="產生 feedback_digest.md")
    p_digest.add_argument("--out", default=None)

    p_preview = sub.add_parser("preview", help="看某個主旨會帶哪幾筆參考回饋")
    p_preview.add_argument("subject", help="公文主旨")
    p_preview.add_argument("--doc-no", default="", help="來文字號（選填）")

    sub.add_parser("cases", help="只產生知識庫案例卡檔案，不上傳")

    p_upload = sub.add_parser("upload", help="產生案例卡並上傳 Pensieve 知識庫")
    p_upload.add_argument("--force", action="store_true", help="內容沒變也重傳")
    p_upload.add_argument("--dry-run", action="store_true", help="不真的上傳")

    args = parser.parse_args(argv)

    if args.cmd == "digest":
        print(f"已寫入 {write_digest(args.file, args.out)}")
        return 0

    if args.cmd == "cases":
        path = write_cases(args.file)
        print(f"已寫入 {path}" if path else "還沒有任何回饋")
        return 0

    if args.cmd == "upload":
        try:
            upload_cases(force=args.force, dry_run=args.dry_run)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1
        return 0

    latest = latest_by_doc(load_feedback(args.file))
    examples = select_examples({"來文字號": args.doc_no, "主旨": args.subject}, latest)
    print(f"共 {len(latest)} 篇有回饋，這個主旨會帶 {len(examples)} 筆：")
    print(json.dumps(examples, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
