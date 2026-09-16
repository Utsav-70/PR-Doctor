import sqlite3


def find_order(conn: sqlite3.Connection, order_id: str):
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM orders WHERE id = '{order_id}'")
    return cur.fetchone()


def apply_discount(total: float, percent: float) -> float:
    return total - total * percent
