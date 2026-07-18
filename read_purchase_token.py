"""
read_purchase_token.py
通过 ADB 从 Google Play library.db 读取 ChatGPT 购买 Token，保存到 purchases.txt
"""

import os
import re
import json
import time
import sqlite3
import tempfile
import subprocess

# ── 配置 ────────────────────────────────────────────────────────────────────
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "purchases.txt")
DB_SRC      = "/data/data/com.android.vending/databases/library.db"
DB_SDCARD   = "/sdcard/.tmp_read_token.db"
# 留空则自动选第一台设备；填入序列号可指定设备，如 "emulator-5554"
DEVICE_SERIAL = ""


# ── ADB 工具 ─────────────────────────────────────────────────────────────────
def adb(*args, timeout=15) -> tuple[int, str]:
    cmd = ["adb"]
    if DEVICE_SERIAL:
        cmd += ["-s", DEVICE_SERIAL]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return -1, "adb 未找到，请确认已安装 Android Platform Tools 并加入 PATH"
    except subprocess.TimeoutExpired:
        return -2, "adb 命令超时"


def log(msg: str):
    print(msg)


# ── 主逻辑 ───────────────────────────────────────────────────────────────────
def read_token():
    tmp_path = None
    try:
        log("══ 开始读取 library.db ══")

        # 1. 将数据库复制到 sdcard（需要 root）
        code, out = adb("shell", f"su -c 'cp {DB_SRC} {DB_SDCARD} && chmod 644 {DB_SDCARD} && echo DONE'", timeout=15)
        log(f"[cp] code={code}  out={out!r}")
        if code != 0 or "DONE" not in out:
            log(f"❌ 复制失败，请确认设备已 root，路径: {DB_SRC}")
            return

        # 2. pull 到本地临时文件
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        code, out = adb("pull", DB_SDCARD, tmp_path, timeout=20)
        log(f"[pull] code={code}  size={os.path.getsize(tmp_path)} bytes  {out!r}")
        if code != 0:
            log("❌ pull 失败")
            return

        # 3. 解析 SQLite
        conn = sqlite3.connect(tmp_path)
        cur  = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        log(f"[db] 所有表: {tables}")

        if "ownership" not in tables:
            log("❌ 找不到 ownership 表")
            conn.close()
            return

        cur.execute("PRAGMA table_info(ownership)")
        cols = [r[1] for r in cur.fetchall()]
        log(f"[db] ownership 列: {cols}")

        # 自动识别 JSON 数据列
        JSON_COL_CANDIDATES = ["inapp_purchase_data", "purchase_data", "value", "data"]
        json_col = next((c for c in JSON_COL_CANDIDATES if c in cols), None)
        if not json_col:
            json_col = next((c for c in cols if "purchase" in c.lower() or "inapp" in c.lower()), None)
        log(f"[db] JSON列={json_col!r}")

        doc_col = "doc_id" if "doc_id" in cols else (cols[3] if len(cols) > 3 else cols[0])

        if json_col:
            sql = f"""
                SELECT account, {doc_col}, {json_col}
                FROM ownership
                WHERE ({doc_col} LIKE '%openai%' OR {doc_col} LIKE '%chatgpt%')
                  AND {json_col} IS NOT NULL
                ORDER BY rowid DESC
            """
        else:
            sql = f"SELECT * FROM ownership WHERE ({doc_col} LIKE '%openai%' OR {doc_col} LIKE '%chatgpt%') ORDER BY rowid DESC"

        cur.execute(sql)
        rows = cur.fetchall()
        conn.close()
        log(f"[db] 命中 {len(rows)} 行")

        saved = set()
        for row in rows:
            email    = str(row[0]) if row[0] else "unknown"
            doc_id   = str(row[1]) if len(row) > 1 and row[1] else ""
            raw_json = str(row[2]) if len(row) > 2 and row[2] else ""

            if not raw_json or "purchaseToken" not in raw_json:
                continue
            try:
                data  = json.loads(raw_json)
                token = data.get("purchaseToken", "")
                order = data.get("orderId", "")
            except Exception as e:
                log(f"[db] JSON 解析失败: {e}")
                continue

            if not token or token in saved:
                continue
            saved.add(token)

            ts    = time.strftime("%Y-%m-%d %H:%M:%S")
            entry = f"{ts} | {email} | {order} | {token}\n"
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(entry)

            log(f"✅ 邮箱 : {email}")
            log(f"   Order: {order}")
            log(f"   Token: {token[:60]}…")
            log(f"💾 已保存 → {OUTPUT_FILE}")

        if not saved:
            log("⚠ 未找到 ChatGPT 购买记录")
        log(f"══ 完成，共保存 {len(saved)} 条 ══")

    except Exception as e:
        log(f"❌ 异常: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        adb("shell", f"rm -f {DB_SDCARD}", timeout=5)


if __name__ == "__main__":
    read_token()
