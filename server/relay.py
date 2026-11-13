"""
Walkie-Talkie WebSocket Relay Server
=====================================
Forwards audio packets between clients on the same channel.

Packet format (binary): b'WALK' + 4-byte-device-id + 1-byte-channel + raw-pcm-audio
JSON join message:      {"type": "join", "channel": <int>}
"""

import asyncio
import json
import os
import struct
import logging
from collections import defaultdict

import websockets
from websockets.http import Headers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relay")

# channel (int) -> set of connected websocket clients
channels: dict[int, set] = defaultdict(set)
# websocket -> set of channels the client is on
client_channels: dict = {}

MAGIC = b"WALK"
HEADER_LEN = 4 + 4 + 1  # magic(4) + device_id(4) + channel(1)


async def health_check(path, request_headers):
    """Respond to plain HTTP GET / with 200 OK so Render's health check passes."""
    if path == "/":
        return (200, Headers({"Content-Type": "text/plain"}), b"OK\n")
    # Return None to let websockets handle the connection normally
    return None


async def handler(ws):
    """Handle a single WebSocket client connection."""
    client_channels[ws] = set()
    remote = ws.remote_address
    log.info("Client connected: %s", remote)

    try:
        async for message in ws:
            if isinstance(message, bytes):
                await handle_audio(ws, message)
            elif isinstance(message, str):
                await handle_text(ws, message)
    except websockets.ConnectionClosed:
        pass
    finally:
        # Clean up: remove client from all channels
        for ch in client_channels.pop(ws, set()):
            channels[ch].discard(ws)
            if not channels[ch]:
                del channels[ch]
        log.info("Client disconnected: %s", remote)


async def handle_audio(sender, data: bytes):
    """Parse an audio packet and relay it to other clients on the same channel."""
    if len(data) < HEADER_LEN:
        return  # malformed packet, ignore

    magic = data[:4]
    if magic != MAGIC:
        return  # not a walkie-talkie packet

    channel = data[8]  # 1 byte after magic(4) + device_id(4)

    # Auto-join the channel on first audio send
    if channel not in client_channels.get(sender, set()):
        join_channel(sender, channel)

    # Forward to everyone else on this channel
    peers = [c for c in channels[channel] if c is not sender]
    if peers:
        await asyncio.gather(
            *(peer.send(data) for peer in peers),
            return_exceptions=True,
        )


async def handle_text(ws, text: str):
    """Handle JSON text messages (e.g. channel join requests)."""
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return

    if msg.get("type") == "join":
        ch = msg.get("channel")
        if isinstance(ch, int) and 0 <= ch <= 255:
            join_channel(ws, ch)
            await ws.send(json.dumps({"type": "joined", "channel": ch}))
            log.info("Client %s joined channel %d via JSON", ws.remote_address, ch)


def join_channel(ws, channel: int):
    """Register a client on a channel."""
    channels[channel].add(ws)
    client_channels[ws].add(channel)
    log.info("Client %s on channel %d (%d peers)", ws.remote_address, channel, len(channels[channel]))


async def main():
    port = int(os.environ.get("PORT", 10000))
    log.info("Starting relay server on 0.0.0.0:%d", port)

    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        process_request=health_check,
        ping_interval=30,
        ping_timeout=10,
        max_size=2**16,  # 64 KB max message — plenty for audio chunks
    ):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
