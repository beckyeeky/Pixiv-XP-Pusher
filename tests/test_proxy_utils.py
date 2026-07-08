import unittest

from proxy_utils import normalize_proxy_url


class ProxyNormalizationTests(unittest.TestCase):
    def test_normalize_proxy_url_treats_none_like_empty(self):
        self.assertIsNone(normalize_proxy_url(None))
        self.assertIsNone(normalize_proxy_url(""))
        self.assertIsNone(normalize_proxy_url("   "))
        self.assertIsNone(normalize_proxy_url("None"))
        self.assertIsNone(normalize_proxy_url(" none "))

    def test_normalize_proxy_url_adds_http_scheme_when_missing(self):
        self.assertEqual(normalize_proxy_url("127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_normalize_proxy_url_keeps_existing_supported_scheme(self):
        self.assertEqual(normalize_proxy_url("http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(normalize_proxy_url("https://127.0.0.1:7890"), "https://127.0.0.1:7890")
        self.assertEqual(normalize_proxy_url("socks5://127.0.0.1:7890"), "socks5://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
