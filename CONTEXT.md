# Pixiv Recommendation

This context describes how Pixiv works are understood, selected, and presented as personalized recommendations.

## Language

**Daily Slate**:
The final set of works delivered in one daily recommendation run. It is the product outcome whose composition is controlled.
_Avoid_: Candidate pool, search results

**Recommendation Motive**:
The single dominant reason a work belongs in the Daily Slate, independent of which retrieval source found it. It is determined by the strongest Preference Contribution, with ties broken in favor of Feature-led over Character-led over Copyright-led.
_Avoid_: Source, strategy, matched tag

**Motive Mix**:
The target composition of a Daily Slate by Recommendation Motive. It is independent of retrieval-source allocation.
_Avoid_: Strategy quota, candidate-source mix

**Identity Cap**:
The maximum number of works in a Daily Slate that may share the same primary Character or Copyright identity, regardless of Recommendation Motive.
_Avoid_: Motive quota, source quota, score decay

**Identity Contribution**:
The preference support attributable to who or which source work is depicted. When both Character and Copyright preferences support the same work, it uses the stronger contribution rather than summing naturally co-occurring identities.
_Avoid_: Character-plus-Copyright score

**Feature Contribution**:
The preference support attributable to transferable visual features on a work. The strongest matching Feature Tag contributes in full, and additional distinct Feature Tags contribute with diminishing weight rather than full summation.
_Avoid_: Full feature stack, all-feature sum

**Preference Contribution**:
The independent strength with which one semantic kind of known preference supports recommending a work. The strongest contribution determines the Recommendation Motive.
_Avoid_: Tag presence, retrieval source

**Preference Profile**:
The observed affinity facts derived from the user's behavior. It preserves the raw relative strength of Feature, Character, and Copyright preferences without applying Daily Slate diversity policy.
_Avoid_: Delivery quota, diversity-adjusted score

**Delivery Policy**:
The retrieval, ranking, Motive Mix, and Identity Cap rules that shape a Preference Profile into a Daily Slate without rewriting the underlying preference facts.
_Avoid_: User preference, profile weight

**Feature-led**:
A Recommendation Motive where visual traits, clothing, pose, composition, or subject matter are the dominant reason for recommending the work.

**Character-led**:
A Recommendation Motive where affinity for a specific fictional character is the dominant reason for recommending the work.

**Copyright-led**:
A Recommendation Motive where affinity for a franchise, series, game, anime, manga, or other source work is the dominant reason for recommending the work.

**Exploration**:
A Recommendation Motive for works intentionally selected without relying on an established strong identity preference. Exploration may still include familiar Characters or Copyrights when the dominant reason is a new transferable feature or deliberate taste expansion rather than identity affinity.

## Tag Semantics

**Normalized Tag**:
The stable, canonical identity under which equivalent raw Pixiv tag spellings are aggregated and classified.
_Avoid_: Raw tag, per-work tag occurrence

**Tag Category**:
The single recommendation-relevant meaning assigned to a normalized tag: Feature, Character, Copyright, Artist, or Non-preference. A tag has no category until its meaning is resolved.
_Avoid_: Multi-label type, review status

**Feature Tag**:
A tag whose meaning describes visual traits, clothing, pose, composition, or subject matter that can transfer across characters and copyrights.

**Character Tag**:
A tag whose meaning identifies a specific fictional character.

**Copyright Tag**:
A tag whose meaning identifies a franchise, series, game, anime, manga, or other source work.
_Avoid_: Character tag

**Identity Tag**:
A Character Tag or Copyright Tag that identifies who or which source work is depicted, rather than a transferable visual feature.

**Artist Tag**:
A tag whose meaning identifies a creator rather than the depicted content.

**Non-preference Tag**:
A resolved tag that does not express a useful recommendation preference, such as popularity markers, platform labels, event labels, or content metadata.
_Avoid_: Meta tag, noise

**Unresolved Tag**:
A tag with no accepted Tag Category because available classifiers disagree, lack enough evidence, or fail. It waits for human review and does not act as a personalized retrieval seed.
_Avoid_: Unknown category, needs-review category

**Review Queue**:
Unresolved Tags awaiting a human Tag Category decision, ordered by their likely impact on recommendation outcomes.
_Avoid_: Error log, all classified tags

**Classification Consensus**:
Sufficient independent agreement to accept a Tag Category without human review. A lack of consensus leaves the tag unresolved.

**Judge Model**:
A uniquely configured language model that contributes one independent classification vote. Repeated calls to the same provider and model identity remain a single Judge Model.
_Avoid_: Request, retry, provider account

**Tag Evidence**:
Information from Pixiv context, external tag systems, or prior human decisions that supports classification without itself being the accepted Tag Category.
_Avoid_: Classification, source of truth

**Seed Tag**:
A resolved, preference-bearing tag permitted to initiate personalized retrieval. Unresolved and Non-preference Tags are never Seed Tags.
_Avoid_: Any profile tag, matched tag

**Classification Maintenance**:
The periodic process that gathers Tag Evidence and Judge Model votes for important profile tags, accepting classifications with consensus and leaving the rest unresolved. Daily recommendation delivery consumes its latest accepted results without waiting for it.
_Avoid_: Daily recommendation run, profile rebuild

