"""Compose a Daily Slate from ranked works without changing preference facts."""

from collections import Counter

from tag_categories import get_classification_category
from utils import normalize_tag


MOTIVES = ("feature", "character", "copyright", "exploration")


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

        def add_from(candidates, motive, count):
            for illust in candidates:
                if sum(getattr(item, "recommendation_motive", None) == motive for item in selected) >= count:
                    break
                if illust.id in used_ids or not self._within_caps(illust, classifications, profile, identity_counts):
                    continue
                illust.recommendation_motive = motive
                selected.append(illust)
                used_ids.add(illust.id)
                identity_counts.update(self._identity_keys(illust, classifications, profile))

        for motive in ("feature", "character", "copyright"):
            add_from((item for item in ranked if self._motive(item, classifications, profile) == motive), motive, quotas[motive])
        exploration_pool = [item for item in ranked[limit:] if self._motive(item, classifications, profile) == "feature"]
        add_from(exploration_pool, "exploration", quotas["exploration"])
        feature_shortfall = quotas["feature"] - sum(getattr(item, "recommendation_motive", None) == "feature" for item in selected)
        if feature_shortfall:
            add_from(exploration_pool, "exploration", min(int(limit * 0.40), quotas["exploration"] + feature_shortfall))
        for illust in ranked:
            if len(selected) >= limit:
                break
            if illust.id in used_ids or not self._within_caps(illust, classifications, profile, identity_counts):
                continue
            illust.recommendation_motive = self._motive(illust, classifications, profile)
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

    def _motive(self, illust, classifications, profile):
        if getattr(illust, "feature_contribution", 0.0) >= getattr(illust, "ip_contribution", 0.0) and getattr(illust, "feature_contribution", 0.0) > 0:
            return "feature"
        identity_weights = {"character": 0.0, "copyright": 0.0}
        for tag in illust.tags or []:
            normalized = normalize_tag(tag)
            category = get_classification_category(classifications.get(normalized))
            if category in identity_weights:
                identity_weights[category] = max(
                    identity_weights[category], profile.get(normalized, profile.get(tag.lower(), 0.0))
                )
        return "character" if identity_weights["character"] >= identity_weights["copyright"] else "copyright"

    def _identity_keys(self, illust, classifications, profile):
        keys = []
        for tag in illust.tags or []:
            normalized = normalize_tag(tag)
            category = get_classification_category(classifications.get(normalized))
            if category in {"character", "copyright"} and profile.get(normalized, profile.get(tag.lower(), 0.0)) > 0:
                keys.append(f"{category}:{normalized}")
        return keys

    def _within_caps(self, illust, classifications, profile, counts):
        for key in self._identity_keys(illust, classifications, profile):
            cap = self.max_per_character if key.startswith("character:") else self.max_per_copyright
            if counts[key] >= cap:
                return False
        return True
