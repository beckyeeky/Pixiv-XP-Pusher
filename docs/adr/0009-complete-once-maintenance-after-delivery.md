# Complete bounded maintenance after once-mode delivery

In `--once` mode, a successfully sent Daily Slate completes delivery first, then the process waits up to ninety seconds for its bounded Classification Maintenance attempt before closing shared clients. Maintenance has an independent success outcome, so failure or timeout is recorded without invalidating delivery; in scheduler mode, maintenance remains single-instance background work and delivery neither waits for it nor starts a duplicate attempt when one is already running.
