# -*- coding: utf-8 -*-
"""
公文 comment 回饋 → LLM 的知識整理

feedback_server.py 把使用者在 officialdoc.html 上的評分寫進 feedback.jsonl，
這支負責把那些回饋「整理成 LLM 吃得下的形狀」，有兩條路：

  1) 自動：officialdoc_pipeline.py 每次呼叫 Pensieve 前，用 select_examples()
     挑出「主旨最像」的幾筆回饋，塞進 input 的「參考回饋」欄位一起送出。
     不用動 flow、不用上傳檔案，下一次跑就生效。

  2) 人工：write_digest() 把所有回饋彙整成 feedback_digest.md。
     同一種錯一直出現時，代表 comment.txt 的規則本身要改——
     照著彙整檔改 prompt，再把 pipeline 的 PROMPT_VERSION +1。

指令：
    python feedback_knowledge.py digest              # 產生 feedback_digest.md
    python feedback_knowledge.py preview "主旨文字"   # 看某個主旨會帶哪幾筆回饋
"""

import json
import os
import re
import sys
import unicodedata
from datetime import datetime


# ═══════════════════════════════════════════════════════════
# 設定區
# ═══════════════════════════════════════════════════════════

KNOWLEDGE_DIR = r"D:\ASEKH\K20076\2026\公文簽核agent\knowledge"
FEEDBACK_FILE = os.path.join(KNOWLEDGE_DIR, "feedback.jsonl")
DIGEST_FILE = os.path.join(KNOWLEDGE_DIR, "feedback_digest.md")

# 每篇公文最多帶幾筆參考回饋。帶太多會把 prompt 撐大，也容易讓 LLM 混淆
MAX_EXAMPLES = 3

# 主旨相似度門檻（0~1，字元 bigram 的 Dice 係數）。
# 同一篇公文（來文字號相同）一律視為 1.0。
MIN_SIMILARITY = 0.3

# 5 分的回饋也拿來當「這樣做是對的」示範；False 則只帶低分的修正
INCLUDE_FIVE_STAR = True

# 來文字號是這些值時視為沒有字號，改用主旨當作識別
_EMPTY_DOC_NO = {"", "nan", "none", "—", "-", "無"}


# ═══════════════════════════════════════════════════════════

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
# 彙整檔（給人看，用來改 prompt）
# ═══════════════════════════════════════════════════════════

def _md(text):
    """單行化，避免 comment 裡的換行把 markdown 版面弄亂。"""
    return str(text or "").replace("\r", "").replace("\n", " / ").strip() or "（無）"


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
    """產生彙整檔，回傳寫入的路徑。先寫暫存檔再 os.replace，避免半截檔。"""
    digest_file = digest_file or DIGEST_FILE
    content = build_digest(load_feedback(feedback_file))
    folder = os.path.dirname(digest_file)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(digest_file)}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, digest_file)
    return digest_file


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

    args = parser.parse_args(argv)

    if args.cmd == "digest":
        path = write_digest(args.file, args.out)
        print(f"已寫入 {path}")
        return 0

    latest = latest_by_doc(load_feedback(args.file))
    examples = select_examples({"來文字號": args.doc_no, "主旨": args.subject}, latest)
    print(f"共 {len(latest)} 篇有回饋，這個主旨會帶 {len(examples)} 筆：")
    print(json.dumps(examples, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
