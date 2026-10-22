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
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_COVER_BYTES = 15 * 1024 * 1024
MAX_LYRIC_BYTES = 2 * 1024 * 1024
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

def allowed_source_url(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
        return (p.scheme == "https" and not p.username and not p.password
            and p.port in (None, 443)
            and any(host == d or host.endswith("." + d) for d in ALLOWED_HOST_SUFFIXES))
    except Exception:
        return False

def download(url, dest, timeout=120, max_bytes=MAX_AUDIO_BYTES):
    """有域名/重定向/字节上限的下载；每跳复核，超限立即终止并清 .part。"""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    current = url
    try:
        for _ in range(6):
            if not allowed_source_url(current):
                raise RuntimeError("URL host/protocol not allowed")
            r = SESSION.get(current, stream=True, timeout=timeout, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                r.close()
                if not loc: raise RuntimeError("redirect without Location")
                current = requests.compat.urljoin(current, loc)
                continue
            r.raise_for_status()
            length = r.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                r.close()
                raise RuntimeError(f"download exceeds limit ({length} > {max_bytes})")
            total = 0
            try:
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1024 * 64):
                        if not chunk: continue
                        total += len(chunk)
                        if total > max_bytes: raise RuntimeError(f"download exceeds limit ({total} > {max_bytes})")
                        f.write(chunk)
                ctype = r.headers.get("Content-Type", "")
            finally:
                r.close()
            part.rename(dest)
            return ctype
        raise RuntimeError("too many redirects")
    finally:
        part.unlink(missing_ok=True)

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
    path = Path(path)
    tmp = path.with_name(path.name + ".normalized.jpg")
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            bg.save(tmp, "JPEG", quality=88, optimize=True, progressive=True)
        tmp.replace(path)
        return True
    finally:
        tmp.unlink(missing_ok=True)

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

GOODURL = Path(os.environ.get("GOODURL_DIR", "goodurl"))
MAIN = Path(os.environ.get("MAIN_DIR", "main"))
import sys
sys.path.insert(0, str(MAIN / "scripts"))
from audio_validation import sniff_audio
JSON_DIR = os.environ.get("JSON_DIR", "").strip().strip("/")
SRC = GOODURL / JSON_DIR if JSON_DIR else GOODURL
MIN_AUDIO_BYTES = 50 * 1024  # 小于此值视为下载失败（可能是错误页）
MIN_DURATION = 60            # 服务端二次校验：时长必须 >60s
MISSING = []                 # (folder, kind, reason) —— 只用于日志汇总，状态记在 info.json
try:
  alias_data = json.loads((MAIN / "aliases.json").read_text(encoding="utf-8"))
  ALIASES = alias_data.get("aliases", {}) if alias_data.get("version") == 1 else {}
  if not isinstance(ALIASES, dict): ALIASES = {}
except Exception:
  ALIASES = {}

def ext_from_url(u, default):
    try:
        ext = os.path.splitext(unquote(urlparse(u).path))[1].lower()
        if re.fullmatch(r"\.[a-z0-9]{2,5}", ext):
            return ext
    except Exception:
        pass
    return default

def audio_duration(path):
    try:
        f = mutagen.File(str(path))
        if f is not None and getattr(f, "info", None) is not None:
            L = getattr(f.info, "length", 0) or 0
            return float(L)
    except Exception as e:
        print(f"    读取时长失败（后续 fail-closed）: {e}")
    return None

def process_one(json_path):
  try:
    record = json.loads(json_path.read_text(encoding="utf-8"))
  except Exception as e:
    print(f"[SKIP] {json_path.name}: JSON 解析失败: {e}")
    return "skip"
  title = str(record.get("title", "")).strip()
  author = str(record.get("author", "")).strip()
  pic = normalize_url(record.get("pic"))
  file_url = normalize_url(record.get("url"))
  lrc = normalize_url(record.get("lrc"))
  if not title or not author or not file_url.startswith("http"):
    print(f"[SKIP] {json_path.name}: 字段缺失/非法")
    return "skip"

  # 文件夹名直接沿用 JSON 文件名（不含 .json），保证与 cleanup 的对照逻辑一致
  folder_name = json_path.stem
  target = MAIN / folder_name
  alias_target = ALIASES.get(folder_name)
  if isinstance(alias_target, str) and alias_target != folder_name:
    alias_dir = MAIN / alias_target
    if alias_dir.is_dir() and any(p.suffix.lower() in AUDIO_EXTS and p.stat().st_size > MIN_AUDIO_BYTES for p in alias_dir.glob("song.*")):
      print(f"[SKIP] {folder_name}: aliases.json 指向已归档 {alias_target}")
      return "skip"

  # 已归档（文件夹存在且有音频文件）→ 跳过
  # 注意：这里只判断音频，缺封面/缺歌词的老文件夹由 cleanup 的 refetch 负责补
  if target.is_dir():
    for p in target.glob("song.*"):
      if p.suffix.lower() in AUDIO_EXTS and p.stat().st_size > MIN_AUDIO_BYTES:
        print(f"[SKIP] {folder_name}: main 已存在，跳过")
        return "skip"

  print(f"[PROC] {folder_name}")
  tmp = target.with_name(target.name + ".__tmp__")
  if tmp.exists():
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
  tmp.mkdir(parents=True, exist_ok=True)
  try:
    # 1) 下载歌曲文件
    audio_ext = ext_from_url(file_url, ".mp3")
    if audio_ext not in AUDIO_EXTS:
      audio_ext = ".mp3"
    audio_tmp = tmp / ("song" + audio_ext)
    print(f"    下载歌曲: {file_url[:90]}...")
    ctype = download(file_url, audio_tmp, max_bytes=MAX_AUDIO_BYTES)
    if audio_tmp.stat().st_size < MIN_AUDIO_BYTES:
      raise RuntimeError(f"歌曲文件过小({audio_tmp.stat().st_size}B)，可能已过期或被拒")
    audio_kind = sniff_audio(audio_tmp)
    if audio_kind is None:
      raise RuntimeError("audio signature not recognized; refusing non-audio payload")
    # 纠正扩展名：URL 没带扩展名时按内容推断
    if audio_kind == "mp4" and audio_ext not in (".m4a", ".mp4"):
      fixed = tmp / "song.m4a"
      audio_tmp.rename(fixed)
      audio_tmp = fixed
    dur = audio_duration(audio_tmp)
    if dur is not None and dur <= MIN_DURATION:
      raise RuntimeError(f"实际时长 {dur:.1f}s ≤ 60s，丢弃")
    if dur:
      print(f"    时长: {dur:.1f}s")
    elif dur is None:
      raise RuntimeError("mutagen 无法识别有效音频/时长，拒绝归档（fail-closed）")

    # 2) 下载专辑图（失败/无效只丢封面，歌曲继续归档）
    cover_file = None
    cover_err = None
    cover_bin = tmp / "cover.bin"
    if not pic.startswith("http"):
      cover_err = "no_url"
      print("    无封面 url，跳过封面")
    else:
      try:
        print(f"    下载封面: {pic[:90]}...")
        ctype_img = (download(pic, cover_bin, max_bytes=MAX_COVER_BYTES) or "").lower()
        size = cover_bin.stat().st_size
        kind = sniff_image(cover_bin)
        if size < MIN_COVER_BYTES:
          cover_err = f"too_small({size}B)"
          print(f"    封面过小({size}B)，丢弃封面")
        elif kind is None:
          # 典型情况：链接失效后服务器返回 200 + HTML 错误页
          cover_err = f"not_image({ctype_img or 'unknown-ctype'})"
          print(f"    封面不是有效图片(Content-Type={ctype_img or '未知'})，丢弃封面")
        else:
          if ctype_img and "image" not in ctype_img:
            print(f"    提示: Content-Type={ctype_img} 与魔数({kind})不符，按魔数保存")
          normalize_cover(cover_bin)
          cover_file = tmp / "cover.jpg"
          cover_bin.rename(cover_file)
          print(f"    封面: {cover_file.name} (normalized, {cover_file.stat().st_size}B)")
      except Exception as e:
        cover_err = brief_error(e)
        print(f"    封面下载失败（继续归档歌曲）: {cover_err} / {e}")
      finally:
        # 断流/异常时清掉半成品，避免被 rename 进正式文件夹提交到 main
        cover_bin.unlink(missing_ok=True)

    # 3) 下载歌词（失败/无效只丢歌词，歌曲继续归档）
    lyric_file = None
    lyric_err = None
    if not lrc.startswith("http"):
      lyric_err = "no_url"
      print("    无歌词 url，跳过歌词")
    else:
      try:
        print(f"    下载歌词: {lrc[:90]}...")
        text = fetch_lyric_text(lrc)
        ok_l, why = lyric_looks_ok(text)
        if ok_l:
          lyric_file = tmp / "lyric.lrc"
          lyric_file.write_text(text, encoding="utf-8")
          print(f"    歌词: lyric.lrc ({len(text)} 字符)")
        else:
          lyric_err = why
          print(f"    歌词不可用({why})，跳过歌词")
      except Exception as e:
        lyric_err = brief_error(e)
        print(f"    歌词下载失败（继续归档歌曲）: {lyric_err} / {e}")

    # 4) 写标题/艺术家 tag —— 与封面无关，缺封面也照样写
    tagged = write_tags(audio_tmp, title, author)
    print(f"    写标题/艺术家 tag: {'成功' if tagged else '失败/不支持'}")

    # 5) 嵌入封面（独立封面文件保留不删）
    embedded = False
    if cover_file is not None:
      embedded = embed_cover(audio_tmp, cover_file)
      print(f"    嵌入封面: {'成功' if embedded else '失败/不支持（已保留独立封面）'}")

    # 6) 写 info.json（pic/lrc 保留原始 URL，缺失原因单独记录）
    info = {
      "title": title,
      "author": author,
      "pic": pic,
      "picOk": cover_file is not None,
      "coverError": cover_err,
      "url": file_url,
      "lrc": lrc,
      "lrcOk": lyric_file is not None,
      "lyricError": lyric_err,
      "songmid": str(record.get("songmid", "")),
      "server": str(record.get("server", "")),
      "duration": record.get("duration"),
      "reportedAt": record.get("reportedAt"),
      "archivedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "files": {
        "audio": audio_tmp.name,
        "cover": cover_file.name if cover_file else None,
        "lyric": lyric_file.name if lyric_file else None,
        "coverEmbedded": embedded,
        "tagsWritten": tagged,
      },
    }
    (tmp / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 7) 落盘：tmp 原子替换为正式文件夹
    if target.exists():
      import shutil
      shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)

    if cover_file is None:
      MISSING.append((folder_name, "cover", cover_err or "unknown"))
    if lyric_file is None:
      MISSING.append((folder_name, "lyric", lyric_err or "unknown"))
    note = ""
    if cover_file is None or lyric_file is None:
      bits = []
      if cover_file is None:
        bits.append(f"缺封面({cover_err})")
      if lyric_file is None:
        bits.append(f"缺歌词({lyric_err})")
      note = " | " + "、".join(bits) + " → 已记入 info.json，等 cleanup 补抓"
    print(f"[OK] {folder_name} -> {audio_tmp.name}{note}")
    return "ok"
  except Exception as e:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[FAIL] {folder_name}: {e}")
    return "fail"

# 上一轮如果被硬中断（超时/OOM），可能留下 .__tmp__ 目录；先扫掉，
# 否则会被后面的 git add -A 提交进 main
import shutil
for stale in sorted(MAIN.glob("*.__tmp__")):
  shutil.rmtree(stale, ignore_errors=True)
  print(f"[CLEAN] 删除残留临时目录: {stale.name}")

jsons = sorted([p for p in SRC.glob("*.json") if p.is_file()])
print(f"goodurl 待处理 JSON: {len(jsons)} 个")
ok = fail = skip = 0
for jp in jsons:
  r = process_one(jp)
  if r == "ok":
    ok += 1
  elif r == "fail":
    fail += 1
  else:
    skip += 1
no_cover = sum(1 for _, k, _ in MISSING if k == "cover")
no_lyric = sum(1 for _, k, _ in MISSING if k == "lyric")
print(f"归档结果: 成功 {ok} / 失败 {fail} / 跳过 {skip}")
print(f"资源缺失: 缺封面 {no_cover} / 缺歌词 {no_lyric}")
for folder, kind, reason in MISSING:
  print(f"  [MISSING] {folder} {kind} {reason}")
# 个别歌曲失败/缺资源都不让 workflow 失败：
#   - 歌曲失败 → JSON 留在 goodurl，下一轮新上报覆盖后重试
#   - 缺封面/歌词 → 已归档，由 cleanup 的 refetch 每天补抓
if fail > 0:
  raise SystemExit(f"{fail} 首归档失败；保持队列并将 workflow 标红以触发通知")

