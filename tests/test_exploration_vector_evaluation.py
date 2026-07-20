import unittest

from exploration_vector_evaluation import evaluate_vector_exploration_rows


class VectorExplorationEvaluationTests(unittest.TestCase):
    def test_reports_all_four_acceptance_metric_families(self):
        report = evaluate_vector_exploration_rows(
            [{"status": "completed", "profile_concentration": 0.20,
              "slate_profile_concentration": 0.25,
              "duplicate_semantic_rate": 0.10}],
            [
                {"selected": 1, "action": "like", "retrieval_rank": 1, "final_rank": 3},
                {"selected": 1, "action": "dislike", "retrieval_rank": 3, "final_rank": 2},
                {"selected": 0, "action": None, "retrieval_rank": 2, "final_rank": None},
            ],
        )
        self.assertEqual(report.selected_count, 2)
        self.assertEqual((report.likes, report.dislikes), (1, 1))
        self.assertEqual(report.feedback_rate, 1.0)
        self.assertEqual(report.like_rate, 0.5)
        self.assertEqual(report.mean_signed_rank_movement, 0.5)
        self.assertEqual(report.mean_absolute_rank_movement, 1.5)
        self.assertEqual(report.mean_profile_concentration, 0.20)
        self.assertEqual(report.mean_slate_profile_concentration, 0.25)
        self.assertEqual(report.mean_duplicate_semantic_rate, 0.10)
