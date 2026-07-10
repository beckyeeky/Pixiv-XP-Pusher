# Apply diminishing returns to multiple feature matches

When a work matches multiple Feature preferences, the strongest matching Feature contributes in full and additional distinct Feature matches contribute with diminishing weight rather than equal full summation or max-only selection. The initial schedule is simple: the strongest Feature contributes at 100%, the second at 50%, the third at 25%, and later Feature matches do not add more score. This keeps real multi-feature appeal visible without letting tag-dense works dominate ranking purely by stacking many correlated visual traits.
