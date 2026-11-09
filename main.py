import socket
import threading
import sys
import random
import string
import array
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.utils import platform
from kivy.clock import Clock, mainthread

# Audio parameters
SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
DTYPE_BYTES = 2  # 16-bit PCM = 2 bytes per sample
BUFFER_SIZE = CHUNK_SIZE * DTYPE_BYTES

# Packet Protocol
MAGIC_HEADER = b'WALK'
BASE_PORT = 50000  # Channel 1 = 50001, Channel 2 = 50002, etc.

def generate_device_id():
    """Generates a random 4-character ID to identify this device."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4)).encode('ascii')

def get_broadcast_targets(port):
    """Returns local broadcast targets for zero-config multi-user group chat."""
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

def get_my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


class GroupTransmitter:
    """Streams audio to all group members on the selected channel."""
    def __init__(self, device_id):
        self.device_id = device_id
        self.is_recording = False
        self.thread = None
        self.channel = 1
        self.extra_peers = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def start(self, channel, extra_peers):
        self.channel = channel
        self.extra_peers = extra_peers
        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_recording = False

    def _record_loop(self):
        port = BASE_PORT + self.channel
        targets = get_broadcast_targets(port)
        for peer in self.extra_peers:
            targets.append((peer, port))

        header = MAGIC_HEADER + self.device_id + bytes([self.channel])

        if platform == 'android':
            from jnius import autoclass
            AudioRecord = autoclass('android.media.AudioRecord')
            AudioSource = autoclass('android.media.MediaRecorder$AudioSource')
            AudioFormat = autoclass('android.media.AudioFormat')

            min_buf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            buf_size = max(min_buf, BUFFER_SIZE)
            recorder = AudioRecord(
                AudioSource.MIC,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                buf_size
            )
            recorder.startRecording()
            byte_buf = bytearray(buf_size)

            try:
                while self.is_recording:
                    read_bytes = recorder.read(byte_buf, 0, len(byte_buf))
                    if read_bytes > 0:
                        samples = array.array('h', bytes(byte_buf[:read_bytes]))
                        # 2.5x Mic boost with int16 clamping
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
            # Desktop fallback (Windows / Mac / Linux)
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
    """Listens for audio broadcasts from any group member on the channel."""
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
        except Exception:
            pass

        try:
            self.sock.bind(('', port))
        except Exception as e:
            print(f"Receiver bind error on port {port}: {e}")
            return

        if platform == 'android':
            from jnius import autoclass
            AudioTrack = autoclass('android.media.AudioTrack')
            AudioManager = autoclass('android.media.AudioManager')
            AudioFormat = autoclass('android.media.AudioFormat')

            min_buf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            # Use STREAM_MUSIC so sound comes out of the main loud speaker
            track = AudioTrack(
                AudioManager.STREAM_MUSIC,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                max(min_buf, BUFFER_SIZE),
                AudioTrack.MODE_STREAM
            )
            track.play()

            try:
                while self.running:
                    packet, addr = self.sock.recvfrom(BUFFER_SIZE + 32)
                    if not packet or len(packet) < 9:
                        continue

                    # Validate Walkie-Talkie magic header
                    if packet[:4] != MAGIC_HEADER:
                        continue

                    sender = packet[4:8]
                    # Drop own echo
                    if sender == self.device_id:
                        continue

                    channel_in_pkt = packet[8]
                    if channel_in_pkt != self.channel:
                        continue

                    self.on_speaker_detected(sender.decode('ascii', errors='ignore'))

                    raw_audio = packet[9:]
                    samples = array.array('h', raw_audio)
                    # 3.0x Loudspeaker boost with int16 clamping
                    boosted = array.array('h', (min(32767, max(-32768, int(s * 3.0))) for s in samples)).tobytes()
                    track.write(boosted, 0, len(boosted))
            finally:
                track.stop()
                track.release()
        else:
            # Desktop testing fallback
            import sounddevice as sd
            import numpy as np
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
                while self.running:
                    packet, addr = self.sock.recvfrom(BUFFER_SIZE + 32)
                    if not packet or len(packet) < 9:
                        continue
                    if packet[:4] != MAGIC_HEADER:
                        continue
                    sender = packet[4:8]
                    if sender == self.device_id:
                        continue
                    channel_in_pkt = packet[8]
                    if channel_in_pkt != self.channel:
                        continue

                    self.on_speaker_detected(sender.decode('ascii', errors='ignore'))
                    raw_audio = packet[9:]
                    audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                    boosted = np.clip(audio_data.astype(np.float32) * 3.0, -32768, 32767).astype(np.int16)
                    stream.write(boosted)


class WalkieTalkieApp(App):
    def build(self):
        self.title = "Group Walkie-Talkie"
        self.device_id = generate_device_id()
        self.current_channel = 1
        self.transmitter = GroupTransmitter(self.device_id)
        self.receiver = GroupReceiver(self.device_id, self.handle_speaker_detected)
        self.speaker_reset_event = None

        # Acquire Android MulticastLock and runtime permissions
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
                activity = PythonActivity.mActivity
                wifi = activity.getSystemService(Context.WIFI_SERVICE)
                self.multicast_lock = wifi.createMulticastLock("walkie_talkie_lock")
                self.multicast_lock.setReferenceCounted(True)
                self.multicast_lock.acquire()
            except Exception as e:
                print(f"MulticastLock error: {e}")

        # Root Layout
        root = BoxLayout(orientation='vertical', padding=15, spacing=10)

        # Header
        my_ip = get_my_ip()
        my_id_str = self.device_id.decode('ascii')
        header = Label(
            text=f"[b]Group Walkie-Talkie[/b]\n[size=14sp]My ID: {my_id_str} | IP: {my_ip}[/size]",
            markup=True,
            halign='center',
            font_size='20sp',
            size_hint_y=0.15
        )
        root.add_widget(header)

        # Channel Selector Label
        ch_label = Label(
            text="[b]Select Channel (1 to 6):[/b]",
            markup=True,
            font_size='15sp',
            size_hint_y=0.08
        )
        root.add_widget(ch_label)

        # 6 Channel Buttons Grid
        channel_grid = GridLayout(cols=6, spacing=6, size_hint_y=0.12)
        self.ch_buttons = []
        for ch in range(1, 7):
            btn = Button(
                text=f"CH {ch}",
                font_size='14sp',
                bold=True,
                background_color=(0.2, 0.7, 0.3, 1) if ch == 1 else (0.3, 0.3, 0.3, 1)
            )
            btn.bind(on_release=lambda instance, c=ch: self.change_channel(c))
            self.ch_buttons.append(btn)
            channel_grid.add_widget(btn)
        root.add_widget(channel_grid)

        # Status Label
        self.status_label = Label(
            text=f"Ready on Channel {self.current_channel} (Everyone on CH {self.current_channel} can talk)",
            font_size='15sp',
            color=(0.4, 0.8, 1, 1),
            size_hint_y=0.1
        )
        root.add_widget(self.status_label)

        # Optional: Direct Peer IPs (Fallback for routers that block broadcast)
        fallback_box = BoxLayout(orientation='horizontal', size_hint_y=0.1, spacing=8)
        fallback_box.add_widget(Label(text="Direct IPs (optional):", font_size='12sp', size_hint_x=0.4))
        self.extra_peers_input = TextInput(
            hint_text="e.g. 192.168.1.15, 192.168.1.20",
            multiline=False,
            font_size='13sp',
            size_hint_x=0.6
        )
        fallback_box.add_widget(self.extra_peers_input)
        root.add_widget(fallback_box)

        # Big Push-to-Talk Button
        self.ptt_btn = Button(
            text="HOLD TO TALK TO GROUP",
            background_color=(0.85, 0.2, 0.2, 1),
            size_hint_y=0.45,
            font_size='24sp',
            bold=True
        )
        self.ptt_btn.bind(on_press=self.on_ptt_press)
        self.ptt_btn.bind(on_release=self.on_ptt_release)
        root.add_widget(self.ptt_btn)

        # Auto-start listening on Channel 1
        self.receiver.start(self.current_channel)

        return root

    def change_channel(self, channel):
        self.current_channel = channel
        for idx, btn in enumerate(self.ch_buttons):
            if idx + 1 == channel:
                btn.background_color = (0.2, 0.7, 0.3, 1)
            else:
                btn.background_color = (0.3, 0.3, 0.3, 1)

        self.receiver.stop()
        self.receiver.start(self.current_channel)
        self.status_label.text = f"Switched to Channel {channel} (Ready)"
        self.status_label.color = (0.4, 0.8, 1, 1)

    @mainthread
    def handle_speaker_detected(self, sender_id):
        self.status_label.text = f"🔊 User-{sender_id} is speaking on CH {self.current_channel}..."
        self.status_label.color = (0.2, 1.0, 0.4, 1)
        if self.speaker_reset_event:
            self.speaker_reset_event.cancel()
        self.speaker_reset_event = Clock.schedule_once(self.reset_status, 1.5)

    def reset_status(self, dt):
        self.status_label.text = f"Channel {self.current_channel}: Idle"
        self.status_label.color = (0.7, 0.7, 0.7, 1)

    def on_ptt_press(self, instance):
        self.ptt_btn.background_color = (0.1, 0.85, 0.3, 1)  # Green while speaking
        self.ptt_btn.text = f"BROADCASTING TO CH {self.current_channel}..."
        self.status_label.text = f"Transmitting voice to Channel {self.current_channel}..."
        self.status_label.color = (1.0, 0.8, 0.2, 1)

        # Parse extra peer IPs if user entered any
        extra_peers = []
        raw_text = self.extra_peers_input.text.strip()
        if raw_text:
            for ip in raw_text.split(','):
                cleaned = ip.strip()
                if cleaned:
                    extra_peers.append(cleaned)

        self.transmitter.start(self.current_channel, extra_peers)

    def on_ptt_release(self, instance):
        self.transmitter.stop()
        self.ptt_btn.background_color = (0.85, 0.2, 0.2, 1)  # Red idle
        self.ptt_btn.text = "HOLD TO TALK TO GROUP"
        self.status_label.text = f"Channel {self.current_channel}: Idle"
        self.status_label.color = (0.7, 0.7, 0.7, 1)

    def on_stop(self):
        self.transmitter.stop()
        self.receiver.stop()

if __name__ == '__main__':
    WalkieTalkieApp().run()
