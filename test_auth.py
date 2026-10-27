import time
import unittest
from app import app, limiter
from db import (
    init_db,
    create_or_update_user,
    get_connection,
    INACTIVITY_TIMEOUT_SECONDS,
)


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

    def test_logout_invalidates_server_session(self):
        # 1. Log in
        self.client.post(
            "/login",
            data={"username": "tester", "password": "mypassword123"},
            follow_redirects=True,
        )

        # 2. Extract cookie before logout (simulating friend capturing the session)
        session_cookie = self.client.get_cookie("session")
        self.assertIsNotNone(session_cookie)

        # 3. Legitimate user logs out
        res = self.client.post("/logout", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"You have been logged out", res.data)

        # 4. Attacker injects the copied cookie into a new client
        attacker_client = app.test_client()
        attacker_client.set_cookie("session", session_cookie.value)

        # 5. Attacker tries to access protected home page
        replay_response = attacker_client.get("/", follow_redirects=True)
        self.assertEqual(replay_response.status_code, 200)
        # Should be redirected to /login with session expired warning
        self.assertIn(b"Your session has expired or was terminated", replay_response.data)

    def test_inactivity_timeout_after_10_minutes(self):
        # 1. Log in
        self.client.post(
            "/login",
            data={"username": "tester", "password": "mypassword123"},
            follow_redirects=True,
        )

        # 2. Verify access works immediately
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)

        # 3. Simulate 11 minutes (660 seconds) of inactivity by aging last_active_at in DB
        with get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET last_active_at = last_active_at - ?",
                (INACTIVITY_TIMEOUT_SECONDS + 60,),
            )
            conn.commit()

        # 4. Attempt to access / after inactivity
        expired_res = self.client.get("/", follow_redirects=True)
        self.assertEqual(expired_res.status_code, 200)
        self.assertIn(b"Your session has expired or was terminated", expired_res.data)

    def test_user_agent_mismatch_blocks_replay(self):
        # 1. Log in with Chrome User-Agent
        ua_original = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
        self.client.post(
            "/login",
            data={"username": "tester", "password": "mypassword123"},
            headers={"User-Agent": ua_original},
            follow_redirects=True,
        )

        # 2. Save cookie
        session_cookie = self.client.get_cookie("session")
        self.assertIsNotNone(session_cookie)

        # 3. Attacker uses cookie on a different device/browser (e.g. Firefox on Mac)
        attacker = app.test_client()
        attacker.set_cookie("session", session_cookie.value)

        ua_attacker = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/119.0"
        res = attacker.get("/", headers={"User-Agent": ua_attacker}, follow_redirects=True)
        self.assertIn(b"Your session has expired or was terminated", res.data)

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
                environ_base={"REMOTE_ADDR": f"192.168.2.{i+1}"},
            )
            self.assertEqual(res.status_code, 302)

        # 16th across different IP should hit global limit 429
        res = self.client.post(
            "/login",
            data={"username": "tester", "password": "bad"},
            environ_base={"REMOTE_ADDR": "192.168.2.99"},
        )
        self.assertEqual(res.status_code, 429)


if __name__ == "__main__":
    unittest.main()
