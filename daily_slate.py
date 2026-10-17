"""Compose a Daily Slate from typed Preference Contributions and Delivery Policy."""

import math
from collections import Counter
from dataclasses import dataclass

from tag_categories import (
    get_classification_category,
    is_feature_category,
    is_identity_category,
    is_seed_category,
)
from tag_mapping import TagIdentityResolver
from utils import normalize_tag


MOTIVES = ("feature", "character", "copyright", "exploration")
MOTIVE_TIE_ORDER = ("feature", "character", "copyright")
FEATURE_CONTRIBUTION_SCHEDULE = (1.0, 0.5, 0.25)


@dataclass(frozen=True)
class PreferenceContributions:
    max_weight: float = 1.0
    matched_count: int = 0
    high_weight_matches: int = 0
    negative_penalty: float = 0.0
    total_score: float = 0.0
    feature: float = 0.0
    identity: float = 0.0
    character: float = 0.0
    copyright: float = 0.0
    feature_match_count: int = 0

    @property
    def motive(self) -> str:
        values = {
            "feature": self.feature,
            "character": self.character,
            "copyright": self.copyright,
        }
        strongest = max(values.values())
        if strongest <= 0:
            return "exploration"
        return next(
            motive for motive in MOTIVE_TIE_ORDER
            if values[motive] == strongest
        )

    @property
    def match_score(self) -> float:
        if self.matched_count == 0:
            if self.negative_penalty > 0:
                return max(
                    -self.negative_penalty / (self.max_weight + 1),
                    -0.5,
                )
            return 0.0
        base_score = (
            self.total_score / (self.matched_count * self.max_weight)
            if self.max_weight > 0
            else 0.0
        )
        quantity_bonus = min(
            math.log(1 + self.matched_count) / math.log(6),
            0.3,
        )
        quality_bonus = min(self.high_weight_matches * 0.05, 0.2)
        penalty = (
            self.negative_penalty / (self.max_weight + 1)
            if self.max_weight > 0
            else 0.0
        )
        return max(min(base_score + quantity_bonus + quality_bonus - penalty, 1.0), 0.0)


@dataclass(frozen=True)
class DailySlateResult:
    selected: tuple
    ranked: tuple
    motives: dict[int, str]
    contributions: dict[int, PreferenceContributions]


def calculate_preference_contributions(
    tags: list[str],
    profile: dict[str, float],
    negative_profile: dict[str, float] | None = None,
    classifications: dict | None = None,
    resolver: TagIdentityResolver | None = None,
) -> PreferenceContributions:
    if not tags or not profile:
        return PreferenceContributions()

    sorted_weights = sorted(profile.values(), reverse=True)
    max_weight = sorted_weights[0] if sorted_weights else 1.0
    top_threshold = (
        sorted_weights[len(sorted_weights) // 5]
        if len(sorted_weights) >= 5
        else max_weight * 0.8
    )
    feature_weights: list[tuple[float, float]] = []
    identity_weights: list[tuple[float, float]] = []
    typed_identity_weights = {"character": [], "copyright": []}
    negative_penalty = 0.0
    seen_positive_tags: set[str] = set()
    seen_negative_tags: set[str] = set()
    resolver = resolver or TagIdentityResolver()

    for tag in tags:
        normalized = resolver.resolve(tag)
        tag_key = normalized or tag.lower().strip()
        weight = (
            profile.get(normalized)
            if normalized in profile
            else profile.get(tag.lower())
        )
        classification = (classifications or {}).get(normalized)
        preference_bearing = is_seed_category(classification)
        if (
            preference_bearing
            and weight is not None
            and tag_key not in seen_positive_tags
        ):
            seen_positive_tags.add(tag_key)
            effective_weight = float(weight) * (
                1.3 if is_feature_category(classification) else 1.0
            )
            if is_feature_category(classification):
                feature_weights.append((effective_weight, float(weight)))
            else:
                if is_identity_category(classification):
                    identity_weights.append((effective_weight, float(weight)))
                    category = getattr(
                        classification,
                        "classification",
                        classification,
                    )
                    category = str(category).lower()
                    if category in typed_identity_weights:
                        typed_identity_weights[category].append(effective_weight)

        if (
            preference_bearing
            and negative_profile
            and tag_key not in seen_negative_tags
        ):
            seen_negative_tags.add(tag_key)
            negative = negative_profile.get(
                normalized,
                negative_profile.get(tag.lower(), 0),
            )
            if negative > 0:
                negative_penalty += negative * 0.5

    feature_weights.sort(key=lambda item: item[0], reverse=True)
    feature_contribution = 0.0
    feature_high_weight_matches = 0
    for index, (effective_weight, raw_weight) in enumerate(
        feature_weights[:len(FEATURE_CONTRIBUTION_SCHEDULE)]
    ):
        feature_contribution += (
            effective_weight * FEATURE_CONTRIBUTION_SCHEDULE[index]
        )
        if raw_weight >= top_threshold:
            feature_high_weight_matches += 1

    identity_contribution, identity_raw_weight = max(
        identity_weights,
        key=lambda item: item[0],
        default=(0.0, 0.0),
    )
    identity_high_weight_match = int(
        bool(identity_weights) and identity_raw_weight >= top_threshold
    )
    return PreferenceContributions(
        max_weight=max_weight,
        matched_count=min(
            len(feature_weights),
            len(FEATURE_CONTRIBUTION_SCHEDULE),
        ) + int(bool(identity_weights)),
        high_weight_matches=(
            feature_high_weight_matches + identity_high_weight_match
        ),
        negative_penalty=negative_penalty,
        total_score=feature_contribution + identity_contribution,
        feature=feature_contribution,
        identity=identity_contribution,
        character=max(typed_identity_weights["character"], default=0.0),
        copyright=max(typed_identity_weights["copyright"], default=0.0),
        feature_match_count=len(feature_weights),
    )


class DailySlateComposer:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.ratios = {motive: float(cfg.get(f"{motive}_ratio", default)) for motive, default in (
            ("feature", 0.55), ("character", 0.15), ("copyright", 0.10), ("exploration", 0.20)
        )}
        self.max_per_character = max(1, int(cfg.get("max_per_character", 2)))
        self.max_per_copyright = max(1, int(cfg.get("max_per_copyright", 4)))

    def compose(
        self,
        ranked,
        limit: int,
        classifications,
        profile,
        contributions: dict[int, PreferenceContributions],
        resolver: TagIdentityResolver | None = None,
    ) -> DailySlateResult:
        if not self.enabled:
            selected = tuple(ranked[:limit])
            return DailySlateResult(
                selected,
                tuple(ranked),
                {},
                dict(contributions),
            )
        quotas = self._quotas(limit)
        selected, used_ids, identity_counts = [], set(), Counter()
        motives: dict[int, str] = {}

        def add(candidates, motive, target):
            for illust in candidates:
                if (
                    len(selected) >= limit
                    or sum(value == motive for value in motives.values()) >= target
                ):
                    break
                if illust.id in used_ids or not self._within_caps(
                    illust,
                    classifications,
                    profile,
                    identity_counts,
                    resolver,
                ):
                    continue
                illust.recommendation_motive = motive
                motives[illust.id] = motive
                selected.append(illust)
                used_ids.add(illust.id)
                identity_counts.update(
                    self._identity_keys(illust, classifications, profile, resolver)
                )

        inherent = {
            illust.id: contributions.get(
                illust.id,
                PreferenceContributions(),
            ).motive
            for illust in ranked
        }
        for motive in ("feature", "character", "copyright"):
            add(
                (
                    illust for illust in ranked
                    if not getattr(illust, "exploration_only", False)
                    and inherent[illust.id] == motive
                ),
                motive,
                quotas[motive],
            )

        feature_shortfall = quotas["feature"] - sum(
            value == "feature" for value in motives.values()
        )
        exploration_target = min(int(limit * 0.40), quotas["exploration"] + max(feature_shortfall, 0))
        # Explicit Exploration retrieval candidates are eligible only for this
        # lane. Ordinary candidates still need to sit outside the normal Top N.
        vector_exploration = [
            illust for illust in ranked
            if getattr(illust, "exploration_only", False)
        ]
        ordinary_exploration = [
            illust for illust in ranked[limit:]
            if not getattr(illust, "exploration_only", False)
            and inherent[illust.id] in {"feature", "exploration"}
        ]
        exploration_pool = vector_exploration + ordinary_exploration
        add(exploration_pool, "exploration", exploration_target)

        # Only after exploration reaches its policy ceiling may identity lanes fill remaining capacity.
        for illust in ranked:
            if len(selected) >= limit:
                break
            if illust.id in used_ids or not self._within_caps(
                illust,
                classifications,
                profile,
                identity_counts,
                resolver,
            ):
                continue
            final_motive = (
                "exploration"
                if getattr(illust, "exploration_only", False)
                else inherent[illust.id]
            )
            if final_motive == "exploration" and sum(
                value == "exploration" for value in motives.values()
            ) >= int(limit * 0.40):
                continue
            illust.recommendation_motive = final_motive
            motives[illust.id] = final_motive
            selected.append(illust)
            used_ids.add(illust.id)
            identity_counts.update(
                self._identity_keys(illust, classifications, profile, resolver)
            )
        return DailySlateResult(
            tuple(selected),
            tuple(ranked),
            motives,
            dict(contributions),
        )

    def _quotas(self, limit):
        exact = {motive: limit * ratio for motive, ratio in self.ratios.items()}
        quotas = {motive: int(value) for motive, value in exact.items()}
        for motive in sorted(MOTIVES, key=lambda item: exact[item] - quotas[item], reverse=True):
            if sum(quotas.values()) >= limit:
                break
            quotas[motive] += 1
        return quotas

    def _identity_keys(self, illust, classifications, profile, resolver=None):
        resolver = resolver or TagIdentityResolver()
        strongest = {"character": (0.0, None), "copyright": (0.0, None)}
        for tag in illust.tags or []:
            normalized = resolver.resolve(tag)
            category = get_classification_category(classifications.get(normalized))
            if category in strongest:
                weight = float(profile.get(normalized, profile.get(tag.lower(), 0.0)))
                # Identity Caps apply even when Exploration introduces an
                # identity absent from the current Preference Profile.
                if strongest[category][1] is None or weight > strongest[category][0]:
                    strongest[category] = (weight, normalized)
        return [f"{category}:{tag}" for category, (_, tag) in strongest.items() if tag]

    def _within_caps(self, illust, classifications, profile, counts, resolver=None):
        for key in self._identity_keys(illust, classifications, profile, resolver):
            cap = self.max_per_character if key.startswith("character:") else self.max_per_copyright
            if counts[key] >= cap:
                return False
        return True
