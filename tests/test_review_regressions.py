"""HTTP regressions for the three findings in the PR review."""

from test_acceptance import ServerTestCase


class TestReviewRegressions(ServerTestCase):
    def test_unicode_url_redirects_and_is_persisted_encoded(self):
        cases = (
            ("emoji", "https://example.com/😀", "https://example.com/%F0%9F%98%80"),
            ("idn", "https://例え.jp/😀", "https://xn--r8jz45g.jp/%F0%9F%98%80"),
            (
                None,
                "https://example.com/café%20a?q=ação&x=1#😀",
                "https://example.com/caf%C3%A9%20a?q=a%C3%A7%C3%A3o&x=1#%F0%9F%98%80",
            ),
        )
        for alias, url, expected in cases:
            with self.subTest(url=url):
                created = self.create(url=url, alias=alias)
                self.assertEqual(created.status, 201)
                alias = created.json()["alias"]
                # Exercise the actual header writer before checking serialization.
                for method in ("HEAD", "GET"):
                    response = self.server.request(method, "/" + alias)
                    self.assertEqual(response.status, 302)
                    self.assertEqual(response.headers["Location"], expected)
                    self.assertEqual(response.body, b"")
                self.assertEqual(created.json()["url"], expected)
                stats = self.server.request("GET", "/api/links/" + alias + "/stats")
                self.assertEqual(stats.json()["url"], expected)
                self.assertEqual(stats.json()["total_clicks"], 1)
                links = self.server.request("GET", "/api/links").json()["links"]
                self.assertEqual(next(x for x in links if x["alias"] == alias)["url"], expected)

    def test_alias_with_trailing_newline_is_rejected(self):
        response = self.create(alias="abc\n")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json(), {"error": "invalid_alias"})
        links = self.server.request("GET", "/api/links").json()["links"]
        self.assertNotIn("abc\n", [link["alias"] for link in links])

    def test_malformed_url_returns_invalid_url(self):
        for url in ("http://[", "http://[not-ipv6]/", "http://example.com\uff0fpath"):
            with self.subTest(url=url):
                response = self.create(url=url, alias="malformed")
                self.assertEqual(response.status, 400)
                self.assertEqual(response.json(), {"error": "invalid_url"})
        links = self.server.request("GET", "/api/links").json()["links"]
        self.assertNotIn("malformed", [link["alias"] for link in links])
