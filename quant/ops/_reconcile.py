"""launchd-Entry: reconciliert beide Sleeves nach dem US-Open."""
from quant.execution import onx_live, xsr_live
for name, fn in [("xsr", xsr_live.reconcile), ("onx", onx_live.reconcile)]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"{name} reconcile fehlgeschlagen: {e}")
