from types import SimpleNamespace

from daily_slate import DailySlateComposer
from tag_categories import TagClassification


def work(identifier, tags, feature=0, character=0, copyright=0):
    return SimpleNamespace(
        id=identifier,
        tags=tags,
        feature_contribution=feature,
        character_contribution=character,
        copyright_contribution=copyright,
    )


def test_motive_uses_strongest_typed_contribution_with_feature_tie_break():
    slate = DailySlateComposer({"enabled": True})
    assert slate._motive(work(1, [], feature=2, character=2)) == "feature"
    assert slate._motive(work(2, [], character=3, copyright=3)) == "character"
    assert slate._motive(work(3, [], copyright=4)) == "copyright"


def test_feature_shortfall_expands_exploration_before_identity_fill():
    slate = DailySlateComposer({"enabled": True})
    ranked = [
        work(1, ["character_a"], character=5),
        work(2, ["copyright_a"], copyright=5),
        work(3, ["new_feature"], feature=1),
        work(4, ["ordinary_identity"], character=1),
        work(5, ["ordinary_identity_two"], copyright=1),
        work(6, ["new_space"]),
        work(7, ["new_style"]),
        work(8, ["new_subject"]),
    ]
    classifications = {"new_feature": TagClassification("feature", "manual")}
    classifications.update({"character_a": TagClassification("character", "manual"), "copyright_a": TagClassification("copyright", "manual")})
    selected = slate.compose(ranked, 5, classifications, {tag: 1 for tag in classifications})
    assert sum(item.recommendation_motive == "exploration" for item in selected) >= 2
    assert sum(item.recommendation_motive == "exploration" for item in selected) <= 2


def test_identity_caps_apply_to_primary_character_and_copyright_across_motives():
    slate = DailySlateComposer({"enabled": True, "max_per_character": 1, "max_per_copyright": 1})
    ranked = [work(1, ["character_a", "copyright_a"], feature=3), work(2, ["character_a", "copyright_a"], feature=2)]
    classifications = {"character_a": TagClassification("character", "manual"), "copyright_a": TagClassification("copyright", "manual")}
    selected = slate.compose(ranked, 2, classifications, {"character_a": 1, "copyright_a": 1})
    assert [item.id for item in selected] == [1]
