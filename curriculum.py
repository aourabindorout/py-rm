import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml

CURRICULUM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "curriculum.yaml")


@lru_cache(maxsize=1)
def get_curriculum_data() -> Dict[str, Any]:
    """Load and cache the curriculum structure from curriculum.yaml."""
    with open(CURRICULUM_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    topics = sorted(data.get("topics", []), key=lambda t: t.get("order", 0))
    return {
        "title": data.get("title", "Python Roadmap"),
        "subtitle": data.get("subtitle", ""),
        "pass_threshold_percent": int(data.get("pass_threshold_percent", 75)),
        "topics": topics,
    }


def get_topics() -> List[Dict[str, Any]]:
    """Return ordered list of all curriculum topics."""
    return get_curriculum_data()["topics"]


@lru_cache(maxsize=1)
def _valid_checklist_ids() -> Set[str]:
    """Build a cached set of every valid checklist item ID defined in curriculum.yaml."""
    ids: Set[str] = set()
    for topic in get_topics():
        for item in topic.get("checklist", []):
            if item.get("id"):
                ids.add(item["id"])
    return ids


def is_valid_checklist_item(item_id: str) -> bool:
    """Return True if item_id exists in the loaded curriculum."""
    return item_id in _valid_checklist_ids()


def get_topic(topic_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (topic, prev_topic, next_topic) for a topic ID, or (None, None, None) if missing."""
    topics = get_topics()
    for idx, topic in enumerate(topics):
        if topic.get("id") == topic_id:
            prev_topic = topics[idx - 1] if idx > 0 else None
            next_topic = topics[idx + 1] if idx + 1 < len(topics) else None
            return topic, prev_topic, next_topic
    return None, None, None


def get_quiz(quiz_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (parent_topic, quiz) for a quiz ID, or (None, None) if missing."""
    for topic in get_topics():
        quiz = topic.get("quiz")
        if quiz and quiz.get("id") == quiz_id:
            return topic, quiz
    return None, None


def grade_quiz(quiz: Dict[str, Any], form_data: Dict[str, str]) -> Dict[str, Any]:
    """
    Grade submitted answers against a quiz definition.
    form_data maps 'q_<question_id>' -> selected option index string.
    """
    questions = quiz.get("questions", [])
    threshold = get_curriculum_data()["pass_threshold_percent"]

    score = 0
    details = []
    for q in questions:
        qid = q["id"]
        raw_choice = form_data.get(f"q_{qid}")
        try:
            selected_index = int(raw_choice) if raw_choice is not None else None
        except ValueError:
            selected_index = None

        correct_index = int(q["answer_index"])
        is_correct = selected_index == correct_index
        if is_correct:
            score += 1

        details.append(
            {
                "id": qid,
                "prompt": q["prompt"],
                "options": q["options"],
                "selected_index": selected_index,
                "correct_index": correct_index,
                "is_correct": is_correct,
                "explanation": q.get("explanation", ""),
            }
        )

    total = len(questions)
    percent = round((score / total) * 100) if total else 0
    passed = percent >= threshold

    return {
        "score": score,
        "total": total,
        "percent": percent,
        "passed": passed,
        "threshold": threshold,
        "details": details,
    }


def compute_progress_summary(progress: Dict[str, Any]) -> Dict[str, Any]:
    """Compute overall dashboard metrics and enriched card metadata for every topic."""
    topics = get_topics()
    completed_set = set(progress.get("completed_topics", []))
    checklist_map = progress.get("checklist", {})
    quiz_map = progress.get("quiz_scores", {})

    total_topics = len(topics)
    completed_topics_count = 0
    total_checklist = 0
    completed_checklist = 0
    total_quizzes = 0
    passed_quizzes = 0
    next_topic = None
    topic_cards = []

    for topic in topics:
        tid = topic["id"]
        is_completed = tid in completed_set
        if is_completed:
            completed_topics_count += 1
        elif next_topic is None:
            next_topic = topic

        items = topic.get("checklist", [])
        t_total_checks = len(items)
        t_done_checks = sum(1 for item in items if checklist_map.get(item["id"]))
        total_checklist += t_total_checks
        completed_checklist += t_done_checks

        quiz = topic.get("quiz")
        quiz_result = None
        if quiz:
            total_quizzes += 1
            quiz_result = quiz_map.get(quiz["id"])
            if quiz_result and quiz_result.get("passed"):
                passed_quizzes += 1

        topic_cards.append(
            {
                "topic": topic,
                "is_completed": is_completed,
                "checklist_done": t_done_checks,
                "checklist_total": t_total_checks,
                "quiz_result": quiz_result,
            }
        )

    # Calculate overall completion percentage (weighted across topics, tasks, and quizzes)
    total_units = total_topics + total_checklist + total_quizzes
    done_units = completed_topics_count + completed_checklist + passed_quizzes
    overall_percent = round((done_units / total_units) * 100) if total_units else 0

    return {
        "total_topics": total_topics,
        "completed_topics_count": completed_topics_count,
        "total_checklist": total_checklist,
        "completed_checklist": completed_checklist,
        "total_quizzes": total_quizzes,
        "passed_quizzes": passed_quizzes,
        "overall_percent": overall_percent,
        "next_topic": next_topic,
        "topic_cards": topic_cards,
    }
