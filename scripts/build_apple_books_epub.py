#!/usr/bin/env python3
"""Build an Apple Books Read-Aloud compatible Fixed-Layout EPUB3 with word-by-word SMIL."""

import html
import json
import math
import re
import subprocess
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAPTERS_DIR = ROOT / "chapters"
BUILD_DIR = ROOT / "build"
AUDIO_MP3_DIR = BUILD_DIR / "audio"
AUDIO_M4A_DIR = BUILD_DIR / "audio_m4a"
DIST_DIR = ROOT / "dist"

PAGE_W = 768
PAGE_H = 1024
MAX_LINES_PER_PAGE = 26
CHARS_PER_LINE = 56


def format_smil_time(ticks_100ns: int) -> str:
    """Convert 100-nanosecond ticks into HH:MM:SS.mmm format."""
    total_ms = max(0, round(ticks_100ns / 10_000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def normalize_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_markdown_blocks(md_text: str):
    lines = md_text.strip().splitlines()
    title = "Chapter"
    blocks = []
    para_buf = []
    quote_buf = []

    def flush_para():
        if para_buf:
            blocks.append(("p", " ".join(para_buf).strip()))
            para_buf.clear()

    def flush_quote():
        if quote_buf:
            blocks.append(("blockquote", "\n".join(quote_buf).strip()))
            quote_buf.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and title == "Chapter":
            title = stripped[2:].strip()
            continue
        if stripped.startswith(">"):
            flush_para()
            quote_buf.append(stripped[1:].strip())
        elif stripped == "":
            flush_para()
            flush_quote()
        elif stripped.startswith("## "):
            flush_para()
            flush_quote()
            blocks.append(("h2", stripped[3:].strip()))
        else:
            flush_quote()
            para_buf.append(stripped)

    flush_para()
    flush_quote()
    return title, blocks


def render_inline_with_word_spans(text: str, word_counter: list, word_list: list) -> str:
    segments = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    out = []
    for seg in segments:
        if not seg:
            continue
        tag = None
        inner = seg
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            tag = "strong"
            inner = seg[2:-2]
        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
            tag = "em"
            inner = seg[1:-1]

        tokens = re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|[^\w\s]+|\s+", inner)
        seg_html = []
        for tok in tokens:
            if re.match(r"^[A-Za-z0-9]", tok):
                word_counter[0] += 1
                wid = f"w{word_counter[0]:05d}"
                word_list.append({"id": wid, "text": tok})
                seg_html.append(f'<span id="{wid}">{html.escape(tok)}</span>')
            else:
                seg_html.append(html.escape(tok))
        joined = "".join(seg_html)
        out.append(f"<{tag}>{joined}</{tag}>" if tag else joined)
    return "".join(out)


def estimate_block_lines(tag: str, raw: str) -> int:
    if tag == "h1":
        return 4
    if tag == "h2":
        return 3
    if tag == "blockquote":
        lines = raw.splitlines()
        return sum(max(1, math.ceil(len(ln) / (CHARS_PER_LINE - 6))) for ln in lines) + 2
    return max(1, math.ceil(len(raw) / CHARS_PER_LINE)) + 1


def split_long_paragraph(raw: str, max_lines: int) -> list:
    """Split a long paragraph at sentence boundaries if it exceeds max_lines."""
    if estimate_block_lines("p", raw) <= max_lines:
        return [raw]
    sentences = re.split(r"(?<=[.!?\"”])\s+", raw)
    chunks = []
    cur = []
    cur_len = 0
    max_chars = (max_lines - 1) * CHARS_PER_LINE
    for s in sentences:
        if cur and cur_len + len(s) + 1 > max_chars:
            chunks.append(" ".join(cur))
            cur = [s]
            cur_len = len(s)
        else:
            cur.append(s)
            cur_len += len(s) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def paginate_blocks(title: str, blocks: list) -> list:
    """Group chapter blocks into fixed-layout pages."""
    expanded = [("h1", title)]
    for tag, raw in blocks:
        if tag == "p":
            for piece in split_long_paragraph(raw, MAX_LINES_PER_PAGE - 2):
                expanded.append(("p", piece))
        else:
            expanded.append((tag, raw))

    pages = []
    cur_page = []
    cur_lines = 0
    for tag, raw in expanded:
        blines = estimate_block_lines(tag, raw)
        if cur_page and cur_lines + blines > MAX_LINES_PER_PAGE:
            pages.append(cur_page)
            cur_page = [(tag, raw)]
            cur_lines = blines
        else:
            cur_page.append((tag, raw))
            cur_lines += blines
    if cur_page:
        pages.append(cur_page)
    return pages


def align_words_to_events(words: list, events: list) -> list:
    aligned = []
    e_idx = 0
    n_events = len(events)
    last_end = 0

    for w in words:
        wnorm = normalize_token(w["text"])
        if e_idx >= n_events:
            aligned.append({"id": w["id"], "text": w["text"], "begin": last_end, "end": last_end + 1_500_000})
            last_end += 1_500_000
            continue

        matched_idx = None
        for probe in range(e_idx, min(n_events, e_idx + 6)):
            enorm = normalize_token(events[probe]["text"])
            if not enorm:
                continue
            if enorm == wnorm or (wnorm and (wnorm.startswith(enorm) or enorm.startswith(wnorm))):
                matched_idx = probe
                break

        if matched_idx is not None:
            start_tick = events[matched_idx]["offset"]
            end_tick = start_tick + max(events[matched_idx]["duration"], 500_000)
            cur_norm = normalize_token(events[matched_idx]["text"])
            next_idx = matched_idx + 1
            while (
                next_idx < n_events
                and len(cur_norm) < len(wnorm)
                and wnorm.startswith(cur_norm + normalize_token(events[next_idx]["text"]))
            ):
                cur_norm += normalize_token(events[next_idx]["text"])
                end_tick = events[next_idx]["offset"] + max(events[next_idx]["duration"], 500_000)
                next_idx += 1
            e_idx = next_idx
            last_end = end_tick
            aligned.append({"id": w["id"], "text": w["text"], "begin": start_tick, "end": end_tick})
        else:
            ev = events[e_idx]
            start_tick = max(last_end, ev["offset"])
            end_tick = start_tick + max(ev["duration"], 1_000_000)
            last_end = end_tick
            e_idx += 1
            aligned.append({"id": w["id"], "text": w["text"], "begin": start_tick, "end": end_tick})

    for i in range(len(aligned)):
        if aligned[i]["end"] <= aligned[i]["begin"]:
            aligned[i]["end"] = aligned[i]["begin"] + 500_000
        if i > 0 and aligned[i]["begin"] < aligned[i - 1]["end"]:
            if aligned[i]["begin"] > aligned[i - 1]["begin"]:
                aligned[i - 1]["end"] = aligned[i]["begin"]
            else:
                aligned[i]["begin"] = aligned[i - 1]["end"]
                if aligned[i]["end"] <= aligned[i]["begin"]:
                    aligned[i]["end"] = aligned[i]["begin"] + 500_000
    return aligned


def ensure_m4a(ch_num: int) -> Path:
    AUDIO_M4A_DIR.mkdir(parents=True, exist_ok=True)
    mp3_path = AUDIO_MP3_DIR / f"ch{ch_num:02d}.mp3"
    m4a_path = AUDIO_M4A_DIR / f"ch{ch_num:02d}.m4a"
    if m4a_path.exists() and m4a_path.stat().st_size > 1000:
        return m4a_path
    subprocess.run(
        ["afconvert", "-f", "m4af", "-d", "aac", "-c", "2", "-b", "64000", str(mp3_path), str(m4a_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return m4a_path


STYLE_CSS = f"""@charset "utf-8";
html, body {{
  margin: 0;
  padding: 0;
  width: {PAGE_W}px;
  height: {PAGE_H}px;
  overflow: hidden;
  background-color: #ffffff;
  color: #111827;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 18px;
  line-height: 1.55;
}}
.page {{
  box-sizing: border-box;
  width: {PAGE_W}px;
  height: {PAGE_H}px;
  padding: 58px 64px 54px 64px;
  position: relative;
}}
h1 {{
  font-size: 28px;
  line-height: 1.25;
  margin: 0 0 24px 0;
  text-align: center;
}}
h2 {{
  font-size: 22px;
  margin: 16px 0 12px 0;
}}
p {{
  margin: 0 0 14px 0;
  text-align: justify;
}}
blockquote {{
  margin: 12px 24px 16px 24px;
  padding-left: 14px;
  border-left: 3px solid #9ca3af;
  font-style: italic;
}}
.footer {{
  position: absolute;
  bottom: 24px;
  left: 64px;
  right: 64px;
  text-align: center;
  font-size: 13px;
  color: #6b7280;
  font-family: -apple-system, sans-serif;
}}
.-epub-media-overlay-active {{
  background-color: #fde047;
  color: #111827;
  border-radius: 3px;
}}
"""


COVER_XHTML = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width={PAGE_W}, height={PAGE_H}"/>
  <title>Alice’s Adventures in Wonderland (2026 Modern Edition)</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <div class="page" style="display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center;">
    <h1 style="font-size:38px; margin-bottom:16px;">Alice’s Adventures in Wonderland</h1>
    <p style="text-align:center; font-size:20px; color:#374151; margin-bottom:32px;">2026 Modern All-Ages Edition · Read Aloud</p>
    <p style="text-align:center; font-size:22px; font-weight:bold;">By Lewis Carroll</p>
    <p style="text-align:center; font-size:15px; color:#6b7280; margin-top:48px;">Public Domain Classic · Word-by-Word Highlighting</p>
  </div>
</body>
</html>
"""


def main():
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    epub_root = BUILD_DIR / "epub_fxl_src"
    if epub_root.exists():
        import shutil
        shutil.rmtree(epub_root)
    oebps = epub_root / "OEBPS"
    for sub in ["META-INF", "OEBPS/text", "OEBPS/smil", "OEBPS/audio", "OEBPS/styles"]:
        (epub_root / sub).mkdir(parents=True, exist_ok=True)

    (epub_root / "META-INF" / "container.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    (oebps / "styles" / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (oebps / "text" / "cover.xhtml").write_text(COVER_XHTML, encoding="utf-8")

    chapter_files = sorted(CHAPTERS_DIR.glob("ch*.md"))
    page_counter = 0
    all_pages = []
    toc_entries = []
    total_ticks = 0

    for ch_idx, ch_path in enumerate(chapter_files, 1):
        m4a_path = ensure_m4a(ch_idx)
        (oebps / "audio" / f"ch{ch_idx:02d}.m4a").write_bytes(m4a_path.read_bytes())
        events = json.loads((AUDIO_MP3_DIR / f"ch{ch_idx:02d}.events.json").read_text(encoding="utf-8"))

        title, blocks = parse_markdown_blocks(ch_path.read_text(encoding="utf-8"))
        pages_blocks = paginate_blocks(title, blocks)

        word_counter = [0]
        ch_words = []
        pages_xhtml_info = []

        for p_idx, p_blocks in enumerate(pages_blocks):
            page_counter += 1
            p_words_start = len(ch_words)
            body_parts = []
            for tag, raw in p_blocks:
                if tag == "h1":
                    body_parts.append(f"<h1>{render_inline_with_word_spans(raw, word_counter, ch_words)}</h1>")
                elif tag == "h2":
                    body_parts.append(f"<h2>{render_inline_with_word_spans(raw, word_counter, ch_words)}</h2>")
                elif tag == "blockquote":
                    q_lines = [
                        render_inline_with_word_spans(ln.strip(), word_counter, ch_words) if ln.strip() else ""
                        for ln in raw.splitlines()
                    ]
                    body_parts.append(f"<blockquote><p>{'<br/>'.join(q_lines)}</p></blockquote>")
                else:
                    body_parts.append(f"<p>{render_inline_with_word_spans(raw, word_counter, ch_words)}</p>")
            p_words_end = len(ch_words)
            pages_xhtml_info.append((page_counter, body_parts, p_words_start, p_words_end))

        aligned_ch_words = align_words_to_events(ch_words, events)

        for local_i, (p_num, body_parts, w_start, w_end) in enumerate(pages_xhtml_info):
            if local_i == 0:
                toc_entries.append((p_num, title))
            p_aligned = aligned_ch_words[w_start:w_end]
            p_id = f"p{p_num:03d}"

            xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width={PAGE_W}, height={PAGE_H}"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <div class="page">
    {chr(10).join("    " + b for b in body_parts)}
    <div class="footer">{p_num}</div>
  </div>
</body>
</html>
"""
            (oebps / "text" / f"{p_id}.xhtml").write_text(xhtml, encoding="utf-8")

            pars = []
            for k, item in enumerate(p_aligned, 1):
                cb = format_smil_time(item["begin"])
                ce = format_smil_time(item["end"])
                pars.append(
                    f'      <par id="par{k}">\n'
                    f'        <text src="../text/{p_id}.xhtml#{item["id"]}"/>\n'
                    f'        <audio src="../audio/ch{ch_idx:02d}.m4a" clipBegin="{cb}" clipEnd="{ce}"/>\n'
                    f'      </par>'
                )
            smil = f"""<?xml version="1.0" encoding="utf-8"?>
<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
  <body>
    <seq id="seq1" epub:textref="../text/{p_id}.xhtml">
{chr(10).join(pars)}
    </seq>
  </body>
</smil>
"""
            (oebps / "smil" / f"{p_id}.smil").write_text(smil, encoding="utf-8")
            p_dur_ticks = max(0, p_aligned[-1]["end"] - p_aligned[0]["begin"]) if p_aligned else 0
            total_ticks += p_dur_ticks
            all_pages.append({"p_id": p_id, "duration": format_smil_time(p_dur_ticks)})

        print(f"Chapter {ch_idx:02d}: {len(pages_xhtml_info)} fixed-layout pages, {len(aligned_ch_words)} words")

    nav_items = "\n".join(
        f'      <li><a href="text/p{p_num:03d}.xhtml">{html.escape(title)}</a></li>'
        for p_num, title in toc_entries
    )
    (oebps / "nav.xhtml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width={PAGE_W}, height={PAGE_H}"/>
  <title>Table of Contents</title>
  <link rel="stylesheet" type="text/css" href="styles/style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="text/cover.xhtml">Cover</a></li>
{nav_items}
    </ol>
  </nav>
</body>
</html>
""",
        encoding="utf-8",
    )

    book_uuid = str(uuid.uuid4())
    manifest_lines = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="styles/style.css" media-type="text/css"/>',
        '    <item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_lines = ['    <itemref idref="cover" properties="rendition:page-spread-center"/>']
    duration_metas = [f'    <meta property="media:duration">{format_smil_time(total_ticks)}</meta>']

    for ch_idx in range(1, len(chapter_files) + 1):
        manifest_lines.append(
            f'    <item id="aud_ch{ch_idx:02d}" href="audio/ch{ch_idx:02d}.m4a" media-type="audio/m4a"/>'
        )

    for p in all_pages:
        pid = p["p_id"]
        manifest_lines.append(
            f'    <item id="{pid}" href="text/{pid}.xhtml" media-type="application/xhtml+xml" media-overlay="smil_{pid}"/>'
        )
        manifest_lines.append(
            f'    <item id="smil_{pid}" href="smil/{pid}.smil" media-type="application/smil+xml"/>'
        )
        spine_lines.append(f'    <itemref idref="{pid}"/>')
        duration_metas.append(
            f'    <meta property="media:duration" refines="#smil_{pid}">{p["duration"]}</meta>'
        )

    (oebps / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/# ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>Alice’s Adventures in Wonderland (2026 Modern Edition)</dc:title>
    <dc:creator>Lewis Carroll</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-11-16T02:40:00Z</meta>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:orientation">auto</meta>
    <meta property="rendition:spread">auto</meta>
    <meta property="ibooks:specified-fonts">true</meta>
    <meta property="media:active-class">-epub-media-overlay-active</meta>
{chr(10).join(duration_metas)}
  </metadata>
  <manifest>
{chr(10).join(manifest_lines)}
  </manifest>
  <spine>
{chr(10).join(spine_lines)}
  </spine>
</package>
""",
        encoding="utf-8",
    )

    epub_out = DIST_DIR / "alice-in-wonderland-2026-word-by-word.epub"
    with zipfile.ZipFile(epub_out, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for path in sorted(epub_root.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(epub_root).as_posix()
                zf.write(path, arcname, compress_type=zipfile.ZIP_DEFLATED)

    print(f"Created Apple Books Fixed-Layout Read-Aloud EPUB3: {epub_out} ({epub_out.stat().st_size:,} bytes, {len(all_pages)} pages)")


if __name__ == "__main__":
    main()
