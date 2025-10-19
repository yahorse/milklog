import importlib
import os
import sys
import tempfile
import unittest


class MilkLogAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls._tmpdir.name
        sys.modules.pop("app", None)
        cls.app_module = importlib.import_module("app")
        cls.app_module.app.config["TESTING"] = True
        cls.admin_email = "admin@example.com"
        cls.admin_password = "Password123!"
        with cls.app_module.app.test_client() as client:
            client.post(
                "/register",
                data={"email": cls.admin_email, "password": cls.admin_password},
                follow_redirects=True,
            )
            client.post(
                "/add",
                data={
                    "cow_number": "101",
                    "litres": "12.0",
                    "session": "AM",
                    "record_date": "2025-01-01",
                    "price_per_litre": "0.50",
                },
                follow_redirects=True,
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        self.client = self.app_module.app.test_client()
        response = self.client.post(
            "/login",
            data={"email": self.admin_email, "password": self.admin_password},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Milk Log", response.get_data(as_text=True))

    def test_recent_shows_price_and_gain(self):
        response = self.client.get("/recent")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Price/L", page)
        self.assertIn("Gain", page)
        self.assertIn("0.50", page)
        self.assertIn("6.00", page)

    def test_navigation_routes_load(self):
        routes = [
            ("/", None),
            ("/new", "TPL_NEW"),
            ("/records", "TPL_RECORDS"),
            ("/recent", "TPL_RECENT"),
            ("/bulk", "TPL_BULK"),
            ("/import", "TPL_IMPORT"),
            ("/cows", "TPL_COWS"),
            ("/health", "TPL_HEALTH"),
            ("/breeding", "TPL_BREEDING"),
            ("/alerts", "TPL_ALERTS"),
            ("/admin", None),
        ]
        urls = [url for url, attr in routes if attr is None or hasattr(self.app_module, attr)]
        self.assertTrue(urls, "No navigation routes available for testing")
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_add_record_flow(self):
        resp = self.client.post(
            "/add",
            data={
                "cow_number": "202",
                "litres": "9.10",
                "session": "PM",
                "record_date": "2025-02-02",
                "price_per_litre": "0.73",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        recent = self.client.get("/recent")
        self.assertEqual(recent.status_code, 200)
        html = recent.get_data(as_text=True)
        self.assertIn("202", html)
        self.assertIn("0.73", html)
        self.assertIn("6.64", html)

    def test_export_endpoints(self):
        csv_resp = self.client.get("/export.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("text/csv", csv_resp.headers.get("Content-Type", ""))

        xlsx_resp = self.client.get("/export.xlsx")
        self.assertEqual(xlsx_resp.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            xlsx_resp.headers.get("Content-Type", ""),
        )

    def test_health_and_manifest_endpoints(self):
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_data(as_text=True), "ok")

        manifest = self.client.get("/manifest.json")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("Milk Log", manifest.get_data(as_text=True))

        sw = self.client.get("/sw.js")
        self.assertEqual(sw.status_code, 200)
        script = sw.get_data(as_text=True)
        self.assertIn("milklog-v6", script)
        self.assertIn("skipWaiting", script)
        self.assertIn("clients.claim", script)


if __name__ == "__main__":
    unittest.main()
