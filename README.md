# Alice's Adventures in Wonderland — 2026 Modern All-Ages Edition

A complete, unabridged modernization of Lewis Carroll's public-domain classic *Alice's Adventures in Wonderland* (1865, [Project Gutenberg #11](https://www.gutenberg.org/ebooks/11)) into natural, vivid **2026 English** that is easy and fun to read for all ages.

Includes:
- **Complete 12-chapter Markdown edition** (`BOOK.md` and `chapters/ch01.md`–`ch12.md`)
- **EPUB3 with Word-by-Word Read-Aloud (`dist/alice-in-wonderland-2026-word-by-word.epub`)** — built with **EPUB3 SMIL Media Overlays** (`clipBegin`/`clipEnd` per word) and neural narration (`en-US-AriaNeural`), enabling synchronized word-by-word highlighting in EPUB3 readers like Apple Books and Thorium Reader.
- **Interactive Web Word-by-Word Reader (`docs/index.html`)** — supports both **Read-Aloud Sync** (click any word to jump audio & highlight) and **RSVP Speed Reader** (adjustable 120–600 WPM).

## Structure

- `chapters/ch01.md` – `ch12.md` — Individual modernized chapters
- `BOOK.md` — Full combined book in Markdown
- `dist/alice-in-wonderland-2026-word-by-word.epub` — EPUB3 with SMIL word-by-word Media Overlays
- `docs/` — Standalone web word-by-word reader (ready for GitHub Pages)
- `scripts/build_epub.py` — Reproducible Python script to synthesize audio, align word boundaries, and package the EPUB3 + web reader
- `original/` — Original Project Gutenberg source text

## License

The original text of *Alice's Adventures in Wonderland* by Lewis Carroll is in the **Public Domain**.
