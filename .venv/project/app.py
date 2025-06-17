from flask import *
import psycopg2
import pandas as pd
from flask import send_file, Response
from datetime import datetime, timedelta
import logging
from utils.reserve_logic import calculate_reserve, calculate_all_reserves
from db_connect import get_db_connection
from collections import defaultdict
import openpyxl
from io import BytesIO
import chardet

app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Настройка логирования
logging.basicConfig(
    filename='reserve_bot.log',
    level=logging.INFO,
    encoding='utf-8',
    format='%(asctime)s - %(levelname)s - %(message)s')

# Фильтр для форматирования дат
app.jinja_env.filters['russian_date'] = lambda x: datetime.strptime(x, '%Y-%m-%d').strftime('%d.%m.%Y')

def excel_date_to_datetime(excel_serial):
    try:
        return datetime(1899, 12, 30) + timedelta(days=int(excel_serial))
    except Exception:
        return None

def detect_encoding(file):
    raw_data = file.read()
    result = chardet.detect(raw_data)
    file.seek(0)
    return result['encoding']

def normalize_1c_columns(df):
    df.columns = df.columns.str.strip().str.lower()
    column_mapping = {
        'номенклатура': 'name',
        'количество': 'quantity',
        'цена': 'price',
        'срок хранения': 'shelf_life_months',
        'дата поступления': 'received_date',
        'вероятность использования': 'usage_probability',
        'рыночная цена': 'market_price',
        'категория': 'category'
    }
    df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns}, inplace=True)
    return df

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload_excel():
    if request.method == 'POST':
        conn = None
        try:
            file = request.files['file']
            file_type = request.form.get('file_type', 'standard')
            if not file.filename.endswith(('.xlsx', '.xls', '.csv')):
                flash('Ошибка: файл должен быть в формате .xlsx, .xls или .csv', 'danger')
                return redirect('/upload')

            if file.filename.endswith('.csv'):
                encoding = detect_encoding(file)
                df = pd.read_csv(file, encoding=encoding)
            else:
                df = pd.read_excel(file)

            if file_type == '1c':
                df = normalize_1c_columns(df)

            df.columns = df.columns.str.strip().str.lower()

            required_columns = ['name', 'quantity', 'price']
            if not all(col in df.columns for col in required_columns):
                flash('Ошибка: файл должен содержать столбцы: name, quantity, price', 'danger')
                return redirect('/upload')

            conn = get_db_connection()
            cur = conn.cursor()
            upload_time = datetime.now()

            for _, row in df.iterrows():
                raw_date = row.get("received_date")
                if pd.isna(raw_date):
                    received_date = None
                elif isinstance(raw_date, (float, int)):
                    received_date = excel_date_to_datetime(raw_date)
                elif isinstance(raw_date, datetime):
                    received_date = raw_date
                else:
                    try:
                        received_date = pd.to_datetime(raw_date)
                    except Exception:
                        received_date = None

                cur.execute('''
                    INSERT INTO inventory_items
                    (name, category, quantity, price, shelf_life_months, received_date, usage_probability, market_price, upload_timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    row.get("name"),
                    row.get("category"),
                    int(row.get("quantity", 0)),
                    float(row.get("price", 0)),
                    int(row.get("shelf_life_months", 12)),
                    received_date,
                    float(row.get("usage_probability", 100)),
                    float(row.get("market_price")) if not pd.isna(row.get("market_price")) else None,
                    upload_time
                ))

            conn.commit()
            flash('Данные успешно загружены', 'success')
        except Exception as e:
            flash(f'Ошибка при загрузке файла: {str(e)}', 'danger')
            logging.error(f"Ошибка при загрузке файла: {str(e)}")
        finally:
            if conn is not None:
                conn.close()
        return redirect('/')
    return render_template('upload.html')

@app.route('/inventory')
def show_inventory():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        # Получаем все данные, сгруппируем их по upload_timestamp
        cur.execute('SELECT * FROM inventory_items ORDER BY upload_timestamp, id')
        rows = cur.fetchall()

        # Группируем по upload_timestamp
        grouped_items = {}
        for row in rows:
            key = row['upload_timestamp'].strftime("%Y-%m-%d %H:%M:%S") if row['upload_timestamp'] else "Без даты"
            grouped_items.setdefault(key, []).append(row)

        return render_template('inventory.html', grouped_items=grouped_items)
    except Exception as e:
        logging.error(f"Ошибка в маршруте /inventory: {str(e)}")
        flash(f'Ошибка при загрузке списка МПЗ: {str(e)}', 'danger')
        return render_template('inventory.html', grouped_items={})
    finally:
        cur.close()
        conn.close()

@app.route('/calculate', methods=['POST'])
def calculate_reserve_route():
    method = request.form.get('method')
    upload_time_str = request.form.get('upload_time')
    logging.info(f"{method} method === {upload_time_str} upload_time")

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        if upload_time_str == "all":
            calculate_all_reserves(conn, override_method=method)
        else:
            # Парсим строку upload_time и округляем до секунд
            upload_time_dt = datetime.strptime(upload_time_str, '%Y-%m-%d %H:%M:%S')

            # Округляем метку времени в БД до секунд
            cur.execute('''
                SELECT * FROM inventory_items 
                WHERE DATE_TRUNC('second', upload_timestamp) = %s
            ''', (upload_time_dt,))
            items = cur.fetchall()

            today_str = datetime.today().strftime('%Y-%m-%d')

            for item in items:
                cur.execute('''
                    SELECT calculated_reserve FROM reserve_calculations
                    WHERE item_id = %s ORDER BY calculation_date DESC LIMIT 1
                ''', (item['id'],))
                row = cur.fetchone()
                prev_reserve = row['calculated_reserve'] if row else 0

                reserve = calculate_reserve(item, override_method=method, prev_reserve=prev_reserve)

                cur.execute('''
                    INSERT INTO reserve_calculations (item_id, calculated_reserve, method_used, calculation_date)
                    VALUES (%s, %s, %s, %s)
                ''', (item['id'], reserve, method, today_str))

            cur.close()

        conn.commit()
        flash(f'Расчёт резервов выполнен методом "{method}" для документа "{upload_time_str}".', 'success')

    except Exception as e:
        conn.rollback()
        flash(f'Ошибка при расчёте резервов: {str(e)}', 'danger')

    finally:
        conn.close()

    return redirect(url_for('show_inventory'))

@app.route('/reserves')
def show_reserves():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        cur.execute('''
            SELECT r.item_id, i.name, r.calculated_reserve, r.method_used, r.calculation_date
            FROM reserve_calculations r
            JOIN inventory_items i ON r.item_id = i.id
            ORDER BY r.calculation_date DESC, i.name
        ''')
        reserves = cur.fetchall()

        # Преобразуем в список словарей для шаблона
        reserve_list = [
            {
                'item_id': row['item_id'],
                'name': row['name'],
                'calculated_reserve': row['calculated_reserve'],
                'method_used': row['method_used'],
                'calculation_date': row['calculation_date'].strftime('%Y-%m-%d') if row['calculation_date'] else '—'
            }
            for row in reserves
        ]
        logging.info(f"Рассчитан резерв для товара {reserve_list}")
        return render_template('reserve.html', reserves=reserve_list)

    except Exception as e:
        logging.error(f"Ошибка в маршруте /reserves: {str(e)}")
        flash(f'Ошибка при загрузке резервов: {str(e)}', 'danger')
        return render_template('reserve.html', reserves=[])

    finally:
        cur.close()
        conn.close()

@app.route('/export_reserves')
def export_reserves():
    import openpyxl
    import csv
    from io import BytesIO, StringIO
    from flask import send_file, Response, request
    from collections import defaultdict
    from datetime import datetime
    import logging

    export_format = request.args.get('format', 'excel').lower()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        cur.execute('''
            SELECT r.item_id, i.name, r.calculated_reserve, r.method_used, r.calculation_date
            FROM reserve_calculations r
            JOIN inventory_items i ON r.item_id = i.id
            ORDER BY r.calculation_date DESC, i.name
        ''')
        reserves = cur.fetchall()

        if export_format == '1c_csv':
            output = StringIO()
            writer = csv.writer(output, delimiter=';', lineterminator='\n')
            writer.writerow(['Номенклатура', 'Количество', 'Сумма', 'Комментарий', 'Дата'])

            for row in reserves:
                writer.writerow([
                    row['name'] or '',
                    1,
                    float(row['calculated_reserve']),
                    row['method_used'],
                    row['calculation_date'].strftime('%Y-%m-%d') if row['calculation_date'] else ''
                ])

            response = Response(output.getvalue().encode('utf-8-sig'), mimetype='text/csv')
            filename = f"reserves_for_1c_{datetime.now().strftime('%Y%m%d')}.csv"
            response.headers.set("Content-Disposition", f"attachment; filename={filename}")
            return response

        elif export_format in ('1c', 'excel'):
            wb = openpyxl.Workbook()

            if export_format == '1c':
                ws = wb.active
                ws.title = "Для 1С"

                headers = ['Номенклатура', 'Количество', 'Сумма', 'Комментарий', 'Дата']
                ws.append(headers)

                for row in reserves:
                    ws.append([
                        row['name'] or '',
                        1,
                        float(row['calculated_reserve']),
                        row['method_used'],
                        row['calculation_date'].strftime('%Y-%m-%d') if row['calculation_date'] else ''
                    ])
            else:
                default_sheet = wb.active
                wb.remove(default_sheet)

                grouped = defaultdict(list)
                for row in reserves:
                    date_str = row['calculation_date'].strftime('%Y-%m-%d') if row['calculation_date'] else '—'
                    grouped[date_str].append(row)

                for date_str, items in grouped.items():
                    ws = wb.create_sheet(title=date_str)
                    headers = ['№', 'Наименование', 'Метод расчёта', 'Рассчитанный резерв']
                    ws.append(headers)

                    for idx, row in enumerate(items, start=1):
                        ws.append([
                            idx,
                            row['name'] or '—',
                            row['method_used'],
                            float(row['calculated_reserve'])
                        ])

            for sheet in wb.worksheets:
                for col in sheet.columns:
                    max_length = 0
                    column = col[0].column_letter
                    for cell in col:
                        try:
                            if cell.value:
                                max_length = max(max_length, len(str(cell.value)))
                        except:
                            pass
                    sheet.column_dimensions[column].width = max_length + 2

            output = BytesIO()
            wb.save(output)
            output.seek(0)

            filename = (
                f"reserves_for_1c_{datetime.now().strftime('%Y%m%d')}.xlsx"
                if export_format == '1c'
                else f"reserves_export_{datetime.now().strftime('%Y%m%d')}.xlsx"
            )

            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                download_name=filename,
                as_attachment=True
            )

        else:
            return Response(f"Неверный формат экспорта: {export_format}", status=400)

    except Exception as e:
        logging.error(f"Ошибка при экспорте резервов ({export_format}): {e}")
        return Response(f"Ошибка при экспорте: {e}", status=500)

    finally:
        cur.close()
        conn.close()




@app.route('/delete_by_upload_time', methods=['POST'])
def delete_by_upload_time():
    data = request.get_json()
    if not data or 'upload_time' not in data:
        return jsonify({'error': 'upload_time не указан'}), 400

    upload_time_str = data['upload_time']

    try:
        upload_time = datetime.strptime(upload_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify({'error': 'Неверный формат upload_time. Ожидается YYYY-MM-DD HH:MM:SS'}), 400

    start_time = upload_time
    end_time = upload_time + timedelta(seconds=1)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('DELETE FROM inventory_items WHERE upload_timestamp >= %s AND upload_timestamp < %s', (start_time, end_time))
        deleted_count = cur.rowcount
        conn.commit()
        return jsonify({'message': f'Удалено записей: {deleted_count} для даты загрузки {upload_time_str}'}), 200
    except Exception as e:
        conn.rollback()
        logging.error(f"Ошибка при удалении по upload_time={upload_time_str}: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
