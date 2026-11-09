import socket
import threading
import sys
import random
import string
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

def send_loop(channel: int, extra_peers: list):
    """Broadcasts audio to all devices on the channel while is_transmitting is True."""
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

def receive_loop(channel: int):
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

def main():
    global is_transmitting, running
    local_ip = get_my_ip()
    print("=" * 60)
    print("       Multi-User Group Walkie-Talkie (PC Console)")
    print("=" * 60)
    print(f"My PC ID: {MY_DEVICE_ID.decode('ascii')} | My IP: {local_ip}")
    print("\nSelect a Channel (1 to 6) to connect with your Android phones:")
    ch_input = input("Enter Channel [1-6] (Default 1): ").strip()
    channel = int(ch_input) if ch_input in ['1', '2', '3', '4', '5', '6'] else 1

    extra_input = input("Optional direct phone IPs (comma-separated, or press Enter to skip): ").strip()
    extra_peers = [ip.strip() for ip in extra_input.split(',') if ip.strip()]

    print("-" * 60)
    print(f"[+] Connected to Channel {channel} (Zero-config group broadcast)")
    print("[+] Press [ENTER] once to START talking (Unmute)")
    print("[+] Press [ENTER] again to STOP talking (Mute)")
    print("[+] Press Ctrl+C to quit")
    print("-" * 60)

    # Start Receiver thread
    t_recv = threading.Thread(target=receive_loop, args=(channel,), daemon=True)
    t_recv.start()

    # Start Transmitter thread
    t_send = threading.Thread(target=send_loop, args=(channel, extra_peers), daemon=True)
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
