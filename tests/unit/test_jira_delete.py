from __future__ import annotations

from typing import Any

from jira.exceptions import JIRAError
from tools.jira_tools import create_jira_ticket as jira_module


class FakeIssue:
    def __init__(self) -> None:
        self.deleted = False

    def delete(self) -> None:
        self.deleted = True


class FakeJira:
    def __init__(self, issue: FakeIssue | None = None, error: Exception | None = None) -> None:
        self.fake_issue = issue or FakeIssue()
        self.error = error
        self.requested_key: str | None = None

    def issue(self, issue_key: str) -> FakeIssue:
        self.requested_key = issue_key
        if self.error:
            raise self.error
        return self.fake_issue


def _configure_jira(monkeypatch: Any) -> None:
    monkeypatch.setenv("JIRA_SERVER", "https://jira.example.test")
    monkeypatch.setenv("JIRA_EMAIL", "agent@example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")


def test_delete_jira_ticket_deletes_url_issue_key(monkeypatch: Any) -> None:
    _configure_jira(monkeypatch)
    fake_jira = FakeJira()
    monkeypatch.setattr(jira_module, "JIRA", lambda **_: fake_jira)

    result = jira_module.delete_jira_ticket(
        "https://jira.example.test/browse/dev-1205?focusedCommentId=1"
    )

    assert result["status"] == "success"
    assert fake_jira.requested_key == "DEV-1205"
    assert fake_jira.fake_issue.deleted


def test_delete_jira_ticket_reports_missing_permission_without_soft_delete(
    monkeypatch: Any,
) -> None:
    _configure_jira(monkeypatch)
    fake_jira = FakeJira(error=JIRAError(status_code=403, text="Delete permission denied"))
    monkeypatch.setattr(jira_module, "JIRA", lambda **_: fake_jira)
    monkeypatch.setattr(
        jira_module,
        "add_label_to_ticket",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not soft-delete the issue")),
    )

    result = jira_module.delete_jira_ticket("DEV-1205")

    assert result["status"] == "error"
    assert "Delete Issues permission" in result["message"]
