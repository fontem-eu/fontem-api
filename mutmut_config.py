"""mutmut hooks.

Prompt prose is not a contract. The assistant's tool definitions carry long
natural-language `description` strings that the model reads; mutating a word
inside one produces a mutant no test can honestly kill, because pinning that
wording would freeze copy we deliberately keep tuning. Those mutants are
noise that buries the real signal — in the first doc_tools run over in
fontem-community-api they were a large share of the 96 survivors.

Error-message wording is the same class of noise for the same reason. A
tool result the model reads is prose we tune: "unsupported syntax: X" and
"only numbers are allowed" carry no contract a test should freeze, and in
the calc_tools run roughly half the survivors were nothing but the text
inside a `raise`. The condition that decides WHETHER to raise is on its own
line and is still mutated — which is the part that can actually be wrong.

Structure is still mutated: names, parameter names, types, required lists,
enums, and every branch of the dispatch logic — including the forbidden-
keyword tuples the read-only proxies are built on.

Kept in step with fontem-community-api's copy of this file.
"""


def pre_mutation(context):
    line = context.current_source_line.strip()
    # `description=` / `"description": …` — the value is prompt copy.
    if '"description"' in line or line.startswith('description'):
        context.skip = True
        return
    # A prose fragment of an implicitly-concatenated description block.
    # It opens with a quote and carries no dict braces — that last part is
    # what keeps `"parameters": {"type": "object", …}` (structure, dense
    # with spaces AND colons) from being swallowed. Prose may contain a
    # colon mid-sentence ("about: title, "), so colons alone cannot decide.
    if line.startswith(('"', "'")) and '{' not in line and '}' not in line:
        if ':' not in line or line.count(' ') >= 4:
            context.skip = True
            return
    # The message inside a `raise`. The guard that decides whether to raise
    # sits on the `if` above it and is still mutated; this line is the
    # sentence the model reads afterwards. Skipped only when the raise is
    # self-contained prose — a raise carrying a comparison or arithmetic
    # keeps its mutants.
    if line.startswith('raise ') and ('"' in line or "'" in line):
        if not any(op in line for op in ('==', '!=', '<', '>', ' + ', ' - ')):
            context.skip = True
