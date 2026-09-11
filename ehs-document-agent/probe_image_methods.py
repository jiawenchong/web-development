# -*- coding: utf-8 -*-
"""
probe_image_methods.py — 測試「怎麼把圖片/掃描檔送進 Pensieve flow_chat」

背景
----
officialdoc_pipeline.py 目前只有送純文字（PDF 用 pdfplumber 抽字）。
如果來源 PDF 是「掃描檔」（整頁都是圖，沒有文字層），pdfplumber.extract_text()
會拿到空字串，程式就把它標成「紙本公文，無法解析」。

但你說 Pensieve 平台本身「讀得懂圖」，而官方文件只講純文字的
POST /api/flow_chat/（token / empno / variables），沒有講怎麼傳檔案 —
不知道它要的是 base64、multipart、還是先上傳拿 URL 再回傳參照。

這支腳本不猜一種就送正式資料，而是把 10 種常見的「圖片/檔案傳遞方式」
都包成獨立函式，一種一種送同一份測試資料過去，把每一種的 HTTP 狀態碼
和回應內容印出來，讓你自己比對哪一種 Pensieve 真的吃得下去、有讀到圖。

⚠️ 重要：這支程式必須在「連得到你們內網 pensieve.kh.asegroup.com、
也讀得到 U:\\ 網路磁碟」的那台機器上執行（例如你平常手動測試 / RPA 在跑的
那台 Windows）。我（Claude）目前這個對話 session 跑在 Anthropic 的雲端
沙箱容器裡，兩者都連不到，所以沒辦法在這裡幫你實際打這支 API 或讀那份
PDF 來驗證——這支腳本是寫給你在你自己機器上跑、自己看結果用的。

安裝需求（你自己的機器上）：
    pip install requests PyMuPDF

用法：
    # 列出所有方法
    python probe_image_methods.py --list

    # 全部 10 種都送一次（每種送完會停頓幾秒），把結果存成報告
    python probe_image_methods.py --pdf "U:\\0.廠務環工公用資料夾\\23.UTI-各項資料回覆\\● 公文掃瞄\\2026\\1150728_1158090772_台灣電力股份有限公司高雄區營業處_K3B_(Security C).pdf"

    # 只想看某幾種（先看 body 長什麼樣，不要真的送出去）
    python probe_image_methods.py --pdf "...pdf" --only 1,3,7 --dry-run

    # 只想看某幾種，且真的送出去
    python probe_image_methods.py --pdf "...pdf" --only 5,9

參數：
    --pdf PATH      測試用的掃描 PDF（預設就是你給的那份 K3B 公文）
    --empno         呼叫 Pensieve 的工號（預設用 pipeline 裡的 K20076）
    --token         Flow Token（預設讀 pipeline.py 裡那個，或環境變數 PENSIEVE_TOKEN）
    --only 1,3,7    只測這幾號方法（逗號分隔），預設全測
    --dry-run       只印出要送出去的 body 摘要，不真的呼叫 API
    --max-pages N   掃描 PDF 只取前 N 頁轉圖（預設 1，避免 payload 太肥）
    --dpi N         轉圖解析度（預設 150，掃描公文通常夠用又不會太大）
    --gap N         每個方法之間停頓幾秒（預設 3，避免把 flow 打爆）

怎麼判讀結果：
    * HTTP 4xx              → 這個方法的欄位名稱 / 格式 Pensieve 直接拒收
    * HTTP 200 但回應像是「沒收到附件、只當文字處理」→ 平台把 base64 當亂碼文字
    * HTTP 200 且 flow 回應內容真的提到圖片內容（例如複誦了公文上的文字/圖章）
      → 這個方法可行

如果 10 種全部都不行：
    最可靠的做法是「用瀏覽器開 Pensieve 網頁版聊天室，手動上傳一張圖進同一個
    flow，同時打開瀏覽器 DevTools 的 Network 分頁」，把那次上傳的真實 request
    （URL、method、Content-Type、body 長相）截圖或存下來給我看——那是唯一
    100% 準確的「官方到底怎麼收檔案」的答案，比我在這裡猜 10 種要準。
"""

import argparse
import base64
import json
import os
import sys
import time

import requests

# 沿用 officialdoc_pipeline.py 的設定，兩支腳本放在同一個資料夾
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import officialdoc_pipeline as base
except ImportError:
    base = None


# ═══════════════════════════════════════════════════════════
# 設定（找不到 officialdoc_pipeline.py 時的備援預設值）
# ═══════════════════════════════════════════════════════════

PENSIEVE_URL = getattr(base, "PENSIEVE_URL", "https://pensieve.kh.asegroup.com/api/flow_chat/")
DEFAULT_EMPNO = getattr(base, "PENSIEVE_EMPNO", "K20076")
VERIFY_SSL = getattr(base, "VERIFY_SSL", False)
PROXIES = getattr(base, "PROXIES", {"http": None, "https": None})
DEFAULT_PDF = (
    r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\2026"
    r"\1150728_1158090772_台灣電力股份有限公司高雄區營業處_K3B_(Security C).pdf"
)

if VERIFY_SSL is False:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_token(cli_token):
    if cli_token:
        return cli_token.strip()
    if base is not None:
        try:
            return base.get_token()
        except Exception:
            pass
    token = os.environ.get("PENSIEVE_TOKEN")
    if token:
        return token.strip()
    raise RuntimeError(
        "找不到 Flow Token。用 --token 直接帶入，或設環境變數 PENSIEVE_TOKEN，"
        "或確定 officialdoc_pipeline.py 就在同一個資料夾且已填好 PENSIEVE_TOKEN。"
    )


# ═══════════════════════════════════════════════════════════
# 把掃描 PDF 轉成圖片 / base64
# ═══════════════════════════════════════════════════════════

def render_pdf_pages(pdf_path, max_pages=1, dpi=150):
    """用 PyMuPDF 把 PDF 前 N 頁轉成 PNG bytes 的 list。

    掃描公文本來就是「整頁一張圖」，pdfplumber 抽不到文字很正常；
    這裡改用 fitz 直接把整頁 render 成點陣圖，才是「圖」的來源。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "沒有安裝 PyMuPDF。請先在這台機器上執行：pip install PyMuPDF"
        )

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(
            f"讀不到 PDF：{pdf_path}\n"
            "（確認這台機器有掛載 U:\\，且路徑、檔名逐字正確——含那個特殊符號「●」）"
        )

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pages = []

    with fitz.open(pdf_path) as doc:
        n = min(max_pages, doc.page_count)
        for i in range(n):
            pix = doc[i].get_pixmap(matrix=matrix)
            pages.append(pix.tobytes("png"))

    return pages


def load_whole_pdf_bytes(pdf_path):
    with open(pdf_path, "rb") as f:
        return f.read()


def b64(data):
    return base64.b64encode(data).decode("ascii")


# ═══════════════════════════════════════════════════════════
# 10 種測試方法
#
# 每個方法回傳 (kind, kwargs)：
#   kind == "json"      → requests.post(url, json=body, ...)
#   kind == "multipart" → requests.post(url, data=form, files=files, ...)
# ═══════════════════════════════════════════════════════════

def method_1_raw_base64_string(ctx):
    """方法 1：input 直接放整份 PDF 的 base64（不加任何包裝）。"""
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": ctx["pdf_b64"]},
    }
    return "json", {"json": body}


def method_2_data_uri_pdf(ctx):
    """方法 2：input 放 data URI 格式（data:application/pdf;base64,...）。"""
    uri = f"data:application/pdf;base64,{ctx['pdf_b64']}"
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": uri},
    }
    return "json", {"json": body}


def method_3_data_uri_png_page(ctx):
    """方法 3：input 放「第一頁轉成 PNG」的 data URI（掃描公文本來就是整頁圖）。"""
    uri = f"data:image/png;base64,{ctx['page_b64_list'][0]}"
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": uri},
    }
    return "json", {"json": body}


def method_4_structured_json_string(ctx):
    """方法 4：input 是一段 JSON 字串，裡面帶 filename / mime_type / content_base64
    這個結構模仿常見的「檔案物件」慣例，跟 pipeline.py 對文字資料
    做 json.dumps 再放進 input 的邏輯一致（PAYLOAD_AS_STRING=True）。
    """
    payload = {
        "來文字號": ctx["doc_id"],
        "主旨": ctx["subject"],
        "filename": ctx["filename"],
        "mime_type": "application/pdf",
        "content_base64": ctx["pdf_b64"],
    }
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": json.dumps(payload, ensure_ascii=False)},
    }
    return "json", {"json": body}


def method_5_page_images_array(ctx):
    """方法 5：input 是「每頁一張圖」的 JSON 陣列字串。"""
    payload = [
        {"page": i + 1, "image_base64": p}
        for i, p in enumerate(ctx["page_b64_list"])
    ]
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": json.dumps(payload, ensure_ascii=False)},
    }
    return "json", {"json": body}


def method_6_sibling_variable_image(ctx):
    """方法 6：input 照舊放文字說明，另外開一個 image 變數放 base64。
    如果 flow 裡除了 input 這個 user intervention 變數，
    還存在別的變數名稱（要用 Pensieve 後台 / DevTools 才能確定確切名稱），
    這種「文字 + 附加變數」的形狀最貼近一般聊天機器人「訊息 + 附件」的做法。
    """
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {
            "input": f"請審核附件公文：{ctx['subject']}",
            "image": ctx["page_b64_list"][0],
        },
    }
    return "json", {"json": body}


def method_7_sibling_variable_attachment_datauri(ctx):
    """方法 7：跟方法 6 類似，但變數名稱換成 attachment，且值用 data URI。"""
    uri = f"data:image/png;base64,{ctx['page_b64_list'][0]}"
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {
            "input": f"請審核附件公文：{ctx['subject']}",
            "attachment": uri,
        },
    }
    return "json", {"json": body}


def method_8_markdown_embedded_image(ctx):
    """方法 8：把圖片用 Markdown 語法內嵌在 input 的文字裡。
    有些聊天式介面的前端本來就是用 Markdown 渲染，
    後端也可能沿用同一套解析器去抓文字裡的圖片。
    """
    uri = f"data:image/png;base64,{ctx['page_b64_list'][0]}"
    text = f"請審核以下掃描公文（主旨：{ctx['subject']}）：\n![scan]({uri})"
    body = {
        "token": ctx["token"],
        "empno": ctx["empno"],
        "variables": {"input": text},
    }
    return "json", {"json": body}


def method_9_multipart_form(ctx):
    """方法 9：改用 multipart/form-data，token / empno / variables 當表單欄位，
    PDF 檔案本體用 files= 上傳。
    如果 flow_chat 這支 API 其實同時支援 JSON 與表單兩種 Content-Type
    （很多 Django/FastAPI 後端會這樣設計），這種方式最接近瀏覽器
    「選擇檔案」上傳時真正送出的格式。
    """
    variables = json.dumps({"input": f"請審核附件公文：{ctx['subject']}"}, ensure_ascii=False)
    form = {"token": ctx["token"], "empno": ctx["empno"], "variables": variables}
    files = {"file": (ctx["filename"], ctx["pdf_bytes"], "application/pdf")}
    return "multipart", {"data": form, "files": files}


def method_10_multipart_form_image_field(ctx):
    """方法 10：跟方法 9 一樣是 multipart，但檔案欄位名稱換成 image，
    且只送轉好的第一頁 PNG（有些後端限定只收圖片 mime type，PDF 會被拒）。
    """
    variables = json.dumps({"input": f"請審核附件公文：{ctx['subject']}"}, ensure_ascii=False)
    form = {"token": ctx["token"], "empno": ctx["empno"], "variables": variables}
    files = {"image": (f"{ctx['filename']}.page1.png", ctx["page_bytes_list"][0], "image/png")}
    return "multipart", {"data": form, "files": files}


METHODS = [
    ("整份 PDF 的 base64 直接塞進 input", method_1_raw_base64_string),
    ("整份 PDF 的 data URI 塞進 input", method_2_data_uri_pdf),
    ("第一頁轉 PNG 的 data URI 塞進 input", method_3_data_uri_png_page),
    ("input 放 JSON 字串（含 filename/mime_type/content_base64）", method_4_structured_json_string),
    ("input 放「每頁一張圖」的 JSON 陣列字串", method_5_page_images_array),
    ("input 純文字 + 另開 image 變數放 base64", method_6_sibling_variable_image),
    ("input 純文字 + 另開 attachment 變數放 data URI", method_7_sibling_variable_attachment_datauri),
    ("input 文字裡用 Markdown 內嵌圖片", method_8_markdown_embedded_image),
    ("改用 multipart/form-data，files 欄位名稱 file，上傳整份 PDF", method_9_multipart_form),
    ("改用 multipart/form-data，files 欄位名稱 image，上傳第一頁 PNG", method_10_multipart_form_image_field),
]


# ═══════════════════════════════════════════════════════════
# 執行 / 報告
# ═══════════════════════════════════════════════════════════

def redact(obj):
    """印 body 前把 base64 / token 這種超長字串截短，log 才看得下去。"""
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes binary>"
    if isinstance(obj, str) and len(obj) > 120:
        return obj[:60] + f"...<截斷，共 {len(obj)} 字元>..." + obj[-20:]
    return obj


def run_one(idx, desc, fn, ctx, dry_run, timeout):
    print("═" * 70)
    print(f"方法 {idx}：{desc}")
    print("─" * 70)

    kind, req_kwargs = fn(ctx)

    if kind == "json":
        print("送法：POST json=")
        print(json.dumps(redact(req_kwargs["json"]), ensure_ascii=False, indent=2))
    else:
        print("送法：POST multipart/form-data")
        print("  data =", redact(req_kwargs["data"]))
        print("  files =", {k: (v[0], f"<{len(v[1])} bytes>", v[2]) for k, v in req_kwargs["files"].items()})

    if dry_run:
        print("（--dry-run：不實際送出）")
        return None

    try:
        if kind == "json":
            resp = requests.post(PENSIEVE_URL, timeout=timeout, verify=VERIFY_SSL,
                                 proxies=PROXIES, **req_kwargs)
        else:
            resp = requests.post(PENSIEVE_URL, timeout=timeout, verify=VERIFY_SSL,
                                 proxies=PROXIES, **req_kwargs)
    except requests.RequestException as e:
        print(f"❌ 連線失敗：{type(e).__name__}: {e}")
        return {"method": idx, "desc": desc, "error": str(e)}

    print(f"HTTP {resp.status_code} | Content-Type: {resp.headers.get('Content-Type')}")
    body_text = resp.content.decode("utf-8", errors="replace")
    print(body_text[:1500])

    return {
        "method": idx,
        "desc": desc,
        "status": resp.status_code,
        "response_preview": body_text[:2000],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", default=DEFAULT_PDF, help="測試用掃描 PDF 路徑")
    ap.add_argument("--empno", default=DEFAULT_EMPNO)
    ap.add_argument("--token", default=None)
    ap.add_argument("--only", default=None, help="只測這幾號方法，例如 1,3,7")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出所有方法後結束")
    ap.add_argument("--max-pages", type=int, default=1)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--gap", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if args.list:
        for i, (desc, _) in enumerate(METHODS, 1):
            print(f"{i:2d}. {desc}")
        return 0

    selected = list(range(1, len(METHODS) + 1))
    if args.only:
        selected = [int(x) for x in args.only.split(",") if x.strip()]

    token = get_token(args.token)

    print(f"讀取測試 PDF：{args.pdf}")
    pdf_bytes = load_whole_pdf_bytes(args.pdf)
    print(f"  → {len(pdf_bytes)} bytes")

    print(f"轉圖：前 {args.max_pages} 頁 @ {args.dpi} dpi …")
    page_bytes_list = render_pdf_pages(args.pdf, max_pages=args.max_pages, dpi=args.dpi)
    for i, p in enumerate(page_bytes_list, 1):
        print(f"  第 {i} 頁 PNG → {len(p)} bytes")

    ctx = {
        "token": token,
        "empno": args.empno,
        "doc_id": "TEST-" + os.path.splitext(os.path.basename(args.pdf))[0][:20],
        "subject": "【測試】" + os.path.splitext(os.path.basename(args.pdf))[0],
        "filename": os.path.basename(args.pdf),
        "pdf_bytes": pdf_bytes,
        "pdf_b64": b64(pdf_bytes),
        "page_bytes_list": page_bytes_list,
        "page_b64_list": [b64(p) for p in page_bytes_list],
    }

    print(f"\n目標 URL：{PENSIEVE_URL}")
    print(f"工號：{args.empno}  |  dry-run：{args.dry_run}  |  測試方法：{selected}\n")

    results = []
    for i in selected:
        if not (1 <= i <= len(METHODS)):
            print(f"（略過不存在的方法編號 {i}）")
            continue
        desc, fn = METHODS[i - 1]
        try:
            r = run_one(i, desc, fn, ctx, args.dry_run, args.timeout)
        except Exception as e:
            print(f"❌ 方法 {i} 執行時發生例外：{type(e).__name__}: {e}")
            r = {"method": i, "desc": desc, "error": str(e)}
        results.append(r)
        if not args.dry_run and i != selected[-1]:
            time.sleep(args.gap)

    if not args.dry_run:
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "probe_image_methods_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n已把每一種方法的結果存到：{report_path}")
        print("拿這份報告比對哪一種 HTTP 200 且回應內容真的讀到圖，再回報給我。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
