"""Automated secret-hygiene checks over everything git would commit.

This exists because the test suite is the easiest place for real data to leak:
fixtures want realistic values, and a real phone number is the most convenient
realistic value there is. These tests make that mistake fail CI instead of
reaching a public repository.

Three independent checks, deliberately not relying on each other:

1. **Structural** — no phone-number-shaped or business-ID-shaped digit strings
   outside the fiction-reserved ranges. Catches leaks with no prior knowledge.
2. **Known-value** — if `NANO_SECRET_SCAN_VALUES` is set (a comma-separated list
   of real identifiers, supplied from the local environment or a GitHub secret,
   never from the repo), assert none of them appear anywhere.
3. **Denylist** — resource names and token patterns that should only ever be
   supplied through configuration.

None of this file contains a real identifier, which is the point.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Files whose whole job is to describe deployment; project identifiers are
# expected in CI config and are excluded from the structural scan only.
SCAN_SKIP = {".gitignore", "uv.lock"}

# Digit strings we allow: the +1-555-01xx fiction block, and all-zero placeholders.
ALLOWED_NUMERIC = re.compile(
    r"""^(
        0+                    # all-zero placeholder IDs
      | 1555010\d{4}          # +1 555 01xx reserved-for-fiction numbers
      | 15550100000           # the placeholder business display number
    )$""",
    re.VERBOSE,
)

# A run of 11-16 digits looks like an E.164 number or a Meta business/phone ID.
NUMERIC_CANDIDATE = re.compile(r"\b\d{11,16}\b")

# Things that should only ever arrive via environment or secrets.
DENYLISTED_PATTERNS = [
    (re.compile(r"EAA[A-Za-z0-9]{20,}"), "a Meta/WhatsApp access token"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "a Google API key"),
    (re.compile(r'"type"\s*:\s*"service_account"'), "a service account key file"),
]


def _tracked_files() -> list[Path]:
    """Every file git would include: tracked plus untracked-but-not-ignored."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = []
    for line in out.stdout.splitlines():
        p = REPO_ROOT / line
        if p.name in SCAN_SKIP or not p.is_file():
            continue
        if p.suffix in {".png", ".jpg", ".jpeg", ".pdf", ".ico"}:
            continue
        paths.append(p)
    return paths


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


@pytest.fixture(scope="module")
def committable_files() -> list[Path]:
    files = _tracked_files()
    assert files, "expected to find files git would commit"
    return files


class TestGitignoreProtectsLocalDocs:
    """The three local-only files hold every real identifier in the project."""

    @pytest.mark.parametrize(
        "filename", ["context.md", "project-log.md", "CLAUDE.local.md", ".env"]
    )
    def test_local_only_file_is_ignored(self, filename: str):
        result = subprocess.run(
            ["git", "check-ignore", filename],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{filename} is NOT gitignored. It holds real project identifiers "
            f"(project IDs, phone numbers, tokens) and must never be committed."
        )

    @pytest.mark.parametrize("filename", ["context.md", "project-log.md", "CLAUDE.local.md"])
    def test_local_only_file_is_not_tracked(self, filename: str):
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", filename],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, f"{filename} is tracked by git and would be published."


class TestNoRealIdentifiersInCommittableFiles:
    def test_no_phone_or_business_id_shaped_numbers(self, committable_files: list[Path]):
        """Any long digit run must be a documented placeholder.

        Real WhatsApp phone-number IDs are ~15 digits and E.164 numbers 11-15,
        so this catches both without needing to know the actual values.
        """
        offenders: list[str] = []
        for path in committable_files:
            for lineno, line in enumerate(_read(path).splitlines(), 1):
                for match in NUMERIC_CANDIDATE.findall(line):
                    if not ALLOWED_NUMERIC.match(match):
                        rel = path.relative_to(REPO_ROOT)
                        offenders.append(f"{rel}:{lineno}: {match}")

        assert not offenders, (
            "Found digit strings that look like real phone numbers or Meta business IDs:\n  "
            + "\n  ".join(offenders)
            + "\n\nUse the placeholders in tests/fakes/whatsapp.py (+1 555 01xx is "
            "reserved for fiction), and supply real values via the environment."
        )

    def test_no_tokens_or_keys(self, committable_files: list[Path]):
        offenders: list[str] = []
        for path in committable_files:
            content = _read(path)
            for pattern, description in DENYLISTED_PATTERNS:
                if pattern.search(content):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: looks like {description}")
        assert not offenders, "Possible credential material committed:\n  " + "\n  ".join(offenders)

    def test_known_real_values_are_absent(self, committable_files: list[Path]):
        """Belt-and-braces check against actual values, supplied out-of-band.

        Set NANO_SECRET_SCAN_VALUES to a comma-separated list of the real
        identifiers (numbers, project IDs, tokens, names). It is read from the
        environment precisely so the values never appear in this repository.
        Skips when unset, so the suite stays runnable by anyone.
        """
        raw = os.environ.get("NANO_SECRET_SCAN_VALUES", "").strip()
        if not raw:
            pytest.skip("NANO_SECRET_SCAN_VALUES not set; structural checks still applied")

        needles = [v.strip() for v in raw.split(",") if v.strip()]
        offenders: list[str] = []
        for path in committable_files:
            content = _read(path).lower()
            for needle in needles:
                if needle.lower() in content:
                    # Report the location, never the value itself.
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}: contains a value from "
                        f"NANO_SECRET_SCAN_VALUES (index {needles.index(needle)})"
                    )
        assert not offenders, "Real identifiers found in committable files:\n  " + "\n  ".join(
            offenders
        )


class TestFixturesAreSynthetic:
    """Guard the fixtures themselves, so realistic-looking data stays fake."""

    def test_member_numbers_are_in_the_fiction_reserved_block(self):
        from tests.fakes.whatsapp import MEMBER_PRIMARY, MEMBER_SECONDARY, OUTSIDER

        for number in (MEMBER_PRIMARY, MEMBER_SECONDARY, OUTSIDER):
            assert number.startswith("1555010"), (
                f"{number} is outside the +1 555 01xx fiction block and may be a real number"
            )

    def test_business_ids_are_placeholders(self):
        from tests.fakes.whatsapp import PHONE_NUMBER_ID, WABA_ID

        assert set(PHONE_NUMBER_ID) == {"0"}
        assert set(WABA_ID) == {"0"}

    def test_no_personal_names_in_fixtures(self):
        """Members are addressed by role, so a name change never means a data change."""
        from tests.fakes import whatsapp

        source = Path(whatsapp.__file__).read_text(encoding="utf-8")
        assert "MEMBER_PRIMARY" in source
        assert "MEMBER_SECONDARY" in source

    def test_receipt_fixture_is_not_a_real_store(self, receipt):
        assert "EXEMPLE" in receipt.store_name.upper()

    def test_synthetic_credentials_are_labelled_as_such(self):
        from tests.conftest import ACCESS_TOKEN, APP_SECRET, VERIFY_TOKEN

        for value in (APP_SECRET, VERIFY_TOKEN, ACCESS_TOKEN):
            assert "not-real" in value, f"{value!r} should be obviously synthetic"
