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
    ImageService, SceneService, known_scene_places, known_scene_locations, known_npc_names,
    live_scene_manifest, discard_scene_manifest,
    _gear_line, _veterancy_line, appearance_override,
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

    def fake(prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None, thinking_level=None, seed=None):  # noqa: ARG001 - stub
        calls["n"] += 1
        return _PNG

    svc.generate = fake  # type: ignore[assignment]
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
        custom = "In the style of Day of the Tentacle: cartoony, bold outlines, saturated palette."
        rec("a custom style replaces the default portrait art direction",
            custom in svc.portrait_prompt(PLAYER, style=custom)
            and "Painterly dark-fantasy" not in svc.portrait_prompt(PLAYER, style=custom))
        rec("the no-text guard survives a custom portrait style",
            "no text" in svc.portrait_prompt(PLAYER, style=custom).lower())
        rec("an empty style keeps the default portrait art direction",
            "Painterly dark-fantasy" in svc.portrait_prompt(PLAYER, style=""))
        rec("the style changes the portrait cache key",
            svc.source_hash("portrait", svc._player_payload(PLAYER))
            != svc.source_hash("portrait", svc._player_payload(PLAYER), style=custom))
        aged = dict(PLAYER, age=300)
        aged_prompt = svc.portrait_prompt(aged)
        rec("the portrait carries the age, race-relative",
            "300 years old" in aged_prompt and "in their prime for a" in aged_prompt,
            aged_prompt[-260:])
        rec("no age -> no age line", "years old" not in svc.portrait_prompt(PLAYER))
        rec("an explicit age drops the veteran's age cues",
            "young" not in _veterancy_line(1, has_age=True).lower()
            and "young" in _veterancy_line(1).lower()
            and "ageless" not in _veterancy_line(17, has_age=True).lower()
            and "ageless" in _veterancy_line(17).lower())
        rec("age changes the portrait cache key",
            svc.source_hash("portrait", svc._player_payload(PLAYER))
            != svc.source_hash("portrait", svc._player_payload(dict(PLAYER, age=40))))

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
        svc3.generate = lambda prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None, thinking_level=None, seed=None: b"\xff\xd8\xff\xe0" + b"j" * 32  # type: ignore[assignment]
        svc3.ensure_portrait("jpgsniff", PLAYER)
        rec("jpeg bytes stored as portrait.jpg", svc3.portrait_path("jpgsniff").name == "portrait.jpg")
        rec("portrait_file sniffs image/jpeg", svc3.portrait_file("jpgsniff")[1] == "image/jpeg")

        # Real generate() path with a stubbed SDK client: locks the
        # Developer-API parameter set (no output_mime_type; AFC disabled).
        svc2 = ImageService(tmp)
        fake = _FakeClient()
        svc2.backend._client_or_raise = lambda: fake  # type: ignore[assignment]
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
        rec("default story model is Nano Banana 2.1", svc2.model == "gemini-nano-banana-2.1", svc2.model)
        rec("image_size sent", ic is not None and ic.image_size == "1K")
        tc = getattr(cfg, "thinking_config", None) if cfg else None
        tl = getattr(tc.thinking_level, "value", tc.thinking_level) if tc else None
        rec("thinking_level pinned on the SDK path", str(tl).lower() == "medium", repr(tl))
        # The level is model-specific: only NB2.1 accepts "medium"; Lite/Pro/unknown omit it.
        for mid, expect in (("gemini-nano-banana-2.1", "medium"),
                            ("gemini-3.1-flash-lite-image", None),
                            ("gemini-3-pro-image", None),
                            ("gemini-3.1-flash-image", None),
                            ("bogus-image-model", None)):
            svc_x = ImageService(tmp)
            fake_x = _FakeClient()
            svc_x.backend._client_or_raise = lambda f=fake_x: f  # type: ignore[assignment]
            svc_x.generate("x", model=mid)
            cfg_x = fake_x.captured.get("config")
            tc_x = getattr(cfg_x, "thinking_config", None) if cfg_x else None
            lvl_x = getattr(tc_x.thinking_level, "value", tc_x.thinking_level) if tc_x else None
            rec(f"thinking level for {mid}",
                (str(lvl_x).lower() if lvl_x else None) == expect, repr(lvl_x))
        rec("SDK path wrote the image bytes", svc2.portrait_path("cfgtest").read_bytes() == _PNG)

        # Image-reference continuity: the previous portrait is sent as input.
        svc_ref = ImageService(tmp)
        fake_ref = _FakeClient()
        svc_ref.backend._client_or_raise = lambda: fake_ref  # type: ignore[assignment]
        svc_ref.generate("x", refs=[(b"\xff\xd8\xff\xe0" + b"r" * 8, "image/jpeg")])
        parts = fake_ref.captured.get("contents") or []
        rec("refs produce a two-part request", len(parts) == 2)
        rec("the reference is an inline image part",
            len(parts) == 2 and getattr(parts[1], "inline_data", None) is not None)

        svc_rej = ImageService(tmp)
        fake_rej = _FakeClientRejectRefs()
        svc_rej.backend._client_or_raise = lambda: fake_rej  # type: ignore[assignment]
        out = svc_rej.generate("x", refs=[(b"\xff\xd8\xff\xe0" + b"r" * 8, "image/jpeg")])
        rec("refs rejection falls back to text-only", out == _PNG and fake_rej.calls == [2, 1])

        svc_pref = ImageService(tmp)
        fake_pref = _FakeClient()
        svc_pref.backend._client_or_raise = lambda: fake_pref  # type: ignore[assignment]
        svc_pref.ensure_portrait("refhero", PLAYER)
        first_parts = len(fake_pref.captured.get("contents") or [])
        svc_pref.ensure_portrait("refhero", dict(PLAYER, level=10), force=True)
        second_parts = len(fake_pref.captured.get("contents") or [])
        rec("first portrait has no reference", first_parts == 1)
        rec("regeneration feeds the previous portrait", second_parts == 2)

        # ── storyline scenes: hidden seed + an action drawn fresh from it (16:9, per-save disk) ──
        sc = SceneService()
        rec("scene service uses 16:9", sc.aspect_ratio == "16:9")
        ap = sc.action_prompt(PLAYER, "a city of brass and ash", "standing before a forge at dusk",
                              "tense", kingdom="Kingdom of Eldoria", area="Eldoria City",
                              place=["Hask's Smithy", "the forge"], ref_kind="seed")
        rec("action prompt includes the moment", "forge at dusk" in ap)
        rec("action prompt includes race/class", "High Elf" in ap and "Wizard" in ap)
        rec("action prompt includes the world brief", "brass" in ap)
        rec("action prompt includes the mood", "tense" in ap)
        rec("action prompt names the full address",
            all(x in ap for x in ("Hask's Smithy", "the forge", "Eldoria City", "Kingdom of Eldoria")), ap[:200])
        custom = "In the style of Day of the Tentacle: cartoony, bold outlines, saturated palette."
        rec("a custom style overrides the scene art direction",
            custom in sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"], style=custom)
            and "Painterly dark-fantasy" not in sc.action_prompt(
                PLAYER, "w", "d", "m", place=["Loc", "sub"], style=custom))
        rec("the no-text guard survives a custom scene style",
            "no text" in sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"],
                                          style=custom).lower())
        rec("an empty style keeps the default scene art direction",
            "Painterly dark-fantasy" in sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"]))
        rec("a custom style reaches the seed prompt",
            custom in sc.seed_prompt("world", place=["Loc", "sub"], style=custom))
        rec("action prompt carries the seed reference line",
            "this IS the picture" in ap and "in the described action" in ap)
        clothed = {"race": "High Elf", "character_class": "Wizard",
                   "equipped": {"armor": None, "hands": [None, None],
                                "worn": ["Dark Common Clothes"]},
                   "inventory": ["Spellbook", "Crowbar", "Dark Common Clothes"]}
        cp = sc.action_prompt(clothed, "world", "desc", "mood", kingdom="K", area="A", place=["Loc", "sub"], ref_kind="portrait")
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
        tod = sc.action_prompt(PLAYER, "world", "desc", "mood", place=["Loc", "sub"],
                               ref_kind="seed", time_of_day="deep night", weather="heavy rain")
        rec("action prompt states the explicit time and weather",
            "Time of day: deep night." in tod and "Weather: heavy rain." in tod, tod[:120])
        plain = sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"])
        rec("action prompt omits an empty time/weather line",
            "Time of day:" not in plain and "Weather:" not in plain)
        only_time = sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"], time_of_day="dusk")
        rec("action prompt keeps only the time when weather is empty",
            "Time of day: dusk." in only_time and "Weather:" not in only_time)
        rec("declared weather -> the weather guard keeps it outdoors",
            "Weather is an outdoor phenomenon" in tod
            and "keep the inside dry" in tod
            and "Fog, mist and haze may soften the interior air" in tod)
        rec("weather guard is absent when no weather is declared",
            "Weather is an outdoor phenomenon" not in plain
            and "Weather is an outdoor phenomenon" not in only_time)
        wet_portrait = sc.action_prompt(PLAYER, "world", "desc", "mood", place=["Loc", "sub"], ref_kind="portrait", weather="rain")
        rec("weather guard applies to a portrait-only action too",
            "Weather is an outdoor phenomenon" in wet_portrait)
        cast = sc.action_prompt(PLAYER, "world", "desc", "mood", place=["Loc", "sub"],
                                characters={"three dockhands": "drinking and turning to look",
                                            "the barkeep — a broad, one-eared woman": "talking"})
        rec("action prompt lists each character with their action",
            "Characters present" in cast and "- three dockhands: drinking and turning to look" in cast
            and "one-eared woman" in cast, cast[:160])
        rec("action prompt carries the character render guard",
            "must not be drawn as a man" in cast)
        rec("action prompt omits the character line when the cast is empty",
            "Characters present" not in plain)
        seedref = sc.action_prompt(PLAYER, "world", "desc", "mood", place=["Loc", "sub"],
                                   ref_kind="seed")
        rec("the seed view IS the picture (reproduced, with the cast placed into it)",
            "this IS the picture" in seedref and "reproduce it unchanged" in seedref
            and "immediately preceding moment" not in seedref)
        rec("the old 'room under a different sky' wording is gone",
            "different sky" not in tod and "different sky" not in seedref)
        rec("action prompt forbids extra figures",
            "no other person" in seedref and "no other person" in ap)
        rec("action prompt forbids camera gaze",
            "acknowledges the camera or the viewer" in seedref
            and "acknowledges the camera or the viewer" in ap)
        rec("every action prompt demands true night when it is night",
            "draw true night" in plain and "no blue hour" in plain
            and "draw true night" in seedref)
        rec("the no-gaze guard makes figures face the action (back to the camera is fine)",
            "faces the ACTION at hand" in plain and "back to the camera" in plain)
        rec("the protagonist guard turns them to the action, not the lens",
            "the camera may see their back" in seedref
            and "the camera may see their back" not in plain)
        rec("seed layout + camera are locked; only day/night, weather and damage change",
            "The attached establishing view IS the scene" in seedref
            and "do not change the viewpoint" in seedref
            and "weather (atmosphere only" in seedref
            and "stays outdoors" in seedref
            and "transient damage or mess" in seedref
            and "camera may move closer" not in seedref)
        rec("protagonist guard turns the portrait's frontal pose into the action",
            "does not look at or address the camera" in seedref)
        portraitref = sc.action_prompt(PLAYER, "world", "desc", "mood", place=["Loc", "sub"], ref_kind="portrait")
        rec("layout lock applies only when the seed is attached",
            "The attached establishing view IS the scene" in ap
            and "The attached establishing view IS the scene" not in portraitref
            and "The attached establishing view IS the scene" not in plain)
        rec("the action never invents architecture the seed does not show",
            "ground truth for the space" in seedref
            and "leave them out of the picture" in seedref
            and "the protagonist included" in seedref
            and "always in frame" not in seedref
            and "ground truth for the space" not in portraitref
            and "ground truth for the space" not in plain)
        guard = "appearance is fixed by the attached portrait"
        rec("protagonist guard appears only with a place/portrait reference",
            guard in ap and guard in seedref
            and guard not in sc.action_prompt(PLAYER, "w", "d", "m", place=["Loc", "sub"]))
        seedp = sc.seed_prompt("a city of brass and ash", "Hask's Smithy", "the forge", "a hot forge")
        rec("seed prompt is a grounded empty establishing view",
            "eye level" in seedp and "animals" in seedp.lower()
            and "bird's-eye" not in seedp)
        rec("seed prompt names the place and description",
            "the forge" in seedp and "a hot forge" in seedp)
        rec("seed prompt carries the empty-place guard", "no person" in seedp.lower())
        rec("seed prompt is timeless and weather-neutral",
            "weather-neutral" in seedp and "no rain, snow, fog" in seedp
            and "no dawn, dusk or night" in seedp)

        # ensure_scene: hidden seed + an action drawn fresh from it every time; each
        # action is a one-shot file (served once, then deleted).
        sc3 = SceneService(tmp)
        captured = []

        def _scene_bytes(prompt, aspect_ratio=None, refs=None, ref_media_resolution=None, model=None, thinking_level=None, seed=None):
            captured.append((prompt, refs, ref_media_resolution))
            return jpeg(800, 450)

        sc3.generate = _scene_bytes  # type: ignore[assignment]
        scene_dir = tmp / "images" / "scenetest" / "scenes"
        portrait_dir = tmp / "images" / "scenetest"
        portrait_dir.mkdir(parents=True, exist_ok=True)
        (portrait_dir / "portrait.jpg").write_bytes(jpeg(1200, 1600))
        r1 = sc3.ensure_scene("scenetest", PLAYER, "world", description="a forge at dusk",
                              mood="tense", kingdom="Kingdom of Eldoria", area="Eldoria City",
                              place=["Hask's Smithy", "the forge"],
                              establishing="a hot forge",
                              main_npcs=[{"name": "Gorson", "role": "the smith",
                                          "description": "a burly smith"}],
                              time_of_day="dusk", weather="light rain",
                              characters={"Gorson": "hammering at the anvil",
                                          "three dockhands": "drinking"})
        rec("first visit creates the seed and seeds the action from it",
            r1["seed_created"] and r1["used_seed"], str(r1))
        rec("the hidden seed is generated empty (no references)", not captured[0][1])
        rec("ensure_scene feeds time/weather into the action prompt",
            "Time of day: dusk." in captured[1][0] and "Weather: light rain." in captured[1][0])
        rec("ensure_scene feeds the character cast into the action prompt",
            "Characters present" in captured[1][0] and "- three dockhands: drinking" in captured[1][0])
        rec("the place main NPC's description is injected from the seed",
            "- Gorson — a burly smith: hammering at the anvil" in captured[1][0], captured[1][0][:300])
        rec("an undeclared name is drawn from its own key text",
            "- three dockhands: drinking" in captured[1][0])
        rec("the first action references seed + portrait",
            captured[1][1] is not None and len(captured[1][1]) == 2)
        rec("action references are downscaled to ref_max_side",
            all(max(PILImage.open(io.BytesIO(d)).size) <= 512 for d, _m in captured[1][1]))
        rec("reference media resolution set", captured[1][2] == "MEDIA_RESOLUTION_MEDIUM")
        rec("action written to disk", bool(list(scene_dir.glob("action-*"))))
        rec("hidden seed written to disk", bool(list(scene_dir.glob("seed-*"))))
        r2 = sc3.ensure_scene("scenetest", PLAYER, "world", description="embers dying",
                              place=["Hask's Smithy", "the forge"],
                              npcs=[{"name": "Maera", "description": "a broad, one-eared woman"}],
                              characters={"Maera": "drawing ale"})
        rec("every later action is drawn from the seed too, never the last action",
            r2["used_seed"] and not r2["seed_created"] and "used_last_action" not in r2, str(r2))
        rec("the second action's place reference is the same seed bytes",
            captured[2][1][0] == captured[1][1][0] and len(captured[2][1]) == 2)
        rec("npcs declared on the image call are added to the cast",
            r2["npcs_added"] == ["Maera"], str(r2))
        rec("a declared NPC's description is injected from the cast",
            "- Maera — a broad, one-eared woman: drawing ale" in captured[2][0], captured[2][0][:300])
        r3 = sc3.ensure_scene("scenetest", PLAYER, "world", description="downstairs",
                              kingdom="Kingdom of Eldoria", area="Eldoria City",
                              place=["Hask's Smithy", "common room"],
                              establishing="a low-beamed hall")
        rec("a new sublocation creates a new seed", r3["seed_created"] and r3["used_seed"], str(r3))
        r4 = sc3.ensure_scene("scenetest", PLAYER, "world", description="the rafters catch",
                              kingdom="Kingdom of Eldoria", area="Eldoria City",
                              place=["Hask's Smithy", "the forge"], seed_change="it burned down")
        rec("seed_change regenerates the seed and applies to this action immediately",
            r4["seed_regenerated"] and r4["used_seed"], str(r4))
        rs = sc3.ensure_scene("scenetest", PLAYER, "world", description="back for the ledger",
                              kingdom="Kingdom of Eldoria", area="Eldoria City",
                              place=["Hask's Smithy", "the forge"],
                              style="In the style of Day of the Tentacle: cartoony, bold outlines.")
        rec("a style change regenerates the existing seed",
            rs["seed_regenerated"] and rs["used_seed"], str(rs))
        seed_caps = [c for c in captured if "Empty and unpopulated" in c[0]]
        rec("a style-change redraw passes no old-seed reference",
            bool(seed_caps) and seed_caps[-1][1] is None, str(seed_caps[-1:] if seed_caps else []))
        seeds_now = live_scene_manifest(tmp, "scenetest")["seeds"]
        forge = [e for e in seeds_now.values() if e.get("place") == ["Hask's Smithy", "the forge"]]
        rec("the seed records the new style",
            bool(forge) and "Day of the Tentacle" in (forge[0].get("style") or ""), str(forge))
        rs2 = sc3.ensure_scene("scenetest", PLAYER, "world", description="still here",
                               kingdom="Kingdom of Eldoria", area="Eldoria City",
                               place=["Hask's Smithy", "the forge"],
                               style="In the style of Day of the Tentacle: cartoony, bold outlines.")
        rec("the same style does not regenerate again", not rs2.get("seed"), str(rs2))
        r5 = sc3.ensure_scene("scenetest", PLAYER, "world", description="on the road",
                              kingdom="Borderlands", area="the Eldoria–Silverwood border",
                              place=["Lantern Row"])
        rec("actions are not tracked in the manifest (ephemeral)",
            "actions" not in live_scene_manifest(tmp, "scenetest"),
            str(live_scene_manifest(tmp, "scenetest"))[:120])
        rec("seeds survive across places", len(list(scene_dir.glob("seed-*"))) == 3)
        got = sc3.get_scene("scenetest", r1["action"]["url"].rsplit("/", 1)[-1])
        rec("get_scene returns bytes + mime", bool(got) and got[1].startswith("image/"))
        rec("an action image is deleted after it has been served once",
            sc3.get_scene("scenetest", r1["action"]["url"].rsplit("/", 1)[-1]) is None)
        rec("a hidden seed is never served",
            sc3.get_scene("scenetest", list(scene_dir.glob("seed-*"))[0].stem) is None)
        places = known_scene_places(tmp, "scenetest")
        rec("known_scene_places lists seeded places with the full hierarchy",
            any(p["kingdom"] == "Kingdom of Eldoria" and p["area"] == "Eldoria City"
                and p["place"] == ["Hask's Smithy", "the forge"]
                and p["description"] == "a hot forge"
                and p["main_npcs"] == [{"name": "Gorson", "role": "the smith",
                                        "description": "a burly smith"}]
                for p in places), str(places))
        rec("seed_change preserved the main NPC",
            any(p["place"] == ["Hask's Smithy", "the forge"]
                and p["main_npcs"][0]["name"] == "Gorson" for p in places))
        rec("the main NPCs' roles round-trip through the manifest",
            any(p["place"] == ["Hask's Smithy", "the forge"]
                and p["main_npcs"][0]["role"] == "the smith" for p in places), str(places))
        live_now = live_scene_manifest(tmp, "scenetest")
        rec("the cast is persisted in the manifest (v8)",
            live_now["version"] == 8 and "maera" in live_now["cast"])
        rec("no last_cast bookkeeping remains", "last_cast" not in json.dumps(live_now))
        rec("known_npc_names lists place main NPCs + the storyline cast",
            known_npc_names(tmp, "scenetest") == {"gorson", "maera"},
            str(known_npc_names(tmp, "scenetest")))
        rec("known_scene_places empty for an unknown save", known_scene_places(tmp, "nope") == [])

        # ── manifest v8: the era roots the place path ────────────────────
        sc4 = SceneService(tmp)
        sc4.generate = _scene_bytes  # type: ignore[assignment]
        same = ["Hask's Smithy", "the forge"]
        for era_id in ("egypt", "wallachia"):
            sc4.ensure_scene("eratest", PLAYER, "world", description="a look around",
                             kingdom="Kingdom of Eldoria", area="Eldoria City", place=same,
                             establishing="a hot forge", era=era_id)
        seeds4 = live_scene_manifest(tmp, "eratest")["seeds"]
        rec("the same place in two eras is two seeds with two keys (v8)",
            len(seeds4) == 2 and sorted(e.get("era") for e in seeds4.values()) == ["egypt", "wallachia"],
            str(sorted((e.get("era"), e.get("slug")) for e in seeds4.values())))
        rec("the era is the root of the manifest key",
            all(str(k).startswith(e["era"] + "|") for k, e in seeds4.items()), str(sorted(seeds4)))
        rec("known_scene_places reports each place's era",
            sorted(p["era"] for p in known_scene_places(tmp, "eratest")) == ["egypt", "wallachia"],
            str(known_scene_places(tmp, "eratest")))
        rec("manifest version is 8", live_scene_manifest(tmp, "eratest")["version"] == 8)
        rec("a seed carries a `used` stamp for the LRU cap",
            all(isinstance(e.get("used"), int) and e["used"] >= e["created"]
                for e in seeds4.values()), str(seeds4))
        rec("known_scene_places(era=...) scopes to one era",
            [p["era"] for p in known_scene_places(tmp, "eratest", "egypt")] == ["egypt"]
            and [p["era"] for p in known_scene_places(tmp, "eratest", "wallachia")] == ["wallachia"])
        rec("known_scene_places exposes the LRU stamp",
            all(isinstance(p.get("used"), int) for p in known_scene_places(tmp, "eratest")))

        # a v7 manifest (no era) migrates on read, to the SAVE's era, and is not rewritten
        legacy = tmp / "images" / "legacytest" / "scenes"
        legacy.mkdir(parents=True, exist_ok=True)
        legacy_manifest = legacy / "manifest.json"
        legacy_manifest.write_text(json.dumps({
            "version": 7, "cast": {}, "current": {},
            "seeds": {"kingdom-of-eldoria|eldoria-city|hask-s-smithy|the-forge": {
                "file": "seed-x.png", "slug": "seed-x", "kingdom": "Kingdom of Eldoria",
                "area": "Eldoria City", "place": same, "description": "a hot forge",
                "main_npcs": [], "mime": "image/png", "created": 1}},
        }), encoding="utf-8")
        migrated = SceneService(tmp)._read_manifest("legacytest", "wallachia")
        rec("a v7 manifest migrates to v8, each place taking the save's era",
            migrated["version"] == 8
            and [e.get("era") for e in migrated["seeds"].values()] == ["wallachia"]
            and all(str(k).startswith("wallachia|") for k in migrated["seeds"]),
            str(migrated["seeds"]))
        rec("migration is read-only (the file keeps its version until a write)",
            json.loads(legacy_manifest.read_text(encoding="utf-8"))["version"] == 7)
        rec("a migrated place is scoped to the era it was read with",
            [p["era"] for p in known_scene_places(tmp, "legacytest", "wallachia")] == ["wallachia"]
            and known_scene_places(tmp, "legacytest", "egypt") == [])
        rec("known_scene_locations lists distinct place labels",
            known_scene_locations(tmp, "scenetest") == ["Hask's Smithy — common room",
                                                        "Hask's Smithy — the forge", "Lantern Row"],
            str(known_scene_locations(tmp, "scenetest")))

        # ── appearance-changing effects: the disguise replaces the portrait ──
        disguised = dict(PLAYER)
        disguised["equipped"] = {"armor": "Chain Mail", "worn": [], "hands": ["Longsword", None]}
        disguised["active_effects"] = [{
            "name": "Disguise Self (active)",
            "description": ("You have altered your appearance to that of a human merchant — a "
                            "middling, forgettable fellow with a soft jaw, thinning brown hair "
                            "and plain travelling clothes. Lasts 1 hour."),
        }]
        ov = appearance_override(disguised)
        rec("Disguise Self yields an appearance override that covers gear",
            bool(ov) and ov["covers_gear"] and "human merchant" in ov["look"], str(ov))
        rec("the trailing 'Lasts ...' clause is stripped",
            ov is not None and "Lasts" not in ov["look"], str(ov))
        alter = appearance_override({"active_effects": [{"name": "Alter Self",
                                                          "description": "a taller, greyer elf"}]})
        rec("Alter Self overrides the form but NOT the gear",
            bool(alter) and alter["covers_gear"] is False, str(alter))
        rec("no override without an appearance effect", appearance_override(PLAYER) is None)
        explicit = appearance_override({"active_effects": [
            {"name": "Homebrew Mask", "appearance": "a pale, voiceless stranger"}]})
        rec("an explicit appearance field wins",
            bool(explicit) and explicit["look"] == "a pale, voiceless stranger", str(explicit))

        captured.clear()
        ra = sc3.ensure_scene("scenetest", disguised, "world", description="haggling at a stall",
                              place=["Lantern Row"])
        rec("a disguised action does NOT attach the portrait reference",
            ra["appearance_applied"] and not ra["used_portrait_reference"]
            and len(captured[-1][1]) == 1, str(ra))
        rec("the disguise look replaces the protagonist in the prompt",
            "human merchant" in captured[-1][0]
            and "fixed by the attached portrait" not in captured[-1][0], captured[-1][0][:300])
        rec("the SRD illusion replaces clothing/armour/weapons in the prompt",
            "Longsword" not in captured[-1][0]
            and "The illusion covers clothing, armour and weapons" in captured[-1][0])
        rec("the seed reference drops the portrait sentence",
            "protagonist's portrait" not in captured[-1][0])

        captured.clear()
        sc3.ensure_scene("scenetest", {**PLAYER, "equipped": disguised["equipped"]}, "world",
                         description="training", place=["Lantern Row"])
        rec("without a disguise the real gear and the portrait are still used",
            "Longsword" in captured[-1][0] and "attached portrait" in captured[-1][0]
            and len(captured[-1][1]) == 2)

        # v3 -> v6 migration: the action list (and its files) is abandoned; a legacy
        # string main_npc becomes a one-item main_npcs list (no name -> no expansion).
        legacy = scene_dir / "action-legacy-1.jpg"
        legacy.write_bytes(jpeg(64, 36))
        discard_scene_manifest(tmp, "scenetest")  # the registry is memory-only now
        (scene_dir / "manifest.json").write_text(json.dumps({
            "version": 3,
            "seeds": {"k": {"file": "seed-x.png", "location": "Old Place", "sublocation": "",
                            "description": "", "main_npc": "Gorson — a burly smith",
                            "last_cast": {"three dockhands": "drinking"},
                            "created": 1, "mime": "image/png"}},
            "actions": [{"file": "action-legacy-1.jpg", "slug": "action-legacy-1"}],
            "current": {"location": "Old Place"}, "seq": 7,
        }), encoding="utf-8")
        migrated = sc3._read_manifest("scenetest")
        migrated_entry = next(iter(migrated["seeds"].values()), {})
        rec("a v3 manifest migrates to v8 (actions + seq + last_cast dropped, seeds + current kept)",
            migrated["version"] == 8 and "actions" not in migrated and "seq" not in migrated
            and migrated["cast"] == {} and migrated["current"].get("place") == ["Old Place"]
            and migrated_entry.get("main_npcs") == [{"name": "",
                                                      "description": "Gorson — a burly smith",
                                                      "role": ""}]
            and "main_npc" not in migrated_entry
            and "last_cast" not in migrated_entry, str(migrated))
        rec("migrating deletes the abandoned action files", not legacy.exists())

        sc2 = SceneService()
        fake2 = _FakeClient()
        sc2.backend._client_or_raise = lambda: fake2  # type: ignore[assignment]
        sc2.generate_action(PLAYER, "world", "a duel on the bridge", "mood", place=["Loc", "sub"])
        ic2 = getattr(fake2.captured.get("config"), "image_config", None)
        rec("scene aspect_ratio 16:9 sent to the SDK", ic2 is not None and ic2.aspect_ratio == "16:9")

    return all(RESULTS)


if __name__ == "__main__":
    ok = main()
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
