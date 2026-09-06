# Gates: trim is not strip

Every padded value below is padded with a character the two runtimes disagree about, so a port
using `str.strip()` produces a DIFFERENT parse than the oracle. Both directions are present:

  - U+FEFF  — JS `.trim()` strips it, `str.strip()` does NOT.
  - U+0085  — `str.strip()` strips it, JS `.trim()` does NOT. (Same for U+001C..U+001F.)

The characters are invisible on purpose; `cat -A` shows M-oM-;M-? for the BOM.

Only the U+FEFF direction is exercised HERE. The other one is covered in the jsapi corpus
instead, where the set is built with `chr()`: authoring U+0085 into a markdown file could not be
done reliably -- a trailing one was silently stripped by the writer, and an escaped one
materialised as U+2026, which is not whitespace in either runtime and would have made the row
assert nothing. The corpus check compares js_trim to Node over ALL 30 disagreeing code points,
which is strictly more than a fixture could show anyway.

- [ ] t1: title padded with a trailing NEL
  CHECK: echo ok﻿
  EXPECT: ok
  EVIDENCE: pending
  OWNS: ﻿src/a/**﻿, src/b/**

- [ ] t2: a plain control gate, so a divergence above is attributable
  CHECK: echo ok
  EXPECT: ok
  EVIDENCE: pending

ABANDON t2: ﻿reason padded with a BOM﻿
