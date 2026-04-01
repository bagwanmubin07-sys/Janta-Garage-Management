from flask import Flask, render_template, request, redirect, send_from_directory, session

import os
import re
from collections import Counter
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'janta_garage_secret_2024'  # Secret key for sessions

# File upload configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create upload folder if it doesn't exist
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DB_FILE = 'garage.db'

DEFAULT_GARAGE_INFO = {
    'name': 'Janta Garage',
    'phone': '+91 98765 43210',
    'email': 'contact@jantagarage.com',
    'address': 'Main Road, Your City',
    'description': 'Trusted service, billing, and customer management for your garage.',
}

LOW_STOCK_THRESHOLD = 20

BILL_SELECT_FIELDS = """id, customer, amount, payment_method, payment_status, qr_code_path,
card_holder_name, card_number, card_cvv, card_bank, card_expiry,
service_names, vehicle, customer_id, subtotal, discount_percentage,
discount_amount, gst_percentage, gst_amount, user_id, customer_payment_status,
customer_payment_note, customer_payment_screenshot_path"""

def get_db_connection():
    conn = sqlite3.connect('garage.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Optional: row as dict
    return conn

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_garage_profile():
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, phone, email, address, description FROM garage_profile WHERE id=1")
        return cur.fetchone()

def mask_card_number(card_number):
    card_number = card_number.strip()
    if '*' in card_number:
        return card_number

    digits = ''.join(ch for ch in card_number if ch.isdigit())
    if not digits:
        return ''
    if len(digits) <= 4:
        return digits
    return ('*' * (len(digits) - 4)) + digits[-4:]

def build_customer_redirect(path, customer_id=None):
    if customer_id:
        return f"{path}?customer_id={customer_id}"
    return path

def get_customer_by_id(cur, customer_id):
    cur.execute("SELECT id, name, phone, vehicle, user_id, model FROM customers WHERE id=?", (customer_id,))
    return cur.fetchone()

def normalize_role(role):
    return (role or 'customer').strip().lower()

def is_admin_session():
    return normalize_role(session.get('role')) == 'admin'

def admin_only_redirect():
    if 'user_id' not in session:
        return redirect('/')
    if not is_admin_session():
        return redirect('/customer-dashboard')
    return None

def customer_only_redirect():
    if 'user_id' not in session:
        return redirect('/')
    if is_admin_session():
        return redirect('/admin-dashboard')
    return None

def get_user_id_for_customer(cur, customer):
    if not customer:
        return None

    if customer['user_id']:
        return customer['user_id']

    customer_name = (customer['name'] or '').strip()
    if not customer_name:
        return None

    cur.execute(
        "SELECT id FROM users WHERE LOWER(TRIM(username)) = LOWER(TRIM(?)) LIMIT 1",
        (customer_name,),
    )
    user_row = cur.fetchone()
    if not user_row:
        return None
    return user_row['id']

def get_or_create_customer_for_user(cur, user_id, customer_name, mobile_number='', car_number='', model=''):
    cur.execute(
        "SELECT id, name, phone, vehicle, user_id, model FROM customers WHERE user_id=? LIMIT 1",
        (user_id,),
    )
    customer = cur.fetchone()
    if customer:
        updated_name = customer_name or (customer['name'] or '')
        updated_phone = mobile_number or (customer['phone'] or '')
        updated_vehicle = car_number or (customer['vehicle'] or '')
        updated_model = model or (customer['model'] or '')
        cur.execute(
            "UPDATE customers SET name=?, phone=?, vehicle=?, user_id=?, model=? WHERE id=?",
            (updated_name, updated_phone, updated_vehicle, user_id, updated_model, customer['id']),
        )
        return customer['id']

    cur.execute(
        "SELECT id, name, phone, vehicle, user_id, model FROM customers WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1",
        (customer_name,),
    )
    customer = cur.fetchone()
    if customer:
        updated_name = customer_name or (customer['name'] or '')
        updated_phone = mobile_number or (customer['phone'] or '')
        updated_vehicle = car_number or (customer['vehicle'] or '')
        updated_model = model or (customer['model'] or '')
        cur.execute(
            "UPDATE customers SET name=?, phone=?, vehicle=?, user_id=?, model=? WHERE id=?",
            (updated_name, updated_phone, updated_vehicle, user_id, updated_model, customer['id']),
        )
        return customer['id']

    cur.execute(
        "INSERT INTO customers (name, phone, vehicle, user_id, model) VALUES (?, ?, ?, ?, ?)",
        (customer_name, mobile_number, car_number, user_id, model),
    )
    return cur.lastrowid

def parse_percentage(value, default=0.0, max_value=None):
    try:
        parsed_value = float(str(value).strip())
    except (TypeError, ValueError):
        return default

    parsed_value = max(0.0, parsed_value)
    if max_value is not None:
        parsed_value = min(parsed_value, max_value)
    return parsed_value

def calculate_bill_totals(subtotal, discount_percentage, gst_percentage):
    try:
        subtotal = float(subtotal)
    except (TypeError, ValueError):
        subtotal = 0.0

    subtotal = round(max(0.0, subtotal), 2)
    discount_percentage = parse_percentage(discount_percentage, default=0.0, max_value=100.0)
    gst_percentage = parse_percentage(gst_percentage, default=0.0)

    discount_amount = round(subtotal * (discount_percentage / 100.0), 2)
    taxable_amount = round(max(0.0, subtotal - discount_amount), 2)
    gst_amount = round(taxable_amount * (gst_percentage / 100.0), 2)
    total_amount = round(taxable_amount + gst_amount, 2)

    return {
        'subtotal': subtotal,
        'discount_percentage': discount_percentage,
        'discount_amount': discount_amount,
        'gst_percentage': gst_percentage,
        'gst_amount': gst_amount,
        'total_amount': total_amount,
    }

def parse_service_items(service_names_text):
    expanded_items = []
    for item in parse_service_item_entries(service_names_text):
        expanded_items.extend([item['name']] * item['quantity'])
    return expanded_items

def parse_service_item_entries(service_names_text):
    if not service_names_text:
        return []

    parsed_items = []
    for raw_item in str(service_names_text).split(','):
        item_text = raw_item.strip()
        if not item_text:
            continue

        match = re.match(r"^(.*?)\s*x\s*(\d+)$", item_text, re.IGNORECASE)
        if match:
            item_name = match.group(1).strip()
            item_quantity = max(1, int(match.group(2)))
        else:
            item_name = item_text
            item_quantity = 1

        if not item_name:
            continue

        parsed_items.append({
            'name': item_name,
            'quantity': item_quantity,
        })

    return parsed_items

def parse_service_item_quantities(service_names_text):
    quantities = {}
    for item in parse_service_item_entries(service_names_text):
        quantities[item['name']] = quantities.get(item['name'], 0) + item['quantity']
    return quantities

def format_service_item_quantities(items_with_quantities):
    formatted_items = []
    for item in items_with_quantities:
        item_name = str(item.get('name', '')).strip()
        if not item_name:
            continue

        try:
            item_quantity = int(item.get('quantity', 1))
        except (TypeError, ValueError):
            item_quantity = 1

        formatted_items.append(f"{item_name} x{max(1, item_quantity)}")

    return ', '.join(formatted_items)

def parse_positive_quantity(raw_value, default=1):
    try:
        quantity = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    return max(1, quantity)

def build_inventory_deltas(previous_items, next_items):
    previous_counts = Counter(previous_items)
    next_counts = Counter(next_items)
    all_items = set(previous_counts) | set(next_counts)
    return {
        item: previous_counts[item] - next_counts[item]
        for item in all_items
        if previous_counts[item] != next_counts[item]
    }

def validate_inventory_deltas(cur, inventory_deltas):
    items_to_consume = {item: -delta for item, delta in inventory_deltas.items() if delta < 0}
    if not items_to_consume:
        return None

    placeholders = ','.join('?' for _ in items_to_consume)
    cur.execute(
        f"SELECT name, COALESCE(quantity, 0) AS quantity FROM inventory WHERE name IN ({placeholders})",
        list(items_to_consume.keys()),
    )
    inventory_rows = {row['name']: row['quantity'] for row in cur.fetchall()}

    blocked_items = []
    for item_name, needed_quantity in items_to_consume.items():
        available_quantity = inventory_rows.get(item_name)
        if available_quantity is None:
            blocked_items.append(f"{item_name} (not found)")
        elif available_quantity < needed_quantity:
            blocked_items.append(f"{item_name} (available {available_quantity}, needed {needed_quantity})")

    if blocked_items:
        return "Not enough inventory for: " + ", ".join(blocked_items)
    return None

def apply_inventory_deltas(cur, inventory_deltas):
    for item_name, delta in inventory_deltas.items():
        if delta == 0:
            continue
        cur.execute(
            "UPDATE inventory SET quantity = COALESCE(quantity, 0) + ? WHERE name=?",
            (delta, item_name),
        )

def get_inventory_rows_by_names(cur, item_names):
    if not item_names:
        return {}

    placeholders = ','.join('?' for _ in item_names)
    cur.execute(
        f"SELECT id, name, price, COALESCE(quantity, 0) AS quantity FROM inventory WHERE name IN ({placeholders})",
        item_names,
    )
    return {row['name']: row for row in cur.fetchall()}

def get_inventory_rows_by_ids(cur, item_ids):
    if not item_ids:
        return {}

    placeholders = ','.join('?' for _ in item_ids)
    cur.execute(
        f"SELECT id, name, price, COALESCE(quantity, 0) AS quantity FROM inventory WHERE id IN ({placeholders})",
        item_ids,
    )
    return {row['id']: row for row in cur.fetchall()}

def extract_selected_inventory_entries(form, inventory_rows_by_id, selected_item_ids):
    selected_entries = []
    for item_id in selected_item_ids:
        inventory_row = inventory_rows_by_id.get(item_id)
        if not inventory_row:
            continue

        quantity = parse_positive_quantity(form.get(f'service_quantity_{item_id}'), default=1)
        selected_entries.append({
            'id': item_id,
            'name': inventory_row['name'],
            'quantity': quantity,
            'price': inventory_row['price'] or 0,
            'stock': inventory_row['quantity'] or 0,
        })

    return selected_entries

def create_or_update_bill_for_service(cur, service_row):
    subtotal = service_row['price'] or 0
    totals = calculate_bill_totals(subtotal, 0, 18)
    existing_bill_id = service_row['bill_id']

    if existing_bill_id:
        cur.execute(
            """
            UPDATE bills
            SET customer=?, amount=?, customer_id=?, user_id=?,
                service_names=?, vehicle=?, subtotal=?, discount_percentage=?,
                discount_amount=?, gst_percentage=?, gst_amount=?
            WHERE id=?
            """,
            (
                service_row['customer_name'],
                totals['total_amount'],
                service_row['customer_id'],
                service_row['user_id'],
                service_row['service_items'],
                service_row['car_number'],
                totals['subtotal'],
                totals['discount_percentage'],
                totals['discount_amount'],
                totals['gst_percentage'],
                totals['gst_amount'],
                existing_bill_id,
            ),
        )
        return existing_bill_id

    cur.execute(
        """
        INSERT INTO bills (
            customer, amount, payment_method, payment_status, customer_id, user_id,
            service_names, vehicle, subtotal, discount_percentage, discount_amount,
            gst_percentage, gst_amount, customer_payment_status, customer_payment_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            service_row['customer_name'],
            totals['total_amount'],
            'online',
            'unpaid',
            service_row['customer_id'],
            service_row['user_id'],
            service_row['service_items'],
            service_row['car_number'],
            totals['subtotal'],
            totals['discount_percentage'],
            totals['discount_amount'],
            totals['gst_percentage'],
            totals['gst_amount'],
            'not_submitted',
            '',
        ),
    )
    return cur.lastrowid

import sqlite3




# 👇 YE ADD KAR


# Create tables at startup
with get_db_connection() as conn:
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    password TEXT,
                    role TEXT DEFAULT 'customer')''')

    cur.execute('''CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    price INTEGER)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    phone TEXT,
                    vehicle TEXT,
                    user_id INTEGER,
                    model TEXT)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS services (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    price INTEGER,
                    service_items TEXT,
                    requested_service_items TEXT,
                    customer_name TEXT,
                    mobile_number TEXT,
                    vehicle_type TEXT,
                    vehicle_name TEXT,
                    problem TEXT,
                    status TEXT DEFAULT 'pending',
                    service_date DATE,
                    service_time TIME,
                    remarks TEXT,
                    bill_id INTEGER,
                    user_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer TEXT,
                    amount INTEGER)''')
    
    # Add role column to users if it doesn't exist
    try:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'customer'")
    except:
        pass
    
    cur.execute('''CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    price INTEGER,
                    quantity INTEGER DEFAULT 0)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS garage_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT,
                    phone TEXT,
                    email TEXT,
                    address TEXT,
                    description TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    
    # Add quantity column to inventory if it doesn't exist
    try:
        cur.execute("ALTER TABLE inventory ADD COLUMN quantity INTEGER DEFAULT 0")
    except:
        pass
    
    # Remove vehicle column from services if migration is needed
    try:
        cur.execute("ALTER TABLE services ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    except:
        pass
    
    # Add vehicle column to services table
    try:
        cur.execute("ALTER TABLE services ADD COLUMN vehicle TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN customer_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN user_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN vehicle_type TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN vehicle_name TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN problem TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN status TEXT DEFAULT 'pending'")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN service_date DATE")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN service_time TIME")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN remarks TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN service_items TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN requested_service_items TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN bill_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE customers ADD COLUMN user_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE customers ADD COLUMN model TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE services ADD COLUMN mobile_number TEXT")
    except:
        pass
    
    # Add payment columns to bills table
    try:
        cur.execute("ALTER TABLE bills ADD COLUMN payment_method TEXT DEFAULT 'cash'")
    except:
        pass
    
    try:
        cur.execute("ALTER TABLE bills ADD COLUMN payment_status TEXT DEFAULT 'unpaid'")
    except:
        pass
    
    try:
        cur.execute("ALTER TABLE bills ADD COLUMN qr_code_path TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN card_holder_name TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN card_number TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN card_cvv TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN card_bank TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN card_expiry TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN customer_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN user_id INTEGER")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN service_names TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN vehicle TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN subtotal REAL DEFAULT 0")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN discount_percentage REAL DEFAULT 0")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN discount_amount REAL DEFAULT 0")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN gst_percentage REAL DEFAULT 0")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN gst_amount REAL DEFAULT 0")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN customer_payment_status TEXT DEFAULT 'not_submitted'")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN customer_payment_note TEXT")
    except:
        pass

    try:
        cur.execute("ALTER TABLE bills ADD COLUMN customer_payment_screenshot_path TEXT")
    except:
        pass

    cur.execute(
        """
        UPDATE services
        SET customer_id = (
            SELECT c.id
            FROM customers c
            WHERE c.name = services.customer_name
            LIMIT 1
        )
        WHERE customer_id IS NULL AND customer_name IS NOT NULL AND customer_name != ''
        """
    )

    cur.execute("UPDATE services SET vehicle_name = COALESCE(vehicle_name, vehicle)")
    cur.execute("UPDATE services SET problem = COALESCE(problem, name)")
    cur.execute("UPDATE services SET service_items = COALESCE(service_items, CASE WHEN COALESCE(price, 0) > 0 THEN name ELSE NULL END)")
    cur.execute(
        """
        UPDATE services
        SET status = 'completed'
        WHERE status IS NULL AND COALESCE(price, 0) > 0
        """
    )
    cur.execute(
        """
        UPDATE services
        SET status = COALESCE(status, 'pending')
        """
    )

    cur.execute(
        """
        UPDATE customers
        SET user_id = (
            SELECT u.id
            FROM users u
            WHERE LOWER(TRIM(u.username)) = LOWER(TRIM(customers.name))
            LIMIT 1
        )
        WHERE user_id IS NULL AND name IS NOT NULL AND name != ''
        """
    )

    cur.execute(
        """
        UPDATE services
        SET user_id = (
            SELECT u.id
            FROM users u
            WHERE LOWER(TRIM(u.username)) = LOWER(TRIM(
                COALESCE(
                    (
                        SELECT c.name
                        FROM customers c
                        WHERE c.id = services.customer_id
                        LIMIT 1
                    ),
                    services.customer_name
                )
            ))
            LIMIT 1
        )
        WHERE user_id IS NULL
        """
    )

    cur.execute(
        """
        UPDATE bills
        SET customer_id = (
            SELECT c.id
            FROM customers c
            WHERE c.name = bills.customer
            LIMIT 1
        )
        WHERE customer_id IS NULL AND customer IS NOT NULL AND customer != ''
        """
    )

    cur.execute(
        """
        UPDATE bills
        SET vehicle = COALESCE(vehicle, (
            SELECT c.vehicle
            FROM customers c
            WHERE c.id = bills.customer_id
            LIMIT 1
        ))
        WHERE vehicle IS NULL OR vehicle = ''
        """
    )

    cur.execute(
        """
        UPDATE bills
        SET user_id = (
            SELECT c.user_id
            FROM customers c
            WHERE c.id = bills.customer_id
            LIMIT 1
        )
        WHERE user_id IS NULL
        """
    )

    cur.execute("UPDATE bills SET subtotal = COALESCE(subtotal, amount, 0)")
    cur.execute("UPDATE bills SET discount_percentage = COALESCE(discount_percentage, 0)")
    cur.execute("UPDATE bills SET discount_amount = COALESCE(discount_amount, 0)")
    cur.execute("UPDATE bills SET gst_percentage = COALESCE(gst_percentage, 0)")
    cur.execute("UPDATE bills SET gst_amount = COALESCE(gst_amount, 0)")
    cur.execute(
        """
        UPDATE bills
        SET customer_payment_status = CASE
            WHEN customer_payment_status IS NULL AND payment_status = 'paid' THEN 'submitted'
            ELSE COALESCE(customer_payment_status, 'not_submitted')
        END
        """
    )
    cur.execute("UPDATE bills SET customer_payment_note = COALESCE(customer_payment_note, '')")

    cur.execute(
        '''INSERT OR IGNORE INTO garage_profile (id, name, phone, email, address, description)
           VALUES (1, ?, ?, ?, ?, ?)''',
        (
            DEFAULT_GARAGE_INFO['name'],
            DEFAULT_GARAGE_INFO['phone'],
            DEFAULT_GARAGE_INFO['email'],
            DEFAULT_GARAGE_INFO['address'],
            DEFAULT_GARAGE_INFO['description'],
        ),
    )
    
    conn.commit()

# ----------------- ROUTES -----------------

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    if not username or not password:
        return "Username and password are required ❌"

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, role FROM users WHERE username=? AND password=? LIMIT 1",
            (username, password),
        )
        user = cur.fetchone()

    if user:
        user_role = normalize_role(user['role'])
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user_role

        if user_role == 'admin':
            return redirect('/admin-dashboard')
        return redirect('/customer-dashboard')
    else:
        return "Login Failed ❌"
    # REGISTER PAGE
@app.route('/register')
def register():
    return render_template('register.html')

# SAVE USER
@app.route('/register_user', methods=['POST'])
def register_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = 'customer'

    if not username or not password:
        return "Username and password are required ❌"

    with get_db_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE username=? LIMIT 1", (username,))
            existing_user = cur.fetchone()
            if existing_user:
                return "Username already exists"

            cur.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, password, role))
            registered_user_id = cur.lastrowid
            cur.execute("SELECT id, user_id FROM customers WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1", (username,))
            existing_customer = cur.fetchone()
            if not existing_customer:
                cur.execute(
                    "INSERT INTO customers (name, phone, vehicle, user_id) VALUES (?, ?, ?, ?)",
                    (username, '', '', registered_user_id),
                )
            elif not existing_customer['user_id']:
                cur.execute(
                    "UPDATE customers SET user_id=? WHERE id=?",
                    (registered_user_id, existing_customer['id']),
                )
            conn.commit()
        except Exception as e:
            return f"Registration failed: {str(e)} ❌"

    return redirect('/')
    
@app.route('/')
def home():
    return render_template('index.html', garage_info=get_garage_profile())

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/')

    if normalize_role(session.get('role')) == 'admin':
        return redirect('/admin-dashboard')
    return redirect('/customer-dashboard')

def render_dashboard():
    # Check if user is logged in
    if 'user_id' not in session:
        return redirect('/')

    user_role = normalize_role(session.get('role'))
    logged_in_user_id = session.get('user_id')
    service_requests = []
    customer_bills = []
    low_stock = []
    total_customers = 0
    total_services = 0
    dashboard_title = 'Customer Dashboard'
    services_heading = 'My Service Requests'
    services_empty_message = 'No service requests have been submitted from your account yet.'

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            if user_role == 'admin':
                dashboard_title = 'Admin Dashboard'
                services_heading = 'Latest Service Requests'
                services_empty_message = 'No service requests found yet.'

                try:
                    cur.execute(
                        """
                        SELECT s.id,
                               COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), NULLIF(u.username, ''), 'Unknown Customer') AS customer_name,
                               COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                               COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                               COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                               COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '-') AS requested_services,
                               COALESCE(s.status, 'pending') AS status,
                               s.service_date,
                               s.service_time,
                               COALESCE(s.remarks, '') AS remarks,
                               s.created_at
                        FROM services s
                        LEFT JOIN customers c ON c.id = s.customer_id
                        LEFT JOIN users u ON u.id = s.user_id
                        ORDER BY CASE WHEN LOWER(COALESCE(s.status, 'pending')) = 'pending' THEN 0 ELSE 1 END,
                                 s.created_at DESC,
                                 s.id DESC
                        LIMIT 10
                        """
                    )
                    service_requests = cur.fetchall()
                except:
                    cur.execute(
                        """
                        SELECT s.id,
                               COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), NULLIF(u.username, ''), 'Unknown Customer') AS customer_name,
                               COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                               COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                               COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                               COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '-') AS requested_services,
                               COALESCE(s.status, 'pending') AS status,
                               s.service_date,
                               s.service_time,
                               COALESCE(s.remarks, '') AS remarks,
                               s.created_at
                        FROM services s
                        LEFT JOIN customers c ON c.id = s.customer_id
                        LEFT JOIN users u ON u.id = s.user_id
                        LIMIT 10
                        """
                    )
                    service_requests = cur.fetchall()

                try:
                    cur.execute(
                        "SELECT id, name, COALESCE(quantity, 0) as quantity, price "
                        "FROM inventory WHERE COALESCE(quantity, 0) < ? ORDER BY quantity ASC",
                        (LOW_STOCK_THRESHOLD,),
                    )
                    low_stock = cur.fetchall()
                except:
                    cur.execute("SELECT id, name, 0 as quantity, price FROM inventory LIMIT 5")
                    low_stock = cur.fetchall()

                cur.execute("SELECT COUNT(*) AS total_customers FROM customers")
                total_customers = cur.fetchone()['total_customers']
                cur.execute("SELECT COUNT(*) AS total_services FROM services")
                total_services = cur.fetchone()['total_services']
            else:
                try:
                    cur.execute(
                        """
                        SELECT s.id,
                               COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), 'Unknown Customer') AS customer_name,
                               COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                               COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                               COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                               COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '-') AS requested_services,
                               COALESCE(s.status, 'pending') AS status,
                               s.service_date,
                               s.service_time,
                               COALESCE(s.remarks, '') AS remarks,
                               s.created_at
                        FROM services s
                        LEFT JOIN customers c ON c.id = s.customer_id
                        WHERE s.user_id = ?
                        ORDER BY s.created_at DESC, s.id DESC
                        LIMIT 10
                        """,
                        (logged_in_user_id,),
                    )
                    service_requests = cur.fetchall()
                except:
                    cur.execute(
                        """
                        SELECT s.id,
                               COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), 'Unknown Customer') AS customer_name,
                               COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                               COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                               COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                               COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '-') AS requested_services,
                               COALESCE(s.status, 'pending') AS status,
                               s.service_date,
                               s.service_time,
                               COALESCE(s.remarks, '') AS remarks,
                               s.created_at
                        FROM services s
                        LEFT JOIN customers c ON c.id = s.customer_id
                        WHERE s.user_id = ?
                        ORDER BY s.id DESC
                        LIMIT 10
                        """,
                        (logged_in_user_id,),
                    )
                    service_requests = cur.fetchall()

                cur.execute(
                    "SELECT COUNT(*) AS total_services FROM services WHERE user_id = ?",
                    (logged_in_user_id,),
                )
                total_services = cur.fetchone()['total_services']
                cur.execute(
                    f"""
                    SELECT {BILL_SELECT_FIELDS}
                    FROM bills
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT 10
                    """,
                    (logged_in_user_id,),
                )
                customer_bills = cur.fetchall()
    except Exception as e:
        print(f"Dashboard error: {e}")
        low_stock = []

    return render_template(
        'dashboard.html',
        service_requests=service_requests,
        low_stock=low_stock,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
        user=session.get('username'),
        role=user_role,
        dashboard_title=dashboard_title,
        services_heading=services_heading,
        services_empty_message=services_empty_message,
        total_customers=total_customers,
        total_services=total_services,
        customer_bills=customer_bills,
        garage_info=get_garage_profile(),
    )

@app.route('/admin-dashboard')
def admin_dashboard():
    if 'user_id' not in session:
        return redirect('/')
    if normalize_role(session.get('role')) != 'admin':
        return redirect('/customer-dashboard')
    return render_dashboard()

@app.route('/customer-dashboard')
def customer_dashboard():
    if 'user_id' not in session:
        return redirect('/')
    if normalize_role(session.get('role')) == 'admin':
        return redirect('/admin-dashboard')
    return render_dashboard()

@app.route('/request-service', methods=['GET', 'POST'])
def request_service():
    customer_redirect = customer_only_redirect()
    if customer_redirect:
        return customer_redirect

    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '').strip()
        mobile_number = request.form.get('mobile_number', '').strip()
        car_number = request.form.get('car_number', '').strip()
        model = request.form.get('model', '').strip()
        selected_service_ids = []
        for raw_item_id in request.form.getlist('service_items'):
            try:
                selected_service_ids.append(int(raw_item_id))
            except (TypeError, ValueError):
                continue

        if not customer_name or not mobile_number or not car_number or not model:
            return "Customer name, mobile number, car number, and model are required."

        with get_db_connection() as conn:
            cur = conn.cursor()
            inventory_rows = get_inventory_rows_by_ids(cur, selected_service_ids)
            selected_service_entries = extract_selected_inventory_entries(request.form, inventory_rows, selected_service_ids)
            if not selected_service_entries:
                return "Please select at least one service option from inventory."
            requested_service_names = format_service_item_quantities(selected_service_entries)
            customer_id = get_or_create_customer_for_user(
                cur,
                session['user_id'],
                customer_name,
                mobile_number,
                car_number,
                model,
            )
            cur.execute(
                """
                INSERT INTO services (
                    name, price, customer_name, mobile_number, vehicle, customer_id, user_id,
                    vehicle_type, vehicle_name, problem, status, service_date, service_time, remarks,
                    service_items, requested_service_items
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'Service Request',
                    0,
                    customer_name,
                    mobile_number,
                    car_number,
                    customer_id,
                    session['user_id'],
                    '',
                    model,
                    '',
                    'pending',
                    None,
                    None,
                    '',
                    '',
                    requested_service_names,
                ),
            )
            conn.commit()

        return redirect('/customer-dashboard')

    customer_profile = None
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, phone, vehicle, user_id, model FROM customers WHERE user_id=? LIMIT 1",
            (session['user_id'],),
        )
        customer_profile = cur.fetchone()
        cur.execute("SELECT id, name, price, COALESCE(quantity, 0) AS quantity FROM inventory ORDER BY name")
        inventory_items = cur.fetchall()

    return render_template(
        'request_service.html',
        user=session.get('username'),
        role=normalize_role(session.get('role')),
        garage_info=get_garage_profile(),
        customer_profile=customer_profile,
        inventory_items=inventory_items,
    )

@app.route('/garage-info')
def garage_info():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    return render_template(
        'garage_info.html',
        garage_info=get_garage_profile(),
        user=session.get('username'),
        role=normalize_role(session.get('role')),
    )

@app.route('/garage-info/update', methods=['POST'])
def update_garage_info():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    if session.get('role') != 'admin':
        return "Only admins can edit garage information ❌"

    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    email = request.form.get('email', '').strip()
    address = request.form.get('address', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        return redirect('/garage-info')

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''UPDATE garage_profile
               SET name=?, phone=?, email=?, address=?, description=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=1''',
            (name, phone, email, address, description),
        )
        conn.commit()

    return redirect('/garage-info')

@app.route('/inventory')
def inventory():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM inventory")
        items = cur.fetchall()
    return render_template('inventory.html', items=items, user=session.get('username'), role=normalize_role(session.get('role')))

# Add new inventory item
@app.route('/add_inventory', methods=['POST'])
def add_inventory():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    name = request.form.get('name', '').strip()
    price = request.form.get('price', 0)
    quantity = request.form.get('quantity', 0)

    if not name:
        return redirect('/inventory')
    
    try:
        price = int(price)
        quantity = int(quantity)
    except:
        return redirect('/inventory')

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO inventory (name, price, quantity) VALUES (?, ?, ?)", (name, price, quantity))
        conn.commit()
    return redirect('/inventory')

@app.route('/delete_inventory/<int:id>')
def delete_inventory(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    if session.get('role') != 'admin':
        return "Only admins can delete items ❌"
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM inventory WHERE id=?", (id,))
        conn.commit()
    return redirect('/inventory')

@app.route('/update_inventory/<int:id>', methods=['GET', 'POST'])
def update_inventory(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    if session.get('role') != 'admin':
        return "Only admins can edit items ❌"
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        price = request.form.get('price', 0)
        quantity = request.form.get('quantity', 0)
        
        if not name:
            return redirect('/inventory')
        
        try:
            price = int(price)
            quantity = int(quantity)
        except:
            return redirect('/inventory')
        
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE inventory SET name=?, price=?, quantity=? WHERE id=?", (name, price, quantity, id))
            conn.commit()
        return redirect('/inventory')
    else:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM inventory WHERE id=?", (id,))
            item = cur.fetchone()
        return render_template('edit_inventory.html', item=item, user=session.get('username'), role=normalize_role(session.get('role')))

@app.route('/customers')
def customers():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, phone, vehicle, user_id, model FROM customers ORDER BY name")
        customers = cur.fetchall()
    return render_template('customers.html', customers=customers, role=normalize_role(session.get('role')))

@app.route('/delete_customer/<int:id>')
def delete_customer(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM customers WHERE id=?", (id,))
        conn.commit()
    return redirect('/customers')
@app.route('/add_customer', methods=['POST'])
def add_customer():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    vehicle = request.form.get('vehicle', '').strip()
    model = request.form.get('model', '').strip()

    if not name or not phone:
        return redirect('/customers')

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(TRIM(username)) = LOWER(TRIM(?)) LIMIT 1", (name,))
        linked_user = cur.fetchone()
        linked_user_id = linked_user['id'] if linked_user else None
        cur.execute(
            "INSERT INTO customers (name, phone, vehicle, user_id, model) VALUES (?, ?, ?, ?, ?)",
            (name, phone, vehicle, linked_user_id, model),
        )
        conn.commit()

    return redirect('/customers')

@app.route('/update_customer/<int:id>', methods=['GET', 'POST'])
def update_customer(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        vehicle = request.form.get('vehicle', '')
        model = request.form.get('model', '').strip()
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM customers WHERE id=?", (id,))
            existing_customer = cur.fetchone()
            linked_user_id = existing_customer['user_id'] if existing_customer else None
            if not linked_user_id:
                cur.execute("SELECT id FROM users WHERE LOWER(TRIM(username)) = LOWER(TRIM(?)) LIMIT 1", (name,))
                linked_user = cur.fetchone()
                linked_user_id = linked_user['id'] if linked_user else None
            cur.execute(
                "UPDATE customers SET name=?, phone=?, vehicle=?, user_id=?, model=? WHERE id=?",
                (name, phone, vehicle, linked_user_id, model, id),
            )
            cur.execute(
                "UPDATE services SET customer_name=?, mobile_number=?, vehicle=?, vehicle_name=? WHERE customer_id=?",
                (name, phone, vehicle, model, id),
            )
            cur.execute("UPDATE bills SET customer=?, vehicle=? WHERE customer_id=?", (name, vehicle, id))
            conn.commit()
        return redirect('/customers')
    else:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, phone, vehicle, user_id, model FROM customers WHERE id=?", (id,))
            customer = cur.fetchone()
        return render_template('edit_customer.html', customer=customer)

@app.route('/services')
def services():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    selected_customer_id = request.args.get('customer_id', type=int)

    with get_db_connection() as conn:
        cur = conn.cursor()
        if selected_customer_id:
            cur.execute(
                """SELECT s.id,
                          COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), NULLIF(u.username, ''), 'Unknown Customer') AS customer_name,
                          COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                          COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                          COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                          COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '') AS service_items,
                          COALESCE(s.price, 0) AS total_price,
                          COALESCE(s.status, 'pending') AS status,
                          s.service_date,
                          s.service_time,
                          COALESCE(s.remarks, '') AS remarks,
                          s.bill_id,
                          b.payment_status,
                          s.created_at,
                          s.customer_id
                   FROM services s
                   LEFT JOIN customers c ON c.id = s.customer_id
                   LEFT JOIN users u ON u.id = s.user_id
                   LEFT JOIN bills b ON b.id = s.bill_id
                   WHERE s.customer_id=?
                   ORDER BY CASE WHEN LOWER(COALESCE(s.status, 'pending')) = 'pending' THEN 0 ELSE 1 END,
                            s.created_at DESC,
                            s.id DESC""",
                (selected_customer_id,),
            )
        else:
            cur.execute(
                """SELECT s.id,
                          COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), NULLIF(u.username, ''), 'Unknown Customer') AS customer_name,
                          COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                          COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                          COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                          COALESCE(NULLIF(s.service_items, ''), NULLIF(s.requested_service_items, ''), '') AS service_items,
                          COALESCE(s.price, 0) AS total_price,
                          COALESCE(s.status, 'pending') AS status,
                          s.service_date,
                          s.service_time,
                          COALESCE(s.remarks, '') AS remarks,
                          s.bill_id,
                          b.payment_status,
                          s.created_at,
                          s.customer_id
                   FROM services s
                   LEFT JOIN customers c ON c.id = s.customer_id
                   LEFT JOIN users u ON u.id = s.user_id
                   LEFT JOIN bills b ON b.id = s.bill_id
                   ORDER BY CASE WHEN LOWER(COALESCE(s.status, 'pending')) = 'pending' THEN 0 ELSE 1 END,
                            s.created_at DESC,
                            s.id DESC"""
            )
        services = cur.fetchall()
        cur.execute("SELECT * FROM customers ORDER BY name")
        customers = cur.fetchall()

    return render_template(
        'services.html',
        services=services,
        customers=customers,
        selected_customer_id=selected_customer_id,
        role=normalize_role(session.get('role')),
    )

@app.route('/add_service', methods=['POST'])
def add_service():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    customer_id = request.form.get('customer_id', type=int)
    vehicle = request.form.get('vehicle', '').strip()
    service_names = [name.strip() for name in request.form.getlist('service_names') if name.strip()]

    if not customer_id or not service_names:
        return redirect(build_customer_redirect('/services', customer_id))

    with get_db_connection() as conn:
        cur = conn.cursor()
        customer = get_customer_by_id(cur, customer_id)
        if not customer:
            return redirect('/services')

        linked_user_id = get_user_id_for_customer(cur, customer)
        if linked_user_id is None:
            return "This customer is not linked to a registered user account."
        vehicle = vehicle or (customer['vehicle'] or '')
        placeholders = ','.join('?' for _ in service_names)
        cur.execute(
            f"SELECT name, price, COALESCE(quantity, 0) AS quantity FROM inventory WHERE name IN ({placeholders})",
            service_names,
        )
        inventory_rows = {row['name']: row for row in cur.fetchall()}

        selected_products = [
            (service_name, inventory_rows[service_name]['price'])
            for service_name in service_names
            if service_name in inventory_rows
        ]

        if selected_products:
            inventory_deltas = build_inventory_deltas([], [service_name for service_name, _ in selected_products])
            inventory_error = validate_inventory_deltas(cur, inventory_deltas)
            if inventory_error:
                return inventory_error

            combined_service_names = ', '.join(service_name for service_name, _ in selected_products)
            total_price = sum(price for _, price in selected_products)
            apply_inventory_deltas(cur, inventory_deltas)
            cur.execute(
                "INSERT INTO services (name, price, customer_name, vehicle, customer_id, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                (combined_service_names, total_price, customer['name'], vehicle, customer_id, linked_user_id),
            )
            conn.commit()

    return redirect(build_customer_redirect('/services', customer_id))

@app.route('/delete_service/<int:id>')
def delete_service(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    redirect_customer_id = request.args.get('customer_id', type=int)

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM services WHERE id=?", (id,))
        conn.commit()
    return redirect(build_customer_redirect('/services', redirect_customer_id))

@app.route('/update_service/<int:id>', methods=['GET', 'POST'])
def update_service(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    if request.method == 'POST':
        status = request.form.get('status', 'pending').strip().lower()
        service_date = request.form.get('service_date') or None
        service_time = request.form.get('service_time') or None
        remarks = request.form.get('remarks', '').strip()
        selected_service_ids = []
        for raw_item_id in request.form.getlist('service_items'):
            try:
                selected_service_ids.append(int(raw_item_id))
            except (TypeError, ValueError):
                continue
        schedule_action = request.form.get('schedule_action', 'save')

        if status not in {'pending', 'in progress', 'completed'}:
            status = 'pending'

        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, customer_id, user_id, customer_name, vehicle_name, vehicle, price,
                       COALESCE(service_items, '') AS service_items,
                       COALESCE(requested_service_items, '') AS requested_service_items,
                       bill_id
                FROM services
                WHERE id=?
                """,
                (id,),
            )
            existing_service = cur.fetchone()
            if not existing_service:
                return redirect('/services')

            inventory_rows = get_inventory_rows_by_ids(cur, selected_service_ids)
            selected_service_entries = extract_selected_inventory_entries(request.form, inventory_rows, selected_service_ids)
            previous_items = parse_service_items(existing_service['service_items'])
            normalized_items = []
            for item in selected_service_entries:
                normalized_items.extend([item['name']] * item['quantity'])
            inventory_deltas = build_inventory_deltas(previous_items, normalized_items)
            inventory_error = validate_inventory_deltas(cur, inventory_deltas)
            if inventory_error:
                return inventory_error

            apply_inventory_deltas(cur, inventory_deltas)
            combined_service_names = format_service_item_quantities(selected_service_entries)
            total_price = sum(item['price'] * item['quantity'] for item in selected_service_entries)

            bill_id = existing_service['bill_id']
            cur.execute(
                """
                UPDATE services
                SET status=?, service_date=?, service_time=?, remarks=?, service_items=?, requested_service_items=?, name=?, price=?
                WHERE id=?
                """,
                (status, service_date, service_time, remarks, combined_service_names, combined_service_names, combined_service_names, total_price, id),
            )

            if schedule_action == 'save_and_create_bill':
                if status != 'completed':
                    return "Set status to completed before creating the bill."
                if not normalized_items:
                    return "Select at least one servicing option from inventory."

                service_for_bill = {
                    'bill_id': bill_id,
                    'price': total_price,
                    'customer_name': existing_service['customer_name'],
                    'customer_id': existing_service['customer_id'],
                    'user_id': existing_service['user_id'],
                    'service_items': combined_service_names,
                    'car_number': existing_service['vehicle'] or existing_service['vehicle_name'] or '',
                }
                bill_id = create_or_update_bill_for_service(cur, service_for_bill)
                cur.execute("UPDATE services SET bill_id=? WHERE id=?", (bill_id, id))
            elif bill_id and normalized_items:
                service_for_bill = {
                    'bill_id': bill_id,
                    'price': total_price,
                    'customer_name': existing_service['customer_name'],
                    'customer_id': existing_service['customer_id'],
                    'user_id': existing_service['user_id'],
                    'service_items': combined_service_names,
                    'car_number': existing_service['vehicle'] or existing_service['vehicle_name'] or '',
                }
                create_or_update_bill_for_service(cur, service_for_bill)

            conn.commit()
        return redirect('/services')
    else:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT s.id,
                       COALESCE(NULLIF(s.customer_name, ''), NULLIF(c.name, ''), NULLIF(u.username, ''), 'Unknown Customer') AS customer_name,
                       COALESCE(NULLIF(s.mobile_number, ''), NULLIF(c.phone, ''), '-') AS mobile_number,
                       COALESCE(NULLIF(s.vehicle, ''), NULLIF(c.vehicle, ''), '-') AS car_number,
                       COALESCE(NULLIF(s.vehicle_name, ''), NULLIF(c.model, ''), '-') AS model,
                       COALESCE(s.service_items, '') AS service_items,
                       COALESCE(s.requested_service_items, '') AS requested_service_items,
                       COALESCE(s.price, 0) AS total_price,
                       COALESCE(s.status, 'pending') AS status,
                       s.service_date,
                       s.service_time,
                       COALESCE(s.remarks, '') AS remarks,
                       s.customer_id,
                       s.bill_id
                FROM services s
                LEFT JOIN customers c ON c.id = s.customer_id
                LEFT JOIN users u ON u.id = s.user_id
                WHERE s.id=?
                """,
                (id,),
            )
            service = cur.fetchone()
            cur.execute("SELECT id, name, price, COALESCE(quantity, 0) AS quantity FROM inventory ORDER BY name")
            inventory_items = cur.fetchall()
        if not service:
            return redirect('/services')
        return render_template(
            'edit_service.html',
            service=service,
            inventory_items=inventory_items,
            selected_service_quantities=parse_service_item_quantities(service['service_items'] or service['requested_service_items']),
            selected_service_summary=service['service_items'] or service['requested_service_items'] or 'None selected',
            role=normalize_role(session.get('role')),
        )
@app.route('/billing')
def billing():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    selected_customer_id = request.args.get('customer_id', type=int)

    with get_db_connection() as conn:
        cur = conn.cursor()
        if selected_customer_id:
            cur.execute(
                f"""SELECT {BILL_SELECT_FIELDS}
                   FROM bills
                   WHERE customer_id=?
                   ORDER BY id DESC""",
                (selected_customer_id,),
            )
        else:
            cur.execute(
                f"""SELECT {BILL_SELECT_FIELDS}
                   FROM bills
                   ORDER BY id DESC"""
            )
        bills = cur.fetchall()

        cur.execute("SELECT * FROM customers ORDER BY name")
        customers = cur.fetchall()

        cur.execute(
            """SELECT s.id,
                      s.name,
                      s.price,
                      COALESCE(c.name, s.customer_name) AS customer_name,
                      COALESCE(NULLIF(s.vehicle, ''), c.vehicle) AS vehicle,
                      s.customer_id
               FROM services s
               LEFT JOIN customers c ON c.id = s.customer_id
               WHERE COALESCE(s.price, 0) > 0
               ORDER BY s.created_at DESC, s.id DESC"""
        )
        services = cur.fetchall()

    return render_template(
        'billing.html',
        bills=bills,
        customers=customers,
        services=services,
        selected_customer_id=selected_customer_id,
        role=normalize_role(session.get('role')),
    )

@app.route('/pay-bill/<int:id>', methods=['GET', 'POST'])
def customer_pay_bill(id):
    customer_redirect = customer_only_redirect()
    if customer_redirect:
        return customer_redirect

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {BILL_SELECT_FIELDS}
                FROM bills
                WHERE id=? AND user_id=?""",
            (id, session['user_id']),
        )
        bill = cur.fetchone()
        if not bill:
            return redirect('/customer-dashboard')

        if request.method == 'POST':
            payment_method = request.form.get('payment_method', '').strip().lower()
            customer_payment_note = request.form.get('customer_payment_note', '').strip()
            card_holder_name = request.form.get('card_holder_name', '').strip()
            card_number = request.form.get('card_number', '').strip()
            card_bank = request.form.get('card_bank', '').strip()
            card_expiry = request.form.get('card_expiry', '').strip()
            customer_payment_screenshot_path = request.form.get('existing_customer_payment_screenshot', '').strip() or None

            if payment_method not in {'online', 'card'}:
                return "Choose online or card payment."

            if payment_method == 'card':
                if not all([card_holder_name, card_number, card_bank, card_expiry]):
                    return "Card details are required for card payment."
                card_number = mask_card_number(card_number)
            else:
                card_holder_name = None
                card_number = None
                card_bank = None
                card_expiry = None
                customer_payment_screenshot_path = None

            if payment_method == 'online' and 'customer_payment_screenshot' in request.files:
                file = request.files['customer_payment_screenshot']
                if file and file.filename != '':
                    if not allowed_file(file.filename):
                        return "Upload a valid payment screenshot image."
                    filename = secure_filename(file.filename)
                    import time
                    filename = f"payment_proof_{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    customer_payment_screenshot_path = f"uploads/{filename}"

            if payment_method == 'online' and not customer_payment_screenshot_path:
                return "Please upload the payment screenshot for online payment."

            cur.execute(
                """
                UPDATE bills
                SET payment_method=?, card_holder_name=?, card_number=?, card_bank=?, card_expiry=?,
                    payment_status='unpaid', customer_payment_status='submitted', customer_payment_note=?,
                    customer_payment_screenshot_path=?
                WHERE id=? AND user_id=?
                """,
                (
                    payment_method,
                    card_holder_name,
                    card_number,
                    card_bank,
                    card_expiry,
                    customer_payment_note,
                    customer_payment_screenshot_path,
                    id,
                    session['user_id'],
                ),
            )
            conn.commit()
            return redirect('/customer-dashboard')

    return render_template(
        'customer_pay_bill.html',
        bill=bill,
        garage_info=get_garage_profile(),
        role=normalize_role(session.get('role')),
        customer_online_qr_path=bill['qr_code_path'] or 'static/payment-qr.jpeg',
    )

@app.route('/mark-bill-paid/<int:id>', methods=['POST'])
def mark_bill_paid(id):
    customer_redirect = customer_only_redirect()
    if customer_redirect:
        return customer_redirect

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE bills
            SET customer_payment_status='submitted'
            WHERE id=? AND user_id=? AND payment_status != 'paid'
            """,
            (id, session['user_id']),
        )
        conn.commit()

    return redirect('/customer-dashboard')

@app.route('/review-bill-payment/<int:id>', methods=['POST'])
def review_bill_payment(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    review_action = request.form.get('review_action', '').strip().lower()
    if review_action == 'confirm_paid':
        payment_status = 'paid'
        customer_payment_status = 'submitted'
    else:
        payment_status = 'unpaid'
        customer_payment_status = 'not_submitted'

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE bills
            SET payment_status=?, customer_payment_status=?
            WHERE id=?
            """,
            (payment_status, customer_payment_status, id),
        )
        conn.commit()

    return redirect('/billing')

@app.route('/add_bill', methods=['POST'])
def add_bill():
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    customer_id = request.form.get('customer_id', type=int)
    service_ids = []
    for raw_service_id in request.form.getlist('service_ids'):
        try:
            service_ids.append(int(raw_service_id))
        except (TypeError, ValueError):
            continue

    payment_method = request.form.get('payment_method', 'cash')
    payment_status = request.form.get('payment_status', 'unpaid')
    card_holder_name = request.form.get('card_holder_name', '').strip()
    card_number = request.form.get('card_number', '').strip()
    card_cvv = request.form.get('card_cvv', '').strip()
    card_bank = request.form.get('card_bank', '').strip()
    card_expiry = request.form.get('card_expiry', '').strip()
    discount_percentage = request.form.get('discount_percentage', 0)
    gst_percentage = request.form.get('gst_percentage', 18)

    if not customer_id or not service_ids:
        return redirect(build_customer_redirect('/billing', customer_id))

    qr_code_path = None

    if payment_method == 'card':
        if not all([card_holder_name, card_number, card_bank, card_expiry]):
            return redirect(build_customer_redirect('/billing', customer_id))
        card_number = mask_card_number(card_number)
        card_cvv = None
    else:
        card_holder_name = None
        card_number = None
        card_cvv = None
        card_bank = None
        card_expiry = None

    # Handle QR code upload for online payments
    if payment_method == 'online' and 'qr_code' in request.files:
        file = request.files['qr_code']
        if file and file.filename != '' and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp to avoid file name conflicts
            import time
            filename = f"{int(time.time())}_{filename}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            qr_code_path = f"uploads/{filename}"
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        customer = get_customer_by_id(cur, customer_id)
        if not customer:
            return redirect('/billing')
        if not customer['user_id']:
            return "This customer is not linked to a registered website account."

        placeholders = ','.join('?' for _ in service_ids)
        cur.execute(
            f"""SELECT id, name, price, vehicle
                FROM services
                WHERE customer_id=? AND id IN ({placeholders}) AND COALESCE(price, 0) > 0""",
            [customer_id, *service_ids],
        )
        selected_services = cur.fetchall()
        if selected_services:
            subtotal = sum(service['price'] or 0 for service in selected_services)
            totals = calculate_bill_totals(subtotal, discount_percentage, gst_percentage)
            service_names = ', '.join(service['name'] for service in selected_services)
            vehicle = next(
                (service['vehicle'] for service in selected_services if service['vehicle']),
                customer['vehicle'] or '',
            )
            cur.execute(
                """INSERT INTO bills (
                       customer, amount, payment_method, payment_status, qr_code_path,
                       card_holder_name, card_number, card_cvv, card_bank, card_expiry,
                       customer_id, service_names, vehicle, subtotal, discount_percentage,
                       discount_amount, gst_percentage, gst_amount, user_id, customer_payment_status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    customer['name'],
                    totals['total_amount'],
                    payment_method,
                    payment_status,
                    qr_code_path,
                    card_holder_name,
                    card_number,
                    card_cvv,
                    card_bank,
                    card_expiry,
                    customer_id,
                    service_names,
                    vehicle,
                    totals['subtotal'],
                    totals['discount_percentage'],
                    totals['discount_amount'],
                    totals['gst_percentage'],
                    totals['gst_amount'],
                    customer['user_id'],
                    'not_submitted',
                ),
            )
            conn.commit()

    return redirect(build_customer_redirect('/billing', customer_id))

@app.route('/delete_bill/<int:id>')
def delete_bill(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    redirect_customer_id = request.args.get('customer_id', type=int)

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM bills WHERE id=?", (id,))
        conn.commit()
    return redirect(build_customer_redirect('/billing', redirect_customer_id))

@app.route('/update_bill/<int:id>', methods=['GET', 'POST'])
def update_bill(id):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect

    if request.method == 'POST':
        customer_id = request.form.get('customer_id', type=int)
        customer = None
        bill_user_id = None
        subtotal = request.form.get('subtotal')
        payment_method = request.form.get('payment_method', 'cash')
        payment_status = request.form.get('payment_status', 'unpaid')
        customer_payment_status = 'submitted' if payment_status == 'paid' else 'not_submitted'
        card_holder_name = request.form.get('card_holder_name', '').strip()
        card_number = request.form.get('card_number', '').strip()
        card_cvv = request.form.get('card_cvv', '').strip()
        card_bank = request.form.get('card_bank', '').strip()
        card_expiry = request.form.get('card_expiry', '').strip()
        service_names = request.form.get('service_names', '').strip()
        vehicle = request.form.get('vehicle', '').strip()
        discount_percentage = request.form.get('discount_percentage', 0)
        gst_percentage = request.form.get('gst_percentage', 18)

        qr_code_path = request.form.get('existing_qr_code')

        totals = calculate_bill_totals(subtotal, discount_percentage, gst_percentage)

        if payment_method == 'card':
            if not all([card_holder_name, card_number, card_bank, card_expiry]):
                return redirect(f'/update_bill/{id}')
            card_number = mask_card_number(card_number)
            card_cvv = None
            qr_code_path = None
        else:
            card_holder_name = None
            card_number = None
            card_cvv = None
            card_bank = None
            card_expiry = None

        if payment_method != 'online':
            qr_code_path = None

        # Handle QR code upload for online payments
        if payment_method == 'online' and 'qr_code' in request.files:
            file = request.files['qr_code']
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                import time
                filename = f"{int(time.time())}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                qr_code_path = f"uploads/{filename}"
        
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM bills WHERE id=?", (id,))
            existing_bill = cur.fetchone()
            bill_user_id = existing_bill['user_id'] if existing_bill else None

            if customer_id:
                customer_row = get_customer_by_id(cur, customer_id)
                if customer_row:
                    customer = customer_row['name']
                    vehicle = vehicle or (customer_row['vehicle'] or '')
                    bill_user_id = customer_row['user_id']

            if not customer:
                customer = request.form.get('customer', '').strip()

            cur.execute(
                """UPDATE bills
                   SET customer=?, amount=?, payment_method=?, payment_status=?, qr_code_path=?,
                       card_holder_name=?, card_number=?, card_cvv=?, card_bank=?, card_expiry=?,
                       customer_id=?, service_names=?, vehicle=?, subtotal=?, discount_percentage=?,
                       discount_amount=?, gst_percentage=?, gst_amount=?, user_id=?, customer_payment_status=?
                   WHERE id=?""",
                (
                    customer,
                    totals['total_amount'],
                    payment_method,
                    payment_status,
                    qr_code_path,
                    card_holder_name,
                    card_number,
                    card_cvv,
                    card_bank,
                    card_expiry,
                    customer_id,
                    service_names,
                    vehicle,
                    totals['subtotal'],
                    totals['discount_percentage'],
                    totals['discount_amount'],
                    totals['gst_percentage'],
                    totals['gst_amount'],
                    bill_user_id,
                    customer_payment_status,
                    id,
                ),
            )
            conn.commit()
        return redirect('/billing')
    else:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT {BILL_SELECT_FIELDS}
                   FROM bills WHERE id=?""",
                (id,),
            )
            bill = cur.fetchone()
            cur.execute("SELECT * FROM customers ORDER BY name")
            customers = cur.fetchall()
        return render_template('edit_bill.html', bill=bill, customers=customers, role=normalize_role(session.get('role')))

@app.route('/print_bill/<int:id>')
def print_bill(id):
    if 'user_id' not in session:
        return redirect('/')

    with get_db_connection() as conn:
        cur = conn.cursor()
        if is_admin_session():
            cur.execute(
                f"""SELECT {BILL_SELECT_FIELDS}
                   FROM bills WHERE id=?""",
                (id,),
            )
            back_url = '/billing'
        else:
            cur.execute(
                f"""SELECT {BILL_SELECT_FIELDS}
                   FROM bills WHERE id=? AND user_id=?""",
                (id, session['user_id']),
            )
            back_url = '/customer-dashboard'
        bill = cur.fetchone()

    if not bill:
        return redirect(back_url)

    return render_template('bill_print.html', bill=bill, garage_info=get_garage_profile(), back_url=back_url)

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    admin_redirect = admin_only_redirect()
    if admin_redirect:
        return admin_redirect
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.errorhandler(404)
def page_not_found(e):
    return "Page not found ❌", 404

@app.errorhandler(500)
def server_error(e):
    return "Server error occurred ❌", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
