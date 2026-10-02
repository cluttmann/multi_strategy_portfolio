"""Compact durable result outbox, committed with the financial run archive."""
from .ledger import dec,text


def queue_result(state,run,status):
    run_id=run['id']
    if run_id in state.get('reported_runs',[]):return
    pending=state.setdefault('pending_notifications',{})
    if run_id in pending:return
    if status=='complete':
        fills=[f"{r['strategy']}: {r['side']} {r.get('booked_qty','0')} {r['symbol']} (${dec(r.get('booked_value',0)):.2f})" for r in run['orders']]
        message='✅ Shared ETF execution ('+state['env']+'): '+run['action']+' '+run['period']+'\n'+'\n'.join(fills or ['Keine Trades nötig.'])+'\nBroker und Strategiebestände abgeglichen.'
    else:
        from .monthly import remaining
        remainder=[]
        for intent in run.get('intents',[]):
            left=remaining(run,intent)
            if left>0 and intent.get('status') in ('expired','invalidated'):
                amount=f"${left:.2f}" if intent['side']=='buy' else f"{text(left)} shares"
                remainder.append(f"{intent['strategy']}: {intent['side']} {intent['symbol']} {amount} ({intent.get('reason','expired')})")
        message='⚠️ Monthly execution remainder expired ('+state['env']+'): '+run['period']+'\n'+'\n'.join(remainder or ['Unfilled monthly remainder stopped.'])+'\nConfirmed holdings and sleeve cash preserved; month not marked clean.'
    pending[run_id]={'run_id':run_id,'action':run['action'],'period':run['period'],'status':status,
                     'clean':not run.get('margin_retry'), 'message':message,'attempts':0}
