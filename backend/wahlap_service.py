"""Socket-activated, one-connection broker; game credentials remain in memory only."""

import json
import sys

from backend import wahlap


def dispatch(request: object) -> dict:
    if not isinstance(request, dict):
        raise wahlap.WahlapError("invalid_response")
    if request.get("kind") == "probe" and set(request) == {"kind"}:
        return {"ok": True, **wahlap._local_available()}
    if request.get("kind") == "network_probe" and set(request) == {"kind"}:
        return wahlap._probe_network_local()
    if request.get("kind") == "fetch" and set(request) == {"kind", "sgid"}:
        return {"ok": True, **wahlap._fetch_scores_local(wahlap.validate_sgid(request["sgid"]))}
    raise wahlap.WahlapError("invalid_response")


def serve(source, destination):
    try:
        raw = source.read(4097)
        if len(raw) > 4096:
            raise wahlap.WahlapError("invalid_response")
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            raise wahlap.WahlapError("invalid_response") from None
        response = dispatch(request)
    except wahlap.WahlapError as exc:
        response = wahlap.error_response(exc)
    except Exception:
        response = {"ok": False, "error": "worker_failed"}
    try:
        destination.write(json.dumps(response, allow_nan=False, separators=(",", ":")).encode())
        destination.flush()
    except (BrokenPipeError, OSError):
        pass


def main():
    serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
