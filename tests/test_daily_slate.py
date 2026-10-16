from types import SimpleNamespace

from daily_slate import (
    DailySlateComposer,
    PreferenceContributions,
    calculate_preference_contributions,
)
from tag_categories import TagClassification
from tag_mapping import TagIdentityResolver


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


def test_only_preference_bearing_tags_can_contribute_to_personalized_ranking():
    for category in ("artist", "non_preference", "unresolved", None):
        classifications = (
            {"untrusted_tag": TagClassification(category, "manual")}
            if category is not None
            else {}
        )
        result = calculate_preference_contributions(
            ["untrusted_tag"],
            {"untrusted_tag": 5.0},
            classifications=classifications,
        )

        assert result.match_score == 0.0
        assert result.total_score == 0.0
        assert result.matched_count == 0


def test_character_and_copyright_share_one_identity_contribution():
    classifications = {
        "favorite_character": TagClassification("character", "manual"),
        "favorite_series": TagClassification("copyright", "manual"),
    }
    profile = {"favorite_character": 8.0, "favorite_series": 1.0}

    character_only = calculate_preference_contributions(
        ["favorite_character"],
        profile,
        classifications=classifications,
    )
    both_identities = calculate_preference_contributions(
        ["favorite_character", "favorite_series"],
        profile,
        classifications=classifications,
    )

    assert both_identities.identity == 8.0
    assert both_identities.total_score == 8.0
    assert both_identities.matched_count == 1
    assert both_identities.match_score == character_only.match_score


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


def test_identity_caps_use_accepted_aliases_before_counting_identity():
    slate = DailySlateComposer({
        "enabled": True,
        "max_per_copyright": 1,
    })
    resolver = TagIdentityResolver({"ブルアカ": "blue_archive"})
    ranked = [
        work(1, ["ブルアカ"]),
        work(2, ["blue_archive"]),
    ]

    result = slate.compose(
        ranked,
        2,
        {"blue_archive": TagClassification("copyright", "manual")},
        {"blue_archive": 1.0},
        {1: contribution(feature=3), 2: contribution(feature=2)},
        resolver=resolver,
    )

    assert [item.id for item in result.selected] == [1]


def test_motive_ratios_can_never_select_more_than_the_daily_limit():
    slate = DailySlateComposer({
        "enabled": True,
        "feature_ratio": 1.0,
        "character_ratio": 1.0,
        "copyright_ratio": 1.0,
        "exploration_ratio": 1.0,
    })
    ranked = [work(identifier, [f"tag_{identifier}"]) for identifier in range(1, 10)]
    classifications = {
        **{
            f"tag_{identifier}": TagClassification("feature", "manual")
            for identifier in range(1, 4)
        },
        **{
            f"tag_{identifier}": TagClassification("character", "manual")
            for identifier in range(4, 7)
        },
        **{
            f"tag_{identifier}": TagClassification("copyright", "manual")
            for identifier in range(7, 10)
        },
    }
    contributions = {
        **{identifier: contribution(feature=1) for identifier in range(1, 4)},
        **{identifier: contribution(character=1) for identifier in range(4, 7)},
        **{identifier: contribution(copyright=1) for identifier in range(7, 10)},
    }

    result = slate.compose(
        ranked,
        3,
        classifications,
        {tag: 1.0 for tag in classifications},
        contributions,
    )

    assert len(result.selected) == 3
