# The SRD 5.1 conformance whitelist

`srd-5.1.json` is the list of names this repository is allowed to ship. Every name in
`config/*.yml`, in the Forge's own literals and in every `assets/<family>/manifest.json`
icon is checked against it by `tools/srd_gate.py`, which runs first in
`tools/verify.py`. If a name is not in the file, the build fails.

**It is names, never prose.** The gate cannot judge a description, and it says nothing
about what the running GM invents — that is disclosed in the README and defended by the
protocol's `STRICT SRD ONLY` line. What the gate guarantees is that the *repository*
ships nothing but SRD 5.1 names.

## The rule

- A **shipped** name must be an SRD 5.1 name. SRD 5.1 is CC-BY-4.0 and commercially
  usable with attribution (`SRD_LICENSE.txt` ships the attribution text).
- A name the SRD *renames* to avoid Product Identity (Bigby's Hand → Arcane Hand,
  Tasha's Hideous Laughter → Hideous Laughter, …) lives in `spell_aliases` and is
  accepted **only** as an alias — the SRD spelling is the one that ships.
- Anything else — original fiction, a GM-invented item that has an icon — goes in
  `original`. That list is a **decision, reviewed by a human**, not a default. Adding to
  it is how you say "this is ours, not someone else's".

## Where each list came from

`_meta.basis` marks every list:

- **`source`** — fetched from Open5e's `wotc-srd` document, which mirrors SRD 5.1:
  ```
  curl -s "https://api.open5e.com/v1/<endpoint>/?document__slug=wotc-srd&limit=600&fields=name"
  ```
  endpoints used: `spells` (319), `classes` (12), `races` (9), `conditions` (15),
  `magicitems` (237).
- **`audited-baseline`** — this repository's own values at the **2026-10-08 audit**,
  each reviewed by hand against SRD 5.1 and frozen so they cannot regress. `weapons`
  and `armor` are here because Open5e writes them "Crossbow, hand" where the repository
  writes "Hand Crossbow"; both spellings of the same SRD line are recorded, aliases
  included.
- **`decision`** — `original`, the allow-list described above.

The audit that produced the baselines is recorded in the agent memory
(`decisions/srd-5-1-conformance-the-gate-and-the-remediation-pending-approval`): it
found 59 non-SRD spells with their prose, 13 Product-Identity spell names, 10
Forgotten Realms instruments in the Forge, 60 mis-keyed icons and a README claim that
was not true. All of that is fixed; this file is what keeps it fixed.

## Running it

```
venv\Scripts\python.exe tools\srd_gate.py          # the gate alone
venv\Scripts\python.exe tools\srd_gate.py --json   # machine-readable
venv\Scripts\python.exe tools\verify.py            # the whole build gate
```

Exit codes: `0` clean, `1` a non-SRD name is shipped, `2` this file is missing or
malformed.

## When it fails

The message names the file and the name. Then pick one, deliberately:

1. **Fix the name** — usually right: use the SRD spelling.
2. **Add it to `spell_aliases`** — only if the SRD renames it and the SRD spelling
   shipped.
3. **Add it to `original`** — only if the name is ours (fiction, an invented item, a
   place). Say so in the same breath; that list is read by people.
4. If the list itself is wrong, fix the list — it is checked in, and this file is the
   record of why each list says what it says.

Never weaken the gate to make a red build green without doing one of the above.
