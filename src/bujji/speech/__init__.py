"""Speech subsystem — speech-to-text and text-to-speech backends."""

import importlib

# Optional STT backends — each registers itself via @SpeechRegistry.register()
for _mod in ("faster_whisper", "openai_whisper", "deepgram"):
    try:
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import  -- plugin loader over a fixed in-source module tuple; names are not user input
        importlib.import_module(f".{_mod}", __name__)
    except ImportError:
        pass

# Optional TTS backends — each registers itself via @TTSRegistry.register()
for _mod in (
    "windows_sapi_tts",
    "edge_tts",
    "cartesia_tts",
    "kokoro_tts",
    "openai_tts",
    "mms_tts",
):
    try:
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import  -- plugin loader over a fixed in-source module tuple; names are not user input
        importlib.import_module(f".{_mod}", __name__)
    except ImportError:
        pass
