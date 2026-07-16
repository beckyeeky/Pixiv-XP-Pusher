"""Normalized Tag identity resolution and untrusted mapping proposals.

Runtime callers resolve identities only through accepted aliases.  Candidate
generators return proposals and deliberately have no database dependency, so a
model response cannot activate an alias or rewrite a Preference Profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from utils import normalize_tag

try:
    from openai import AsyncOpenAI

    HAS_OPENAI = True
except ImportError:  # pragma: no cover - optional dependency
    AsyncOpenAI = None
    HAS_OPENAI = False


TAG_ALIAS_EQUIVALENT = "equivalent"
TAG_ALIAS_SEARCH = "search"
TAG_ALIAS_KINDS = {TAG_ALIAS_EQUIVALENT, TAG_ALIAS_SEARCH}


def would_create_alias_cycle(aliases: dict[str, str], original_tag: str, target_tag: str) -> bool:
    """Return whether adding original -> target would create an identity cycle."""
    original = normalize_tag(original_tag)
    current = normalize_tag(target_tag)
    seen: set[str] = set()
    normalized_aliases = {
        normalize_tag(source): normalize_tag(target)
        for source, target in aliases.items()
    }
    while current in normalized_aliases and current not in seen:
        if current == original:
            return True
        seen.add(current)
        current = normalized_aliases[current]
    return current == original


@dataclass(frozen=True)
class TagMappingCandidate:
    """An untrusted proposal which cannot affect runtime identity by itself."""

    original_tag: str
    proposed_normalized_tag: str
    kind: str = TAG_ALIAS_EQUIVALENT
    explanation: str = ""
    source: str = "ai_candidate"


class TagIdentityResolver:
    """Resolve raw spellings through deterministic rules and accepted aliases."""

    def __init__(self, accepted_aliases: dict[str, str] | None = None):
        self._aliases = {
            normalize_tag(original): normalize_tag(normalized)
            for original, normalized in (accepted_aliases or {}).items()
            if normalize_tag(original) and normalize_tag(normalized)
        }

    def resolve(self, tag: str) -> str:
        current = normalize_tag(tag)
        seen: set[str] = set()
        while current in self._aliases and current not in seen:
            seen.add(current)
            current = self._aliases[current]
        return current

    def resolve_many(self, tags: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(resolved for tag in tags if (resolved := self.resolve(tag))))


class AITagMappingCandidateGenerator:
    """LLM adapter that proposes exact aliases without persisting or activating them."""

    def __init__(self, config: dict):
        self.enabled = bool(config.get("enabled", False)) and HAS_OPENAI
        self.model = str(config.get("model") or "gpt-4o-mini")
        self.batch_size = max(1, int(config.get("batch_size", 50)))
        self.client = None
        if self.enabled:
            self.client = AsyncOpenAI(
                api_key=str(config.get("api_key") or ""),
                base_url=str(config.get("base_url") or "") or None,
            )

    async def propose(self, tags: list[str]) -> list[TagMappingCandidate]:
        """Return review candidates; uncertain tags must be omitted by the model."""
        if not self.enabled or not self.client or not tags:
            return []

        proposals: list[TagMappingCandidate] = []
        unique_tags = list(dict.fromkeys(normalize_tag(tag) for tag in tags if normalize_tag(tag)))
        for start in range(0, len(unique_tags), self.batch_size):
            proposals.extend(await self._propose_batch(unique_tags[start:start + self.batch_size]))
        return proposals

    async def _propose_batch(self, tags: list[str]) -> list[TagMappingCandidate]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You propose Pixiv tag aliases for human review. Only propose exact identity "
                        "equivalence across spelling, language, or established canonical naming. "
                        "Do not merge related concepts, broader/narrower concepts, costumes with base "
                        "characters, or classify tags as meaningless. Omit uncertain tags. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "For these tags, return {\"candidates\":[{\"original_tag\":str,"
                        "\"proposed_normalized_tag\":str,\"kind\":\"equivalent\","
                        "\"explanation\":str}]}. Candidate targets use lowercase snake_case.\n"
                        + json.dumps(tags, ensure_ascii=False)
                    ),
                },
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)
        allowed_inputs = set(tags)
        candidates: list[TagMappingCandidate] = []
        for item in payload.get("candidates", []):
            if not isinstance(item, dict):
                continue
            original = normalize_tag(str(item.get("original_tag") or ""))
            target = normalize_tag(str(item.get("proposed_normalized_tag") or ""))
            if original not in allowed_inputs or not target or original == target:
                continue
            candidates.append(TagMappingCandidate(
                original_tag=original,
                proposed_normalized_tag=target,
                kind=TAG_ALIAS_EQUIVALENT,
                explanation=str(item.get("explanation") or "").strip(),
            ))
        return candidates
