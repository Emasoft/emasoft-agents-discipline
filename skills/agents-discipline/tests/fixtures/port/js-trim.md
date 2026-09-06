# Gates: trim is not strip

Every padded value below is padded with U+FEFF, which JS `.trim()` strips and `str.strip()` does
NOT, so a port built on `str.strip()` produces a DIFFERENT parse than the oracle.

Five of the six `.trim()` sites in parse_gates are reached from here, and each was CONFIRMED by
reverting js_trim to str.strip() and reading which fields moved:

  | padded here            | site                | what moves under the mutation      |
  |------------------------|---------------------|------------------------------------|
  | before the gate id     | raw_title           | id becomes <BOM>t3 + an id error    |
  | after the id colon     | title after the id  | title keeps the BOM                 |
  | a CHECK value          | attribute value     | check keeps the BOM (and the digest)|
  | an ABANDON reason      | abandon reason      | reason keeps the BOM                |
  | an OWNS item           | OWNS item           | owns keeps the BOM                  |

The OWNS row is JOINT, not isolated. Under CORRECT code the item trim runs first, so
normalize_owns_glob never sees padding from a ledger — but under the mutation BOTH are
`str.strip()`, so the padded item reaches normalize still padded and it fails to strip it too.
The observed `owns` move therefore does not attribute to either site alone. normalize_owns_glob
has no independent coverage here; its other caller is gate-check's --claim path.

The first version of this fixture spelled OWNS indented under a gate and ABANDON as
`ABANDON t2:`. Both are silently unparsed -- `owns` and `abandoned` came back EMPTY and the
fixture still looked fine, because a fixture that parses to nothing diverges on nothing. Only
one of its three padded values was doing anything.

The characters are invisible on purpose; `cat -A` shows M-oM-;M-? for the BOM.

Only the U+FEFF direction is exercised HERE. The other one is covered in the jsapi corpus
instead, where the set is built with `chr()`: authoring U+0085 into a markdown file could not be
done reliably -- a trailing one was silently stripped by the writer, and an escaped one
materialised as U+2026, which is not whitespace in either runtime and would have made the row
assert nothing. The corpus check compares js_trim to Node over ALL 30 disagreeing code points,
which is strictly more than a fixture could show anyway.

OWNS: ﻿src/a/**﻿, src/b/**

- [ ] t1: a gate whose CHECK value is padded
  CHECK: echo ok﻿
  EXPECT: ok
  EVIDENCE: pending

- [ ] t2: a plain control gate, so a divergence above is attributable
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

- [ ] ﻿t3: padded BEFORE the id, so the id itself stops matching
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

- [ ] t4: ﻿padded after the id colon, so only the title moves
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

ABANDON: t2 ﻿reason padded with a BOM﻿
