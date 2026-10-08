"""Era files: format, content, budgets, and that they stay additive.

Locks the contract in ``config/eras/``:
  - one file per era, a `meta:` header + a `body:` scaffold, all validated;
  - the always-on ERA INDEX <= 150 tok, one scaffold <= 450 tok, one file <= 5 KB;
  - E1 (Egypt) and E3 (Wallachia) are the playable eras;
  - no era's scaffold leaks another era's content;
  - the reputation seed comes from the starting era.

No LLM / no network. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_eras.py
"""

import importlib.util
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from web import eras  # noqa: E402

RESULTS = []


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _load_budget_tool():
    spec = importlib.util.spec_from_file_location("token_budget", REPO / "tools" / "token_budget.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tb = _load_budget_tool()


def _polity(era_id, name_fragment):
    for p in eras.load_era(era_id)["body"].get("polities") or []:
        if name_fragment.lower() in str(p.get("name") or "").lower():
            return p
    return {}


def main():
    files = eras.era_files()
    rec("era files found", len(files) == 5, f"{len(files)}: {[f.stem for f in files]}")

    # -- format ---------------------------------------------------------------
    all_problems = {}
    for f in files:
        problems = eras.validate_era(eras.load_era(f.stem), stem=f.stem)
        if problems:
            all_problems[f.stem] = problems
    rec("every era validates (meta + body + faction keys)", not all_problems, str(all_problems)[:200])

    metas = eras.list_era_meta()
    ids = [m.get("id") for m in metas]
    orders = [m.get("order") for m in metas]
    rec("era ids are unique", len(set(ids)) == len(ids), str(ids))
    rec("era orders are unique and ascending", orders == sorted(orders) and len(set(orders)) == len(orders),
        str(orders))
    rec("the ladder is future..victorian", ids == ["future", "egypt", "tang", "wallachia", "victorian"],
        str(ids))
    rec("all four final eras are playable",
        eras.playable_eras() == ["egypt", "tang", "wallachia", "victorian"],
        str(eras.playable_eras()))
    rec("list_era_meta returns no bodies",
        all(not ({"history", "polities", "visual_hooks"} & set(m)) for m in metas))

    # -- budgets (skipped when the counter is inexact; tools/verify.py refuses anyway) ----
    label, count, exact = tb._counter()
    if exact:
        index_tokens = count(eras.render_era_index())
        rec("ERA INDEX <= 150 tok", index_tokens <= 150, f"{index_tokens} tok")
        worst = max((count(eras.render_era_text(f.stem)), f.stem) for f in files)
        rec("every scaffold <= 450 tok", worst[0] <= 450, f"worst {worst[1]} {worst[0]} tok")
        rec("every era file <= 5120 B", max(f.stat().st_size for f in files) <= 5120,
            f"{max(f.stat().st_size for f in files)} B")
    else:
        rec("token ceilings skipped (no tiktoken)", True, label)

    # -- the index ------------------------------------------------------------
    index = eras.render_era_index("egypt")
    rec("index marks the current era", "(current: egypt)" in index)
    rec("index lists every era", all(i in index for i in ids))
    rec("the index names no arrival point (it is rolled, not a static fact)",
        "Giza" not in index and "quarry" not in index and "Chang'an" not in index)
    rec("every playable era offers >= 2 arrival points",
        all(len(eras.era_arrivals(e)) >= 2 for e in eras.playable_eras()),
        str({e: len(eras.era_arrivals(e)) for e in eras.playable_eras()}))
    picks = {eras.pick_arrival("egypt", random.Random(n)) for n in range(20)}
    rec("pick_arrival draws from the era's own list",
        bool(picks) and picks <= set(eras.era_arrivals("egypt")), str(sorted(picks)))
    rec("pick_arrival is deterministic under a seeded rng",
        eras.pick_arrival("egypt", random.Random(1)) == eras.pick_arrival("egypt", random.Random(1)))
    rec("the frame has nowhere to arrive", eras.era_arrivals("future") == [])

    # -- the first era is rolled, like every later one --------------------------
    starts = {eras.pick_start_era(random.Random(n)) for n in range(30)}
    rec("pick_start_era draws only playable eras",
        bool(starts) and starts <= set(eras.playable_eras()), str(sorted(starts)))
    rec("pick_start_era never draws the frame", "future" not in starts)
    rec("pick_start_era is not stuck on one era", len(starts) > 1, str(len(starts)))
    rec("pick_start_era is deterministic under a seeded rng",
        eras.pick_start_era(random.Random(1)) == eras.pick_start_era(random.Random(1)))
    rec("START_ERA survives only as a no-era fallback",
        eras.START_ERA in eras.playable_eras())

    # -- E1 content ------------------------------------------------
    egypt = eras.load_era("egypt")["body"]
    old_kingdom = _polity("egypt", "Old Kingdom")
    rec("E1: the Old Kingdom with Memphis", "Memphis" in str(old_kingdom.get("capital", "")))
    rec("E1: Nubia/Libya/Sinai/Byblos relations",
        {"Nubia", "Libya", "Sinai", "Byblos"} <= set((old_kingdom.get("relations") or {})),
        str(sorted((old_kingdom.get("relations") or {}))))
    factions = [f.get("name") for f in (old_kingdom.get("factions") or [])]
    rec("E1: the Priesthood of Ra is a faction", "Priesthood of Ra" in factions, str(factions))
    rec("E1: Khufu is named", any("Khufu" in str(p.get("ruler", "")) for p in egypt.get("polities") or []))

    # -- E3 content ----------------------------------------------
    wall = eras.load_era("wallachia")["body"]
    names = [p.get("name") for p in wall.get("polities") or []]
    rec("E3: Wallachia / Moldavia / Transylvania",
        names == ["Wallachia", "Moldavia", "Transylvania"], str(names))
    rec("E3: the capital is Targoviste, not Bucharest",
        str(_polity("wallachia", "Wallachia").get("capital", "")).startswith("T\u00e2rgovi")
        and "Bucharest" not in str(_polity("wallachia", "Wallachia").get("capital", "")),
        _polity("wallachia", "Wallachia").get("capital", ""))
    rec("E3: Vlad III is the ruler", "Vlad III" in str(_polity("wallachia", "Wallachia").get("ruler", "")))
    rec("E3: the 1459 chrysobull is referenced", any("chrysobull" in h for h in wall.get("history") or []))
    wall_factions = {f.get("name") for f in (_polity("wallachia", "Wallachia").get("factions") or [])}
    rec("E3: boyars / Saxon guilds / Orthodox / Ottoman frontier",
        {"the boyars", "the Saxon merchant guilds", "the Orthodox church", "the Ottoman frontier"}
        <= wall_factions, str(sorted(wall_factions)))
    rec("every polity offers at least one faction",
        all(p.get("factions") for e in files for p in eras.load_era(e.stem)["body"].get("polities") or []))

    # -- no leaks between eras -------------------------------------------------
    def _leaks(era_id, needles):
        text = eras.render_era_text(era_id)
        return [n for n in needles if n in text]

    rec("E1 scaffold leaks no other era",
        not _leaks("egypt", ["Wallachia", "Targoviste", "Chang'an", "London"]))
    rec("E3 scaffold leaks no other era",
        not _leaks("wallachia", ["Giza", "Memphis", "Khufu", "Chang'an"]))
    rec("E2 scaffold leaks no other era",
        not _leaks("tang", ["Memphis", "Targoviste", "the Thames", "Ireland"]))
    rec("E4 scaffold leaks no other era",
        not _leaks("victorian", ["Memphis", "Targoviste", "Chang'an", "the boyars"]))

    # -- the reputation hooks ----------------------------------------------------------
    kingdoms = eras.era_kingdoms("wallachia")
    rec("era_kingdoms maps to Kingdom objects with guilds",
        len(kingdoms) == 3 and all(k.guilds for k in kingdoms),
        str([(k.name, len(k.guilds)) for k in kingdoms]))
    keys = eras.era_faction_keys("wallachia")
    rec("era_faction_keys gives stable keys", "boyars" in keys.get("Wallachia", []), str(keys.get("Wallachia")))
    rec("E3 carries one invented faction, the Strigoi",
        "strigoi" in keys.get("Wallachia", []), str(keys.get("Wallachia")))

    # -- E2 and E4 at full depth ----------------------------
    tang = eras.load_era("tang")["body"]
    tang_names = [p.get("name") for p in tang.get("polities") or []]
    rec("E2: the Tang empire at Chang'an, under Xuanzong",
        "the Tang empire" in tang_names
        and "Chang'an" in str(_polity("tang", "Tang").get("capital", ""))
        and "Xuanzong" in str(_polity("tang", "Tang").get("ruler", "")), str(tang_names))
    rec("E2: the jiedushi are a power of their own",
        any("frontier" in str(n).lower() for n in tang_names)
        and "jiedushi" in eras.era_faction_keys("tang").get("the frontier commands", []),
        str(eras.era_faction_keys("tang")))
    rec("E2: the An Lushan rebellion is on the horizon",
        any("An Lushan" in h for h in tang.get("history") or []))
    vic = eras.load_era("victorian")["body"]
    vic_names = [p.get("name") for p in vic.get("polities") or []]
    rec("E4: England, Scotland and Ireland under one Crown",
        vic_names == ["England", "Scotland", "Ireland"]
        and all("Victoria" in str(p.get("ruler", "")) for p in vic.get("polities") or []),
        str(vic_names))
    rec("E4: London is the capital",
        "London" in str(_polity("victorian", "England").get("capital", "")))
    rec("E4: the Irish question is context, not content",
        any("Home Rule" in v for v in (_polity("victorian", "Ireland").get("relations") or {}).values())
        and "land_league" in eras.era_faction_keys("victorian").get("Ireland", []),
        str(eras.era_faction_keys("victorian").get("Ireland")))

    # -- the reputation seed ----------------------------------------------
    seed = eras.era_reputation_seed("egypt")
    scoped = seed.get("egypt", {})
    old_kingdom = scoped.get("the old kingdom", {})
    rec("reputation seed is scoped to the era", set(seed) == {"egypt"}, str(sorted(seed)))
    rec("reputation seed: the starting era's polities",
        "the old kingdom" in scoped, str(sorted(scoped)))
    rec("reputation seed: ruler + faction buckets, and others",
        old_kingdom.get("ruler") == [] and "priesthood_of_ra" in old_kingdom and "others" in scoped,
        str(sorted(old_kingdom)))
    rec("reputation seed: START_ERA is a real, playable era",
        eras.START_ERA in eras.playable_eras(), eras.START_ERA)

    # -- the lookup resolver ----------------------------------------------
    rec("lookup('here') returns the current era's scaffold",
        "history:" in eras.lookup("here", "egypt") and "the Old Kingdom" in eras.lookup("here", "egypt"))
    rec("lookup(<id>) returns another era, and only that era",
        "the boyars" in eras.lookup("wallachia", "egypt")
        and "the Old Kingdom" not in eras.lookup("wallachia", "egypt"))
    polity = eras.lookup("old kingdom", "egypt")
    rec("lookup(<polity>) returns just that polity",
        "the Old Kingdom" in polity and "history:" not in polity)
    rec("lookup(a miss) lists the known eras instead of failing",
        "Known eras" in eras.lookup("atlantis", "egypt"))
    rec("lookup defaults an empty current era to START_ERA",
        "the Old Kingdom" in eras.lookup("here", ""))

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
