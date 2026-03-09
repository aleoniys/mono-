from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room, leave_room 
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import json
import os
import random
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'my_super_secret_key')

socketio = SocketIO(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = None  # не показувати повідомлення при переході на логін 

# PostgreSQL (Replit: можна перевизначити через Secrets — DATABASE_URL)
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:password@helium/heliumdb?sslmode=disable')
USERS_FILE = 'users.json'

def get_db():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception:
        return None

def init_db():
    """Створює таблиці users, rooms, game_results якщо їх ще немає."""
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username VARCHAR(255) PRIMARY KEY,
                    password VARCHAR(255) NOT NULL,
                    games_played INT NOT NULL DEFAULT 0,
                    wins INT NOT NULL DEFAULT 0,
                    bonus_points INT NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    name VARCHAR(255) PRIMARY KEY,
                    created_by VARCHAR(255) NOT NULL,
                    max_players INT NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'waiting',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                ) 
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS game_results (
                    id SERIAL PRIMARY KEY,
                    room_name VARCHAR(255) NOT NULL,
                    winner_username VARCHAR(255),
                    players TEXT NOT NULL,
                    finished_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS game_states (
                    room_name VARCHAR(255) PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        import sys
        print(f'[WARN] init_db: {e}', file=sys.stderr)
    finally:
        conn.close()

def db_save_room(room_name, created_by, max_players, status='waiting'):
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rooms (name, created_by, max_players, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status, created_by = EXCLUDED.created_by, max_players = EXCLUDED.max_players
            """, (room_name, created_by, max_players, status))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def db_update_room_status(room_name, status):
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE rooms SET status = %s WHERE name = %s", (status, room_name))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def db_save_game_result(room_name, winner_username, players_list):
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO game_results (room_name, winner_username, players)
                VALUES (%s, %s, %s)
            """, (room_name, winner_username or None, json.dumps(players_list)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def db_save_game_state(room_name, state):
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO game_states (room_name, state, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (room_name) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
            """, (room_name, json.dumps(state)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def db_load_game_state(room_name):
    conn = get_db()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM game_states WHERE room_name = %s", (room_name,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()

def db_delete_game_state(room_name):
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM game_states WHERE room_name = %s", (room_name,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def persist_game_state(room_name, state):
    """Зберігає стан гри в БД перед відправкою клієнтам."""
    if room_name and state:
        db_save_game_state(room_name, state)

def load_rooms_from_db():
    """Заповнює active_rooms з БД: waiting-кімнати та playing-кімнати зі збереженим станом гри."""
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, created_by, max_players, status FROM rooms WHERE status IN ('waiting', 'playing')")
            for row in cur.fetchall():
                name, created_by, max_players, status = row[0], row[1], row[2], row[3]
                if name in active_rooms:
                    continue
                if status == 'waiting':
                    active_rooms[name] = {
                        'name': name,
                        'players': [],
                        'max_players': max_players,
                        'created_by': created_by or '',
                    }
                else:
                    state = db_load_game_state(name)
                    if state and 'players_order' in state:
                        active_rooms[name] = {
                            'name': name,
                            'players': list(state['players_order']),
                            'max_players': max_players,
                            'created_by': created_by or '',
                            'state': state,
                            'started': True,
                        }
    finally:
        conn.close()

def load_users():
    conn = get_db()
    if not conn:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
                for u in users:
                    if 'games_played' not in users[u]: users[u]['games_played'] = 0
                    if 'wins' not in users[u]: users[u]['wins'] = 0
                    if 'bonus_points' not in users[u]: users[u]['bonus_points'] = 0
                return users
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password, games_played, wins, bonus_points FROM users")
            rows = cur.fetchall()
        users = {}
        for r in rows:
            users[r[0]] = {
                'password': r[1],
                'games_played': r[2] or 0,
                'wins': r[3] or 0,
                'bonus_points': r[4] or 0,
            }
        return users
    finally:
        conn.close()

def save_users(users_data):
    conn = get_db()
    if not conn:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, indent=4)
        return
    try:
        with conn.cursor() as cur:
            for username, data in users_data.items():
                cur.execute("""
                    INSERT INTO users (username, password, games_played, wins, bonus_points)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO UPDATE SET
                        password = EXCLUDED.password,
                        games_played = EXCLUDED.games_played,
                        wins = EXCLUDED.wins,
                        bonus_points = EXCLUDED.bonus_points
                """, (
                    username,
                    data.get('password', ''),
                    data.get('games_played', 0),
                    data.get('wins', 0),
                    data.get('bonus_points', 0),
                ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        import sys
        print(f'[WARN] save_users: {e}', file=sys.stderr)
    finally:
        conn.close()

class User(UserMixin):
    def __init__(self, username):
        self.id = username
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    if user_id in users: return User(user_id)
    return None

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        if username in users:
            flash('Це ім\'я вже зайняте.')
            return redirect(url_for('register'))
        users[username] = {
            'password': generate_password_hash(password, method='pbkdf2:sha256'),
            'games_played': 0, 'wins': 0, 'bonus_points': 0
        }
        save_users(users)
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        stored = users.get(username, {}).get('password', '')
        if username in users and (
            (stored.count('$') >= 2 and check_password_hash(stored, password))
            or (stored == password)  # старий формат без хешу
        ):
            user_obj = User(username)
            login_user(user_obj)
            return redirect(url_for('index'))
        else:
            flash('Неправильне ім\'я або пароль.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/profile')
@login_required
def profile():
    users = load_users()
    user_data = users.get(current_user.username, {})
    return render_template('profile.html', 
                           username=current_user.username,
                           games=user_data.get('games_played', 0),
                           wins=user_data.get('wins', 0),
                           bonus=user_data.get('bonus_points', 0))

@app.route('/')
@login_required
def index():
    return render_template('index.html', username=current_user.username)

@app.route('/game/<room_name>')
@login_required
def game(room_name):
    if room_name not in active_rooms or current_user.username not in active_rooms[room_name]['players']:
        return redirect(url_for('index'))
    return render_template('game.html', room_name=room_name, username=current_user.username, board_cells=BOARD_DATA)

# --- WEBSOCKETS ТА ЛОГІКА ГРИ ---
active_rooms = {}

def get_lobby_rooms():
    """Кімнати, які ще не почали гру — показуємо тільки їх у лобі."""
    return {k: v for k, v in active_rooms.items() if not v.get('started', False)}

# Дані клітинок — з Python-модуля (працює на Replit без читання файлів)
try:
    from board_cells_data import BOARD_DATA
except Exception as e:
    import sys
    print(f'[WARN] board_cells_data import failed: {e}', file=sys.stderr)
    BOARD_DATA = {'cells': [], 'upgrade_cost_per_star': 500, 'sell_star_value': 500}

BOARD_BY_ID = {c['id']: c for c in BOARD_DATA.get('cells', [])}

def _get_cell(pos, key, default=None):
    c = BOARD_BY_ID.get(pos)
    return (c.get(key) if c else None) or default

PURCHASABLE_CELLS = [c['id'] for c in BOARD_DATA['cells'] if c.get('price') is not None]
CAR_CELLS = [c['id'] for c in BOARD_DATA['cells'] if c.get('type') == 'car']
RENT_BY_STARS = {
    c['id']: [c['rent_0'], c['rent_1'], c['rent_2'], c['rent_3']]
    for c in BOARD_DATA['cells']
    if c.get('type') == 'company' and all(c.get('rent_' + str(i)) is not None for i in range(4))
}

def car_rent(n_cars):
    """Оренда за автомобілі: беруться з board_cells (rent_0..rent_3 = 1..4 авто)."""
    if n_cars < 1 or not CAR_CELLS:
        return 0
    cell = BOARD_BY_ID.get(CAR_CELLS[0])
    if not cell:
        return 0
    r = cell.get('rent_' + str(min(n_cars, 4) - 1))
    return r if r is not None else 0

CHANCE_CELLS = [c['id'] for c in BOARD_DATA['cells'] if c.get('type') == 'chance']
TAX_CELLS = [c['id'] for c in BOARD_DATA['cells'] if c.get('type') == 'tax']
TAX_AMOUNT = 2000
JAIL_FINE = 500
# Групи монополій: 2 компанії → Шанс → 1 компанія (наприклад [1,2,6], [5,7,9])
COLOR_GROUPS = [[1, 2, 6], [5, 7, 9], [11, 12, 16], [15, 17, 19], [21, 22, 26], [25, 27, 29], [31, 32, 36], [35, 38]]
UPGRADE_COST_DEFAULT = BOARD_DATA.get('upgrade_cost_per_star', 500)
SELL_STAR_DEFAULT = BOARD_DATA.get('sell_star_value', 500)

def pass_turn(state, room):
    if state.get('extra_turn', False):
        state['extra_turn'] = False
        state['has_upgraded_this_turn'] = False
        return

    current_player = state['players_order'][state['turn_index']]
    
    expired_mortgages = []
    for prop, timer in list(state.get('mortgages', {}).items()):
        if state['properties'].get(int(prop)) == current_player:
            state['mortgages'][prop] -= 1
            if state['mortgages'][prop] <= 0:
                expired_mortgages.append(int(prop))
    
    for prop in expired_mortgages:
        del state['properties'][prop]
        del state['mortgages'][str(prop)]
        socketio.emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'Банк конфіскував клітинку {prop} у {current_player} за несплату застави!'}, to=room['name'])

    state['has_upgraded_this_turn'] = False
    state['players_data'][current_player]['doubles_rolled'] = 0 
    
    while True:
        state['turn_index'] = (state['turn_index'] + 1) % room['max_players']
        next_player = state['players_order'][state['turn_index']]
        if not state['players_data'][next_player].get('bankrupt', False):
            break

def process_bankruptcy(player, state):
    state['players_data'][player]['bankrupt'] = True
    cells_to_free = [c for c, owner in state['properties'].items() if owner == player]
    for c in cells_to_free:
        del state['properties'][c]
        if str(c) in state['upgrades']: del state['upgrades'][str(c)]
        if str(c) in state.get('mortgages', {}): del state['mortgages'][str(c)]
    state['debt'] = None 

def check_win(room_name, room, state):
    active_players = [p for p in room['players'] if not state['players_data'][p].get('bankrupt', False)]
    if len(active_players) <= 1:
        winner = active_players[0] if active_players else "Ніхто"

        db_save_game_result(room_name, winner if winner != "Ніхто" else None, room['players'])
        db_update_room_status(room_name, 'finished')
        db_delete_game_state(room_name)

        users = load_users()
        for p in room['players']:
            if p in users:
                users[p]['games_played'] += 1
                if p == winner:
                    users[p]['wins'] += 1
                    users[p]['bonus_points'] += 10
        save_users(users)

        persist_game_state(room_name, state)
        emit('update_state', state, to=room_name)
        emit('game_over', {'winner': winner}, to=room_name)
        return True
    return False

@socketio.on('create_room')
def handle_create_room(data):
    room_name = data['room_name']
    player_name = current_user.username
    max_players = int(data['max_players'])
    if room_name not in active_rooms:
        active_rooms[room_name] = {'name': room_name, 'players': [], 'max_players': max_players, 'created_by': player_name}
        join_room(room_name)
        active_rooms[room_name]['players'].append(player_name)
        db_save_room(room_name, player_name, max_players, 'waiting')
        emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)
        if len(active_rooms[room_name]['players']) == active_rooms[room_name]['max_players']:
            room = active_rooms[room_name]
            colors = ['#e74c3c', '#3498db', '#2ecc71', '#f1c40f', '#9b59b6', '#e67e22']
            order = random.sample(room['players'], len(room['players']))
            room['state'] = {
                'turn_index': 0, 'players_order': order, 'players_data': {},
                'properties': {}, 'upgrades': {}, 'mortgages': {},
                'waiting_for_buy': False, 'has_upgraded_this_turn': False, 'debt': None,
                'extra_turn': False
            }
            for i, p in enumerate(order):
                room['state']['players_data'][p] = {
                    'pos': 0, 'balance': 10000, 'color': colors[i],
                    'jail_turns': 0, 'bankrupt': False, 'doubles_rolled': 0, 'start_bonus_count': 0
                }
            room['started'] = True
            db_update_room_status(room_name, 'playing')
            db_save_game_state(room_name, room['state'])
            emit('start_game', {'room_name': room_name}, to=room_name)
            emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)

@socketio.on('request_rooms')
def handle_req_rooms():
    emit('update_rooms', {'rooms': get_lobby_rooms()})

@socketio.on('leave_game_room')
def handle_leave_room(data):
    room_name = data.get('room_name')
    player_name = current_user.username
    if room_name in active_rooms:
        room = active_rooms[room_name]
        if room.get('started'):
            return
        if player_name in room['players']:
            room['players'].remove(player_name)
            leave_room(room_name)
            if len(room['players']) == 0:
                db_update_room_status(room_name, 'finished')
                del active_rooms[room_name]
            emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)

@socketio.on('join_game_room')
def handle_join_room(data):
    room_name = data['room_name']
    player_name = current_user.username
    if room_name not in active_rooms:
        return
    room = active_rooms[room_name]
    join_room(room_name)
    if room.get('started'):
        if player_name in room['players']:
            persist_game_state(room_name, room['state'])
            emit('update_state', room['state'], to=request.sid)
        return
    if player_name not in room['players'] and len(room['players']) < room['max_players']:
        room['players'].append(player_name)
        emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)
        if len(room['players']) == room['max_players']:
            colors = ['#e74c3c', '#3498db', '#2ecc71', '#f1c40f', '#9b59b6', '#e67e22']
            order = random.sample(room['players'], len(room['players']))
            room['state'] = {
                'turn_index': 0, 'players_order': order, 'players_data': {}, 
                'properties': {}, 'upgrades': {}, 'mortgages': {}, 
                'waiting_for_buy': False, 'has_upgraded_this_turn': False, 'debt': None,
                'extra_turn': False
            }
            for i, p in enumerate(order):
                room['state']['players_data'][p] = {
                    'pos': 0, 'balance': 10000, 'color': colors[i], 
                    'jail_turns': 0, 'bankrupt': False, 'doubles_rolled': 0, 'start_bonus_count': 0
                }
            room['started'] = True
            db_update_room_status(room_name, 'playing')
            db_save_game_state(room_name, room['state'])
            emit('start_game', {'room_name': room_name}, to=room_name)
            emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)

@socketio.on('request_game_state')
def handle_req_state(data):
    if data['room_name'] in active_rooms and 'state' in active_rooms[data['room_name']]:
        state = active_rooms[data['room_name']]['state']
        persist_game_state(data['room_name'], state)
        emit('update_state', state, to=data['room_name'])

@socketio.on('send_chat_message')
def handle_chat(data): emit('receive_chat_message', {'sender': current_user.username, 'message': data['message']}, to=data['room_name'])

# --- ОНОВЛЕНИЙ ТАЙМЕР ---
@socketio.on('turn_timeout')
def handle_turn_timeout(data):
    room_name = data['room_name']
    target_player = data.get('target_player') # Той, чий час вийшов
    
    room = active_rooms.get(room_name)
    if not room: return
    state = room['state']
    
    current_turn_player = state['players_order'][state['turn_index']]
    
    # Перевіряємо, чи запит актуальний (захист від застарілих сигналів)
    if target_player != current_turn_player: return
    # Якщо він вже банкрут, не обробляємо двічі
    if state['players_data'][current_turn_player].get('bankrupt', False): return
        
    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⏳ ЧАС ВИЙШОВ! {current_turn_player} стає банкрутом і вибуває з гри!'}, to=room_name)
    process_bankruptcy(current_turn_player, state)
    
    if check_win(room_name, room, state): return 
    
    state['waiting_for_buy'] = False
    state['extra_turn'] = False
    pass_turn(state, room)
    persist_game_state(room_name, state)
    emit('update_state', state, to=room_name)

@socketio.on('roll_dice')
def handle_roll_dice(data):
    room_name = data['room_name']
    player = current_user.username
    room = active_rooms.get(room_name)
    if not room: return
    state = room['state']
    current_turn_player = state['players_order'][state['turn_index']]
    
    if player != current_turn_player or state['waiting_for_buy'] or state['debt']: return
    if state.get('pending_trade_from') == player: return

    if state['players_data'][player].get('skip_next_turn', False):
        state['players_data'][player]['skip_next_turn'] = False
        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⏭️ {player} пропускає хід (забув ключі).'}, to=room_name)
        pass_turn(state, room)
        persist_game_state(room_name, state)
        emit('update_state', state, to=room_name)
        return

    dice1 = random.randint(1, 6)
    dice2 = random.randint(1, 6)
    total = dice1 + dice2
    is_double = (dice1 == dice2)
    player_data = state['players_data'][player]

    if player_data['jail_turns'] > 0:
        if is_double:
            player_data['jail_turns'] = 0
            player_data['doubles_rolled'] = 0
            state['extra_turn'] = False 
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} викидає дубль і виходить з тюрми!'}, to=room_name)
        else:
            emit('dice_rolled', {'player': player, 'dice1': dice1, 'dice2': dice2, 'total': total, 'is_double': is_double}, to=room_name)
            socketio.sleep(1.5)
            player_data['jail_turns'] -= 1
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} не викидає дубль. У тюрмі: {player_data["jail_turns"]} х.'}, to=room_name)
            pass_turn(state, room)
            persist_game_state(room_name, state)
            emit('update_state', state, to=room_name)
            return
    else:
        if is_double:
            player_data['doubles_rolled'] += 1
            if player_data['doubles_rolled'] == 3:
                jail_cell = _get_cell(10, 'name') or 'ТЮРМА'
                emit('dice_rolled', {'player': player, 'dice1': dice1, 'dice2': dice2, 'total': total, 'is_double': is_double, 'landing_pos': 10, 'landing_cell': jail_cell}, to=room_name)
                socketio.sleep(1.5)
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🚨 3 ДУБЛІ ПІДРЯД! {player} відправляється до тюрми за перевищення швидкості!'}, to=room_name)
                player_data['pos'] = 10
                player_data['jail_turns'] = 3
                player_data['doubles_rolled'] = 0
                state['extra_turn'] = False
                pass_turn(state, room)
                persist_game_state(room_name, state)
                emit('update_state', state, to=room_name)
                return
            else:
                state['extra_turn'] = True
        else:
            player_data['doubles_rolled'] = 0
            state['extra_turn'] = False

    old_pos = player_data['pos']
    landing_pos = (old_pos + total) % 40
    landing_cell = _get_cell(landing_pos, 'name') or f'Клітинка {landing_pos}'
    emit('dice_rolled', {'player': player, 'dice1': dice1, 'dice2': dice2, 'total': total, 'is_double': is_double, 'landing_pos': landing_pos, 'landing_cell': landing_cell}, to=room_name)
    socketio.sleep(1.5)
    player_data['pos'] = landing_pos
    pos = landing_pos

    if pos < old_pos and old_pos != 30:
        start_count = player_data.get('start_bonus_count', 0)
        if start_count < 6:
            player_data['balance'] += 2000
            player_data['start_bonus_count'] = start_count + 1
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} проходить СТАРТ: +2000 балів! (бонус {player_data["start_bonus_count"]}/6)'}, to=room_name)
        else:
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} проходить СТАРТ, але ліміт бонусів (6) вичерпано.'}, to=room_name)

    if pos == 0:
        player_data['balance'] += 1000
        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} потрапив на СТАРТ: бонус +1000 балів!'}, to=room_name)

    if pos == 30:
        player_data['pos'] = 10 
        player_data['jail_turns'] = 3
        state['extra_turn'] = False 
        player_data['doubles_rolled'] = 0
        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} потрапляє до тюрми!'}, to=room_name)
        pass_turn(state, room)
    else:
        if pos in PURCHASABLE_CELLS or pos in CAR_CELLS:
            if pos in state['properties']:
                owner = state['properties'][pos]
                if owner != player and str(pos) not in state.get('mortgages', {}):
                    if pos in CAR_CELLS:
                        n_cars = sum(1 for p in CAR_CELLS if state['properties'].get(p) == owner)
                        rent = car_rent(n_cars)
                    else:
                        upgrades = state['upgrades'].get(str(pos), 0)
                        rent = RENT_BY_STARS[pos][min(upgrades, 3)]
                    if player_data['balance'] >= rent:
                        player_data['balance'] -= rent
                        state['players_data'][owner]['balance'] += rent
                        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} платить оренду {rent} гравцю {owner}.'}, to=room_name)
                        pass_turn(state, room)
                    else:
                        state['debt'] = {'player': player, 'amount': rent, 'creditor': owner}
                        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⚠️ {player} винен {rent} балів гравцю {owner}!'}, to=room_name)
                else:
                    pass_turn(state, room)
            else:
                state['waiting_for_buy'] = True
        elif pos in TAX_CELLS:
            if player_data['balance'] >= TAX_AMOUNT:
                player_data['balance'] -= TAX_AMOUNT
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} сплачує податок {TAX_AMOUNT} балів.'}, to=room_name)
                pass_turn(state, room)
            else:
                state['debt'] = {'player': player, 'amount': TAX_AMOUNT, 'creditor': 'SYSTEM'}
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⚠️ {player} винен {TAX_AMOUNT} балів (податок)!'}, to=room_name)
        elif pos in CHANCE_CELLS:
            # Розширена колода Шанс: перейти на поле N, вийти з тюрми, кожен платить тобі, на СТАРТ тощо
            effect = random.randint(1, 14)
            if effect == 1:
                player_data['pos'] = (pos - 1) % 40
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — один крок у зворотному напрямку! Тепер на клітинці {(pos - 1) % 40}.'}, to=room_name)
            elif effect == 2:
                lost = int(player_data['balance'] * 0.25)
                player_data['balance'] = max(0, player_data['balance'] - lost)
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — ви розвелися, дружина забрала 25% активів (−{lost} балів).'}, to=room_name)
            elif effect == 3:
                state['players_data'][player]['skip_next_turn'] = True
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — ви забули ключі від дому. Пропускаєте один хід!'}, to=room_name)
            elif effect == 4:
                player_data['pos'] = 10
                player_data['jail_turns'] = 3
                state['extra_turn'] = False
                player_data['doubles_rolled'] = 0
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — СБУ провело обшуки. Ви потрапляєте в тюрму!'}, to=room_name)
                pass_turn(state, room)
                persist_game_state(room_name, state)
                emit('update_state', state, to=room_name)
                return
            elif effect == 5:
                div = int(player_data['balance'] * 0.10)
                player_data['balance'] += div
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — дивіденди 10% від депозиту (+{div} балів)!'}, to=room_name)
            elif effect == 6:
                player_data['balance'] += 1500
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — ви отримали спадок (+1500 балів)!'}, to=room_name)
            elif effect == 7:
                player_data['pos'] = 0
                player_data['balance'] += 2000
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — перехід на СТАРТ! Отримуєте 2000 балів.'}, to=room_name)
            elif effect == 8:
                if player_data.get('jail_turns', 0) > 0:
                    player_data['jail_turns'] = 0
                    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — вийти з тюрми безкоштовно! Ви вільні.'}, to=room_name)
                else:
                    state['players_data'][player]['get_out_of_jail_free'] = True
                    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — картка «Вийти з тюрми безкоштовно». Збережено на потім!'}, to=room_name)
            elif effect == 9:
                each_pays = 300
                others = [p for p in state['players_order'] if p != player and not state['players_data'][p].get('bankrupt', False)]
                total = 0
                for other in others:
                    pay = min(state['players_data'][other]['balance'], each_pays)
                    state['players_data'][other]['balance'] -= pay
                    state['players_data'][player]['balance'] += pay
                    total += pay
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — кожен гравець платить вам {each_pays} балів. Ви отримали {total}!'}, to=room_name)
            elif effect == 10:
                target = random.choice([5, 15, 25, 35])
                player_data['pos'] = target
                cell_name = _get_cell(target, 'name') or f'Клітинка {target}'
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — перехід на поле «{cell_name}» (клітинка {target}).'}, to=room_name)
            elif effect == 11:
                target = random.choice([1, 11, 21, 31])
                player_data['pos'] = target
                cell_name = _get_cell(target, 'name') or f'Клітинка {target}'
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — перехід на поле «{cell_name}» (клітинка {target}).'}, to=room_name)
            elif effect == 12:
                player_data['balance'] += 500
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — бонус від банку +500 балів!'}, to=room_name)
            elif effect == 13:
                pay = random.choice([200, 500, 800])
                if player_data['balance'] >= pay:
                    player_data['balance'] -= pay
                    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — штраф за паркування −{pay} балів.'}, to=room_name)
                else:
                    state['debt'] = {'player': player, 'amount': pay, 'creditor': 'SYSTEM'}
                    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — штраф {pay} балів. Не вистачає грошей — борг!'}, to=room_name)
                    persist_game_state(room_name, state)
                    emit('update_state', state, to=room_name)
                    return
            else:
                player_data['balance'] += 1000
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🎲 ШАНС: {player} — виграш у лотерею +1000 балів!'}, to=room_name)
            pass_turn(state, room)
        elif pos not in [0, 10, 20]:
            chance_penalty = random.choice([100, 250, 500, 1000])
            if player_data['balance'] >= chance_penalty:
                player_data['balance'] -= chance_penalty
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'ШАНС: {player} платить {chance_penalty} балів!'}, to=room_name)
                pass_turn(state, room)
            else:
                state['debt'] = {'player': player, 'amount': chance_penalty, 'creditor': 'SYSTEM'}
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⚠️ {player} винен банку {chance_penalty} балів!'}, to=room_name)
        else:
            pass_turn(state, room)

    persist_game_state(room_name, state)
    emit('update_state', state, to=room_name)

@socketio.on('pay_debt')
def handle_pay_debt(data):
    room_name = data['room_name']
    room = active_rooms.get(room_name)
    if not room: return
    state = room['state']
    player = current_user.username
    if state.get('pending_trade_from') == player: return

    if state['debt'] and state['debt']['player'] == player:
        debt_amount = state['debt']['amount']
        creditor = state['debt']['creditor']
        
        if state['players_data'][player]['balance'] >= debt_amount:
            state['players_data'][player]['balance'] -= debt_amount
            if creditor != 'SYSTEM':
                state['players_data'][creditor]['balance'] += debt_amount
            
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} успішно сплачує борг {debt_amount} балів!'}, to=room_name)
            state['debt'] = None
            pass_turn(state, room)
            persist_game_state(room_name, state)
            emit('update_state', state, to=room_name)

@socketio.on('pay_jail_fine')
def handle_pay_jail_fine(data):
    room_name = data['room_name']
    room = active_rooms.get(room_name)
    if not room: return
    state = room['state']
    player = current_user.username
    if state.get('pending_trade_from') == player or state.get('debt'): return
    current_turn_player = state['players_order'][state['turn_index']]
    if player != current_turn_player: return
    player_data = state['players_data'][player]
    if player_data.get('jail_turns', 0) <= 0: return
    if player_data.get('balance', 0) < JAIL_FINE: return
    player_data['balance'] -= JAIL_FINE
    player_data['jail_turns'] = 0
    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} сплатив {JAIL_FINE} балів і вийшов з тюрми!'}, to=room_name)
    pass_turn(state, room)
    persist_game_state(room_name, state)
    emit('update_state', state, to=room_name)

@socketio.on('buy_property')
def handle_buy_property(data):
    room_name = data['room_name']
    room = active_rooms[room_name]
    state = room['state']
    player = current_user.username
    if state.get('pending_trade_from') == player: return
    if state['waiting_for_buy']:
        buy_pos = state['players_data'][player]['pos']
        price = _get_cell(buy_pos, 'price', 1000)
        if price is None or state['players_data'][player]['balance'] < price:
            return
        state['players_data'][player]['balance'] -= price
        state['properties'][buy_pos] = player
        state['waiting_for_buy'] = False
        pass_turn(state, room)
        persist_game_state(room_name, state)
        emit('update_state', state, to=room_name)

@socketio.on('skip_buy')
def handle_skip_buy(data):
    room_name = data['room_name']
    room = active_rooms[room_name]
    state = room['state']
    if state.get('pending_trade_from') == current_user.username: return
    if state['waiting_for_buy']:
        state['waiting_for_buy'] = False
        pass_turn(state, room)
        persist_game_state(room_name, state)
        emit('update_state', state, to=room_name)

@socketio.on('manage_property_action')
def handle_manage_property(data):
    room_name = data['room_name']
    room = active_rooms.get(room_name)
    if not room: return
    state = room['state']
    player = current_user.username
    pos = int(data['pos'])
    action = data['action']

    if state['players_order'][state['turn_index']] != player: return
    if state.get('pending_trade_from') == player: return
    if state['properties'].get(pos) != player: return

    p_data = state['players_data'][player]
    is_car_cell = pos in CAR_CELLS
    upgrades = 0 if is_car_cell else state['upgrades'].get(str(pos), 0)
    is_mortgaged = str(pos) in state.get('mortgages', {})
    mortgage_val = _get_cell(pos, 'mortgage', 500)
    unmortgage_val = _get_cell(pos, 'unmortgage', 1000)
    upgrade_cost = _get_cell(pos, 'upgrade_cost') or UPGRADE_COST_DEFAULT
    sell_star_val = _get_cell(pos, 'sell_star') or SELL_STAR_DEFAULT

    if action == 'upgrade' and not is_car_cell and not state.get('has_upgraded_this_turn', False):
        group = next((g for g in COLOR_GROUPS if pos in g), None)
        owns_all = all(state['properties'].get(p) == player for p in group)
        min_other = min(state['upgrades'].get(str(p), 0) for p in group if p != pos) if group else 0
        can_upgrade_evenly = (min_other >= upgrades)
        if owns_all and not is_mortgaged and upgrades < 3 and p_data['balance'] >= upgrade_cost and can_upgrade_evenly:
            p_data['balance'] -= upgrade_cost
            state['upgrades'][str(pos)] = upgrades + 1
            state['has_upgraded_this_turn'] = True
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} покращує клітинку {pos}!'}, to=room_name)

    elif action == 'sell_upgrade' and not is_car_cell:
        if upgrades > 0:
            p_data['balance'] += sell_star_val
            state['upgrades'][str(pos)] = upgrades - 1
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} продає 1 покращення з клітинки {pos} за {sell_star_val} балів.'}, to=room_name)

    elif action == 'mortgage':
        if upgrades == 0 and not is_mortgaged and mortgage_val is not None:
            p_data['balance'] += mortgage_val
            state['mortgages'][str(pos)] = 10
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} закладає клітинку {pos}. У нього є 10 ходів на викуп!'}, to=room_name)

    elif action == 'unmortgage':
        if is_mortgaged and unmortgage_val is not None and p_data['balance'] >= unmortgage_val:
            p_data['balance'] -= unmortgage_val
            del state['mortgages'][str(pos)]
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} викупає клітинку {pos} із застави!'}, to=room_name)

    persist_game_state(room_name, state)
    emit('update_state', state, to=room_name)

def clear_trade_timeout(room_name, sender_username):
    room = active_rooms.get(room_name)
    if not room or not room.get('state'): return
    state = room['state']
    if state.get('pending_trade_from') != sender_username: return
    state['pending_trade_from'] = None
    persist_game_state(room_name, state)
    socketio.emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'⏳ Час на відповідь вийшов. Обмін скасовано.'}, to=room_name)
    socketio.emit('update_state', state, to=room_name)

@socketio.on('propose_trade')
def handle_propose_trade(data):
    room_name = data['room_name']
    if room_name not in active_rooms: return
    room = active_rooms[room_name]
    state = room['state']
    state['pending_trade_from'] = current_user.username
    data['sender'] = current_user.username
    persist_game_state(room_name, state)
    emit('trade_offer', data, to=room_name)
    emit('update_state', state, to=room_name)
    t = threading.Timer(30.0, clear_trade_timeout, [room_name, current_user.username])
    t.daemon = True
    t.start()

@socketio.on('trade_response')
def handle_trade_response(data):
    room_name = data['room_name']
    room = active_rooms[room_name]
    state = room['state']
    sender, target = data['sender'], data['target']
    state['pending_trade_from'] = None
    if not data['accepted']:
        persist_game_state(room_name, state)
        emit('update_state', state, to=room_name)
        return
    offer_money, request_money = int(data['offer_money']), int(data['request_money'])
    offer_props, request_props = [int(p) for p in data['offer_props']], [int(p) for p in data['request_props']]

    s_data, t_data = state['players_data'][sender], state['players_data'][target]
    if s_data['balance'] < offer_money or t_data['balance'] < request_money: return
    for p in offer_props:
        if state['properties'].get(p) != sender: return
    for p in request_props:
        if state['properties'].get(p) != target: return

    s_data['balance'] = s_data['balance'] - offer_money + request_money
    t_data['balance'] = t_data['balance'] - request_money + offer_money

    for p in offer_props:
        state['properties'][p] = target
        if str(p) in state['upgrades']: del state['upgrades'][str(p)] 
    for p in request_props:
        state['properties'][p] = sender
        if str(p) in state['upgrades']: del state['upgrades'][str(p)]

    persist_game_state(room_name, state)
    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🤝 Успішна угода між {sender} та {target}!'}, to=room_name)
    emit('update_state', state, to=room_name)

if __name__ == '__main__':
    init_db()
    load_rooms_from_db()
    port = int(os.environ.get('PORT', 5000))
    # debug=False щоб на Replit не запускався reloader (інакше порт не слухається)
    import sys
    print(f'Starting server on 0.0.0.0:{port}', file=sys.stderr)
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)