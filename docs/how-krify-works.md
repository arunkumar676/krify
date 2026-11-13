# How Krify Works

## Overview

1. **You type a task** in the side panel.
2. **The side panel sends it** to the server (Python on your PC).
3. **The planner makes a plan**: goal, steps, and how to know it's done (one AI call).
4. **The loop runs**:
   - **See**: the extension reads the page (numbered buttons, links, boxes).
   - **Decide**: the AI picks one action (click, type, read, scroll…).
   - **Act**: the extension does it in Chrome.
   - **Check**: the server looks at what happened, then repeats.
5. **Safety**: before paying, sending, deleting or anything involving accounts, Krify asks you.
6. **Notes**: Krify saves important facts (like prices) so it doesn't forget them.
7. **Done**: the AI gives the answer, and the server checks that the numbers were really on the page.
8. **Saved**: the task, its steps and the tokens used go into the database (History).

If a model hits its limit, Krify waits or switches to another free model.

---

## 1. SEE

Code: [extension/context/content.js](../extension/context/content.js)

### What it can see

- 🔢 **Buttons, links, boxes, dropdowns and checkboxes**: only those on screen now, up to 100
- 📝 **Text on the screen**, line by line (up to 3000 characters)
- 📄 **Whole page text**: only the first 2500 characters
- 📍 **Page address, title and scroll position**
- 📂 **Open menus and popups**, which are listed first
- 📊 **Google Sheets data**, read as a table (SQL)

### What it can't see

- ❌ **Anything after the first 2500 characters** of a long page (why the iPhone test failed: Amazon's menus and filters filled the 2500, so the product list was cut off)
- ❌ **Iframes**: boxes embedded from other sites (payment forms, videos, some popups)
- ❌ **Shadow DOM**: parts hidden inside a site's own components
- ❌ **Pictures**: only the image name (alt text), never what the picture shows
- ❌ **Canvas drawings**: charts, maps, games, the Sheets right-click menu
- ❌ **Element 101 onwards** on busy pages
- ❌ **Long labels**: a button's name is cut after 50 characters

### Ideas to make it better

1. **Read the main content first** and skip menus and filters. *Waiting for a paid AI model.*
2. **Product cards**: on shopping pages, give the AI a short list like "name, price, rating" for each product. Fewer tokens and clearer. ⭐ Top pick.
3. **Most useful first**: sort elements by how well they match the task, so the important ones are never cut off.
4. **Only send what changed** after an action, not the whole page again. Saves tokens.
5. **Look inside iframes and shadow DOM**.

---

## 2. DECIDE

Code: [backend/app/agent/loop.py](../backend/app/agent/loop.py), [prompt.py](../backend/app/context/prompt.py), [schemas.py](../backend/app/tools/schemas.py)

### What it uses to decide

Every step, the AI gets one message with:

- 📜 **Rules**: how to behave (~400 tokens)
- 🧰 **Tool list**: 24 actions it can pick from (~1090 tokens)
- 🎯 **Header**: your task, goal, plan and notes
- 👀 **The page**: what SEE found
- 🕘 **History**: what it did before (old page views removed to save tokens)

It replies with **one action**, or a **batch of up to 5** when it's sure without looking.

**Code helps it decide** with:

- 🔁 **Repeat check**: "you did the same thing 3 times"
- 😴 **Stuck check**: "5 steps with no progress"
- 📖 **Same read check**: "you already read this"
- 📜 **Scroll limit**: at most 4 scrolls per page
- ⏰ **Wrap-up warning** when 5 steps are left (limit is 25)
- 💬 **Nudge** if it replies with plain text instead of an action

### What it can't do

- ❌ **No memory of past tasks**: each task starts from zero
- ❌ **Doesn't learn from mistakes**: the same site trips it the same way every time
- ❌ **Thinks only a little**: reasoning is low or off to save tokens
- ❌ **Forgets old pages**: only the newest read of each site (up to 3 sites) is kept, the rest depends on notes (the iPhone test lost a price this way)
- ❌ **Resends everything every step**: rules and tool list go out each time, which is slow and costly
- ❌ **Mostly one action at a time**: slow, especially with Groq's per-minute limits
- ❌ **Weak backup models wander**: they scroll and switch tabs without reading or noting

### Ideas to make it better

1. **Send only the tools that are needed**: the Sheets tools only on Sheets; hide tools that don't apply (e.g. switch_tab with one tab). Saves tokens every step. ⭐ Top pick.
2. **Think harder only when stuck**: low reasoning normally, high after a repeat or stuck warning.
3. **Two models together**: a cheap one for easy steps (type, click), a smart one for hard steps (plan, compare, done).
4. **More batches**: e.g. search, open the first result and read it, in one call.
5. **Site tips**: remember what worked on a site (e.g. "Amazon: product list starts after filters") and give that tip next time.
6. **Prompt caching** with paid models: rules and tools get cached, so they're cheaper and faster.

## 3. ACT

Code: [extension/tools/operations.js](../extension/tools/operations.js), [realinput.js](../extension/tools/realinput.js), [extension/context/content.js](../extension/context/content.js)

### How it acts

The AI's choice goes to the extension, which does it in Chrome:

1. **JavaScript way** (normal sites):
   - 🖱️ **Click**: pretends to be a mouse press on the element
   - ⌨️ **Type**: puts the text in the box and tells the site it changed
   - 👆 **Hover, scroll, choose from dropdown**
   - 🎬 **Animated cursor**: moves, ripples, types letter by letter
2. **Real keyboard and mouse way** (Google Sheets, Docs and shortcuts):
   - 🎹 Uses Chrome's own input (chrome.debugger), so sites treat it like a real person
   - Right-click, double-click, drag and drop between elements
   - Chrome shows a "started debugging" bar; only used when needed
3. **Browser actions**: open a URL, go back, reload, open, switch or close tabs

After every action it **waits for the page to load** (checks the tab every 0.3 s).

### What it can't do

- ❌ **Some sites ignore the JavaScript way**: they notice it's "not a real click"; only the real-input way works there
- ❌ **Can't upload files**
- ❌ **Can't use right-click menus on normal sites**
- ❌ **Basic waiting**: only checks "page loaded", not "results finished appearing", so it can read too early
- ❌ **Can't work on Chrome's own pages** (chrome://, new tab page); the iPhone test wasted a step there
- ❌ **No CAPTCHAs, logins or passwords**, on purpose
- ❌ **Animations are slow**: cursor movement and letter-by-letter typing add time to every step

### Ideas to make it better

1. **Smarter waiting**: wait until the page stops changing, not just until it has loaded. Avoids reading half-loaded results. ⭐ Top pick.
2. **Fallback to real input**: if a JavaScript click does nothing, try a real click automatically.
3. **Start from a website**: if the task starts on a Chrome page, open the site directly.
4. **File upload** (already in TODO).
5. **Fast mode**: skip animations when you want speed.

## 4. CHECK

Code: [backend/app/agent/loop.py](../backend/app/agent/loop.py), [safety.py](../backend/app/guardrails/safety.py)

### How it checks

**Before an action (safety):**

- 🛑 **Risky actions ask you first**: pay, buy, send, delete, sign in/out, passwords
- ⚠️ **Overwrite guard on Sheets**: "these cells already have data, overwrite?"
- 📍 **Sheets cell check**: makes sure the right cell is selected before typing

**After an action:**

- 👀 **The AI sees the new page** and should check for itself that its action worked
- 🔄 **Code notices whether the page changed**; if not, it counts toward the stuck and repeat warnings
- 📦 **Batches stop** if the page changes in the middle (element numbers might be wrong)

**While working:**

- 🏷️ **Wrong product check on notes**: you asked for "16e" but the note doesn't say "16e", so it isn't saved
- 📄 **Wrong page warning**: the page title doesn't mention your product

**At the end (done):**

- 🔢 **Number check**: every price in the answer must have appeared on a page it read; if not, rejected once
- 🏷️ **Product check**: the answer must name your exact product; if not, rejected once

### What it can't check

- ❌ **"Did my click work?" is left to the AI**: code only knows whether the page changed, not whether it changed the right way
- ❌ **Weak number check**: only asks "did this number appear anywhere", not "did ₹72,990 belong to the right product"
- ❌ **Only 3+ digit numbers** are checked; ratings, small counts and text facts aren't
- ❌ **Product words need a digit**: "16e" can be checked, "iPhone Pro" or "red" can't
- ❌ **"Done when" isn't checked by code**: only the AI decides it's true
- ❌ **Rejections happen once only**: after that, a wrong answer still goes through, with a warning
- ❌ **Safety works by spotting words**: a risky button with an unusual name could slip through

### Ideas to make it better

1. **Number + product check together**: check that the price appears next to the product's name on the page, not just anywhere. ⭐ Top pick.
2. **AI judge at the end**: one small AI call compares the answer to the goal and "Done when", and says yes or no (a few hundred tokens).
3. **Did-it-work check**: after typing, read the box back; after a click, check that something expected appeared.
4. **Check all product words**, not just the ones with digits (Pro, Max, color).
5. **Show proof**: the final answer shows where each price came from (site + product name), so you can check it quickly.
