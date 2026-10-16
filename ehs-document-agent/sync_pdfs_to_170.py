# -*- coding: utf-8 -*-
"""
把 U: 共用資料夾裡的公文 PDF 複製到 10.10.28.170——在「你的電腦」上跑

10.10.28.170 沒有 U: 的權限，所以由你的電腦（讀得到 U:）把新的 / 有更新的 PDF
直接複製到 .170 的 pdf\\scan、pdf\\download，170 上的 pipeline 再去讀那兩個資料夾。

.170 的帳密不寫在程式裡：
  第一次執行  python sync_pdfs_to_170.py --save-password
  輸入一次密碼，用 Windows DPAPI 加密存成 sync_pdfs_password.bin。
  這個檔案只有「你的 Windows 帳號、在這台電腦上」才解得開，被複製到別台也沒用。
  改密碼後再執行一次 --save-password 即可。

執行：
    python sync_pdfs_to_170.py                  # 同步
    python sync_pdfs_to_170.py --dry-run        # 只列出會複製哪些檔案
    python sync_pdfs_to_170.py --save-password  # 存 / 更新 .170 的密碼

排程：工作排程器設「只在使用者登入時執行」（要有 U:），時間比 .170 的 pipeline 早。
"""

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys


# ═══════════════════════════════════════════════════════════
# 設定區
# ═══════════════════════════════════════════════════════════

# 來源：你電腦上的 U:
SOURCE_ROOT = r"U:\0.廠務環工公用資料夾\23.UTI-各項資料回覆\● 公文掃瞄"

# 目的地：10.10.28.170 上 pipeline 讀的資料夾
REMOTE_SHARE = r"\\10.10.28.170\d$"                       # net use 連的對象
REMOTE_ROOT = r"\\10.10.28.170\d$\Web Service\official_doc\pdf"

# (來源子資料夾, 目的子資料夾)
SYNC_PAIRS = [
    ("新增資料夾", "scan"),
    (os.path.join("公文下載-自動化", "解壓縮"), "download"),
]

# ★ .170 的帳號 ★  本機帳號要寫成「IP\帳號」，例如 10.10.28.170\administrator
REMOTE_USER = r"10.10.28.170\administrator"

HERE = os.path.dirname(os.path.abspath(__file__))
PASSWORD_FILE = os.path.join(HERE, "sync_pdfs_password.bin")   # DPAPI 加密後的密碼
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


# ── Windows DPAPI：用目前登入的 Windows 帳號加密 / 解密 ─────────────

def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    func = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    func.argtypes = [ctypes.POINTER(DATA_BLOB),
                     wintypes.LPCWSTR if encrypt else ctypes.c_void_p,
                     ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    func.restype = wintypes.BOOL

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    desc = "officialdoc sync" if encrypt else None

    if not func(ctypes.byref(blob_in), desc, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def save_password():
    pw = getpass.getpass(f"{REMOTE_USER} 的密碼（輸入時不會顯示）：")
    if not pw:
        print("沒有輸入密碼，取消。")
        return 1
    with open(PASSWORD_FILE, "wb") as f:
        f.write(_dpapi(pw.encode("utf-8"), encrypt=True))
    print(f"已加密存到 {PASSWORD_FILE}（只有你的帳號在這台電腦上解得開）")
    return 0


def load_password():
    env = os.environ.get("SYNC_170_PASSWORD")
    if env:
        return env
    if not os.path.exists(PASSWORD_FILE):
        return None
    with open(PASSWORD_FILE, "rb") as f:
        return _dpapi(f.read(), encrypt=False).decode("utf-8")


# ── 連線與複製 ─────────────────────────────────────

def ensure_remote():
    """確保連得到 .170。已經連得到就不登入；否則用存好的密碼 net use。"""
    if os.path.isdir(REMOTE_ROOT):
        return True

    password = load_password()
    if not password:
        log.error("連不到 %s，而且還沒存密碼。先執行：python sync_pdfs_to_170.py --save-password",
                  REMOTE_ROOT)
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
            log.error("net use 失敗（密碼錯？帳號格式要是 IP\\帳號？）：%s", msg)
        return False

    if not os.path.isdir(REMOTE_ROOT):
        log.error("登入成功但找不到 %s，請確認 .170 上有這個資料夾", REMOTE_ROOT)
        return False
    return True


def needs_copy(src, dst):
    if not os.path.exists(dst):
        return True
    s, d = os.stat(src), os.stat(dst)
    return s.st_size != d.st_size or s.st_mtime > d.st_mtime


def sync(dry_run=False):
    if not os.path.isdir(SOURCE_ROOT):
        log.error("讀不到來源 %s（U: 沒掛上？排程要選「只在使用者登入時執行」）", SOURCE_ROOT)
        return 1
    if not ensure_remote():
        return 1

    total_copied, total_failed = 0, 0
    for src_rel, dst_rel in SYNC_PAIRS:
        src_dir = os.path.join(SOURCE_ROOT, src_rel)
        dst_dir = os.path.join(REMOTE_ROOT, dst_rel)
        if not os.path.isdir(src_dir):
            log.error("來源資料夾不存在：%s", src_dir)
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

    log.info("完成：%s %d 個 PDF，失敗 %d", "會複製" if dry_run else "共複製", total_copied, total_failed)
    return 1 if total_failed else 0


def main():
    parser = argparse.ArgumentParser(description="把 U: 的公文 PDF 複製到 10.10.28.170")
    parser.add_argument("--save-password", action="store_true", help="輸入並加密儲存 .170 的密碼")
    parser.add_argument("--dry-run", action="store_true", help="只列出會複製哪些檔案")
    args = parser.parse_args()

    if args.save_password:
        return save_password()

    setup_logging()
    log.info("=" * 50)
    try:
        return sync(dry_run=args.dry_run)
    except Exception as e:          # 排程跑的時候要留下原因
        log.exception("同步失敗：%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
