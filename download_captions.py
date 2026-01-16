
import html
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import List, Tuple

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    # Optional dependency; script still works without .env support.
    pass

import yt_dlp
from yt_dlp.utils import DownloadError

BASE_DIR = Path(__file__).resolve().parent
INPUT_LIST = Path(os.getenv("INPUT_LIST", str(BASE_DIR / "youtube_video_links_test.txt"))).expanduser()
DEST_DIR = Path(os.getenv("DEST_DIR", str(BASE_DIR / "test"))).expanduser()

# Optional: tweak YouTube client/PO token/cookies/rate limit via envs.
# Defaults use yt-dlp builtin client (no impersonation).
PLAYER_CLIENT = os.getenv("YDL_PLAYER_CLIENT", "").strip()  # e.g., web, android, ios
PO_TOKEN = os.getenv("YDL_PO_TOKEN", "").strip()  # needed for android/ios if required
COOKIE_FILE = os.getenv("YOUTUBE_COOKIES")  # path to cookies.txt
COOKIES_FROM_BROWSER = os.getenv("YDL_COOKIES_FROM_BROWSER", "").strip()
RATE_LIMIT_RAW = os.getenv("YDL_RATELIMIT", "").strip()  # e.g., "1M" or "500K"

YDL_EXTRACTOR_ARGS: dict | None = None
if PLAYER_CLIENT or PO_TOKEN:
    args: dict = {"youtube": {}}
    if PLAYER_CLIENT:
        args["youtube"]["player_client"] = [PLAYER_CLIENT]
    if PO_TOKEN:
        args["youtube"]["po_token"] = [PO_TOKEN]
    YDL_EXTRACTOR_ARGS = args


def _parse_rate_limit(value: str) -> int | None:
    """Parse human-friendly rate limit like 500K/1M into integer bytes/sec."""

    if not value:
        return None
    value = value.strip().upper()
    match = re.match(r"^(\d+(?:\.\d+)?)([KMG]?)$", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    factor = 1
    if unit == "K":
        factor = 1024
    elif unit == "M":
        factor = 1024 ** 2
    elif unit == "G":
        factor = 1024 ** 3
    return int(number * factor)


RATE_LIMIT = _parse_rate_limit(RATE_LIMIT_RAW)

# Preferred subtitle languages; adjust as needed.
SUB_LANGS = ["en", "en-*"]


# Text-cleaning knobs for generated TXT.
# - TXT_WRITE_METADATA_JSON: write sidecar .json with title/date/link (default 1)
# - TXT_REMOVE_INLINE_FILLERS: remove filler phrases inside sentences (default 0)
# - TXT_FILLERS: comma-separated filler phrases to remove (optional)
# - TXT_FILLERS_FILE: path to a newline-separated filler phrase list (optional)
TXT_WRITE_METADATA_JSON = os.getenv("TXT_WRITE_METADATA_JSON", "1").strip() not in {"0", "false", "False"}
TXT_REMOVE_INLINE_FILLERS = os.getenv("TXT_REMOVE_INLINE_FILLERS", "0").strip() in {"1", "true", "True"}
TXT_FILLERS_RAW = os.getenv("TXT_FILLERS", "").strip()
TXT_FILLERS_FILE = os.getenv("TXT_FILLERS_FILE", "").strip()


DEFAULT_FILLERS = {
    "aha",
    "mmhm",
    "mhm",
    "yep",
    "uh",
    "um",
    "you know",
    "like",
}


def _load_fillers() -> set[str]:
    fillers = set(DEFAULT_FILLERS)
    if TXT_FILLERS_RAW:
        for item in TXT_FILLERS_RAW.split(","):
            item = item.strip().lower()
            if item:
                fillers.add(item)
    if TXT_FILLERS_FILE:
        p = Path(TXT_FILLERS_FILE)
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    fillers.add(line)
    return fillers


def _norm_for_compare(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s.strip()


def _strip_kind_language_prefix(line: str) -> str:
    # Handles cases like: "Kind: captions Language: en The guiding principle..."
    return re.sub(
        r"^\s*Kind:\s*captions\s+Language:\s*[-\w]+\s*",
        "",
        line,
        flags=re.IGNORECASE,
    ).strip()


def _remove_filler_lines(text: str, fillers: set[str]) -> str:
    kept: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            kept.append("")
            continue
        simplified = re.sub(r"[^A-Za-z ]+", "", line).strip().lower()
        # Conservative: drop only very short standalone filler lines.
        if simplified in fillers and len(simplified) <= 20:
            continue
        kept.append(raw)
    return "\n".join(kept)


def _remove_inline_fillers(text: str, fillers: set[str]) -> str:
    # Remove filler phrases only when they appear as whole words.
    # Example: "You know," -> "" (then whitespace is normalized later).
    out = text
    for phrase in sorted(fillers, key=len, reverse=True):
        if not phrase or " " not in phrase and len(phrase) <= 1:
            continue
        pattern = r"\b" + re.escape(phrase) + r"\b"
        out = re.sub(pattern, "", out, flags=re.IGNORECASE)
    return out


def _dedup_adjacent_sentences(text: str) -> str:
    # Remove consecutive duplicate sentences (common in captions).
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: List[str] = []
    prev_norm = ""
    for p in parts:
        s = p.strip()
        if not s:
            continue
        cur_norm = _norm_for_compare(s)
        if cur_norm and cur_norm == prev_norm:
            continue
        out.append(s)
        prev_norm = cur_norm
    return " ".join(out)


def clean_transcript_text(text: str) -> str:
    # Unicode normalize first.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove common file headers / metadata lines.
    # Keep these as JSON sidecar instead of polluting the plain TXT.
    cleaned_lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip("\ufeff").strip()
        if not line:
            cleaned_lines.append("")
            continue
        if line.startswith("// filepath:"):
            continue
        if re.match(r"^(Title|Date|Link):\s*", line, flags=re.IGNORECASE):
            continue
        line = _strip_kind_language_prefix(line)
        if not line:
            continue
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)

    fillers = _load_fillers()
    text = _remove_filler_lines(text, fillers)
    if TXT_REMOVE_INLINE_FILLERS:
        text = _remove_inline_fillers(text, fillers)

    # Normalize spaces per-line, then re-merge excessive blank lines.
    normed: List[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        normed.append(line)
    text = "\n".join(normed)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Sentence-level adjacent dedupe (does not cross paragraph breaks aggressively).
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    paragraphs = [_dedup_adjacent_sentences(p) for p in paragraphs]
    return "\n\n".join([p for p in paragraphs if p]).strip()


def parse_lines(path: Path) -> List[Tuple[int, str, str]]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "->" not in line:
            continue
        try:
            left, url = line.split("->", 1)
            url = url.strip()
            # left like "1. Title" -> split on first '.'
            num_part, title = left.split(".", 1)
            idx = int(num_part.strip())
            title = title.strip()
            items.append((idx, title, url))
        except Exception:
            continue
    return items


def slugify(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 80:
        name = name[:80]
    return name or "untitled"


def _convert_vtt_file(
    vtt_path: Path,
    title: str | None,
    upload_date: str | None,
    video_url: str | None,
) -> Path:
    text = vtt_to_text(vtt_path)
    text = clean_transcript_text(text)
    date_part = re.sub(r"[^0-9]", "", upload_date or "") or "unknown"
    safe_title = slugify(title or vtt_path.stem)
    out_txt = vtt_path.with_name(f"{safe_title}_{date_part}.txt")
    out_txt.write_text(text, encoding="utf-8")
    if TXT_WRITE_METADATA_JSON:
        meta = {
            "title": title or "",
            "upload_date": upload_date or "",
            "video_url": video_url or "",
            "source_vtt": str(vtt_path),
            "output_txt": str(out_txt),
        }
        out_json = out_txt.with_suffix(".json")
        out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        vtt_path.unlink()
    except OSError as exc:
        print(f"  Warning: could not delete VTT {vtt_path}: {exc}")
    return out_txt

def list_youtube_links(start: str) -> List[str]:
    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "forcejson": True,
    }
    if YDL_EXTRACTOR_ARGS:
        opts["extractor_args"] = YDL_EXTRACTOR_ARGS
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    if COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER
    if RATE_LIMIT:
        opts["ratelimit"] = RATE_LIMIT
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(start, download=False)

    entries = info.get("entries") or []
    if not entries:
        entries = [info]

    lines: List[str] = []
    for idx, entry in enumerate(entries, 1):
        vid = entry.get("id") or ""
        url = entry.get("url") or entry.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={vid}" if vid else ""
        )
        title = entry.get("title") or ""
        lines.append(f"{idx}. {title} -> {url}")
    return lines


def _normalize_line(line: str) -> str:
    """Strip timestamps/tags and collapse whitespace for a single VTT line."""

    line = line.strip("\ufeff").strip()
    if not line:
        return ""
    if line.upper().startswith("WEBVTT"):
        return ""
    if re.match(r"^\d+$", line):
        return ""
    if "-->" in line:
        return ""
    

    # Remove inline timestamp/tag fragments like <00:00:01.000><c>word</c>
    line = re.sub(r"<[^>]+>", " ", line)
    line = html.unescape(line)
    line = re.sub(r"\s+", " ", line).strip()
    return line


def vtt_to_text(vtt_path: Path) -> str:
    # Normalize and drop duplicate consecutive lines.
    cleaned: List[str] = []
    prev = ""
    for raw in vtt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = _normalize_line(raw)
        if not line:
            continue
        if line == prev:
            continue
        cleaned.append(line)
        prev = line

    # Re-flow into paragraphs: join consecutive lines until we hit sentence enders.
    paragraphs: List[str] = []
    buffer: List[str] = []
    for line in cleaned:
        buffer.append(line)
        if re.search(r"[.!?…]$", line):
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))

    return "\n".join(paragraphs)


def download_subs(video_url: str, out_dir: Path) -> tuple[Path | None, str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt",
        "subtitleslangs": SUB_LANGS,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "sleep_interval_requests": 1,
        "max_sleep_interval_requests": 3,
    }
    if YDL_EXTRACTOR_ARGS:
        ydl_opts["extractor_args"] = YDL_EXTRACTOR_ARGS
    if COOKIE_FILE:
        ydl_opts["cookiefile"] = COOKIE_FILE
    if COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER
    if RATE_LIMIT:
        ydl_opts["ratelimit"] = RATE_LIMIT
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
        except DownloadError as exc:
            print(f"  Subtitle download failed: {exc}")
            return None, "", ""
    if not info:
        print(f"  No info returned for {video_url}")
        return None, "", ""
    vid_id = info.get("id")
    title = info.get("title") or ""
    upload_date = info.get("upload_date") or info.get("release_date") or ""
    # Find downloaded vtt
    for lang in SUB_LANGS:
        vtt = out_dir / f"{vid_id}.{lang}.vtt"
        if vtt.exists():
            return vtt, title, upload_date
    # yt_dlp may normalize lang codes; fallback to any vtt with id prefix
    candidates = list(out_dir.glob(f"{vid_id}*.vtt"))
    if candidates:
        return candidates[0], title, upload_date

    # Log available subtitle tracks when VTT is missing.
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    if subs or autos:
        langs = sorted(set(subs.keys()) | set(autos.keys()))
        print(f"  Subtitles exist but not downloaded. Available: {', '.join(langs)}")
    else:
        print("  No subtitles advertised by YouTube for this video.")
    return None, title, upload_date


def main() -> None:
    start_link = os.getenv("YOUTUBE_START")
    if not start_link:
        start_link = input("请输入YouTube频道/播放列表/视频链接: ").strip()
    if not start_link:
        print("未提供链接，退出。")
        return

    print("拉取视频列表...")
    lines = list_youtube_links(start_link)
    INPUT_LIST.write_text("\n".join(lines), encoding="utf-8")
    print(f"已保存链接列表: {INPUT_LIST}")

    videos = parse_lines(INPUT_LIST)
    if not videos:
        print(f"No entries parsed from {INPUT_LIST}")
        return

    # Only process the first N entries. Set TOP_N=0/-1/all to process all.
    top_n_raw = os.getenv("TOP_N", "5").strip().lower()
    process_all = top_n_raw in {"0", "-1", "all"}
    if not process_all:
        try:
            top_n = int(top_n_raw)
        except ValueError:
            top_n = 5
        if top_n > 0:
            videos = videos[:top_n]

    info_map: dict[str, tuple[str, str, str]] = {}

    for idx, title, url in videos:
        print(f"Processing {idx}: {title}")
        vtt_path, video_title, upload_date = download_subs(url, DEST_DIR)
        if not vtt_path:
            print(f"  No subtitles found for {url}")
            continue
        vid_id = vtt_path.stem.split(".")[0]
        info_map[vid_id] = (video_title or title, upload_date, url)
        out_txt = _convert_vtt_file(vtt_path, video_title or title, upload_date, url)
        print(f"  Saved -> {out_txt}")

    # Fallback: convert any remaining VTT files that were downloaded but not processed.
    leftovers = sorted(DEST_DIR.glob("*.vtt"))
    if leftovers:
        print(f"Converting leftover VTT files: {len(leftovers)}")
        for vtt in leftovers:
            vid_id = vtt.stem.split(".")[0]
            title, upload_date, video_url = info_map.get(
                vid_id,
                (vid_id, "unknown", ""),
            )
            out_txt = vtt.with_name(f"{slugify(title)}_{re.sub(r'[^0-9]', '', upload_date) or 'unknown'}.txt")
            if out_txt.exists():
                continue
            _convert_vtt_file(vtt, title, upload_date, video_url)


if __name__ == "__main__":
    main()
