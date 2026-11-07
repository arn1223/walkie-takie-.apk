import socket
import threading
import sys
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.utils import platform
from kivy.clock import mainthread

# Audio parameters
SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
DTYPE_BYTES = 2  # 16-bit PCM = 2 bytes per sample
BUFFER_SIZE = CHUNK_SIZE * DTYPE_BYTES

class AudioTransmitter:
    """Handles audio capture and UDP streaming."""
    def __init__(self):
        self.is_recording = False
        self.thread = None
        self.target_ip = "127.0.0.1"
        self.target_port = 50002
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self, target_ip, target_port):
        self.target_ip = target_ip
        self.target_port = target_port
        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_recording = False

    def _record_loop(self):
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
            byte_buf = bytearray(BUFFER_SIZE)

            try:
                while self.is_recording:
                    read_bytes = recorder.read(byte_buf, 0, len(byte_buf))
                    if read_bytes > 0:
                        self.sock.sendto(bytes(byte_buf[:read_bytes]), (self.target_ip, self.target_port))
            finally:
                recorder.stop()
                recorder.release()
        else:
            # Fallback for desktop testing (Windows / Mac / Linux)
            import sounddevice as sd
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
                while self.is_recording:
                    data, _ = stream.read(CHUNK_SIZE)
                    self.sock.sendto(data.tobytes(), (self.target_ip, self.target_port))


class AudioReceiver:
    """Listens for UDP packets and plays audio."""
    def __init__(self, listen_port=50001):
        self.listen_port = listen_port
        self.running = False
        self.thread = None
        self.sock = None

    def start(self, port):
        self.listen_port = port
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
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(('0.0.0.0', self.listen_port))
        except Exception as e:
            print(f"Receiver bind error: {e}")
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
            track = AudioTrack(
                AudioManager.STREAM_VOICE_CALL,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                max(min_buf, BUFFER_SIZE),
                AudioTrack.MODE_STREAM
            )
            track.play()

            try:
                while self.running:
                    packet, _ = self.sock.recvfrom(BUFFER_SIZE)
                    if packet:
                        track.write(packet, 0, len(packet))
            finally:
                track.stop()
                track.release()
        else:
            # Fallback for desktop testing
            import sounddevice as sd
            import numpy as np
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
                while self.running:
                    packet, _ = self.sock.recvfrom(BUFFER_SIZE)
                    if packet:
                        audio_data = np.frombuffer(packet, dtype=np.int16)
                        stream.write(audio_data)


class PTTWalkieTalkieApp(App):
    def build(self):
        self.title = "PTT Walkie Talkie"
        self.transmitter = AudioTransmitter()
        self.receiver = AudioReceiver()

        # Request Android Runtime Permissions
        if platform == 'android':
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.RECORD_AUDIO,
                Permission.INTERNET,
                Permission.ACCESS_NETWORK_STATE
            ])

        # Root UI
        root = BoxLayout(orientation='vertical', padding=20, spacing=15)

        # Header
        header = Label(
            text="[b]Push-to-Talk Walkie Talkie[/b]",
            markup=True,
            font_size='22sp',
            size_hint_y=0.15
        )
        root.add_widget(header)

        # IP Input
        ip_layout = BoxLayout(orientation='horizontal', size_hint_y=0.12, spacing=10)
        ip_label = Label(text="Peer IP:", size_hint_x=0.3, font_size='16sp')
        self.ip_input = TextInput(
            text="192.168.1.100",
            multiline=False,
            font_size='16sp',
            size_hint_x=0.7
        )
        ip_layout.add_widget(ip_label)
        ip_layout.add_widget(self.ip_input)
        root.add_widget(ip_layout)

        # Port Inputs
        port_layout = BoxLayout(orientation='horizontal', size_hint_y=0.12, spacing=10)
        port_layout.add_widget(Label(text="My Port:", size_hint_x=0.25))
        self.my_port = TextInput(text="50001", multiline=False, size_hint_x=0.25)
        port_layout.add_widget(self.my_port)

        port_layout.add_widget(Label(text="Peer Port:", size_hint_x=0.25))
        self.peer_port = TextInput(text="50002", multiline=False, size_hint_x=0.25)
        port_layout.add_widget(self.peer_port)
        root.add_widget(port_layout)

        # Status Label
        self.status_label = Label(
            text="Status: Ready (Receiver Inactive)",
            font_size='15sp',
            size_hint_y=0.1
        )
        root.add_widget(self.status_label)

        # Listen / Connect Toggle Button
        self.listen_btn = Button(
            text="Start Listening",
            background_color=(0.2, 0.6, 0.9, 1),
            size_hint_y=0.15,
            font_size='18sp'
        )
        self.listen_btn.bind(on_release=self.toggle_receiver)
        root.add_widget(self.listen_btn)

        # Large Push-to-Talk Button
        self.ptt_btn = Button(
            text="HOLD TO TALK",
            background_color=(0.8, 0.2, 0.2, 1),
            size_hint_y=0.36,
            font_size='26sp',
            bold=True
        )
        # Bind press and release events
        self.ptt_btn.bind(on_press=self.on_ptt_press)
        self.ptt_btn.bind(on_release=self.on_ptt_release)
        root.add_widget(self.ptt_btn)

        return root

    def toggle_receiver(self, instance):
        if not self.receiver.running:
            try:
                port = int(self.my_port.text.strip())
                self.receiver.start(port)
                self.listen_btn.text = f"Stop Listening (Port {port})"
                self.listen_btn.background_color = (0.9, 0.4, 0.2, 1)
                self.status_label.text = f"Status: Listening on port {port}"
            except ValueError:
                self.status_label.text = "Error: Invalid port number"
        else:
            self.receiver.stop()
            self.listen_btn.text = "Start Listening"
            self.listen_btn.background_color = (0.2, 0.6, 0.9, 1)
            self.status_label.text = "Status: Receiver Stopped"

    def on_ptt_press(self, instance):
        peer_ip = self.ip_input.text.strip()
        try:
            target_port = int(self.peer_port.text.strip())
        except ValueError:
            self.status_label.text = "Error: Invalid peer port"
            return

        self.ptt_btn.background_color = (0.1, 0.8, 0.3, 1)  # Green when transmitting
        self.ptt_btn.text = "TRANSMITTING..."
        self.status_label.text = f"Transmitting audio to {peer_ip}:{target_port}"
        self.transmitter.start(peer_ip, target_port)

    def on_ptt_release(self, instance):
        self.transmitter.stop()
        self.ptt_btn.background_color = (0.8, 0.2, 0.2, 1)  # Back to red
        self.ptt_btn.text = "HOLD TO TALK"
        self.status_label.text = "Status: Idle"

    def on_stop(self):
        self.transmitter.stop()
        self.receiver.stop()

if __name__ == '__main__':
    PTTWalkieTalkieApp().run()
