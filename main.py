import socket
import threading
import random
import string
import array
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.graphics import Color, RoundedRectangle, Line, Rectangle
from kivy.utils import platform
from kivy.core.window import Window
from kivy.clock import Clock, mainthread

# Audio parameters
SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
DTYPE_BYTES = 2
BUFFER_SIZE = CHUNK_SIZE * DTYPE_BYTES

MAGIC_HEADER = b'WALK'
BASE_PORT = 50000

# Color Palette matching the hardware walkie-talkie image
COLOR_BG = (0.07, 0.07, 0.09, 1)          # Dark chassis #121317
COLOR_LCD_GREEN = (0.08, 0.90, 0.28, 1)   # Neon Green display
COLOR_LCD_ORANGE = (1.00, 0.58, 0.05, 1)  # Warm Amber display
COLOR_LCD_TEXT = (0.05, 0.12, 0.05, 1)    # Dark LCD pixel text
COLOR_BTN_DARK = (0.17, 0.18, 0.22, 1)    # Hardware matte button

def generate_device_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4)).encode('ascii')

def get_broadcast_targets(port):
    targets = [('255.255.255.255', port)]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split('.')
        if len(parts) == 4:
            targets.append((f"{parts[0]}.{parts[1]}.{parts[2]}.255", port))
    except Exception:
        pass
    return targets

class GroupTransmitter:
    def __init__(self, device_id):
        self.device_id = device_id
        self.is_recording = False
        self.thread = None
        self.channel = 1
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def start(self, channel):
        self.channel = channel
        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_recording = False

    def _record_loop(self):
        port = BASE_PORT + self.channel
        targets = get_broadcast_targets(port)
        header = MAGIC_HEADER + self.device_id + bytes([self.channel])

        if platform == 'android':
            from jnius import autoclass
            AudioRecord = autoclass('android.media.AudioRecord')
            AudioSource = autoclass('android.media.MediaRecorder$AudioSource')
            AudioFormat = autoclass('android.media.AudioFormat')

            min_buf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            buf_size = max(min_buf, BUFFER_SIZE)
            recorder = AudioRecord(
                AudioSource.MIC, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, buf_size
            )
            recorder.startRecording()
            byte_buf = bytearray(buf_size)

            try:
                while self.is_recording:
                    read_bytes = recorder.read(byte_buf, 0, len(byte_buf))
                    if read_bytes > 0:
                        samples = array.array('h', bytes(byte_buf[:read_bytes]))
                        boosted = array.array('h', (min(32767, max(-32768, int(s * 2.5))) for s in samples)).tobytes()
                        packet = header + boosted
                        for addr, p in targets:
                            try:
                                self.sock.sendto(packet, (addr, p))
                            except Exception:
                                pass
            finally:
                recorder.stop()
                recorder.release()
        else:
            import sounddevice as sd
            import numpy as np
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
                while self.is_recording:
                    data, _ = stream.read(CHUNK_SIZE)
                    boosted = np.clip(data.astype(np.float32) * 2.5, -32768, 32767).astype(np.int16)
                    packet = header + boosted.tobytes()
                    for addr, p in targets:
                        try:
                            self.sock.sendto(packet, (addr, p))
                        except Exception:
                            pass

class GroupReceiver:
    def __init__(self, device_id, on_speaker_detected):
        self.device_id = device_id
        self.on_speaker_detected = on_speaker_detected
        self.running = False
        self.thread = None
        self.channel = 1
        self.sock = None

    def start(self, channel):
        self.channel = channel
        self.running = True
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def _receive_loop(self):
        port = BASE_PORT + self.channel
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('', port))
        except Exception as e:
            print(f"Bind error: {e}")
            return

        if platform == 'android':
            from jnius import autoclass
            AudioTrack = autoclass('android.media.AudioTrack')
            AudioManager = autoclass('android.media.AudioManager')
            AudioFormat = autoclass('android.media.AudioFormat')

            min_buf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            track = AudioTrack(
                AudioManager.STREAM_MUSIC, SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, max(min_buf, BUFFER_SIZE), AudioTrack.MODE_STREAM
            )
            track.play()

            try:
                while self.running:
                    packet, addr = self.sock.recvfrom(BUFFER_SIZE + 32)
                    if not packet or len(packet) < 9 or packet[:4] != MAGIC_HEADER:
                        continue
                    if packet[4:8] == self.device_id or packet[8] != self.channel:
                        continue

                    self.on_speaker_detected(packet[4:8].decode('ascii', errors='ignore'))
                    raw_audio = packet[9:]
                    samples = array.array('h', raw_audio)
                    boosted = array.array('h', (min(32767, max(-32768, int(s * 3.0))) for s in samples)).tobytes()
                    track.write(boosted, 0, len(boosted))
            finally:
                track.stop()
                track.release()
        else:
            import sounddevice as sd
            import numpy as np
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
                while self.running:
                    packet, addr = self.sock.recvfrom(BUFFER_SIZE + 32)
                    if not packet or len(packet) < 9 or packet[:4] != MAGIC_HEADER:
                        continue
                    if packet[4:8] == self.device_id or packet[8] != self.channel:
                        continue

                    self.on_speaker_detected(packet[4:8].decode('ascii', errors='ignore'))
                    raw_audio = packet[9:]
                    audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                    boosted = np.clip(audio_data.astype(np.float32) * 3.0, -32768, 32767).astype(np.int16)
                    stream.write(boosted)


class SpeakerGrille(Widget):
    """Draws authentic horizontal speaker vents at the bottom of the radio."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self.redraw, size=self.redraw)

    def redraw(self, *args):
        self.canvas.clear()
        with self.canvas:
            Color(0.04, 0.04, 0.05, 1)  # Deep grill slot shadow
            rows = 5
            slot_h = 3
            spacing = 7
            start_y = self.y + 10
            for i in range(rows):
                # Staggered pill slots
                y = start_y + (i * spacing)
                w_offset = (i % 2) * 15
                RoundedRectangle(
                    pos=(self.x + 30 + w_offset, y),
                    size=(self.width - 60 - (w_offset * 2), slot_h),
                    radius=[2]
                )


class WalkieTalkieApp(App):
    def build(self):
        self.title = "Retro Walkie-Talkie"
        self.device_id = generate_device_id()
        self.current_channel = 1
        self.total_channels = 6
        self.is_private_mode = False
        self.is_powered_on = True
        self.is_hardware_pressed = False
        self.speaker_reset_event = None

        self.transmitter = GroupTransmitter(self.device_id)
        self.receiver = GroupReceiver(self.device_id, self.handle_speaker_detected)

        # Android Multicast & Permissions
        if platform == 'android':
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.RECORD_AUDIO,
                Permission.INTERNET,
                Permission.ACCESS_NETWORK_STATE,
                'android.permission.CHANGE_WIFI_MULTICAST_STATE',
                'android.permission.ACCESS_WIFI_STATE'
            ])
            try:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Context = autoclass('android.content.Context')
                wifi = PythonActivity.mActivity.getSystemService(Context.WIFI_SERVICE)
                self.multicast_lock = wifi.createMulticastLock("walkie_lock")
                self.multicast_lock.setReferenceCounted(True)
                self.multicast_lock.acquire()
            except Exception:
                pass

        # Bind Physical Hardware Keys (Volume Down / Volume Up as PTT Button!)
        Window.bind(on_key_down=self.on_hardware_key_down, on_key_up=self.on_hardware_key_up)

        # Main Chassis Container
        root = BoxLayout(orientation='vertical', padding=[20, 15, 20, 10], spacing=12)

        # Background Chassis Drawing
        with root.canvas.before:
            Color(*COLOR_BG)
            self.bg_rect = Rectangle(pos=root.pos, size=root.size)
        root.bind(pos=lambda obj, val: setattr(self.bg_rect, 'pos', val),
                  size=lambda obj, val: setattr(self.bg_rect, 'size', val))

        # 1. Top Status Header (Mini LED indicator)
        top_bar = BoxLayout(orientation='horizontal', size_hint_y=0.06)
        self.led_label = Label(
            text="● ON AIR", font_size='12sp', bold=True,
            color=COLOR_LCD_GREEN, halign='left', size_hint_x=0.5
        )
        self.led_label.bind(size=self.led_label.setter('text_size'))
        top_bar.add_widget(self.led_label)

        gear_label = Label(
            text=f"ID: {self.device_id.decode('ascii')}", font_size='12sp',
            color=(0.5, 0.5, 0.5, 1), halign='right', size_hint_x=0.5
        )
        gear_label.bind(size=gear_label.setter('text_size'))
        top_bar.add_widget(gear_label)
        root.add_widget(top_bar)

        # 2. Backlit LCD Display Screen
        self.lcd_box = BoxLayout(orientation='vertical', padding=12, spacing=4, size_hint_y=0.42)
        with self.lcd_box.canvas.before:
            self.lcd_color = Color(*COLOR_LCD_GREEN)
            self.lcd_rect = RoundedRectangle(pos=self.lcd_box.pos, size=self.lcd_box.size, radius=[14])
        self.lcd_box.bind(pos=lambda obj, val: setattr(self.lcd_rect, 'pos', val),
                          size=lambda obj, val: setattr(self.lcd_rect, 'size', val))

        # LCD Top: Mode & Battery/Freq
        lcd_top = BoxLayout(orientation='horizontal', size_hint_y=0.2)
        self.mode_label = Label(
            text="[b]PUBLIC[/b]  Private", markup=True,
            font_size='13sp', color=COLOR_LCD_TEXT, halign='left'
        )
        self.mode_label.bind(size=self.mode_label.setter('text_size'))
        lcd_top.add_widget(self.mode_label)

        self.freq_label = Label(
            text="50001 Hz", font_size='12sp',
            color=COLOR_LCD_TEXT, halign='right'
        )
        self.freq_label.bind(size=self.freq_label.setter('text_size'))
        lcd_top.add_widget(self.freq_label)
        self.lcd_box.add_widget(lcd_top)

        # LCD Channel Number & Room Tag
        self.channel_title = Label(
            text=f"CH {self.current_channel} / {self.total_channels}",
            font_size='15sp', bold=True, color=COLOR_LCD_TEXT,
            halign='left', size_hint_y=0.22
        )
        self.channel_title.bind(size=self.channel_title.setter('text_size'))
        self.lcd_box.add_widget(self.channel_title)

        self.hashtag_label = Label(
            text="#MainSquad", font_size='26sp', bold=True,
            color=COLOR_LCD_TEXT, halign='left', size_hint_y=0.28
        )
        self.hashtag_label.bind(size=self.hashtag_label.setter('text_size'))
        self.lcd_box.add_widget(self.hashtag_label)

        # LCD Status Message
        self.lcd_status = Label(
            text="Online: Ready to chat", font_size='13sp',
            color=COLOR_LCD_TEXT, halign='left', size_hint_y=0.15
        )
        self.lcd_status.bind(size=self.lcd_status.setter('text_size'))
        self.lcd_box.add_widget(self.lcd_status)

        # LCD Bottom Info (Volume & Signal Bars)
        lcd_bottom = BoxLayout(orientation='horizontal', size_hint_y=0.15)
        vol_label = Label(text="🔊 100%", font_size='12sp', color=COLOR_LCD_TEXT, halign='left')
        vol_label.bind(size=vol_label.setter('text_size'))
        lcd_bottom.add_widget(vol_label)

        sig_label = Label(text="📶 ||||||||||", font_size='12sp', color=COLOR_LCD_TEXT, halign='right')
        sig_label.bind(size=sig_label.setter('text_size'))
        lcd_bottom.add_widget(sig_label)
        self.lcd_box.add_widget(lcd_bottom)

        root.add_widget(self.lcd_box)

        # 3. Four Round Hardware Controls (+, ▲, ▼, ⏻)
        btn_row = BoxLayout(orientation='horizontal', spacing=15, size_hint_y=0.12)
        btn_row.add_widget(Widget(size_hint_x=0.05))

        btn_plus = Button(text="+", font_size='22sp', bold=True,
                          background_color=COLOR_BTN_DARK, size_hint=(0.22, 1))
        btn_plus.bind(on_release=self.toggle_mode)
        btn_row.add_widget(btn_plus)

        btn_up = Button(text="▲", font_size='18sp',
                        background_color=COLOR_BTN_DARK, size_hint=(0.22, 1))
        btn_up.bind(on_release=self.channel_up)
        btn_row.add_widget(btn_up)

        btn_down = Button(text="▼", font_size='18sp',
                          background_color=COLOR_BTN_DARK, size_hint=(0.22, 1))
        btn_down.bind(on_release=self.channel_down)
        btn_row.add_widget(btn_down)

        btn_power = Button(text="⏻", font_size='20sp', bold=True,
                           background_color=COLOR_BTN_DARK, size_hint=(0.22, 1))
        btn_power.bind(on_release=self.toggle_power)
        btn_row.add_widget(btn_power)

        btn_row.add_widget(Widget(size_hint_x=0.05))
        root.add_widget(btn_row)

        # 4. Large Capsule PTT Button with Neon Glow Outline
        ptt_container = BoxLayout(size_hint_y=0.22, padding=[10, 5, 10, 5])
        self.ptt_btn = Button(
            text="◯   HOLD TO TALK   ◯\n[size=12sp](Or Hold Volume-Down Button)[/size]",
            markup=True,
            bold=True,
            font_size='16sp',
            background_color=(0.14, 0.15, 0.18, 1),
            color=(0.9, 0.9, 0.9, 1)
        )
        self.ptt_btn.bind(on_press=self.on_ptt_press)
        self.ptt_btn.bind(on_release=self.on_ptt_release)

        # Draw glowing rounded pill border
        with self.ptt_btn.canvas.before:
            self.ptt_border_color = Color(*COLOR_LCD_GREEN)
            self.ptt_border = Line(rounded_rectangle=[self.ptt_btn.x, self.ptt_btn.y, self.ptt_btn.width, self.ptt_btn.height, 22], width=2.5)
        self.ptt_btn.bind(pos=self._update_ptt_border, size=self._update_ptt_border)

        ptt_container.add_widget(self.ptt_btn)
        root.add_widget(ptt_container)

        # 5. Realistic Speaker Vents
        self.grille = SpeakerGrille(size_hint_y=0.12)
        root.add_widget(self.grille)

        # Start background receiver on Channel 1
        self.receiver.start(self.current_channel)

        return root

    def _update_ptt_border(self, instance, *args):
        self.ptt_border.rounded_rectangle = [instance.x + 2, instance.y + 2, instance.width - 4, instance.height - 4, 22]

    # Mode Toggle (Public Neon Green <-> Private Amber Orange)
    def toggle_mode(self, *args):
        self.is_private_mode = not self.is_private_mode
        if self.is_private_mode:
            self.lcd_color.rgba = COLOR_LCD_ORANGE
            self.ptt_border_color.rgba = COLOR_LCD_ORANGE
            self.led_label.color = COLOR_LCD_ORANGE
            self.mode_label.text = "Public  [b]PRIVATE[/b]"
            self.hashtag_label.text = "#MyPrivate"
        else:
            self.lcd_color.rgba = COLOR_LCD_GREEN
            self.ptt_border_color.rgba = COLOR_LCD_GREEN
            self.led_label.color = COLOR_LCD_GREEN
            self.mode_label.text = "[b]PUBLIC[/b]  Private"
            self.hashtag_label.text = "#MainSquad"

    def channel_up(self, *args):
        if self.current_channel < self.total_channels:
            self.switch_channel(self.current_channel + 1)
        else:
            self.switch_channel(1)

    def channel_down(self, *args):
        if self.current_channel > 1:
            self.switch_channel(self.current_channel - 1)
        else:
            self.switch_channel(self.total_channels)

    def switch_channel(self, new_channel):
        self.current_channel = new_channel
        self.receiver.stop()
        if self.is_powered_on:
            self.receiver.start(self.current_channel)
        self.channel_title.text = f"CH {self.current_channel} / {self.total_channels}"
        self.freq_label.text = f"{BASE_PORT + self.current_channel} Hz"
        self.lcd_status.text = f"Connected to Channel {self.current_channel}"

    def toggle_power(self, *args):
        self.is_powered_on = not self.is_powered_on
        if self.is_powered_on:
            self.receiver.start(self.current_channel)
            self.lcd_status.text = f"Online: Channel {self.current_channel}"
            self.led_label.text = "● ON AIR"
        else:
            self.receiver.stop()
            self.lcd_status.text = "Muted / Standby"
            self.led_label.text = "○ OFF"

    # Hardware Volume Buttons as Physical PTT
    def on_hardware_key_down(self, window, keycode, scancode, codepoint, modifiers):
        # Keycode 25: Android Volume Down, Keycode 24: Volume Up, Keycode 32: Desktop Spacebar
        if keycode in [25, 24, 32]:
            if not self.is_hardware_pressed:
                self.is_hardware_pressed = True
                self.on_ptt_press()
            return True  # Consume key event so OS volume popup doesn't appear
        return False

    def on_hardware_key_up(self, window, keycode, scancode):
        if keycode in [25, 24, 32]:
            self.is_hardware_pressed = False
            self.on_ptt_release()
            return True
        return False

    def on_ptt_press(self, *args):
        if not self.is_powered_on:
            return
        self.ptt_btn.background_color = (0.0, 0.7, 0.2, 1) if not self.is_private_mode else (0.85, 0.45, 0.0, 1)
        self.ptt_btn.text = ">> TRANSMITTING <<\n[size=12sp]Voice streaming to channel...[/size]"
        self.lcd_status.text = f">> TX: Broadcasting on CH {self.current_channel} <<"
        self.transmitter.start(self.current_channel)

    def on_ptt_release(self, *args):
        self.transmitter.stop()
        self.ptt_btn.background_color = (0.14, 0.15, 0.18, 1)
        self.ptt_btn.text = "◯   HOLD TO TALK   ◯\n[size=12sp](Or Hold Volume-Down Button)[/size]"
        self.lcd_status.text = f"Online: Channel {self.current_channel} (Idle)"

    @mainthread
    def handle_speaker_detected(self, sender_id):
        self.lcd_status.text = f"🔊 RX: User-{sender_id} is talking!"
        if self.speaker_reset_event:
            self.speaker_reset_event.cancel()
        self.speaker_reset_event = Clock.schedule_once(self.reset_lcd_status, 1.8)

    def reset_lcd_status(self, dt):
        self.lcd_status.text = f"Online: Channel {self.current_channel} (Idle)"

    def on_stop(self):
        self.transmitter.stop()
        self.receiver.stop()

if __name__ == '__main__':
    WalkieTalkieApp().run()
