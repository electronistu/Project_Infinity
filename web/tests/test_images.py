"""Image service contract: prompt building, caching, path convention, status.

No network — the Gemini call is replaced with a stub. Run from the repo root:
    venv\\Scripts\\python.exe web\\tests\\test_images.py
"""

import json
import io
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image as PILImage  # noqa: E402

from web.images import (  # noqa: E402
    ImageService, SceneService, known_scene_places, known_scene_locations,
    _gear_line, _veterancy_line,
)

RESULTS = []
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32  # opaque bytes; nothing decodes it


def rec(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def jpeg(w, h, color=(40, 30, 20)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


PLAYER = {
    "name": "Electronistu",
    "race": "High Elf",
    "character_class": "Wizard",
    "background": "Sage",
    "alignment": "Chaotic Good",
    "gender": "Male",
    "level": 4,
    "inventory": [
        {"name": "Magic Dagger (+1)", "description": "enchanted"},
        {"name": "Spellbook"},
        {"name": "Silver-chased Rapier (cane-sword)", "description": "hidden blade"},
    ],
}


def make_service(tmp: Path, key=None):
    if key is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = key
    svc = ImageService(tmp)
    calls = {"n": 0}

    def fake(prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None):  # noqa: ARG001 - stub
        calls["n"] += 1
        return _PNG

    svc._generate_bytes = fake  # type: ignore[assignment]
    svc._calls = calls
    return svc


class _FakeInline:
    def __init__(self, data):
        self.data = data


class _FakePart:
    def __init__(self, data):
        self.inline_data = _FakeInline(data)


class _FakeContent:
    def __init__(self, data):
        self.parts = [_FakePart(data)]


class _FakeCandidate:
    def __init__(self, data):
        self.content = _FakeContent(data)


class _FakeResponse:
    def __init__(self, data):
        self.candidates = [_FakeCandidate(data)]


class _FakeClient:
    """Stands in for genai.Client; records the config actually sent."""

    def __init__(self):
        self.captured = {}

        def generate_content(model, contents, config):
            self.captured.update(model=model, contents=contents, config=config)
            return _FakeResponse(_PNG)

        self.models = type("M", (), {"generate_content": staticmethod(generate_content)})()


class _FakeClientRejectRefs:
    """Raises when the request includes image parts (simulates a text-only model)."""

    def __init__(self):
        self.calls = []

        def generate_content(model, contents, config):
            self.calls.append(len(contents))
            if len(contents) > 1:
                raise RuntimeError("image input not supported")
            return _FakeResponse(_PNG)

        self.models = type("M", (), {"generate_content": staticmethod(generate_content)})()


def main() -> bool:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        svc = make_service(tmp, key=None)
        rec("no key -> not available", not svc.available())
        rec("status shape", set(svc.status()) >= {"available", "model", "aspect_ratio"})

        svc = make_service(tmp, key="test-key")
        rec("key present -> available", svc.available())

        prompt = svc.portrait_prompt(PLAYER)
        rec("prompt includes race", "High Elf" in prompt)
        rec("prompt includes class", "Wizard" in prompt)
        rec("prompt excludes the character name", "Electronistu" not in prompt)
        geared = dict(PLAYER, equipped={"armor": None, "hands": ["Magic Dagger (+1)"], "worn": []})
        rec("prompt strips weapon parentheticals",
            "Magic Dagger" in svc.portrait_prompt(geared)
            and "(+1)" not in svc.portrait_prompt(geared))
        rec("prompt forbids rendered text", "no text" in prompt.lower())
        rec("prompt reflects the character level", "level-4 adventurer" in prompt.lower(), prompt)
        low = dict(PLAYER, level=1)
        high = dict(PLAYER, level=10)
        rec("portrait prompt reflects a low level",
            "level-1 novice" in svc.portrait_prompt(low).lower())
        rec("portrait prompt reflects a high level",
            "level-10 seasoned" in svc.portrait_prompt(high).lower())
        rec("level changes the portrait cache key",
            svc.source_hash("portrait", svc._player_payload(low))
            != svc.source_hash("portrait", svc._player_payload(high)))
        rec("reference line only when a reference is used",
            "identity reference" in svc.portrait_prompt(PLAYER, has_ref=True)
            and "identity reference" not in svc.portrait_prompt(PLAYER, has_ref=False))

        r1 = svc.ensure_portrait("hero", PLAYER)
        rec("first ensure generates", r1["generated"] is True and r1["cached"] is False)
        rec("portrait file written", svc.portrait_path("hero").exists())
        rec("manifest written", svc.manifest_path("hero").exists())
        manifest = json.loads(svc.manifest_path("hero").read_text(encoding="utf-8"))
        rec("manifest records a source hash", "source_hash" in manifest.get("portrait", {}))
        rec("portrait path convention", svc.portrait_path("hero") == tmp / "images" / "hero" / "portrait.png")

        r2 = svc.ensure_portrait("hero", PLAYER)
        rec("second ensure is cached", r2["generated"] is False and r2["cached"] is True)
        rec("cache hit made no API call", svc._calls["n"] == 1)

        player2 = dict(PLAYER, character_class="Fighter")
        r3 = svc.ensure_portrait("hero", player2)
        rec("changed class regenerates", r3["generated"] is True)
        rec("regeneration made an API call", svc._calls["n"] == 2)

        r4 = svc.ensure_portrait("hero", player2, force=True)
        rec("force regenerates", r4["generated"] is True)
        rec("force made an API call", svc._calls["n"] == 3)

        svc.ensure_portrait("other", PLAYER)
        rec("separate stem gets its own dir", (tmp / "images" / "other" / "portrait.png").exists())

        # The image-model override participates in the cache key, so switching
        # models regenerates instead of serving a stale file.
        svc_m = make_service(tmp)
        r_m1 = svc_m.ensure_portrait("modeltest", PLAYER, model="gemini-3-pro-image")
        n1 = svc_m._calls["n"]
        rec("model override is recorded on the portrait", r_m1["model"] == "gemini-3-pro-image")
        r_m2 = svc_m.ensure_portrait("modeltest", PLAYER)  # default model
        rec("switching image model regenerates", r_m2["generated"] is True and svc_m._calls["n"] == n1 + 1)
        r_m3 = svc_m.ensure_portrait("modeltest", PLAYER)
        rec("same model is cached again", r_m3["cached"] is True and svc_m._calls["n"] == n1 + 1)

        # The Lite model returns JPEG; store it under the right extension/mime.
        svc3 = ImageService(tmp)
        svc3._generate_bytes = lambda prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None: b"\xff\xd8\xff\xe0" + b"j" * 32  # type: ignore[assignment]
        svc3.ensure_portrait("jpgsniff", PLAYER)
        rec("jpeg bytes stored as portrait.jpg", svc3.portrait_path("jpgsniff").name == "portrait.jpg")
        rec("portrait_file sniffs image/jpeg", svc3.portrait_file("jpgsniff")[1] == "image/jpeg")

        # Real _generate_bytes path with a stubbed SDK client: locks the
        # Developer-API parameter set (no output_mime_type; AFC disabled).
        svc2 = ImageService(tmp)
        fake = _FakeClient()
        svc2._client_or_raise = lambda: fake  # type: ignore[assignment]
        r5 = svc2.ensure_portrait("cfgtest", PLAYER)
        cfg = fake.captured.get("config")
        ic = getattr(cfg, "image_config", None) if cfg else None
        rec("SDK path generates", r5["generated"] is True)
        rec("image_config present", ic is not None)
        rec("output_mime_type not sent (Developer API)", ic is not None and ic.output_mime_type is None)
        rec("aspect_ratio sent", ic is not None and ic.aspect_ratio == "3:4")
        afc = getattr(cfg, "automatic_function_calling", None) if cfg else None
        rec("automatic function calling disabled", afc is not None and afc.disable is True)
        rec("model id sent", fake.captured.get("model") == svc2.model)
        rec("SDK path wrote the image bytes", svc2.portrait_path("cfgtest").read_bytes() == _PNG)

        # Image-reference continuity: the previous portrait is sent as input.
        svc_ref = ImageService(tmp)
        fake_ref = _FakeClient()
        svc_ref._client_or_raise = lambda: fake_ref  # type: ignore[assignment]
        svc_ref._generate_bytes("x", refs=[(b"\xff\xd8\xff\xe0" + b"r" * 8, "image/jpeg")])
        parts = fake_ref.captured.get("contents") or []
        rec("refs produce a two-part request", len(parts) == 2)
        rec("the reference is an inline image part",
            len(parts) == 2 and getattr(parts[1], "inline_data", None) is not None)

        svc_rej = ImageService(tmp)
        fake_rej = _FakeClientRejectRefs()
        svc_rej._client_or_raise = lambda: fake_rej  # type: ignore[assignment]
        out = svc_rej._generate_bytes("x", refs=[(b"\xff\xd8\xff\xe0" + b"r" * 8, "image/jpeg")])
        rec("refs rejection falls back to text-only", out == _PNG and fake_rej.calls == [2, 1])

        svc_pref = ImageService(tmp)
        fake_pref = _FakeClient()
        svc_pref._client_or_raise = lambda: fake_pref  # type: ignore[assignment]
        svc_pref.ensure_portrait("refhero", PLAYER)
        first_parts = len(fake_pref.captured.get("contents") or [])
        svc_pref.ensure_portrait("refhero", dict(PLAYER, level=10), force=True)
        second_parts = len(fake_pref.captured.get("contents") or [])
        rec("first portrait has no reference", first_parts == 1)
        rec("regeneration feeds the previous portrait", second_parts == 2)

        # ── storyline scenes: hidden seed + chained action (16:9, per-save disk) ──
        sc = SceneService()
        rec("scene service uses 16:9", sc.aspect_ratio == "16:9")
        ap = sc.action_prompt(PLAYER, "a city of brass and ash", "standing before a forge at dusk",
                              "tense", kingdom="Kingdom of Eldoria", area="Eldoria City",
                              location="Hask's Smithy", sublocation="the forge", ref_kind="seed")
        rec("action prompt includes the moment", "forge at dusk" in ap)
        rec("action prompt includes race/class", "High Elf" in ap and "Wizard" in ap)
        rec("action prompt includes the world brief", "brass" in ap)
        rec("action prompt includes the mood", "tense" in ap)
        rec("action prompt names the full address",
            all(x in ap for x in ("Hask's Smithy", "the forge", "Eldoria City", "Kingdom of Eldoria")), ap[:200])
        rec("action prompt carries the seed reference line",
            "empty establishing view" in ap and "central figure" in ap)
        clothed = {"race": "High Elf", "character_class": "Wizard",
                   "equipped": {"armor": None, "hands": [None, None],
                                "worn": ["Dark Common Clothes"]},
                   "inventory": ["Spellbook", "Crowbar", "Dark Common Clothes"]}
        cp = sc.action_prompt(clothed, "world", "desc", "mood", kingdom="K", area="A", location="Loc", sublocation="sub", ref_kind="portrait")
        rec("scene gear line comes from the equipped set",
            "Wearing Dark Common Clothes." in cp and "Crowbar" not in cp and "Spellbook" not in cp, cp)
        rec("carried (inventory) items are never described",
            _gear_line(PLAYER) == "", _gear_line(PLAYER))
        full = {"equipped": {"armor": "Chain Mail", "hands": ["Longsword", "Shield"],
                             "worn": ["Dark Common Clothes"]},
                "inventory": ["Spellbook", "Crowbar"]}
        rec("every equipped item is listed (armor + worn + hands)",
            _gear_line(full) == ("Wearing Chain Mail and Dark Common Clothes. "
                                 "Wielding Longsword and Shield."), _gear_line(full))
        rec("veterancy lines never name gear",
            all("gear" not in _veterancy_line(lvl).lower() for lvl in (1, 3, 8, 14, 20)))
        rec("portrait prompt carries the equipped gear and the no-invention guard",
            "Wearing Dark Common Clothes." in svc.portrait_prompt(clothed)
            and "nothing more" in svc.portrait_prompt(clothed))
        rec("action prompt forbids rendered text", "no text" in ap.lower())
        tod = sc.action_prompt(PLAYER, "world", "desc", "mood", location="Loc", sublocation="sub",
                               ref_kind="chain", time_of_day="deep night", weather="heavy rain")
        rec("action prompt states the explicit time and weather",
            "Time of day: deep night." in tod and "Weather: heavy rain." in tod, tod[:120])
        plain = sc.action_prompt(PLAYER, "w", "d", "m", location="Loc", sublocation="sub")
        rec("action prompt omits an empty time/weather line",
            "Time of day:" not in plain and "Weather:" not in plain)
        only_time = sc.action_prompt(PLAYER, "w", "d", "m", location="Loc", sublocation="sub", time_of_day="dusk")
        rec("action prompt keeps only the time when weather is empty",
            "Time of day: dusk." in only_time and "Weather:" not in only_time)
        cast = sc.action_prompt(PLAYER, "world", "desc", "mood", location="Loc", sublocation="sub",
                                characters={"three dockhands": "drinking and turning to look",
                                            "the barkeep — a broad, one-eared woman": "talking"})
        rec("action prompt lists each character with their action",
            "Characters present" in cast and "- three dockhands: drinking and turning to look" in cast
            and "one-eared woman" in cast, cast[:160])
        rec("action prompt carries the character render guard",
            "must not be drawn as a man" in cast)
        rec("action prompt omits the character line when the cast is empty",
            "Characters present" not in plain)
        chain = sc.action_prompt(PLAYER, "world", "desc", "mood", location="Loc", sublocation="sub", ref_kind="chain")
        rec("chain reference line continues the previous moment",
            "immediately preceding moment" in chain and "central figure" in chain)
        guard = "appearance is fixed by the attached portrait"
        rec("protagonist guard appears only with a place/portrait reference",
            guard in ap and guard in chain
            and guard not in sc.action_prompt(PLAYER, "w", "d", "m", location="Loc", sublocation="sub"))
        seedp = sc.seed_prompt("a city of brass and ash", "Hask's Smithy", "the forge", "a hot forge")
        rec("seed prompt is an empty establishing view",
            "bird's-eye" in seedp and "animals" in seedp.lower())
        rec("seed prompt names the place and description",
            "the forge" in seedp and "a hot forge" in seedp)
        rec("seed prompt carries the empty-place guard", "no person" in seedp.lower())

        # ensure_scene: hidden seed + chained action; cleanup only when leaving the location.
        sc3 = SceneService(tmp)
        captured = []

        def _scene_bytes(prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None):
            captured.append((prompt, refs, ref_media_resolution))
            return jpeg(800, 450)

        sc3._generate_bytes = _scene_bytes  # type: ignore[assignment]
        scene_dir = tmp / "images" / "scenetest" / "scenes"
        portrait_dir = tmp / "images" / "scenetest"
        portrait_dir.mkdir(parents=True, exist_ok=True)
        (portrait_dir / "portrait.jpg").write_bytes(jpeg(1200, 1600))
        r1 = sc3.ensure_scene("scenetest", PLAYER, "world", description="a forge at dusk",
                              mood="tense", kingdom="Kingdom of Eldoria", area="Eldoria City",
                              location="Hask's Smithy", sublocation="the forge",
                              establishing="a hot forge", main_npc="Gorson — a burly smith",
                              time_of_day="dusk", weather="light rain",
                              characters={"three dockhands": "drinking"})
        rec("first visit creates the seed and seeds the action from it",
            r1["seed_created"] and r1["used_seed"], str(r1))
        rec("the hidden seed is generated empty (no references)", not captured[0][1])
        rec("ensure_scene feeds time/weather into the action prompt",
            "Time of day: dusk." in captured[1][0] and "Weather: light rain." in captured[1][0])
        rec("ensure_scene feeds the character cast into the action prompt",
            "Characters present" in captured[1][0] and "- three dockhands: drinking" in captured[1][0])
        rec("the first action references seed + portrait",
            captured[1][1] is not None and len(captured[1][1]) == 2)
        rec("action references are downscaled to ref_max_side",
            all(max(PILImage.open(io.BytesIO(d)).size) <= 512 for d, _m in captured[1][1]))
        rec("reference media resolution set", captured[1][2] == "MEDIA_RESOLUTION_MEDIUM")
        rec("action written to disk", bool(list(scene_dir.glob("action-*"))))
        rec("hidden seed written to disk", bool(list(scene_dir.glob("seed-*"))))
        r2 = sc3.ensure_scene("scenetest", PLAYER, "world", description="embers dying",
                              location="Hask's Smithy", sublocation="the forge")
        rec("second action chains from the last action",
            r2["used_last_action"] and not r2["seed_created"], str(r2))
        r3 = sc3.ensure_scene("scenetest", PLAYER, "world", description="downstairs",
                              kingdom="Kingdom of Eldoria", area="Eldoria City",
                              location="Hask's Smithy", sublocation="common room",
                              establishing="a low-beamed hall")
        rec("a new sublocation creates a new seed", r3["seed_created"] and r3["used_seed"], str(r3))
        r4 = sc3.ensure_scene("scenetest", PLAYER, "world", description="the rafters catch",
                              kingdom="Kingdom of Eldoria", area="Eldoria City",
                              location="Hask's Smithy", sublocation="the forge", seed_change="it burned down")
        rec("seed_change regenerates the seed but keeps the action chain",
            r4["seed_regenerated"] and r4["used_last_action"], str(r4))
        r5 = sc3.ensure_scene("scenetest", PLAYER, "world", description="on the road",
                              kingdom="Borderlands", area="the Eldoria–Silverwood border",
                              location="Lantern Row", sublocation="")
        rec("leaving the location deletes its action images",
            len(list(scene_dir.glob("action-*"))) == 1, str(list(scene_dir.glob("action-*"))))
        rec("seeds survive leaving the location", len(list(scene_dir.glob("seed-*"))) == 3)
        slug = r5["action"]["url"].rsplit("/", 1)[-1]
        got = sc3.get_scene("scenetest", slug)
        rec("get_scene returns bytes + mime", bool(got) and got[1].startswith("image/"))
        places = known_scene_places(tmp, "scenetest")
        rec("known_scene_places lists seeded places with the full hierarchy",
            any(p["kingdom"] == "Kingdom of Eldoria" and p["area"] == "Eldoria City"
                and p["location"] == "Hask's Smithy" and p["sublocation"] == "the forge"
                and p["description"] == "a hot forge" and p["main_npc"] == "Gorson — a burly smith"
                for p in places), str(places))
        rec("seed_change preserved the main NPC",
            any(p["location"] == "Hask's Smithy" and p["sublocation"] == "the forge"
                and p["main_npc"] == "Gorson — a burly smith" for p in places))
        rec("known_scene_places empty for an unknown save", known_scene_places(tmp, "nope") == [])
        rec("known_scene_locations lists distinct locations",
            known_scene_locations(tmp, "scenetest") == ["Hask's Smithy", "Lantern Row"],
            str(known_scene_locations(tmp, "scenetest")))

        sc2 = SceneService()
        fake2 = _FakeClient()
        sc2._client_or_raise = lambda: fake2  # type: ignore[assignment]
        sc2.generate_action(PLAYER, "world", "a duel on the bridge", "mood", "Loc", "sub")
        ic2 = getattr(fake2.captured.get("config"), "image_config", None)
        rec("scene aspect_ratio 16:9 sent to the SDK", ic2 is not None and ic2.aspect_ratio == "16:9")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
