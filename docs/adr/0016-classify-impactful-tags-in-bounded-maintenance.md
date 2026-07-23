# Classify impactful tags in bounded maintenance

Production tag classification uses bounded Classification Maintenance over tags with Maintenance Eligibility, ordered by absolute Preference Profile weight. New tags are not searched or classified on arrival; they wait until their impact reaches the configured threshold or a human explicitly selects them. This favors early usefulness for recommendation-driving tags while containing external search cost and leaving low-quality long-tail Pixiv tags conservatively unresolved.
