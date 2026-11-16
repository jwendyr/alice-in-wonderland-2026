# Read-Along EPUB3 Production Pipeline & Lessons Learned

This document records the complete end-to-end architecture and technical fixes used to produce **Alice's Adventures in Wonderland (2026 Modern Edition)** as a retail-grade Apple Books Read-Aloud Fixed-Layout EPUB3 + Interactive Web Reader.

---

## 1. Architecture Overview

1. **Source Extraction (`original/`)**: Public domain text from Project Gutenberg (#11), split into 12 chapters (`ch01.txt`–`ch12.txt`).
2. **2026 Modernization (`chapters/ch01.md`–`ch12.md`, `BOOK.md`)**:
   - Unabridged 100% translation into clear, natural 2026 English for all ages.
   - Poems/songs formatted as full rhythmic verse lines inside `>` blockquotes (avoiding 2–3 word chopped concrete-poem lines that break TTS pacing and page layout).
3. **Cover & Frameless Etching Illustrations (`images/`)**:
   - `cover.jpg` (text-free artwork; crisp typography rendered in `cover.xhtml`).
   - `ch01.png`–`ch12.png` converted to **true transparent RGBA PNGs** (`alpha=0` outside ink, outer scanned paper-cut edges erased) so illustrations blend seamlessly onto White, Sepia, and Dark reader themes with zero rectangular border or paper edge.
4. **16-Voice Theatrical Audio (`scripts/synthesize_multivoice.py`)**:
   - Splits narration vs. quoted `"..."` dialogue and assigns distinct neural voices to the Narrator (`en-GB-SoniaNeural`) and 15 character roles (`en-GB-MaisieNeural`, `en-US-RogerNeural`, `en-GB-RyanNeural`, `en-GB-ThomasNeural`, `en-US-AndrewMultilingualNeural`, `en-US-AriaNeural`, `en-US-ChristopherNeural`, `en-US-GuyNeural`, `en-US-AnaNeural`, etc.).
   - Punctuation-only segments are filtered out before calling `edge-tts` (preventing `NoAudioReceived`).
   - Segments are decoded to `24 kHz` mono PCM WAV via `afconvert`, stitched with `80 ms` breath gaps, shifted by exact cumulative PCM frame counts for sample-accurate `WordBoundary` offsets, and encoded to stereo AAC `.m4a` (`64 kbps`).
5. **Apple Books Fixed-Layout EPUB3 (`scripts/build_apple_books_epub.py`)**:
   - **Pre-paginated Fixed-Layout (`768×1024`)**: Required by Apple Books to display Read Aloud controls.
   - **Typography & Page Fill**: `20px` Georgia, `line-height: 1.6`, sentence-level paragraph splitting + line-level blockquote splitting, no `hyphens: auto`, trailing space before `<br/>` (`' <br/>\n'`) so copied text never glues words together.
   - **Front/Back Matter & Separate TOCs**:
     - `nav.xhtml` (`properties="nav"`, not in spine): clean chapter titles only so Apple Books' TOC sidebar never glues page numbers onto titles.
     - `text/toc.xhtml` (in spine): visual Table of Contents with right-aligned page numbers.
     - `cover.xhtml`, `title.xhtml`, `copyright.xhtml`, `colophon.xhtml`, and `toc.ncx`.
   - **Two-Level Highlighting & Gapless SMIL**:
     - `OEBPS/scripts/highlight.js` (`MutationObserver`, `properties="scripted"`) adds `.sent-active` (`#fef9c3`) to the parent `span.sent` whenever Apple Books highlights `span.word.-epub-media-overlay-active` (`#facc15`)—avoiding CSS4 `:has()` which fails EPUBCheck (`CSS-008`).
     - `difflib.SequenceMatcher` aligns words to `WordBoundary` events with `-80 ms` visual lead and zero cumulative drift.
     - Gapless SMIL (`clipEnd[i] == clipBegin[i+1]`) prevents Apple Books from seeking/stuttering between words.
6. **Validation**:
   - Verified with **W3C EPUBCheck 5.1.0**: `0 fatals / 0 errors / 0 warnings` (`xmlns="http://www.idpf.org/2007/opf"`, `media-type="audio/mp4"`).
