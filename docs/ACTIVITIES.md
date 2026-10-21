# Activities: the UiPath building blocks, as code

`wrapper_core.activities` is the library a migrating UiPath workflow reaches for. Everything is plain Python (standard library
only by default), runs **unattended as the signed-in user**, and raises the platform's *classified* exceptions so
`wrapper.run_item` and the retry engine need no extra code:

| Failure | Raised as | Retry engine does |
|---|---|---|
| connection / DNS / timeout / TLS | `NetworkError` | VPN tool, wait, retry |
| 401/403, bad credentials, expired token | `AuthExpired` | refresh once, retry once, escalate |
| 408/429/5xx, mail 4xx, busy DB, file not there yet | `SystemUnavailable` | exponential backoff, escalate |
| bad data, 4xx, rejected recipient, unreadable file | `DataError` | skip the item, continue the run |
| missing capability/credential, wrong selector | `NotAvailable` / `LogicError` / `ElementNotFound` | never retried, escalates with a clear message |

```python
import wrapper_core as wrapper
from wrapper_core.activities import excel, mail, files, http, db, queue, credentials, windows_ui, pdf, images, notify
```

Submodules import lazily. Telemetry: each activity emits an `activity` event with **counts and sizes only** - never addresses,
subjects, bodies, URLs with query strings, tokens or passwords. Credentials/secrets come from Windows Credential Manager
(`credentials.get_credential`), not from code or the manifest.

## UiPath activity -> AutoStation call

| UiPath | AutoStation | Notes |
|---|---|---|
| Read Range / Read CSV | `excel.read_xlsx(path, sheet=None, header=True)` / `excel.read_csv(path)` | returns `list[dict]`; dates come back as `date`/`datetime`; CSV sniffs encoding + delimiter; no Excel needed |
| Write Range / Write CSV | `excel.write_xlsx(path, rows, sheet="Sheet1")` / `excel.write_workbook(path, {"A": rows, "B": rows})` / `excel.write_csv` | atomic write, reproducible bytes |
| Append Range | `excel.append_xlsx(path, rows, sheet)` | rewrites sheet data: formatting is **not** preserved |
| Filter Data Table | `excel.filter_rows(rows, predicate, col=value)` | |
| Join Data Tables / Merge | `excel.join(left, right, on="id", how="inner"\|"left")` | |
| Remove Duplicate Rows | `excel.dedupe(rows, key=None, keep="first")` | |
| Group By / Aggregate | `excel.group_by(rows, "col")`, `excel.aggregate(rows, "col", total=("amt", "sum"))` | |
| Send SMTP Mail Message | `mail.send_smtp(host, port, sender, to, subject, body, html=None, attachments=[], credential="smtp")` | STARTTLS/SSL/plain, `cc`/`bcc` |
| Get IMAP Mail Messages | `mail.read_imap(host, credential="mailbox", search="UNSEEN", save_attachments_to=dir, mark_seen=True)` | attachment names sanitised (no path traversal) |
| Send / Get Outlook Mail Message | `mail.outlook_send(...)`, `mail.outlook_read(folder="Inbox/Invoices", unread_only=True, save_attachments_to=dir, mark_read=True)` | desktop Outlook COM via PowerShell; `NotAvailable` without Outlook |
| HTTP Request / Deserialize JSON | `c = http.client(base_url, auth=("u","p")\|http.BearerToken(t)\|http.OAuth2ClientCredentials(...))`; `c.get_json("/x")`, `c.post("/y", json={...})` | retries idempotent calls on 5xx/429/network (honours Retry-After); never logs tokens |
| Pagination loop | `c.paginate("/items", style="page"\|"offset"\|"link"\|"next_key", items_key="data")` | generator of items |
| Download File | `http.download(url, dest, sha256=...)` | atomic, size-capped, hash verified |
| Connect / Execute Query (SQLite) | `with db.sqlite(path) as conn: db.query(conn, sql, params)` | dict rows, WAL |
| Execute Query / Non Query (ODBC: SQL Server, Oracle, Access...) | `db.query_odbc(conn_string_or_None, sql, params, connection_string_secret="erp")`, `db.execute_odbc(...)` | machine's ODBC drivers + Windows auth; parameterised `?` only |
| Add Queue Item | `q = queue.open("invoices"); q.add(ref, payload, priority=1, defer_until=..., deadline=...)` | de-dupes by reference |
| Get Transaction Item | `item = q.next()` | atomic claim; priority -> deadline -> FIFO |
| Set Transaction Status | `q.succeed(item, output)`, `q.fail_business(item, reason)`, `q.fail_system(item, reason, max_retries=3)` | business never retried; system re-queued then failed |
| Orchestrator Action Center / human validation | `q.needs_review(item, reason)`, `q.review_items()`, `q.resolve_review(item, approve=True, note=...)` | approved items go back to `new` |
| REFramework performer loop | `queue.run_queue(q, handler, policy)` | handler raises `DataError`/`queue.BusinessError` (business), `queue.NeedsReview`, anything else is a system error |
| Should Stop / Max runtime | `run_queue(..., max_items=, max_seconds=)` | stops cleanly; also stops after N consecutive system failures |
| Get Credential | `user, pwd = credentials.get_credential("sap")` | Credential Manager target `AUTOMAGIX/cred/sap`; password `repr` is masked |
| Get Asset (text) | `credentials.get_asset("api-url", default)` | manifest `assets` for the ring |
| Copy / Move File | `files.safe_copy(src, dst)`, `files.safe_move(src, dst)` | atomic, never clobbers (`doc (1).txt`), sha256-verified |
| Wait for file / File Change Trigger | `files.wait_for_file(path, timeout)`, `files.new_files(folder, "*.csv", state_name)` + `files.mark_processed(...)` | the latter is the watcher for a `poll` slot |
| Compress / Extract Files | `files.zip_dir(src, zip)`, `files.unzip_safe(zip, dest)` | zip-slip, entry-count and zip-bomb guards |
| Delete old files | `files.cleanup_older_than(folder, days=30, pattern="*.log")` | `dry_run=True` first |
| Create temp folder | `with files.workdir() as d:` | cleaned up on exit |
| Open Application | `windows_ui.launch("app.exe")` | |
| Attach Window / Wait Element | `win = windows_ui.find_window(r"Title regex", timeout=15)`, `el = windows_ui.find(win, name=..., automation_id=..., control_type="Button")` | UI Automation; bounded waits |
| Click / Double Click | `windows_ui.click(el)`, `windows_ui.double_click(el)` | uses the control's Invoke pattern, mouse as fallback |
| Type Into / Set Text | `windows_ui.set_text(el, "text")` | text is never logged (only its length) |
| Get Text | `windows_ui.get_text(el)` | |
| Send Hotkey / Type keys | `windows_ui.send_keys("^s", el)` | .NET `SendKeys` syntax |
| Select Item | `windows_ui.select(combo, "Item name")` | |
| Element Vanish | `windows_ui.wait_vanish(el, timeout)` | |
| Take Screenshot | `windows_ui.screenshot(path, window)` / `notify.screenshot_screen(path)` | pair with `wrapper.add_evidence` for failures |
| Close Application | `windows_ui.close(win)` | |
| Copy / Paste to clipboard | `notify.clipboard.get()` / `.set(text)` | needs an interactive desktop |
| Read PDF Text | `pdf.extract_text(path)` | text-based PDFs; `engine="pdfminer"` if installed |
| Read PDF With OCR / OCR Text | `images.ocr_image(path)` | needs pytesseract + Tesseract |
| Find Image / Click Image | `images.find_on_screen(template)` | needs pyautogui (+ opencv for `confidence`) - last resort |
| Send Teams message | `notify.teams_webhook("teams-alerts", title, text, facts)` | webhook URL lives in Credential Manager |
| Delay | `time.sleep(n)` | prefer `files.wait_for_file` / `windows_ui.find(..., timeout=)` polling |
| Retry Scope | `wrapper.run_with_retry(fn)` / `wrapper.run_item(id, fn)` | classifier-driven, logged per attempt |
| Try Catch / Throw (Business vs System exception) | `raise wrapper.DataError(...)` / `raise queue.BusinessError(...)` (business); any other error is system | |
| Log Message | `wrapper.log_event(...)`, `wrapper.log_item(...)` | structured, redactable |
| Invoke Workflow File | an ordinary Python function / module import | |
| Invoke Python/Powershell | `subprocess.run([...])` (argv list, no `shell=True`) | |
| Orchestrator queue trigger | `poll` schedule slot + `queue.run_queue` | |
| Orchestrator time trigger | schedule `slots` in the registry manifest | |

## Examples

```python
# Excel -> queue -> performer (REFramework in ~25 lines)
@wrapper.entrypoint
def main():
    q = queue.open("expenses")
    for row in excel.read_xlsx(files.new_files(inbox, "*.xlsx", "expenses", mark=True)[0]):    # dispatcher
        q.add(str(row["id"]), row)
    summary = queue.run_queue(q, post_expense, max_seconds=1500)                               # performer
    wrapper.set_result(**summary)

def post_expense(item):
    r = http.client("https://erp.example.internal", auth=http.OAuth2ClientCredentials(
        token_url, *credentials.get_credential("erp-api"))).post("/expenses", json=item.payload)
    return {"erp_id": r.json()["id"]}                          # DataError (4xx) => business; 5xx/network => system, retried
```

```python
# Desktop app without an API
with windows_ui.session():
    windows_ui.launch(r"C:\Program Files\Legacy\app.exe")
    win = windows_ui.find_window(r"Legacy App", timeout=30)
    windows_ui.set_text(windows_ui.find(win, automation_id="txtCustomer", required=True), customer_id)
    windows_ui.click(windows_ui.find(win, name="Search", control_type="Button", required=True))
    result = windows_ui.get_text(windows_ui.find(win, automation_id="lblBalance", timeout=20, required=True))
```

## Optional dependencies (all guarded: you get a `NotAvailable` that says what to install)

| Feature | Package / component | Why optional |
|---|---|---|
| `pdf.extract_text(engine="pdfminer")` and the `auto` fallback | `pdfminer.six` | complex PDFs; the built-in extractor covers common text PDFs |
| `images.ocr_image` | `pytesseract`, `Pillow`, the Tesseract engine | native component, large |
| `images.find_on_screen` | `pyautogui`, `Pillow`, `opencv-python` (for `confidence`) | image automation is fragile; opt in deliberately |
| `mail.outlook_*`, `windows_ui`, `notify.clipboard`, `db.query_odbc` | Windows PowerShell 5.1 (in-box); Outlook / ODBC drivers for those | Windows only |

Add them to the execution-framework's embedded Python only for the automations that need them.

## Honest gaps (what UiPath does that this library does not)

* **Citrix / RDP-published apps and other bitmap-only UIs.** There is no UI tree to read. Options: automate the app's own API,
  run the automation *inside* the remote session, or use `images.find_on_screen` as a last resort. Budget for these separately.
* **SAP GUI.** Use SAP GUI Scripting (COM `SAPFEWSELib`) from PowerShell/Python, or SAP's APIs (RFC/OData). Not wrapped here yet.
* **Mainframe / terminal emulators (3270/5250).** Use the emulator's HLLAPI/EHLLAPI or a TN3270 library; not wrapped here.
* **Java Swing/JavaFX apps** expose little UI Automation without the Java Access Bridge enabled.
* **Document Understanding (trained ML extractors, validation stations).** Use `llm_call` extraction with grounding checks
  (see `automation-invoice-router`) and `queue.needs_review` for human validation; there is no model-training studio.
* **Attended automation / hotkey-triggered robots and a tray assistant.** By design (unattended by default); use triggers.
* **Excel formatting, charts, pivots, macros, `.xls`/`.xlsb`.** The pure-Python Excel layer reads/writes data only. For
  formatting-heavy output generate a template with Excel once and fill it with an Office COM script, or produce CSV/HTML.
* **Multi-machine queue distribution.** The queue is single-machine (SQLite). That fits the unattended single-VDI model.
* **A visual designer/recorder for desktop apps.** Browser flows: `playwright codegen`. Desktop: inspect with Windows
  `inspect.exe`/Accessibility Insights, then use `windows_ui.find(...)` selectors.
