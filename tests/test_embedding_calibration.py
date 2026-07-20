import unittest

from embedding_calibration import CalibrationSample, evaluate_embedding_weights


class EmbeddingCalibrationTests(unittest.TestCase):
    def test_useful_semantic_signal_recommends_the_strongest_candidate(self):
        samples = [
            CalibrationSample(1, "like", 0.4, 0.9),
            CalibrationSample(2, "like", 0.6, 0.8),
            CalibrationSample(3, "dislike", 0.7, 0.1),
            CalibrationSample(4, "dislike", 0.5, 0.2),
        ]
        report = evaluate_embedding_weights(
            samples, [0, 0.5, 1], current_weight=0.3, min_samples=4, min_per_class=2,
        )

        self.assertTrue(report["sufficient"])
        self.assertEqual(report["recommended_weight"], 1.0)
        self.assertEqual(report["evaluations"][-1]["auc"], 1.0)

    def test_harmful_semantic_signal_recommends_tag_only(self):
        samples = [
            CalibrationSample(1, "like", 0.9, 0.1),
            CalibrationSample(2, "like", 0.8, 0.2),
            CalibrationSample(3, "dislike", 0.2, 0.8),
            CalibrationSample(4, "dislike", 0.1, 0.9),
        ]
        report = evaluate_embedding_weights(
            samples, [0, 0.5, 1], current_weight=0.3, min_samples=4, min_per_class=2,
        )

        self.assertEqual(report["recommended_weight"], 0.0)

    def test_insufficient_class_balance_refuses_to_guess(self):
        report = evaluate_embedding_weights(
            [CalibrationSample(1, "like", 0.8, 0.9)],
            [0, 0.3], current_weight=0.3, total_feedback=3,
            missing={"missing_work_embedding": 2}, min_samples=1, min_per_class=1,
        )

        self.assertFalse(report["sufficient"])
        self.assertIsNone(report["recommended_weight"])
        self.assertIn("dislike 样本", report["reasons"][0])
        self.assertEqual(report["sample_counts"]["missing"], 2)

    def test_equal_metrics_prefer_the_current_weight_deterministically(self):
        samples = [
            CalibrationSample(1, "like", 0.8, 0.8),
            CalibrationSample(2, "dislike", 0.2, 0.2),
        ]
        report = evaluate_embedding_weights(
            samples, [0, 0.1, 0.5], current_weight=0.3, min_samples=2, min_per_class=1,
        )

        self.assertEqual(report["recommended_weight"], 0.3)


if __name__ == "__main__":
    unittest.main()
