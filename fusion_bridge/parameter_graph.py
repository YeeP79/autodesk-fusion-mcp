"""Fusion-free parameter expression parsing and dependency traversal.

Fusion exposes ``Parameter.dependentParameters`` / ``dependencyParameters``,
which the ``parameters`` tool prefers. This module is the fallback (and the
cross-check): it pulls parameter references out of expression strings and
walks a dependents graph given as a callable, so it is testable with fakes.
"""

import re
from collections import deque

# Unit names Fusion accepts in expressions. An identifier in this set is never
# treated as a parameter reference.
UNIT_WORDS = frozenset({
    # length
    "in", "inch", "inches", "ft", "foot", "feet", "yd", "mi", "mil", "thou",
    "mm", "cm", "m", "km", "um", "micron", "nm",
    # angle
    "deg", "rad", "grad", "rev",
    # mass, force, time and friends
    "kg", "g", "mg", "lb", "lbm", "lbmass", "oz", "slug", "N", "lbf", "s", "sec",
    "min", "hr", "h", "ms", "Hz", "J", "W", "Pa", "psi", "K", "C", "F",
})

# Named constants and keywords that are not parameters.
CONSTANTS = frozenset({"PI", "pi", "E", "true", "false"})

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>'[^']*')
  | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[^\W\d]\w*)
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def expression_identifiers(expression):
    """Return the identifiers in ``expression`` that may name parameters.

    Skips quoted text literals, numbers, function calls (an identifier
    followed by ``(``), unit names, units written after a number or ``)``
    (``2in``, ``2 mm``, ``(a + b) mm``) and named constants. Order of first
    appearance is kept; duplicates are dropped.
    """
    if not isinstance(expression, str) or not expression:
        return []
    tokens = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN.match(expression, pos)
        kind = match.lastgroup
        text = match.group()
        # A quote straight after a number is the feet mark (5'), not a string.
        if kind == "string" and tokens and tokens[-1][0] == "number" and tokens[-1][2] == pos:
            kind, text = "other", "'"
        end = pos + len(text)
        if kind != "space":
            tokens.append((kind, text, end))
        pos = end

    found = []
    for index, (kind, text, end) in enumerate(tokens):
        if kind != "ident":
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is not None and following[1] == "(":
            continue
        previous = tokens[index - 1] if index > 0 else None
        if previous is not None and (previous[0] in ("number", "unit") or previous[1] == ")"):
            tokens[index] = ("unit", text, end)
            continue
        if text in UNIT_WORDS or text in CONSTANTS:
            continue
        if text not in found:
            found.append(text)
    return found


def parameter_references(expression, known_names, self_name=None):
    """Identifiers in ``expression`` that are existing parameter names."""
    return [
        name for name in expression_identifiers(expression)
        if name in known_names and name != self_name
    ]


def build_dependents(expressions):
    """Map each parameter name to the names whose expressions reference it.

    ``expressions`` maps parameter name -> expression. Every name appears as a
    key; lists keep the iteration order of ``expressions``.
    """
    known = set(expressions)
    dependents = {name: [] for name in expressions}
    for name, expression in expressions.items():
        for ref in parameter_references(expression, known, self_name=name):
            dependents[ref].append(name)
    return dependents


def transitive_dependents(root, dependents_of, max_depth=5, max_nodes=500):
    """Breadth-first walk of everything that depends on ``root``.

    ``dependents_of(name)`` returns the direct dependents of ``name``.
    Returns ``(nodes, truncated)`` where each node is
    ``{"name", "depth", "via"}`` (``via`` is the parameter it was reached
    from) and ``truncated`` is true when the depth or node cap cut the walk
    short. Cycles are visited once.
    """
    nodes = []
    seen = {root}
    queue = deque([(root, 0)])
    truncated = False
    while queue:
        name, depth = queue.popleft()
        children = [c for c in dependents_of(name) if c not in seen]
        if not children:
            continue
        if depth >= max_depth:
            truncated = True
            continue
        for child in children:
            if len(nodes) >= max_nodes:
                return nodes, True
            seen.add(child)
            nodes.append({"name": child, "depth": depth + 1, "via": name})
            queue.append((child, depth + 1))
    return nodes, truncated


def would_create_cycle(target, new_references, dependents_of, max_nodes=5000):
    """Names in ``new_references`` that would make ``target`` depend on itself.

    Setting ``target``'s expression to reference ``r`` is a cycle when ``r`` is
    ``target`` or already depends (transitively) on ``target``.
    """
    downstream, _ = transitive_dependents(target, dependents_of, max_depth=max_nodes, max_nodes=max_nodes)
    downstream_names = {node["name"] for node in downstream}
    return [r for r in new_references if r == target or r in downstream_names]


def normalize_expression(expression):
    """Whitespace-insensitive form for comparing an expression read back from Fusion."""
    return re.sub(r"\s+", "", expression or "")
