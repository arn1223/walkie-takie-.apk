import socket
import threading
import sys
import numpy as np
import sounddevice as sd

# Audio Stream Settings
SAMPLE_RATE = 16000  # 16 kHz is standard for voice (low bandwidth, clear speech)
CHANNELS = 1         # Mono
CHUNK_SIZE = 1024    # 1024 samples per frame (~64 ms per packet)
DTYPE = 'int16'      # 16-bit signed PCM (2 bytes per sample)

# Network Settings
BUFFER_SIZE = CHUNK_SIZE * 2 * CHANNELS  # 2048 bytes per UDP payload
running = True

def send_audio(target_ip: str, target_port: int):
    """Captures audio from microphone and transmits packets over UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"\n[Transmitter] Streaming audio to {target_ip}:{target_port} ...")
    print("[Transmitter] Speak into your microphone (press Ctrl+C to stop).")

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE) as stream:
            while running:
                audio_chunk, _ = stream.read(CHUNK_SIZE)
                # Apply 2.5x microphone boost with int16 clipping
                boosted = np.clip(audio_chunk.astype(np.float32) * 2.5, -32768, 32767).astype(np.int16)
                sock.sendto(boosted.tobytes(), (target_ip, target_port))
    except Exception as e:
        if running:
            print(f"[Transmitter Error] {e}")
    finally:
        sock.close()

def receive_audio(listen_port: int):
    """Listens for UDP audio packets and plays them through the speaker."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(('0.0.0.0', listen_port))
        print(f"[Receiver] Listening on port {listen_port} ...")

        with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE, blocksize=CHUNK_SIZE) as stream:
            while running:
                packet, _ = sock.recvfrom(BUFFER_SIZE)
                if not packet:
                    break
                audio_data = np.frombuffer(packet, dtype=np.int16)
                # Apply 3.0x speaker boost with int16 clipping
                boosted = np.clip(audio_data.astype(np.float32) * 3.0, -32768, 32767).astype(np.int16)
                stream.write(boosted)
    except Exception as e:
        if running:
            print(f"[Receiver Error] {e}")
    finally:
        sock.close()

def get_local_ip():
    """Detects the machine's local LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting to a public address (does not actually send packets)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def main():
    global running
    local_ip = get_local_ip()
    print("=" * 55)
    print("       Python Real-Time Voice Communicator (UDP)")
    print("=" * 55)
    print(f"Your Local IP Address: {local_ip}")
    print("\nSelect Mode:")
    print("  [1] Loopback Test (Test Mic & Speaker on this computer)")
    print("  [2] Peer A (Host: Listens on port 50001, sends to 50002)")
    print("  [3] Peer B (Client: Listens on port 50002, sends to 50001)")
    print("=" * 55)

    choice = input("Enter choice (1/2/3): ").strip()

    if choice == '1':
        print("\n[*] Starting Local Loopback Test (Use headphones to avoid feedback loop!)...")
        t_recv = threading.Thread(target=receive_audio, args=(50005,), daemon=True)
        t_recv.start()
        send_audio('127.0.0.1', 50005)

    elif choice == '2':
        peer_ip = input("\nEnter Peer B's IP address: ").strip()
        t_recv = threading.Thread(target=receive_audio, args=(50001,), daemon=True)
        t_recv.start()
        send_audio(peer_ip, 50002)

    elif choice == '3':
        peer_ip = input("\nEnter Peer A's IP address: ").strip()
        t_recv = threading.Thread(target=receive_audio, args=(50002,), daemon=True)
        t_recv.start()
        send_audio(peer_ip, 50001)

    else:
        print("[!] Invalid choice. Exiting.")
        return

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\n[*] Stopping voice transmission...")
        running = False
        sys.exit(0)

if __name__ == '__main__':
    main()
