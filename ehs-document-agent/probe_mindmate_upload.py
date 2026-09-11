# -*- coding: utf-8 -*-
"""
probe_mindmate_upload.py — 測試真正的檔案上傳端點 /mindMate/upload_file/

背景
----
probe_image_methods.py 測完的結論：/api/flow_chat/ 這條路線完全不接受
圖片/檔案（10 種塞法全部失敗，其中一種還會在讀不到內容時捏造假公文）。

從瀏覽器 DevTools 抓到的真實請求證實：Pensieve 網頁版上傳檔案，
打的其實是另一個完全不同的端點：

    POST https://pensieve.kh.asegroup.com/mindMate/upload_file/

跟 flow_chat 的差異：
  * 命名空間不同：mindMate（網頁後台） vs api（flow_chat 的 REST API）
  * 驗證方式不同：這裡用瀏覽器登入後的 session cookie
    （sessionid / PensieveLog），不是 flow_chat 用的 token + empno
  * 綁定 flow：Referer 帶著 /flow/<flow_id>，代表上傳跟你正在開的那個
    flow 頁面是綁在一起的

這支腳本就是照抓到的那個 curl 重建，差別是真的把 PDF 二進位內容送出去
（瀏覽器複製 cURL 時，二進位檔案內容本來就不會被包含在文字裡，所以你
複製到的那個 curl 指令本身是不能直接執行的——這支腳本補上這一塊）。

⚠️ 兩個要注意的地方：
  1. sessionid / PensieveLog 是「你目前登入瀏覽器的身分」，等同一組密碼。
     不要把它寫死進這支程式、不要 commit 進 git，用完就丟。
     這裡設計成只能用命令列參數或環境變數帶入，不放在程式碼裡。
  2. 這組 cookie 有時效性，過一段時間或登出後就會失效，
     失效的話重新在瀏覽器 F12 → Application → Cookies 複製新的即可。

用法（在你自己那台連得到內網的機器上）：
    set PENSIEVE_SESSIONID=<你從瀏覽器複製的 sessionid 值>
    set PENSIEVE_PENSIEVELOG=<你從瀏覽器複製的 PensieveLog 值>
    python probe_mindmate_upload.py

    也可以直接用參數帶（不建議留在 shell history 裡）：
    python probe_mindmate_upload.py --sessionid xxx --pensievelog yyy

    若回應是 403 且內容提到 CSRF，代表這個端點其實有擋 CSRF token，
    去瀏覽器 DevTools 的 Cookies 裡找 csrftoken 的值，補上：
    python probe_mindmate_upload.py --sessionid xxx --pensievelog yyy --csrftoken zzz

跑完看什麼：
  * 印出來的完整回應 body——這就是「上傳完後伺服器給你的檔案代號」，
    把這個貼給我，或直接告訴我裡面的鍵名（例如 file_id / url / id）。
  * 接下來還要抓「送出對話那個請求」怎麼引用這個代號，
    這支腳本測不出來，要回瀏覽器 DevTools 抓那個請求。
"""

import argparse
import json
import os
import sys

import requests

MINDMATE_UPLOAD_URL = "https://pensieve.kh.asegroup.com/mindMate/upload_file/"

# 從你抓到的 curl 裡的 Referer 取出來的 flow_id，跟 flow_chat 的 token 裡的
# flow_id 完全一致，代表上傳確實是綁在這個 flow 頁面底下。
FLOW_ID = "5d5d65ad19374419b281ecab12114ca9"
REFERER = f"https://pensieve.kh.asegroup.com/flow/{FLOW_ID}"

DEFAULT_PDF = (
    r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\2026"
    r"\1150728_1158090772_台灣電力股份有限公司高雄區營業處_K3B_(Security C).pdf"
)

VERIFY_SSL = False
PROXIES = {"http": None, "https": None}

if VERIFY_SSL is False:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def build_cookies(args):
    sessionid = args.sessionid or os.environ.get("PENSIEVE_SESSIONID")
    pensievelog = args.pensievelog or os.environ.get("PENSIEVE_PENSIEVELOG")
    csrftoken = args.csrftoken or os.environ.get("PENSIEVE_CSRFTOKEN")

    if not sessionid:
        raise RuntimeError(
            "缺 sessionid。到瀏覽器 DevTools → Application（或 儲存空間）→ Cookies → "
            "pensieve.kh.asegroup.com，複製 sessionid 的值，"
            "用 --sessionid 帶入，或設環境變數 PENSIEVE_SESSIONID。\n"
            "（這組值等同你的登入身分，不要外流、不要寫進程式碼。）"
        )

    cookies = {"sessionid": sessionid}
    if pensievelog:
        cookies["PensieveLog"] = pensievelog
    if csrftoken:
        cookies["csrftoken"] = csrftoken

    return cookies, csrftoken


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", default=DEFAULT_PDF, help="要上傳的測試 PDF 路徑")
    ap.add_argument("--sessionid", default=None,
                    help="瀏覽器 cookie 裡的 sessionid（也可用環境變數 PENSIEVE_SESSIONID）")
    ap.add_argument("--pensievelog", default=None,
                    help="瀏覽器 cookie 裡的 PensieveLog（也可用環境變數 PENSIEVE_PENSIEVELOG）")
    ap.add_argument("--csrftoken", default=None,
                    help="若回 403 且提到 CSRF，補這個再試（環境變數 PENSIEVE_CSRFTOKEN）")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    cookies, csrftoken = build_cookies(args)

    if not os.path.exists(args.pdf):
        print(f"讀不到 PDF：{args.pdf}", file=sys.stderr)
        return 1

    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"),
    }
    if csrftoken:
        # Django 的慣例標頭名稱；若平台用別的框架，實際標頭名稱可能不同，
        # 但先用這個試，403 訊息裡通常會講清楚它要哪個標頭名。
        headers["X-CSRFToken"] = csrftoken

    print(f"上傳 {args.pdf} 到 {MINDMATE_UPLOAD_URL} …")
    print(f"使用的 cookies：{ {k: (v[:6] + '…') for k, v in cookies.items()} }")

    with open(args.pdf, "rb") as f:
        files = {"file": (os.path.basename(args.pdf), f, "application/pdf")}
        try:
            resp = requests.post(
                MINDMATE_UPLOAD_URL, headers=headers, cookies=cookies, files=files,
                timeout=args.timeout, verify=VERIFY_SSL, proxies=PROXIES,
            )
        except requests.RequestException as e:
            print(f"❌ 連線失敗：{type(e).__name__}: {e}", file=sys.stderr)
            return 1

    print("─" * 60)
    print(f"HTTP {resp.status_code} | Content-Type: {resp.headers.get('Content-Type')}")
    print("─" * 60)
    body_text = resp.content.decode("utf-8", errors="replace")

    try:
        parsed = json.loads(body_text)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        print("─" * 60)
        print("★ 把上面這個結構貼給我——特別是有沒有 file_id / id / url 這類欄位，")
        print("  那就是接下來要在 flow_chat 的 variables 裡引用的東西。")
    except json.JSONDecodeError:
        print(body_text[:3000])
        if resp.status_code == 403 and "CSRF" in body_text:
            print("─" * 60)
            print("看起來是 CSRF 擋下來的，去瀏覽器 Cookies 找 csrftoken 的值，")
            print("用 --csrftoken 帶進來再試一次。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
