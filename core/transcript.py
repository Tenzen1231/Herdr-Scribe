"""Long recordings to a timestamped text file; audio stays in memory, only text hits the disk.

One worker per audio source. Audio is recognized in windows of about 30 s, cut
only after a finished phrase, so memory stays flat for hours of recording.
"""
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np

from dictate import CHUNK, RATE, _lock, clean_text, model_instance

WINDOW = 30 * RATE
# A phrase that ends this close to the window edge may still be going on.
TAIL = RATE // 2
# A longer pause starts a new line with its own timestamp.
PAUSE = 2 * RATE


def recognize(audio):
    with _lock:
        return clean_text(model_instance().recognize(audio, sample_rate=RATE))


def speech_regions(audio):
    from vad import speech_timestamps
    return speech_timestamps(audio, max_speech_s=CHUNK / RATE)


def lines(regions):
    """Group VAD regions into lines: at most CHUNK samples, split at long pauses."""
    groups = []
    for region in regions:
        if groups and region['start'] - groups[-1][1] < PAUSE and region['end'] - groups[-1][0] <= CHUNK:
            groups[-1][1] = region['end']
        else:
            groups.append([region['start'], region['end']])
    return groups


def stamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


class Transcriber:
    """Feed 16 kHz mono float32; on_phrase(label, seconds, text) gets each recognized phrase."""

    def __init__(self, label, on_phrase, recognize=recognize, detect=speech_regions, window=WINDOW):
        self.label = label
        self.on_phrase = on_phrase
        self.recognize = recognize
        self.detect = detect
        self.window = window
        self._queue = queue.SimpleQueue()
        self._cancelled = threading.Event()
        self._finished = threading.Event()
        self._sealed = False
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, samples):
        # Called from the audio thread: copying/queuing only.
        if not self._sealed and not self._cancelled.is_set():
            self._queue.put(np.asarray(samples, dtype=np.float32).reshape(-1).copy())

    def skip(self, samples):
        """The source went quiet without sending audio; keep timestamps on the clock."""
        if not self._sealed and samples > 0:
            self._queue.put(int(samples))

    def finish(self):
        if not self._sealed:
            self._sealed = True
            self._queue.put(None)
        self._finished.wait()
        if self._error is not None:
            raise self._error

    def cancel(self):
        self._cancelled.set()
        self._queue.put(None)

    def _run(self):
        self.buffer = np.zeros(0, dtype=np.float32)
        self.offset = 0  # samples of the recording before self.buffer
        try:
            while not self._cancelled.is_set():
                # Items: audio blocks, an int gap (skip) or None (finish).
                item = self._queue.get()
                incoming = []
                while isinstance(item, np.ndarray):
                    incoming.append(item)
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        item = ''
                if incoming:
                    self.buffer = np.concatenate([self.buffer, *incoming])
                if item is None:
                    self._process(final=True)
                    return
                if isinstance(item, int):
                    self._process(final=True)
                    self.offset += item
                elif len(self.buffer) >= self.window:
                    self._process(final=False)
        except Exception as error:
            self._error = error
        finally:
            self._finished.set()

    def _process(self, final):
        buffer = self.buffer
        if not len(buffer):
            return
        regions = self.detect(buffer)
        if final:
            done, cut = regions, len(buffer)
        else:
            done = [region for region in regions if region['end'] <= len(buffer) - TAIL]
            if done:
                cut = done[-1]['end']
            elif regions:
                cut = regions[0]['start']  # only the unfinished phrase: drop silence before it
            else:
                cut = len(buffer) - TAIL
        for start, end in lines(done):
            if self._cancelled.is_set():
                return
            text = self.recognize(buffer[start:end])
            if text:
                self.on_phrase(self.label, (self.offset + start) / RATE, text)
        self.buffer = buffer[cut:]
        self.offset += cut


class TranscriptFile:
    """Text file written as phrases arrive, so a crash keeps everything said so far."""

    def __init__(self, folder, title, labelled):
        self.started = time.localtime()
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime('%Y-%m-%d %H-%M', self.started)
        path = folder / f'{name}.txt'
        n = 2
        while path.exists():
            path = folder / f'{name} ({n}).txt'
            n += 1
        self.path = path
        self.header = (f'Стенограмма {time.strftime("%d.%m.%Y %H:%M", self.started)}\n'
                       f'Источник: {title}\n\n')
        self.labelled = labelled
        self.lines = []
        self.last = ''
        self._lock = threading.Lock()
        self.path.write_text(self.header, 'utf-8')

    def add(self, label, seconds, text):
        line = f'[{stamp(seconds)}] {label}: {text}' if self.labelled else f'[{stamp(seconds)}] {text}'
        with self._lock:
            self.lines.append((seconds, line))
            self.last = text
            with open(self.path, 'a', encoding='utf-8') as file:
                file.write(line + '\n')

    def close(self, duration):
        """Rewrite in time order (sources finish phrases at different moments)."""
        with self._lock:
            lines = [line for _, line in sorted(self.lines, key=lambda item: item[0])]
            body = '\n'.join(lines) if lines else '(речь не обнаружена)'
            text = f'{self.header}{body}\n\nДлительность записи: {stamp(duration)}\n'
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(text, 'utf-8')
            os.replace(temporary, self.path)
            return bool(lines)
