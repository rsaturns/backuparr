"""Read restore metadata without materializing legacy artwork lists."""
from contextlib import closing, suppress

try:
    # The Python fallback can retain previously parsed text at chunk boundaries.
    # Select the bounded C parser explicitly, regardless of IJSON_BACKEND.
    from ijson.backends.yajl2_c import basic_parse_coro
except ImportError:
    raise RuntimeError(
        "Plex restore agent requires ijson's yajl2_c backend; "
        "install the ijson binary wheel or rebuild the agent image"
    ) from None

from ijson.common import JSONError
from ijson.utils import sendable_list

from plex_restore_agent.docker import AgentError


class ManifestStream:
    def __init__(self, source, limit):
        self.source = source
        self.limit = limit
        self.total = 0
        self.since_event = 0

    def read(self, size=-1):
        data = self.source.read(min(size, 64 * 1024) if size >= 0 else 64 * 1024)
        self.total += len(data)
        self.since_event += len(data)
        if self.total > self.limit:
            raise AgentError("Backup manifest exceeds the configured size limit")
        # A huge string/number (or whitespace run) must not force the streaming
        # parser to accumulate an unbounded token. Normal legacy artwork lists
        # yield an event per URL and do not consume this budget cumulatively.
        if self.since_event > 1024 * 1024:
            raise AgentError("Backup manifest contains an oversized JSON value")
        return data


def manifest_events(stream):
    events = sendable_list()
    parser = basic_parse_coro(events)
    try:
        while chunk := stream.read(64 * 1024):
            parser.send(chunk)
            for event in events:
                stream.since_event = 0
                yield event
            events.clear()
        parser.close()  # Validate EOF, including an incomplete final JSON value.
    finally:
        # Explicitly close even after an I/O error or an early schema rejection.
        # A secondary incomplete-JSON error must not mask the original failure.
        with suppress(JSONError):
            parser.close()


def read_manifest(source, limit):
    stream = ManifestStream(source, limit)
    try:
        # Basic events avoid constructing dotted prefixes for deeply nested
        # input, and let us distinguish literal keys from actual nesting.
        with closing(manifest_events(stream)) as events:
            return collect_manifest(events)
    except (JSONError, UnicodeError):
        raise AgentError("Invalid or corrupt Plex backup manifest") from None


def collect_manifest(events):
    fields = {"format", "format_version", "database_archive", "server"}
    identity_fields = {"version", "machine_identifier"}
    manifest, identity, seen = {}, {}, set()
    depth = 0
    key = identity_key = None
    for event, value in events:
        if event in ("start_map", "start_array"):
            if depth == 0 and event != "start_map":
                raise AgentError("Expected a Plex backup manifest object")
            if depth == 1 and key in fields:
                if key != "server" or event != "start_map":
                    raise AgentError("Invalid Plex backup manifest field")
                manifest["server"] = identity
            if depth == 2 and key == "server" and identity_key in identity_fields:
                raise AgentError("Invalid Plex server identity")
            depth += 1
            if depth > 32:
                raise AgentError("Backup manifest is nested too deeply")
        elif event in ("end_map", "end_array"):
            depth -= 1
        elif event == "map_key":
            if depth == 1:
                key = value
                if key in fields:
                    if key in seen:
                        raise AgentError("Duplicate Plex backup manifest field")
                    seen.add(key)
            elif depth == 2 and key == "server":
                identity_key = value
                if identity_key in identity:
                    raise AgentError("Duplicate Plex server identity field")
        elif depth == 1 and key in fields:
            manifest[key] = value
        elif depth == 2 and key == "server" and identity_key in identity_fields:
            identity[identity_key] = value
    # Consume the entire document, including ignored fields, to check JSON
    # syntax and the ZIP member's CRC before a restore can stop Plex.
    return manifest
