"""Shared Tag Category primitives for recommendation decisions."""

from dataclasses import dataclass

TAG_CATEGORY_FEATURE = "feature"
TAG_CATEGORY_CHARACTER = "character"
TAG_CATEGORY_COPYRIGHT = "copyright"
TAG_CATEGORY_ARTIST = "artist"
TAG_CATEGORY_NON_PREFERENCE = "non_preference"
TAG_CATEGORY_UNRESOLVED = "unresolved"

SEED_TAG_CATEGORIES = {
    TAG_CATEGORY_FEATURE,
    TAG_CATEGORY_CHARACTER,
    TAG_CATEGORY_COPYRIGHT,
}
IDENTITY_TAG_CATEGORIES = {
    TAG_CATEGORY_CHARACTER,
    TAG_CATEGORY_COPYRIGHT,
}

_CATEGORY_ALIASES = {
    "feature": TAG_CATEGORY_FEATURE,
    "features": TAG_CATEGORY_FEATURE,
    "character": TAG_CATEGORY_CHARACTER,
    "characters": TAG_CATEGORY_CHARACTER,
    "copyright": TAG_CATEGORY_COPYRIGHT,
    "copyrights": TAG_CATEGORY_COPYRIGHT,
    "ip": TAG_CATEGORY_COPYRIGHT,
    "artist": TAG_CATEGORY_ARTIST,
    "artists": TAG_CATEGORY_ARTIST,
    "non-preference": TAG_CATEGORY_NON_PREFERENCE,
    "non_preference": TAG_CATEGORY_NON_PREFERENCE,
    "nonpreference": TAG_CATEGORY_NON_PREFERENCE,
    "non-preferences": TAG_CATEGORY_NON_PREFERENCE,
    "non_preferences": TAG_CATEGORY_NON_PREFERENCE,
    "nonpreferences": TAG_CATEGORY_NON_PREFERENCE,
    "unresolved": TAG_CATEGORY_UNRESOLVED,
    "unknown": TAG_CATEGORY_UNRESOLVED,
    "needs_review": TAG_CATEGORY_UNRESOLVED,
}


def normalize_tag_category(value: str | None) -> str:
    """Return the canonical lowercase Tag Category used by the pipeline."""
    if value is None:
        return TAG_CATEGORY_UNRESOLVED
    key = str(value).strip().lower().replace(" ", "_")
    return _CATEGORY_ALIASES.get(key, TAG_CATEGORY_UNRESOLVED)


def get_classification_category(classification) -> str | None:
    if classification is None:
        return None
    return normalize_tag_category(getattr(classification, "classification", classification))


def is_feature_category(classification) -> bool:
    return get_classification_category(classification) == TAG_CATEGORY_FEATURE


def is_identity_category(classification) -> bool:
    return get_classification_category(classification) in IDENTITY_TAG_CATEGORIES


def is_seed_category(classification) -> bool:
    return get_classification_category(classification) in SEED_TAG_CATEGORIES


@dataclass(frozen=True)
class TagClassification:
    classification: str
    source: str

    def __post_init__(self):
        object.__setattr__(
            self,
            "classification",
            normalize_tag_category(self.classification),
        )
