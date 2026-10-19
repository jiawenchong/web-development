# -*- coding: utf-8 -*-
"""
把「環安衛報表」和 U: 的公文 PDF 送到 10.10.28.170——在「你的電腦」上跑

10.10.28.170 沒有 U: 的權限，也沒辦法自動登入環安衛網站，所以由你的電腦：
  1) 執行 download_report.py 下載環安衛報表（用你電腦的身分，網站會自動登入）
  2) 把報表複製到 .170 的 official_doc 資料夾
  3) 把 U: 新的 / 有更新的 PDF 複製到 .170 的 pdf\\scan、pdf\\download
.170 上的 pipeline 設成不下載（RUN_DOWNLOAD = False），直接讀這些檔案。

.170 的帳號密碼直接填在下面設定區的 REMOTE_USER / REMOTE_PASSWORD。
  ⚠ 填了密碼的這份檔案不要 commit / push 到 git，也不要放到共用資料夾。

執行：
    python sync_pdfs_to_170.py                  # 下載報表 + 同步報表與 PDF
    python sync_pdfs_to_170.py --skip-download  # 不下載，只複製現有的報表與 PDF
    python sync_pdfs_to_170.py --dry-run        # 不下載，只列出會複製哪些檔案

排程：工作排程器設「只在使用者登入時執行」（要有 U:），時間比 .170 的 pipeline 早。
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time


# ═══════════════════════════════════════════════════════════
# 設定區
# ═══════════════════════════════════════════════════════════

# ★ 哪個資料夾複製到哪裡：(U: 上的來源, .170 上的目的地)，一組一行 ★
#   .170 的目的地就是 officialdoc_pipeline.py 裡 PDF_FOLDERS 讀的那兩個資料夾
SYNC_PAIRS = [
    (r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\新增資料夾",
     r"\\10.10.28.170\d$\Web Service\official_doc\pdf\scan"),

    (r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄\公文下載-自動化\解壓縮",
     r"\\10.10.28.170\d$\Web Service\official_doc\pdf\download"),
]

# ★ 環安衛報表：在你電腦上下載，再複製到 .170 ★
#   DOWNLOAD_SCRIPT 留空 = 不下載，只複製 REPORT_SRC 現有的檔案
#   REPORT_SRC 要是 download_report.py 實際下載到的位置（看它的 download.default_directory）
DOWNLOAD_SCRIPT = r"D:\ASEKH\K20076\2026\公文簽核agent\download_report.py"
REPORT_SRC = r"D:\ASEKH\K20076\2026\公文簽核agent\環安衛收發文管理查詢報表_(Security B).xls"
REPORT_DST = r"\\10.10.28.170\d$\Web Service\official_doc\環安衛收發文管理查詢報表_(Security B).xls"
DOWNLOAD_TIMEOUT = 600          # 秒

# 登入 .170 用：net use 連的對象，以及用來確認「連得到」的資料夾
REMOTE_SHARE = r"\\10.10.28.170\d$"
REMOTE_ROOT = r"\\10.10.28.170\d$\Web Service\official_doc\pdf"

# ★ .170 的帳號密碼 ★  本機帳號要寫成「IP\帳號」，例如 10.10.28.170\administrator
#   ⚠ 填了密碼之後，這個檔案不要 commit / push 到 git
REMOTE_USER = r"10.10.28.170\administrator"
REMOTE_PASSWORD = r""            # ← 把 .170 的密碼填在引號中間

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "sync_pdfs.log")


# ═══════════════════════════════════════════════════════════

log = logging.getLogger("sync_pdfs")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


# ── 連線與複製 ─────────────────────────────────────

def ensure_remote():
    """確保連得到 .170。已經連得到就不登入；否則用存好的密碼 net use。"""
    if os.path.isdir(REMOTE_ROOT):
        return True

    password = REMOTE_PASSWORD
    if not password:
        log.error("連不到 %s，而且 REMOTE_PASSWORD 還沒填（在程式最上面的設定區）", REMOTE_ROOT)
        return False

    log.info("登入 %s（%s）", REMOTE_SHARE, REMOTE_USER)
    # 用 list 傳參數、不經過 shell：密碼有特殊字元也不會被拆壞
    result = subprocess.run(
        ["net", "use", REMOTE_SHARE, password, f"/user:{REMOTE_USER}", "/persistent:no"],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        if "1219" in msg:
            log.error("net use 失敗：這台電腦已經用「另一組帳號」連著 10.10.28.170。"
                      "先執行 net use \\\\10.10.28.170\\d$ /delete 再重跑。（%s）", msg)
        else:
            log.error("net use 失敗（REMOTE_PASSWORD 錯？REMOTE_USER 格式要是 IP\\帳號？）：%s", msg)
        return False

    if not os.path.isdir(REMOTE_ROOT):
        log.error("登入成功但找不到 %s，請確認 .170 上有這個資料夾", REMOTE_ROOT)
        return False
    return True


def run_download():
    """在這台電腦執行 download_report.py。回傳是否成功。"""
    if not DOWNLOAD_SCRIPT.strip():
        log.info("沒有設定 DOWNLOAD_SCRIPT，略過下載，直接複製現有報表")
        return True
    if not os.path.isfile(DOWNLOAD_SCRIPT):
        log.error("找不到下載腳本：%s", DOWNLOAD_SCRIPT)
        return False

    log.info("執行下載腳本：%s", DOWNLOAD_SCRIPT)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        result = subprocess.run(
            [sys.executable, DOWNLOAD_SCRIPT], cwd=os.path.dirname(DOWNLOAD_SCRIPT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=DOWNLOAD_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        log.error("下載腳本超過 %d 秒沒結束（卡在登入視窗？）", DOWNLOAD_TIMEOUT)
        return False

    for line in (result.stdout or "").strip().splitlines():
        log.info("  [download] %s", line)
    level = log.error if result.returncode != 0 else log.warning
    for line in (result.stderr or "").strip().splitlines():
        level("  [download:err] %s", line)

    if result.returncode != 0:
        log.error("下載腳本失敗，returncode=%d", result.returncode)
        return False
    return True


def wait_until_stable(path, timeout=60, quiet=2.0):
    """等檔案大小連續 quiet 秒沒變（瀏覽器下載可能還在寫）。"""
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
    return False


def copy_report(dry_run=False):
    """把報表複製到 .170。回傳是否成功。"""
    if not os.path.isfile(REPORT_SRC):
        log.error("找不到報表：%s（download_report.py 下載到別的地方了？）", REPORT_SRC)
        return False
    if not dry_run and not wait_until_stable(REPORT_SRC):
        log.error("報表一直在變動，等不到下載完成：%s", REPORT_SRC)
        return False

    age_h = (time.time() - os.path.getmtime(REPORT_SRC)) / 3600
    if age_h > 24:
        log.warning("報表是 %.0f 小時前的，不是今天下載的：%s", age_h, REPORT_SRC)

    if not needs_copy(REPORT_SRC, REPORT_DST):
        log.info("報表已是最新，不用複製：%s", REPORT_DST)
        return True
    if dry_run:
        log.info("會複製報表：%s → %s", REPORT_SRC, REPORT_DST)
        return True

    tmp = REPORT_DST + ".tmp"
    shutil.copy2(REPORT_SRC, tmp)
    os.replace(tmp, REPORT_DST)
    log.info("已複製報表：%s → %s", REPORT_SRC, REPORT_DST)
    return True


def needs_copy(src, dst):
    if not os.path.exists(dst):
        return True
    s, d = os.stat(src), os.stat(dst)
    return s.st_size != d.st_size or s.st_mtime > d.st_mtime


def sync(dry_run=False, download=True):
    total_copied, total_failed = 0, 0

    # 1) 下載報表（dry-run 不下載）
    if download and not dry_run and not run_download():
        total_failed += 1      # 下載失敗還是繼續：PDF 照樣同步，報表沿用上一次的

    if not ensure_remote():
        return 1

    # 2) 報表
    try:
        if not copy_report(dry_run=dry_run):
            total_failed += 1
    except OSError as e:
        total_failed += 1
        log.error("報表複製失敗：%s: %s", type(e).__name__, e)

    # 3) PDF
    for src_dir, dst_dir in SYNC_PAIRS:
        if not os.path.isdir(src_dir):
            log.error("讀不到來源 %s（U: 沒掛上？路徑打錯？排程要選「只在使用者登入時執行」）", src_dir)
            total_failed += 1
            continue
        if not dry_run:
            os.makedirs(dst_dir, exist_ok=True)

        copied = skipped = 0
        for name in sorted(os.listdir(src_dir)):
            if not name.lower().endswith(".pdf"):
                continue
            src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
            try:
                if not needs_copy(src, dst):
                    skipped += 1
                    continue
                if dry_run:
                    log.info("  會複製：%s", name)
                else:
                    tmp = dst + ".tmp"      # 先寫暫存檔，pipeline 不會讀到複製一半的 PDF
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                copied += 1
            except OSError as e:
                total_failed += 1
                log.warning("  複製失敗 %s：%s: %s", name, type(e).__name__, e)

        total_copied += copied
        log.info("%s → %s：%s %d 個，已是最新 %d 個",
                 src_dir, dst_dir, "會複製" if dry_run else "複製", copied, skipped)

    log.info("完成：PDF %s %d 個，失敗 %d 項", "會複製" if dry_run else "共複製", total_copied, total_failed)
    return 1 if total_failed else 0


def main():
    parser = argparse.ArgumentParser(description="下載環安衛報表，並把報表與 U: 的公文 PDF 複製到 10.10.28.170")
    parser.add_argument("--dry-run", action="store_true", help="不下載，只列出會複製哪些檔案")
    parser.add_argument("--skip-download", action="store_true", help="不跑 download_report.py，只複製現有檔案")
    args = parser.parse_args()

    setup_logging()
    log.info("=" * 50)
    try:
        return sync(dry_run=args.dry_run, download=not args.skip_download)
    except Exception as e:          # 排程跑的時候要留下原因
        log.exception("同步失敗：%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
