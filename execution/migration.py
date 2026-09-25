"""One-time import from the last exclusively owned portfolio. Never infer shares
from new target weights. Unknown/retired positions remain explicit legacy assets.
"""
from decimal import Decimal
from copy import deepcopy
from .ledger import SafetyStop,new_state,dec,text,reconcile_state

OLD_UNIVERSES={
 'aaa':['NTSD','SAA','EET','UBT','UST','UGL','DBC','SHV'],
 'world_trend':['WLDU','UGLD','USFR'],
 'mix8':['SSO','QLD','EFO','EEM','GLD','IEF','TLT','KMLM','SGOV'],
 'spx_trend':['SPXL','BIL'],
}


def bootstrap_state(account,positions,open_orders,universes,old_states,env,as_of):
    if open_orders: raise SafetyStop('Migration requires no open broker orders')
    holdings={k:{} for k in universes};holdings['legacy']={}
    basis={k:{} for k in holdings};values={k:Decimal(0) for k in holdings}
    for row in positions:
        sym=row['symbol'];qty=dec(row['qty'])
        if qty<0: raise SafetyStop('Short positions require explicit migration')
        owners=[k for k,syms in universes.items() if sym in syms]
        if len(owners)>1: raise SafetyStop(f'Ambiguous migration ownership: {sym}')
        key=owners[0] if owners else 'legacy'
        holdings[key][sym]=text(qty);basis[key][sym]=text(row['cost_basis'])
        values[key]+=dec(row['market_value'])
    cash=dec(account['cash']);debt={};debt_left=max(Decimal(0),-cash)
    invested=sum(values.values());nonempty=[k for k,v in values.items() if v>0]
    if debt_left and not invested: raise SafetyStop('Debt without positions')
    for i,key in enumerate(nonempty):
        amount=debt_left if i==len(nonempty)-1 else (max(Decimal(0),-cash)*values[key]/invested).quantize(Decimal('.000000001'))
        debt[key]=text(amount);debt_left-=amount
    meta={}
    for key in holdings:
        before=deepcopy(old_states.get(key,{}))
        gross=values[key];net=gross-dec(debt.get(key,0))
        # Preserve the pre-migration percentage drawdown on the now-net NAV.
        peak=dec(before.get('peak_nav',gross) or gross)
        before['peak_nav']=text(peak*net/gross if gross>0 else Decimal(0))
        before['legacy_total_invested']=before.pop('total_invested',None)
        before['migration_nav']=text(net)
        for field in ('current_positions','current_values','position_cost_basis'):
            before.pop(field,None)
        meta[key]=before
    s=new_state(account['id'],env,holdings,{'reserve':text(max(Decimal(0),cash))},debt,as_of,meta)
    for key in holdings:s['portfolios'][key]['basis']=basis[key]
    s['migration_policy']='exclusive holdings; retired assets legacy; initial debt proportional to actual gross value; cash reserve'
    reconcile_state(s,positions,account['cash'])
    return s
