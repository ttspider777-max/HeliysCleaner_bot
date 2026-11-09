from types import SimpleNamespace as U
from scoring import evaluate

class UserStatusEmpty: pass
class UserStatusOnline: pass

def mk(**kw):
    d = dict(deleted=False, fake=False, scam=False, photo=object(), username="x",
             status=UserStatusOnline(), first_name="Ivan", last_name="P")
    d.update(kw); return U(**d)

assert evaluate(mk())[0] == 0
assert evaluate(mk(deleted=True))[0] >= 100
assert evaluate(mk(scam=True))[0] >= 100
s, r = evaluate(mk(photo=None, username=None))
assert s == 3 and set(r) == {"no_photo", "no_username"}
s, r = evaluate(mk(photo=None, username=None, status=UserStatusEmpty()))
assert s == 5
assert "spam_name" in evaluate(mk(first_name="Заработок t.me/xyz"))[1]
assert evaluate(mk(username=None))[0] == 1   # один признак — не повод удалять
print("OK")
