import pytest


def test_configuration_rejects_missing_secret_and_insecure_production(monkeypatch):
    from core.config import Settings
    monkeypatch.setenv('APP_ENV','test')
    monkeypatch.delenv('SESSION_SECRET',raising=False)
    with pytest.raises(ValueError,match='SESSION_SECRET'):
        Settings.from_env()
    monkeypatch.setenv('SESSION_SECRET','a'*48)
    assert not Settings.from_env().https_only
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('SESSION_HTTPS_ONLY','false')
    with pytest.raises(ValueError):
        Settings.from_env()


def test_database_closes_connection_and_rolls_back_on_failure():
    from core.database import Database
    events=[]
    class Connection:
        def cursor(self,**kwargs): return self
        def execute(self,*args): raise RuntimeError('database error')
        def commit(self): events.append('commit')
        def rollback(self): events.append('rollback')
        def close(self): events.append('close')
    with pytest.raises(RuntimeError):
        Database(connection_factory=Connection).execute('bad statement')
    assert events == ['rollback','close','close']
