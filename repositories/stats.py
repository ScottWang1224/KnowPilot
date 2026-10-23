"""Admin dashboard figures. Rows are fetched by UTC range and grouped by Taipei date in Python,
so the SQL stays free of time-zone functions and runs on both MySQL and SQLite."""
import json
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from llm.guard import TAIPEI, taipei_day_bounds

MONEY_DIGITS = 4
DOWN_LIST_LIMIT = 50
TOP_USERS = 10


def taipei_date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).astimezone(TAIPEI).date()


def money(value):
    return round(value, MONEY_DIGITS)


def dashboard(db, days, *, user_limit, global_limit, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(TAIPEI).date()
    first = today - timedelta(days=days - 1)
    start, _ = taipei_day_bounds(datetime.combine(first, time(12), TAIPEI))
    daily = {first + timedelta(i): {'date': (first + timedelta(i)).isoformat(), 'questions': 0, 'new_users': 0,
                                    'router_cost': 0.0, 'answer_cost': 0.0, 'feedback_up': 0, 'feedback_down': 0,
                                    'capped_users': 0} for i in range(days)}
    people = {u['id']: u for u in db.query('SELECT u.id,u.name,u.email,u.role,u.created_at,d.name AS department_name '
                                           'FROM users u JOIN departments d ON d.id=u.department_id')}

    for u in people.values():
        day = taipei_date(u['created_at'])
        if day in daily:
            daily[day]['new_users'] += 1

    questions_by_user = Counter()
    for m in db.query("SELECT m.created_at,c.user_id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                      "WHERE m.role='user' AND m.created_at>=%s", (start,)):
        day = taipei_date(m['created_at'])
        if day in daily:
            daily[day]['questions'] += 1
            questions_by_user[m['user_id']] += 1

    per_user_day, per_user, per_question = defaultdict(float), defaultdict(float), defaultdict(float)
    for c in db.query('SELECT user_id,message_id,purpose,budget_cost_usd,created_at FROM llm_calls WHERE created_at>=%s', (start,)):
        day = taipei_date(c['created_at'])
        if day not in daily:
            continue
        cost = float(c['budget_cost_usd'])
        daily[day][c['purpose'] + '_cost'] += cost
        per_user_day[(c['user_id'], day)] += cost
        per_user[c['user_id']] += cost
        if c['message_id'] is not None:
            per_question[c['message_id']] += cost
    capped = {(uid, day) for (uid, day), total in per_user_day.items()
              if total >= user_limit and people.get(uid, {}).get('role') != 'admin'}
    for _, day in capped:
        daily[day]['capped_users'] += 1

    totals = Counter()
    by_department, by_status = defaultdict(Counter), defaultdict(Counter)
    down = []
    rows = db.query("SELECT f.rating,f.comment,f.created_at,m.id AS message_id,m.payload,c.id AS conversation_id,"
                    "d.name AS department_name,u.id AS user_id,u.name AS user_name,"
                    "(SELECT q.content FROM messages q WHERE q.conversation_id=m.conversation_id AND q.id<m.id "
                    "AND q.role='user' ORDER BY q.id DESC LIMIT 1) AS question "
                    "FROM feedback f JOIN messages m ON m.id=f.message_id JOIN conversations c ON c.id=m.conversation_id "
                    "JOIN departments d ON d.id=c.department_id JOIN users u ON u.id=f.user_id "
                    "WHERE f.created_at>=%s ORDER BY f.created_at DESC,f.id DESC", (start,))
    for f in rows:
        day = taipei_date(f['created_at'])
        if day not in daily:
            continue
        status = (json.loads(f['payload']) if f['payload'] else {}).get('status') or 'unknown'
        daily[day]['feedback_' + f['rating']] += 1
        totals[f['rating']] += 1
        by_department[f['department_name']][f['rating']] += 1
        by_status[status][f['rating']] += 1
        if f['rating'] == 'down' and len(down) < DOWN_LIST_LIMIT:
            down.append({'message_id': f['message_id'], 'conversation_id': f['conversation_id'],
                         'created_at': f['created_at'], 'user': {'id': f['user_id'], 'name': f['user_name']},
                         'department': f['department_name'], 'status': status,
                         'question': f['question'], 'comment': f['comment']})

    today_start, today_end = taipei_day_bounds(now)
    spent_today = float(db.query('SELECT COALESCE(SUM(budget_cost_usd),0) AS total FROM llm_calls '
                                 'WHERE created_at>=%s AND created_at<%s', (today_start, today_end), one=True)['total'])
    rated = totals['up'] + totals['down']
    top = sorted(per_user.items(), key=lambda item: item[1], reverse=True)[:TOP_USERS]
    for row in daily.values():
        row['router_cost'], row['answer_cost'] = money(row['router_cost']), money(row['answer_cost'])
    return {
        'days': days, 'from': first.isoformat(), 'to': today.isoformat(),
        'daily': list(daily.values()),
        'feedback': {'up': totals['up'], 'down': totals['down'],
                     'up_ratio': round(totals['up'] / rated, 3) if rated else None,
                     'by_department': [{'department': k, 'up': v['up'], 'down': v['down']} for k, v in sorted(by_department.items())],
                     'by_status': [{'status': k, 'up': v['up'], 'down': v['down']} for k, v in sorted(by_status.items())],
                     'down_list': down},
        'usage': {'cost': money(sum(per_user.values())),
                  'questions': sum(r['questions'] for r in daily.values()),
                  'avg_cost_per_question': money(sum(per_question.values()) / len(per_question)) if per_question else None,
                  'capped_users': len({uid for uid, _ in capped}),
                  'today_spent': money(spent_today), 'today_limit': global_limit,
                  'today_percent': round(spent_today / global_limit * 100, 1) if global_limit else None},
        'top_users': [{'user_id': uid, 'name': people.get(uid, {}).get('name'), 'email': people.get(uid, {}).get('email'),
                       'department': people.get(uid, {}).get('department_name'), 'cost': money(cost),
                       'questions': questions_by_user[uid]} for uid, cost in top],
    }
