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


def note_lines(entrypoints, unknown_keys):
    """The NOTE lines for a build: sessions left out by entrypoint, and user records dropped for a key this tool does not know."""
    lines = []
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
    return lines
