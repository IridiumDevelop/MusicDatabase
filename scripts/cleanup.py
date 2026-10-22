import base64
import os
import re
import time
from pathlib import Path
import requests

REPO = os.environ["GH_REPO"]
TOKEN = os.environ["GH_TOKEN"]
JSON_DIR = os.environ.get("JSON_DIR", "").strip().strip("/")
SRC = Path(JSON_DIR) if JSON_DIR else Path(".")

H = {
  "Authorization": f"Bearer {TOKEN}",
  "Accept": "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
}
H_RAW = {**H, "Accept": "application/vnd.github.raw"}
API = f"https://api.github.com/repos/{REPO}"
SONG_RE = re.compile(r"^[^/]+/song\.[a-z0-9]{2,5}$", re.IGNORECASE)

# 1) 遍历 main：一次 trees API 拿到全库文件清单（不下载二进制，库再大也很快），
#    找出「顶层目录下含有 song.* 音频」的目录 = 已收集集合
song_dirs = set()
r = requests.get(f"{API}/git/trees/main?recursive=1", headers=H, timeout=60)
r.raise_for_status()
tree = r.json()
if tree.get("truncated"):
  # 极端规模兜底：退化为根目录清单（排除 .github）
  print("WARN: trees 返回被截断，退化为根目录清单模式")
  d = requests.get(f"{API}/contents?ref=main", headers=H, timeout=30)
  d.raise_for_status()
  song_dirs = {e["name"] for e in d.json() if e.get("type") == "dir" and e["name"] != ".github"}
else:
  for e in tree.get("tree", []):
    if e.get("type") == "blob" and SONG_RE.match(e.get("path", "")):
      top = e["path"].split("/")[0]
      if top != ".github":
        song_dirs.add(top)
print(f"main 已收集: {len(song_dirs)} 首")

# 2) 删除 goodurl 中已处理过的 JSON
jsons = sorted([p for p in SRC.glob("*.json") if p.is_file()])
print(f"goodurl JSON 数: {len(jsons)}")
deleted, kept = [], []
for jp in jsons:
  if jp.stem in song_dirs:
    jp.unlink()
    deleted.append(jp.name)
    print(f"[DEL] {jp.name} （main/{jp.stem}/ 已归档）")
  else:
    kept.append(jp.name)
print(f"清理结果: 删除 {len(deleted)} / 保留 {len(kept)}")

# 3) 生成已收集目录 → main/list.txt（每行一个【歌名-歌手】，纯文本）
content = "\n".join(sorted(song_dirs)) + "\n" if song_dirs else ""
old = requests.get(f"{API}/contents/list.txt?ref=main", headers=H_RAW, timeout=30)
if old.status_code == 200 and old.text == content:
  print("list.txt 无变化，跳过更新")
else:
  # 取现有 sha（404 表示首次创建），带冲突重试
  new_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
  ok = False
  for attempt in range(3):
    sha = None
    meta = requests.get(f"{API}/contents/list.txt?ref=main", headers=H, timeout=30)
    if meta.status_code == 200:
      sha = meta.json().get("sha")
    body = {
      "message": f"chore: 更新已收集目录 ({len(song_dirs)} 首)",
      "content": new_b64,
      "branch": "main",
      "author": {
        "name": "lingyicute",
        "email": "160479206+lingyicute@users.noreply.github.com",
      },
      "committer": {
        "name": "lingyicute",
        "email": "160479206+lingyicute@users.noreply.github.com",
      },
    }
    if sha:
      body["sha"] = sha
    put = requests.put(f"{API}/contents/list.txt", headers=H, json=body, timeout=60)
    if put.status_code in (200, 201):
      ok = True
      print(f"list.txt 已更新（{len(song_dirs)} 首）")
      break
    if put.status_code in (409, 422):
      print(f"list.txt 写入冲突，重试 {attempt + 1}/3...")
      time.sleep(3)
      continue
    put.raise_for_status()
  if not ok:
    raise RuntimeError("list.txt 写入冲突，重试后仍失败")

