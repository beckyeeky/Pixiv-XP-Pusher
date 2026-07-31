# Persist bounded Grounding provenance on the AI Classification Record

Search-first Classification Maintenance persists review provenance on the same AI Classification Record as the Grounded Judge decision. The optional versioned JSON value contains the classifier Model, selected Search Provider and pool, at most five source URLs, at most three short evidence excerpts, a redacted Search trace, and whitelisted usage counters.

This provenance is diagnostic context, not a second evidence or classification model. It cannot activate a Tag Category independently, does not change the one-Judge decision path, and remains subordinate to the permanent Human Classification override. Provider errors, credentials, raw responses, and unbounded source content are not persisted.

Existing records migrate with empty provenance. A later successful AI classification replaces the record and its provenance atomically.
