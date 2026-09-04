import os, sqlite3, secrets
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-me')
DB = os.path.join(os.path.dirname(__file__), 'itp.db')


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS clients (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      phone TEXT NOT NULL,
      plate TEXT NOT NULL,
      make_model TEXT,
      expiry_date TEXT NOT NULL,
      token TEXT NOT NULL UNIQUE,
      consent INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_id INTEGER NOT NULL,
      sent_at TEXT NOT NULL,
      channel TEXT NOT NULL,
      body TEXT NOT NULL,
      status TEXT NOT NULL,
      reminder_days INTEGER,
      FOREIGN KEY(client_id) REFERENCES clients(id)
    );
    CREATE TABLE IF NOT EXISTS appointments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_id INTEGER NOT NULL,
      appointment_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'confirmed',
      created_at TEXT NOT NULL,
      FOREIGN KEY(client_id) REFERENCES clients(id)
    );
    ''')
    con.commit()
    con.close()


def send_sms(phone, body):
    """Uses Twilio when configured; otherwise logs a demo SMS."""
    sid = os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    sender = os.getenv('TWILIO_FROM')

    if sid and token and sender:
        from twilio.rest import Client
        msg = Client(sid, token).messages.create(
            body=body,
            from_=sender,
            to=phone
        )
        return 'sent:' + msg.sid

    print(f'[DEMO SMS] to={phone} body={body}')
    return 'demo'


def reminder_body(client, days):
    station = os.getenv('STATION_NAME', 'Stația ITP Demo')
    base = os.getenv('PUBLIC_BASE_URL', 'http://localhost:5000')
    exp = datetime.strptime(
        client['expiry_date'],
        '%Y-%m-%d'
    ).strftime('%d.%m.%Y')

    if days == 0:
        timing = 'expiră astăzi'
    elif days == 1:
        timing = 'expiră mâine'
    else:
        timing = f'expiră în {days} zile'

    return (
        f'Bună ziua! ITP-ul pentru {client["plate"]} {timing}, '
        f'la data de {exp}. Programați-vă rapid: '
        f'{base}/book/{client["token"]} — {station}'
    )


def run_reminders():
    today = date.today()
    con = db()
    clients = con.execute(
        'SELECT * FROM clients WHERE consent=1'
    ).fetchall()

    sent = 0

    for c in clients:
        exp = datetime.strptime(
            c['expiry_date'],
            '%Y-%m-%d'
        ).date()

        days = (exp - today).days

        if days not in (30, 7, 1, 0):
            continue

        already = con.execute(
            '''
            SELECT 1 FROM messages
            WHERE client_id=?
            AND reminder_days=?
            AND date(sent_at)=?
            ''',
            (c['id'], days, today.isoformat())
        ).fetchone()

        if already:
            continue

        body = reminder_body(c, days)

        try:
            status = send_sms(c['phone'], body)
        except Exception as e:
            status = 'error:' + str(e)[:120]

        con.execute(
            '''
            INSERT INTO messages(
                client_id,
                sent_at,
                channel,
                body,
                status,
                reminder_days
            )
            VALUES(?,?,?,?,?,?)
            ''',
            (
                c['id'],
                datetime.now().isoformat(timespec='seconds'),
                'SMS',
                body,
                status,
                days
            )
        )

        sent += 1

    con.commit()
    con.close()
    return sent

init_db()

@app.route('/')
def index():
    con = db()
    today = date.today()

    clients = con.execute(
        'SELECT * FROM clients ORDER BY expiry_date'
    ).fetchall()

    due30 = sum(
        0 <= (
            datetime.strptime(
                c['expiry_date'],
                '%Y-%m-%d'
            ).date() - today
        ).days <= 30
        for c in clients
    )

    messages_today = con.execute(
        "SELECT COUNT(*) n FROM messages WHERE date(sent_at)=?",
        (today.isoformat(),)
    ).fetchone()['n']

    appts = con.execute(
        '''
        SELECT COUNT(*) n
        FROM appointments
        WHERE status='confirmed'
        AND date(appointment_at)>=?
        ''',
        (today.isoformat(),)
    ).fetchone()['n']

    recent = con.execute(
        '''
        SELECT m.*, c.name, c.plate
        FROM messages m
        JOIN clients c ON c.id=m.client_id
        ORDER BY m.id DESC
        LIMIT 8
        '''
    ).fetchall()

    con.close()

    return render_template(
        'index.html',
        clients=clients,
        due30=due30,
        messages_today=messages_today,
        appts=appts,
        recent=recent,
        today=today
    )


@app.route('/clients', methods=['GET', 'POST'])
def clients():
    con = db()

    if request.method == 'POST':
        name = request.form['name'].strip()
        phone = request.form['phone'].strip()
        plate = request.form['plate'].strip().upper()
        make_model = request.form.get(
            'make_model',
            ''
        ).strip()
        expiry = request.form['expiry_date']
        consent = 1 if request.form.get('consent') else 0

        con.execute(
            '''
            INSERT INTO clients(
                name,
                phone,
                plate,
                make_model,
                expiry_date,
                token,
                consent,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            ''',
            (
                name,
                phone,
                plate,
                make_model,
                expiry,
                secrets.token_urlsafe(12),
                consent,
                datetime.now().isoformat(timespec='seconds')
            )
        )

        con.commit()
        con.close()

        flash('Client adăugat.')
        return redirect(url_for('clients'))

    rows = con.execute(
        'SELECT * FROM clients ORDER BY expiry_date'
    ).fetchall()

    con.close()

    return render_template(
        'clients.html',
        clients=rows
    )


@app.post('/clients/<int:cid>/delete')
def delete_client(cid):
    con = db()

    con.execute(
        'DELETE FROM appointments WHERE client_id=?',
        (cid,)
    )
    con.execute(
        'DELETE FROM messages WHERE client_id=?',
        (cid,)
    )
    con.execute(
        'DELETE FROM clients WHERE id=?',
        (cid,)
    )

    con.commit()
    con.close()

    flash('Client șters.')
    return redirect(url_for('clients'))


@app.route('/appointments')
def appointments():
    con = db()

    rows = con.execute(
        '''
        SELECT a.*, c.name, c.plate, c.phone
        FROM appointments a
        JOIN clients c ON c.id=a.client_id
        ORDER BY appointment_at
        '''
    ).fetchall()

    con.close()

    return render_template(
        'appointments.html',
        appointments=rows
    )


@app.route('/messages')
def messages():
    con = db()

    rows = con.execute(
        '''
        SELECT m.*, c.name, c.plate, c.phone
        FROM messages m
        JOIN clients c ON c.id=m.client_id
        ORDER BY m.id DESC
        LIMIT 200
        '''
    ).fetchall()

    con.close()

    return render_template(
        'messages.html',
        messages=rows
    )


@app.route('/book/<token>', methods=['GET', 'POST'])
def book(token):
    con = db()

    c = con.execute(
        'SELECT * FROM clients WHERE token=?',
        (token,)
    ).fetchone()

    if not c:
        con.close()
        return 'Link invalid', 404

    if request.method == 'POST':
        dt = request.form['appointment_at']

        con.execute(
            '''
            INSERT INTO appointments(
                client_id,
                appointment_at,
                status,
                created_at
            )
            VALUES(?,?,?,?)
            ''',
            (
                c['id'],
                dt,
                'confirmed',
                datetime.now().isoformat(timespec='seconds')
            )
        )

        con.commit()
        con.close()

        return render_template(
            'book_success.html',
            client=c,
            appointment_at=dt
        )

    con.close()

    min_dt = (
        datetime.now() + timedelta(hours=2)
    ).strftime('%Y-%m-%dT%H:%M')

    return render_template(
        'book.html',
        client=c,
        min_dt=min_dt
    )


@app.post('/run-reminders')
def run_reminders_now():
    n = run_reminders()

    flash(
        f'Remindere procesate: {n} mesaje noi.'
    )

    return redirect(url_for('index'))


@app.get('/health')
def health():
    return jsonify(ok=True)


if __name__ == '__main__':
    init_db()

    scheduler = BackgroundScheduler(
        daemon=True
    )

    scheduler.add_job(
        run_reminders,
        'cron',
        hour=9,
        minute=0
    )

    scheduler.start()

    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', '5000')),
        debug=True,
        use_reloader=False
    )
