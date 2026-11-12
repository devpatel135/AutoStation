from . import transport
from .cli import main

# A real run is one process: queue its telemetry (run record, heartbeats, lifecycle) in the durable spool and send it as ONE commit when the
# process ends. Tests and library users never go through here, so they keep the immediate one-artefact-per-send behaviour.
transport.enable_batching()
try:
    code = main()
finally:
    transport.flush_batch()
raise SystemExit(code)
