import socket
import threading
import sys
import random
import string
import json
import struct
import ssl
import os
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
DTYPE = 'int16'
BUFFER_SIZE = CHUNK_SIZE * 2
MAGIC_HEADER = b'WALK'
BASE_PORT = 50000

MY_DEVICE_ID = ('PC' + ''.join(random.choices(string.digits, k=2))).encode('ascii')
running = True
is_transmitting = False

# ─── Minimal WebSocket client (same as in main.py, no pip deps) ─────────────

class SimpleWebSocket:
    """Minimal RFC 6455 WebSocket client using raw sockets."""

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
        import base64 as _b64
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
        import base64
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

            ws_key = base64.b64encode(os.urandom(16)).decode('ascii')
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

            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()
            return True

        except Exception as e:
            print(f"[WS] Connect error: {e}")
            return False

    def send_binary(self, data):
        if not self.connected or not self.sock:
            return
        try:
            frame = self._build_frame(data, opcode=0x02)
            self.sock.sendall(frame)
        except Exception as e:
            print(f"[WS] Send error: {e}")
            self._close_internal()

    def send_text(self, text):
        if not self.connected or not self.sock:
            return
        try:
            frame = self._build_frame(text.encode('utf-8'), opcode=0x01)
            self.sock.sendall(frame)
        except Exception as e:
            print(f"[WS] Send error: {e}")
            self._close_internal()

    def close(self):
        if self.connected:
            try:
                frame = self._build_frame(b'', opcode=0x08)
                self.sock.sendall(frame)
            except Exception:
                pass
        self._close_internal()

    def _build_frame(self, payload, opcode=0x02):
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)

        mask = os.urandom(4)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        return header + mask + bytes(masked)

    def _recv_loop(self):
        try:
            while self.connected:
                data = self._read_frame()
                if data is None:
                    break
                opcode, payload = data
                if opcode == 0x08:
                    break
                elif opcode == 0x09:
                    try:
                        pong = self._build_frame(payload, opcode=0x0A)
                        self.sock.sendall(pong)
                    except Exception:
                        break
                elif opcode in (0x01, 0x02):
                    if self.on_message:
                        self.on_message(payload, opcode)
        except Exception as e:
            if self.connected:
                print(f"[WS] Recv error: {e}")
        finally:
            self._close_internal()

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _read_frame(self):
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
            mask_key = self._read_exact(4)
            if not mask_key:
                return None

        payload = self._read_exact(length) if length > 0 else b""
        if payload is None:
            return None

        if masked:
            payload = bytearray(payload)
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]
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


# ─── LAN Broadcast functions (original) ─────────────────────────────────────

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

def send_loop_lan(channel: int, extra_peers: list):
    """Broadcasts audio to all devices on the channel via LAN while is_transmitting is True."""
    global running, is_transmitting
    port = BASE_PORT + channel
    targets = get_broadcast_targets(port)
    for peer in extra_peers:
        targets.append((peer, port))

    header = MAGIC_HEADER + MY_DEVICE_ID + bytes([channel])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE) as stream:
        while running:
            if is_transmitting:
                audio_chunk, _ = stream.read(CHUNK_SIZE)
                # 2.5x Mic Boost
                boosted = np.clip(audio_chunk.astype(np.float32) * 2.5, -32768, 32767).astype(np.int16)
                packet = header + boosted.tobytes()
                for addr, p in targets:
                    try:
                        sock.sendto(packet, (addr, p))
                    except Exception:
                        pass
            else:
                sd.sleep(20)

def receive_loop_lan(channel: int):
    """Listens for audio broadcasts from all phones on the channel."""
    global running
    port = BASE_PORT + channel
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass

    try:
        sock.bind(('', port))
        print(f"[*] Receiver listening on Channel {channel} (Port {port})...")
    except Exception as e:
        print(f"[!] Bind error: {e}")
        return

    with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE) as stream:
        while running:
            try:
                packet, addr = sock.recvfrom(BUFFER_SIZE + 32)
                if not packet or len(packet) < 9:
                    continue
                if packet[:4] != MAGIC_HEADER:
                    continue
                sender = packet[4:8]
                if sender == MY_DEVICE_ID:
                    continue  # Ignore self-echo
                channel_in_pkt = packet[8]
                if channel_in_pkt != channel:
                    continue

                raw_audio = packet[9:]
                audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                # 3.0x Loudspeaker Boost
                boosted = np.clip(audio_data.astype(np.float32) * 3.0, -32768, 32767).astype(np.int16)
                stream.write(boosted)
            except Exception:
                pass


# ─── Cloud Relay functions ───────────────────────────────────────────────────

def send_loop_cloud(channel: int, ws: SimpleWebSocket):
    """Sends audio to the relay server via WebSocket while is_transmitting is True."""
    global running, is_transmitting
    header = MAGIC_HEADER + MY_DEVICE_ID + bytes([channel])

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE) as stream:
        while running:
            if is_transmitting:
                audio_chunk, _ = stream.read(CHUNK_SIZE)
                boosted = np.clip(audio_chunk.astype(np.float32) * 2.5, -32768, 32767).astype(np.int16)
                packet = header + boosted.tobytes()
                if ws.connected:
                    ws.send_binary(packet)
            else:
                sd.sleep(20)

def receive_loop_cloud(channel: int, ws: SimpleWebSocket):
    """Audio playback is driven by WebSocket on_message callback; this is a no-op placeholder."""
    # The WebSocket recv loop already runs inside SimpleWebSocket._recv_loop.
    # Audio packets arrive via the on_message callback and are played directly.
    pass


# ─── Main entry point ───────────────────────────────────────────────────────

def main():
    global is_transmitting, running
    local_ip = get_my_ip()
    print("=" * 60)
    print("       Multi-User Group Walkie-Talkie (PC Console)")
    print("=" * 60)
    print(f"My PC ID: {MY_DEVICE_ID.decode('ascii')} | My IP: {local_ip}")

    # Mode selection
    print("\nSelect mode:")
    print("  1. LAN Mode (local Wi-Fi, zero-config)")
    print("  2. Cloud Mode (Render relay, global internet)")
    mode_input = input("Enter mode [1/2] (Default 2 - Cloud): ").strip()
    use_cloud = mode_input != "1"

    print("\nSelect a Channel (1 to 6) to connect with your Android phones:")
    ch_input = input("Enter Channel [1-6] (Default 1): ").strip()
    channel = int(ch_input) if ch_input in ['1', '2', '3', '4', '5', '6'] else 1

    if use_cloud:
        default_url = "wss://walkie-takie-relay.onrender.com/ws"
        url_prompt = f"Enter relay URL (Press Enter for default: {default_url}): "
        relay_url = input(url_prompt).strip()
        if not relay_url:
            relay_url = default_url
        else:
            # Normalize URL
            if not relay_url.startswith("ws://") and not relay_url.startswith("wss://"):
                if relay_url.startswith("https://"):
                    relay_url = "wss://" + relay_url[8:]
                elif relay_url.startswith("http://"):
                    relay_url = "ws://" + relay_url[7:]
                else:
                    relay_url = "wss://" + relay_url
            if not relay_url.endswith("/ws"):
                if relay_url.endswith("/"):
                    relay_url += "ws"
                else:
                    relay_url += "/ws"

    if use_cloud:
        print("-" * 60)
        print(f"[+] Connecting to relay: {relay_url}")
        print(f"[+] Channel {channel}")

        # Setup audio output stream for cloud received audio
        out_stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE)
        out_stream.start()

        def on_ws_message(data, opcode):
            if opcode == 0x02:  # Binary = audio packet
                if not data or len(data) < 9 or data[:4] != MAGIC_HEADER:
                    return
                sender = data[4:8]
                if sender == MY_DEVICE_ID:
                    return
                pkt_channel = data[8]
                if pkt_channel != channel:
                    return
                raw_audio = data[9:]
                audio_data = np.frombuffer(raw_audio, dtype=np.int16)
                boosted = np.clip(audio_data.astype(np.float32) * 3.0, -32768, 32767).astype(np.int16)
                try:
                    out_stream.write(boosted)
                except Exception:
                    pass
            elif opcode == 0x01:  # Text = control message
                try:
                    msg = json.loads(data.decode('utf-8'))
                    print(f"[Server] {msg.get('message', msg)}")
                except Exception:
                    pass

        def on_ws_close():
            print("[!] Relay disconnected.")

        ws = SimpleWebSocket(relay_url, on_message=on_ws_message, on_close=on_ws_close)
        if not ws.connect():
            print("[!] Failed to connect to relay. Exiting.")
            sys.exit(1)

        print("[+] Connected to relay server!")

        # Join channel
        ws.send_text(json.dumps({"type": "join", "channel": channel}))

        # Start cloud transmitter thread
        t_send = threading.Thread(target=send_loop_cloud, args=(channel, ws), daemon=True)
        t_send.start()

        print("[+] Press [ENTER] once to START talking (Unmute)")
        print("[+] Press [ENTER] again to STOP talking (Mute)")
        print("[+] Press Ctrl+C to quit")
        print("-" * 60)

        try:
            while True:
                input()
                is_transmitting = not is_transmitting
                if is_transmitting:
                    print(">> [TALKING...] Your microphone is broadcasting via cloud relay! <<")
                else:
                    print("-- [MUTED] Listening for other speakers... --")
        except KeyboardInterrupt:
            print("\n[*] Exiting Walkie-Talkie...")
            running = False
            ws.close()
            out_stream.stop()
            out_stream.close()
            sys.exit(0)

    else:
        # LAN Mode (original)
        extra_input = input("Optional direct phone IPs (comma-separated, or press Enter to skip): ").strip()
        extra_peers = [ip.strip() for ip in extra_input.split(',') if ip.strip()]

        print("-" * 60)
        print(f"[+] Connected to Channel {channel} (Zero-config group broadcast)")
        print("[+] Press [ENTER] once to START talking (Unmute)")
        print("[+] Press [ENTER] again to STOP talking (Mute)")
        print("[+] Press Ctrl+C to quit")
        print("-" * 60)

        # Start Receiver thread
        t_recv = threading.Thread(target=receive_loop_lan, args=(channel,), daemon=True)
        t_recv.start()

        # Start Transmitter thread
        t_send = threading.Thread(target=send_loop_lan, args=(channel, extra_peers), daemon=True)
        t_send.start()

        try:
            while True:
                input()
                is_transmitting = not is_transmitting
                if is_transmitting:
                    print(">> [TALKING...] Your microphone is broadcasting to all phones! <<")
                else:
                    print("-- [MUTED] Listening for other speakers... --")
        except KeyboardInterrupt:
            print("\n[*] Exiting Walkie-Talkie...")
            running = False
            sys.exit(0)

if __name__ == '__main__':
    main()
