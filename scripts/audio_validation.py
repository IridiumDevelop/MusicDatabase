"""Audio container signature checks shared by the archive/refetch audit tooling."""
from pathlib import Path


def sniff_audio(path):
    """Return a recognized container/codec name, or None for non-audio/unknown bytes.

    MP3 files can carry large ID3v2 tags; calculate their synchsafe length and
    inspect the frame sync after the tag rather than assuming a short prefix.
    """
    try:
        p = Path(path)
        size = p.stat().st_size
        if size < 4:
            return None
        with p.open("rb") as f:
            head = f.read(16)
            if head.startswith(b"fLaC"):
                return "flac"
            if head.startswith(b"OggS"):
                return "ogg"
            if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
                return "wav"
            if len(head) >= 8 and head[4:8] == b"ftyp":
                return "mp4"
            if head.startswith(b"ID3") and len(head) >= 10:
                # ID3 size is a 4-byte synchsafe integer; reject malformed bytes.
                if any(b & 0x80 for b in head[6:10]):
                    return None
                tag_size = ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9])
                offset = 10 + tag_size + (10 if head[5] & 0x10 else 0)
                if offset + 2 <= size:
                    f.seek(offset)
                    frame = f.read(4)
                    if len(frame) >= 2 and frame[0] == 0xFF and frame[1] & 0xE0 == 0xE0:
                        return "mp3"
                    return None
            # MP3 without ID3; scan only the first 64 KiB for a plausible frame sync.
            f.seek(0)
            buf = f.read(min(size, 65536))
            for i in range(len(buf) - 1):
                if buf[i] == 0xFF and buf[i + 1] & 0xE0 == 0xE0:
                    return "mp3"
            # ADTS AAC syncword
            if len(buf) >= 2 and buf[0] == 0xFF and buf[1] & 0xF6 == 0xF0:
                return "aac"
    except (OSError, ValueError):
        return None
    return None
