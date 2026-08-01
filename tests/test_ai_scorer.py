import asyncio
import logging
import unittest
from types import SimpleNamespace

from ai_scorer import AIScorer


class _Completions:
    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.content),
            finish_reason=self.finish_reason,
        )])


def _scorer(content, finish_reason="stop"):
    completions = _Completions(content, finish_reason)
    scorer = object.__new__(AIScorer)
    scorer.enabled = True
    scorer.max_candidates = 50
    scorer.model = "fixture-model"
    scorer.PROMPT_TEMPLATE = AIScorer.PROMPT_TEMPLATE
    scorer._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return scorer, completions


def _candidates():
    return [SimpleNamespace(id=1, tags=["tag-a"]), SimpleNamespace(id=2, tags=["tag-b"])]


class AIScorerTests(unittest.TestCase):
    def test_requests_json_object_and_parses_scores_envelope(self):
        scorer, completions = _scorer('{"scores": [{"id": 1, "score": 0.85}]}')

        scores = asyncio.run(scorer.score_candidates(_candidates(), {"tag-a": 1.0}))

        self.assertEqual(scores, {1: 0.85})
        self.assertEqual(completions.requests[0]["response_format"], {"type": "json_object"})
        self.assertEqual(completions.requests[0]["max_tokens"], 3000)

    def test_accepts_legacy_array_in_fenced_json_response(self):
        scorer, _ = _scorer('```json\n[{"id": 2, "score": 0.4}]\n```')

        scores = asyncio.run(scorer.score_candidates(_candidates(), {"tag-a": 1.0}))

        self.assertEqual(scores, {2: 0.4})

    def test_empty_response_logs_actionable_diagnostics_and_falls_back(self):
        scorer, _ = _scorer("   ", finish_reason="stop")

        with self.assertLogs("ai_scorer", logging.ERROR) as logs:
            scores = asyncio.run(scorer.score_candidates(_candidates(), {"tag-a": 1.0}))

        self.assertEqual(scores, {})
        self.assertIn("AI 评分响应无效", logs.output[0])
        self.assertIn("content_length=0", logs.output[0])
        self.assertIn("finish_reason=stop", logs.output[0])

    def test_non_json_response_logs_excerpt_and_falls_back(self):
        scorer, _ = _scorer("评分结果如下：候选 1 更符合偏好", finish_reason="length")

        with self.assertLogs("ai_scorer", logging.ERROR) as logs:
            scores = asyncio.run(scorer.score_candidates(_candidates(), {"tag-a": 1.0}))

        self.assertEqual(scores, {})
        self.assertIn("content_length=17", logs.output[0])
        self.assertIn("content_excerpt='评分结果如下：候选 1 更符合偏好'", logs.output[0])
        self.assertIn("finish_reason=length", logs.output[0])

    def test_rejects_json_object_without_scores_array(self):
        scorer, _ = _scorer('{"score": 0.85}')

        with self.assertLogs("ai_scorer", logging.ERROR) as logs:
            scores = asyncio.run(scorer.score_candidates(_candidates(), {"tag-a": 1.0}))

        self.assertEqual(scores, {})
        self.assertIn("field 'scores' must be an array", logs.output[0])
