import os
import json
import tempfile
from datetime import datetime
from dataclasses import asdict

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from parser import parse_ozon_bank_pdf, BankStatement, StatementHeader, StatementFooter, Transaction

app = Flask(__name__)
app.secret_key = os.urandom(24)

ALLOWED_EXTENSIONS = {"pdf"}
DATA_DIR = os.path.dirname(__file__)
HISTORY_FILE = os.path.join(DATA_DIR, "upload_history.json")
STORE_FILE = os.path.join(DATA_DIR, "transactions_store.json")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# === История загрузок ===

def load_history() -> list[dict]:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list[dict]):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# === Хранилище транзакций ===

def load_store() -> dict:
    """Загружает накопленные данные: header, transactions (по document ID), footer."""
    if os.path.exists(STORE_FILE):
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"header": {}, "transactions": {}, "footer": {}}


def save_store(store: dict):
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def dmy_to_sortable(dmy: str) -> str:
    """Конвертация dd.mm.yyyy -> yyyy-mm-dd для сортировки."""
    parts = dmy.split(".")
    if len(parts) != 3:
        return dmy
    return f"{parts[2]}-{parts[1]}-{parts[0]}"


def merge_statement(store: dict, statement: BankStatement) -> tuple[dict, int]:
    """Мержит новую выписку в хранилище. Возвращает (store, кол-во новых транзакций)."""
    # Обновляем header — расширяем период
    h = store.get("header", {})
    new_h = asdict(statement.header)

    if not h:
        h = new_h
    else:
        # Расширяем период до максимального диапазона
        old_from = dmy_to_sortable(h.get("period_from", ""))
        new_from = dmy_to_sortable(new_h.get("period_from", ""))
        if new_from and (not old_from or new_from < old_from):
            h["period_from"] = new_h["period_from"]

        old_to = dmy_to_sortable(h.get("period_to", ""))
        new_to = dmy_to_sortable(new_h.get("period_to", ""))
        if new_to and (not old_to or new_to > old_to):
            h["period_to"] = new_h["period_to"]

        # Обновляем остальные поля из последней выписки
        for key in ("owner", "account_number", "account_opened", "currency",
                     "document_number", "document_date", "generated_at"):
            if new_h.get(key):
                h[key] = new_h[key]

    store["header"] = h

    # Мержим транзакции по document ID (ключ — номер документа)
    txns = store.get("transactions", {})
    new_count = 0
    for t in statement.transactions:
        if t.document not in txns:
            txns[t.document] = asdict(t)
            new_count += 1
    store["transactions"] = txns

    # Пересчитываем итоги из реальных данных
    total_credits = 0.0
    total_debits = 0.0
    for t_data in txns.values():
        if t_data["is_credit"]:
            total_credits += t_data["amount"]
        else:
            total_debits += t_data["amount"]

    store["footer"] = {
        "total_credits": round(total_credits, 2),
        "total_debits": round(total_debits, 2),
        "closing_balance": round(
            h.get("opening_balance", 0) + total_credits - total_debits, 2
        ),
    }

    return store, new_count


def store_to_statement(store: dict) -> BankStatement | None:
    """Конвертирует хранилище в BankStatement для шаблона."""
    if not store.get("transactions"):
        return None

    header_data = store.get("header", {})
    header = StatementHeader(**{
        k: header_data.get(k, v)
        for k, v in StatementHeader().__dict__.items()
    })

    footer_data = store.get("footer", {})
    footer = StatementFooter(**{
        k: footer_data.get(k, v)
        for k, v in StatementFooter().__dict__.items()
    })

    # Сортировка транзакций по дате+времени (новые сверху)
    txn_list = list(store["transactions"].values())
    txn_list.sort(
        key=lambda t: (dmy_to_sortable(t["date"]), t.get("time", "")),
        reverse=True,
    )
    transactions = [Transaction(**t) for t in txn_list]

    return BankStatement(header=header, transactions=transactions, footer=footer)


@app.route("/", methods=["GET"])
def index():
    store = load_store()
    statement = store_to_statement(store)
    return render_template("main.html", statement=statement)


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        flash("Файл не выбран")
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "":
        flash("Файл не выбран")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Допустимы только PDF файлы")
        return redirect(url_for("index"))

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        parsed = parse_ozon_bank_pdf(tmp_path)

        store = load_store()
        store, new_count = merge_statement(store, parsed)
        save_store(store)

        total = len(store["transactions"])
        if new_count > 0:
            flash(f"Добавлено {new_count} новых операций (всего: {total})")
        else:
            flash(f"Новых операций не найдено. Все {total} уже загружены.")

        # Сохраняем историю
        history = load_history()
        history.insert(0, {
            "filename": file.filename,
            "uploaded_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "period_from": parsed.header.period_from,
            "period_to": parsed.header.period_to,
            "owner": parsed.header.owner,
            "transactions_in_file": len(parsed.transactions),
            "new_added": new_count,
            "total_in_system": len(store["transactions"]),
        })
        save_history(history)

    except Exception as e:
        flash(f"Ошибка парсинга: {e}")
    finally:
        os.unlink(tmp_path)

    return redirect(url_for("index"))


@app.route("/api/history", methods=["GET"])
def api_history():
    return jsonify(load_history())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
