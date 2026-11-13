"""Small in-memory signaling and moderation server for the call demo.

Run with ``python3 demo/server.py``. For a remote browser, use HTTPS; camera
and microphone access are restricted to secure contexts by modern browsers.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT_DIR = Path(__file__).resolve().parent.parent
DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from moderation import Rule, SignalKind, WarningEngine  # noqa: E402

try:  # The call itself works without the SDK or a configured key.
    from detectors import OpenAIDetectors  # noqa: E402

    if os.environ.get("OPENAI_API_KEY"):
        DETECTORS = OpenAIDetectors(
            transcription_model=os.environ.get("OPENAI_TRANSCRIPTION_MODEL", "gpt-transcribe"),
            moderation_model=os.environ.get("OPENAI_MODERATION_MODEL", "omni-moderation-latest"),
        )
    else:
        DETECTORS = None
except (ImportError, RuntimeError, ValueError):
    DETECTORS = None

RULES = {
    SignalKind.POTENTIALLY_HARMFUL_LANGUAGE: Rule(
        threshold=0.85, min_hits=1, window_seconds=8, cooldown_seconds=45
    ),
    SignalKind.CHAT_MESSAGE: Rule(
        threshold=0.85, min_hits=1, window_seconds=8, cooldown_seconds=30
    ),
    SignalKind.EXPLICIT_VISUAL: Rule(
        threshold=0.90, min_hits=2, window_seconds=12, cooldown_seconds=60
    ),
    SignalKind.VISIBLE_INJURY: Rule(
        threshold=0.90, min_hits=2, window_seconds=12, cooldown_seconds=60
    ),
}

STATE_LOCK = threading.RLock()
ROOMS: dict[str, dict] = {}
MAX_BODY_BYTES = 25 * 1024 * 1024


def new_room_code() -> str:
    return secrets.token_hex(3).upper()


def new_warning_engine() -> WarningEngine:
    return WarningEngine(RULES)


def emit(room: dict, event_type: str, data: dict, to: str | None = None,
         sender: str | None = None) -> dict:
    event = {
        "id": room["next_event_id"],
        "type": event_type,
        "data": data,
        "to": to,
        "from": sender,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    room["next_event_id"] += 1
    room["events"].append(event)
    if len(room["events"]) > 1500:
        del room["events"][:-1000]
    return event


def json_safe_room(room_code: str, client_id: str) -> tuple[dict | None, str | None]:
    room = ROOMS.get(room_code)
    if not room or client_id not in room["clients"]:
        return None, None
    return room, room["clients"][client_id]["name"]


class CallHandler(SimpleHTTPRequestHandler):
    server_version = "CallSafetyDemo/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def log_message(self, format_string, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, status: int, value: dict):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length <= 0:
            raise ValueError("Request body is empty")
        if length > limit:
            raise OverflowError("Request body is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Request body was incomplete")
        return body

    def read_json(self, limit: int = 2 * 1024 * 1024) -> dict:
        raw = self.read_body(limit)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request JSON must be an object")
        return value

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/api/config":
            self.send_json(200, {"moderationConfigured": DETECTORS is not None})
            return
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            room_code = query.get("room", [""])[0].upper()
            client_id = query.get("client", [""])[0]
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                self.send_json(400, {"error": "Invalid event cursor"})
                return
            with STATE_LOCK:
                room, _ = json_safe_room(room_code, client_id)
                if room is None:
                    self.send_json(404, {"error": "Call participant not found"})
                    return
                events = [
                    event for event in room["events"]
                    if event["id"] > since and (event["to"] is None or event["to"] == client_id)
                ]
                cursor = room["next_event_id"] - 1
            self.send_json(200, {"events": events, "cursor": cursor})
            return
        if parsed.path.startswith("/api/"):
            self.send_json(404, {"error": "Unknown API endpoint"})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/join":
                self.handle_join()
            elif parsed.path == "/api/leave":
                self.handle_leave(self.read_json())
            elif parsed.path == "/api/signal":
                self.handle_signal(self.read_json())
            elif parsed.path == "/api/chat":
                self.handle_chat(self.read_json())
            elif parsed.path == "/api/audio":
                self.handle_audio()
            elif parsed.path == "/api/frame":
                self.handle_frame()
            else:
                self.send_json(404, {"error": "Unknown API endpoint"})
        except OverflowError as exc:
            self.send_json(413, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            self.send_json(400, {"error": str(exc)})
        except BrokenPipeError:
            pass

    def handle_join(self):
        body = self.read_json()
        name = str(body.get("name", "")).strip()[:40]
        room_code = str(body.get("room", "")).strip().upper()
        if not name:
            raise ValueError("Enter a display name")
        if not body.get("consent"):
            raise ValueError("Consent is required to join this monitored demo")
        if room_code and (not room_code.isalnum() or not 4 <= len(room_code) <= 12):
            raise ValueError("Room code must be 4–12 letters or numbers")

        with STATE_LOCK:
            if not room_code:
                room_code = new_room_code()
            room = ROOMS.setdefault(room_code, {
                "clients": {}, "events": [], "next_event_id": 1, "engines": {}
            })
            if len(room["clients"]) >= 2:
                self.send_json(409, {"error": "This room already has two participants"})
                return
            client_id = secrets.token_urlsafe(18)
            existing = list(room["clients"].items())
            room["clients"][client_id] = {"name": name, "joined_at": time.monotonic()}
            room["engines"][client_id] = new_warning_engine()
            peer = existing[0] if existing else None
            if peer:
                emit(
                    room,
                    "peer_joined",
                    {"client": client_id, "name": name},
                    to=peer[0],
                    sender=client_id,
                )
        self.send_json(200, {
            "room": room_code,
            "client": client_id,
            "name": name,
            "peerPresent": peer is not None,
            "peerName": peer[1]["name"] if peer else None,
            "moderationConfigured": DETECTORS is not None,
        })

    def handle_leave(self, body: dict):
        room_code = str(body.get("room", "")).upper()
        client_id = str(body.get("client", ""))
        with STATE_LOCK:
            room, name = json_safe_room(room_code, client_id)
            if room is not None:
                room["clients"].pop(client_id, None)
                room["engines"].pop(client_id, None)
                emit(room, "peer_left", {"client": client_id, "name": name}, sender=client_id)
                if not room["clients"]:
                    ROOMS.pop(room_code, None)
        self.send_json(200, {"ok": True})

    def handle_signal(self, body: dict):
        room_code = str(body.get("room", "")).upper()
        client_id = str(body.get("client", ""))
        signal_type = str(body.get("type", ""))
        if signal_type not in {"offer", "answer", "ice"}:
            raise ValueError("Invalid WebRTC signal type")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Signal payload must be an object")
        with STATE_LOCK:
            room, _ = json_safe_room(room_code, client_id)
            if room is None:
                self.send_json(404, {"error": "Call participant not found"})
                return
            peer_ids = [participant for participant in room["clients"] if participant != client_id]
            if not peer_ids:
                self.send_json(409, {"error": "No other participant is in this room"})
                return
            emit(room, signal_type, payload, to=peer_ids[0], sender=client_id)
        self.send_json(200, {"ok": True})

    def handle_chat(self, body: dict):
        room_code = str(body.get("room", "")).upper()
        client_id = str(body.get("client", ""))
        text = str(body.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise ValueError("Chat messages must be between 1 and 2000 characters")
        with STATE_LOCK:
            room, name = json_safe_room(room_code, client_id)
            if room is None:
                self.send_json(404, {"error": "Call participant not found"})
                return
            event = emit(room, "chat", {"name": name, "text": text}, sender=client_id)

        if DETECTORS is not None:
            try:
                signal = DETECTORS.transcript_signal(
                    text, f"chat-{event['id']}", self.elapsed(room_code, client_id),
                    kind=SignalKind.CHAT_MESSAGE,
                )
                if signal:
                    self.apply_signal(room_code, client_id, signal)
            except Exception as exc:
                self.emit_status(room_code, client_id, f"Chat check unavailable: {exc}")
        self.send_json(200, {"ok": True})

    def handle_audio(self):
        room_code, client_id = self.media_identity()
        audio_bytes = self.read_body()
        if DETECTORS is None:
            self.send_json(503, {"error": "OpenAI moderation is not configured on the server"})
            return
        mime_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip().lower()
        extensions = {
            "audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3",
            "audio/wav": "wav", "audio/x-wav": "wav",
        }
        extension = extensions.get(mime_type)
        if not extension:
            self.send_json(415, {"error": "Unsupported audio type"})
            return
        try:
            signal = DETECTORS.audio_chunk_signal(
                audio_bytes,
                sample_id=f"audio-{secrets.token_urlsafe(10)}",
                timestamp=self.elapsed(room_code, client_id),
                filename=f"meeting-chunk.{extension}",
            )
            if signal:
                self.apply_signal(room_code, client_id, signal)
            self.send_json(200, {"ok": True})
        except Exception as exc:
            self.emit_status(room_code, client_id, f"Audio check unavailable: {exc}")
            self.send_json(502, {"error": "Audio moderation request failed"})

    def handle_frame(self):
        room_code, client_id = self.media_identity()
        image_bytes = self.read_body(20 * 1024 * 1024)
        if DETECTORS is None:
            self.send_json(503, {"error": "OpenAI moderation is not configured on the server"})
            return
        try:
            signals = DETECTORS.frame_signals(
                image_bytes,
                sample_id=f"frame-{secrets.token_urlsafe(10)}",
                timestamp=self.elapsed(room_code, client_id),
            )
            for signal in signals:
                self.apply_signal(room_code, client_id, signal)
            self.send_json(200, {"ok": True})
        except Exception as exc:
            self.emit_status(room_code, client_id, f"Video check unavailable: {exc}")
            self.send_json(502, {"error": "Video moderation request failed"})

    def media_identity(self) -> tuple[str, str]:
        query = parse_qs(urlsplit(self.path).query)
        room_code = query.get("room", [""])[0].upper()
        client_id = query.get("client", [""])[0]
        with STATE_LOCK:
            room, _ = json_safe_room(room_code, client_id)
        if room is None:
            raise ValueError("Call participant not found")
        return room_code, client_id

    @staticmethod
    def elapsed(room_code: str, client_id: str) -> float:
        with STATE_LOCK:
            room, _ = json_safe_room(room_code, client_id)
            if room is None:
                return 0.0
            return max(0.0, time.monotonic() - room["clients"][client_id]["joined_at"])

    @staticmethod
    def apply_signal(room_code: str, client_id: str, signal):
        with STATE_LOCK:
            room, _ = json_safe_room(room_code, client_id)
            if room is None:
                return
            warning = room["engines"][client_id].ingest(signal)
            if warning:
                emit(room, "warning", {
                    "kind": warning.kind.value,
                    "message": warning.message,
                    "confidence": round(warning.confidence, 3),
                }, to=client_id)

    @staticmethod
    def emit_status(room_code: str, client_id: str, message: str):
        with STATE_LOCK:
            room, _ = json_safe_room(room_code, client_id)
            if room is not None:
                emit(room, "monitoring_error", {"message": message[:220]}, to=client_id)


def main():
    parser = argparse.ArgumentParser(description="Run the Call Safety two-person demo")
    parser.add_argument("--host", default=os.environ.get("CALL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", os.environ.get("CALL_PORT", "8000"))))
    parser.add_argument("--cert", default=os.environ.get("CALL_TLS_CERT"))
    parser.add_argument("--key", default=os.environ.get("CALL_TLS_KEY"))
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be supplied together")

    server = ThreadingHTTPServer((args.host, args.port), CallHandler)
    scheme = "http"
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"Call Safety Demo ready at {scheme}://{args.host}:{args.port}")
    if DETECTORS is None:
        print("AI checks disabled. Install the SDK and set OPENAI_API_KEY to enable them.")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping Call Safety Demo.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
