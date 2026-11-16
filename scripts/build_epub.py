#!/usr/bin/env python3
"""Build an EPUB3 with word-by-word SMIL Media Overlays and an interactive web reader."""

import asyncio
import html
import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent.parent
CHAPTERS_DIR = ROOT / "chapters"
BUILD_DIR = ROOT / "build"
AUDIO_DIR = BUILD_DIR / "audio"
DIST_DIR = ROOT / "dist"
DOCS_DIR = ROOT / "docs"
VOICE = "en-US-AriaNeural"


def format_smil_time(ticks_100ns: int) -> str:
    """Convert 100-nanosecond ticks from edge-tts into HH:MM:SS.mmm format."""
    total_ms = max(0, round(ticks_100ns / 10_000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def normalize_token(s: str) -> str:
    """Normalize a word token for fuzzy alignment between text and TTS events."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_markdown_blocks(md_text: str):
    """Parse chapter markdown into (title, list of (tag, raw_text)) blocks."""
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
            qline = stripped[1:].strip()
            if qline:
                quote_buf.append(qline)
            else:
                quote_buf.append("")
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
    """Convert basic markdown emphasis (*...*, **...**) and wrap each word in <span id='wN'>."""
    # Tokenize into bold/italic markers, words, punctuation, and whitespace
    # First split by markdown ** or * markers? Better: tokenize words & spaces while tracking * / ** state.
    # Strip markdown ** and * into HTML tags cleanly at segment level:
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
        if tag:
            out.append(f"<{tag}>{joined}</{tag}>")
        else:
            out.append(joined)
    return "".join(out)


def build_chapter_xhtml(ch_num: int, title: str, blocks: list):
    """Build XHTML body with word-level spans and return (xhtml_str, spoken_text, words)."""
    word_counter = [0]
    words = []
    body_parts = []

    # Include title in spoken text and word spans
    title_html = render_inline_with_word_spans(title, word_counter, words)
    body_parts.append(f"<h1>{title_html}</h1>")
    spoken_lines = [title]

    for tag, raw in blocks:
        if tag == "blockquote":
            q_lines = []
            for ln in raw.splitlines():
                if ln.strip():
                    q_lines.append(render_inline_with_word_spans(ln.strip(), word_counter, words))
                    spoken_lines.append(re.sub(r"\*+", "", ln.strip()))
                else:
                    q_lines.append("")
            inner_html = "<br/>\n".join(q_lines)
            body_parts.append(f"<blockquote><p>{inner_html}</p></blockquote>")
        elif tag == "h2":
            h_html = render_inline_with_word_spans(raw, word_counter, words)
            body_parts.append(f"<h2>{h_html}</h2>")
            spoken_lines.append(re.sub(r"\*+", "", raw))
        else:
            p_html = render_inline_with_word_spans(raw, word_counter, words)
            body_parts.append(f"<p>{p_html}</p>")
            spoken_lines.append(re.sub(r"\*+", "", raw))

    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
  <section epub:type="chapter">
    {chr(10).join("    " + p for p in body_parts)}
  </section>
</body>
</html>
"""
    spoken_text = "\n\n".join(spoken_lines)
    return xhtml, spoken_text, words


async def synthesize_chapter_audio(ch_num: int, spoken_text: str):
    """Synthesize MP3 and collect WordBoundary events using edge-tts."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    mp3_path = AUDIO_DIR / f"ch{ch_num:02d}.mp3"
    events_path = AUDIO_DIR / f"ch{ch_num:02d}.events.json"

    if mp3_path.exists() and events_path.exists() and mp3_path.stat().st_size > 1000:
        with open(events_path, "r", encoding="utf-8") as f:
            return mp3_path, json.load(f)

    comm = edge_tts.Communicate(spoken_text, VOICE, boundary="WordBoundary")
    events = []
    with open(mp3_path, "wb") as audio_file:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                events.append({
                    "text": chunk["text"],
                    "offset": chunk["offset"],
                    "duration": chunk["duration"],
                })

    with open(events_path, "w", encoding="utf-8") as f:
        json.dump(events, f)

    return mp3_path, events


def align_words_to_events(words: list, events: list):
    """Align XHTML word spans with edge-tts WordBoundary events."""
    aligned = []
    e_idx = 0
    n_events = len(events)
    last_end = 0

    for w in words:
        wnorm = normalize_token(w["text"])
        if e_idx >= n_events:
            aligned.append({
                "id": w["id"],
                "text": w["text"],
                "begin": last_end,
                "end": last_end + 1_500_000,
            })
            last_end += 1_500_000
            continue

        # Look ahead up to 5 events for a match
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
            # If word is compound/hyphenated, consume consecutive matching sub-events
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
            aligned.append({
                "id": w["id"],
                "text": w["text"],
                "begin": start_tick,
                "end": end_tick,
            })
        else:
            # Fallback: interpolate briefly without advancing event cursor far
            ev = events[e_idx]
            start_tick = max(last_end, ev["offset"])
            end_tick = start_tick + max(ev["duration"], 1_000_000)
            last_end = end_tick
            e_idx += 1
            aligned.append({
                "id": w["id"],
                "text": w["text"],
                "begin": start_tick,
                "end": end_tick,
            })

    # Ensure monotonic non-overlapping clipBegin <= clipEnd
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


def build_smil(ch_num: int, aligned_words: list) -> str:
    """Build EPUB3 SMIL 3.0 Media Overlay file for word-by-word highlighting."""
    pars = []
    for idx, item in enumerate(aligned_words, 1):
        cb = format_smil_time(item["begin"])
        ce = format_smil_time(item["end"])
        pars.append(
            f'      <par id="par{idx}">\n'
            f'        <text src="../text/ch{ch_num:02d}.xhtml#{item["id"]}"/>\n'
            f'        <audio src="../audio/ch{ch_num:02d}.mp3" clipBegin="{cb}" clipEnd="{ce}"/>\n'
            f'      </par>'
        )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
  <body>
    <seq id="seq1" epub:textref="../text/ch{ch_num:02d}.xhtml">
{chr(10).join(pars)}
    </seq>
  </body>
</smil>
"""


STYLE_CSS = """@charset "utf-8";
body {
  font-family: Georgia, "Times New Roman", serif;
  line-height: 1.75;
  margin: 1.5em auto;
  max-width: 38em;
  padding: 0 1em;
  color: #1f2937;
}
h1 {
  font-size: 1.75em;
  line-height: 1.25;
  margin-bottom: 1em;
  text-align: center;
}
h2 {
  font-size: 1.3em;
  margin-top: 1.5em;
}
p {
  margin: 0.9em 0;
  text-align: justify;
}
blockquote {
  margin: 1.2em 2em;
  padding-left: 1em;
  border-left: 3px solid #9ca3af;
  font-style: italic;
}
.-epub-media-overlay-active {
  background-color: #fde047;
  color: #111827;
  border-radius: 3px;
  padding: 0 2px;
}
"""


COVER_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Alice's Adventures in Wonderland (2026 Modern Edition)</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
  <style type="text/css">
    .cover { text-align: center; margin-top: 4em; }
    .subtitle { font-size: 1.1em; color: #4b5563; margin-top: 0.5em; }
    .author { font-size: 1.2em; margin-top: 2em; font-weight: bold; }
    .edition { font-size: 0.95em; color: #6b7280; margin-top: 3em; }
  </style>
</head>
<body>
  <section class="cover" epub:type="cover">
    <h1>Alice’s Adventures in Wonderland</h1>
    <p class="subtitle">2026 Modern All-Ages Edition with Word-by-Word Read-Aloud</p>
    <p class="author">By Lewis Carroll</p>
    <p class="edition">Public Domain Classic · Modernized into Clear, Everyday 2026 English</p>
  </section>
</body>
</html>
"""


WEB_READER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Alice in Wonderland (2026) — Word-by-Word Reader</title>
  <style>
    :root {
      --bg: #0f172a; --panel: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
      --accent: #38bdf8; --hl: #fde047; --hl-text: #0f172a; --border: #334155;
    }
    body.light {
      --bg: #f8fafc; --panel: #ffffff; --text: #0f172a; --muted: #64748b;
      --accent: #0284c7; --hl: #fde047; --hl-text: #0f172a; --border: #e2e8f0;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: system-ui, -apple-system, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.7;
    }
    header {
      position: sticky; top: 0; z-index: 20;
      background: var(--panel); border-bottom: 1px solid var(--border);
      padding: 0.75rem 1.25rem; display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between;
    }
    .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    select, button, input[type="range"] {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 0.75rem; font-size: 0.9rem; cursor: pointer;
    }
    button.primary { background: var(--accent); color: #0f172a; border-color: var(--accent); font-weight: 600; }
    main { max-width: 820px; margin: 1.5rem auto; padding: 0 1.25rem 4rem; }
    .rsvp-box {
      background: var(--panel); border: 1px solid var(--border); border-radius: 16px;
      padding: 2.5rem 1.5rem; text-align: center; margin-bottom: 1.5rem;
      display: none;
    }
    .rsvp-box.active { display: block; }
    .rsvp-word { font-size: clamp(2rem, 6vw, 3.5rem); font-weight: 700; letter-spacing: 0.02em; min-height: 4.5rem; }
    .rsvp-meta { color: var(--muted); font-size: 0.9rem; margin-top: 0.5rem; }
    .chapter-card {
      background: var(--panel); border: 1px solid var(--border); border-radius: 16px;
      padding: 2rem; font-family: Georgia, serif; font-size: 1.15rem;
    }
    .chapter-card h1 { font-family: system-ui, sans-serif; text-align: center; margin-top: 0; }
    .chapter-card blockquote { border-left: 3px solid var(--accent); margin: 1.2rem 1rem; padding-left: 1rem; font-style: italic; }
    .w { cursor: pointer; border-radius: 4px; transition: background 0.08s; }
    .w:hover { background: rgba(56, 189, 248, 0.2); }
    .w.active { background: var(--hl); color: var(--hl-text); padding: 0 2px; }
    .hint { color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; text-align: center; }
  </style>
</head>
<body>
  <header>
    <div><strong>Alice in Wonderland (2026)</strong></div>
    <div class="controls">
      <select id="chapterSelect"></select>
      <select id="modeSelect">
        <option value="sync">Read-Aloud (Word-by-Word)</option>
        <option value="rsvp">RSVP Speed Reader</option>
      </select>
      <button id="playBtn" class="primary">Play</button>
      <label id="wpmWrap" style="display:none; font-size:0.85rem;">
        WPM: <span id="wpmVal">260</span>
        <input id="wpmRange" type="range" min="120" max="600" step="20" value="260" />
      </label>
      <button id="themeBtn">Theme</button>
    </div>
  </header>
  <main>
    <div class="hint">Click any word in the text to jump directly to that word.</div>
    <div id="rsvpBox" class="rsvp-box">
      <div id="rsvpWord" class="rsvp-word">Ready</div>
      <div id="rsvpMeta" class="rsvp-meta">Word 0 / 0</div>
    </div>
    <article id="reader" class="chapter-card"></article>
  </main>
  <audio id="audioEl"></audio>

  <script>
    let bookData = null;
    let currentChapter = 0;
    let currentWords = [];
    let activeIdx = -1;
    let rsvpTimer = null;
    let rsvpPlaying = false;

    const chapterSelect = document.getElementById('chapterSelect');
    const modeSelect = document.getElementById('modeSelect');
    const playBtn = document.getElementById('playBtn');
    const wpmWrap = document.getElementById('wpmWrap');
    const wpmRange = document.getElementById('wpmRange');
    const wpmVal = document.getElementById('wpmVal');
    const rsvpBox = document.getElementById('rsvpBox');
    const rsvpWord = document.getElementById('rsvpWord');
    const rsvpMeta = document.getElementById('rsvpMeta');
    const reader = document.getElementById('reader');
    const audioEl = document.getElementById('audioEl');

    function setActiveWord(idx, scroll = true) {
      if (activeIdx >= 0 && currentWords[activeIdx]) {
        currentWords[activeIdx].el.classList.remove('active');
      }
      activeIdx = idx;
      if (idx >= 0 && currentWords[idx]) {
        const w = currentWords[idx];
        w.el.classList.add('active');
        rsvpWord.textContent = w.text;
        rsvpMeta.textContent = `Word ${idx + 1} / ${currentWords.length}`;
        if (scroll) w.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }

    function stopAll() {
      audioEl.pause();
      clearInterval(rsvpTimer);
      rsvpPlaying = false;
      playBtn.textContent = 'Play';
    }

    function loadChapter(idx) {
      stopAll();
      currentChapter = idx;
      const ch = bookData.chapters[idx];
      reader.innerHTML = ch.html;
      audioEl.src = ch.audio;
      currentWords = ch.words.map(w => ({
        ...w,
        el: document.getElementById(w.id)
      })).filter(w => w.el);

      currentWords.forEach((w, i) => {
        w.el.classList.add('w');
        w.el.onclick = () => {
          setActiveWord(i, false);
          if (modeSelect.value === 'sync') {
            audioEl.currentTime = w.beginSec;
            audioEl.play();
            playBtn.textContent = 'Pause';
          }
        };
      });
      setActiveWord(0, false);
    }

    audioEl.addEventListener('timeupdate', () => {
      if (modeSelect.value !== 'sync') return;
      const t = audioEl.currentTime;
      // Binary search for active word
      let lo = 0, hi = currentWords.length - 1, found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (currentWords[mid].beginSec <= t) { found = mid; lo = mid + 1; }
        else { hi = mid - 1; }
      }
      if (found >= 0 && found !== activeIdx) setActiveWord(found, true);
    });

    audioEl.addEventListener('ended', () => { playBtn.textContent = 'Play'; });

    function startRsvp() {
      const intervalMs = 60000 / Number(wpmRange.value);
      rsvpPlaying = true;
      playBtn.textContent = 'Pause';
      rsvpTimer = setInterval(() => {
        const next = activeIdx + 1;
        if (next >= currentWords.length) { stopAll(); return; }
        setActiveWord(next, false);
      }, intervalMs);
    }

    playBtn.onclick = () => {
      if (modeSelect.value === 'sync') {
        if (audioEl.paused) { audioEl.play(); playBtn.textContent = 'Pause'; }
        else { audioEl.pause(); playBtn.textContent = 'Play'; }
      } else {
        if (rsvpPlaying) stopAll();
        else startRsvp();
      }
    };

    modeSelect.onchange = () => {
      stopAll();
      const isRsvp = modeSelect.value === 'rsvp';
      rsvpBox.classList.toggle('active', isRsvp);
      wpmWrap.style.display = isRsvp ? 'inline-flex' : 'none';
    };

    wpmRange.oninput = () => {
      wpmVal.textContent = wpmRange.value;
      if (rsvpPlaying) { clearInterval(rsvpTimer); startRsvp(); }
    };

    chapterSelect.onchange = () => loadChapter(Number(chapterSelect.value));
    document.getElementById('themeBtn').onclick = () => document.body.classList.toggle('light');

    fetch('data/book.json')
      .then(r => r.json())
      .then(data => {
        bookData = data;
        data.chapters.forEach((ch, i) => {
          const opt = document.createElement('option');
          opt.value = i;
          opt.textContent = ch.title;
          chapterSelect.appendChild(opt);
        });
        loadChapter(0);
      });
  </script>
</body>
</html>
"""


async def main():
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "audio").mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "data").mkdir(parents=True, exist_ok=True)

    chapter_files = sorted(CHAPTERS_DIR.glob("ch*.md"))
    if not chapter_files:
        raise RuntimeError("No chapter markdown files found in chapters/")

    epub_root = BUILD_DIR / "epub_src"
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

    chapters_meta = []
    web_chapters = []
    total_ticks = 0

    for idx, ch_path in enumerate(chapter_files, 1):
        md_text = ch_path.read_text(encoding="utf-8")
        title, blocks = parse_markdown_blocks(md_text)
        xhtml_str, spoken_text, words = build_chapter_xhtml(idx, title, blocks)
        mp3_path, events = await synthesize_chapter_audio(idx, spoken_text)
        aligned = align_words_to_events(words, events)
        smil_str = build_smil(idx, aligned)

        (oebps / "text" / f"ch{idx:02d}.xhtml").write_text(xhtml_str, encoding="utf-8")
        (oebps / "smil" / f"ch{idx:02d}.smil").write_text(smil_str, encoding="utf-8")
        mp3_bytes = mp3_path.read_bytes()
        (oebps / "audio" / f"ch{idx:02d}.mp3").write_bytes(mp3_bytes)
        (DOCS_DIR / "audio" / f"ch{idx:02d}.mp3").write_bytes(mp3_bytes)

        ch_ticks = aligned[-1]["end"] if aligned else 0
        total_ticks += ch_ticks
        chapters_meta.append({
            "num": idx,
            "title": title,
            "duration": format_smil_time(ch_ticks),
        })

        # Extract <section ...>...</section> for web reader
        sec_match = re.search(r"<section[^>]*>(.*?)</section>", xhtml_str, re.DOTALL)
        inner_html = sec_match.group(1) if sec_match else ""
        web_chapters.append({
            "title": title,
            "audio": f"audio/ch{idx:02d}.mp3",
            "html": inner_html,
            "words": [
                {
                    "id": w["id"],
                    "text": w["text"],
                    "beginSec": round(w["begin"] / 10_000_000, 3),
                }
                for w in aligned
            ],
        })
        print(f"Built ch{idx:02d}: {len(aligned)} words, duration {format_smil_time(ch_ticks)}")

    # nav.xhtml
    nav_items = "\n".join(
        f'      <li><a href="text/ch{c["num"]:02d}.xhtml">{html.escape(c["title"])}</a></li>'
        for c in chapters_meta
    )
    (oebps / "nav.xhtml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
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

    # content.opf
    book_uuid = str(uuid.uuid4())
    manifest_lines = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="styles/style.css" media-type="text/css"/>',
        '    <item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_lines = ['    <itemref idref="cover"/>']
    duration_metas = [f'    <meta property="media:duration">{format_smil_time(total_ticks)}</meta>']

    for c in chapters_meta:
        n = c["num"]
        manifest_lines.append(
            f'    <item id="ch{n:02d}" href="text/ch{n:02d}.xhtml" media-type="application/xhtml+xml" media-overlay="smil_ch{n:02d}"/>'
        )
        manifest_lines.append(
            f'    <item id="smil_ch{n:02d}" href="smil/ch{n:02d}.smil" media-type="application/smil+xml"/>'
        )
        manifest_lines.append(
            f'    <item id="aud_ch{n:02d}" href="audio/ch{n:02d}.mp3" media-type="audio/mpeg"/>'
        )
        spine_lines.append(f'    <itemref idref="ch{n:02d}"/>')
        duration_metas.append(
            f'    <meta property="media:duration" refines="#smil_ch{n:02d}">{c["duration"]}</meta>'
        )

    (oebps / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>Alice’s Adventures in Wonderland (2026 Modern Edition)</dc:title>
    <dc:creator>Lewis Carroll</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-11-16T00:30:00Z</meta>
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

    # Write web reader assets
    (DOCS_DIR / "index.html").write_text(WEB_READER_HTML, encoding="utf-8")
    (DOCS_DIR / "data" / "book.json").write_text(
        json.dumps({"title": "Alice's Adventures in Wonderland (2026 Modern Edition)", "chapters": web_chapters}),
        encoding="utf-8",
    )
    print(f"Created EPUB: {epub_out} ({epub_out.stat().st_size:,} bytes)")
    print(f"Created Web Reader: {DOCS_DIR / 'index.html'}")


if __name__ == "__main__":
    asyncio.run(main())
