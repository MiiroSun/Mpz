import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE = {
    'dbname': 'mpz',
    'user': 'postgres',
    'password': '1234',
    'host': 'localhost',
    'port': 5432
}

def get_db_connection():
    """Подключение к PostgreSQL и возврат курсора как словаря."""
    conn = psycopg2.connect(**DATABASE)
    return conn