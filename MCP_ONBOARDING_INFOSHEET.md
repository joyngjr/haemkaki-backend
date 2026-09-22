# Switch to HaemKakis without retyping a thing

**Infosheet — content baseline for design. Everything here is verified against
the shipping code; wording and hierarchy are open, the field names and rules
are not.**

---

## The problem

Every tracker asks you to start from zero. Months of doses, bleeds and
deliveries sit in a spreadsheet, a notes app or another tracker — and the only
way in is to type them again, day by day. Most people never do. They lose their
history, and with it the numbers that make a supply tracker worth using.

## What we do instead

HaemKakis has no import form, because you should not have to fill one in.
It exposes itself as an **MCP server**: you connect an AI assistant — Claude —
to your tracker, hand it your file however it already looks, and it does the
mapping, shows you a preview, and writes only when you say yes.

**Three steps, one conversation.**

| Step               | What you do                                              | What happens                                                                   |
| ------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **1. Connect**     | Paste one address into Claude, once.                      | Claude can now see your tracker. No account, no sign-in, no export file.        |
| **2. Hand it over** | Attach your spreadsheet. Say _"Import this into HaemKakis."_ | Claude reads your columns, converts IU to vials, and shows you a preview table. |
| **3. Confirm**      | Read the preview. Say yes.                                | Your history is in. Home and the Tracker are populated the next time they open. |

Nothing is written before you confirm. If it comes out wrong, one sentence
undoes the whole import.

---

## What your sheet needs

**Three things per row.** That is the whole requirement.

| You need    | Meaning                                | We accept                                                            |
| ----------- | -------------------------------------- | -------------------------------------------------------------------- |
| **A date**  | The day it happened                    | Any format. Claude normalises to the calendar day, Singapore time.   |
| **What it was** | Dose, bleed, or delivery           | Your own words — "infusion", "prophy", "knee bleed", "stock arrived". |
| **How much** | Amount of factor                      | Vials, or IU — Claude asks how many IU your vial holds and converts.  |

No template. No required column order. No header names to match. Claude reads
the sheet you already keep.

### It looks like this

Your sheet:

```
Date         Entry               Amount
12/03/2026   Regular infusion    1000 IU
15/03/2026   Knee bleed          1500 IU
16/03/2026   Follow-up dose      1000 IU
18/03/2026   Delivery received   20 vials
20/03/2026   Missed
```

What lands in HaemKakis (at 500 IU per vial):

```
2026-03-12   prophylaxis   2 vials
2026-03-15   on-demand     3 vials      ← this is how the app records a bleed
2026-03-16   follow-up     2 vials
2026-03-18   refill       20 vials
2026-03-20   —                          ← a missed dose is not a row; see below
```

---

## The five entry types

Every row becomes one of five. Dates are calendar days, `YYYY-MM-DD`, Singapore
time — there is no time of day. Amounts are whole **vials**, 1–99.

| Type          | Required fields                      | What it means                                          |
| ------------- | ------------------------------------ | ------------------------------------------------------ |
| `refill`      | `occurred_on`, `vials`               | Vials that arrived.                                     |
| `prophylaxis` | `occurred_on` _(+ `vials`)_          | A routine preventative dose. Your routine sizes it if the sheet doesn't say. |
| `on-demand`   | `occurred_on`, `vials`               | A dose for a bleed. **This is how a bleed is recorded** — there is no separate bleed entry. |
| `follow-up`   | `occurred_on`, `vials`               | A later dose for that same bleed.                       |
| `makeup`      | `occurred_on`, `missed_on`, `amount` | A dose you took late: the day you took it, and the planned day it was owed for. |

### A missed dose is not a field

You never record one, and there is nothing in your sheet to import. A missed
dose **is** a planned day that passed with no dose on it — the app works it out
from your routine, every time you open it. So:

- Bring your **routine** across and the gaps appear on their own.
- A dose you took a few days late comes in as a `makeup` naming the day it
  covered, and that day stops reading as missed.
- Backdate a dose later and the calendar updates itself. Nothing to clean up.

---

## Two things worth bringing besides history

**Your routine — three values.** Start date, how often (every N days, or fixed
weekdays), and vials per dose. Claude can set it during the same conversation.
It sizes your past prophylaxis doses, drives the next-dose reminder, and is what
makes missed days visible at all.

**Today's shelf count — one row.** HaemKakis counts vials from what you log:
deliveries in, doses out, starting at zero. If your old sheet never tracked
stock, say how many vials are in the fridge right now and Claude logs it as a
refill dated today. From that moment days of cover, the run-out date and the
order-by date are real numbers.

Skip it and nothing breaks — the import still succeeds, the app simply shows
**0 vials on hand** until you count. Factor used that the ledger couldn't
supply is reported separately, so the count is never negative and never
invented.

---

## Why it is safe to hand over your history

| Guarantee                | How                                                                                             |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| **Preview before write** | Every import runs as a dry run first. You see each row and what would happen to it.               |
| **You confirm**          | Nothing is written until you say so.                                                              |
| **One-sentence undo**    | Every import gets a batch id. "Undo that import" removes exactly what it added.                   |
| **Safe to re-run**       | A row identical to one already logged is skipped, so running the same sheet twice changes nothing. |
| **It refuses nonsense**  | Future dates, two doses on one day, a late dose claiming a day you already dosed on — all rejected with a reason, and one bad row means nothing is written. |

Up to 500 entries per import — more than a year of alternate-day prophylaxis.

---

## What you get the moment it lands

- Vials on hand, counted from your own history.
- Days of cover, your run-out date, and the date to order by.
- Your bleeds on the calendar, from the on-demand doses you already logged.
- Missed days, derived from your routine — no data entry, ever.
- The Medical ID and the rest of the app, populated and ready.

---

## The honest small print

- There is no sign-in. The connector address is the key: anyone holding it can
  read and change every profile on that server. Only connect assistants you
  trust.
- Amounts are vials, not IU. Claude asks for your vial size rather than
  guessing.
- Imported entries appear the next time the Tracker opens.

---

## Notes for design

- **Hero:** the three-step table, or a before/after of the spreadsheet snippet.
  That pairing is the whole pitch — a messy sheet on the left, a clean tracker
  on the right.
- **Lead with "three things per row"**, not the five entry types. The type table
  is reference; the audience needs to believe their existing file will work.
- **"A missed dose is not a field"** is the differentiator worth a callout — the
  app derives adherence instead of asking users to log their own failures.
- **Safety table** should read as reassurance, not fine print: preview → confirm
  → undo is a three-beat sequence and could be a strip of icons.
- Keep `refill`, `prophylaxis`, `on-demand`, `follow-up`, `makeup`,
  `occurred_on`, `missed_on` spelled exactly as written — they are the literal
  values the API accepts.
