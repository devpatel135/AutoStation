#!/usr/bin/env python3
"""(Re)generate the binary fixtures of the SYNTHETIC sample projects (currently only Data/Config.xlsx). Deterministic."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from xlsxmini import write_xlsx  # noqa: E402


def main() -> None:
    write_xlsx(HERE / "Invoice_Processing_REF" / "Data" / "Config.xlsx", {
        "Settings": [
            ["Name", "Value", "Description"],
            ["OrchestratorQueueName", "InvoiceQueue", "Queue the performer reads"],
            ["logF_BusinessProcessName", "InvoiceProcessing", "Business process name used in logs"],
            ["MaxRetryNumber", 3, "REFramework retries per transaction"],
            ["SMTP_Server", "smtp.example.internal", "Mail relay"],
            ["SMTP_Password", "FAKE-SYNTHETIC-SECRET-1234", "SYNTHETIC hard-coded secret (the inventory must redact this)"],
            ["ApprovedVendorsPath", "\\\\fileshare\\finance\\approved_vendors.xlsx", "Approved vendor list"],
        ],
        "Constants": [
            ["Name", "Value", "Description"],
            ["MaxConsecutiveSystemExceptions", 3, "Stop after N system exceptions in a row"],
            ["RetryNumberGetTransactionItem", 3, "Retries when the queue read fails"],
        ],
        "Assets": [
            ["Name", "OrchestratorAssetName", "Description"],
            ["SMTP_Credential", "SMTP_CredentialAsset", "Credential asset for the mail relay"],
        ],
    })
    print("wrote Invoice_Processing_REF/Data/Config.xlsx")


if __name__ == "__main__":
    main()
