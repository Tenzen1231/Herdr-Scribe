"""macOS menu-bar recorder: computer sound (and optionally the microphone) to a text transcript.

Audio stays in memory; only recognized text is written, line by line, to a .txt file.
"""
import fcntl
import json
import plistlib
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import objc
# Not used by name: it registers AudioStreamBasicDescription, without which CoreMedia returns
# the audio format as a bare tuple and pcm() fails on the first buffer.
import CoreAudio  # noqa: F401
import CoreMedia as CM
import Quartz as Q
import ScreenCaptureKit as SCK
from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSMenu, NSMenuItem,
                    NSOpenPanel, NSPasteboard, NSPasteboardTypeString, NSStatusBar, NSVariableStatusItemLength,
                    NSWorkspace)
from dispatch import dispatch_queue_create
from Foundation import NSActivityUserInitiated, NSObject, NSProcessInfo, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))  # shared engine
from transcript import RATE, TranscriptFile, Transcriber, stamp

APP_SUPPORT = Path.home() / 'Library/Application Support/HerdrScribe'
LAUNCH_AGENT = Path.home() / 'Library/LaunchAgents/com.herdr.scribe.plist'
DEFAULT_FOLDER = Path.home() / 'Documents/Стенограммы'
SCREEN_SETTINGS = 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'
SYSTEM_LABEL, MIC_LABEL = 'Собеседники', 'Я'


def host_seconds():
    return CM.CMTimeGetSeconds(CM.CMClockGetTime(CM.CMClockGetHostTimeClock()))


def pcm(data, description):
    """Raw Core Audio bytes -> 16 kHz mono float32."""
    if description.mFormatFlags & 1:  # kAudioFormatFlagIsFloat
        samples = np.frombuffer(data, np.float32)
    else:
        samples = np.frombuffer(data, np.int16).astype(np.float32) / 32768
    channels = max(1, int(description.mChannelsPerFrame))
    if channels > 1:
        samples = samples[:len(samples) // channels * channels]
        if description.mFormatFlags & 32:  # non-interleaved: one plane per channel
            samples = samples.reshape(channels, -1).mean(0)
        else:
            samples = samples.reshape(-1, channels).mean(1)
    rate = float(description.mSampleRate)
    if rate and rate != RATE and len(samples):
        samples = np.interp(np.arange(0, len(samples), rate / RATE), np.arange(len(samples)), samples)
    return np.ascontiguousarray(samples, dtype=np.float32)


def error_text(error):
    if error is None:
        return ''
    try:
        return str(error.localizedDescription())
    except Exception:
        return str(error)


class StreamOutput(NSObject, protocols=[objc.protocolNamed('SCStreamOutput'),
                                         objc.protocolNamed('SCStreamDelegate')]):
    """ScreenCaptureKit callbacks on its own queue; forwards to SystemAudio."""
    def stream_didOutputSampleBuffer_ofType_(self, stream, buffer, kind):
        if kind == SCK.SCStreamOutputTypeAudio:
            self.owner.receive(buffer)

    def stream_didStopWithError_(self, stream, error):
        self.owner.failed(error_text(error) or 'захват звука остановлен системой')


class SystemAudio:
    """Everything the computer plays, via ScreenCaptureKit (macOS 13+); this app's own sound excluded."""

    def __init__(self, on_samples, on_gap, on_started, on_error):
        self.on_samples, self.on_gap = on_samples, on_gap
        self.on_started, self.on_error = on_started, on_error
        self.output = StreamOutput.alloc().init()
        self.output.owner = self
        self.queue = dispatch_queue_create(b'com.herdr.scribe.audio', None)
        self.stream = None
        self.stopped = False
        self.peak = 0.0
        self.clock = None

    def start(self):
        # Silence may come as no buffers at all: count it from the moment of the click.
        self.clock = host_seconds()
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, True, self._content)

    def _content(self, content, error):
        if self.stopped:
            return
        if error is not None or content is None or not content.displays():
            self.failed(error_text(error) or 'нет доступного экрана')
            return
        try:
            content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(content.displays()[0], [])
            config = SCK.SCStreamConfiguration.alloc().init()
            config.setCapturesAudio_(True)
            config.setExcludesCurrentProcessAudio_(True)
            config.setSampleRate_(RATE)
            config.setChannelCount_(1)
            # Video can't be switched off: the smallest picture once a second, thrown away.
            config.setWidth_(2)
            config.setHeight_(2)
            config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
            config.setShowsCursor_(False)
            self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
                content_filter, config, self.output)
            for kind in (SCK.SCStreamOutputTypeScreen, SCK.SCStreamOutputTypeAudio):
                ok, add_error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self.output, kind, self.queue, None)
                if not ok:
                    raise RuntimeError(error_text(add_error))
            self.stream.startCaptureWithCompletionHandler_(self._started)
        except Exception as exception:
            self.failed(str(exception))

    def _started(self, error):
        if error is not None:
            self.failed(error_text(error))
        else:
            self.on_started()

    def receive(self, buffer):
        if self.stopped:
            return
        try:
            count = CM.CMSampleBufferGetNumSamples(buffer)
            if not CM.CMSampleBufferIsValid(buffer) or count <= 0:
                return
            description = CM.CMAudioFormatDescriptionGetStreamBasicDescription(
                CM.CMSampleBufferGetFormatDescription(buffer))
            block = CM.CMSampleBufferGetDataBuffer(buffer)
            status, data = CM.CMBlockBufferCopyDataBytes(block, 0, CM.CMBlockBufferGetDataLength(block), None)
            if status:
                return
            samples = pcm(bytes(data), description)
            start = CM.CMTimeGetSeconds(CM.CMSampleBufferGetPresentationTimeStamp(buffer))
            gap = start - self.clock if self.clock is not None else 0
            if 1 < gap < 12 * 3600:
                self.on_gap(int(gap * RATE))
            elif 0.05 < gap <= 1:
                self.on_samples(np.zeros(int(gap * RATE), dtype=np.float32))
            self.clock = start + count / float(description.mSampleRate or RATE)
            if len(samples):
                self.peak = max(self.peak, float(np.max(np.abs(samples))))
                self.on_samples(samples)
        except Exception as exception:
            self.failed(f'не удалось прочитать звук: {exception}')

    def failed(self, text):
        if not self.stopped:
            self.on_error(text)

    def stop(self):
        self.stopped = True
        if self.stream is not None:
            self.stream.stopCaptureWithCompletionHandler_(lambda error: None)
            self.stream = None


class Target(NSObject):
    """Objective-C target for menu items and timers; forwards to Python."""
    def menuAction_(self, sender):
        callback = self.callbacks.get(sender.tag())
        if callback:
            callback()

    def tick_(self, timer):
        self.app.tick()

    def menuNeedsUpdate_(self, menu):
        self.app.fill_menu(menu)


class App:
    def __init__(self, interactive=True):
        import sounddevice as sd
        self.sd = sd
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        self.config_path = APP_SUPPORT / 'settings.json'
        try:
            self.config = json.loads(self.config_path.read_text('utf-8'))
        except (OSError, ValueError):
            self.config = {}
        self.ns = NSApplication.sharedApplication()
        self.ns.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.target = Target.alloc().init()
        self.target.app = self
        self.target.callbacks = {}
        self.state = 'idle'
        self.events = queue.Queue()
        self.generation = 0
        self.capture = self.mic_stream = self.transcript = self.activity = None
        self.tracks = []
        self.mic_peak = 0.0
        self.started = 0.0
        self.frame = 0
        self.error_until = 0
        self.last_error = ''
        self.folder = Path(self.config.get('folder') or DEFAULT_FOLDER)
        self.use_mic = self.config.get('use_mic', False)
        self.last_path = Path(self.config['last_path']) if self.config.get('last_path') else None
        self.refresh_devices()
        self.make_status_item()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self.target, 'tick:', None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        if interactive and not Q.CGPreflightScreenCaptureAccess():
            self.explain_screen_capture()

    # ---------- status item and menu ----------
    def make_status_item(self):
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status_symbol = None
        self.set_status('waveform', '')
        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self.target)
        self.menu.setAutoenablesItems_(False)
        self.status.setMenu_(self.menu)

    def set_status(self, symbol, title):
        if symbol != self.status_symbol:
            self.status_symbol = symbol
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, 'Herdr Стенограф')
            if image is not None:
                image.setTemplate_(True)
                self.status.button().setImage_(image)
            elif not title:
                title = 'HS'
        if str(self.status.button().title()) != title:
            self.status.button().setTitle_(title)

    def build_menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.fill_menu(menu)
        return menu

    def fill_menu(self, menu):
        menu.removeAllItems()
        callbacks = self.target.callbacks
        callbacks.clear()
        def add(parent, title, action=None, checked=False, enabled=True):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, 'menuAction:' if action else None, '')
            if action:
                tag = len(callbacks) + 1
                callbacks[tag] = action
                item.setTag_(tag)
                item.setTarget_(self.target)
            item.setEnabled_(bool(action) and enabled)
            item.setState_(1 if checked else 0)
            parent.addItem_(item)
            return item
        def submenu(parent, title):
            item = add(parent, title)
            item.setEnabled_(True)
            child = NSMenu.alloc().initWithTitle_(title)
            child.setAutoenablesItems_(False)
            item.setSubmenu_(child)
            return child
        if self.state == 'idle':
            add(menu, 'Начать стенограмму', self.start)
        elif self.state == 'starting':
            add(menu, 'Подключаюсь к звуку…')
        elif self.state == 'recording':
            add(menu, f'Остановить и сохранить   ({stamp(self.elapsed())})', self.stop)
            heard = self.transcript.last if self.transcript else ''
            add(menu, f'Последнее: {heard[:60]}…' if len(heard) > 60 else
                f'Последнее: {heard}' if heard else 'Жду речь…')
        else:
            add(menu, 'Сохраняю стенограмму…')
        last = self.last_path if self.last_path and self.last_path.exists() else None
        add(menu, 'Открыть последнюю стенограмму', (lambda: self.open_path(last)) if last else None)
        add(menu, 'Скопировать последнюю стенограмму', self.copy_last if last else None)
        add(menu, 'Показать папку стенограмм', lambda: self.open_path(self.folder))
        menu.addItem_(NSMenuItem.separatorItem())
        if not Q.CGPreflightScreenCaptureAccess():
            add(menu, 'Разрешить запись звука компьютера…', self.open_screen_settings)
        if self.last_error:
            add(menu, self.last_error)
        idle = self.state == 'idle'
        add(menu, 'Записывать и мой микрофон', self.toggle_mic, self.use_mic, enabled=idle)
        mics = submenu(menu, 'Микрофон')
        for index, name in self.devices:
            add(mics, name, lambda index=index: self.choose_device(index), index == self.device_index, enabled=idle)
        add(menu, f'Папка: {self.folder.name}…', self.choose_folder, enabled=idle)
        add(menu, 'Запускать при входе в систему', self.toggle_autostart, LAUNCH_AGENT.exists())
        menu.addItem_(NSMenuItem.separatorItem())
        add(menu, 'Выйти', self.quit)

    # ---------- settings ----------
    def save(self):
        self.config.update(folder=str(self.folder), use_mic=self.use_mic,
                           last_path=str(self.last_path) if self.last_path else None,
                           microphone=next((name for i, name in self.devices if i == self.device_index), None))
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), 'utf-8')

    def refresh_devices(self):
        devices = self.sd.query_devices()
        self.devices = [(i, d['name']) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
        default = self.sd.default.device[0]
        wanted = self.config.get('microphone')
        self.device_index = next((i for i, name in self.devices if name == wanted),
                                 default if default is not None and default >= 0 else None)

    def choose_device(self, index):
        self.device_index = index
        self.save()

    def toggle_mic(self):
        self.use_mic = not self.use_mic
        if self.use_mic:
            self.request_microphone()
        self.save()

    def choose_folder(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setCanCreateDirectories_(True)
        panel.setPrompt_('Выбрать')
        self.ns.activateIgnoringOtherApps_(True)
        if panel.runModal() == 1:
            self.folder = Path(str(panel.URL().path()))
            self.save()

    def toggle_autostart(self):
        try:
            if LAUNCH_AGENT.exists():
                LAUNCH_AGENT.unlink()
            else:
                set_autostart(app_bundle())
        except (OSError, RuntimeError):
            self.hint('Не удалось изменить автозапуск')

    def open_path(self, path):
        if path == self.folder:
            path.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(path)))

    def copy_last(self):
        try:
            text = self.last_path.read_text('utf-8')
        except OSError:
            self.hint('Файл стенограммы не найден')
            return
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, NSPasteboardTypeString)
        self.hint('Стенограмма скопирована')

    def open_screen_settings(self):
        Q.CGRequestScreenCaptureAccess()
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(SCREEN_SETTINGS))

    def explain_screen_capture(self):
        alert = NSAlert.alloc().init()
        alert.setMessageText_('Herdr Стенограф: нужна «Запись экрана и системного звука»')
        alert.setInformativeText_(
            'Через это разрешение macOS даёт программам слышать звук компьютера. '
            'Картинка экрана не сохраняется, звук не покидает компьютер, на диск пишется только текст.\n\n'
            'Откроются Системные настройки → Конфиденциальность и безопасность → '
            'Запись экрана. Включите HerdrScribe и согласитесь перезапустить программу.')
        alert.addButtonWithTitle_('Открыть настройки')
        alert.addButtonWithTitle_('Позже')
        self.ns.activateIgnoringOtherApps_(True)
        if alert.runModal() == 1000:
            self.open_screen_settings()

    def alert(self, title, text, button='OK', other=None):
        """Visible message: a menu-bar app has no window to show errors in."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.addButtonWithTitle_(button)
        if other:
            alert.addButtonWithTitle_(other)
        self.ns.activateIgnoringOtherApps_(True)
        return alert.runModal() == 1000

    def request_microphone(self):
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            if AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) == 0:  # not determined
                AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, lambda granted: None)
        except Exception:
            pass

    # ---------- recording ----------
    def hint(self, text):
        self.last_error = text
        self.error_until = time.monotonic() + 4

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0

    def start(self):
        if self.state != 'idle':
            return
        if not Q.CGPreflightScreenCaptureAccess():
            self.explain_screen_capture()
            return
        self.generation += 1
        generation = self.generation
        self.last_error = ''
        try:
            self.transcript = TranscriptFile(
                self.folder, 'звук компьютера и микрофон' if self.use_mic else 'звук компьютера', self.use_mic)
        except OSError:
            # macOS guards Documents/Desktop: a folder picked in the dialog is always allowed.
            self.hint('Нет доступа к папке для стенограмм')
            if self.alert('Herdr Стенограф: нет доступа к папке',
                          f'macOS не дала создать файл в папке «{self.folder}».\n\n'
                          'Выберите папку для стенограмм – запись начнётся сразу.',
                          'Выбрать папку…', 'Отмена'):
                previous = self.folder
                self.choose_folder()
                if self.folder != previous:
                    self.start()
            return
        def warmup():
            from dictate import warmup
            try:
                warmup()  # loads the model while capture is connecting
            except Exception:
                self.events.put(('error', (generation, 'Не удалось загрузить распознавание')))
        threading.Thread(target=warmup, daemon=True).start()
        system = Transcriber(SYSTEM_LABEL, self.transcript.add)
        self.tracks = [system]
        self.capture = SystemAudio(
            system.feed, system.skip,
            on_started=lambda: self.events.put(('started', generation)),
            on_error=lambda text: self.events.put(('error', (generation, f'Звук компьютера: {text}'))))
        self.state = 'starting'
        self.started = time.monotonic()
        self.capture.start()
        if self.use_mic:
            try:
                mic = Transcriber(MIC_LABEL, self.transcript.add)
                self.tracks.append(mic)
                self.mic_peak = 0.0
                def receive(data, frames, timing, status):
                    mic.feed(data)
                    self.mic_peak = max(self.mic_peak, float(np.max(np.abs(data))))
                self.mic_stream = self.sd.InputStream(device=self.device_index, samplerate=RATE, channels=1,
                                                      dtype='float32', blocksize=1600, callback=receive)
                self.mic_stream.start()
            except Exception:
                self.close_mic()
                self.hint('Микрофон недоступен · пишу только звук компьютера')
        # Keep the Mac awake during long meetings: sleep would stop the capture.
        self.activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiated, 'Herdr Стенограф: идёт запись')

    def close_mic(self):
        if self.mic_stream is not None:
            self.mic_stream.stop()
            self.mic_stream.close()
            self.mic_stream = None

    def stop(self, problem=''):
        if self.state not in ('starting', 'recording'):
            return
        self.close_mic()
        if self.capture:
            self.capture.stop()
        if self.activity is not None:
            NSProcessInfo.processInfo().endActivity_(self.activity)
            self.activity = None
        self.state = 'saving'
        generation, tracks, transcript = self.generation, self.tracks, self.transcript
        duration = self.elapsed()
        silent = (self.capture.peak if self.capture else 0) == 0 and self.mic_peak == 0
        def worker():
            failure = problem
            for track in tracks:
                try:
                    track.finish()
                except Exception:
                    failure = failure or 'Часть записи не распознана'
            try:
                spoken = transcript.close(duration)
            except OSError:
                spoken, failure = False, failure or 'Не удалось сохранить файл'
            self.events.put(('saved', (generation, transcript.path, spoken, silent, failure)))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- loop ----------
    def tick(self):
        self.frame += 1
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'started' and value == self.generation and self.state == 'starting':
                self.state = 'recording'
            elif kind == 'error' and value[0] == self.generation:
                if self.state in ('starting', 'recording'):
                    self.stop(value[1])
                else:
                    self.hint(value[1])
            elif kind == 'saved' and value[0] == self.generation:
                _, path, spoken, silent, failure = value
                self.state = 'idle'
                self.started = 0.0
                if spoken:
                    self.last_path = path
                    self.save()
                    self.hint(failure or 'Стенограмма сохранена')
                    NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(path))])
                else:
                    path.unlink(missing_ok=True)  # an empty transcript is of no use
                    self.hint(failure or ('Звука не было · проверьте разрешение «Запись экрана»' if silent else
                                          'Речь не обнаружена'))
                    if failure:
                        self.alert('Herdr Стенограф: запись не получилась', failure)
        recording = self.state in ('starting', 'recording')
        blink = recording and self.frame % 2 == 0
        if recording:
            self.set_status('record.circle.fill' if blink else 'record.circle', ' ' + stamp(self.elapsed()))
        elif self.state == 'saving':
            self.set_status('ellipsis.circle', '')
        else:
            self.set_status('exclamationmark.triangle' if time.monotonic() < self.error_until
                            and self.last_error != 'Стенограмма сохранена' else 'waveform', '')

    def quit(self):
        if self.state in ('starting', 'recording'):
            self.stop()
        deadline = time.monotonic() + 30
        while self.state == 'saving' and time.monotonic() < deadline:
            time.sleep(0.1)
            self.tick()
        self.save()
        self.ns.terminate_(None)


def app_bundle():
    # .../HerdrScribe.app/Contents/MacOS/HerdrScribe -> .../HerdrScribe.app
    executable = Path(sys.executable).resolve()
    if executable.parent.name == 'MacOS' and executable.parents[1].name == 'Contents':
        return executable.parents[2]
    raise RuntimeError('Автозапуск доступен только для HerdrScribe.app')


def set_autostart(bundle):
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps({
        'Label': 'com.herdr.scribe',
        'ProgramArguments': ['/usr/bin/open', '-a', str(bundle)],
        'RunAtLoad': True,
    }))


def diagnose(wav_path, out_path):
    """Headless check for CI: menu, capture classes and a transcript of a WAV file."""
    import tempfile
    import wave
    with wave.open(wav_path, 'rb') as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), np.int16).astype(np.float32) / 32768
        rate = audio.getframerate()
    samples = np.interp(np.arange(0, len(samples), rate / RATE), np.arange(len(samples)), samples).astype(np.float32)
    app = App(interactive=False)
    app.tick()
    menu = app.build_menu()
    SystemAudio(lambda s: None, lambda n: None, lambda: None, lambda t: None)
    # The format of a captured buffer, read the way receive() reads it.
    _, audio_format = CM.CMAudioFormatDescriptionCreate(
        None, (float(RATE), 0x6C70636D, 9, 4, 1, 4, 1, 32, 0), 0, None, 0, None, None, None)
    captured = len(pcm(np.ones(1600, np.float32).tobytes(),
                       CM.CMAudioFormatDescriptionGetStreamBasicDescription(audio_format)))
    with tempfile.TemporaryDirectory() as folder:
        transcript = TranscriptFile(folder, 'проверка', labelled=False)
        track = Transcriber(SYSTEM_LABEL, transcript.add)
        audio = np.concatenate([np.zeros(RATE * 2, np.float32), samples, np.zeros(RATE * 40, np.float32), samples])
        for start in range(0, len(audio), 1600):
            track.feed(audio[start:start + 1600])
        track.finish()
        transcript.close(len(audio) / RATE)
        text = transcript.path.read_text('utf-8')
    result = dict(text=text, lines=len(transcript.lines), menu_items=int(menu.numberOfItems()),
                  devices=len(app.devices), captured=captured)
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), 'utf-8')


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--diagnose':
        diagnose(sys.argv[2], sys.argv[3])
        return
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    lock = open(APP_SUPPORT / 'instance.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # already running
    app = App()
    app.ns.run()


if __name__ == '__main__':
    main()
