# Call Safety video room

A small two-person video chat demo with WebRTC, room chat, and private advisory
warnings. It is designed to present the idea end to end. It is not a production
meeting service or an automated decision system.

## Run it locally

From the project root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="your-key"
python3 demo/server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Enter a name, leave the
room code blank to create a room, and accept the monitoring notice. Copy the
invite link from the call header and open it in a second browser/device. For a
second device on the public internet, deploy the app over HTTPS as described
below; browsers do not allow camera/microphone access on an ordinary remote
HTTP address.

The API key stays on the server. Without `OPENAI_API_KEY`, video chat and chat
still work, but AI checks show as unavailable and no warning checks run.

## Deploy the backend and page on Render

The root `render.yaml` defines a single Render web service that hosts both the
Python API/signaling server and this page. Render's edge provides HTTPS for the
service; the server listens on `0.0.0.0:$PORT` as required for Render web
services.

1. Push this project to a GitHub, GitLab, or Bitbucket repository.
2. In Render, choose **New → Blueprint**, connect the repository, and deploy
   the `render.yaml` blueprint.
3. When Render asks for `OPENAI_API_KEY`, enter it as a secret value. Do not
   add it to the repository or the browser code.
4. Open the generated `onrender.com` HTTPS address. Create a room, copy the
   invite link, and send it to the second participant.

The free web service is suitable for a presentation, but it can spin down after
15 minutes without inbound traffic, and the next request needs a cold start.
The room state is held only in server memory, so a restart or spin-down clears
active rooms. A paid single-instance service avoids idle spin-down; durable
rooms would need shared storage and signaling designed for multiple instances.

WebRTC uses public STUN servers for peer discovery. Some networks require a
TURN relay; configure a TURN provider before relying on calls across restrictive
firewalls. The current demo has no sign-in, authentication, end-to-end encrypted
signaling, TURN credentials, or abuse controls. Do not use it for sensitive or
high-stakes meetings.

## What the checks do

- Chat text is sent to `omni-moderation-latest` for hate and harassment
  category scores.
- Audio is sampled in short chunks, transcribed with `gpt-transcribe`, and the
  transcript is passed to `omni-moderation-latest`.
- Video is sampled as JPEG frames and sent to the moderation endpoint. The
  `sexual` category is used for possible explicit content; `violence` and
  `violence/graphic` are used as a broad possible-injury signal.
- The warning engine applies confidence thresholds, repeated-frame windows,
  and cooldowns. Warnings are sent privately to the participant whose own
  message/media was analyzed. The app does not mute, remove, or report anyone.

The hosted moderation model does not specifically identify lacerations or
determine self-harm intent. It can miss content or return false positives.
Treat each message as a fallible reminder, never proof. Example thresholds in
`demo/server.py` are starting values for a demo, not validated policy settings.

Before joining, each participant must check the monitoring notice. It explains
that room chat, short audio samples, and sampled video frames may go to the
hosted AI service. The demo does not save the submitted media or transcript to
disk; call signaling and chat are held in memory while the room is active. The
peer-to-peer call media is also sent to the other participant, like a normal
video call.

## Project files

- `demo/index.html`, `demo/styles.css`, `demo/app.js`: room interface, WebRTC,
  chat, media sampling, and private warning dashboard.
- `demo/server.py`: static page host, in-memory room signaling, chat moderation,
  detector routes, and warning delivery.
- `moderation.py`: per-participant threshold, repeat-window, and cooldown
  warning engine.
- `detectors.py`: OpenAI audio transcription and text/image moderation calls.
- `render.yaml`: Render Blueprint for the web service.

For library-level use, `OpenAIDetectors.audio_chunk_signal()` transcribes and
moderates an audio chunk; `transcript_signal()` moderates text; and
`frame_signals()` scores one JPEG, PNG, or WebP image. Create a separate
`WarningEngine` per participant per call. Keep the OpenAI key in the server
environment.
