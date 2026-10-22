import tempfile
import unittest
from pathlib import Path

from scripts.audio_validation import sniff_audio


class AudioSignatureTests(unittest.TestCase):
    def check(self, data):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "song.bin"
            path.write_bytes(data)
            return sniff_audio(path)

    def test_reject_html_even_with_id3_header_and_large_tag(self):
        # A valid ID3v2 synchsafe size followed by an HTML page is not audio.
        payload = b"<!DOCTYPE html><html>error</html>"
        n = len(payload)
        synchsafe = bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])
        self.assertIsNone(self.check(b"ID3\x04\x00\x00" + synchsafe + payload))

    def test_accept_id3_and_mp3_frame_sync(self):
        payload = b"\xff\xfb\x90\x64" + b"\x00" * 32
        n = len(payload)
        synchsafe = bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])
        self.assertEqual(self.check(b"ID3\x04\x00\x00" + synchsafe + payload), "mp3")

    def test_accept_mp4_flac_ogg_and_wave(self):
        self.assertEqual(self.check(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8), "mp4")
        self.assertEqual(self.check(b"fLaC" + b"\x00" * 16), "flac")
        self.assertEqual(self.check(b"OggS" + b"\x00" * 16), "ogg")
        self.assertEqual(self.check(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 8), "wav")


if __name__ == "__main__":
    unittest.main()
