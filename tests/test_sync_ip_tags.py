import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_ip_tags.py"
SPEC = importlib.util.spec_from_file_location("sync_ip_tags", SCRIPT_PATH)
sync_ip_tags = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_ip_tags)


class SyncIpTagsTests(unittest.TestCase):
    def test_fetch_copyright_tags_sets_user_agent_to_login(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.side_effect = [
            [{"name": "blue_archive"}],
            [],
        ]
        response.raise_for_status = Mock()

        with patch.object(sync_ip_tags.requests, "get", return_value=response) as mock_get:
            tags = sync_ip_tags.fetch_copyright_tags("my_user", "secret")

        self.assertEqual(tags, ["blue_archive"])
        self.assertEqual(mock_get.call_args.kwargs["headers"]["User-Agent"], "my_user")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Accept"], "application/json")

    def test_blocked_response_reports_cloudflare_challenge_clearly(self):
        response = Mock()
        response.status_code = 403
        response.headers = {
            "Cf-Mitigated": "challenge",
            "Content-Type": "text/html; charset=UTF-8",
        }
        response.text = "<html>Just a moment...</html>"

        with self.assertRaisesRegex(RuntimeError, "Cloudflare challenge"):
            sync_ip_tags._raise_for_blocked_or_unexpected_response(response)

    def test_non_json_response_reports_content_type_clearly(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/html; charset=UTF-8"}
        response.text = "<html>blocked</html>"

        with self.assertRaisesRegex(RuntimeError, "Unexpected response content-type"):
            sync_ip_tags._raise_for_blocked_or_unexpected_response(response)


if __name__ == "__main__":
    unittest.main()
