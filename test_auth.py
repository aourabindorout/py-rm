import unittest
from app import app, limiter
from db import init_db, create_or_update_user


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        limiter.reset()
        self.client = app.test_client()
        with app.app_context():
            init_db()
            create_or_update_user("tester", "mypassword123")

    def tearDown(self):
        limiter.reset()

    def test_unauthenticated_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_healthz(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")

    def test_login_failed(self):
        response = self.client.post(
            "/login",
            data={"username": "tester", "password": "wrongpassword"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)

    def test_login_success_and_home_display(self):
        response = self.client.post(
            "/login",
            data={"username": "tester", "password": "mypassword123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Hello!", response.data)
        self.assertIn(b"tester", response.data)

    def test_logout(self):
        # Log in first
        self.client.post(
            "/login",
            data={"username": "tester", "password": "mypassword123"},
            follow_redirects=True,
        )
        # Log out
        response = self.client.post("/logout", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You have been logged out", response.data)

        # Accessing / again should redirect
        res_after = self.client.get("/")
        self.assertEqual(res_after.status_code, 302)

    def test_rate_limit_per_ip_get(self):
        # Limit is 3 per minute per IP
        for _ in range(3):
            res = self.client.get("/login", environ_base={"REMOTE_ADDR": "10.0.0.1"})
            self.assertEqual(res.status_code, 200)

        # 4th should be 429
        res = self.client.get("/login", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        self.assertEqual(res.status_code, 429)
        self.assertIn(b"Too Many Requests", res.data)

    def test_rate_limit_per_ip_post(self):
        # Limit is 3 per minute per IP
        for _ in range(3):
            res = self.client.post(
                "/login",
                data={"username": "tester", "password": "bad"},
                environ_base={"REMOTE_ADDR": "10.0.0.2"},
            )
            self.assertEqual(res.status_code, 302)

        # 4th should be 429
        res = self.client.post(
            "/login",
            data={"username": "tester", "password": "bad"},
            environ_base={"REMOTE_ADDR": "10.0.0.2"},
        )
        self.assertEqual(res.status_code, 429)
        self.assertIn(b"Too Many Requests", res.data)

    def test_rate_limit_global_post(self):
        # Limit is 15 overall per minute
        for i in range(15):
            res = self.client.post(
                "/login",
                data={"username": "tester", "password": "bad"},
                environ_base={"REMOTE_ADDR": f"192.168.1.{i+1}"},
            )
            self.assertEqual(res.status_code, 302)

        # 16th across different IP should hit global limit 429
        res = self.client.post(
            "/login",
            data={"username": "tester", "password": "bad"},
            environ_base={"REMOTE_ADDR": "192.168.1.99"},
        )
        self.assertEqual(res.status_code, 429)


if __name__ == "__main__":
    unittest.main()
