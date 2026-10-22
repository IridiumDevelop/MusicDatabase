#!/usr/bin/env python3
"""Audit all song.* payloads in a checked-out MusicDatabase tree.

Safe by default: only reports invalid payloads. --delete-invalid removes only
invalid song.* files; it never deletes folders or commits changes.
"""
import argparse
from pathlib import Path
import sys

from audio_validation import sniff_audio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="MusicDatabase checkout")
    ap.add_argument("--delete-invalid", action="store_true", help="delete invalid song.* files after reporting")
    args = ap.parse_args()
    try:
        from mutagen import File as mutagen_file
    except ImportError:
        raise SystemExit("mutagen is required: python -m pip install mutagen")

    root = Path(args.root)
    checked = invalid = removed = 0
    for audio in sorted(root.glob("*/song.*")):
        if not audio.is_file():
            continue
        checked += 1
        signature = sniff_audio(audio)
        try:
            media = mutagen_file(str(audio))
            duration = float(media.info.length) if media and getattr(media, "info", None) else 0
        except Exception:
            duration = 0
        if signature is None or duration <= 0:
            invalid += 1
            print(f"INVALID\t{audio.parent.name}\t{audio.name}\tsignature={signature}\tduration={duration}")
            if args.delete_invalid:
                audio.unlink()
                removed += 1
    print(f"checked={checked} invalid={invalid} removed={removed}")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
