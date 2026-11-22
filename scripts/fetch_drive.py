"""Google側(LINEで承認済み)から、次に投稿する写真と文章を1件受け取る。

posts/queue/ に写真がまだ残っているときは何もしない(1回に1件ずつ)。

必要な環境変数(GitHub Secrets):
  DRIVE_URL … Google Apps Script のウェブアプリURL
  DRIVE_KEY … 合言葉
"""

import base64
import csv
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "posts" / "queue"
LOG = ROOT / "posts" / "posted" / "log.csv"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/webp": ".webp",
}


def google(action: str, **params) -> dict:
    params.update(action=action, key=os.environ["DRIVE_KEY"])
    r = requests.get(os.environ["DRIVE_URL"], params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Google側のエラー: {data['error']}(合言葉が合っているか確認)")
    return data


def posted_rows() -> set[str]:
    """これまでに投稿した行番号(二重投稿防止用)"""
    if not LOG.exists():
        return set()
    with LOG.open(encoding="utf-8") as fp:
        names = [row.get("file", "") for row in csv.DictReader(fp)]
    return {Path(n).stem[len("gas-row"):] for n in names if n.startswith("gas-row")}


def main() -> int:
    if not os.environ.get("DRIVE_URL") or not os.environ.get("DRIVE_KEY"):
        print("DRIVE_URL / DRIVE_KEY が未設定のため、Googleからの取り込みはスキップします。")
        return 0

    QUEUE.mkdir(parents=True, exist_ok=True)
    if any(p.suffix.lower() in IMAGE_EXTS for p in QUEUE.iterdir()):
        print("投稿待ちの写真がすでにあるので、今回は取り込みません。")
        return 0

    done = posted_rows()
    for _ in range(5):
        d = google("next")
        if d.get("empty"):
            print("LINEで承認済みの投稿がありません。今回は投稿しません。")
            return 0
        row = str(d["row"])
        if row in done:
            # 投稿済みなのに Google 側で「投稿済み」になっていなかったもの(前回の後片付け失敗)
            google("done", row=row)
            continue
        ext = Path(d.get("name", "")).suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = MIME_EXT.get(d.get("mime", ""), ".jpg")
        image = QUEUE / f"gas-row{row}{ext}"
        image.write_bytes(base64.b64decode(d["data"]))
        image.with_suffix(".txt").write_text(d.get("caption", "").strip() + "\n", encoding="utf-8")
        print(f"承認済みの投稿を受け取りました(表の {row} 行目)")
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"エラー: {e}")
        sys.exit(1)
