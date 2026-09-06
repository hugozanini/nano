"""Deduplication, list mutation, and task dispatch.

The dedupe tests are the ones that matter. In production Meta delivered one
receipt seven times over 45 minutes and produced seven purchases; two
independent layers now prevent that, and each is tested on its own so neither
can quietly stop working behind the other.
"""

from __future__ import annotations

import pytest
from nano import store, tasks
from nano.config import load_settings

from tests.fakes.firestore import FakeFirestoreClient


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_firestore: FakeFirestoreClient):
    monkeypatch.setattr(store, "firestore_client", lambda: fake_firestore)
    return fake_firestore


class TestClaimMessage:
    def test_first_claim_wins(self, wired, settings):
        assert store.claim_message("wamid.A", settings) is True

    def test_second_claim_loses(self, wired, settings):
        store.claim_message("wamid.A", settings)
        assert store.claim_message("wamid.A", settings) is False

    def test_seven_deliveries_yield_one_claim(self, wired, settings):
        """The exact production multiplicity."""
        results = [store.claim_message("wamid.RECEIPT", settings) for _ in range(7)]
        assert results.count(True) == 1
        assert results.count(False) == 6

    def test_distinct_messages_each_claim(self, wired, settings):
        assert store.claim_message("wamid.A", settings) is True
        assert store.claim_message("wamid.B", settings) is True

    def test_claims_are_namespaced_by_prefix(self, wired, settings):
        store.claim_message("wamid.A", settings)
        assert any("nano_test_processed_messages" in path for path in wired.store)

    def test_claim_records_an_expiry_for_the_ttl_policy(self, wired, settings):
        store.claim_message("wamid.A", settings)
        doc = next(v for k, v in wired.store.items() if "processed_messages" in k)
        assert "expires_at" in doc and "claimed_at" in doc

    def test_released_claim_can_be_retaken(self, wired, settings):
        """A failure before anything durable must not swallow the message."""
        store.claim_message("wamid.A", settings)
        store.release_claim("wamid.A", settings)
        assert store.claim_message("wamid.A", settings) is True

    def test_firestore_failure_errs_toward_processing(self, monkeypatch, settings):
        """Processing twice beats dropping silently; downstream ids still dedupe."""

        class Exploding:
            def collection(self, *_):
                raise RuntimeError("firestore down")

        monkeypatch.setattr(store, "firestore_client", lambda: Exploding())
        assert store.claim_message("wamid.A", settings) is True


class TestListMutations:
    def test_add_items(self, wired, settings):
        assert store.add_items(["coffee", "milk"], settings) == ["coffee", "milk"]

    def test_add_skips_case_insensitive_duplicates(self, wired, settings):
        store.add_items(["Coffee"], settings)
        assert store.add_items(["coffee", "milk"], settings) == ["Coffee", "milk"]

    def test_add_deduplicates_within_one_call(self, wired, settings):
        assert store.add_items(["coffee", "Coffee"], settings) == ["coffee"]

    def test_remove_items(self, wired, settings):
        store.add_items(["coffee", "milk"], settings)
        assert store.remove_items(["COFFEE"], settings) == ["milk"]

    def test_close_list_empties_it(self, wired, settings):
        store.add_items(["coffee"], settings)
        assert store.close_list(settings) == []

    def test_reads_legacy_plain_string_documents(self, wired, settings):
        wired.store["nano_test_lists/shared_list"] = {"items": ["coffee", "milk"]}
        assert store.get_items(settings) == ["coffee", "milk"]

    def test_reads_structured_item_documents(self, wired, settings):
        wired.store["nano_test_lists/shared_list"] = {
            "items": [{"name": "coffee", "qty": "2"}, {"name": "milk"}]
        }
        assert store.get_items(settings) == ["coffee", "milk"]

    def test_mutation_is_transactional(self, wired, settings):
        """Read-modify-write must happen inside a transaction, not around it."""
        store.add_items(["coffee"], settings)
        assert wired.doc("nano_test_lists/shared_list")["items"] == ["coffee"]


class TestClearMatchedItems:
    def test_clears_what_was_bought(self, wired, settings):
        store.add_items(["coffee", "bananas"], settings)
        assert store.clear_matched_items(["COFFEE 250G"], settings) == ["bananas"]

    def test_does_not_clear_on_a_substring_collision(self, wired, settings):
        """The old bidirectional `in` test cleared "ham" when you bought
        "graham crackers"."""
        store.add_items(["ham"], settings)
        assert store.clear_matched_items(["GRAHAM CRACKERS"], settings) == ["ham"]

    def test_short_tokens_do_not_match_promiscuously(self, wired, settings):
        store.add_items(["tea"], settings)
        assert store.clear_matched_items(["STEAK"], settings) == ["tea"]

    def test_unrelated_items_survive(self, wired, settings):
        store.add_items(["coffee", "bread"], settings)
        assert store.clear_matched_items(["BANANAS"], settings) == ["coffee", "bread"]


class TestTaskNaming:
    def test_name_is_deterministic(self):
        assert tasks.task_name_for("wamid.ABC") == tasks.task_name_for("wamid.ABC")

    def test_different_messages_get_different_names(self):
        assert tasks.task_name_for("wamid.A") != tasks.task_name_for("wamid.B")

    def test_name_is_safe_for_the_cloud_tasks_api(self):
        """Real ids like `wamid.HBgLMzQ2...==` contain characters the API rejects."""
        name = tasks.task_name_for("wamid.HBgLMzQ2NzIxMTMyNDMVAgASGBQzQTI4RjE=")
        assert name.replace("-", "").replace("_", "").isalnum()
        assert len(name) <= 500

    def test_dedupe_depends_only_on_the_message_id(self):
        """This is what makes Cloud Tasks reject a redelivery outright."""
        assert tasks.task_name_for("wamid.X") == tasks.task_name_for("wamid.X")


class TestInlineDispatch:
    def test_inline_backend_runs_the_handler(self, settings):
        ran = []
        tasks.enqueue(
            {"payload": True},
            "wamid.A",
            settings,
            background_runner=lambda fn, payload: ran.append((fn, payload)),
            handler=lambda payload: None,
        )
        assert len(ran) == 1
        assert ran[0][1] == {"payload": True}

    def test_inline_backend_requires_a_runner(self, settings):
        with pytest.raises(RuntimeError, match="inline backend"):
            tasks.enqueue({}, "wamid.A", settings)


class TestTaskAuthentication:
    def test_inline_backend_skips_oidc(self, settings):
        """No Cloud Tasks means no token to check."""
        assert tasks.verify_task_request(None, settings) is True

    def test_cloud_tasks_rejects_a_missing_token(self, monkeypatch):
        monkeypatch.setenv("TASK_BACKEND", "cloud_tasks")
        monkeypatch.setenv("TASKS_TARGET_URL", "https://example.invalid/tasks/process")
        monkeypatch.setenv("TASKS_INVOKER_SA", "invoker@example.invalid")
        settings = load_settings()
        assert tasks.verify_task_request(None, settings) is False

    def test_cloud_tasks_rejects_a_malformed_token(self, monkeypatch):
        monkeypatch.setenv("TASK_BACKEND", "cloud_tasks")
        monkeypatch.setenv("TASKS_TARGET_URL", "https://example.invalid/tasks/process")
        monkeypatch.setenv("TASKS_INVOKER_SA", "invoker@example.invalid")
        settings = load_settings()
        assert tasks.verify_task_request("Bearer garbage", settings) is False
        assert tasks.verify_task_request("Basic abc", settings) is False
