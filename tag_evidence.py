"""Evidence collection primitives used by tag classification maintenance."""

from collections import defaultdict

from tag_categories import TAG_CATEGORY_UNRESOLVED, TagClassification, normalize_tag_category


def resolve_tag_evidence(tag: str, evidence: list[dict]) -> TagClassification:
    """Accept manual decisions or independent agreement; otherwise queue review."""
    manual = [item for item in evidence if item["source"] == "manual"]
    if manual:
        return TagClassification(manual[-1]["classification"], "manual")

    votes: dict[str, set[str]] = defaultdict(set)
    for item in evidence:
        category = normalize_tag_category(item["classification"])
        if category != TAG_CATEGORY_UNRESOLVED:
            votes[category].add(item["source"])

    accepted = [category for category, sources in votes.items() if len(sources) >= 2]
    if len(accepted) == 1:
        return TagClassification(accepted[0], "evidence_consensus")
    return TagClassification(TAG_CATEGORY_UNRESOLVED, "evidence_unresolved")
