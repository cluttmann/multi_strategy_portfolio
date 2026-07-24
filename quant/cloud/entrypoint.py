"""Cloud-Run-Job-Entrypoint — dispatcht per TASK-Env auf die Executors.

Ein Container, sieben Zeitfenster. Cloud Scheduler setzt TASK und triggert
eine Job-Execution. Der Handelstag-Guard in den Executors macht Feiertage
zu No-Ops; Secrets kommen als Env aus Secret Manager.

TASK ∈ {dailyops, xsr-plan, xsr-execute, onx-exit, onx-decide, onx-enter,
        reconcile, sleeve-health, etf-rebalance, etf-reconcile}
"""

import os
import sys
import traceback


def main():
    task = os.environ.get("TASK", "").strip()
    print(f"=== quant cloud task: {task} ===", flush=True)
    try:
        if task == "dailyops":
            import subprocess
            subprocess.run(["/bin/bash", "quant/ops/daily.sh"], check=False)
        elif task == "xsr-plan":
            from quant.execution import xsr_live
            xsr_live.plan(dry_run=False)
        elif task == "xsr-execute":
            from quant.execution import xsr_live
            xsr_live.execute(dry_run=False)
        elif task == "onx-exit":
            from quant.execution import onx_live
            onx_live.exit_(dry_run=False)
        elif task == "onx-decide":
            from quant.execution import onx_live
            onx_live.decide(dry_run=False)
        elif task == "onx-enter":
            from quant.execution import onx_live
            onx_live.enter(dry_run=False)
        elif task == "etf-rebalance":
            from quant.execution import etf_sleeves
            for sl in ("volc", "eomt"):
                try:
                    etf_sleeves.rebalance(sl, dry_run=False)
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001
                    print(f"{sl} rebalance: {e}")
        elif task == "etf-reconcile":
            from quant.execution import etf_sleeves
            for sl in ("volc", "eomt"):
                try:
                    etf_sleeves.reconcile(sl)
                except Exception as e:  # noqa: BLE001
                    print(f"{sl} reconcile: {e}")
        elif task == "sleeve-health":
            from quant.ops import sleeve_health
            sleeve_health.check(alert=True)
        elif task == "reconcile":
            from quant.execution import onx_live, xsr_live
            for name, fn in [("xsr", xsr_live.reconcile),
                             ("onx", onx_live.reconcile)]:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    print(f"{name} reconcile: {e}")
        else:
            print(f"unbekannter TASK: {task!r}")
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        # Nicht-Null-Exit → Cloud Run markiert Execution als failed (Alerting)
        sys.exit(1)
    print(f"=== task {task} fertig ===", flush=True)


if __name__ == "__main__":
    main()
