#!/usr/bin/env python3
"""Synthesize expressive multi-voice theatrical audiobook tracks for all 12 chapters
with sample-accurate concatenated WordBoundary timestamps."""

import asyncio
import json
import re
import subprocess
import wave
from pathlib import Path

import edge_tts
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_apple_books_epub as b

CHAPTERS_DIR = ROOT / "chapters"
BUILD_DIR = ROOT / "build"
SEG_DIR = BUILD_DIR / "mv_segments"
AUDIO_DIR = BUILD_DIR / "audio"
AUDIO_M4A_DIR = BUILD_DIR / "audio_m4a"
DOCS_AUDIO_DIR = ROOT / "docs" / "audio"

SAMPLE_RATE = 24000
GAP_FRAMES = int(SAMPLE_RATE * 0.08)  # 80ms crisp breath between segments
GAP_BYTES = b"\x00\x00" * GAP_FRAMES

VOICE_CAST = {
    "narrator": ("en-GB-SoniaNeural", "+0%", "+0Hz"),
    "alice": ("en-GB-MaisieNeural", "+2%", "+2Hz"),
    "rabbit": ("en-US-RogerNeural", "+12%", "+4Hz"),
    "mouse": ("en-GB-LibbyNeural", "+4%", "+3Hz"),
    "dodo": ("en-AU-WilliamMultilingualNeural", "-4%", "-2Hz"),
    "caterpillar": ("en-GB-ThomasNeural", "-10%", "-6Hz"),
    "duchess": ("en-AU-NatashaNeural", "+2%", "-2Hz"),
    "cheshire": ("en-US-AndrewMultilingualNeural", "-3%", "-2Hz"),
    "hatter": ("en-GB-RyanNeural", "+8%", "+3Hz"),
    "hare": ("en-US-BrianNeural", "+6%", "+2Hz"),
    "dormouse": ("en-US-AnaNeural", "-10%", "+4Hz"),
    "queen": ("en-US-AriaNeural", "+5%", "+5Hz"),
    "king": ("en-US-ChristopherNeural", "-4%", "-4Hz"),
    "gryphon": ("en-AU-WilliamMultilingualNeural", "+4%", "-3Hz"),
    "turtle": ("en-US-GuyNeural", "-8%", "-4Hz"),
    "other": ("en-US-EricNeural", "+0%", "+0Hz"),
}

CHAR_PATTERNS = [
    ("mock turtle", "turtle"),
    ("turtle", "turtle"),
    ("march hare", "hare"),
    ("hare", "hare"),
    ("mad hatter", "hatter"),
    ("hatter", "hatter"),
    ("dormouse", "dormouse"),
    ("white rabbit", "rabbit"),
    ("rabbit", "rabbit"),
    ("cheshire", "cheshire"),
    ("cat", "cheshire"),
    ("caterpillar", "caterpillar"),
    ("duchess", "duchess"),
    ("queen", "queen"),
    ("king", "king"),
    ("gryphon", "gryphon"),
    ("dodo", "dodo"),
    ("mouse", "mouse"),
    ("alice", "alice"),
    ("pigeon", "other"),
    ("lory", "other"),
    ("eaglet", "other"),
    ("duck", "other"),
    ("frog", "other"),
    ("fish", "other"),
    ("cook", "other"),
    ("knave", "other"),
    ("bill", "other"),
]


def detect_speaker(context_after: str, context_before: str, last_char: str) -> str:
    after_l = context_after.lower()
    before_l = context_before.lower()
    for text_window in (after_l[:110], before_l[-120:]):
        for kw, role in CHAR_PATTERNS:
            if re.search(rf"\b{re.escape(kw)}\b", text_window):
                return role
    return last_char if last_char != "narrator" else "alice"


def split_chapter_into_voice_segments(title: str, blocks: list) -> list:
    """Return list of (role, text) segments preserving 100% of chapter words."""
    segments = [("narrator", title)]
    last_char = "alice"

    for tag, raw in blocks:
        clean = re.sub(r"\*+", "", raw).strip()
        if not clean:
            continue
        if tag == "blockquote":
            # Poems/songs: keep narrator or last_char consistently
            segments.append(("narrator" if last_char == "narrator" else last_char, clean))
            continue

        parts = re.split(r'("[^"]+")', clean)
        for idx, part in enumerate(parts):
            txt = part.strip()
            if not txt or not re.search(r"[A-Za-z0-9]", txt):
                continue
            if txt.startswith('"') and txt.endswith('"') and len(txt) > 2:
                before = parts[idx - 1] if idx > 0 else ""
                after = parts[idx + 1] if idx + 1 < len(parts) else ""
                role = detect_speaker(after, before, last_char)
                last_char = role
                segments.append((role, txt))
            else:
                segments.append(("narrator", txt))
    return segments


async def synth_segment(sem: asyncio.Semaphore, ch_num: int, seg_idx: int, role: str, text: str):
    voice, rate, pitch = VOICE_CAST[role]
    seg_dir = SEG_DIR / f"ch{ch_num:02d}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = seg_dir / f"seg_{seg_idx:04d}.mp3"
    wav_path = seg_dir / f"seg_{seg_idx:04d}.wav"
    ev_path = seg_dir / f"seg_{seg_idx:04d}.json"

    if wav_path.exists() and ev_path.exists() and wav_path.stat().st_size > 100:
        return wav_path, json.loads(ev_path.read_text(encoding="utf-8"))

    async with sem:
        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
                events = []
                with open(mp3_path, "wb") as f:
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                        elif chunk["type"] == "WordBoundary":
                            events.append({
                                "text": chunk["text"],
                                "offset": chunk["offset"],
                                "duration": chunk["duration"],
                            })
                if mp3_path.stat().st_size > 0:
                    break
            except Exception as exc:
                if attempt == 2:
                    raise exc
                await asyncio.sleep(1.0 * (attempt + 1))

    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(mp3_path), str(wav_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ev_path.write_text(json.dumps(events), encoding="utf-8")
    return wav_path, events


async def process_chapter(ch_num: int, ch_path: Path):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_M4A_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    title, blocks = b.parse_markdown_blocks(ch_path.read_text(encoding="utf-8"))
    segs = split_chapter_into_voice_segments(title, blocks)

    sem = asyncio.Semaphore(4)
    tasks = [synth_segment(sem, ch_num, i, role, txt) for i, (role, txt) in enumerate(segs)]
    results = await asyncio.gather(*tasks)

    full_wav = BUILD_DIR / f"ch{ch_num:02d}_mv.wav"
    all_events = []
    cum_frames = 0

    with wave.open(str(full_wav), "wb") as out_w:
        out_w.setnchannels(1)
        out_w.setsampwidth(2)
        out_w.setframerate(SAMPLE_RATE)

        for idx, (wav_path, evs) in enumerate(results):
            cum_ticks = round(cum_frames * 10_000_000 / SAMPLE_RATE)
            for ev in evs:
                all_events.append({
                    "text": ev["text"],
                    "offset": ev["offset"] + cum_ticks,
                    "duration": ev["duration"],
                })
            with wave.open(str(wav_path), "rb") as in_w:
                frames = in_w.readframes(in_w.getnframes())
                n_frames = in_w.getnframes()
            out_w.writeframes(frames)
            cum_frames += n_frames
            if idx < len(results) - 1:
                out_w.writeframes(GAP_BYTES)
                cum_frames += GAP_FRAMES

    m4a_path = AUDIO_M4A_DIR / f"ch{ch_num:02d}.m4a"
    subprocess.run(
        ["afconvert", "-f", "m4af", "-d", "aac", "-c", "2", "-b", "64000", str(full_wav), str(m4a_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (DOCS_AUDIO_DIR / f"ch{ch_num:02d}.m4a").write_bytes(m4a_path.read_bytes())
    (AUDIO_DIR / f"ch{ch_num:02d}.events.json").write_text(json.dumps(all_events), encoding="utf-8")

    dur_s = cum_frames / SAMPLE_RATE
    print(f"Ch {ch_num:02d} multi-voice: {len(segs)} segments, {len(all_events)} words, {dur_s:.1f}s")


async def main():
    for ch_idx, ch_path in enumerate(sorted(CHAPTERS_DIR.glob("ch*.md")), 1):
        await process_chapter(ch_idx, ch_path)


if __name__ == "__main__":
    asyncio.run(main())
