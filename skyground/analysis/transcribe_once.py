"""Transcribe one file and exit.

The whole point is the exit: the model, its weights and the allocator's
arenas — around a gigabyte while a six minute take is heard — go back to the
system the moment this process ends, instead of staying resident in the worker
for the rest of the day. The worker calls it with one JSON object on stdin and
reads one JSON object from stdout.

    echo '{"audio": "a.wav", "language": "it"}' | python -m skyground.analysis.transcribe_once
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    from skyground.analysis.transcription import LocalWhisperTranscriber

    transcriber = LocalWhisperTranscriber(
        model=request.get("model") or "small",
        compute_type=request.get("compute_type") or "int8",
        cache_root=request.get("cache_root") or None,
        beam_size=int(request.get("beam_size", 1)),
        cpu_threads=int(request.get("cpu_threads", 2)),
        in_process=True,
    )
    words = transcriber.transcribe(request["audio"], language=request.get("language") or "it")
    json.dump({"words": [word.as_dict() for word in words]}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
