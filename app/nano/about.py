"""What Nano can say about itself.

Someone asked which AI model Nano uses, and it answered "I can't work that out
yet" and filed a feature request — the same failure as `PIT FAM.`, since the
model name is sitting in configuration. So it should answer. The care goes into
*how*.

Two rules, both learned elsewhere in this project.

**The text is written here, not generated.** Asked how it works, a language
model will describe a plausible system: vector databases, embeddings, a
fine-tune. None of that is true of this one, and none of it would be flagged as
invention. That is the failure from the currency bug — a gap in the input filled
convincingly — and the answer to it is the same as it was for `cannot_answer`
and the clarification question: the model picks which question was asked, and
code supplies the words.

**Configuration is not a source of facts.** `Settings` holds the WhatsApp token,
the app secret, the verify token, the project id, the receipts bucket and the
service-account emails. A self-description assembled by reflection, or by
handing config to a model, leaks infrastructure into a chat transcript — and
"how do you work?" is exactly the exchange that ends up screenshotted. So the
facts are an allowlist, and exactly one value is read at runtime: the model
name, which is a public product name.

`tests/unit/test_about_leaks_nothing.py` enforces both.
"""

from __future__ import annotations

from nano.config import Settings, get_settings

WHICH_MODEL = "which_model"
HOW_IT_WORKS = "how_it_works"
DATA_PRIVACY = "data_privacy"
WHO_BUILT_IT = "who_built_it"

TOPICS = {WHICH_MODEL, HOW_IT_WORKS, DATA_PRIVACY, WHO_BUILT_IT}


def _which_model(settings: Settings) -> str:
    # The one runtime value in this module. A public product name, not an
    # identifier — and still read from config rather than written down, because
    # a hardcoded answer would quietly go stale at the next model upgrade.
    return (
        f"I run on Google's {settings.gemini_model}, through Vertex AI.\n\n"
        "I use it for three things: working out what you're asking me, reading "
        "receipt photos, and translating item names into English. It doesn't "
        "learn from our chats — every message starts fresh, apart from the "
        "recent conversation I'm given for context."
    )


def _how_it_works(settings: Settings) -> str:
    return (
        "Roughly:\n\n"
        "- Your message arrives from WhatsApp and I work out what you meant.\n"
        "- The shopping list lives in a database, so we both see the same one.\n"
        "- A receipt photo goes to the model, which pulls out the store, date "
        "and every line item. Those get stored so I can answer questions later.\n"
        "- Item names are translated to English and given a common form, so "
        "MADUIXA and strawberry count as the same thing.\n"
        "- Questions about spending are answered from that stored history.\n\n"
        "It all runs on Google Cloud, in a private project that only Hugo can reach."
    )


def _data_privacy(settings: Settings) -> str:
    return (
        "Honestly: yes, I keep things.\n\n"
        "- Your messages to me, and my replies, for the last few turns — that's "
        "how I follow a conversation.\n"
        "- Every receipt photo you send, and everything printed on it.\n"
        "- The questions you ask me, so we can see which ones I answer badly.\n\n"
        "It's all in a private Google Cloud project that only Hugo can reach. "
        "Nothing is shared with anyone, and nothing is used to train any model. "
        "The one thing to know is that the two of you share everything — list "
        "changes and receipts are visible to both."
    )


def _who_built_it(settings: Settings) -> str:
    return (
        "Hugo built me — a WhatsApp assistant for a shared grocery list and the "
        "receipts that follow it.\n\n"
        "Ask me for *help* and I'll show you what I can do."
    )


_ANSWERS = {
    WHICH_MODEL: _which_model,
    HOW_IT_WORKS: _how_it_works,
    DATA_PRIVACY: _data_privacy,
    WHO_BUILT_IT: _who_built_it,
}


def answer(topic: str, settings: Settings | None = None) -> str:
    """Answer one question about Nano itself.

    An unknown topic falls back to the general description rather than an error:
    the model chose a word outside the enum, which is not the user's problem.
    """
    settings = settings or get_settings()
    return _ANSWERS.get(str(topic or ""), _how_it_works)(settings)
