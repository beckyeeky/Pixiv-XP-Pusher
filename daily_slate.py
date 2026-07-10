"""Compose a Daily Slate from typed Preference Contributions and Delivery Policy."""

from collections import Counter

from tag_categories import get_classification_category
from utils import normalize_tag


MOTIVES = ("feature", "character", "copyright", "exploration")
MOTIVE_TIE_ORDER = ("feature", "character", "copyright")


class DailySlateComposer:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.ratios = {motive: float(cfg.get(f"{motive}_ratio", default)) for motive, default in (
            ("feature", 0.55), ("character", 0.15), ("copyright", 0.10), ("exploration", 0.20)
        )}
        self.max_per_character = max(1, int(cfg.get("max_per_character", 2)))
        self.max_per_copyright = max(1, int(cfg.get("max_per_copyright", 4)))

    def compose(self, ranked, limit: int, classifications, profile):
        if not self.enabled:
            return list(ranked[:limit])
        quotas = self._quotas(limit)
        selected, used_ids, identity_counts = [], set(), Counter()

        def add(candidates, motive, target):
            for illust in candidates:
                if sum(getattr(item, "recommendation_motive", None) == motive for item in selected) >= target:
                    break
                if illust.id in used_ids or not self._within_caps(illust, classifications, profile, identity_counts):
                    continue
                illust.recommendation_motive = motive
                selected.append(illust)
                used_ids.add(illust.id)
                identity_counts.update(self._identity_keys(illust, classifications, profile))

        inherent = {illust.id: self._motive(illust) for illust in ranked}
        for motive in ("feature", "character", "copyright"):
            add((illust for illust in ranked if inherent[illust.id] == motive), motive, quotas[motive])

        feature_shortfall = quotas["feature"] - sum(getattr(item, "recommendation_motive", None) == "feature" for item in selected)
        exploration_target = min(int(limit * 0.40), quotas["exploration"] + max(feature_shortfall, 0))
        # Exploration intentionally draws outside ordinary top-ranked picks.
        exploration_pool = [illust for illust in ranked[limit:] if inherent[illust.id] in {"feature", "exploration"}]
        add(exploration_pool, "exploration", exploration_target)

        # Only after exploration reaches its policy ceiling may identity lanes fill remaining capacity.
        for illust in ranked:
            if len(selected) >= limit:
                break
            if illust.id in used_ids or not self._within_caps(illust, classifications, profile, identity_counts):
                continue
            if inherent[illust.id] == "exploration" and sum(
                getattr(item, "recommendation_motive", None) == "exploration" for item in selected
            ) >= int(limit * 0.40):
                continue
            illust.recommendation_motive = inherent[illust.id]
            selected.append(illust)
            used_ids.add(illust.id)
            identity_counts.update(self._identity_keys(illust, classifications, profile))
        return selected

    def _quotas(self, limit):
        exact = {motive: limit * ratio for motive, ratio in self.ratios.items()}
        quotas = {motive: int(value) for motive, value in exact.items()}
        for motive in sorted(MOTIVES, key=lambda item: exact[item] - quotas[item], reverse=True):
            if sum(quotas.values()) >= limit:
                break
            quotas[motive] += 1
        return quotas

    def _motive(self, illust):
        contributions = {
            "feature": float(getattr(illust, "feature_contribution", 0.0)),
            "character": float(getattr(illust, "character_contribution", 0.0)),
            "copyright": float(getattr(illust, "copyright_contribution", 0.0)),
        }
        strongest = max(contributions.values())
        if strongest <= 0:
            return "exploration"
        return next(motive for motive in MOTIVE_TIE_ORDER if contributions[motive] == strongest)

    def _identity_keys(self, illust, classifications, profile):
        strongest = {"character": (0.0, None), "copyright": (0.0, None)}
        for tag in illust.tags or []:
            normalized = normalize_tag(tag)
            category = get_classification_category(classifications.get(normalized))
            if category in strongest:
                weight = float(profile.get(normalized, profile.get(tag.lower(), 0.0)))
                if weight > strongest[category][0]:
                    strongest[category] = (weight, normalized)
        return [f"{category}:{tag}" for category, (_, tag) in strongest.items() if tag]

    def _within_caps(self, illust, classifications, profile, counts):
        for key in self._identity_keys(illust, classifications, profile):
            cap = self.max_per_character if key.startswith("character:") else self.max_per_copyright
            if counts[key] >= cap:
                return False
        return True
