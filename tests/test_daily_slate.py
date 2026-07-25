from types import SimpleNamespace

from daily_slate import DailySlateComposer, PreferenceContributions
from tag_categories import TagClassification


def work(identifier, tags):
    return SimpleNamespace(id=identifier, tags=tags)


def contribution(feature=0, character=0, copyright=0):
    return PreferenceContributions(
        feature=feature,
        character=character,
        copyright=copyright,
    )


def test_motive_uses_strongest_typed_contribution_with_feature_tie_break():
    slate = DailySlateComposer({"enabled": True})
    assert contribution(feature=2, character=2).motive == "feature"
    assert contribution(character=3, copyright=3).motive == "character"
    assert contribution(copyright=4).motive == "copyright"


def test_feature_shortfall_expands_exploration_before_identity_fill():
    slate = DailySlateComposer({"enabled": True})
    ranked = [
        work(1, ["character_a"]),
        work(2, ["copyright_a"]),
        work(3, ["new_feature"]),
        work(4, ["ordinary_identity"]),
        work(5, ["ordinary_identity_two"]),
        work(6, ["new_space"]),
        work(7, ["new_style"]),
        work(8, ["new_subject"]),
    ]
    contributions = {
        1: contribution(character=5),
        2: contribution(copyright=5),
        3: contribution(feature=1),
        4: contribution(character=1),
        5: contribution(copyright=1),
    }
    classifications = {"new_feature": TagClassification("feature", "manual")}
    classifications.update({"character_a": TagClassification("character", "manual"), "copyright_a": TagClassification("copyright", "manual")})
    result = slate.compose(
        ranked,
        5,
        classifications,
        {tag: 1 for tag in classifications},
        contributions,
    )
    selected = result.selected
    assert sum(item.recommendation_motive == "exploration" for item in selected) >= 2
    assert sum(item.recommendation_motive == "exploration" for item in selected) <= 2


def test_identity_caps_apply_to_primary_character_and_copyright_across_motives():
    slate = DailySlateComposer({"enabled": True, "max_per_character": 1, "max_per_copyright": 1})
    ranked = [
        work(1, ["character_a", "copyright_a"]),
        work(2, ["character_a", "copyright_a"]),
    ]
    classifications = {"character_a": TagClassification("character", "manual"), "copyright_a": TagClassification("copyright", "manual")}
    result = slate.compose(
        ranked,
        2,
        classifications,
        {"character_a": 1, "copyright_a": 1},
        {1: contribution(feature=3), 2: contribution(feature=2)},
    )
    assert [item.id for item in result.selected] == [1]
