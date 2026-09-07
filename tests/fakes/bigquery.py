"""In-memory stand-in for google.cloud.bigquery.Client.

Records every statement and its bound parameters so tests can assert on the SQL
that was actually built, and returns scripted rows for reads. It deliberately
does not parse SQL — matching is by substring against a registered pattern,
which keeps tests readable and refactor-tolerant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeRow:
    """Mimics bigquery.Row: attribute access, mapping access, and indexing."""

    def __init__(self, values: dict[str, Any]):
        self._values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self._values.values())[key]
        return self._values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def keys(self):
        return self._values.keys()

    def items(self):
        return self._values.items()

    def __repr__(self) -> str:
        return f"FakeRow({self._values!r})"


@dataclass
class RecordedQuery:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    raw_params: list[Any] = field(default_factory=list)

    def param(self, name: str) -> Any:
        return self.params.get(name)


class FakeQueryJob:
    def __init__(self, rows: list[FakeRow], error: Exception | None = None):
        self._rows = rows
        self._error = error
        self.num_dml_affected_rows = len(rows)

    def result(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return list(self._rows)

    def __iter__(self):
        return iter(self.result())


def _as_plain(value: Any) -> Any:
    """Reduce a StructQueryParameter to a dict of field name -> value."""
    struct_values = getattr(value, "struct_values", None)
    if struct_values is None:
        return value
    return {name: getattr(field, "value", field) for name, field in dict(struct_values).items()}


def _flatten_params(job_config: Any) -> tuple[dict[str, Any], list[Any]]:
    """Pull {name: value} out of a QueryJobConfig for easy assertions."""
    if job_config is None or not getattr(job_config, "query_parameters", None):
        return {}, []
    flat: dict[str, Any] = {}
    raw = list(job_config.query_parameters)
    for p in raw:
        name = getattr(p, "name", None)
        if name is None:
            continue
        if hasattr(p, "values"):  # ArrayQueryParameter
            # An array of structs is the batched-insert shape. Flatten each
            # struct into a plain dict so tests can assert on field values
            # directly instead of poking at SDK objects.
            flat[name] = [_as_plain(v) for v in p.values]
        elif hasattr(p, "struct_values"):  # StructQueryParameter
            flat[name] = _as_plain(p)
        else:  # ScalarQueryParameter
            flat[name] = getattr(p, "value", None)
    return flat, raw


class FakeBigQueryClient:
    def __init__(self, project: str = "nano-test-project"):
        self.project = project
        self.queries: list[RecordedQuery] = []
        self._responses: list[tuple[str, list[FakeRow]]] = []
        self._errors: list[tuple[str, Exception]] = []
        self.default_rows: list[FakeRow] = []

    # --- scripting ----------------------------------------------------------

    def when(self, sql_contains: str, rows: list[dict[str, Any]]) -> FakeBigQueryClient:
        """Return `rows` for any statement containing `sql_contains`."""
        self._responses.append((sql_contains, [FakeRow(r) for r in rows]))
        return self

    def fail(self, sql_contains: str, error: Exception) -> FakeBigQueryClient:
        self._errors.append((sql_contains, error))
        return self

    # --- client surface -----------------------------------------------------

    def query(self, sql: str, job_config: Any = None, **kwargs) -> FakeQueryJob:
        params, raw = _flatten_params(job_config)
        self.queries.append(RecordedQuery(sql=sql, params=params, raw_params=raw))

        normalized = " ".join(sql.split())
        for needle, error in self._errors:
            if needle in normalized:
                return FakeQueryJob([], error=error)
        for needle, rows in self._responses:
            if needle in normalized:
                return FakeQueryJob(rows)
        return FakeQueryJob(list(self.default_rows))

    # --- assertion helpers --------------------------------------------------

    def statements_matching(self, needle: str) -> list[RecordedQuery]:
        return [q for q in self.queries if needle in " ".join(q.sql.split())]

    def count_matching(self, needle: str) -> int:
        return len(self.statements_matching(needle))

    @property
    def sql_log(self) -> list[str]:
        return [" ".join(q.sql.split()) for q in self.queries]
