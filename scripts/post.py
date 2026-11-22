"""Instagram 自動投稿スクリプト

posts/queue/ にある一番先頭(ファイル名順)の画像を1件だけ投稿し、
投稿できたら posts/posted/ に移動する。

使い方:
  python scripts/post.py --prepare   # 画像をインスタ用JPEGに整える(サイズ・比率・形式)
  python scripts/post.py --dry-run   # 投稿はせず、次に投稿される内容とトークンを確認
  python scripts/post.py             # 本番投稿

必要な環境変数(GitHub Secrets に登録):
  IG_ACCESS_TOKEN  … Meta で発行したアクセストークン
  IG_USER_ID       … Instagram アカウントのID(1784… で始まる数字)
"""

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "posts" / "queue"
POSTED = ROOT / "posts" / "posted"
LOG = POSTED / "log.csv"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
API_VERSION = os.environ.get("IG_API_VERSION", "v25.0")
JST = dt.timezone(dt.timedelta(hours=9))

# インスタのフィード画像の条件
MIN_RATIO = 4 / 5      # 縦長の限界(4:5)
MAX_RATIO = 1.91       # 横長の限界(1.91:1)
MAX_WIDTH = 1440


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- キュー ----------

def queue_items() -> list[Path]:
    QUEUE.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in QUEUE.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def caption_for(image: Path) -> str:
    for ext in (".txt", ".md"):
        c = image.with_suffix(ext)
        if c.exists():
            return c.read_text(encoding="utf-8").strip()
    return ""


# ---------- 画像の準備 ----------

def prepare_images() -> bool:
    """キュー内の画像をインスタが受け付けるJPEGに変換する。変更があれば True。"""
    from PIL import Image, ImageOps

    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass

    changed = False
    for img_path in queue_items():
        with Image.open(img_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            ratio = w / h
            needs = img_path.suffix.lower() not in (".jpg", ".jpeg") or w > MAX_WIDTH \
                or ratio < MIN_RATIO or ratio > MAX_RATIO
            if not needs:
                continue

            # 比率が範囲外なら中央を切り抜く
            if ratio < MIN_RATIO:      # 縦長すぎ
                new_h = int(w / MIN_RATIO)
                top = (h - new_h) // 2
                im = im.crop((0, top, w, top + new_h))
            elif ratio > MAX_RATIO:    # 横長すぎ
                new_w = int(h * MAX_RATIO)
                left = (w - new_w) // 2
                im = im.crop((left, 0, left + new_w, h))

            if im.width > MAX_WIDTH:
                im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)

            out = img_path.with_suffix(".jpg")
            im.save(out, "JPEG", quality=92)

        if out != img_path:
            img_path.unlink()
        log(f"画像を整えました: {img_path.name} -> {out.name} ({im.width}x{im.height})")
        changed = True
    return changed


# ---------- Graph API ----------

class Api:
    def __init__(self, token: str, user_id: str):
        self.token = token
        self.user_id = user_id
        # Instagramログインのトークン(IG…)は graph.instagram.com、それ以外は graph.facebook.com
        host = "graph.instagram.com" if token.startswith("IG") else "graph.facebook.com"
        self.base = f"https://{host}/{API_VERSION}"

    def call(self, method: str, path: str, retries: int = 3, **params) -> dict:
        params["access_token"] = self.token
        for attempt in range(1, retries + 1):
            try:
                r = requests.request(method, f"{self.base}/{path}", params=params, timeout=60)
            except requests.RequestException as e:
                err = f"通信エラー: {e}"
            else:
                data = r.json() if r.content else {}
                if r.ok:
                    return data
                err = f"HTTP {r.status_code}: {data.get('error', data)}"
                if r.status_code < 500 and r.status_code != 429:
                    raise RuntimeError(err)  # 設定ミス等はリトライしても直らない
            if attempt < retries:
                wait = 10 * attempt
                log(f"{err} → {wait}秒後にリトライ ({attempt}/{retries})")
                time.sleep(wait)
        raise RuntimeError(err)

    def whoami(self) -> str:
        return self.call("GET", self.user_id, fields="username").get("username", "?")

    def quota(self) -> tuple[int, int]:
        d = self.call("GET", f"{self.user_id}/content_publishing_limit", fields="quota_usage,config")
        item = (d.get("data") or [{}])[0]
        return item.get("quota_usage", 0), (item.get("config") or {}).get("quota_total", 100)

    def publish_image(self, image_url: str, caption: str) -> str:
        container = self.call("POST", f"{self.user_id}/media", image_url=image_url, caption=caption)["id"]
        log(f"アップロード完了 (container {container})。処理を待っています…")
        for _ in range(30):
            status = self.call("GET", container, fields="status_code").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"インスタ側で画像の処理に失敗しました: {status}")
            time.sleep(5)
        else:
            raise RuntimeError("画像の処理が時間内に終わりませんでした")
        # 公開は二重投稿を防ぐためリトライしない
        return self.call("POST", f"{self.user_id}/media_publish", retries=1, creation_id=container)["id"]


def public_url(image: Path) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("POST_COMMIT") or os.environ.get("GITHUB_SHA")
    if not repo or not ref:
        raise RuntimeError("GitHub Actions 上で実行してください(画像の公開URLが作れません)")
    rel = image.relative_to(ROOT).as_posix()
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{rel}"


def move_to_posted(image: Path, media_id: str) -> None:
    POSTED.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(JST).strftime("%Y%m%d-%H%M")
    for f in [image, image.with_suffix(".txt"), image.with_suffix(".md")]:
        if f.exists():
            shutil.move(str(f), POSTED / f"{stamp}_{f.name}")
    new_log = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        if new_log:
            w.writerow(["posted_at", "file", "media_id"])
        w.writerow([dt.datetime.now(JST).isoformat(timespec="minutes"), image.name, media_id])


# ---------- メイン ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="投稿せずに確認だけする")
    ap.add_argument("--prepare", action="store_true", help="画像をインスタ用に整えるだけ")
    args = ap.parse_args()

    if args.prepare:
        prepare_images()
        return 0

    items = queue_items()
    if not items:
        log("投稿待ちの画像がありません(posts/queue が空)。今回はスキップします。")
        return 0

    image = items[0]
    caption = caption_for(image)
    log(f"次に投稿する画像: {image.name}(残り {len(items)} 件)")
    log("キャプション:\n" + (caption or "(なし)"))

    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("IG_USER_ID", "").strip()
    if not token or not user_id:
        log("IG_ACCESS_TOKEN / IG_USER_ID が設定されていません。GitHub Secrets を確認してください。")
        return 0 if args.dry_run else 1

    api = Api(token, user_id)
    log(f"接続確認OK: @{api.whoami()}")
    used, total = api.quota()
    log(f"24時間の投稿上限: {used}/{total}")
    if used >= total:
        log("投稿上限に達しているため、今回はスキップします。")
        return 0

    if args.dry_run:
        log("[dry-run] ここまで問題なし。実際の投稿はしていません。")
        return 0

    media_id = api.publish_image(public_url(image), caption)
    log(f"投稿しました! media_id={media_id}")
    move_to_posted(image, media_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log(f"エラー: {e}")
        sys.exit(1)
