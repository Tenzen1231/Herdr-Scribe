import subprocess, sys, unittest
from pathlib import Path

MAC = Path(__file__).resolve().parents[1]

# A fresh interpreter, as at app start: nothing else may have imported CoreAudio yet.
PROBE = '''
import numpy as np
import CoreMedia as CM
import mac_app
status, fmt = CM.CMAudioFormatDescriptionCreate(
    None, (16000.0, 0x6C70636D, 9, 4, 1, 4, 1, 32, 0), 0, None, 0, None, None, None)
description = CM.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
print(len(mac_app.pcm(np.ones(1600, np.float32).tobytes(), description)))
'''


class CaptureFormatTests(unittest.TestCase):
    def test_format_description_is_readable_right_after_start(self):
        # 27.10.2026: with «Записывать и мой микрофон» already on, nothing imported CoreAudio,
        # the format came back as a bare tuple and every recording stopped at once.
        result = subprocess.run([sys.executable, '-c', PROBE], cwd=MAC, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-1], '1600')


if __name__ == '__main__':
    unittest.main()
