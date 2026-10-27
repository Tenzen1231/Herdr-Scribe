import sys, tempfile, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript import RATE, TranscriptFile, Transcriber, stamp


def regions(audio):
    active = np.flatnonzero(audio)
    if not len(active):
        return []
    boundaries = np.flatnonzero(np.diff(active) > 1)
    starts = np.r_[active[0], active[boundaries + 1]]
    ends = np.r_[active[boundaries] + 1, active[-1] + 1]
    return [dict(start=int(a), end=int(b)) for a, b in zip(starts, ends)]


def words(audio):
    # Each phrase is a run of one value: 1 -> «слово1».
    return ' '.join(f'слово{int(v)}' for v in np.unique(audio[audio != 0]))


class TranscriberTests(unittest.TestCase):
    def run_blocks(self, blocks, window=4 * RATE):
        phrases, calls = [], []
        def recognize(audio):
            calls.append(len(audio))
            return words(audio)
        t = Transcriber('Я', lambda *phrase: phrases.append(phrase), recognize, regions, window)
        for block in blocks:
            if isinstance(block, int):
                t.skip(block)
            else:
                for start in range(0, len(block), 1600):  # 100 ms blocks, like the audio thread
                    t.feed(block[start:start + 1600])
        t.finish()
        return phrases, calls

    def test_phrases_get_recording_timestamps_across_windows(self):
        audio = np.zeros(12 * RATE)
        audio[1 * RATE:2 * RATE] = 1
        audio[5 * RATE:6 * RATE] = 2
        audio[9 * RATE:10 * RATE] = 3
        phrases, _ = self.run_blocks([audio])
        self.assertEqual(phrases, [('Я', 1.0, 'слово1'), ('Я', 5.0, 'слово2'), ('Я', 9.0, 'слово3')])

    def test_phrase_across_window_edge_is_recognized_once_and_whole(self):
        audio = np.zeros(8 * RATE)
        audio[3 * RATE:5 * RATE] = 1  # crosses the 4 s window
        phrases, calls = self.run_blocks([audio])
        self.assertEqual(phrases, [('Я', 3.0, 'слово1')])
        self.assertEqual(calls, [2 * RATE])

    def test_short_pauses_stay_in_one_line_long_pauses_start_a_new_one(self):
        audio = np.zeros(12 * RATE)
        audio[1 * RATE:2 * RATE] = 1
        audio[3 * RATE:4 * RATE] = 2   # 1 s pause: same line
        audio[7 * RATE:8 * RATE] = 3   # 3 s pause: new line
        phrases, _ = self.run_blocks([audio], window=20 * RATE)
        self.assertEqual(phrases, [('Я', 1.0, 'слово1 слово2'), ('Я', 7.0, 'слово3')])

    def test_gap_keeps_clock_and_flushes_pending_audio(self):
        first = np.zeros(2 * RATE)
        first[RATE // 2:RATE] = 1
        second = np.zeros(2 * RATE)
        second[RATE:] = 2
        phrases, _ = self.run_blocks([first, 60 * RATE, second])
        self.assertEqual(phrases, [('Я', 0.5, 'слово1'), ('Я', 63.0, 'слово2')])

    def test_memory_stays_bounded_in_silence(self):
        t = Transcriber('Я', lambda *p: None, lambda a: '', regions, 4 * RATE)
        for _ in range(100):
            t.feed(np.zeros(RATE))
        t.finish()
        self.assertLess(len(t.buffer), 4 * RATE)
        self.assertEqual(t.offset + len(t.buffer), 100 * RATE)

    def test_errors_reach_finish(self):
        def broken(audio):
            raise RuntimeError('model')
        t = Transcriber('Я', lambda *p: None, broken, regions, 4 * RATE)
        t.feed(np.ones(RATE))
        with self.assertRaises(RuntimeError):
            t.finish()


class TranscriptFileTests(unittest.TestCase):
    def test_lines_are_saved_immediately_and_sorted_on_close(self):
        with tempfile.TemporaryDirectory() as folder:
            f = TranscriptFile(folder, 'звук компьютера и микрофон', labelled=True)
            f.add('Собеседники', 65, 'Привет.')
            f.add('Я', 3, 'Начинаем.')
            self.assertIn('[00:01:05] Собеседники: Привет.', f.path.read_text('utf-8'))
            self.assertTrue(f.close(3725))
            text = f.path.read_text('utf-8')
            self.assertLess(text.index('Начинаем'), text.index('Привет'))
            self.assertTrue(text.startswith('Стенограмма '))
            self.assertIn('Длительность записи: 01:02:05', text)
            self.assertEqual(f.last, 'Начинаем.')
            second = TranscriptFile(folder, 'звук компьютера', labelled=False)
            self.assertNotEqual(second.path, f.path)
            second.add('Я', 0, 'Текст.')
            self.assertIn('[00:00:00] Текст.', second.path.read_text('utf-8'))

    def test_stamp(self):
        self.assertEqual(stamp(0), '00:00:00')
        self.assertEqual(stamp(3599.9), '00:59:59')


if __name__ == '__main__':
    unittest.main()
