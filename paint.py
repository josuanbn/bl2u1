"""
Per-triangle color painting in Bambu Studio / Orca .3mf files.

Each painted triangle carries a `paint_color="..."` hex string: a TriangleSelector
bitstream. Read as one integer (int(code, 16)), its bits run from bit 0 upward:

    node := split:2 ( state:2 [ext:4]       if split == 0  -- a leaf
                    | side:2 node*(split+1) )  otherwise     -- split into children

A leaf's state is the filament it is painted with (0 = the part's own
filament). States 0-2 fit in the 2 bits; for 3 and up the 2 bits are 3 and the
next 4 bits hold state - 3. The string is the bitstream's nibbles, last first,
in uppercase hex.

Dropping filaments from a project therefore means rewriting every one of these
strings, not just the object's extruder number -- otherwise the paint keeps
pointing at the old filament numbers.
"""
from collections import Counter

MAX_STATE = 18  # 3 + 15: the most a 4-bit extension can hold


def _leaves(code: str):
    """Yield (bit position, bit length, state) for each leaf, plus the structure in between."""
    v = int(code, 16)
    nbits = 4 * len(code)
    pos, todo = 0, 1
    while todo:
        todo -= 1
        if pos + 2 > nbits:
            raise ValueError(f'truncated paint string {code[:40]!r}')
        split = (v >> pos) & 3
        pos += 2
        if split == 0:
            start = pos
            state = (v >> pos) & 3
            pos += 2
            if state == 3:
                state = ((v >> pos) & 15) + 3
                pos += 4
            yield start, pos - start, state
        else:
            pos += 2  # special side
            todo += split + 1
    if pos > nbits:
        raise ValueError(f'truncated paint string {code[:40]!r}')
    if v >> pos:
        raise ValueError(f'data after the last node in paint string {code[:40]!r}')


def states(code: str) -> Counter:
    """How many leaves of each state one paint string holds."""
    return Counter(state for _start, _length, state in _leaves(code))


def _encode_state(state: int) -> tuple[int, int]:
    """(bits, bit length) for one leaf state."""
    if not 0 <= state <= MAX_STATE:
        raise ValueError(f'paint state {state} is out of range 0-{MAX_STATE}')
    if state < 3:
        return state, 2
    return 3 | ((state - 3) << 2), 6


def remap(code: str, mapping: dict) -> str:
    """The paint string with every leaf's state replaced by mapping[state].

    State 0 (the part's own filament) never moves. A state missing from the
    mapping raises KeyError: dropping paint silently is how colors go wrong.
    """
    v = int(code, 16)
    out, out_pos, read = 0, 0, 0
    for start, length, state in _leaves(code):
        # the structure bits between the previous leaf and this one, unchanged
        gap = start - read
        out |= ((v >> read) & ((1 << gap) - 1)) << out_pos
        out_pos += gap
        bits, n = _encode_state(mapping[state] if state else 0)
        out |= bits << out_pos
        out_pos += n
        read = start + length
    return format(out, 'X').zfill((out_pos + 3) // 4)
