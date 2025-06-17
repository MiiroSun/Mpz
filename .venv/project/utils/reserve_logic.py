from datetime import datetime
import psycopg2
import psycopg2.extras
import logging
from db_connect import get_db_connection

# Настройка логирования
logging.basicConfig(filename='reserve_bot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',  encoding='utf-8',)


def validate_item(item):
    """Валидация входных данных для расчета резерва."""
    required_fields = ["quantity", "price", "shelf_life_months", "received_date"]
    for field in required_fields:
        if field not in item or item[field] is None:
            raise ValueError(f"Поле {field} отсутствует или равно None")

    if item["quantity"] < 0:
        raise ValueError("Количество не может быть отрицательным")
    if item["price"] < 0:
        raise ValueError("Цена не может быть отрицательной")
    if item["shelf_life_months"] < 0:
        raise ValueError("Срок хранения не может быть отрицательным")
    if "usage_probability" in item and item["usage_probability"] is not None and not (
            0 <= item["usage_probability"] <= 100):
        raise ValueError("Вероятность использования должна быть в диапазоне [0, 100]")


def calculate_reserve(item, override_method=None, prev_reserve=0):
    """Расчет резерва для одного товара с учетом РСБУ."""
    try:
        item = dict(item)
        validate_item(item)

        qty = item["quantity"]
        price = item["price"]
        shelf_life = item["shelf_life_months"]
        received_date = item["received_date"]
        usage_prob = item.get("usage_probability", 100)
        market_price = item.get("market_price", None)
        method = override_method
        max_reserve = qty * price

        try:
            received_dt = datetime.strptime(str(received_date).split()[0], '%Y-%m-%d')
        except (ValueError, AttributeError):
            logging.warning(f"Некорректный формат даты: {received_date}, используется текущая дата")
            received_dt = datetime.today()

        today = datetime.today()
        months = (today.year - received_dt.year) * 12 + (today.month - received_dt.month)

        reserve = 0
        if method == 'standard':
            coef_storage = min(1, months / shelf_life) if shelf_life > 0 else 1
            unused_share = 1 - usage_prob / 100
            reserve_by_usage = qty * price * coef_storage * unused_share
            reserve_by_market = max(price - market_price, 0) * qty if market_price is not None else 0
            reserve = max(reserve_by_usage, reserve_by_market)

        elif method == 'shelf_life':
            coef_storage = min(1, months / shelf_life) if shelf_life > 0 else 1
            reserve = qty * price * coef_storage
            if market_price is not None:
                reserve = min(reserve, max(price - market_price, 0) * qty)

        elif method == 'market':
            if market_price is not None and market_price >= 0:
                reserve = max(price - market_price, 0) * qty

        elif method == 'conservative':
            if shelf_life and months > shelf_life:
                reserve = qty * price
            else:
                coef = min(1, months / (shelf_life * 1.5)) if shelf_life > 0 else 0
                reserve = qty * price * coef
                if market_price is not None:
                    reserve = max(reserve, max(price - market_price, 0) * qty)

        reserve = min(round(reserve, 2), max_reserve)

        name = item.get("name", "unknown")
        if prev_reserve > reserve:
            logging.info(f"Восстановление резерва для товара {name}: {prev_reserve - reserve}")
        elif prev_reserve < reserve:
            logging.info(f"Начисление резерва для товара {name}: {reserve - prev_reserve}")
        logging.info(f"Рассчитан резерв для товара {name}: {reserve} (метод: {method})")

        return reserve

    except Exception as e:
        name = item.get("name", "unknown")
        logging.error(f"Ошибка при расчете резерва для товара {name}: {str(e)}")
        raise


def calculate_all_reserves(conn, override_method=None):
    """Расчет резервов для всех товаров."""
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Получаем все товары
        cur.execute('SELECT * FROM inventory_items')
        items = cur.fetchall()
        today_str = datetime.today().strftime('%Y-%m-%d')

        # Получение предыдущих резервов
        cur.execute('''
            SELECT item_id, MAX(calculation_date) AS last_date 
            FROM reserve_calculations 
            GROUP BY item_id
        ''')
        last_dates = {row["item_id"]: row["last_date"] for row in cur.fetchall()}

        reserves_prev = {}
        for item_id, last_date in last_dates.items():
            cur.execute('''
                SELECT calculated_reserve 
                FROM reserve_calculations 
                WHERE item_id = %s AND calculation_date = %s
            ''', (item_id, last_date))
            row = cur.fetchone()
            reserves_prev[item_id] = row["calculated_reserve"] if row else 0

        for item in items:
            prev_reserve = reserves_prev.get(item["id"], 0)
            reserve = calculate_reserve(item, override_method, prev_reserve)
            method_used = override_method
            cur.execute('''
                INSERT INTO reserve_calculations (item_id, calculated_reserve, method_used, calculation_date)
                VALUES (%s, %s, %s, %s)
            ''', (item["id"], reserve, method_used, today_str))

        conn.commit()
        logging.info("Расчет резервов успешно завершен")

    except Exception as e:
        logging.error(f"Ошибка при расчете резервов: {str(e)}")
        conn.rollback()
        raise
