"""Snapshot, bootstrap, preview, execute and reconcile shared ETF ownership.

All broker writes use the deployed execution package. Output contains personal
financial data and must be outside git. No raw credentials are written.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import main as bot
from execution.broker import AlpacaBroker
from execution.controller import Controller
from execution.ledger import FirestoreStore,SafetyStop,reconcile_state
from execution.migration import bootstrap_state,OLD_UNIVERSES


def snapshot(broker,db,env):
    first=broker.open_orders()
    positions=broker.positions();account=broker.account();clock=broker.clock()
    second=broker.open_orders()
    if first or second: raise SafetyStop('Open orders must be resolved before migration')
    return {'account':account,'positions':positions,'open_orders':second,'clock':clock,
            'old_states':{d.id:d.to_dict() for d in db.collection(f'strategy-balances-{env}').stream()}}


def run(args):
    api=bot.set_alpaca_environment(args.env,use_secret_manager=False)
    broker=AlpacaBroker(api);db=bot.get_firestore_client()
    directory=Path(args.output).resolve();directory.mkdir(parents=True,exist_ok=True)
    at=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if args.action in ('snapshot','initialize'):
        snap=snapshot(broker,db,args.env)
        (directory/f'{args.env}-{at}-broker.json').write_text(json.dumps(snap,indent=2,default=str))
        state=bootstrap_state(snap['account'],snap['positions'],snap['open_orders'],OLD_UNIVERSES,
                              snap['old_states'],args.env,snap['clock']['timestamp'])
        (directory/f'{args.env}-{at}-ledger.json').write_text(json.dumps(state,indent=2,default=str))
        if args.action=='initialize':
            store=FirestoreStore(db,snap['account']['id'],args.env)
            store.initialize(state)
            reread=store.read()
            if reread!=state: raise SafetyStop('Ledger readback differs from bootstrap')
            reconcile_state(reread,broker.positions(),broker.account()['cash'])
        return {'status':args.action,'snapshot_directory':str(directory),'portfolios':{k:p['positions'] for k,p in state['portfolios'].items()}}
    c=Controller(bot,api,args.env)
    if args.action=='preview':
        s=c.store.read();plan=c.build(s,'mix8-upgrade',args.period)
        (directory/f'{args.env}-{at}-preview.json').write_text(json.dumps(plan,indent=2,default=str))
        return plan
    if args.action=='execute':return c.execute('mix8-upgrade',period=args.period)
    if args.action=='resume':return c.execute('reconcile')
    if args.action=='reconcile':
        t=c.ledger.acquire()
        try:return c.executor.recover(t)
        finally:c.ledger.release(t)
    if args.action=='authorize':
        def authorize(s):
            if s.get('active'):raise SafetyStop('Resolve active plan before authorization')
            s.setdefault('pending_actions',{})['mix8-upgrade']=args.period
        c.store.mutate('authorize_mix8_upgrade',authorize)
        return {'status':'authorized','period':args.period}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',choices=['live','paper'],required=True)
    parser.add_argument('--action',choices=['snapshot','initialize','preview','execute','reconcile','resume','authorize'],required=True)
    parser.add_argument('--period',default='2026-09-25')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(run(args),indent=2,default=str))
