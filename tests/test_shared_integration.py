from decimal import Decimal
import pytest
import main
from execution.ledger import new_state, SafetyStop
from execution.controller import position_value, target_orders


def test_shared_position_value_uses_own_shares():
    s=new_state('x','paper',{'aaa':{'EET':'5'},'mix8':{'EET':'8'}},{}, {}, '2026-09-25T00:00:00Z')
    assert position_value(s,'mix8',{'EET':Decimal('100')})['total_value']==800
    assert position_value(s,'aaa',{'EET':Decimal('100')})['total_value']==500


def test_shared_exit_sells_only_own_position_and_buys_with_worst_proceeds():
    s=new_state('x','paper',{'world_trend':{'UGLD':'10'},'mix8':{'UGLD':'20'}},{}, {}, '2026-09-25T00:00:00Z')
    assets={t:{'tradable':True,'fractionable':True,'status':'active'} for t in ['UGLD','USFR']}
    rows=target_orders(s,{'world_trend':{'USFR':1000}}, {'UGLD':Decimal('100'),'USFR':Decimal('50')},assets)
    assert rows[0]['strategy']=='world_trend' and rows[0]['symbol']=='UGLD' and rows[0]['qty']=='10'
    sells=sum(Decimal(r['qty'])*Decimal(r['limit_price']) for r in rows if r['side']=='sell')
    buys=sum(Decimal(r['qty'])*Decimal(r['limit_price']) for r in rows if r['side']=='buy')
    assert buys<=sells


def test_missing_selected_product_volatility_stops_trades(monkeypatch):
    monkeypatch.setattr(main,'_rotator_momentum',lambda *a,**kw:0.1)
    monkeypatch.setattr(main,'_rotator_realized_vol',lambda *a,**kw:None)
    with pytest.raises(main.RotatorDataError): main.plan_rotator_weights({},main.mix8_config)


def test_small_old_etf_balance_is_fully_liquidated_on_replacement():
    s=new_state('x','paper',{'mix8':{'EEM':'0.000018'}},{}, {}, '2026-09-25T00:00:00Z')
    rows=target_orders(s,{'mix8':{'EEM':0}}, {'EEM':Decimal('60')},{'EEM':{'tradable':True,'fractionable':True,'status':'active'}})
    assert rows[0]['qty']=='0.000018'

from execution.controller import Controller
from test_execution import MemoryStore, FakeBroker

class Bot:
    SLEEVES=main.SLEEVES
    strategy_allocations=main.strategy_allocations
    aaa_config=main.aaa_config
    mix8_config=main.mix8_config
    TREND_SLEEVES=main.TREND_SLEEVES
    rebalance_config=main.rebalance_config
    validate_force_environment=staticmethod(main.validate_force_environment)
    world_trend_target_weights=staticmethod(main.world_trend_target_weights)
    def get_execution_price(self,api,s,env,require_fresh=False): return 100
    def check_trading_day(self,mode): return True
    def check_margin_conditions(self,api,env): return {'errors':[], 'target_margin':0,'allowed':False}
    def calculate_rebalanced_allocations(self,api): return {'adjusted_allocations':self.strategy_allocations}
    def plan_rotator_weights(self,api,cfg):
        return {'weights':{cfg['candidates'][2][1]:1},'cash_weight':0,'picks':[cfg['candidates'][2][1]],'realized_vols':{'x':.2}}
    def trend_signals(self,cfg): return {s:{'on':True} for _,s in cfg['legs']}

class PlanningBroker(FakeBroker):
    def positions(self): return [dict(symbol=s,qty='100',current_price='100') for s in ['EET','UGLD','SPXL']]
    def asset(self,s): return {'tradable':True,'fractionable':True,'status':'active'}
    def account(self): return {'id':'account','cash':'100','equity':'10100'}


def controller_fixture():
    b=Bot();s=new_state('account','paper',{'aaa':{'EET':'30'},'mix8':{'EET':'40'},'world_trend':{'UGLD':'20'},'spx_trend':{'SPXL':'10'}},
                       {'reserve':'100'}, {}, '2026-09-25T19:00:00Z')
    store=MemoryStore(s);broker=PlanningBroker();c=Controller(b,{},'paper',store,broker)
    return c,store,broker


def test_monthly_plan_keeps_shared_ownership_and_funding_sums_once():
    c,store,broker=controller_fixture()
    plan=c.build(store.read(),'monthly','2026-10')
    assert sum(Decimal(v) for v in plan['funding'].values())==100
    for row in plan['orders']:
        if row['side']=='sell':
            assert Decimal(row['qty'])<=Decimal(store.read()['portfolios'][row['strategy']]['positions'].get(row['symbol'],0))
    t=c.ledger.acquire();c.ledger.start(t,plan);c.ledger.start(t,plan)
    assert store.read()['portfolios']['mix8']['cash']=='42.500000'


def test_daily_gold_exit_cannot_touch_mix8_gold():
    c,store,broker=controller_fixture()
    store.state['portfolios']['mix8']['positions']={'UGLD':'40'}
    c.bot.trend_signals=lambda cfg:{s:{'on':False} for _,s in cfg['legs']}
    p=c.build(store.read(),'daily','2026-09-25')
    sells=[r for r in p['orders'] if r['symbol']=='UGLD' and r['side']=='sell']
    assert len(sells)==1 and sells[0]['qty']=='20' and sells[0]['strategy']=='world_trend'


def test_annual_rebalance_transfers_conserve_cash_and_buy_within_floor():
    c,store,broker=controller_fixture()
    p=c.build(store.read(),'monthly','2027-01',annual=True)
    assert abs(sum(Decimal(v) for v in p['transfers'].values()))<Decimal('0.000001')
    assert any(Decimal(v)<0 for v in p['transfers'].values())
    assert any(Decimal(v)>0 for v in p['transfers'].values())
    projected={k:Decimal(port['cash'])+Decimal(p['funding'].get(k,0))+Decimal(p['transfers'].get(k,0)) for k,port in store.read()['portfolios'].items()}
    for row in p['orders']:
        amount=Decimal(row['qty'])*Decimal(row['limit_price'])
        projected[row['strategy']]+=amount if row['side']=='sell' else -amount
    assert all(v>=0 for v in projected.values())


def test_closed_market_never_persists_plan_or_funding():
    c,store,broker=controller_fixture();broker.clock=lambda:{'is_open':False}
    result=c.execute('mix8-upgrade')
    assert result['status']=='awaiting_market'
    assert store.read()['active'] is None
    assert store.read()['portfolios']['mix8']['cash']=='0'


def test_new_margin_is_not_counted_as_nav_contribution():
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']='0'
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'0','equity':'10000'}
    p=c.build(store.read(),'monthly','2026-10')
    assert sum(Decimal(v) for v in p['funding_debt'].values())==1000
    # Mix8's starting net NAV is 4000. Borrowing buys assets AND adds debt.
    assert Decimal(p['metadata']['mix8']['peak_nav'])==4000

from execution.migration import bootstrap_state


def test_bootstrap_preserves_retired_dust_and_exclusive_owners():
    positions=[{'symbol':'EET','qty':'5','cost_basis':'400','market_value':'500'},
               {'symbol':'EEM','qty':'3','cost_basis':'210','market_value':'240'},
               {'symbol':'TQQQ','qty':'0.000006','cost_basis':'0.0004','market_value':'0.0005'}]
    s=bootstrap_state({'id':'a','cash':'-74'},positions,[],{'aaa':['EET'],'mix8':['EEM']},{},'paper','2026-09-25T19:00:00Z')
    assert s['portfolios']['aaa']['positions']=={'EET':'5'}
    assert s['portfolios']['mix8']['positions']=={'EEM':'3'}
    assert s['portfolios']['legacy']['positions']=={'TQQQ':'0.000006'}
    assert sum(Decimal(p['debt']) for p in s['portfolios'].values())==74


def test_bootstrap_refuses_shared_ownership_without_evidence_and_open_orders():
    pos=[{'symbol':'EET','qty':'5','cost_basis':'400','market_value':'500'}]
    with pytest.raises(SafetyStop): bootstrap_state({'id':'a','cash':'0'},pos,[],{'aaa':['EET'],'mix8':['EET']},{},'paper','2026-09-25T19:00:00Z')
    with pytest.raises(SafetyStop): bootstrap_state({'id':'a','cash':'0'},pos,[{'id':'open'}],{'aaa':['EET']},{},'paper','2026-09-25T19:00:00Z')


def test_annual_transfers_use_net_equity_with_tilted_margin_funding():
    c,store,broker=controller_fixture()
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'100','equity':'10000'}
    c.bot.calculate_rebalanced_allocations=lambda api:{'adjusted_allocations':{'aaa_allo':.10,'world_trend_allo':.10,'mix8_allo':.70,'spx_trend_allo':.10}}
    p=c.build(store.read(),'monthly','2027-01',annual=True)
    # Net equity 10,100; AAA target 2,146.25, current 3,000, net new cash 10.
    # Borrowing 100 is debt, not net new equity. Transfer keeps 1% execution buffer.
    assert Decimal(p['transfers']['aaa'])==Decimal('-855.11250000')


def test_margin_data_failure_still_rotates_without_new_money():
    c,store,broker=controller_fixture()
    c.bot.check_margin_conditions=lambda api,env:{'errors':['FRED unavailable'],'target_margin':0,'allowed':False}
    p=c.build(store.read(),'monthly','2026-10')
    assert p['orders']
    assert all(Decimal(v)==0 for v in p['funding'].values())
    assert p['margin_retry'] is True


def test_funding_retry_only_adds_to_monthly_weights_without_rotating_again():
    c,store,broker=controller_fixture()
    store.state['portfolios']['aaa']['metadata']['last_momentum_check']={'weights':{'EET':1},'cash_weight':0}
    store.state['portfolios']['mix8']['metadata']['last_momentum_check']={'weights':{'EET':1},'cash_weight':0}
    p=c.build(store.read(),'monthly-funding','2026-10')
    assert not any(r['side']=='sell' for r in p['orders'])
    assert sum(Decimal(v) for v in p['funding'].values())==100


def test_earmarked_sleeve_cash_is_not_counted_again_as_new_margin():
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']='0'
    store.state['portfolios']['mix8'].update(cash='100',debt='1000')
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'-900','equity':'10000'}
    p=c.build(store.read(),'monthly','2026-10')
    assert Decimal(p['margin_budget'])==0
