# ===== MEDIA-HELPERS-BEGIN =====
# Helpers are kept in the scripts/ tree (not embedded in workflow YAML) so they
# can be tested and reviewed independently. Some overlap with refetch.py remains.
import datetime
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import mutagen
from PIL import Image, ImageOps
from mutagen.id3 import ID3, APIC, TIT2, TPE1
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://y.qq.com/",
}
AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".oga", ".wav", ".aac", ".wma", ".opus"}
IMG_EXT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}
IMG_MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
MIN_COVER_BYTES = int(os.environ.get("MIN_COVER_BYTES", "1024"))
RETRY_TOTAL = int(os.environ.get("RETRY_TOTAL", "3"))

def make_session():
    """带自动重试的 Session：封面/歌词/音频三条下载链路共用。"""
    s = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        status=RETRY_TOTAL,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,          # 交给调用方的 raise_for_status 统一处理
        respect_retry_after_header=False,  # 无视 Retry-After，避免被 CDN 拖到 job 超时
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(UA)
    return s

SESSION = make_session()

def normalize_url(u):
    """补全协议相对链接（QQ 音乐有时给 //y.gtimg.cn/...），其余原样返回。
    任何类型都不抛异常：字段可能是 null / 数字 / 数组，畸形 JSON 不能拖垮整批。"""
    u = "" if u is None else str(u)
    u = u.strip()
    return "https:" + u if u.startswith("//") else u

def brief_error(e):
    """把异常压成一个短标记，写进 info.json 的 coverError / lyricError。"""
    if isinstance(e, (RuntimeError, ValueError)) and str(e):
        return str(e)[:60]          # 自定义校验原因，如 too_small(103B) / not_image / not_lrc
    if isinstance(e, requests.HTTPError):
        r = getattr(e, "response", None)
        if r is not None:
            return f"HTTP {r.status_code}"
        return "HTTPError"
    if isinstance(e, requests.Timeout):
        return "Timeout"
    if isinstance(e, requests.ConnectionError):
        return "ConnectionError"
    return type(e).__name__

ALLOWED_HOST_SUFFIXES = ("qq.com", "gtimg.cn", "music.126.net", "126.net", "163.com", "kuwo.cn", "musifei.org", "toubiec.cn")
MAX_MEDIA_BYTES = 15 * 1024 * 1024
def allowed_source_url(url):
    try:
        p = urlparse(url); host = (p.hostname or "").lower().rstrip(".")
        return p.scheme == "https" and not p.username and not p.password and p.port in (None, 443) and any(host == d or host.endswith("." + d) for d in ALLOWED_HOST_SUFFIXES)
    except Exception: return False
def download(url, dest, timeout=120, max_bytes=MAX_MEDIA_BYTES):
    """逐跳复核 HTTPS 来源域名并限制字节数；断流/超限不留下半成品。"""
    dest = Path(dest); part = dest.with_name(dest.name + ".part"); current = url
    try:
        for _ in range(6):
            if not allowed_source_url(current): raise RuntimeError("URL host/protocol not allowed")
            r = SESSION.get(current, stream=True, timeout=timeout, allow_redirects=False)
            if r.status_code in (301,302,303,307,308):
                loc = r.headers.get("Location"); r.close()
                if not loc: raise RuntimeError("redirect without Location")
                current = requests.compat.urljoin(current, loc); continue
            r.raise_for_status()
            size = r.headers.get("Content-Length")
            if size and int(size) > max_bytes: r.close(); raise RuntimeError("download exceeds limit")
            total = 0
            try:
                with open(part, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if not chunk: continue
                        total += len(chunk)
                        if total > max_bytes: raise RuntimeError("download exceeds limit")
                        f.write(chunk)
                ctype = r.headers.get("Content-Type", "")
            finally: r.close()
            part.rename(dest); return ctype
        raise RuntimeError("too many redirects")
    finally: part.unlink(missing_ok=True)

def sniff_image(path):
    """按文件头判断真实图片类型，返回 jpeg/png/webp/gif 或 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None

def normalize_cover(path, max_side=800):
    """Rotate by EXIF, flatten transparency to white, resize, JPEG-compress in place."""
    path = Path(path); tmp = path.with_name(path.name + ".normalized.jpg")
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            bg = Image.new("RGB", im.size, "white"); bg.paste(im, mask=im.getchannel("A"))
            bg.save(tmp, "JPEG", quality=88, optimize=True, progressive=True)
        tmp.replace(path); return True
    finally: tmp.unlink(missing_ok=True)

def looks_like_html(text):
    """直链失效时 CDN 可能返回 200 + 错误页；真歌词正文里不会出现 HTML 标签。"""
    t = text.lstrip()[:2000]
    low = t.lower()
    if low.startswith("<!doctype") or low.startswith("<html") or low.startswith("<?xml"):
        return True
    return re.search(r"<\s*[a-z!/][^>]*>", t, re.I) is not None

def lyric_looks_ok(text):
    """lrc 直链的内容校验，只挡三种明显不是歌词的响应。返回 (ok, reason)。"""
    t = (text or "").strip()
    if not t:
        return False, "empty"          # 空文件 / 全空白
    if looks_like_html(t):
        return False, "html_page"      # 404 错误页之类
    if t.startswith("{") and t.endswith("}"):
        return False, "not_lrc"        # 直链失效后返回的 JSON 错误体
    return True, None

def fetch_lyric_text(url):
    """有域名/重定向/字节上限的歌词下载，支持常见中文编码。"""
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as tmp: path = Path(tmp.name)
    try:
        download(url, path, timeout=60, max_bytes=2 * 1024 * 1024)
        raw = path.read_bytes()
    finally: path.unlink(missing_ok=True)
    for enc in ("utf-8", "gb18030"):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    return raw.decode("utf-8", errors="replace")

def write_tags(audio_path, title, author):
    """只写标题/艺术家。与封面解耦：没有封面时也会调用，避免音频变成无名文件。"""
    ext = audio_path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(str(audio_path))
            except Exception:
                tags = ID3()
            tags.delall("TIT2")
            tags.delall("TPE1")
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=author))
            tags.save(str(audio_path))
        elif ext in (".m4a", ".mp4"):
            mp4 = MP4(str(audio_path))
            if mp4.tags is None:
                mp4.add_tags()
            mp4["\xa9nam"] = [title]
            mp4["\xa9ART"] = [author]
            mp4.save()
        elif ext == ".flac":
            fl = FLAC(str(audio_path))
            fl["title"] = title
            fl["artist"] = author
            fl.save()
        else:
            # ogg / opus / wav / wma 等：交给 mutagen 通用层尽力写入
            f = mutagen.File(str(audio_path))
            if f is None:
                print(f"    {ext} 格式无法识别，跳过 tag 写入")
                return False
            if f.tags is None:
                f.add_tags()
            if isinstance(f.tags, ID3):
                f.tags.delall("TIT2")
                f.tags.delall("TPE1")
                f.tags.add(TIT2(encoding=3, text=title))
                f.tags.add(TPE1(encoding=3, text=author))
            else:
                f.tags["title"] = title
                f.tags["artist"] = author
            f.save()
        return True
    except Exception as e:
        print(f"    写标题/艺术家 tag 失败({ext}): {e}")
        return False

def embed_cover(audio_path, cover_path):
    """只负责嵌入封面图。返回 True/False。"""
    try:
        data = cover_path.read_bytes()
    except Exception as e:
        print(f"    读取封面失败: {e}")
        return False
    kind = sniff_image(cover_path) or "jpeg"
    mime = IMG_MIME.get(kind, "image/jpeg")
    ext = audio_path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(str(audio_path))
            except Exception:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            tags.save(str(audio_path))
        elif ext in (".m4a", ".mp4"):
            mp4 = MP4(str(audio_path))
            if mp4.tags is None:
                mp4.add_tags()
            fmt = MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG
            mp4["covr"] = [MP4Cover(data, imageformat=fmt)]
            mp4.save()
        elif ext == ".flac":
            fl = FLAC(str(audio_path))
            fl.clear_pictures()
            pic = Picture()
            pic.data = data
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            fl.add_picture(pic)
            fl.save()
        else:
            # wav / ogg / opus 等：能拿到 ID3 容器就嵌，拿不到就只留独立封面文件
            f = mutagen.File(str(audio_path))
            if f is None:
                print(f"    {ext} 格式无法识别，保留独立封面文件")
                return False
            if f.tags is None:
                f.add_tags()
            if isinstance(f.tags, ID3):
                f.tags.delall("APIC")
                f.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                f.save()
                return True
            print(f"    {ext} 的 tag 容器不支持封面，保留独立封面文件")
            return False
        return True
    except Exception as e:
        print(f"    嵌入封面失败({ext}): {e}")
        return False
# ===== MEDIA-HELPERS-END =====

import shutil
import tempfile

MAIN = Path(os.environ.get("MAIN_DIR", "main"))
MISSING = MAIN / "missing.txt"
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
PER_RUN_LIMIT = int(os.environ.get("PER_RUN_LIMIT", "40"))
NO_RETRY_REASONS = {"no_url"}   # 没有链接可补，只在 missing.txt 里留档
# 下载暂存放在仓库外：即使 job 被硬中断（超时/OOM），也不会有半成品被 git add -A 提交进 main
WORK = Path(tempfile.mkdtemp(prefix="refetch-"))

HEADER = (
    "# missing.txt — 由 cleanup workflow 的 refetch job 自动生成/维护\n"
    "# 来源：各歌曲文件夹 info.json 里的 picOk / lrcOk（archive 软失败时写入）\n"
    "# 列（TAB 分隔）：folder  kind(cover|lyric)  reason  attempts  lastAttempt  url\n"
    "# attempts 达到上限后不再重试，条目保留在此供人工排查\n"
)

def find_audio(folder):
    best = None
    for p in folder.glob("song.*"):
        if p.suffix.lower() in AUDIO_EXTS and p.is_file():
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
    return best

def clean_field(v):
    return str(v if v is not None else "").replace("\t", " ").replace("\n", " ").strip()

def load_attempts():
    """读取上一轮 missing.txt 里的 attempts / lastAttempt。"""
    hist = {}
    if not MISSING.exists():
        return hist
    for line in MISSING.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        try:
            attempts = int(cols[3])
        except ValueError:
            attempts = 0
        hist[(cols[0], cols[1])] = {"attempts": attempts, "lastAttempt": cols[4], "reason": cols[2]}
    return hist

def save_info(folder, info):
    (folder / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def infer_server(info):
  server = str(info.get("server") or "").lower()
  if server in ("tencent", "netease"): return server
  urls = [str(info.get(k) or "") for k in ("url", "pic", "lrc")]
  for raw in urls:
    try:
      u = urlparse(raw); host = (u.hostname or "").lower()
      q = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(u.query)
      if q.get("server", [""])[0] in ("tencent", "netease"): return q["server"][0]
      if host.endswith("qq.com") or host.endswith("gtimg.cn"): return "tencent"
      if host.endswith("126.net") or host.endswith("163.com"): return "netease"
    except Exception: pass
  return ""

# ---- 1) 扫描 main，找出所有缺资源的文件夹 ----------------------------------
hist = load_attempts()
targets = []
scanned = 0
for folder in sorted(p for p in MAIN.iterdir() if p.is_dir()):
  if folder.name.startswith("."):
    continue
  info_path = folder / "info.json"
  if not info_path.is_file():
    continue
  try:
    info = json.loads(info_path.read_text(encoding="utf-8"))
  except Exception as e:
    print(f"[WARN] {folder.name}: info.json 解析失败: {e}")
    continue
  scanned += 1
  files = info.get("files") or {}
  pic = normalize_url(info.get("pic"))
  lrc = normalize_url(info.get("lrc"))
  server = infer_server(info)
  songmid = str(info.get("songmid") or "").strip()
  if server and songmid:
    info["server"] = server
    base = "https://vercel-meting-api.musifei.org/api?server=" + server + "&type={kind}&id=" + requests.utils.quote(songmid, safe="")
    if not pic.startswith("http"): pic = base.format(kind="pic")
    if not lrc.startswith("http"): lrc = base.format(kind="lrc")
    info["pic"], info["lrc"] = pic, lrc
  # 兼容旧版 info.json（没有 picOk / lrcOk 字段）：按有没有落地文件推断
  pic_ok = info.get("picOk")
  if pic_ok is None:
    pic_ok = bool(files.get("cover"))
  lrc_ok = info.get("lrcOk")
  if lrc_ok is None:
    lrc_ok = bool(files.get("lyric"))

  for kind, ok_flag, url, err_key in (
    ("cover", pic_ok, pic, "coverError"),
    ("lyric", lrc_ok, lrc, "lyricError"),
  ):
    if ok_flag:
        continue
    reason = clean_field(info.get(err_key)) or "legacy_unknown"
    if not url.startswith("http"):
        reason = "no_url"
    elif reason == "no_url" and server and songmid:
        reason = "derived_url"
    h = hist.get((folder.name, kind), {})
    attempts = int(h.get("attempts", 0))
    last = h.get("lastAttempt", "")
    # 文件夹被重新归档过（链接是新的）→ 失败计数归零，别拿旧账直接 GIVEUP
    archived_at, last_at = parse_iso(info.get("archivedAt")), parse_iso(last)
    if attempts and archived_at and last_at and archived_at > last_at:
      print(f"[RESET] {folder.name} [{kind}]: 已重新归档，attempts {attempts} -> 0")
      attempts, last = 0, ""
    targets.append({
        "folder": folder.name,
        "kind": kind,
        "reason": reason,
        "url": url,
        "attempts": attempts,
        "lastAttempt": last,
    })
print(f"扫描 info.json: {scanned} 个；待补抓条目: {len(targets)} 条")

# ---- 2) 逐条补抓 ----------------------------------------------------------
fixed = failed = given_up = deferred = skipped = 0
budget = PER_RUN_LIMIT
for t in sorted(targets, key=lambda x: (x["attempts"], x["folder"], x["kind"])):
  folder = MAIN / t["folder"]
  kind = t["kind"]
  tag = f'{t["folder"]} [{kind}]'

  if t["reason"] in NO_RETRY_REASONS or not t["url"].startswith("http"):
    skipped += 1
    print(f"[SKIP] {tag}: 没有可用链接（{t['reason']}），仅留档")
    continue
  if t["attempts"] >= MAX_ATTEMPTS:
    given_up += 1
    print(f"[GIVEUP] {tag}: 已尝试 {t['attempts']} 次，最后原因 {t['reason']}")
    continue
  if budget <= 0:
    deferred += 1
    print(f"[DEFER] {tag}: 本轮补抓额度({PER_RUN_LIMIT})用完，下一轮再试")
    continue
  budget -= 1

  info_path = folder / "info.json"
  try:
    info = json.loads(info_path.read_text(encoding="utf-8"))
  except Exception as e:
    t["reason"] = "info_unreadable"
    t["attempts"] += 1
    t["lastAttempt"] = now_iso()
    failed += 1
    print(f"[FAIL] {tag}: info.json 读不出来: {e}")
    continue

  stage = WORK / f'{t["folder"]}.{kind}'    # 仓库外的暂存文件
  dest = None
  try:
    if kind == "cover":
      download(t["url"], stage)
      size = stage.stat().st_size
      img_kind = sniff_image(stage)
      if size < MIN_COVER_BYTES:
        raise RuntimeError(f"too_small({size}B)")
      if img_kind is None:
        raise RuntimeError("not_image")
      normalize_cover(stage)
      dest = folder / "cover.jpg"
      shutil.move(str(stage), str(dest))
      audio = find_audio(folder)
      title = str(info.get("title", "") or "")
      author = str(info.get("author", "") or "")
      tagged = bool((info.get("files") or {}).get("tagsWritten"))
      embedded = False
      if audio is not None:
        # write_tags 是幂等的（先 delall 再写）：老归档在没封面时连标题/艺术家都没写，
        # 这里无脑补一遍，顺便兜住 info.json 说写了但实际没写的情况
        if title and author:
          tagged = write_tags(audio, title, author) or tagged
        embedded = embed_cover(audio, dest)
        print(f"    嵌入封面: {'成功' if embedded else '失败/不支持（已保留独立封面）'}")
      files = info.setdefault("files", {})
      files["cover"] = dest.name
      files["coverEmbedded"] = embedded
      files["tagsWritten"] = tagged
      info["picOk"] = True
      info["coverError"] = None
      info["refetchedCoverAt"] = now_iso()
    else:
      text = fetch_lyric_text(t["url"])
      ok_l, why = lyric_looks_ok(text)
      if not ok_l:
        raise RuntimeError(why)
      dest = folder / "lyric.lrc"
      dest.write_text(text, encoding="utf-8")
      files = info.setdefault("files", {})
      files["lyric"] = dest.name
      info["lrcOk"] = True
      info["lyricError"] = None
      info["refetchedLyricAt"] = now_iso()

    save_info(folder, info)
    fixed += 1
    print(f"[FIXED] {tag}: 已补齐 -> {dest.name}")
    t["_done"] = True
  except Exception as e:
    if dest is not None:
      dest.unlink(missing_ok=True)   # 别把补到一半的文件留在库里
    t["reason"] = clean_field(brief_error(e)) or "unknown"
    t["attempts"] += 1
    t["lastAttempt"] = now_iso()
    failed += 1
    left = MAX_ATTEMPTS - t["attempts"]
    print(f"[FAIL] {tag}: {t['reason']} / {e} （已尝试 {t['attempts']}/{MAX_ATTEMPTS}，剩余 {max(left, 0)} 次）")
  finally:
    stage.unlink(missing_ok=True)

# ---- 3) 重写 missing.txt（只保留仍然缺失的条目）---------------------------
remain = [t for t in targets if not t.get("_done")]
lines = [HEADER.rstrip("\n")]
for t in sorted(remain, key=lambda x: (x["folder"], x["kind"])):
  lines.append("\t".join([
    clean_field(t["folder"]),
    t["kind"],
    clean_field(t["reason"]) or "unknown",
    str(t["attempts"]),
    clean_field(t["lastAttempt"]),
    clean_field(t["url"]),
  ]))
MISSING.write_text("\n".join(lines) + "\n", encoding="utf-8")

shutil.rmtree(WORK, ignore_errors=True)
print(f"补抓结果: 成功 {fixed} / 失败 {failed} / 放弃 {given_up} / 顺延 {deferred} / 无链接跳过 {skipped}")
print(f"missing.txt: 剩余 {len(remain)} 条待补")
retryable = [t for t in targets if t["url"].startswith("https://") and t["attempts"] < MAX_ATTEMPTS]
if retryable and failed > 0 and fixed == 0:
  raise SystemExit("存在可重试资源但本轮零成功；将 workflow 标红以触发运维通知")

