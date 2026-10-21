# Catalog metadata

Each automation may carry a `catalog:` block. `wrapper-core` ignores it; the portal and governance reviews use it. It prevents
duplicate automations for the same process and answers "what does this touch, who owns it, what data does it see?" without
reading code.

```yaml
catalog:
  summary: "Routes open AP invoices to the correct team."      # <= 400 chars; shown on the card
  systems: ["System X", "AP workflow queue"]                   # every system the automation touches; [none] if truly none
  owner_team: "AP Automation"
  data_classification: internal                                # must equal the automation's data_classification
  process_owner: ap-lead@example.com                           # the business owner of the *process* (not the developer)
  links: [{title: "Process runbook", url: "https://wiki.example.internal/ap"}]   # http(s)/mailto only
  minutes_per_item: 4                                          # manual effort saved per item (feeds the BI layer)
  tags: [finance, ap, llm]                                     # lower-case, dashes
```

Validation: unknown keys inside `catalog` are an error (typos), classification mismatch is an error, links must be
`http(s)`/`mailto`; missing `summary`/`owner_team`/`process_owner`/`systems` are warnings (`validate --strict` makes them errors,
and `new-automation` pre-fills a complete skeleton so they start populated).

`owner` (top level) must be an email address: failure alerts and the notifier route by it.

Related top-level fields that govern LLM use: `data_classification` and `llm_allowed_classification`. When the data is classified
higher than the LLM endpoint is approved for, every `llm_call()` is disabled at runtime (the run synopsis falls back to a
deterministic one) and `validate` prints a warning so the owner knows before go-live.
