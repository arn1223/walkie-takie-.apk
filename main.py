import socket
import threading
import random
import string
import array
import json
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.uix.textinput import TextInput
from kivy.graphics import Color, RoundedRectangle, Line, Rectangle
from kivy.utils import platform
from kivy.core.window import Window
from kivy.clock import Clock, mainthread

# Android-specific PyJNIus imports for Hardware Key Interception & Telemetry
if platform == 'android':
    from jnius import autoclass, PythonJavaClass, java_method
    from android.runnable import run_on_ui_thread

    class AndroidVolumeKeyListener(PythonJavaClass):
        """Native Android OnKeyListener that intercepts physical Volume Down & Up keys."""
        __javainterfaces__ = ['android/view/View$OnKeyListener']
        __javacontext__ = 'app'

        def __init__(self, app_instance):
            super().__init__()
            self.app = app_instance
            self.is_pressed = False

        @java_method('(Landroid/view/View;ILandroid/view/KeyEvent;)Z')
        def onKey(self, view, keyCode, event):
            try:
                # KeyEvent.KEYCODE_VOLUME_DOWN = 25, KEYCODE_VOLUME_UP = 24
                if keyCode in [25, 24]:
                    action = event.getAction()
                    if action == 0:  # ACTION_DOWN (Key Pressed)
                        if not self.is_pressed:
                            self.is_pressed = True
                            Clock.schedule_once(lambda dt: self.app.on_ptt_press(), 0)
                    elif action == 1:  # ACTION_UP (Key Released)
                        if self.is_pressed:
                            self.is_pressed = False
                            Clock.schedule_once(lambda dt: self.app.on_ptt_release(), 0)
                    return True  # Consume key event so system volume slider does not appear
            except Exception as e:
                print(f"Volume key event error: {e}")
            return False

# Audio parameters
SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
DTYPE_BYTES = 2
BUFFER_SIZE = CHUNK_SIZE * DTYPE_BYTES

MAGIC_HEADER = b'WALK'
BASE_PORT = 50000

# Default relay server URL (update after Render deployment)
DEFAULT_RELAY_URL = ""

# Color Palette matching the hardware retro walkie-talkie
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

def get_actual_battery():
    """Safely reads battery percentage without crashing on 32-bit Android 10 or Android 16+."""
    if platform == 'android':
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            if activity:
                Intent = autoclass('android.content.Intent')
                IntentFilter = autoclass('android.content.IntentFilter')
                BatteryManager = autoclass('android.os.BatteryManager')
                battery_intent = activity.registerReceiver(None, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
                if battery_intent:
                    level = battery_intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                    scale = battery_intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
                    if level >= 0 and scale > 0:
                        return max(1, min(100, int((level / float(scale)) * 100)))
        except Exception as e:
            print(f"Battery telemetry error: {e}")
    return 85

def get_actual_wifi_bars():
    """Safely determines Wi-Fi connectivity without throwing Android 10/16 SecurityExceptions."""
    if platform == 'android':
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Context = autoclass('android.content.Context')
            activity = PythonActivity.mActivity
            if activity:
                cm = activity.getSystemService(Context.CONNECTIVITY_SERVICE)
                info = cm.getActiveNetworkInfo()
                if info and info.isConnected():
                    return "▂▄▅▆█"
        except Exception as e:
            print(f"Wi-Fi telemetry error: {e}")
    return "▂▄▅▆█"


# ─── Lightweight WebSocket client (no external deps, runs on Android) ────────
# Uses raw sockets + HTTP Upgrade handshake to avoid needing the `websockets` pip package
# which is NOT available in Kivy/p4a build. This is a minimal WS client (RFC 6455).

import hashlib
import base64
import struct
import ssl
import os as _os

class SimpleWebSocket:
    """Minimal RFC 6455 WebSocket client using raw sockets. No external dependencies."""

    def __init__(self, url, on_message=None, on_close=None, on_error=None):
        self.url = url
        self.on_message = on_message
        self.on_close = on_close
        self.on_error = on_error
        self.sock = None
        self.connected = False
        self._recv_thread = None
        self._host = ""
        self._port = 443
        self._path = "/"
        self._use_ssl = False

    def _parse_url(self):
        url = self.url
        if url.startswith("wss://"):
            self._use_ssl = True
            url = url[6:]
        elif url.startswith("ws://"):
            self._use_ssl = False
            url = url[5:]
        else:
            self._use_ssl = url.startswith("https://")
            url = url.split("://", 1)[-1]

        if "/" in url:
            hostport, self._path = url.split("/", 1)
            self._path = "/" + self._path
        else:
            hostport = url
            self._path = "/"

        if ":" in hostport:
            self._host, port_str = hostport.rsplit(":", 1)
            self._port = int(port_str)
        else:
            self._host = hostport
            self._port = 443 if self._use_ssl else 80

    def connect(self):
        """Perform TCP connect + WebSocket HTTP Upgrade handshake."""
        try:
            self._parse_url()
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.settimeout(10)
            raw_sock.connect((self._host, self._port))

            if self._use_ssl:
                ctx = ssl.create_default_context()
                self.sock = ctx.wrap_socket(raw_sock, server_hostname=self._host)
            else:
                self.sock = raw_sock

            # WebSocket upgrade handshake
            ws_key = base64.b64encode(_os.urandom(16)).decode('ascii')
            handshake = (
                f"GET {self._path} HTTP/1.1\r\n"
                f"Host: {self._host}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            self.sock.sendall(handshake.encode('ascii'))

            # Read HTTP response headers
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Server closed during handshake")
                response += chunk

            status_line = response.split(b"\r\n")[0].decode('ascii', errors='ignore')
            if "101" not in status_line:
                raise ConnectionError(f"WebSocket handshake failed: {status_line}")

            self.sock.settimeout(None)
            self.connected = True

            # Start receive loop in background
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()
            return True

        except Exception as e:
            print(f"[WS] Connect error: {e}")
            if self.on_error:
                self.on_error(e)
            return False

    def send_binary(self, data):
        """Send a binary WebSocket frame (opcode 0x02, masked)."""
        if not self.connected or not self.sock:
            return
        try:
            frame = self._build_frame(data, opcode=0x02)
            self.sock.sendall(frame)
        except Exception as e:
            print(f"[WS] Send error: {e}")
            self._close_internal()

    def send_text(self, text):
        """Send a text WebSocket frame (opcode 0x01, masked)."""
        if not self.connected or not self.sock:
            return
        try:
            frame = self._build_frame(text.encode('utf-8'), opcode=0x01)
            self.sock.sendall(frame)
        except Exception as e:
            print(f"[WS] Send error: {e}")
            self._close_internal()

    def close(self):
        """Send close frame and shut down."""
        if self.connected:
            try:
                frame = self._build_frame(b'', opcode=0x08)
                self.sock.sendall(frame)
            except Exception:
                pass
        self._close_internal()

    def _build_frame(self, payload, opcode=0x02):
        """Build a masked WebSocket frame per RFC 6455."""
        header = bytes([0x80 | opcode])  # FIN + opcode
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])  # MASK bit set
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)

        mask = _os.urandom(4)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        return header + mask + bytes(masked)

    def _recv_loop(self):
        """Continuously read WebSocket frames from the server."""
        try:
            while self.connected:
                data = self._read_frame()
                if data is None:
                    break
                opcode, payload = data
                if opcode == 0x08:  # Close
                    break
                elif opcode == 0x09:  # Ping -> Pong
                    try:
                        pong = self._build_frame(payload, opcode=0x0A)
                        self.sock.sendall(pong)
                    except Exception:
                        break
                elif opcode in (0x01, 0x02):  # Text or Binary
                    if self.on_message:
                        self.on_message(payload, opcode)
        except Exception as e:
            if self.connected:
                print(f"[WS] Recv error: {e}")
        finally:
            self._close_internal()

    def _read_exact(self, n):
        """Read exactly n bytes from the socket."""
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _read_frame(self):
        """Read and decode one WebSocket frame from the server."""
        hdr = self._read_exact(2)
        if not hdr:
            return None
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F

        if length == 126:
            ext = self._read_exact(2)
            if not ext:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._read_exact(8)
            if not ext:
                return None
            length = struct.unpack("!Q", ext)[0]

        if masked:
            mask = self._read_exact(4)
            if not mask:
                return None

        payload = self._read_exact(length) if length > 0 else b""
        if payload is None:
            return None

        if masked:
            payload = bytearray(payload)
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
            payload = bytes(payload)

        return (opcode, payload)

    def _close_internal(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass


# ─── Cloud Relay Transmitter & Receiver ──────────────────────────────────────

class CloudTransmitter:
    """Sends audio to the Render WebSocket relay server."""
    def __init__(self, device_id, ws_client):
        self.device_id = device_id
        self.ws = ws_client
        self.is_recording = False
        self.thread = None
        self.channel = 1

    def start(self, channel):
        self.channel = channel
        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_recording = False

    def _record_loop(self):
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
                        if self.ws and self.ws.connected:
                            self.ws.send_binary(packet)
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
                    if self.ws and self.ws.connected:
                        self.ws.send_binary(packet)


class CloudReceiver:
    """Receives audio from the Render WebSocket relay server and plays it."""
    def __init__(self, device_id, on_speaker_detected):
        self.device_id = device_id
        self.on_speaker_detected = on_speaker_detected
        self.running = False
        self.channel = 1
        self.volume_factor = 1.0
        self._player = None
        self._player_lock = threading.Lock()

    def set_volume(self, percent):
        self.volume_factor = max(0.0, min(1.0, percent / 100.0))

    def start(self, channel):
        self.channel = channel
        self.running = True
        self._start_player()

    def stop(self):
        self.running = False
        self._stop_player()

    def _start_player(self):
        """Initialize the audio output player."""
        if platform == 'android':
            from jnius import autoclass
            AudioTrack = autoclass('android.media.AudioTrack')
            AudioManager = autoclass('android.media.AudioManager')
            AudioFormat = autoclass('android.media.AudioFormat')
            min_buf = AudioTrack.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            self._player = AudioTrack(
                AudioManager.STREAM_MUSIC, SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, max(min_buf, BUFFER_SIZE), AudioTrack.MODE_STREAM
            )
            self._player.play()
        else:
            import sounddevice as sd
            self._player = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE)
            self._player.start()

    def _stop_player(self):
        with self._player_lock:
            if self._player:
                try:
                    if platform == 'android':
                        self._player.stop()
                        self._player.release()
                    else:
                        self._player.stop()
                        self._player.close()
                except Exception:
                    pass
                self._player = None

    def handle_audio_packet(self, packet):
        """Called when a binary WebSocket message is received from the relay."""
        if not self.running or not packet or len(packet) < 9:
            return
        if packet[:4] != MAGIC_HEADER:
            return
        if packet[4:8] == self.device_id or packet[8] != self.channel:
            return
        if self.volume_factor <= 0.01:
            return

        self.on_speaker_detected(packet[4:8].decode('ascii', errors='ignore'))
        raw_audio = packet[9:]

        with self._player_lock:
            if not self._player:
                return
            if platform == 'android':
                samples = array.array('h', raw_audio)
                current_gain = self.volume_factor * 3.5
                boosted = array.array('h', (min(32767, max(-32768, int(s * current_gain))) for s in samples)).tobytes()
                self._player.write(boosted, 0, len(boosted))
            else:
                import numpy as np
                audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                current_gain = self.volume_factor * 3.5
                boosted = np.clip(audio_data.astype(np.float32) * current_gain, -32768, 32767).astype(np.int16)
                self._player.write(boosted)


# ─── LAN Broadcast Transmitter & Receiver (original, zero-config) ───────────

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
        self.volume_factor = 1.0

    def set_volume(self, percent):
        self.volume_factor = max(0.0, min(1.0, percent / 100.0))

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
            print(f"Bind error on port {port}: {e}")
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
                    if self.volume_factor <= 0.01:
                        continue

                    self.on_speaker_detected(packet[4:8].decode('ascii', errors='ignore'))
                    raw_audio = packet[9:]
                    samples = array.array('h', raw_audio)
                    current_gain = self.volume_factor * 3.5
                    boosted = array.array('h', (min(32767, max(-32768, int(s * current_gain))) for s in samples)).tobytes()
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
                    if self.volume_factor <= 0.01:
                        continue

                    self.on_speaker_detected(packet[4:8].decode('ascii', errors='ignore'))
                    raw_audio = packet[9:]
                    audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                    current_gain = self.volume_factor * 3.5
                    boosted = np.clip(audio_data.astype(np.float32) * current_gain, -32768, 32767).astype(np.int16)
                    stream.write(boosted)


class SpeakerGrille(Widget):
    """Draws realistic horizontal speaker vents at the bottom of the radio chassis."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self.redraw, size=self.redraw)

    def redraw(self, *args):
        self.canvas.clear()
        with self.canvas:
            Color(0.04, 0.04, 0.05, 1)
            rows = 5
            slot_h = 3
            spacing = 7
            start_y = self.y + 10
            for i in range(rows):
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
        self.volume_steps = [25, 50, 75, 100, 0]
        self.vol_index = 3  # Default 100%
        self.speaker_reset_event = None

        # ─── Network Mode ───────────────────────────────────────────
        # "lan" = original UDP broadcast, "cloud" = WebSocket relay
        self.net_mode = "lan"
        self.relay_url = DEFAULT_RELAY_URL
        self.ws_client = None

        # LAN mode transmitter/receiver
        self.lan_transmitter = GroupTransmitter(self.device_id)
        self.lan_receiver = GroupReceiver(self.device_id, self.handle_speaker_detected)
        self.lan_receiver.set_volume(self.volume_steps[self.vol_index])

        # Cloud mode transmitter/receiver (initialized when connected)
        self.cloud_transmitter = None
        self.cloud_receiver = None

        # Android Runtime Permissions and Hardware Interceptions
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
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Context = autoclass('android.content.Context')
                wifi = PythonActivity.mActivity.getSystemService(Context.WIFI_SERVICE)
                self.multicast_lock = wifi.createMulticastLock("walkie_lock")
                self.multicast_lock.setReferenceCounted(True)
                self.multicast_lock.acquire()
            except Exception as e:
                print(f"MulticastLock error: {e}")

            # Attach Native Volume Key Listener
            Clock.schedule_once(lambda dt: self.attach_android_volume_listener(), 0.5)

        # Desktop Keyboard Support (Spacebar for testing)
        Window.bind(on_key_down=self.on_desktop_key_down, on_key_up=self.on_desktop_key_up)

        # Root Chassis Layout
        root = BoxLayout(orientation='vertical', padding=[20, 15, 20, 10], spacing=8)

        with root.canvas.before:
            Color(*COLOR_BG)
            self.bg_rect = Rectangle(pos=root.pos, size=root.size)
        root.bind(pos=lambda obj, val: setattr(self.bg_rect, 'pos', val),
                  size=lambda obj, val: setattr(self.bg_rect, 'size', val))

        # 1. Top Status Header
        top_bar = BoxLayout(orientation='horizontal', size_hint_y=0.05)
        self.led_label = Label(
            text="● ON AIR", font_size='12sp', bold=True,
            color=COLOR_LCD_GREEN, halign='left', size_hint_x=0.5
        )
        self.led_label.bind(size=self.led_label.setter('text_size'))
        top_bar.add_widget(self.led_label)

        id_label = Label(
            text=f"ID: {self.device_id.decode('ascii')}", font_size='12sp',
            color=(0.5, 0.5, 0.5, 1), halign='right', size_hint_x=0.5
        )
        id_label.bind(size=id_label.setter('text_size'))
        top_bar.add_widget(id_label)
        root.add_widget(top_bar)

        # 2. Backlit LCD Display Screen
        self.lcd_box = BoxLayout(orientation='vertical', padding=12, spacing=4, size_hint_y=0.36)
        with self.lcd_box.canvas.before:
            self.lcd_color = Color(*COLOR_LCD_GREEN)
            self.lcd_rect = RoundedRectangle(pos=self.lcd_box.pos, size=self.lcd_box.size, radius=[14])
        self.lcd_box.bind(pos=lambda obj, val: setattr(self.lcd_rect, 'pos', val),
                          size=lambda obj, val: setattr(self.lcd_rect, 'size', val))

        # LCD Top: Mode & REAL Battery Level
        lcd_top = BoxLayout(orientation='horizontal', size_hint_y=0.2)
        self.mode_btn = Button(
            text="[b]PUBLIC[/b]  Private", markup=True,
            font_size='13sp', color=COLOR_LCD_TEXT,
            background_color=(0, 0, 0, 0), halign='left', size_hint_x=0.6
        )
        self.mode_btn.bind(size=self.mode_btn.setter('text_size'))
        self.mode_btn.bind(on_release=self.toggle_mode)
        lcd_top.add_widget(self.mode_btn)

        self.battery_label = Label(
            text=f"🔋 {get_actual_battery()}%", font_size='13sp', bold=True,
            color=COLOR_LCD_TEXT, halign='right', size_hint_x=0.4
        )
        self.battery_label.bind(size=self.battery_label.setter('text_size'))
        lcd_top.add_widget(self.battery_label)
        self.lcd_box.add_widget(lcd_top)

        # LCD Middle: Channel Number & Hashtag Name
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

        # LCD Bottom: CONTROLLABLE Volume Button & Wi-Fi Signal Bars
        lcd_bottom = BoxLayout(orientation='horizontal', size_hint_y=0.18)

        current_vol = self.volume_steps[self.vol_index]
        vol_str = f"🔊 {current_vol}" if current_vol > 0 else "🔇 MUTE"
        self.vol_btn = Button(
            text=f"[b]{vol_str}[/b] (tap)", markup=True,
            font_size='13sp', color=COLOR_LCD_TEXT,
            background_color=(0, 0, 0, 0.08), halign='left', size_hint_x=0.45
        )
        self.vol_btn.bind(size=self.vol_btn.setter('text_size'))
        self.vol_btn.bind(on_release=self.cycle_volume)
        lcd_bottom.add_widget(self.vol_btn)

        self.wifi_label = Label(
            text=f"📶 {get_actual_wifi_bars()}", font_size='14sp', bold=True,
            color=COLOR_LCD_TEXT, halign='right', size_hint_x=0.55
        )
        self.wifi_label.bind(size=self.wifi_label.setter('text_size'))
        lcd_bottom.add_widget(self.wifi_label)

        self.lcd_box.add_widget(lcd_bottom)
        root.add_widget(self.lcd_box)

        # 3. Network Mode Toggle Bar (LAN ↔ CLOUD)
        net_bar = BoxLayout(orientation='horizontal', spacing=8, size_hint_y=0.06)
        self.net_mode_btn = Button(
            text="📡 LAN Mode", font_size='12sp', bold=True,
            background_color=(0.1, 0.4, 0.15, 1), color=(1, 1, 1, 1),
            size_hint_x=0.35
        )
        self.net_mode_btn.bind(on_release=self.toggle_net_mode)
        net_bar.add_widget(self.net_mode_btn)

        self.relay_input = TextInput(
            text=self.relay_url,
            hint_text="wss://your-server.onrender.com/ws",
            font_size='11sp', multiline=False,
            size_hint_x=0.65,
            background_color=(0.12, 0.12, 0.15, 1),
            foreground_color=(0.8, 1.0, 0.8, 1),
            cursor_color=(0, 1, 0, 1)
        )
        net_bar.add_widget(self.relay_input)
        root.add_widget(net_bar)

        # 4. Four Round Hardware Controls (+, ▲, ▼, ⏻)
        btn_row = BoxLayout(orientation='horizontal', spacing=15, size_hint_y=0.10)
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

        # 5. Large Capsule PTT Button
        ptt_container = BoxLayout(size_hint_y=0.20, padding=[10, 5, 10, 5])
        self.ptt_btn = Button(
            text="◯   HOLD TO TALK   ◯\n[size=12sp](Or Hold Physical Volume-Down Button)[/size]",
            markup=True,
            bold=True,
            font_size='15sp',
            background_color=(0.14, 0.15, 0.18, 1),
            color=(0.9, 0.9, 0.9, 1)
        )
        self.ptt_btn.bind(on_press=lambda *a: self.on_ptt_press())
        self.ptt_btn.bind(on_release=lambda *a: self.on_ptt_release())

        with self.ptt_btn.canvas.before:
            self.ptt_border_color = Color(*COLOR_LCD_GREEN)
            self.ptt_border = Line(rounded_rectangle=[self.ptt_btn.x, self.ptt_btn.y, self.ptt_btn.width, self.ptt_btn.height, 22], width=2.5)
        self.ptt_btn.bind(pos=self._update_ptt_border, size=self._update_ptt_border)

        ptt_container.add_widget(self.ptt_btn)
        root.add_widget(ptt_container)

        # 6. Realistic Speaker Vents
        self.grille = SpeakerGrille(size_hint_y=0.10)
        root.add_widget(self.grille)

        # Refresh Telemetry periodically
        Clock.schedule_interval(self.refresh_telemetry, 4.0)

        # Start background receiver on Channel 1 (LAN mode by default)
        self.lan_receiver.start(self.current_channel)

        return root

    def attach_android_volume_listener(self):
        """Attaches native Android OnKeyListener to intercept Volume Down/Up hardware keys."""
        if platform == 'android':
            try:
                @run_on_ui_thread
                def _do_attach():
                    PythonActivity = autoclass('org.kivy.android.PythonActivity')
                    activity = PythonActivity.mActivity
                    if activity:
                        decor_view = activity.getWindow().getDecorView()
                        decor_view.setFocusableInTouchMode(True)
                        decor_view.requestFocus()
                        self.vol_key_listener = AndroidVolumeKeyListener(self)
                        decor_view.setOnKeyListener(self.vol_key_listener)
                        print("[+] Native Android Volume-Down/Up Key Listener successfully registered!")
                _do_attach()
            except Exception as e:
                print(f"[!] Failed to attach Android volume key listener: {e}")

    def _update_ptt_border(self, instance, *args):
        self.ptt_border.rounded_rectangle = [instance.x + 2, instance.y + 2, instance.width - 4, instance.height - 4, 22]

    def refresh_telemetry(self, dt):
        self.battery_label.text = f"🔋 {get_actual_battery()}%"
        self.wifi_label.text = f"📶 {get_actual_wifi_bars()}"

    def cycle_volume(self, *args):
        self.vol_index = (self.vol_index + 1) % len(self.volume_steps)
        vol = self.volume_steps[self.vol_index]
        self.lan_receiver.set_volume(vol)
        if self.cloud_receiver:
            self.cloud_receiver.set_volume(vol)
        if vol > 0:
            self.vol_btn.text = f"[b]🔊 {vol}[/b] (tap)"
            self.lcd_status.text = f"Volume set to {vol}%"
        else:
            self.vol_btn.text = "[b]🔇 MUTE[/b] (tap)"
            self.lcd_status.text = "Audio Muted"

    # ─── Network Mode Switching ──────────────────────────────────────────
    def toggle_net_mode(self, *args):
        if self.net_mode == "lan":
            # Switch to Cloud mode
            url = self.relay_input.text.strip()
            if not url:
                self.lcd_status.text = "⚠ Enter relay URL first!"
                return
            self._connect_cloud(url)
        else:
            # Switch back to LAN mode
            self._disconnect_cloud()

    def _connect_cloud(self, url):
        """Connect to the WebSocket relay server."""
        # Normalize URL: allow user to paste bare domain or https URL
        if not url.startswith("ws://") and not url.startswith("wss://"):
            if url.startswith("https://"):
                url = "wss://" + url[8:]
            elif url.startswith("http://"):
                url = "ws://" + url[7:]
            else:
                url = "wss://" + url
        if not url.endswith("/ws"):
            if url.endswith("/"):
                url += "ws"
            else:
                url += "/ws"

        self.relay_url = url
        self.lcd_status.text = "🔗 Connecting to relay..."
        self.net_mode_btn.text = "⏳ Connecting..."

        def _do_connect():
            self.ws_client = SimpleWebSocket(
                url,
                on_message=self._on_ws_message,
                on_close=self._on_ws_close,
                on_error=self._on_ws_error
            )
            ok = self.ws_client.connect()
            if ok:
                # Send join message
                join_msg = json.dumps({"type": "join", "channel": self.current_channel})
                self.ws_client.send_text(join_msg)

                # Stop LAN receiver, start cloud receiver
                self.lan_receiver.stop()
                self.cloud_receiver = CloudReceiver(self.device_id, self.handle_speaker_detected)
                vol = self.volume_steps[self.vol_index]
                self.cloud_receiver.set_volume(vol)
                self.cloud_receiver.start(self.current_channel)

                self.cloud_transmitter = CloudTransmitter(self.device_id, self.ws_client)

                Clock.schedule_once(lambda dt: self._update_cloud_ui(True), 0)
            else:
                Clock.schedule_once(lambda dt: self._update_cloud_ui(False), 0)

        threading.Thread(target=_do_connect, daemon=True).start()

    @mainthread
    def _update_cloud_ui(self, success):
        if success:
            self.net_mode = "cloud"
            self.net_mode_btn.text = "🌐 CLOUD"
            self.net_mode_btn.background_color = (0.1, 0.2, 0.6, 1)
            self.lcd_status.text = f"☁ Connected to relay (CH {self.current_channel})"
        else:
            self.net_mode = "lan"
            self.net_mode_btn.text = "📡 LAN Mode"
            self.net_mode_btn.background_color = (0.1, 0.4, 0.15, 1)
            self.lcd_status.text = "⚠ Relay connection failed!"
            # Restart LAN receiver
            self.lan_receiver.start(self.current_channel)

    def _disconnect_cloud(self):
        """Disconnect from cloud relay and switch back to LAN."""
        if self.cloud_receiver:
            self.cloud_receiver.stop()
            self.cloud_receiver = None
        self.cloud_transmitter = None
        if self.ws_client:
            self.ws_client.close()
            self.ws_client = None
        self.net_mode = "lan"
        self.net_mode_btn.text = "📡 LAN Mode"
        self.net_mode_btn.background_color = (0.1, 0.4, 0.15, 1)
        self.lan_receiver.start(self.current_channel)
        self.lcd_status.text = f"📡 LAN Mode: Channel {self.current_channel}"

    def _on_ws_message(self, data, opcode):
        """Handle incoming WebSocket messages from the relay."""
        if opcode == 0x02 and self.cloud_receiver:  # Binary = audio packet
            self.cloud_receiver.handle_audio_packet(data)
        elif opcode == 0x01:  # Text = control message
            try:
                msg = json.loads(data.decode('utf-8'))
                if msg.get("type") == "info":
                    Clock.schedule_once(lambda dt: setattr(self.lcd_status, 'text', msg.get("message", "")), 0)
            except Exception:
                pass

    @mainthread
    def _on_ws_close(self):
        if self.net_mode == "cloud":
            self.lcd_status.text = "⚠ Relay disconnected!"
            self._disconnect_cloud()

    def _on_ws_error(self, error):
        print(f"[WS] Error: {error}")

    # ─── Original controls ───────────────────────────────────────────────
    def toggle_mode(self, *args):
        self.is_private_mode = not self.is_private_mode
        if self.is_private_mode:
            self.lcd_color.rgba = COLOR_LCD_ORANGE
            self.ptt_border_color.rgba = COLOR_LCD_ORANGE
            self.led_label.color = COLOR_LCD_ORANGE
            self.mode_btn.text = "Public  [b]PRIVATE[/b]"
            self.hashtag_label.text = "#MyFamily"
        else:
            self.lcd_color.rgba = COLOR_LCD_GREEN
            self.ptt_border_color.rgba = COLOR_LCD_GREEN
            self.led_label.color = COLOR_LCD_GREEN
            self.mode_btn.text = "[b]PUBLIC[/b]  Private"
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
        if self.net_mode == "lan":
            self.lan_receiver.stop()
            if self.is_powered_on:
                self.lan_receiver.start(self.current_channel)
        elif self.net_mode == "cloud":
            if self.cloud_receiver:
                self.cloud_receiver.stop()
                self.cloud_receiver.start(self.current_channel)
            # Notify server of channel change
            if self.ws_client and self.ws_client.connected:
                self.ws_client.send_text(json.dumps({"type": "join", "channel": self.current_channel}))
        self.channel_title.text = f"CH {self.current_channel} / {self.total_channels}"
        self.lcd_status.text = f"Connected to Channel {self.current_channel}"

    def toggle_power(self, *args):
        self.is_powered_on = not self.is_powered_on
        if self.is_powered_on:
            if self.net_mode == "lan":
                self.lan_receiver.start(self.current_channel)
            elif self.net_mode == "cloud" and self.cloud_receiver:
                self.cloud_receiver.start(self.current_channel)
            self.lcd_status.text = f"Online: Channel {self.current_channel}"
            self.led_label.text = "● ON AIR"
        else:
            if self.net_mode == "lan":
                self.lan_receiver.stop()
            elif self.net_mode == "cloud" and self.cloud_receiver:
                self.cloud_receiver.stop()
            self.lcd_status.text = "Muted / Standby"
            self.led_label.text = "○ OFF"

    def on_desktop_key_down(self, window, keycode, scancode, codepoint, modifiers):
        if keycode == 32:  # Spacebar on desktop
            self.on_ptt_press()
            return True
        return False

    def on_desktop_key_up(self, window, keycode, scancode):
        if keycode == 32:
            self.on_ptt_release()
            return True
        return False

    def on_ptt_press(self):
        if not self.is_powered_on:
            return
        self.ptt_btn.background_color = (0.0, 0.7, 0.2, 1) if not self.is_private_mode else (0.85, 0.45, 0.0, 1)
        self.ptt_btn.text = ">> TRANSMITTING <<\n[size=12sp]Voice streaming to channel...[/size]"

        if self.net_mode == "cloud" and self.cloud_transmitter:
            self.lcd_status.text = f">> TX: Cloud CH {self.current_channel} <<"
            self.cloud_transmitter.start(self.current_channel)
        else:
            self.lcd_status.text = f">> TX: Broadcasting on CH {self.current_channel} <<"
            self.lan_transmitter.start(self.current_channel)

    def on_ptt_release(self):
        if self.net_mode == "cloud" and self.cloud_transmitter:
            self.cloud_transmitter.stop()
        else:
            self.lan_transmitter.stop()
        self.ptt_btn.background_color = (0.14, 0.15, 0.18, 1)
        self.ptt_btn.text = "◯   HOLD TO TALK   ◯\n[size=12sp](Or Hold Physical Volume-Down Button)[/size]"
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
        self.lan_transmitter.stop()
        self.lan_receiver.stop()
        if self.cloud_transmitter:
            self.cloud_transmitter.stop()
        if self.cloud_receiver:
            self.cloud_receiver.stop()
        if self.ws_client:
            self.ws_client.close()

if __name__ == '__main__':
    WalkieTalkieApp().run()
