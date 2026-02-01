import os
import json
import tempfile
from datetime import datetime
from dataclasses import asdict

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session

from parser import parse_ozon_bank_pdf, BankStatement, StatementHeader, StatementFooter, Transaction

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pzk-secret-key-change-me")

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "changeme")


@app.before_request
def require_login():
    if request.endpoint in ("login",) or request.path.startswith("/static"):
        return
    if not session.get("authenticated"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == SITE_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        flash("Неверный пароль")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

ALLOWED_EXTENSIONS = {"pdf"}
DATA_DIR = os.path.dirname(__file__)
HISTORY_FILE = os.path.join(DATA_DIR, "upload_history.json")
STORE_FILE = os.path.join(DATA_DIR, "transactions_store.json")
COMMENTS_FILE = os.path.join(DATA_DIR, "comments.json")
TAGS_FILE = os.path.join(DATA_DIR, "tags.json")


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


def auto_tag_transactions(store: dict):
    """Автоматически проставляет теги на основе описания транзакций."""
    tags_data = load_tags()
    doc_tags = tags_data.get("doc_tags", {})
    changed = False

    for doc_id, txn in store.get("transactions", {}).items():
        desc = txn.get("description", "")
        # Автотег Boosty для поступлений с "Boosty" в описании
        if txn.get("is_credit") and "boosty" in desc.lower():
            current_tags = doc_tags.get(doc_id, [])
            if "Boosty" not in current_tags:
                doc_tags[doc_id] = current_tags + ["Boosty"]
                changed = True

    if changed:
        tags_data["doc_tags"] = doc_tags
        all_tags = set(tags_data.get("all_tags", []))
        for tag_list in doc_tags.values():
            all_tags.update(tag_list)
        tags_data["all_tags"] = sorted(all_tags)
        save_tags(tags_data)


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

        # Автоматически проставляем теги по описанию
        auto_tag_transactions(store)

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


# === Комментарии (серверное хранилище) ===

def load_comments() -> dict:
    if os.path.exists(COMMENTS_FILE):
        with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_comments(data: dict):
    with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/comments", methods=["GET"])
def api_get_comments():
    return jsonify(load_comments())


@app.route("/api/comments", methods=["POST"])
def api_save_comment():
    body = request.get_json(force=True)
    doc_id = body.get("doc_id", "")
    comment = body.get("comment", "")
    comments = load_comments()
    if comment:
        comments[doc_id] = comment
    else:
        comments.pop(doc_id, None)
    save_comments(comments)
    return jsonify({"ok": True})


# === Теги (серверное хранилище) ===

def load_tags() -> dict:
    if os.path.exists(TAGS_FILE):
        with open(TAGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"doc_tags": {}, "all_tags": []}


def save_tags(data: dict):
    with open(TAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/tags", methods=["GET"])
def api_get_tags():
    return jsonify(load_tags())


@app.route("/api/tags", methods=["POST"])
def api_save_tags():
    body = request.get_json(force=True)
    doc_id = body.get("doc_id", "")
    tags = body.get("tags", [])
    data = load_tags()
    if tags:
        data["doc_tags"][doc_id] = tags
    else:
        data["doc_tags"].pop(doc_id, None)
    # Update all_tags list
    all_tags = set(data.get("all_tags", []))
    for tag_list in data["doc_tags"].values():
        all_tags.update(tag_list)
    data["all_tags"] = sorted(all_tags)
    save_tags(data)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
