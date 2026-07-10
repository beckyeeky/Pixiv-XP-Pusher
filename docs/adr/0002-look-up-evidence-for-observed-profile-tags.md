# Look up evidence for observed profile tags

External tag evidence is collected only for tags important enough to enter the current profile classification scope, then persisted and refreshed incrementally. We will not mirror the full Danbooru taxonomy: an observed-tag lookup keeps the dataset aligned with actual recommendation needs, limits external requests and storage, and still allows cached evidence to be reused when Danbooru is unavailable.
