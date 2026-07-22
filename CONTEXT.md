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

## Vector Retrieval

**Cached-Vector Scan**:
The bounded retrieval source that compares a current Preference Profile vector with recent cached work vectors to find Exploration candidates.
_Avoid_: Full vector search, vector index

**Retrieval Sufficiency**:
The state in which a Cached-Vector Scan meets the approved latency and candidate-coverage needs for Exploration. It does not describe recommendation quality.
_Avoid_: Recommendation quality, user satisfaction

**Index Eligibility**:
The evidence-based state that permits comparison of vector-index designs after a Cached-Vector Scan fails an approved latency or coverage requirement.
_Avoid_: Index rollout, automatic upgrade

**Vector Index**:
A replaceable derived cache that can accelerate vector lookup. It is not a source of Preference Profile facts, work facts, Tag Aliases, or recommendation policy.
_Avoid_: Preference store, recommendation rule

## Tag Semantics

**Normalized Tag**:
The stable, canonical identity under which equivalent raw Pixiv tag spellings are aggregated and classified.
_Avoid_: Raw tag, per-work tag occurrence

**Tag Alias**:
A human-accepted equivalence between a raw tag spelling and one Normalized Tag. Only a Tag Alias may aggregate preference observations under another tag identity.
_Avoid_: Automatic synonym, mapping cache

**Search Alias**:
A human-accepted retrieval spelling associated with a Normalized Tag without declaring identity equivalence.
_Avoid_: Tag Alias, canonical tag

**Tag Mapping Candidate**:
An untrusted proposal that a raw tag may be a Tag Alias or Search Alias. It has no effect on profile construction, retrieval, classification, or ranking until a human accepts it.
_Avoid_: Automatic mapping, pending alias

**Tag Relationship Judge**:
An optional configured LLM Model, such as DeepSeek, which reviews one Tag Mapping Candidate using both tags, classifications, translations, Grounded Judge explanations, profile weights, optional Embedding similarity, proposal provenance, and versioned merge principles. It produces an AI Relationship Recommendation and never accepts a candidate.
_Avoid_: Grounded Judge, alias activation

**AI Relationship Recommendation**:
A persisted advisory result containing Equivalent, Related, Distinct, or Uncertain, confidence, rationale, canonical-name advice, risk flags, model identity, principle version, and an evidence snapshot. It may be shortlisted for human review but cannot create a Tag Alias.
_Avoid_: Tag Alias, human acceptance

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
A tag for which the Grounded Judge failed or explicitly returned `unresolved`. It waits for human review and does not act as a personalized retrieval seed.
_Avoid_: Unknown category, needs-review category

**Review Queue**:
Unresolved Tags awaiting a human Tag Category decision, ordered by their likely impact on recommendation outcomes.
_Avoid_: Error log, all classified tags

**Grounded Judge**:
The one configured AI model that uses live search to classify a Normalized Tag. Gemini with Google Search Grounding is the initial Grounded Judge; a successful result is accepted directly, while failed or explicitly uncertain results remain unresolved.
_Avoid_: Multi-Judge vote, consensus classifier

**Search Quota Pool**:
One independently billed search credential whose bounded free allowance may be used for a Search-first Grounded Judge evaluation. It is selected as one unit for a request and is never a Tag identity or classification source.
_Avoid_: API key rotation, shared Provider

**Search-first Shadow Evaluation**:
A non-persisting comparison of a Search-first Grounded Judge against known Tag Categories. It records outcomes and pool usage without activating an AI Classification Record or changing a Human Classification.
_Avoid_: Classification Maintenance, production rollout

**AI Classification Record**:
The complete persisted result from a Grounded Judge: `tag`, `classification`, `explanation`, and `languages`. `languages` stores one primary ISO language code; explanation is the full human-readable basis for review, not separately stored evidence or a second classification.
_Avoid_: Tag Evidence, Judge vote, source record

**Human Classification**:
A Tag Category explicitly chosen by a human reviewer. It permanently overrides an AI Classification Record for that Normalized Tag.
_Avoid_: Temporary override, machine refresh

**Seed Tag**:
A resolved, preference-bearing tag permitted to initiate personalized retrieval. Unresolved and Non-preference Tags are never Seed Tags.
_Avoid_: Any profile tag, matched tag

**Classification Maintenance**:
The periodic process, also available as a manual bulk action from the Review Queue, that asks the Grounded Judge to classify important profile tags. Successful AI Classification Records are accepted directly; failures and explicit uncertainty remain unresolved for human review. Daily recommendation delivery consumes its latest accepted results without waiting for it.
_Avoid_: Daily recommendation run, profile rebuild

**Maintenance Completion**:
The settled outcome of a bounded Classification Maintenance attempt after a Daily Slate has been delivered. It is recorded independently of delivery success.
_Avoid_: Daily Slate completion, delivery rollback

**Provider**:
A configured external service instance, such as a named LLM account, Pixiv, or Danbooru, whose Capability Type determines its credential fields.
_Avoid_: Connection, client, API key

**Capability Type**:
The declared kind of a Provider that determines its valid credentials and available uses, such as LLM, Embedding, Pixiv, or Danbooru.
_Avoid_: Provider name, model

**Model**:
A configured selection of one LLM or Embedding model from a Provider. Product functions reference a Model rather than carrying endpoint or credential details.
_Avoid_: Judge Profile, connection, API model string

**Credential**:
A secret belonging to a Provider, such as an API key or Pixiv token. It is writable through authenticated settings but is never returned in full after storage.
_Avoid_: Provider, endpoint, model
