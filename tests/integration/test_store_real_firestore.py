"""Verify against real Firestore what the unit tier can only assert against a fake.

Two claims are load-bearing for the webhook fix and neither can be proved with a
fake, because a fake is written to whatever the code expects:

1. `create()` on an existing document really does raise AlreadyExists. The whole
   deduplication design collapses if it silently overwrites instead.
2. The transaction context-manager form `store._mutate` uses really does commit
   against the live client. If `__exit__` did not commit, every list mutation
   would be a no-op in production while passing every unit test.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def wired_store(monkeypatch, firestore_client, firestore_prefix, project_id):
    """Point the store at real Firestore, namespaced so prod is unreachable."""
    from nano import clients, config, store

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("FIRESTORE_PREFIX", firestore_prefix)
    monkeypatch.setenv("VERIFY_TOKEN", "integration-token")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    monkeypatch.setenv("TASK_BACKEND", "inline")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)

    config.reset_settings_cache()
    clients.reset_clients()
    monkeypatch.setattr(store, "firestore_client", lambda: firestore_client)

    yield store, config.get_settings()

    config.reset_settings_cache()
    clients.reset_clients()


def _cleanup(client, prefix, doc_id):
    for collection in ("processed_messages", "lists"):
        client.collection(f"{prefix}{collection}").document(doc_id).delete()


class TestRealClaimSemantics:
    def test_create_is_atomic_and_conflicts_are_detectable(
        self, wired_store, firestore_client, firestore_prefix, run_id
    ):
        store, settings = wired_store
        try:
            assert store.claim_message(run_id, settings) is True
            assert store.claim_message(run_id, settings) is False
            assert store.claim_message(run_id, settings) is False
        finally:
            _cleanup(firestore_client, firestore_prefix, run_id)

    def test_seven_redeliveries_yield_exactly_one_claim(
        self, wired_store, firestore_client, firestore_prefix, run_id
    ):
        """The production multiplicity, against the real database."""
        store, settings = wired_store
        try:
            results = [store.claim_message(run_id, settings) for _ in range(7)]
            assert results.count(True) == 1
        finally:
            _cleanup(firestore_client, firestore_prefix, run_id)

    def test_released_claim_can_be_retaken(
        self, wired_store, firestore_client, firestore_prefix, run_id
    ):
        store, settings = wired_store
        try:
            assert store.claim_message(run_id, settings) is True
            store.release_claim(run_id, settings)
            assert store.claim_message(run_id, settings) is True
        finally:
            _cleanup(firestore_client, firestore_prefix, run_id)


class TestRealTransactionCommits:
    def test_context_manager_transaction_actually_persists(
        self, wired_store, firestore_client, firestore_prefix
    ):
        """If `with transaction:` did not commit, this returns an empty list.

        Every unit test would still pass, because the fake commits on `__exit__`
        by construction. This is the check that the real client agrees.
        """
        store, settings = wired_store
        marker = f"coffee-{uuid.uuid4().hex[:8]}"
        doc = firestore_client.collection(f"{firestore_prefix}lists").document("shared_list")
        try:
            store.add_items([marker], settings)
            persisted = (doc.get().to_dict() or {}).get("items", [])
            assert marker in persisted, "transaction did not commit to real Firestore"

            assert store.get_items(settings) == persisted
        finally:
            doc.delete()

    def test_add_then_remove_round_trips(self, wired_store, firestore_client, firestore_prefix):
        store, settings = wired_store
        doc = firestore_client.collection(f"{firestore_prefix}lists").document("shared_list")
        try:
            store.add_items(["coffee", "milk"], settings)
            assert set(store.get_items(settings)) == {"coffee", "milk"}
            store.remove_items(["COFFEE"], settings)
            assert store.get_items(settings) == ["milk"]
            store.close_list(settings)
            assert store.get_items(settings) == []
        finally:
            doc.delete()
