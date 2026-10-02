from execution import alerts
import pytest
import main


def test_cash_mismatch_notifies_once_until_recovery_even_if_amount_changes():
    state={}
    state,notify=alerts.failure_transition(state,ValueError('Unexplained broker cash: -0.2425978659'))
    assert notify
    state,notify=alerts.failure_transition(state,ValueError('Unexplained broker cash: -0.9625978659'))
    assert not notify
    assert state['count']==2
    state,recovered=alerts.success_transition(state)
    assert recovered
    state,recovered=alerts.success_transition(state)
    assert not recovered
    state,notify=alerts.failure_transition(state,ValueError('Unexplained broker cash: -0.24'))
    assert notify


def test_cash_alert_formats_cents_without_losing_safety_stop():
    message=alerts.failure_message(ValueError('Unexplained broker cash: -0.2425978659'))
    assert '$0.24' in message
    assert 'blocked' in message.lower()
    assert '-0.2425978659' not in message


def test_reconcile_route_sends_one_failure_and_one_recovery(monkeypatch,_no_outside_world):
    current={}
    class Gate:
        def __init__(self,db,env):pass
        def failed(self,error):
            nonlocal current
            current,notify=alerts.failure_transition(current,error)
            return notify
        def recovered(self):
            nonlocal current
            current,notify=alerts.success_transition(current)
            return notify
        def retry_notification(self,error):pass
    class Controller:
        attempts=0
        def reconcile(self):
            self.attempts+=1
            if self.attempts<=2:raise ValueError('Unexplained broker cash: -0.2425978659')
            return {'status':'reconciled'}
    controller=Controller()
    monkeypatch.setattr(main,'set_alpaca_environment',lambda env:{'ENV':env})
    monkeypatch.setattr(main,'get_controller',lambda *args:controller)
    monkeypatch.setattr(main,'get_firestore_client',lambda:None)
    monkeypatch.setattr(main,'FirestoreIncidentGate',Gate,raising=False)
    def send(message):
        _no_outside_world.append(('TELEGRAM_CHAT_ID',message))
        return 200
    monkeypatch.setattr(main,'send_telegram_message',send)
    with main.app.app_context():
        for _ in range(2):
            with pytest.raises(ValueError,match='Unexplained broker cash'):
                main.shared_etf_reconcile_route(None)
        result,status=main.shared_etf_reconcile_route(None)
    assert status==200
    assert result.json=={'status':'reconciled'}
    assert len(_no_outside_world)==2
    assert '$0.24' in _no_outside_world[0][1]
    assert 'reconciled' in _no_outside_world[1][1].lower()


@pytest.mark.parametrize('action', ['daily', 'monthly', 'monthly-funding'])
def test_strategy_run_waits_for_short_reconcile_lease_without_alert(monkeypatch,_no_outside_world,action):
    attempts=[]; waits=[]
    class Controller:
        def execute(self,action,**kwargs):
            attempts.append(action)
            if len(attempts)==1:
                raise main.SafetyStop('Another executor owns this account')
            return {'status':'complete'}
    monkeypatch.setattr(main,'get_controller',lambda *args:Controller())
    monkeypatch.setattr(main.time,'sleep',lambda seconds:waits.append(seconds))

    assert main._managed_run({},'live',action)=={'status':'complete'}
    assert attempts==[action,action]
    assert waits==[5]
    assert _no_outside_world==[]


@pytest.mark.parametrize('action', ['daily', 'monthly', 'monthly-funding'])
def test_strategy_run_reports_lease_after_bounded_retries(monkeypatch,_no_outside_world,action):
    attempts=[]; waits=[]
    class Controller:
        def execute(self,action,**kwargs):
            attempts.append(action)
            raise main.SafetyStop('Another executor owns this account')
    monkeypatch.setattr(main,'get_controller',lambda *args:Controller())
    monkeypatch.setattr(main.time,'sleep',lambda seconds:waits.append(seconds))

    with pytest.raises(main.SafetyStop,match='Another executor'):
        main._managed_run({},'live',action)
    assert attempts==[action]*3
    assert waits==[5,5]
    assert len(_no_outside_world)==1


@pytest.mark.parametrize('action', ['daily', 'monthly', 'monthly-funding'])
def test_strategy_run_does_not_retry_other_safety_stops(monkeypatch,_no_outside_world,action):
    attempts=[]
    class Controller:
        def execute(self,action,**kwargs):
            attempts.append(action)
            raise main.SafetyStop('Unexplained broker cash: -1')
    monkeypatch.setattr(main,'get_controller',lambda *args:Controller())

    with pytest.raises(main.SafetyStop,match='Unexplained broker cash'):
        main._managed_run({},'live',action)
    assert attempts==[action]
    assert len(_no_outside_world)==1


def test_reconcile_data_error_does_not_report_recovery(monkeypatch,_no_outside_world):
    class Controller:
        def reconcile(self):
            return {'status':'data_error','errors':['EET: Quote stale']}
    class Gate:
        def __init__(self,*args):pass
        def failed(self,error):return True
        def recovered(self):raise AssertionError('A data error is not recovery')
    monkeypatch.setattr(main,'set_alpaca_environment',lambda env:{})
    monkeypatch.setattr(main,'get_controller',lambda *args:Controller())
    monkeypatch.setattr(main,'get_firestore_client',lambda:None)
    monkeypatch.setattr(main,'FirestoreIncidentGate',Gate)
    monkeypatch.setattr(main,'send_telegram_message',lambda message:(_no_outside_world.append(message) or 200))
    with pytest.raises(main.SafetyStop,match='Quote stale'):
        main.shared_etf_reconcile_route(None)
    assert len(_no_outside_world)==1


@pytest.mark.parametrize('route', ['monthly', 'daily'])
@pytest.mark.parametrize('execution_status,http_status', [('data_error',500),('pending',200),('expired',200),('complete',200)])
def test_strategy_http_status_distinguishes_data_error_from_waiting(monkeypatch,route,execution_status,http_status):
    result={'status':execution_status,'errors':['stale quote'] if execution_status=='data_error' else []}
    monkeypatch.setattr(main,'set_alpaca_environment',lambda env:{})
    if route=='monthly':
        monkeypatch.setattr(main,'monthly_invest_all_strategies',lambda api:result)
        monkeypatch.setattr(main,'jsonify',lambda value:value)
        response=main.monthly_invest_all(None)
    else:
        monkeypatch.setattr(main,'daily_trend_sleeves',lambda *args,**kwargs:result)
        response=main.daily_trend_sleeves_route(None)
    assert response==(result,http_status)
