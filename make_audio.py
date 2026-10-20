"""Turns out/digest.txt into out/digest.mp3 with free Microsoft neural voices (edge-tts).

usage: python make_audio.py out/digest.txt out/digest.mp3
Never fails the build: on any error it prints a message, writes no mp3, and exits 0.
"""
import asyncio
import os
import sys

VOICE = os.environ.get("TTS_VOICE", "en-IN-NeerjaNeural")   # alternative: en-IN-PrabhatNeural (male)
RATE = os.environ.get("TTS_RATE", "+0%")


def chunks(text, limit=2500):
    out, cur = [], ""
    for para in text.split("\n"):
        if cur and len(cur) + len(para) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def speak(text, tries=3):
    import edge_tts
    last = None
    for i in range(tries):
        try:
            buf = bytearray()
            async for ev in edge_tts.Communicate(text, VOICE, rate=RATE).stream():
                if ev["type"] == "audio":
                    buf += ev["data"]
            if buf:
                return bytes(buf)
            last = "empty audio"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(2 * (i + 1))
    raise RuntimeError(last)


async def run(src, dst):
    text = open(src, encoding="utf-8").read()
    parts = chunks(text)
    data = b""
    for n, c in enumerate(parts, 1):
        print(f"  audio part {n}/{len(parts)}", file=sys.stderr)
        data += await speak(c)
    open(dst, "wb").write(data)
    print(f"audio written: {dst} ({len(data) // 1024} KB, voice {VOICE})", file=sys.stderr)


if __name__ == "__main__":
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"audio skipped: {e}", file=sys.stderr)
        if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
            os.remove(sys.argv[2])
    sys.exit(0)
