import unittest
from app import app, limiter
from db import init_db, create_or_update_user, delete_user, get_user_progress
from curriculum import get_curriculum_data


class CurriculumTestCase(unittest.TestCase):
    TEST_USER = "curriculum_tester"
    TEST_PASS = "learnpython123"

    def setUp(self):
        app.config["TESTING"] = True
        limiter.reset()
        self.client = app.test_client()
        with app.app_context():
            init_db()
            delete_user(self.TEST_USER)
            create_or_update_user(self.TEST_USER, self.TEST_PASS, is_superuser=False)

    def tearDown(self):
        limiter.reset()
        with app.app_context():
            delete_user(self.TEST_USER)

    def _login_and_get_csrf(self) -> str:
        res = self.client.post(
            "/login",
            data={"username": self.TEST_USER, "password": self.TEST_PASS},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        with self.client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        self.assertTrue(csrf)
        return csrf

    def test_curriculum_yaml_integrity(self):
        data = get_curriculum_data()
        topics = data["topics"]
        self.assertGreaterEqual(len(topics), 8)

        seen_topic_ids = set()
        seen_quiz_ids = set()
        seen_check_ids = set()

        for t in topics:
            self.assertNotIn(t["id"], seen_topic_ids)
            seen_topic_ids.add(t["id"])
            self.assertTrue(t["video"]["id"])
            self.assertTrue(t["checklist"])

            for item in t["checklist"]:
                self.assertNotIn(item["id"], seen_check_ids)
                seen_check_ids.add(item["id"])

            quiz = t["quiz"]
            self.assertNotIn(quiz["id"], seen_quiz_ids)
            seen_quiz_ids.add(quiz["id"])
            self.assertGreaterEqual(len(quiz["questions"]), 3)

            for q in quiz["questions"]:
                idx = int(q["answer_index"])
                self.assertGreaterEqual(idx, 0)
                self.assertLess(idx, len(q["options"]))

    def test_unauthenticated_blocked_from_curriculum_and_api(self):
        res_topic = self.client.get("/topic/setup-hello")
        self.assertEqual(res_topic.status_code, 302)
        self.assertIn("/login", res_topic.headers["Location"])

        res_quiz = self.client.get("/quiz/quiz-setup-hello")
        self.assertEqual(res_quiz.status_code, 302)
        self.assertIn("/login", res_quiz.headers["Location"])

        res_api = self.client.post("/api/checklist", json={"item_id": "setup_watch_video", "completed": True})
        self.assertEqual(res_api.status_code, 401)
        self.assertFalse(res_api.get_json().get("ok", False))

    def test_roadmap_home_and_topic_detail(self):
        self._login_and_get_csrf()

        home_res = self.client.get("/")
        self.assertEqual(home_res.status_code, 200)
        self.assertIn(b"Setup &amp; Your First Python Program", home_res.data)
        self.assertIn(b"0%", home_res.data)

        topic_res = self.client.get("/topic/setup-hello")
        self.assertEqual(topic_res.status_code, 200)
        self.assertIn(b"youtube-nocookie.com/embed/YYXdXT2l-Gg", topic_res.data)
        self.assertIn(b"hello.py", topic_res.data)

        missing_res = self.client.get("/topic/does-not-exist")
        self.assertEqual(missing_res.status_code, 404)

    def test_checklist_api_csrf_validation_and_persistence(self):
        csrf = self._login_and_get_csrf()

        # 1. Missing CSRF -> 403
        no_csrf = self.client.post(
            "/api/checklist",
            json={"item_id": "setup_watch_video", "completed": True},
        )
        self.assertEqual(no_csrf.status_code, 403)

        # 2. Invalid checklist item -> 400
        bad_item = self.client.post(
            "/api/checklist",
            headers={"X-CSRF-Token": csrf},
            json={"item_id": "nonexistent_item_id", "completed": True},
        )
        self.assertEqual(bad_item.status_code, 400)

        # 3. Valid request -> 200 and persisted in Firestore
        ok_res = self.client.post(
            "/api/checklist",
            headers={"X-CSRF-Token": csrf},
            json={"item_id": "setup_watch_video", "completed": True},
        )
        self.assertEqual(ok_res.status_code, 200)
        self.assertTrue(ok_res.get_json()["ok"])

        prog = get_user_progress(self.TEST_USER)
        self.assertTrue(prog["checklist"].get("setup_watch_video"))

    def test_topic_completion_toggle(self):
        csrf = self._login_and_get_csrf()

        # Missing CSRF -> 403
        no_csrf = self.client.post("/topic/setup-hello/toggle", data={"completed": "true"})
        self.assertEqual(no_csrf.status_code, 403)

        # Valid complete
        res = self.client.post(
            "/topic/setup-hello/toggle",
            data={"csrf_token": csrf, "completed": "true"},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("setup-hello", get_user_progress(self.TEST_USER)["completed_topics"])

        # Reopen topic
        res_reopen = self.client.post(
            "/topic/setup-hello/toggle",
            data={"csrf_token": csrf, "completed": "false"},
            follow_redirects=True,
        )
        self.assertEqual(res_reopen.status_code, 200)
        self.assertNotIn("setup-hello", get_user_progress(self.TEST_USER)["completed_topics"])

    def test_quiz_submission_and_grading(self):
        csrf = self._login_and_get_csrf()

        # 1. Missing CSRF -> 403
        no_csrf = self.client.post("/quiz/quiz-setup-hello", data={"q_q1": "1"})
        self.assertEqual(no_csrf.status_code, 403)

        # 2. Submit failing score (1 out of 4 right)
        fail_res = self.client.post(
            "/quiz/quiz-setup-hello",
            data={"csrf_token": csrf, "q_q1": "1", "q_q2": "0", "q_q3": "0", "q_q4": "0"},
        )
        self.assertEqual(fail_res.status_code, 200)
        self.assertIn(b"1 out of 4", fail_res.data)
        self.assertIn(b"Keep Practicing", fail_res.data)

        prog = get_user_progress(self.TEST_USER)
        self.assertFalse(prog["quiz_scores"]["quiz-setup-hello"]["passed"])
        self.assertEqual(prog["quiz_scores"]["quiz-setup-hello"]["score"], 1)

        # 3. Submit passing score (4 out of 4 right: answers 1, 2, 1, 2)
        pass_res = self.client.post(
            "/quiz/quiz-setup-hello",
            data={"csrf_token": csrf, "q_q1": "1", "q_q2": "2", "q_q3": "1", "q_q4": "2"},
        )
        self.assertEqual(pass_res.status_code, 200)
        self.assertIn(b"4 out of 4", pass_res.data)
        self.assertIn(b"Great Job! You Passed!", pass_res.data)

        prog = get_user_progress(self.TEST_USER)
        self.assertTrue(prog["quiz_scores"]["quiz-setup-hello"]["passed"])
        self.assertEqual(prog["quiz_scores"]["quiz-setup-hello"]["score"], 4)


if __name__ == "__main__":
    unittest.main()
