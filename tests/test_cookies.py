"""谷歌 cookie 解析单测：形状、过滤、CDP 列表、坏值。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cookies import (  # noqa: E402
    COOKIE_NAMES,
    cookie_header,
    google_cookies,
    host_is_google,
    parse_cookie_string,
)


class ParseTest(unittest.TestCase):
    def test_name_value_string(self):
        pairs = parse_cookie_string("SID=abc; SOCS=CAI; AEC=xyz")
        self.assertEqual(pairs, {"SID": "abc", "SOCS": "CAI", "AEC": "xyz"})

    def test_multiline_and_quotes(self):
        pairs = parse_cookie_string('SID="abc"\nSOCS=CAI\n')
        self.assertEqual(pairs["SID"], "abc")
        self.assertEqual(pairs["SOCS"], "CAI")

    def test_chrome_tab_row(self):
        row = "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1800000000\tSID\tabc123"
        self.assertEqual(parse_cookie_string(row), {"SID": "abc123"})

    def test_empty(self):
        self.assertEqual(parse_cookie_string(""), {})
        self.assertEqual(parse_cookie_string("   "), {})

    def test_skips_garbage_chunk(self):
        self.assertEqual(parse_cookie_string("novalue; A=b"), {"A": "b"})


class GoogleCookiesTest(unittest.TestCase):
    def test_only_known_and_google(self):
        raw = "SID=abc; SOCS=CAI; NID=xyz; otherCookie=1"
        names = [item["name"] for item in google_cookies(raw)]
        self.assertIn("SID", names)
        self.assertIn("SOCS", names)
        self.assertIn("NID", names)
        self.assertNotIn("otherCookie", names)

    def test_secure_cookie_shape(self):
        cookies = {item["name"]: item for item in google_cookies("__Secure-1PSID=abc; __Host-GAPS=zzz")}
        self.assertTrue(cookies["__Secure-1PSID"]["secure"])
        self.assertTrue(cookies["__Secure-1PSID"]["domain"].endswith("google.com"))
        # __Host- 前缀不允许带 domain
        self.assertNotIn("domain", cookies["__Host-GAPS"])

    def test_all_names_whitelisted(self):
        for item in google_cookies("SID=a; SOCS=b; NID=c; AEC=d; DV=e"):
            self.assertIn(item["name"], COOKIE_NAMES)

    def test_binary_value_dropped(self):
        self.assertNotIn("SID", [item["name"] for item in google_cookies("SID=a\x00b")])

    def test_foreign_domain_rejected(self):
        self.assertEqual(google_cookies("SID=a", domain=".evil.com"), [])


class HeaderTest(unittest.TestCase):
    def test_header_only_known(self):
        header = cookie_header("SID=abc; SOCS=CAI; junk=1")
        self.assertIn("SID=abc", header)
        self.assertIn("SOCS=CAI", header)
        self.assertNotIn("junk", header)

    def test_header_empty(self):
        self.assertEqual(cookie_header(""), "")

    def test_host_is_google(self):
        self.assertTrue(host_is_google("https://www.google.com/search"))
        self.assertTrue(host_is_google("https://lens.google.com/x"))
        self.assertFalse(host_is_google("https://example.com/"))
        self.assertFalse(host_is_google("not a url"))


if __name__ == "__main__":
    unittest.main()
