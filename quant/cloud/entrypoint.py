"""Cloud-Run-Job-Entrypoint — dispatcht per TASK-Env auf die Executors.

Ein Container, sieben Zeitfenster. Cloud Scheduler setzt TASK und triggert
eine Job-Execution. Der Handelstag-Guard in den Executors macht Feiertage
zu No-Ops; Secrets kommen als Env aus Secret Manager.

TASK ∈ {dailyops, xsr-plan, xsr-execute, onx-exit, onx-decide, onx-enter,
        reconcile, sleeve-health, etf-rebalance, etf-reconcile,
        cost-monitor, discovery}
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
            # Generischer Executor über das Sleeve-Register: jeder validierte
            # Sleeve ist automatisch live, ohne neuen Code.
            from quant.execution import generic_sleeve, risk
            from quant.sleeves.registry import REGISTRY
            # Der Konto-Gross-Deckel wird über ALLE Sleeves dieses Laufs
            # kumuliert — deshalb einmal am Anfang zurücksetzen, nicht je Sleeve.
            risk.reset_run_state()
            for sl in REGISTRY:
                try:
                    generic_sleeve.rebalance(sl, dry_run=False)
                except SystemExit as e:
                    # guard_or_exit() exits(0) to skip a single paused/non-
                    # trading-day sleeve — that must not abort the sleeves
                    # after it in the loop. Found 2026-07-29: since VOLC was
                    # paused, every etf-rebalance run re-raised this and
                    # never reached eomt/dtrd — both sat at zero positions
                    # since their first scheduled live day (2026-07-27).
                    if e.code not in (0, None):
                        raise
                except Exception as e:  # noqa: BLE001
                    print(f"{sl} rebalance: {e}")
        elif task == "etf-reconcile":
            from quant.execution import generic_sleeve
            from quant.sleeves.registry import REGISTRY
            for sl in REGISTRY:
                try:
                    generic_sleeve.reconcile(sl)
                except Exception as e:  # noqa: BLE001
                    print(f"{sl} reconcile: {e}")
        elif task == "cost-monitor":
            from quant.ops import cost_monitor
            cost_monitor.check(days=7, alert=True)
        elif task == "sleeve-health":
            from quant.ops import sleeve_health
            sleeve_health.check(alert=True)
        elif task == "discovery":
            # Gates laufen in der Cloud, Beförderung bleibt ein Commit
            # (siehe discovery.run docstring: kein persistentes Dateisystem,
            # und eine neue Strategie soll nicht ungeprüft scharf gehen).
            from quant.execution.telegram import notify
            from quant.research import discovery
            discovery.run(run_all=True, promote_enabled=False,
                          notify_fn=notify)
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
