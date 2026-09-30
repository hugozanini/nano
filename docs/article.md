# Nano: a WhatsApp grocery assistant, and the lakehouse under it

My wife and I do one big supermarket run a week. For a long time the list for it lived in our
WhatsApp conversation, which is another way of saying it did not really live anywhere.

Someone would write "we're out of coffee" on a Tuesday. By Saturday that message sat twenty
messages up, buried between a photo of something one of us had seen and a conversation about
the weekend. We would stand in an aisle scrolling backwards through the chat, trying to
reconstruct a list out of fragments, and still come home without the coffee.

So I built Nano: a small assistant that lives in the same WhatsApp conversation we were already
having, keeps one shared list for both of us, and remembers what we bought.

I also had a second motive, and it is worth admitting up front. I wanted to learn Google's
open-format lakehouse tooling — Apache Iceberg tables in BigQuery — and I have never learned
anything properly from a tutorial dataset. Our groceries were data I actually cared about being
right, at a volume where being wrong cost nothing. That turned out to be the ideal teacher.

---

## 1. The problem, and the one that came after it

The first problem was simply that a chat thread is not a list. It has no state. Nothing in it
can be ticked off, and by the time you need it, it has scrolled away.

The obvious answer is to put a bot in the group chat, and that answer is not available.
WhatsApp's Business Platform does not let you add an API number to an existing personal group —
it is a deliberate restriction, not a gap you can engineer around. So Nano runs as **two
separate one-to-one chats sharing a single backend**. When either of us changes the list, both
phones hear about it. It behaves like a shared assistant without being one, and after a few
weeks you stop noticing the difference.

The second problem arrived with the first receipt, and it is the one that shaped everything
underneath.

We live in Barcelona, and our receipts print in Catalan. A line reads `MADUIXA`. Another reads
`PASTANAGA 500 G`. A third says `PIT FAM.`, which even I had to think about. The questions I
wanted to ask were in English — *how much do we spend on strawberries?* — and no amount of
clever database searching will ever connect "strawberries" to `MADUIXA`. The two words do not
share a single letter.

That settled an important design point very early: whatever I built had to understand each line
**at the moment it was stored**, not at the moment it was asked about. Translation is part of
writing the data, not part of reading it.

> **[Screenshot: adding items to the list]**
> _Caption: free text in, a shared list out._

---

## 2. What it actually does

Everything happens in the chat we already use. There are no commands to memorise, which was a
requirement rather than a nicety — my wife was never going to learn a syntax, and she was
right not to.

| You say | Nano does |
|---|---|
| "we're out of coffee" | adds it, and tells the other phone |
| "what's on the list?" | shows the shared list |
| "remove milk" / "undo" | takes it off, or reverses the last change |
| "clear the list" | confirms first — this one is destructive |
| *(photo of a receipt)* | reads the store, date, total and every line; ticks off what was on the list |
| "how much did we spend this month?" | answers from the purchase history |
| "what is family pack breast?" | explains a line from the receipt it read |
| "which AI model do you use?" | answers honestly about itself |

Follow-up messages work, because the last few exchanges travel with each new one. "No, the
decaf one" and "show me that again" resolve against whatever was just said, which sounds small
and is the difference between something you use and something you abandon.

> **[Screenshot: asking about past spending]**
> _Caption: a question answered from the purchase history._

> **[Screenshot: a receipt logged, items listed in English]**
> _Caption: `strawberry (MADUIXA)` — English first, what the shop actually printed in brackets._

---

## 3. The shape of it

```mermaid
flowchart LR
    WA["WhatsApp<br/>two 1:1 chats"] --> CR["Cloud Run<br/>/webhook"]
    CR -- "verify · dedupe<br/>enqueue · 200 OK" --> CT["Cloud Tasks"]
    CT -- "OIDC" --> PR["Cloud Run<br/>/tasks/process"]
    PR --> GM["Gemini on Vertex AI<br/>intent · receipts · translation"]
    PR --> FS["Firestore<br/>live list · memory"]
    PR --> GCS["Cloud Storage<br/>receipt images"]
    PR --> BQ["BigQuery<br/>Iceberg tables"]
```

The webhook has exactly one job: decide in milliseconds whether an incoming message is genuine
and worth processing, hand it to [Cloud Tasks](https://cloud.google.com/tasks/docs), and return
`200`. Everything slow happens somewhere else.

I would like to claim that was foresight. It was not — it was a lesson. The first version did
all the work inline, which meant a receipt photo held the connection open for 82 seconds while
Gemini read it. Meta treats a slow webhook as a failed one and retries it, so a single
photograph of a single receipt became seven identical purchases in the database, and our
monthly grocery total was wrong by a factor of seven. Moving the work behind a queue fixed the
latency and the duplicates in one change.

Reading the messages themselves uses [Gemini's function
calling](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/function-calling).
The model does not write the reply; it chooses one of fourteen functions and fills in its
arguments, and the code decides what to do. Receipt photos go to the same model, so there is no
separate OCR service anywhere in the stack.

---

## 4. Why a lakehouse for a household

There are two kinds of data here, and they want completely different things.

The **live shopping list** is read and written constantly, is a few dozen words long, and has no
analytical value whatsoever on its own. It needs to be fast and simple, and it lives in
[Firestore](https://cloud.google.com/firestore/docs).

The **purchase history** is the opposite. It is written once per shopping trip, never modified,
and exists entirely so that questions can be asked of it later. That is the part worth putting
in a lakehouse: six [Apache Iceberg managed
tables](https://cloud.google.com/bigquery/docs/biglake-iceberg-tables-in-bigquery) in BigQuery,
which until recently were called "BigLake tables for Apache Iceberg".

If Iceberg is unfamiliar, the short version is this. It is an open table format: your data sits
as ordinary Parquet files in a bucket **you own**, alongside metadata files describing the shape
of the table. BigQuery gives you the fully managed experience — plain SQL, no servers — but the
files are yours, in your bucket, in a format other engines can read. Nothing is trapped.

Creating one is unremarkable, which is the appeal:

```sql
CREATE TABLE `<dataset>.purchases` (
  purchase_id  STRING,
  purchased_at TIMESTAMP,
  store_name   STRING,
  total_amount NUMERIC,
  currency     STRING
)
WITH CONNECTION `<region>.<connection>`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = 'gs://<your-bucket>/warehouse/purchases'
);
```

You write to it with normal `INSERT` and `MERGE`. There is no catalog service to operate. The
only piece of plumbing is a [BigQuery
connection](https://cloud.google.com/bigquery/docs/connections-api-intro) whose service account
is allowed to write into your bucket.

### How the data is modelled

Six tables, in three groups that never really touch each other:

```mermaid
erDiagram
    STORES ||--o{ PURCHASES : "one shop, many trips"
    PURCHASES ||--|{ PURCHASE_ITEMS : "one trip, many lines"

    STORES {
        string store_id PK
        string store_brand "Mercadona"
        string store_name_original "as printed"
        string tax_id "the fingerprint"
        string country_code
        string city_inferred
        int64 receipt_count
    }
    PURCHASES {
        string purchase_id PK
        string store_id FK
        timestamp purchased_at
        numeric total_amount
        string currency
        numeric amount_eur "comparable"
        numeric fx_rate
        date fx_rate_date
        string country_code
    }
    PURCHASE_ITEMS {
        string purchase_id FK
        string item_name "MADUIXA"
        string item_name_en "strawberry"
        string normalized_name "strawberry"
        string category "produce"
        numeric quantity
        numeric line_total
        numeric line_total_eur
    }
    LIST_EVENTS {
        string event_id PK
        string event_type "add · remove · close"
        string item_name
        string sender
        timestamp event_ts
    }
    QUESTION_LOG {
        string question_id PK
        string question
        string canonical "for grouping"
        string resolution "template · generated · unanswerable"
        bool answered
        int64 latency_ms
    }
    FEATURE_REQUESTS {
        string request_id PK
        string text
        string canonical_text "dedupe key"
        string source "explicit · unanswered"
        string status
        int64 request_count
    }
```

Three deliberate choices are buried in that diagram.

**Every line item keeps three names.** `item_name` is what the shop printed, `item_name_en` is
the English translation, and `normalized_name` is the canonical term used for matching. Keeping
all three is what lets Nano answer a question about strawberries, reply in English, and still
show you the Catalan text that is actually on the paper in your hand.

**`stores` is a separate table**, and shops are resolved to it once. A supermarket brand prints
its name slightly differently across receipts, and without a stable `store_id` every one of
those spellings becomes its own shop, quietly splitting your spending across four rows that
should be one.

**Money is stored twice.** `total_amount` with its `currency` is what was actually charged.
`amount_eur` is the same figure converted at `fx_rate`, using the rate from `fx_rate_date` —
the day of the purchase, not today. It means totals from last year still reconcile.

`list_events`, `question_log` and `feature_requests` have no relationships at all. They are
append-only logs, and the last two are what section 6 is about.

### The part that earned its keep

Here is what happens when a receipt photo arrives:

```mermaid
flowchart TD
    P["Receipt photo"] --> E["Gemini reads it<br/>store · date · total · every line"]
    E --> L["Where is this from?<br/>VAT rates · tax id · language"]
    E --> C["Translate each line<br/>MADUIXA → strawberry · produce"]
    L --> S["Resolve the shop<br/>one row per brand"]
    E --> X["Convert to EUR<br/>at the rate on that day"]
    S --> W["Write"]
    C --> W
    X --> W
    W --> T1["purchases"]
    W --> T2["purchase_items"]
    P --> G["Archive the image<br/>Cloud Storage"]
```

None of those steps existed on day one. Each was added because a question I wanted to ask
turned out to be unanswerable, and each one needed somewhere new to put its answer.

This is where Iceberg quietly justified itself. The `purchases` table started with seven
columns and now has twenty-one:

| Added later | Because |
|---|---|
| `receipt_language`, `country_code`, `city_inferred` | working out where a receipt came from |
| `tax_rates`, `tax_id` | VAT rates are a reliable fingerprint for a country |
| `store_id` | one row per shop, so totals stop splitting |
| `amount_eur`, `fx_rate`, `fx_rate_date` | one comparable currency, at the rate on the day |

Every one of those arrived as an additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, run
against a table that already held real data, while the assistant carried on answering messages.
Existing rows simply get `NULL`. Nothing is rewritten, there is no backfill, and there is no
migration window. Ten numbered SQL files, applied in order, are the entire history of the schema.

For a project that changes shape every weekend, that property mattered far more than any
query-performance argument.

**One honest caveat.** This is a household's groceries — three shopping trips and thirty-four
line items at the time of writing. Iceberg is comically oversized for that, and I chose it
anyway, deliberately, because I wanted to learn it somewhere the stakes were zero. If you are
weighing this for real work, match the tool to the problem. Just be clear with yourself about
which one you are doing.

---

## 5. Product decisions that cost something

**Answer in English, always.** Receipts arrive in Catalan or Spanish; every answer comes back in
English, with the printed name preserved in brackets — `strawberry (MADUIXA)`. Standardising is
the thing Nano is *for*, so this is a promise rather than a preference. Keeping the original
alongside costs a few characters and means you can still find the line on the paper receipt when
you are checking a total.

**Stop rather than guess.** Asked *"What is family pack breast? Chicken breast?"*, Nano explains
what the receipt contains — the printed text, its reading of it, the category, the price — and
then deliberately stops:

> That's my reading of the abbreviation on the receipt — it doesn't record anything beyond the
> printed name, so I can't confirm more than that.

It is almost certainly chicken. Saying so would have been correct by luck and wrong by method,
and I know that because of a bug that still makes me wince. A generated query once returned an
unnamed column containing `3.11`, and with no column name to go on, the model reported
**"$3.11" for a purchase made in euros**. It was not flagged as a guess. It read exactly like an
answer. A language model fills gaps in its input silently and plausibly, and the only real fix
is never to leave the gap.

**Ask, but rarely.** When a message genuinely has two readings, Nano asks which you meant. The
risk here is obvious and severe: an assistant that checks constantly is far more annoying than
one that occasionally guesses wrong. So the restraint is measured rather than hoped for —
across twelve unambiguous messages tested against the real model, it asks **zero** times.

> **[Screenshot: Nano explaining a receipt line, and declining to guess]**
> _Caption: what the receipt says, and where the receipt stops._

---

## 6. The loop: Nano files its own backlog

This is the part I did not plan, and the part I would build first next time.

Every question gets logged along with how it was resolved: answered from a hand-written SQL
template, answered by SQL the model generated on the spot, or not answered at all.

```mermaid
flowchart LR
    Q["You ask<br/>something"] --> R{"Can Nano<br/>answer it?"}
    R -- "template" --> A["Answer"]
    R -- "generated SQL" --> A
    R -- "no" --> F["feature_requests<br/>deduplicated by meaning"]
    Q --> QL["question_log<br/>every question + how it resolved"]
    QL --> V["v_question_patterns<br/>what recurs, what never works"]
    V --> B["Backlog"]
    F --> B
    B --> SH["Shipped feature"]
    SH -.-> R
```

A question that nothing could answer is the clearest product signal available: someone wanted
something and did not get it. The catch is that nobody files a ticket for a grocery bot — my
wife is certainly not going to — so Nano files it instead. Requests are deduplicated on meaning,
so three different phrasings of the same idea become one row with a count of three. That turns a
pile of utterances into a rough measure of demand.

It works better than I expected. Of the five items in the backlog right now, three were filed by
Nano itself, and all three have since shipped:

| Nano recorded | Became |
|---|---|
| "translate the list to English" | English item names everywhere |
| "what family pack breast is" | explaining any line on a receipt |
| "which AI model you use" | answering questions about itself |

**The failure in that table matters more than the successes.** The very first row was "what
family pack breast is" — a question Nano already had every single fact needed to answer. It had
translated `PIT FAM.` itself, minutes earlier, in the same conversation. Refusing to answer was
bad. Filing a feature request for a feature that already existed was considerably worse, because
the backlog is the thing that decides what gets built next.

Answering wrongly misleads one person once. Filing wrongly misleads the roadmap.

> **[Screenshot: "which AI model do you use?"]**
> _Caption: a question Nano used to refuse, and now answers._

---

## 7. What it cost, and what isn't built

Running it costs a few dollars a month. Cloud Run scales to zero between messages, the Gemini
calls are small, and BigQuery at this volume disappears into the free tier. Secrets live in
[Secret Manager](https://cloud.google.com/secret-manager/docs), and deploys authenticate through
[Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation),
so there are no downloadable keys anywhere in the project.

There are 577 unit tests, and the test code is larger than the application code — roughly 7,100
lines against 5,900. That ratio is not diligence for its own sake. Three separate times in this
project, a test that *could not fail* passed happily while the thing it was guarding was
thoroughly broken. Each time, the only thing that caught it was deliberately breaking the code
to watch the test go red. I now do that as a matter of course, and I would recommend it to
anyone.

And then the things I planned and did not build:

- **No Spark job.** The original design had serverless Spark reading the same Iceberg files to
  learn our usual shopping day. The service account exists and has never run anything.
- **No proactive reminders.** Same reason — it needs months of history before there is a pattern
  worth learning.
- **No second engine has ever read these tables.** "One copy of data, many engines" is the real
  argument for an open format, and I have not actually demonstrated it. The files are open and
  sitting in my bucket, which makes it a claim about what is *possible*, not something I have
  done.
- **Most spending questions still cannot be answered** — for want of data, not code. Three
  shopping trips is not a trend.

That is the honest state of it. The architecture is considerably further along than the dataset,
which is a strange place to end up, and almost exactly where a side project always is.

The list works, though. We have stopped forgetting the coffee.

---

*Nano is open source at [github.com/hugozanini/nano](https://github.com/hugozanini/nano).*
