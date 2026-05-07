# Launch materials

Pre-publish drafts for the Personal MVP launch. Edit and date-stamp
before posting; nothing here ships in the user-facing surface.

| File | Audience / channel |
|------|--------------------|
| [`SHOW_HN.md`](SHOW_HN.md) | Hacker News Show HN submission. Title + URL + Text + reply templates + posting checklist. |
| [`blog_post.md`](blog_post.md) | Project blog / Substack / LinkedIn long-form. Why-built, 16-step pipeline walkthrough, performance, roadmap. |
| [`FAQ.md`](FAQ.md) | Answer bank for the predictable questions. Trust & threat model / performance / privacy / distribution / architecture / maintenance. Use these as drafts in HN replies — copy, edit for the specific phrasing, do not paste verbatim. |
| `dogfooding/*` | Real CLI output captured from a live install (added by a follow-up PR). Replaces the synthetic GIF stills with proof-of-life screenshots. |

## Pre-publish checklist

Apply once, before any of the three channels go live:

- [ ] All install paths verified on a clean VM today
      (see `SHOW_HN.md` § posting checklist).
- [ ] LICENSE file landed (currently blocking; mentioned in
      multiple drafts as TBD).
- [ ] `Formula/aegis.rb` `sha256` updated to the launch-tag value
      (currently the all-zero placeholder; bump procedure in
      [`../../pkg/brew/README.md`](../../pkg/brew/README.md)).
- [ ] CI green on `main` and the tests-passed badge number is
      current.
- [ ] Dogfooding screenshots captured against the actual
      `~/.aegis/audit.jsonl` (PR 6).
- [ ] The 30-second `quickstart.gif` renders inline on a fresh
      GitHub cache-bust.

## Channel order

Recommended sequence:

1. **Blog post first** (project blog or Substack), date-stamped.
2. **Show HN** referencing the GitHub repo (the post body is the
   hook; the blog post is the long-form follow-up linked from the
   replies).
3. **Twitter / LinkedIn / Reddit r/ClaudeAI** as same-day
   amplifications, all linking back to the blog post for the long
   form.

Why blog-first: gives you a stable URL for "longer write-up here"
replies on HN, and the post itself can absorb an HN front-page spike
without GitHub README rate-limit issues.
