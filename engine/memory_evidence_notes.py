"""Plain-words notes the evidence builder prints after a build, so a person learns why fewer digests came out than expected.

Standard library only. Names are counted by the digest module after it cleaned them to letters, digits, underscore and hyphen,
so nothing printed here can carry a control character or a long string from a transcript.
"""

MAX_NAMES_SHOWN = 6


def _shown(counter):
    pairs = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[
        :MAX_NAMES_SHOWN
    ]
    return ", ".join("%s=%d" % pair for pair in pairs)


SIZE_CAP_NOTE = (
    "NOTE up to %d sessions got no digest because the evidence folder reached its 512 KiB cap (some of them would have been "
    "skipped anyway, for example a session nobody typed in). Digests cost nothing to rebuild from your transcripts: remove "
    "the folder (memory_evidence.py purge-plan prints the command) and build again, and the newest sessions will fill it."
)
INVALID_NOTE = (
    "NOTE %d session(s) were not written because their digest would not pass verify (an unusual header or line). Nothing was "
    "lost: the transcripts are untouched. If this keeps happening, please report it without pasting the transcript."
)
NO_DIGEST_NOTE = (
    "NOTE no digest was written. Skipped, by count: %s. 'recent' means idle for less than six hours (finish or close the "
    "session, or wait); 'no_user' means nobody typed in the session; 'non_cli' means the session did not start at a terminal."
)


def note_lines(entrypoints, unknown_keys, skipped=None, written=1):
    """The NOTE lines for a build: why fewer digests came out than expected."""
    lines = []
    skipped = skipped or {}
    if entrypoints:
        lines.append(
            "NOTE sessions left out because of their entrypoint: %s. Only a terminal session (entrypoint cli) counts as a person "
            "typing unless you say otherwise; headless runs (sdk-*) should stay out. If one of these names is an editor or app "
            "you type in yourself, add --include-entrypoint NAME." % _shown(entrypoints)
        )
    if unknown_keys:
        lines.append(
            "NOTE user records left out for safety because they carry keys this tool does not know: %s. A newer Claude Code may "
            "have added them; until this tool learns them those lines cannot be quoted as evidence. Please report the names."
            % _shown(unknown_keys)
        )
    if skipped.get("size_cap"):
        lines.append(SIZE_CAP_NOTE % skipped["size_cap"])
    if skipped.get("invalid"):
        lines.append(INVALID_NOTE % skipped["invalid"])
    if not written and skipped:
        lines.append(NO_DIGEST_NOTE % _shown(skipped))
    return lines
