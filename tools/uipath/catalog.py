"""Static knowledge base for the inventory: UiPath activity -> category, complexity weights, AutoStation equivalents.

Edit this file (not the scanner) to teach the kit about more activities; unknown activities are still counted ("other").
"""
from __future__ import annotations

CATEGORIES = [
    ("browser_ui", "Browser UI"),
    ("desktop_ui", "Desktop UI"),
    ("image_citrix", "Image / Citrix / OCR"),
    ("sap_terminal", "SAP / terminal"),
    ("excel_data", "Excel / data tables"),
    ("mail", "Mail"),
    ("pdf_document", "PDF / document"),
    ("http_api", "HTTP / API"),
    ("database", "Database"),
    ("file_system", "File system"),
    ("queues_assets", "Queues / assets / credentials"),
    ("ai_ml", "AI / ML"),
    ("orchestration", "Orchestration / resilience"),
    ("control_flow", "Control flow"),
    ("other", "Other"),
]
CATEGORY_LABEL = dict(CATEGORIES)

# "tuning knobs": complexity points per activity in each category
WEIGHTS = {
    "browser_ui": 1.0, "desktop_ui": 1.5, "image_citrix": 4.0, "sap_terminal": 3.0, "excel_data": 0.5, "mail": 0.7,
    "pdf_document": 1.5, "http_api": 0.8, "database": 0.8, "file_system": 0.3, "queues_assets": 0.6, "ai_ml": 2.0,
    "orchestration": 0.2, "control_flow": 0.05, "other": 0.3,
}
WORKFLOW_POINTS = 2.0

# activities whose category depends on the selector kind (browser vs desktop) used in the same workflow
UI_INTERACTION = {
    "Click", "TypeInto", "GetText", "SetText", "SelectItem", "Check", "Hover", "DoubleClick", "RightClick", "SendHotkey",
    "ElementExists", "FindElement", "WaitElementVanish", "GetAttribute", "SetFocus", "GetFullText", "ExtractStructuredData",
    "ScreenScraping", "NClick", "NTypeInto", "NGetText", "NSelectItem", "NCheck", "NHover", "NKeyboardShortcuts", "NMouseScroll",
    "NCheckState", "NExtractData", "NApplicationCard", "NSetText", "NDoubleClick", "NRightClick", "NGetAttribute", "NSetFocus",
    "TypeSecureText", "NTypeSecureText", "SecureTypeInto", "DragAndDrop", "SetToClipboard", "GetFromClipboard", "ClickTrigger", "MouseTrigger", "SetClipping", "CheckState",
}
BROWSER_ACTIVITIES = {"OpenBrowser", "AttachBrowser", "NavigateTo", "CloseTab", "GoBack", "GoForward", "Refresh", "BrowserScope",
                      "NGoToUrl", "NGetUrl", "NNavigateBrowser", "InjectJsScript", "GetBrowserData", "SetBrowserData",
                      "ExecuteJavaScript"}
DESKTOP_ACTIVITIES = {"OpenApplication", "AttachWindow", "CloseApplication", "CloseWindow", "MaximizeWindow", "MinimizeWindow",
                      "MoveWindow", "RestoreWindow", "GetActiveWindow", "SendWindowMessages", "NCloseApplication"}
IMAGE_ACTIVITIES = {"ClickImage", "FindImage", "FindImageMatches", "ImageExists", "HoverImage", "DoubleClickImage", "TypeIntoImage",
                    "ClickText", "HoverText", "FindText", "TextExists", "GetOCRText", "GetFullText", "OCRScope", "WaitImageVanish",
                    "ClickOCRText", "FindOCRText", "HoverOCRText", "OCRTextExists", "ImageToText", "ComputerVisionScope", "CVClick",
                    "CVTypeInto", "CVGetText", "CVHover", "CVScreenScope"}
SAP_TERMINAL_PREFIXES = ("SAP", "Terminal", "Mainframe")

EXCEL = {"ExcelApplicationScope", "ExcelReadRange", "ExcelWriteRange", "ReadRange", "WriteRange", "AppendRange", "ReadCsv",
         "WriteCsv", "BuildDataTable", "FilterDataTable", "JoinDataTables", "LookupDataTable", "MergeDataTable", "SortDataTable",
         "RemoveDuplicateRows", "OutputDataTable", "ForEachRow", "AddDataRow", "AddDataColumn", "GetRowItem", "RemoveDataColumn",
         "ExcelProcessScopeX", "UseExcelFile", "ReadRangeX", "WriteRangeX", "WriteCell", "ReadCell", "InvokeVBA", "ExcelGetWorkbookSheets",
         "CopyPasteRange", "FindFirstLast", "ExcelForEachRow", "GenerateDataTable", "ForEachSheet", "ExcelReadCell", "ExcelWriteCell"}
MAIL = {"SendMail", "SendOutlookMail", "GetImapMailMessage", "GetOutlookMailMessages", "GetPOP3MailMessages", "GetIMAPMailMessages",
        "SaveMailMessage", "MoveMailMessage", "OutlookApplicationCard", "MarkMailAsRead", "MarkMailAsUnread", "ReplyToOutlookMail",
        "ForwardOutlookMail", "DeleteMail", "SaveMailAttachments", "SendMailX", "GetExchangeMailMessages", "SendSmtpMail", "SendHtmlMail",
        "ReplyToMail", "ForwardMail", "GetMailMessages"}
PDF = {"ReadPDFText", "ReadPDFWithOCR", "ExtractPDFText", "ExtractPDFPageRange", "GetPDFPageCount", "ManagePDFPassword", "JoinPDF",
       "ExtractImagesFromPDF", "ReadPDFPageText", "DigitizeDocument"}
HTTP = {"HttpClient", "SOAPRequest", "DeserializeJson", "SerializeJson", "DownloadFile", "InvokeRestApi", "GetNetworkCredential",
        "DeserializeXml", "ExecuteXPath", "UploadFile", "NetHttpRequest", "HttpRequest", "SftpPut", "SftpGet", "FtpPut", "FtpGet",
        "FtpUpload", "FtpDownload", "WebRequest"}
DATABASE = {"DatabaseConnect", "ExecuteQuery", "ExecuteNonQuery", "DatabaseDisconnect", "BulkInsert", "DatabaseTransaction",
            "ExecuteDBQuery", "RunQuery", "ConnectToDatabase"}
FILE_SYSTEM = {"CopyFile", "MoveFile", "DeleteFile", "CreateDirectory", "PathExists", "ReadTextFile", "WriteTextFile", "AppendLine",
               "CompressFiles", "ExtractFiles", "ZipFiles", "UnzipFiles", "ForEachFileFolder", "GetFiles", "Rename", "RenameFile",
               "FileChangeTrigger", "GetFolderContents", "CopyFolder", "DeleteFolder", "MoveFolder", "ReadBinary", "WriteBinary",
               "ForEachFile", "SelectFile", "SelectFolder", "CompressZipFiles", "ExtractUnzipFiles", "FileExists", "FolderExists"}
QUEUES = {"AddQueueItem", "BulkAddQueueItems", "GetQueueItem", "GetQueueItems", "GetTransactionItem", "SetTransactionStatus",
          "SetTransactionProgress", "PostponeTransactionItem", "AddTransactionItem", "WaitQueueItem", "GetRobotCredential",
          "GetRobotAsset", "SetRobotAsset", "GetCredential", "GetSecureCredential", "GetAsset", "SetAsset", "DeleteQueueItems",
          "GetQueueItemsByStatus", "AddQueueItemAndGetReference", "GetJobs", "GetAssetValue", "SetCredential", "GetStorageText",
          "UploadStorageFile", "DownloadStorageFile", "ReadStorageText", "WriteStorageText", "ListStorageFiles", "DeleteStorageFile",
          "GetBucketFile", "WriteBucketFile"}
AI_ML = {"MLSkill", "ClassifyDocument", "ExtractDocumentData", "DocumentUnderstanding", "PresentValidationStation", "TrainClassifiers",
         "TrainExtractors", "MachineLearningExtractor", "IntelligentOCR", "TaxonomyManager", "ChatCompletion", "ChatGPT", "ExtractDocument",
         "ClassifyDocumentScope", "MLClassifier", "TextAnalysis", "SentimentAnalysis"}
ORCHESTRATION = {"InvokeWorkflowFile", "InvokeProcess", "StartJob", "StateMachine", "State", "Transition", "Parallel", "ParallelForEach",
                 "RetryScope", "TryCatch", "Throw", "Rethrow", "Delay", "LogMessage", "ShouldStop", "KillProcess", "TerminateWorkflow",
                 "Comment", "CommentOut", "Break", "Continue", "InvokeCode", "InvokePowerShell", "InvokeMethod", "RunOrchestratorJob",
                 "StartJobAndGetReference", "AddLogFields", "RemoveLogFields", "Pick", "PickBranch", "Persist", "ReportStatus",
                 "InvokeWorkflowInteractive", "ActivityTrigger"}
CONTROL_FLOW = {"Sequence", "Flowchart", "FlowStep", "FlowDecision", "FlowSwitch", "If", "IfElseIf", "Switch", "ForEach", "While",
                "DoWhile", "Assign", "MultiAssign", "Add", "Subtract", "Multiply", "Divide", "StateMachine_", "Interrupt", "WriteLine",
                "Compare", "ExpressionStatement", "Switch`1", "ForEach`1", "Case", "Default", "InvokeDelegate", "CancellationScope",
                "CompensableActivity", "TransactionScope", "NoPersistScope", "TimerTrigger", "Scope", "ExpressionNode", "TypeArguments"}

ATTENDED = {"InputDialog", "MessageBox", "SelectFile", "SelectFolder", "CustomInput", "Form", "WaitForUserInput", "ShowForm",
            "ShowDialog", "InteractiveForm", "UserInput", "InputBox", "PresentValidationStation", "CreateFormTask", "WaitForFormTaskAndResume",
            "CreateExternalTask", "WaitForExternalTaskAndResume", "ClickTrigger", "MouseTrigger", "KeyPressTrigger", "SystemTrigger",
            "HotkeyTrigger"}
# Action Center / attended-only activities are tagged: human-in-the-loop (maps to queue needs_review)
HUMAN_IN_LOOP = {"PresentValidationStation", "CreateFormTask", "WaitForFormTaskAndResume", "CreateExternalTask", "WaitForExternalTaskAndResume"}

# --- AutoStation equivalents (names as they exist in this repo) ---------------------------------------------
EQUIV = {
    "OpenBrowser": "fw.browser.launch(...) + page.goto(url)",
    "AttachBrowser": "fw.browser.attach(cdpUrl)",
    "NavigateTo": "page.goto(url) (retry via w.runWithRetry)",
    "NGoToUrl": "page.goto(url)",
    "Click": "fw.resilience.resilientPage(page).click(selector)",
    "NClick": "fw.resilience.resilientPage(page).click(selector)",
    "TypeInto": "resilientPage(page).fill(selector, value)  /  wrapper_core.activities.windows_ui.set_text(...) on desktop",
    "NTypeInto": "resilientPage(page).fill(selector, value)",
    "GetText": "resilientPage(page).textContent(selector)  /  windows_ui.get_text(...)",
    "NGetText": "resilientPage(page).textContent(selector)",
    "TypeSecureText": "prefer SSO (Edge profile); else credentials.get_credential('AUTOMAGIX/<name>') then resilientPage.fill (never log it)",
    "SelectItem": "resilientPage(page).selectOption(selector, value)",
    "Check": "resilientPage(page).check(selector)",
    "ElementExists": "page.locator(selector).count() > 0  /  windows_ui.exists(...)",
    "ExtractStructuredData": "fw.adapters.get('generic-table').scrapeTable(page, {...})",
    "NExtractData": "fw.adapters.get('generic-table').scrapeTable(page, {...})",
    "SendHotkey": "page.keyboard.press(...)  /  windows_ui.send_keys(...)",
    "OpenApplication": "wrapper_core.activities.windows_ui.launch(path)",
    "AttachWindow": "wrapper_core.activities.windows_ui.find_window(title=...)",
    "CloseApplication": "wrapper_core.activities.windows_ui.close(window)",
    "ClickImage": "NO DIRECT EQUIVALENT (image-based). Prefer an API/DOM/UIA path; see docs/UIPATH_MIGRATION.md (Computer Vision / Citrix)",
    "FindImage": "NO DIRECT EQUIVALENT (image-based). See docs/UIPATH_MIGRATION.md",
    "GetOCRText": "optional OCR hook (wrapper_core.activities.pdf/ocr) - verify availability on the VDI image",
    "ReadPDFText": "wrapper_core.activities.pdf.extract_text(path)",
    "ReadPDFWithOCR": "wrapper_core.activities.pdf.extract_text(path) + OCR hook (optional)",
    "ExcelApplicationScope": "wrapper_core.activities.excel.read_xlsx / write_xlsx (file-based, no Excel install needed)",
    "ExcelReadRange": "wrapper_core.activities.excel.read_xlsx(path, sheet)",
    "ReadRange": "wrapper_core.activities.excel.read_xlsx(path, sheet)",
    "ExcelWriteRange": "wrapper_core.activities.excel.write_xlsx(path, {sheet: rows})",
    "WriteRange": "wrapper_core.activities.excel.write_xlsx(path, {sheet: rows})",
    "AppendRange": "wrapper_core.activities.excel.append_rows(...)",
    "ReadCsv": "wrapper_core.activities.excel.read_csv(path)",
    "WriteCsv": "wrapper_core.activities.excel.write_csv(path, rows)",
    "FilterDataTable": "list comprehension / wrapper_core.activities.excel.filter_rows(...)",
    "JoinDataTables": "wrapper_core.activities.excel.join(left, right, on=...)",
    "SortDataTable": "sorted(rows, key=...)",
    "LookupDataTable": "dict lookup / wrapper_core.activities.excel.lookup(...)",
    "BuildDataTable": "list of dicts",
    "ForEachRow": "for row in rows: wrapper.run_item(id, fn, row)",
    "InvokeVBA": "NO DIRECT EQUIVALENT (macros). Re-implement the macro logic in Python",
    "SendMail": "wrapper_core.activities.mail.send_smtp(...)",
    "SendSmtpMail": "wrapper_core.activities.mail.send_smtp(...)",
    "SendOutlookMail": "wrapper_core.activities.mail.send_outlook(...)  (Outlook via PowerShell COM) or send_smtp",
    "GetImapMailMessage": "wrapper_core.activities.mail.read_imap(...)",
    "GetOutlookMailMessages": "wrapper_core.activities.mail.read_outlook(...)",
    "SaveMailAttachments": "wrapper_core.activities.mail (attachments returned by read_imap/read_outlook)",
    "HttpClient": "wrapper_core.activities.http.request(...)  /  fw.http.client(...)",
    "DeserializeJson": "json.loads(...)",
    "SerializeJson": "json.dumps(...)",
    "SOAPRequest": "wrapper_core.activities.http.request(...) with an XML body",
    "DownloadFile": "wrapper_core.activities.http.download(url, path)",
    "DatabaseConnect": "wrapper_core.activities.db.connect(...)",
    "ExecuteQuery": "wrapper_core.activities.db.query(conn, sql, params)",
    "ExecuteNonQuery": "wrapper_core.activities.db.execute(conn, sql, params)",
    "CopyFile": "wrapper_core.activities.files.safe_copy(...)",
    "MoveFile": "wrapper_core.activities.files.safe_move(...)",
    "DeleteFile": "pathlib.Path.unlink()",
    "PathExists": "pathlib.Path.exists()",
    "ReadTextFile": "pathlib.Path.read_text()",
    "WriteTextFile": "pathlib.Path.write_text()",
    "ForEachFileFolder": "wrapper_core.activities.files.watch_new(folder, pattern)",
    "FileChangeTrigger": "a poll slot + wrapper_core.activities.files.watch_new(...)",
    "CompressFiles": "wrapper_core.activities.files.zip_files(...)",
    "AddQueueItem": "queue.open(name).add(reference, payload, priority=...)",
    "BulkAddQueueItems": "queue.open(name).add(...) in a loop",
    "GetQueueItem": "queue.open(name).next()",
    "GetTransactionItem": "queue.open(name).next()",
    "SetTransactionStatus": "q.succeed(item) / q.fail_business(item, reason) / q.fail_system(item, reason, max_retries=n)",
    "PostponeTransactionItem": "q.add(..., defer_until=...)",
    "GetRobotCredential": "wrapper_core.activities.credentials.get_credential('AUTOMAGIX/<name>')",
    "GetCredential": "wrapper_core.activities.credentials.get_credential(...)",
    "GetRobotAsset": "wrapper.assets()['name']  (non-secret) / credentials.get_credential (secret)",
    "GetAsset": "wrapper.assets()['name']",
    "SetRobotAsset": "NO DIRECT EQUIVALENT (assets are manifest-managed via MR); keep state in the automation's data dir",
    "InvokeWorkflowFile": "a normal function/module call",
    "InvokeProcess": "wrapper.emit_trigger(target, params) (event-driven orchestration)",
    "StartJob": "wrapper.emit_trigger(target, params)  or  registry trigger-add",
    "RetryScope": "wrapper.run_with_retry(fn)  (manifest error_handling policy)",
    "TryCatch": "try/except with wrapper.DataError / wrapper.Escalation",
    "Throw": "raise wrapper.DataError(...) (business) or a normal exception (system)",
    "Delay": "time.sleep(...) / poll with timeout",
    "LogMessage": "wrapper.log_event(name, {...})",
    "InputDialog": "ATTENDED: replace with a schedule-slot param (wrapper.param), a file or a rule",
    "MessageBox": "ATTENDED: replace with wrapper.log_event / alerting",
    "SelectFile": "ATTENDED: replace with a param or watch folder",
    "SelectFolder": "ATTENDED: replace with a param",
    "CreateFormTask": "q.needs_review(item, reason)  (human review surfaced on the dashboard)",
    "WaitForFormTaskAndResume": "q.needs_review(item, reason) + a follow-up run via registry trigger-add",
    "PresentValidationStation": "q.needs_review(item, reason)",
    "ClassifyDocument": "wrapper.llm_call(..., schema=...) with grounding (LLM pattern 1)",
    "ExtractDocumentData": "wrapper.llm_call(..., schema=...) with grounding (LLM pattern 1)",
    "MLSkill": "wrapper.llm_call(...) or a deterministic model call; check data classification",
    "InvokeCode": "native Python/JS code",
    "InvokePowerShell": "subprocess with an argv list (no shell=True)",
}
CONTROL_NATIVE = "native Python/JS control flow"
SAP_EQUIV = "wrapper_core.activities.windows_ui (UIA) - or, better, SAP RFC/OData API via wrapper_core.activities.http; see docs/UIPATH_MIGRATION.md"
TERMINAL_EQUIV = "NO DIRECT EQUIVALENT (terminal/mainframe screen scraping): use the host's API/MQ/file interface or redesign"

PATTERN_LABEL = {
    "headless-api": "Headless API / file based (Python, wrapper-core)",
    "playwright-browser": "Playwright browser automation (Node, execution-framework)",
    "windows-uia": "Windows UI Automation desktop (Python, activities.windows_ui)",
    "queue-transactional": "Queue-based transactional (REFramework equivalent)",
    "needs-redesign": "Needs redesign / manual analysis",
}
PATTERN_MULTIPLIER = {"headless-api": 0.8, "playwright-browser": 1.0, "queue-transactional": 1.2, "windows-uia": 1.5, "needs-redesign": 2.5}

CLASSIFICATION_KEYWORDS = {
    "restricted": ["ssn", "socialsecurity", "passport", "patient", "hipaa", "medical", "creditcard", "cardnumber", "cvv", "classified"],
    "confidential": ["salary", "payroll", "iban", "bankaccount", "accountnumber", "taxid", "customer", "contract", "employee",
                     "invoice", "payment", "bank", "dob", "birth"],
}
