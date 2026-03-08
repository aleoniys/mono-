from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room, leave_room 
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import json
import os
import random
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'my_super_secret_key'

socketio = SocketIO(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = None  # не показувати повідомлення при переході на логін 

USERS_FILE = 'users.json'

def load_users():
    if not os.path.exists(USERS_FILE): return {}
    with open(USERS_FILE, 'r', encoding='utf-8') as f: 
        users = json.load(f)
        for u in users:
            if 'games_played' not in users[u]: users[u]['games_played'] = 0
            if 'wins' not in users[u]: users[u]['wins'] = 0
            if 'bonus_points' not in users[u]: users[u]['bonus_points'] = 0
        return users

def save_users(users_data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f: json.dump(users_data, f, indent=4)

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
        users[username] = {'password': password, 'games_played': 0, 'wins': 0, 'bonus_points': 0}
        save_users(users)
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        if username in users and users[username]['password'] == password:
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
    return render_template('game.html', room_name=room_name, username=current_user.username)

# --- WEBSOCKETS ТА ЛОГІКА ГРИ ---
active_rooms = {}

def get_lobby_rooms():
    """Кімнати, які ще не почали гру — показуємо тільки їх у лобі."""
    return {k: v for k, v in active_rooms.items() if not v.get('started', False)}
PURCHASABLE_CELLS = [1,2,3, 6,7,8, 11,12,13, 16,17,18, 21,22,23, 26,27,28, 31,32,33, 36,37,38]
COLOR_GROUPS = [[1,2,3], [6,7,8], [11,12,13], [16,17,18], [21,22,23], [26,27,28], [31,32,33], [36,37,38]]
RENT_PRICES = {
    1: 50, 2: 50, 3: 50, 6: 100, 7: 100, 8: 100, 11: 150, 12: 150, 13: 150,
    16: 200, 17: 200, 18: 200, 21: 250, 22: 250, 23: 250, 26: 300, 27: 300, 28: 300,
    31: 400, 32: 400, 33: 400, 36: 500, 37: 500, 38: 500
}

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
        
        users = load_users()
        for p in room['players']:
            if p in users:
                users[p]['games_played'] += 1
                if p == winner:
                    users[p]['wins'] += 1
                    users[p]['bonus_points'] += 10
        save_users(users)

        emit('update_state', state, to=room_name) 
        emit('game_over', {'winner': winner}, to=room_name) 
        return True
    return False

@socketio.on('create_room')
def handle_create_room(data):
    room_name = data['room_name']
    player_name = current_user.username
    if room_name not in active_rooms:
        active_rooms[room_name] = {'name': room_name, 'players': [], 'max_players': int(data['max_players'])}
        join_room(room_name)
        active_rooms[room_name]['players'].append(player_name)
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
                    'jail_turns': 0, 'bankrupt': False, 'doubles_rolled': 0
                }
            room['started'] = True
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
                    'jail_turns': 0, 'bankrupt': False, 'doubles_rolled': 0
                }
            room['started'] = True
            emit('start_game', {'room_name': room_name}, to=room_name)
            emit('update_rooms', {'rooms': get_lobby_rooms()}, broadcast=True)

@socketio.on('request_game_state')
def handle_req_state(data):
    if data['room_name'] in active_rooms and 'state' in active_rooms[data['room_name']]:
        emit('update_state', active_rooms[data['room_name']]['state'], to=data['room_name'])

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

    dice1 = random.randint(1, 6)
    dice2 = random.randint(1, 6)
    total = dice1 + dice2
    is_double = (dice1 == dice2)
    player_data = state['players_data'][player]

    emit('dice_rolled', {'player': player, 'dice1': dice1, 'dice2': dice2, 'total': total, 'is_double': is_double}, to=room_name)
    socketio.sleep(1.5)

    if player_data['jail_turns'] > 0:
        if is_double:
            player_data['jail_turns'] = 0
            player_data['doubles_rolled'] = 0
            state['extra_turn'] = False 
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} викидає дубль і виходить з тюрми!'}, to=room_name)
        else:
            player_data['jail_turns'] -= 1
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} не викидає дубль. У тюрмі: {player_data["jail_turns"]} х.'}, to=room_name)
            pass_turn(state, room)
            emit('update_state', state, to=room_name)
            return
    else:
        if is_double:
            player_data['doubles_rolled'] += 1
            if player_data['doubles_rolled'] == 3:
                emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🚨 3 ДУБЛІ ПІДРЯД! {player} відправляється до тюрми за перевищення швидкості!'}, to=room_name)
                player_data['pos'] = 10
                player_data['jail_turns'] = 3
                player_data['doubles_rolled'] = 0
                state['extra_turn'] = False
                pass_turn(state, room)
                emit('update_state', state, to=room_name)
                return
            else:
                state['extra_turn'] = True
        else:
            player_data['doubles_rolled'] = 0
            state['extra_turn'] = False

    old_pos = player_data['pos']
    player_data['pos'] = (old_pos + total) % 40
    pos = player_data['pos']

    if pos < old_pos and old_pos != 30:
        player_data['balance'] += 2000
        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} проходить СТАРТ: +2000 балів!'}, to=room_name)

    if pos == 30:
        player_data['pos'] = 10 
        player_data['jail_turns'] = 3
        state['extra_turn'] = False 
        player_data['doubles_rolled'] = 0
        emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} потрапляє до тюрми!'}, to=room_name)
        pass_turn(state, room)
    else:
        if pos in PURCHASABLE_CELLS:
            if pos in state['properties']:
                owner = state['properties'][pos]
                if owner != player and str(pos) not in state.get('mortgages', {}):
                    upgrades = state['upgrades'].get(str(pos), 0)
                    rent = RENT_PRICES[pos] * (1 + upgrades * 2) 
                    
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
            emit('update_state', state, to=room_name)

@socketio.on('buy_property')
def handle_buy_property(data):
    room_name = data['room_name']
    room = active_rooms[room_name]
    state = room['state']
    player = current_user.username
    if state.get('pending_trade_from') == player: return
    if state['waiting_for_buy']:
        state['players_data'][player]['balance'] -= 1000
        state['properties'][state['players_data'][player]['pos']] = player
        state['waiting_for_buy'] = False
        pass_turn(state, room)
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
    upgrades = state['upgrades'].get(str(pos), 0)
    is_mortgaged = str(pos) in state.get('mortgages', {})

    if action == 'upgrade' and not state.get('has_upgraded_this_turn', False):
        group = next((g for g in COLOR_GROUPS if pos in g), None)
        owns_all = all(state['properties'].get(p) == player for p in group)
        if owns_all and not is_mortgaged and upgrades < 3 and p_data['balance'] >= 500:
            p_data['balance'] -= 500
            state['upgrades'][str(pos)] = upgrades + 1
            state['has_upgraded_this_turn'] = True
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} покращує клітинку {pos}!'}, to=room_name)

    elif action == 'sell_upgrade':
        if upgrades > 0:
            p_data['balance'] += 500
            state['upgrades'][str(pos)] = upgrades - 1
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} продає 1 покращення з клітинки {pos} за 500 балів.'}, to=room_name)

    elif action == 'mortgage':
        if upgrades == 0 and not is_mortgaged:
            p_data['balance'] += 500
            state['mortgages'][str(pos)] = 10 
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} закладає клітинку {pos}. У нього є 10 ходів на викуп!'}, to=room_name)

    elif action == 'unmortgage':
        if is_mortgaged and p_data['balance'] >= 1000:
            p_data['balance'] -= 1000
            del state['mortgages'][str(pos)]
            emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'{player} викупає клітинку {pos} із застави!'}, to=room_name)

    emit('update_state', state, to=room_name)

def clear_trade_timeout(room_name, sender_username):
    room = active_rooms.get(room_name)
    if not room or not room.get('state'): return
    state = room['state']
    if state.get('pending_trade_from') != sender_username: return
    state['pending_trade_from'] = None
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

    emit('receive_chat_message', {'sender': 'СИСТЕМА', 'message': f'🤝 Успішна угода між {sender} та {target}!'}, to=room_name)
    emit('update_state', state, to=room_name)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=True)